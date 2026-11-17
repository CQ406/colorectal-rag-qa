/**
 * 问答会话状态管理。
 *
 * 设计要点
 * --------
 * 1. **流式状态机**：idle → retrieving → generating → done/error/aborted
 *    前端需区分「正在检索证据」与「正在生成答案」，
 *    因为首字延迟约 3~5 秒，必须给用户明确的进度反馈而非空白等待。
 *
 * 2. **引用先行到达**：后端在 `retrieval` 事件中先发送证据列表，
 *    答案随后逐字到达，因此引用面板可立即渲染。
 *
 * 3. **流 ID 守卫（关键）**：每个流有唯一 id，回调只在该 id 仍是
 *    「当前流」时才写入状态。这防止了已废弃流的回调污染新消息 ——
 *    实际踩过的 bug：在生成中点「重新生成」，旧流的回调仍持有
 *    已被移出数组的消息对象引用，导致内容残缺、引用面板空白。
 *
 * 4. **可中止**：保存流的 abort 函数，用户可停止生成。
 */

import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

import { api, streamQa } from '@/api/client'
import type { CitationItem, QaParams } from '@/api/types'

/** 生成状态机 */
export type GenerationStatus =
  | 'idle'
  | 'retrieving'
  | 'generating'
  | 'done'
  | 'error'
  | 'aborted'

/** 一条会话消息 */
export interface ChatMessage {
  id: string
  role: 'user' | 'assistant'
  content: string
  citations: CitationItem[]
  /** LLM 引用了但不存在的编号（幻觉信号） */
  invalidCitations: number[]
  /**
   * 答案中出现但检索证据中找不到的数值。
   *
   * 为什么单独记录：阶段十实测发现最危险的幻觉是**编造精确数值**
   * （如证据中没有的「中转率 7.1% vs 3.4%」）。在提示词中明确禁止后
   * 重测，数值编造占比不降反升（22%→26%），说明该问题无法靠提示词解决。
   * 因此后端在生成后独立核验，前端必须显式呈现给用户。
   */
  unsupportedNumbers: string[]
  /** 无据数值的提示语 */
  numericWarning: string
  status: GenerationStatus
  errorMessage?: string
  /** 各阶段耗时(毫秒) */
  timings?: Record<string, number>
  /** 召回证据条数（来自 retrieval 事件） */
  evidenceCount?: number
  model?: string
  /** 该条目是否为「无证据」提示而非真实答案 */
  noEvidence?: boolean
}

/** 问答参数（用户可在界面上调整） */
export interface QaOptions {
  phase: string | null
  topK: number
  useRerank: boolean
  useEvidenceBoost: boolean
  recallMode: string
}

let messageSeq = 0
function nextId(prefix: string): string {
  messageSeq += 1
  return `${prefix}-${Date.now()}-${messageSeq}`
}

/** 轻量日志（带统一前缀，便于在控制台过滤） */
const logger = {
  warn: (...args: unknown[]) => console.warn('[chat]', ...args),
  info: (...args: unknown[]) => console.info('[chat]', ...args),
}

/** 句末标点（中英文） */
const SENTENCE_ENDINGS = ['。', '！', '？', '.', '!', '?', '；', ';', '”', '）', ')']

/**
 * 判断文本是否以句末标点结尾。
 *
 * 用途：截断的答案通常停在句子中间（无句末标点），
 * 这是比长度比对更鲁棒的截断信号 —— 长度差异可能是模型重排所致，
 * 而句末缺失几乎总意味着内容不完整。
 */
function endsWithSentencePunctuation(text: string): boolean {
  const trimmed = text.trimEnd()
  if (!trimmed) return true
  return SENTENCE_ENDINGS.includes(trimmed.slice(-1))
}

export const useChatStore = defineStore('chat', () => {
  /* ----------------------------- 状态 ----------------------------- */
  const messages = ref<ChatMessage[]>([])
  const status = ref<GenerationStatus>('idle')
  const options = ref<QaOptions>({
    phase: null,
    topK: 5,
    useRerank: true,
    useEvidenceBoost: true,
    recallMode: 'hybrid',
  })

  /** 当前流的唯一 id（0 表示无活动流） */
  const activeStreamId = ref(0)
  /** 当前流的中止函数 */
  let abortStream: (() => void) | null = null

  /** 最近一次检测到的传输不完整警告（供界面提示） */
  const truncatedWarning = ref('')
  /** 是否正在执行非流式补偿 */
  const repairing = ref(false)

  /** 分配新的流 id */
  function beginStream(): number {
    activeStreamId.value += 1
    return activeStreamId.value
  }

  /** 判断某个流是否仍是当前活动流（防止废弃流回调污染状态） */
  function isCurrentStream(streamId: number): boolean {
    return activeStreamId.value === streamId
  }

  /** 中止当前流并标记对应消息 */
  function stopActiveStream(): void {
    if (abortStream) {
      abortStream()
      abortStream = null
    }
    // 使所有在途流的回调失效
    activeStreamId.value += 1
  }

  /**
   * 非流式补偿：重新获取完整答案并替换消息内容。
   *
   * 为什么需要：实测 Vite 开发服务器的 SSE 代理会丢失事件
   * （直连 607 delta / 1053 字符 vs 经代理 573 delta / 952 字符），
   * 导致界面展示残缺的医学答案。非流式接口不经过 SSE，
   * 响应体完整性有保障，因此作为兜底手段。
   *
   * 代价：多一次 LLM 调用（约 3~8 秒、少量 token）。
   * 权衡：医学场景下内容正确性优先于成本。
   *
   * ⚠️ 必须按 id 从响应式数组取回对象再改（见 ask() 中的说明）。
   */
  async function repairAnswer(
    messageId: string,
    streamId: number,
    params: QaParams,
  ): Promise<void> {
    if (!isCurrentStream(streamId)) return
    repairing.value = true
    logger.info('开始非流式补偿，重新获取完整答案…')

    /** 按 id 取回响应式代理对象 */
    const pick = (): ChatMessage | undefined =>
      messages.value.find((m) => m.id === messageId)

    try {
      const result = await api.qa(params)
      // 补偿期间用户可能已发起新一轮提问，需再次校验
      if (!isCurrentStream(streamId)) return
      const target = pick()
      if (!target) return

      target.content = result.answer
      // 同理：非流式补偿若没带回引用，保留流式阶段已渲染的引用，避免面板被清空
      if (Array.isArray(result.citations) && result.citations.length > 0) {
        target.citations = result.citations
      }
      target.invalidCitations = result.invalid_citations ?? target.invalidCitations
      target.unsupportedNumbers = result.unsupported_numbers ?? []
      target.numericWarning = result.numeric_warning ?? ''
      target.model = result.model

      if (!endsWithSentencePunctuation(result.answer)) {
        // 连非流式答案都不完整，说明是上游模型侧问题，明确告知用户
        truncatedWarning.value =
          `答案可能不完整（${result.answer.length} 字符，未以句末标点结束）。` +
          `这通常由模型生成长度上限导致，建议缩小问题范围或重新生成。`
        logger.warn('非流式答案仍不完整：', result.answer.length)
      } else {
        logger.info(`补偿成功，答案 ${result.answer.length} 字符`)
      }
    } catch (err) {
      if (!isCurrentStream(streamId)) return
      truncatedWarning.value =
        `答案传输不完整，且自动补全失败：${(err as Error).message}。请点击「重新生成」。`
      logger.warn('补偿失败：', err)
    } finally {
      repairing.value = false
    }
  }

  /* ----------------------------- 计算属性 ----------------------------- */
  const isBusy = computed(
    () => status.value === 'retrieving' || status.value === 'generating',
  )

  /** 正在生成的助手消息（用于实时渲染） */
  const streamingMessage = computed(() =>
    messages.value.find(
      (m) =>
        m.role === 'assistant' &&
        (m.status === 'retrieving' || m.status === 'generating'),
    ),
  )

  /** 状态文案：给用户明确的进度反馈 */
  const statusText = computed(() => {
    switch (status.value) {
      case 'retrieving':
        return '正在检索知识库证据…'
      case 'generating':
        return '正在推理并组织答案…'
      default:
        return ''
    }
  })

  /* ----------------------------- 动作 ----------------------------- */
  /** 发送提问 */
  function ask(question: string, opts: { appendUserMessage?: boolean } = {}): void {
    const text = question.trim()
    if (!text) return

    // 并发保护：生成中不接受新提问。
    // 判定必须放在**任何状态修改之前**，否则会 push 进用户消息
    // 却无法给出回答，界面上留下「有提问无回答」的孤儿消息。
    if (isBusy.value) return

    // 用户消息（regenerate 时已有，不重复插入）
    if (opts.appendUserMessage !== false) messages.value.push({
      id: nextId('u'),
      role: 'user',
      content: text,
      citations: [],
      invalidCitations: [],
      unsupportedNumbers: [],
      numericWarning: '',
      status: 'done',
    })

    // 助手消息占位（立即插入，便于渲染进度状态）
    //
    // ⚠️ 关键：push 之后必须**从数组中取回**对象再操作。
    // Vue 3 的 ref 数组会把 push 进去的对象包装为**响应式代理**，
    // 而局部变量仍指向**原始对象** —— 修改原始对象不会触发任何更新，
    // 界面将永远停留在第一次渲染的内容上。
    //
    // 这个坑的表现极具迷惑性：数据层完全正确（content 是完整答案），
    // 但界面只显示首个流式片段，且控制台无任何报错。
    // 实测确认：messages.value[i] === 原始对象 为 false。
    messages.value.push({
      id: nextId('a'),
      role: 'assistant',
      content: '',
      citations: [],
      invalidCitations: [],
      unsupportedNumbers: [],
      numericWarning: '',
      status: 'retrieving',
      evidenceCount: 0,
    })
    // 取回响应式代理
    const assistant = messages.value[messages.value.length - 1]

    status.value = 'retrieving'
    const streamId = beginStream()

    const params: QaParams = {
      question: text,
      phase: options.value.phase,
      top_k: options.value.topK,
      recall_mode: options.value.recallMode,
      use_rerank: options.value.useRerank,
      use_evidence_boost: options.value.useEvidenceBoost,
    }

    abortStream = streamQa(params, {
      onRetrieval(data) {
        // 废弃流的回调必须忽略，否则会污染新一轮的引用面板
        if (!isCurrentStream(streamId)) return
        status.value = 'generating'
        assistant.status = 'generating'
        assistant.evidenceCount = data.count
        // 引用元数据先于答案到达，立即渲染引用面板
        assistant.citations = data.evidences.map((ev) => ({
          ...ev,
          positions: [],
        })) as CitationItem[]
      },

      onDelta(text) {
        if (!isCurrentStream(streamId)) return
        assistant.content += text
      },

      onDone(data) {
        if (!isCurrentStream(streamId)) return
        status.value = 'done'
        assistant.status = 'done'

        const accumulated = assistant.content
        const finalAnswer = data.answer ?? ''

        assistant.content = finalAnswer
        // 引用赋值要防「被空值覆盖」
        //
        // 背景：`retrieval` 事件先到，已把证据写入 citations；
        // `done` 事件再到，理应携带同一批引用。但实测存在 done 的
        // citations 缺失或为空的情形（SSE 代理截断、上游只回部分字段等），
        // 此时若直接赋值会让右侧引用面板从「已显示」变回「未引用任何文献」，
        // 而正文里的 [1][2] 标记仍在 —— 用户看到的就是「有引用标记但没引用列表」。
        // 因此：只有拿到非空数组才覆盖，否则保留 retrieval 阶段的结果。
        const incomingCitations = Array.isArray(data.citations) ? data.citations : []
        if (incomingCitations.length > 0) {
          assistant.citations = incomingCitations
        }
        assistant.invalidCitations = data.invalid_citations ?? assistant.invalidCitations
        // 数值主张核验结果（后端独立于提示词的校验层）
        assistant.unsupportedNumbers = data.unsupported_numbers ?? []
        assistant.numericWarning = data.numeric_warning ?? ''
        assistant.timings = { total_ms: data.elapsed_ms }
        abortStream = null

        // 完整性校验与自动补偿
        //
        // 背景：实测 Vite 开发服务器的 SSE 代理会丢事件 —— 同一问题
        // 直连后端收到 607 个 delta（1053 字符），经代理只收到 573 个
        // （952 字符），约 10% 内容丢失，且不报错。
        //
        // 因此这里做两道检查：
        //   ① 长度一致性：本地累积 vs 服务端答案
        //   ② 句末完整性：答案是否以句末标点结尾（截断的典型特征）
        //
        // 任一不满足即调用**非流式接口**（不经过 SSE，因此可靠）重新获取
        // 权威答案并替换。这是「流式体验」与「内容正确性」的权衡 ——
        // 医学答案丢一个字即可能改变语义，正确性优先。
        const lengthMismatch = accumulated.length > 0 && accumulated.length !== finalAnswer.length
        const looksTruncated = !endsWithSentencePunctuation(finalAnswer)

        if (lengthMismatch || looksTruncated) {
          logger.warn(
            `[完整性] 长度差异=${lengthMismatch}(${accumulated.length}/${finalAnswer.length}) ` +
            `句末异常=${looksTruncated}，触发非流式补偿`,
          )
          void repairAnswer(assistant.id, streamId, params)
        }
      },

      onError(message, reason) {
        if (!isCurrentStream(streamId)) return
        status.value = 'error'
        assistant.status = 'error'
        assistant.errorMessage = message
        assistant.noEvidence =
          reason === 'no_evidence' || reason === 'low_evidence'
        // 无证据时保留已到达的证据列表，供用户查看实际召回了什么
        abortStream = null
      },
    })
  }

  /** 中止当前生成 */
  function abort(): void {
    // 先标记被中止的消息，再使流回调失效
    const target = streamingMessage.value
    stopActiveStream()
    if (target) {
      target.status = 'aborted'
      if (!target.content) target.content = '（已停止生成）'
    }
    status.value = 'aborted'
  }

  /** 清空会话 */
  function clear(): void {
    stopActiveStream()
    messages.value = []
    status.value = 'idle'
  }

  /**
   * 重新生成最后一条回答。
   *
   * 关键：必须先中止在途流。否则旧流的回调仍持有
   * 已被 splice 移出数组的消息对象引用，会写入一个界面上看不到的对象，
   * 造成「内容残缺 + 引用面板空白」（实际踩过的 bug）。
   */
  function regenerate(): void {
    const lastUser = [...messages.value].reverse().find((m) => m.role === 'user')
    if (!lastUser) return

    stopActiveStream()
    status.value = 'idle'

    // 移除最后一条助手消息后重问
    const lastAssistantIndex = messages.value
      .map((m) => m.role)
      .lastIndexOf('assistant')
    if (lastAssistantIndex >= 0) messages.value.splice(lastAssistantIndex, 1)

    // appendUserMessage:false —— 否则对话里会多出一条一模一样的用户提问
    ask(lastUser.content, { appendUserMessage: false })
  }

  /** 清除传输不完整警告 */
  function clearTruncatedWarning(): void {
    truncatedWarning.value = ''
  }

  return {
    messages,
    status,
    options,
    isBusy,
    streamingMessage,
    statusText,
    truncatedWarning,
    repairing,
    ask,
    abort,
    clear,
    regenerate,
    clearTruncatedWarning,
  }
})
