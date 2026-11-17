<script setup lang="ts">
/**
 * 消息气泡。
 *
 * 关键设计：**思考指示器**
 * LLM 生成的首字延迟约 4 秒（推理模型先思考再输出）。若期间界面空白，
 * 用户会以为系统卡死。因此本组件在等待期显示明确的阶段状态与动效：
 *
 *   retrieving  → 「正在检索知识库证据…」 + 骨架屏
 *   generating  → 「正在推理并组织答案…」 + 已等待秒数
 *
 * 等待秒数很重要：让用户知道系统在推进，而不是无响应。
 */
import { computed, onUnmounted, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'

import type { ChatMessage } from '@/stores/chat'
import { renderAnswer, stripCitationMarkers } from '@/utils/markdown'

const props = defineProps<{
  message: ChatMessage
  /** 当前高亮的引用编号 */
  activeIndex?: number | null
}>()

const emit = defineEmits<{
  (e: 'cite-click', index: number): void
}>()

const isUser = computed(() => props.message.role === 'user')
const isStreaming = computed(
  () => props.message.status === 'retrieving' || props.message.status === 'generating',
)

/** 是否开发模式：诊断条仅开发期显示，避免出现在答辩演示中 */
const isDev = import.meta.env.DEV

/** 渲染后的答案 HTML（含可点击引用标记） */
const renderedHtml = computed(() => renderAnswer(props.message.content))

/** 等待计时（仅流式期间运行） */
const elapsedSeconds = ref(0)
let timer: number | null = null

function startTimer(): void {
  elapsedSeconds.value = 0
  stopTimer()
  timer = window.setInterval(() => {
    elapsedSeconds.value += 0.1
  }, 100)
}

function stopTimer(): void {
  if (timer !== null) {
    window.clearInterval(timer)
    timer = null
  }
}

watch(
  isStreaming,
  (streaming) => {
    if (streaming) startTimer()
    else stopTimer()
  },
  { immediate: true },
)

onUnmounted(stopTimer)

/** 阶段文案 */
const statusLabel = computed(() => {
  if (props.message.status === 'retrieving') return '正在检索知识库证据'
  if (props.message.status === 'generating') return '正在推理并组织答案'
  return ''
})

/** 点击答案中的引用标记 */
function onAnswerClick(event: MouseEvent): void {
  const target = event.target as HTMLElement
  const citeEl = target.closest('sup.cite') as HTMLElement | null
  if (!citeEl) return
  const index = Number(citeEl.dataset.cite)
  if (Number.isFinite(index)) {
    emit('cite-click', index)
  }
}

/** 复制答案（去除引用标记，便于粘贴到病历或文档） */
async function copyAnswer(): Promise<void> {
  const text = stripCitationMarkers(props.message.content)
  try {
    await navigator.clipboard.writeText(text)
    ElMessage.success('已复制答案（不含引用标记）')
  } catch {
    ElMessage.warning('复制失败，请手动选择文本')
  }
}

/** 耗时展示 */
const timingText = computed(() => {
  const t = props.message.timings
  if (!t) return ''
  const total = t.total_ms
  if (typeof total === 'number') return `${(total / 1000).toFixed(1)}s`
  return ''
})
</script>

<template>
  <div class="message" :class="isUser ? 'is-user' : 'is-assistant'">
    <!-- 头像 -->
    <div class="avatar" :class="isUser ? 'avatar-user' : 'avatar-assistant'">
      <el-icon><component :is="isUser ? 'User' : 'Cpu'" /></el-icon>
    </div>

    <div class="bubble-wrap">
      <!-- 用户消息：纯文本 -->
      <div v-if="isUser" class="bubble bubble-user">{{ message.content }}</div>

      <!-- 助手消息 -->
      <div v-else class="bubble bubble-assistant">
        <!-- 思考指示器 -->
        <div v-if="isStreaming && !message.content" class="thinking">
          <span class="thinking-dots"><i /><i /><i /></span>
          <span class="thinking-text">{{ statusLabel }}…</span>
          <span class="thinking-time">{{ elapsedSeconds.toFixed(1) }}s</span>
          <div v-if="message.status === 'retrieving'" class="skeleton">
            <div class="skeleton-line w90" />
            <div class="skeleton-line w75" />
            <div class="skeleton-line w60" />
          </div>
        </div>

        <!-- 答案正文 -->
        <div
          v-if="message.content"
          class="answer-body"
          :class="{ 'stream-cursor': message.status === 'generating' }"
          @click="onAnswerClick"
          v-html="renderedHtml"
        />

        <!-- 生成中但已有内容：显示追加提示 -->
        <div v-if="message.status === 'generating' && message.content" class="generating-hint">
          <span class="thinking-dots small"><i /><i /><i /></span>
          正在继续生成… {{ elapsedSeconds.toFixed(1) }}s
        </div>

        <!-- 错误 / 无证据提示 -->
        <el-alert
          v-if="message.status === 'error'"
          :type="message.noEvidence ? 'warning' : 'error'"
          :closable="false"
          show-icon
          class="msg-alert"
        >
          <template #title>
            {{ message.noEvidence ? '未找到足够证据' : '生成失败' }}
          </template>
          <div class="alert-body">{{ message.errorMessage }}</div>
        </el-alert>

        <el-alert
          v-else-if="message.status === 'aborted'"
          type="info"
          :closable="false"
          show-icon
          title="已停止生成"
        />

        <!-- 幻觉引用警告：LLM 引用了不存在的证据编号 -->
        <el-alert
          v-if="message.invalidCitations?.length"
          type="warning"
          :closable="false"
          show-icon
          class="msg-alert"
        >
          <template #title>检测到无效引用</template>
          <div class="alert-body">
            模型引用了不存在的证据编号
            <span class="mono">[{{ message.invalidCitations.join('][') }}]</span>
            ，已自动剔除。这属于模型幻觉信号，请谨慎对待该回答。
          </div>
        </el-alert>

        <!-- 底部信息栏 -->
        <div v-if="message.status === 'done' && message.content" class="msg-footer">
          <span class="footer-stat">
            <el-icon><Link /></el-icon>
            引用 {{ message.citations.length }} 条
          </span>
          <span v-if="message.evidenceCount" class="footer-stat">
            <el-icon><Document /></el-icon>
            证据 {{ message.evidenceCount }} 条
          </span>
          <span v-if="timingText" class="footer-stat">
            <el-icon><Timer /></el-icon>
            {{ timingText }}
          </span>
          <span v-if="message.model" class="footer-stat mono">{{ message.model }}</span>
          <el-button link size="small" @click="copyAnswer">
            <el-icon><CopyDocument /></el-icon>
            复制
          </el-button>
        </div>

        <!-- 数值核验提示：证据中找不到的数字，需人工核实 -->
        <el-alert
          v-if="message.numericWarning"
          type="warning"
          :closable="false"
          show-icon
          class="numeric-alert"
        >
          <template #title>数值需核实</template>
          <div class="numeric-body">
            {{ message.numericWarning }}
            <div class="numeric-note">
              系统已逐个比对答案中的数值与检索证据。
              医学场景请以原文或最新指南为准。
            </div>
          </div>
        </el-alert>

        <!--
          诊断信息：仅在开发模式显示，用于定位「答案显示不全」类问题。
          显示原始内容长度与渲染后 HTML 长度 —— 若两者差距异常，
          说明渲染环节丢内容；若原始长度本身很短，说明传输环节丢内容。
          生产构建（npm run build / preview）会自动隐藏，不出现于答辩演示。
        -->
        <div v-if="isDev && message.role === 'assistant' && message.content" class="diag">
          原始 {{ message.content.length }} 字 · 渲染 {{ renderedHtml.length }} 字 ·
          引用数组 {{ message.citations.length }} · 状态 {{ message.status }}
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.message {
  display: flex;
  gap: 10px;
  padding: 12px 0;
}

.message.is-user {
  flex-direction: row-reverse;
}

/* ---------- 头像 ---------- */
.avatar {
  display: flex;
  flex-shrink: 0;
  align-items: center;
  justify-content: center;
  width: 32px;
  height: 32px;
  font-size: 16px;
  color: #fff;
  border-radius: 8px;
}

.avatar-user {
  background: #5a6b7b;
}

.avatar-assistant {
  background: var(--cr-primary);
}

.bubble-wrap {
  min-width: 0;
  max-width: calc(100% - 52px);
}

/* ---------- 气泡 ---------- */
.bubble {
  padding: 11px 14px;
  border-radius: var(--cr-radius);
  box-shadow: var(--cr-shadow-sm);
}

.bubble-user {
  font-size: 14px;
  line-height: 1.6;
  color: #fff;
  white-space: pre-wrap;
  background: var(--cr-primary);
  border-top-right-radius: 2px;
}

.bubble-assistant {
  background: var(--cr-surface);
  border-top-left-radius: 2px;
}

/* ---------- 思考指示器 ---------- */
.thinking {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
  color: var(--cr-text-secondary);
}

.thinking-text {
  font-size: 13px;
}

.thinking-time {
  padding: 1px 6px;
  font-family: 'Cascadia Code', Consolas, monospace;
  font-size: 12px;
  color: var(--cr-text-muted);
  background: #f0f3f5;
  border-radius: 10px;
}

.thinking-dots {
  display: inline-flex;
  gap: 3px;
}

.thinking-dots i {
  width: 5px;
  height: 5px;
  background: var(--cr-primary-light);
  border-radius: 50%;
  animation: cr-bounce 1.2s infinite ease-in-out;
}

.thinking-dots i:nth-child(2) {
  animation-delay: 0.15s;
}

.thinking-dots i:nth-child(3) {
  animation-delay: 0.3s;
}

.thinking-dots.small i {
  width: 4px;
  height: 4px;
}

@keyframes cr-bounce {
  0%,
  80%,
  100% {
    transform: scale(0.6);
    opacity: 0.5;
  }
  40% {
    transform: scale(1);
    opacity: 1;
  }
}

/* ---------- 骨架屏 ---------- */
.skeleton {
  width: 100%;
  margin-top: 4px;
}

.skeleton-line {
  height: 10px;
  margin: 7px 0;
  background: linear-gradient(90deg, #eef1f4 25%, #e2e8ec 37%, #eef1f4 63%);
  background-size: 400% 100%;
  border-radius: 4px;
  animation: cr-shimmer 1.4s ease infinite;
}

.skeleton-line.w90 {
  width: 90%;
}
.skeleton-line.w75 {
  width: 75%;
}
.skeleton-line.w60 {
  width: 60%;
}

@keyframes cr-shimmer {
  0% {
    background-position: 100% 50%;
  }
  100% {
    background-position: 0 50%;
  }
}

.generating-hint {
  display: flex;
  gap: 6px;
  align-items: center;
  margin-top: 8px;
  font-size: 12px;
  color: var(--cr-text-muted);
}

/* ---------- 提示 ---------- */
.msg-alert {
  margin-top: 10px;
}

.alert-body {
  font-size: 12.5px;
  line-height: 1.6;
}

/* ---------- 底栏 ---------- */
.msg-footer {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  align-items: center;
  padding-top: 8px;
  margin-top: 10px;
  font-size: 12px;
  color: var(--cr-text-muted);
  border-top: 1px solid var(--cr-border);
}

.footer-stat {
  display: inline-flex;
  gap: 3px;
  align-items: center;
}

/* 诊断条：开发期用于定位「答案显示不全」问题，可在验收后移除 */
.diag {
  padding-top: 4px;
  margin-top: 4px;
  font-family: 'Cascadia Code', Consolas, monospace;
  font-size: 10.5px;
  color: #b0bcc6;
  border-top: 1px dotted var(--cr-border);
}

/* 数值核验提示 */
.numeric-alert {
  margin-top: 10px;
}

.numeric-body {
  font-size: 12.5px;
  line-height: 1.65;
}

.numeric-note {
  margin-top: 4px;
  font-size: 11.5px;
  color: var(--cr-text-muted, #8a99a8);
}
</style>
