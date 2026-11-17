<script setup lang="ts">
/**
 * 多轮会诊对话。
 *
 * 对应报审表「（二）5 多轮对话管理模块」：
 * 支持上下文感知的多轮对话，理解用户追问意图并保持对话连贯性。
 *
 * 与「智能问答」页的区别
 * ---------------------
 * 智能问答页（/qa）走 `/api/qa/stream`：无状态单轮、流式输出，
 * 适合快速查询；多轮会诊页走 `/api/chat`：有状态会话、
 * 自动进行**查询改写**（解析指代）与**意图识别**（分期路由）。
 *
 * 本页刻意把系统的"理解过程"显示出来：
 *   - 改写后的检索查询（证明系统解析了"它""那"等指代）
 *   - 识别出的围术期分期与问题类型
 * 这既方便验证上下文感知是否生效，也便于答辩演示。
 */
import { computed, nextTick, onMounted, onBeforeUnmount, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'

import { api, streamChat } from '@/api/client'
import CitationPanel from '@/components/CitationPanel.vue'
import EvidenceWorkspace from '@/components/EvidenceWorkspace.vue'
import { renderAnswer } from '@/utils/markdown'
import { useAuthStore } from '@/stores/auth'

const workspace = ref<InstanceType<typeof EvidenceWorkspace> | null>(null)
const auth = useAuthStore()
const ownerScope = computed(() => auth.user?.id || 'guest')
let requestScope = 0

/* ----------------------------- 类型 ----------------------------- */
interface Turn {
  id: number
  role: 'user' | 'assistant'
  content: string
  citations: any[]
  invalidCitations: number[]
  numericWarning: string
  /* 系统理解过程 */
  rewrittenQuery: string
  rewritten: boolean
  phase: string
  intent: string
  confidence: number
  evidenceCount: number
  timings: Record<string, number>
  status: 'done' | 'error' | 'aborted'
  errorMessage?: string
}

const PHASE_LABELS: Record<string, string> = {
  preop: '术前', intraop: '术中', postop: '术后', general: '通用',
}

const INTENT_LABELS: Record<string, string> = {
  knowledge: '知识提问', advice: '决策建议', compare: '比较类', summary: '总结类',
  // 「你是谁」「你能做什么」这类不检索的指令类问题
  meta: '助手指令',
}

/* ----------------------------- 状态 ----------------------------- */
const turns = ref<Turn[]>([])
const inputText = ref('')
const busy = ref(false)
const sessionId = ref<string>('')
const scrollRef = ref<HTMLElement | null>(null)
const inputRef = ref<any>(null)

/** 当前流式请求的中止函数（用于「新会话」时中断上一次生成） */
let abortChat: (() => void) | null = null

/**
 * 本轮 `await new Promise(...)` 的 resolve。
 *
 * 为什么必须显式保存：`ssePost()` 在 `AbortError` 时只是静默返回、
 * 不会回调 `onDone`/`onError`，那个 Promise 就永远不会落地，
 * `busy` 一直是 true ——「停止生成」后按钮会卡在「推理中」再也点不动。
 * 这里由停止流程主动 resolve。
 */
let pendingResolve: (() => void) | null = null

/** 右侧面板中高亮的引用编号：点击正文 [n] 后定位到对应卡片 */
const activeCiteIndex = ref<number | null>(null)

let seq = 0
const nextId = () => ++seq

/** 最近一条助手消息的引用（供右侧面板展示） */
const activeTurnId = ref<number | null>(null)
watch(() => turns.value.length, () => { activeTurnId.value = null; activeCiteIndex.value = null })
const currentCitations = computed(() => {
  const chosen = turns.value.find(t => t.id === activeTurnId.value)
  if (chosen) return chosen.citations
  const list = turns.value.filter((t) => t.role === 'assistant')
  return list[list.length - 1]?.citations ?? []
})

/** 会话轮数 */
const turnCount = computed(() => turns.value.filter((t) => t.role === 'user').length)

function rendered(turn: Turn): string {
  return renderAnswer(turn.content)
}

/* ----------------------------- 发送 ----------------------------- */
function onInputKeydown(event: KeyboardEvent) {
  if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) { event.preventDefault(); void send() }
}
async function send() {
  const text = inputText.value.trim()
  if (!text || busy.value) return
  const scope = ++requestScope

  turns.value.push({
    id: nextId(), role: 'user', content: text, citations: [],
    invalidCitations: [], numericWarning: '', rewrittenQuery: '',
    rewritten: false, phase: '', intent: '', confidence: 0,
    evidenceCount: 0, timings: {}, status: 'done',
  })
  inputText.value = ''
  busy.value = true
  await scrollToBottom()

  const placeholder: Turn = {
    id: nextId(), role: 'assistant', content: '', citations: [],
    invalidCitations: [], numericWarning: '', rewrittenQuery: '',
    rewritten: false, phase: '', intent: '', confidence: 0,
    evidenceCount: 0, timings: {}, status: 'done',
  }
  turns.value.push(placeholder)
  // 注意：必须从数组取回响应式代理后再修改，
  // 否则修改的是原始对象，界面不会更新（Vue 3 ref 数组的经典陷阱）
  const target = turns.value[turns.value.length - 1]
  await scrollToBottom()

  try {
    // 流式问答：正文逐字显示，引用在检索完成时即到达
    await new Promise<void>((resolve) => {
      // 保存 resolve：用户点「停止生成」时中止的是 fetch，
      // 不会触发 onDone/onError，需要由停止流程主动让它落地。
      pendingResolve = resolve
      abortChat = streamChat(
        { question: text, session_id: sessionId.value || null, top_k: 5 },
        {
          onMeta(data) {
            if (scope !== requestScope) return
            // 会话与路由信息先于检索到达，立即展示「理解过程」
            sessionId.value = data.session_id
            // 立刻持久化：即使生成中途切走页面，回来也能恢复会话
            try {
              localStorage.setItem(sessionKey(), data.session_id)
            } catch {
              /* 隐私模式下不可用，忽略 */
            }
            target.phase = data.phase
            target.intent = data.intent
            target.confidence = data.intent_confidence
            target.rewrittenQuery = data.rewritten_query
            target.rewritten = data.rewritten
          },

          onRetrieval(data) {
            if (scope !== requestScope) return
            target.evidenceCount = data.count
            // 引用元数据先于答案到达，立即渲染右侧面板
            target.citations = data.evidences.map((ev) => ({
              ...ev,
              positions: [],
            }))
          },

          onDelta(chunk) {
            if (scope !== requestScope) return
            target.content += chunk
            void scrollToBottom()
          },

          onDone(data) {
            if (scope !== requestScope) { resolve(); return }
            const finalAnswer = data.answer ?? ''
            if (finalAnswer) target.content = finalAnswer
            // 只有拿到非空引用才覆盖，避免把检索阶段的结果清空
            if (Array.isArray(data.citations) && data.citations.length > 0) {
              target.citations = data.citations
            }
            target.invalidCitations = data.invalid_citations ?? []
            target.numericWarning = data.numeric_warning ?? ''
            target.timings = data.timings ?? {}
            if (data.session_id) sessionId.value = data.session_id
            target.status = 'done'
            resolve()
          },

          onError(message) {
            if (scope !== requestScope) { resolve(); return }
            // 一律标记为出错，即使已经收到部分内容。
            //
            // 此前是「有内容就当成功」：LLM 在中途故障、已输出若干 token 时，
            // 状态会被置为 done —— 页面不显示失败、底栏还显示「引用 N 条」，
            // 用户会把残缺答案当成完整答案。医学场景下这条必须往保守方向偏。
            // 已收到的内容保留展示，只是同时给出错误提示。
            target.status = 'error'
            target.errorMessage = message
            if (!target.content) ElMessage.warning(message)
            resolve()
          },
        },
      )
    })
  } finally {
    if (scope === requestScope) {
      abortChat = null
      pendingResolve = null
      busy.value = false
      await scrollToBottom()
    }
  }
}

/**
 * 停止生成。
 *
 * 中止 fetch 后 `ssePost()` 不会回调 onDone/onError，因此这里需要：
 *   ① 调 abort 中断网络请求
 *   ② 把当前助手消息标为已停止，保留已收到的增量内容
 *   ③ 主动 resolve 那个 await，否则 busy 永远为 true，按钮卡住
 * 已生成的部分保留，不丢弃 —— 医学回答看一半也比没有强。
 */
function stopGeneration(): void {
  const last = turns.value[turns.value.length - 1]
  if (abortChat) {
    abortChat()
    abortChat = null
  }
  if (last && last.role === 'assistant') {
    if (!last.content) {
      last.content = '（已停止生成）'
    }
    last.status = 'aborted'
  }
  pendingResolve?.()
  pendingResolve = null
  busy.value = false
  ElMessage.info('已停止生成')
}

async function scrollToBottom() {
  await nextTick()
  const el = scrollRef.value
  if (el) el.scrollTop = el.scrollHeight
}

/* ----------------------------- 会话操作 ----------------------------- */
async function clearSession() {
  requestScope++
  const scope = requestScope
  const sid = sessionId.value
  const key = sessionKey()
  // 若正在生成，先中断，否则旧流的增量会继续写入已清空的列表
  if (abortChat) {
    abortChat()
    abortChat = null
  }
  // 同样要让挂起的 await 落地，否则 finally 不执行、busy 不复位
  pendingResolve?.()
  pendingResolve = null
  if (sid) {
    try {
      await api.clearConversation(sid)
    } catch {
      /* 会话可能已过期，忽略 */
    }
  }
  if (scope !== requestScope) return
  turns.value = []
  sessionId.value = ''
  busy.value = false
  // 同时清掉持久化的会话 ID，否则返回本页会把刚清空的会话又拉回来
  try {
    localStorage.removeItem(key)
  } catch {
    /* 隐私模式下不可用，忽略 */
  }
  ElMessage.success('已开始新会话')
}

/** 示例问题：演示多轮追问 */
const examples = [
  '结直肠癌术后吻合口漏怎么处理？',
  '加速康复外科的核心措施有哪些？',
  '术中如何评估肠道吻合口的血供？',
]

/** 追问示例（演示指代解析） */
const followups = [
  '它的发生率是多少？',
  '那该怎么预防？',
  '这个证据等级高吗？',
]

function useExample(q: string) {
  inputText.value = q
  inputRef.value?.focus()
}

function onCiteClick(e: MouseEvent, turn: Turn) {
  const cite = (e.target as HTMLElement).closest('.cite') as HTMLElement | null
  if (!cite) return
  const idx = Number(cite.dataset.cite)
  const found = turn.citations.find((c) => c.index === idx)
  if (!found) return

  // 定位到右侧引用面板的对应卡片并高亮。
  //
  // 早先这里用 ElMessage 弹出 HTML 卡片，缺点是：
  //   ① 弹窗会遮住回答正文，连点两处会叠两个浮层；
  //   ② 消息 8 秒后自动消失，想对照原文时已经没了；
  //   ③ 与「智能问答」页的交互不一致（那里是面板定位）。
  // 改为面板定位后，点击只是把右侧卡片滚动到视野并标为当前项，
  // 不影响阅读正文。
  activeTurnId.value = turn.id
  activeCiteIndex.value = idx
  workspace.value?.show()
  nextTick(() => {
    const el = document.querySelector('.citation-item.is-active')
    el?.scrollIntoView({ behavior: 'smooth', block: 'nearest' })
  })
}

// Only the ID is cached locally; the backend enforces ownership of all history.
const sessionKey = () => `cr.chat.session_id.${ownerScope.value}`

function resetOwner() {
  requestScope++
  abortChat?.()
  abortChat = null
  pendingResolve?.()
  pendingResolve = null
  turns.value = []
  sessionId.value = ''
  inputText.value = ''
  activeCiteIndex.value = null
  busy.value = false
}

watch(ownerScope, () => { resetOwner(); void restoreSession() })
onBeforeUnmount(resetOwner)

onMounted(() => {
  inputRef.value?.focus()
  void restoreSession()
})

/** 从后端恢复上一次的会话（若存在） */
async function restoreSession(): Promise<void> {
  const scope = requestScope
  const key = sessionKey()
  let sid = ''
  try {
    sid = localStorage.getItem(key) || ''
  } catch {
    return // 隐私模式下 sessionStorage 可能不可用，忽略即可
  }
  if (!sid) return

  try {
    const d = await api.conversation(sid)
    if (scope !== requestScope) return
    const list: Turn[] = []
    let n = 0
    for (const t of d.turns || []) {
      list.push({
        id: ++n, role: 'user', content: t.question, citations: [],
        invalidCitations: [], numericWarning: '', rewrittenQuery: '',
        rewritten: false, phase: '', intent: '', confidence: 0,
        evidenceCount: 0, timings: {}, status: 'done',
      })
      list.push({
        id: ++n, role: 'assistant', content: t.answer || '',
        // 历史里已存引用明细，恢复后右侧面板可正常显示（含展开原文）
        citations: Array.isArray(t.citations) ? t.citations : [],
        invalidCitations: [], numericWarning: '',
        rewrittenQuery: t.rewritten_query || '',
        rewritten: !!t.rewritten_query,
        phase: t.phase || 'general',
        intent: t.intent || 'knowledge',
        confidence: 0,
        evidenceCount: (t.citations && t.citations.length) || t.citations_count || 0,
        timings: {}, status: 'done',
      })
    }
    if (list.length) {
      turns.value = list
      sessionId.value = sid
      seq = n
      await scrollToBottom()
    }
  } catch {
    // 会话已过期（后端重启或超过 TTL）——清掉记录，避免每次进来都请求失败
    try {
      if (scope === requestScope) localStorage.removeItem(key)
    } catch {
      /* 忽略 */
    }
  }
}
</script>

<template>
  <EvidenceWorkspace ref="workspace" :count="currentCitations.length">
  <div class="chat-layout">
    <div class="chat-column">
      <!-- 工具栏 -->
      <div class="chat-toolbar">
        <div class="toolbar-left">
          <el-tag size="small" type="success" effect="plain">
            <el-icon><ChatLineSquare /></el-icon>
            多轮会诊
          </el-tag>
          <el-tag v-if="sessionId" size="small" type="info" effect="plain">
            会话 {{ sessionId.slice(0, 8) }} · {{ turnCount }} 轮
          </el-tag>
        </div>
        <div class="toolbar-right">
          <el-button link size="small" :disabled="!turns.length" @click="clearSession">
            <el-icon><Refresh /></el-icon>
            新会话
          </el-button>
        </div>
      </div>

      <!-- 消息区 -->
      <div ref="scrollRef" class="chat-scroll">
        <!-- 空状态 -->
        <div v-if="!turns.length" class="chat-empty">
          <div class="empty-logo">
            <el-icon><ChatLineSquare /></el-icon>
          </div>
          <div class="empty-title">多轮会诊对话</div>
          <div class="empty-desc">
            支持上下文感知的连续追问。系统会自动解析「它」「那」等指代，
            并结合对话历史改写检索查询。
          </div>

          <div class="example-group">
            <div class="example-title">试试这样开始</div>
            <div
              v-for="q in examples"
              :key="q"
              class="example-item"
              @click="useExample(q)"
            >
              {{ q }}
            </div>
          </div>

          <div class="example-group">
            <div class="example-title">发送后再追问（演示指代解析）</div>
            <div
              v-for="q in followups"
              :key="q"
              class="example-item followup"
              @click="useExample(q)"
            >
              {{ q }}
            </div>
          </div>
        </div>

        <!-- 对话轮次 -->
        <div
          v-for="turn in turns"
          :key="turn.id"
          class="msg-row"
          :class="turn.role"
        >
          <div class="avatar" :class="turn.role">
            <el-icon v-if="turn.role === 'user'"><User /></el-icon>
            <el-icon v-else><MagicStick /></el-icon>
          </div>

          <div class="bubble-wrap">
            <!-- 用户消息 -->
            <div v-if="turn.role === 'user'" class="bubble user">
              {{ turn.content }}
            </div>

            <!-- 助手消息 -->
            <template v-else>
              <!-- 系统理解过程：只在确实是追问且改写了时展示 -->
              <div v-if="turn.rewritten" class="reasoning">
                <div class="reasoning-row">
                  <el-icon><Refresh /></el-icon>
                  <span class="reasoning-label">指代解析</span>
                  <span class="reasoning-value">{{ turn.rewrittenQuery }}</span>
                </div>
              </div>
              <div v-if="turn.phase || turn.intent" class="reasoning">
                <div class="reasoning-row">
                  <el-icon><Aim /></el-icon>
                  <span class="reasoning-label">意图识别</span>
                  <span class="reasoning-value">
                    {{ PHASE_LABELS[turn.phase] || turn.phase }} ·
                    {{ INTENT_LABELS[turn.intent] || turn.intent }}
                    <template v-if="turn.confidence">
                      （置信度 {{ turn.confidence.toFixed(2) }}）
                    </template>
                  </span>
                </div>
              </div>

              <!-- 等待中 -->
              <div v-if="!turn.content && turn.status === 'done'" class="thinking">
                <span class="thinking-dots"><i /><i /><i /></span>
                <span>正在检索知识库并推理…</span>
              </div>

              <!-- 答案 -->
              <div
                v-if="turn.content"
                class="bubble assistant md"
                v-html="rendered(turn)"
                @click="onCiteClick($event, turn)"
              />

              <!-- 数值需核实 -->
              <el-alert
                v-if="turn.numericWarning"
                type="warning"
                :closable="false"
                show-icon
                class="numeric-alert"
              >
                <template #title>数值需核实</template>
                {{ turn.numericWarning }}
              </el-alert>

              <!-- 错误 -->
              <el-alert
                v-if="turn.status === 'error'"
                type="error"
                :closable="false"
                show-icon
              >
                <template #title>生成失败</template>
                {{ turn.errorMessage }}
              </el-alert>

              <!-- 底部信息 -->
              <div v-if="turn.content" class="msg-footer">
                <span class="footer-stat">
                  <el-icon><Link /></el-icon>
                  引用 {{ turn.citations.length }} 条
                </span>
                <span v-if="turn.evidenceCount" class="footer-stat">
                  <el-icon><Document /></el-icon>
                  证据 {{ turn.evidenceCount }} 条
                </span>
                <span v-if="turn.timings?.total_ms" class="footer-stat">
                  <el-icon><Timer /></el-icon>
                  {{ (turn.timings.total_ms / 1000).toFixed(1) }}s
                </span>
                <span v-if="turn.invalidCitations.length" class="footer-stat warn">
                  剔除幻觉引用 {{ turn.invalidCitations.length }} 处
                </span>
              </div>
            </template>
          </div>
        </div>
      </div>

      <!-- 输入区 -->
      <div class="chat-input">
        <el-input
          ref="inputRef"
          v-model="inputText"
          type="textarea"
          :rows="2"
          resize="none"
          maxlength="2000"
          show-word-limit
          :disabled="busy"
          placeholder="输入临床问题…（Enter 发送，Shift+Enter 换行）"
          @keydown="onInputKeydown"
        />
        <div class="input-actions">
          <el-button
            size="small"
            :disabled="busy || !turns.length"
            @click="clearSession"
          >
            <el-icon><Refresh /></el-icon>
            新会话
          </el-button>
          <div class="spacer" />
          <!-- 生成中显示「停止生成」，否则显示「发送」 -->
          <el-button
            v-if="busy"
            type="danger"
            plain
            size="small"
            @click="stopGeneration"
          >
            <el-icon><CircleClose /></el-icon>
            停止生成
          </el-button>
          <el-button
            v-else
            type="primary"
            size="small"
            :disabled="!inputText.trim()"
            @click="send"
          >
            <el-icon><Promotion /></el-icon>
            发送
          </el-button>
        </div>
      </div>
    </div>

  </div>
  <template #evidence>
      <CitationPanel :citations="currentCitations" :active-index="activeCiteIndex" :loading="busy" :has-question="turns.length > 0" :failed="turns.filter(t => t.role === 'assistant').at(-1)?.status === 'error'" @locate="activeCiteIndex = $event" />
  </template>
  </EvidenceWorkspace>
</template>

<style scoped>
.chat-layout {
  display: flex;
  height: 100%;
  overflow: hidden;
}

.chat-column {
  display: flex;
  flex: 1;
  min-width: 0;
  flex-direction: column;
}

/* ---------- 工具栏 ---------- */
.chat-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 9px 18px;
  border-bottom: 1px solid var(--cr-border);
  flex-shrink: 0;
}

.toolbar-left,
.toolbar-right {
  display: flex;
  align-items: center;
  gap: 8px;
}

/* ---------- 滚动区 ---------- */
.chat-scroll {
  flex: 1;
  padding: 16px 20px;
  overflow-y: auto;
}

/* ---------- 空状态 ---------- */
.chat-empty {
  max-width: 520px;
  margin: 48px auto 0;
  text-align: center;
}

.empty-logo {
  font-size: 34px;
  color: var(--cr-primary);
  opacity: 0.35;
}

.empty-title {
  margin-top: 8px;
  font-size: 16px;
  font-weight: 700;
  color: #2b3a47;
}

.empty-desc {
  margin-top: 7px;
  font-size: 12.5px;
  line-height: 1.75;
  color: var(--cr-text-muted, #7b8b99);
}

.example-group {
  margin-top: 20px;
  text-align: left;
}

.example-title {
  margin-bottom: 7px;
  font-size: 12px;
  font-weight: 600;
  color: var(--cr-text-muted, #8a99a8);
}

.example-item {
  padding: 8px 11px;
  margin-bottom: 6px;
  font-size: 12.5px;
  cursor: pointer;
  background: #fff;
  border: 1px solid var(--cr-border);
  border-radius: 6px;
  transition: all 0.15s;
}

.example-item:hover {
  color: var(--cr-primary);
  border-color: var(--cr-primary);
  background: rgba(43, 122, 120, 0.04);
}

.example-item.followup {
  border-left: 3px solid var(--cr-primary);
}

/* ---------- 消息 ---------- */
.msg-row {
  display: flex;
  gap: 10px;
  margin-bottom: 18px;
}

.msg-row.user {
  flex-direction: row-reverse;
}

.avatar {
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
  width: 30px;
  height: 30px;
  font-size: 15px;
  color: #fff;
  border-radius: 50%;
}

.avatar.user {
  background: #5a7d9a;
}

.avatar.assistant {
  background: var(--cr-primary);
}

.bubble-wrap {
  max-width: calc(100% - 46px);
  min-width: 0;
}

.bubble {
  padding: 10px 14px;
  font-size: 13.5px;
  line-height: 1.8;
  border-radius: 9px;
  word-break: break-word;
}

.bubble.user {
  color: #fff;
  background: #5a7d9a;
  border-top-right-radius: 3px;
}

.bubble.assistant {
  background: #fff;
  border: 1px solid var(--cr-border);
  border-top-left-radius: 3px;
}

/* ---------- 系统理解过程 ---------- */
.reasoning {
  padding: 6px 10px;
  margin-bottom: 6px;
  font-size: 11.5px;
  background: rgba(43, 122, 120, 0.05);
  border-left: 3px solid var(--cr-primary);
  border-radius: 4px;
}

.reasoning-row {
  display: flex;
  align-items: baseline;
  gap: 6px;
  color: #4a5a68;
}

.reasoning-label {
  flex-shrink: 0;
  font-weight: 600;
  color: var(--cr-primary);
}

.reasoning-value {
  word-break: break-word;
}

/* ---------- 等待指示 ---------- */
.thinking {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 11px 14px;
  font-size: 12.5px;
  color: var(--cr-text-muted, #7b8b99);
  background: #fff;
  border: 1px solid var(--cr-border);
  border-radius: 9px;
}

.thinking-dots {
  display: inline-flex;
  gap: 3px;
}

.thinking-dots i {
  width: 5px;
  height: 5px;
  background: var(--cr-primary);
  border-radius: 50%;
  animation: cr-bounce 1.2s infinite ease-in-out;
}

.thinking-dots i:nth-child(2) { animation-delay: 0.15s; }
.thinking-dots i:nth-child(3) { animation-delay: 0.3s; }

@keyframes cr-bounce {
  0%, 80%, 100% { transform: scale(0.6); opacity: 0.5; }
  40% { transform: scale(1); opacity: 1; }
}

.numeric-alert {
  margin-top: 8px;
}

/* ---------- 底部信息 ---------- */
.msg-footer {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  align-items: center;
  padding: 4px 2px 0;
  font-size: 11.5px;
  color: var(--cr-text-muted, #8a99a8);
}

.footer-stat {
  display: inline-flex;
  gap: 3px;
  align-items: center;
}

.footer-stat.warn {
  color: #c98a2b;
}

/* ---------- 输入区 ---------- */
.chat-input {
  flex-shrink: 0;
  padding: 12px 18px 14px;
  border-top: 1px solid var(--cr-border);
}

.input-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 8px;
}

.spacer {
  flex: 1;
}

/* ---------- 引用面板 ---------- */
/* 只负责宽度与占位；边框、标题与列表由 CitationPanel 组件自带，
   这里再画左边框会出现双线。 */
.citation-column {
  display: flex;
  width: 330px;
  flex-shrink: 0;
  flex-direction: column;
  background: #fcfdfd;
}

/* ---------- Markdown ---------- */
.md :deep(h1),
.md :deep(h2),
.md :deep(h3) {
  margin: 12px 0 6px;
  font-size: 14px;
  font-weight: 700;
  color: var(--cr-primary-dark, #1f5c5a);
}

.md :deep(h1:first-child),
.md :deep(h2:first-child),
.md :deep(h3:first-child) {
  margin-top: 0;
}

.md :deep(p) {
  margin: 5px 0;
}

.md :deep(ul),
.md :deep(ol) {
  padding-left: 20px;
  margin: 5px 0;
}

.md :deep(li) {
  margin: 3px 0;
}

.md :deep(strong) {
  font-weight: 700;
  color: #1f2d3a;
}

.md :deep(table) {
  width: 100%;
  margin: 8px 0;
  font-size: 12.5px;
  border-collapse: collapse;
}

.md :deep(th),
.md :deep(td) {
  padding: 5px 8px;
  text-align: left;
  border: 1px solid var(--cr-border);
}

.md :deep(th) {
  background: var(--cr-bg-soft, #f5f8f9);
}

.md :deep(.cite) {
  padding: 0 3px;
  margin: 0 1px;
  font-size: 10.5px;
  font-weight: 600;
  color: var(--cr-primary);
  cursor: pointer;
  background: rgba(43, 122, 120, 0.1);
  border-radius: 3px;
  vertical-align: super;
}

.md :deep(.cite:hover) {
  color: #fff;
  background: var(--cr-primary);
}
.chat-column, .chat-scroll { min-height: 0; }
.bubble { font-size: 15px; }
.chat-toolbar, .toolbar-left, .toolbar-right { flex-wrap: wrap; }
.chat-input :deep(.el-textarea__inner) { font-size: 16px; padding: 12px; }
.chat-empty { margin-top: 20px; }
@media (max-width: 600px) { .chat-scroll { padding: 12px; } .chat-input { padding: 10px 12px; } .chat-toolbar { padding: 8px 12px; } .bubble { padding: 10px; } }
</style>
