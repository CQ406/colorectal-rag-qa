<script setup lang="ts">
/**
 * 智能问答主界面。
 *
 * 布局：左侧对话区 + 右侧引用溯源面板
 *
 * 交互要点：
 *  - Enter 发送，Shift+Enter 换行
 *  - 点击答案中的 [n] 标记 → 右侧面板高亮并滚动到该引用
 *  - 生成过程中可随时停止
 *  - 分期筛选可显著降低跨期噪声（对应报审表「围术期场景应用模块」）
 */
import { computed, nextTick, onMounted, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'

import { api } from '@/api/client'
import type { PhaseOption } from '@/api/types'
import CitationPanel from '@/components/CitationPanel.vue'
import EvidenceWorkspace from '@/components/EvidenceWorkspace.vue'
import MessageBubble from '@/components/MessageBubble.vue'
import { useChatStore } from '@/stores/chat'

const chat = useChatStore()
const workspace = ref<InstanceType<typeof EvidenceWorkspace> | null>(null)
const documentCount = ref<number | null>(null)
const statsLoading = ref(false)
const statsError = ref(false)
async function loadStats() {
  statsLoading.value = true
  statsError.value = false
  try { documentCount.value = (await api.stats()).documents }
  catch { documentCount.value = null; statsError.value = true }
  finally { statsLoading.value = false }
}
const activeAnswerId = ref<string | null>(null)
const activeAnswer = computed(() => chat.messages.find(m => m.id === activeAnswerId.value) || chat.messages.filter(m => m.role === 'assistant').at(-1))
const answerFailed = computed(() => activeAnswer.value?.status === 'error')
watch(() => chat.messages.length, () => { activeAnswerId.value = null; activeCiteIndex.value = null })

/* ----------------------------- 输入区 ----------------------------- */
const inputText = ref('')
const inputRef = ref<HTMLTextAreaElement | null>(null)

function send(): void {
  const text = inputText.value.trim()
  if (!text) return

  // 生成中不接受新提问。此处明确提示（而非静默丢弃），
  // 并保留输入框内容，避免用户输入的内容丢失。
  if (chat.isBusy) {
    ElMessage.warning('正在生成回答，请等待完成或点击「停止生成」后再提问')
    return
  }

  chat.ask(text)
  inputText.value = ''
}

/** Enter 发送，Shift+Enter 换行 */
function onKeydown(event: KeyboardEvent): void {
  if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
    event.preventDefault()
    send()
  }
}

/* ----------------------------- 引用联动 ----------------------------- */
const activeCiteIndex = ref<number | null>(null)
const citationPanelRef = ref<InstanceType<typeof CitationPanel> | null>(null)

/** 点击答案中的引用标记 */
function onCiteClick(index: number, messageId: string): void {
  activeAnswerId.value = messageId
  activeCiteIndex.value = index
  workspace.value?.show()
  // 滚动到引用面板对应条目
  nextTick(() => {
    const el = document.querySelector(`.citation-item.is-active`)
    el?.scrollIntoView({ behavior: 'smooth', block: 'nearest' })
  })
}

/** 点击右侧引用条目 */
function onLocate(index: number): void {
  activeCiteIndex.value = index
}

/* ----------------------------- 会话滚动 ----------------------------- */
const scrollRef = ref<HTMLElement | null>(null)

/** 新消息或流式增量到达时自动滚到底部 */
function scrollToBottom(): void {
  nextTick(() => {
    const el = scrollRef.value
    if (el) el.scrollTop = el.scrollHeight
  })
}

watch(() => chat.messages.length, scrollToBottom)
watch(
  () => chat.streamingMessage?.content.length ?? 0,
  () => {
    // 仅当用户已在底部附近时才自动滚动，避免打断向上翻阅
    const el = scrollRef.value
    if (!el) return
    const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 160
    if (nearBottom) scrollToBottom()
  },
)

/* ----------------------------- 当前引用 ----------------------------- */
/** 最后一条助手消息的引用（用于右侧面板） */
const currentCitations = computed(() => activeAnswer.value?.citations ?? [])

/* ----------------------------- 分期选项 ----------------------------- */
const phaseOptions = ref<PhaseOption[]>([])

onMounted(async () => {
  void loadStats()
  try {
    const data = await api.phases()
    phaseOptions.value = data.phases
  } catch {
    // 后端未启动时降级为本地默认值，界面仍可用
    phaseOptions.value = [
      { value: 'preop', label: '术前' },
      { value: 'intraop', label: '术中' },
      { value: 'postop', label: '术后' },
      { value: 'general', label: '通用' },
    ]
  }
  inputRef.value?.focus()
})

/* ----------------------------- 示例问题 ----------------------------- */
const examples = [
  '结直肠癌术后吻合口漏应该如何处理？',
  '加速康复外科的核心措施有哪些？',
  '术中如何评估肠道吻合口的血供？',
  '术前机械性肠道准备是否必要？',
]

function useExample(text: string): void {
  inputText.value = text
  inputRef.value?.focus()
}

const perioperativeScenarios = [
  {
    phase: 'preop', label: '术前评估', icon: 'DocumentChecked',
    prompt: '请按术前评估场景，基于知识库资料整理【疾病/术式】相关检查、适应证与风险因素。',
  },
  {
    phase: 'intraop', label: '术中决策', icon: 'FirstAidKit',
    prompt: '请按术中决策支持场景，基于知识库资料整理【术中问题/解剖要点】及文献支持的处理原则。',
  },
  {
    phase: 'postop', label: '术后管理', icon: 'Finished',
    prompt: '请按术后管理场景，基于知识库资料整理【并发症/术后问题】的观察、评估、管理与随访要点。',
  },
]

function chooseScenario(item: (typeof perioperativeScenarios)[number]): void {
  chat.options.phase = item.phase
  inputText.value = item.prompt
  inputRef.value?.focus()
}

/* ----------------------------- 配置面板 ----------------------------- */
const showSettings = ref(false)

</script>

<template>
  <EvidenceWorkspace ref="workspace" :count="currentCitations.length">
  <div class="qa-layout">
    <!-- ==================== 左：对话区 ==================== -->
    <div class="chat-column">
      <!-- 工具栏 -->
      <div class="chat-toolbar">
        <div class="toolbar-left">
          <el-select
            v-model="chat.options.phase"
            placeholder="全部分期"
            clearable
            size="small"
            style="width: 120px"
          >
            <el-option
              v-for="p in phaseOptions"
              :key="p.value"
              :label="p.label"
              :value="p.value"
            />
          </el-select>

        </div>

        <div class="toolbar-right">
          <!-- 非流式补偿进行中的提示：让用户知道系统正在补全答案 -->
          <el-tag v-if="chat.repairing" size="small" type="warning" effect="plain">
            <el-icon class="spin"><Loading /></el-icon>
            正在补全答案…
          </el-tag>
          <el-button link size="small" @click="showSettings = !showSettings">
            <el-icon><Setting /></el-icon>
            高级设置
          </el-button>
          <el-button link size="small" :disabled="!chat.messages.length" @click="chat.clear()">
            <el-icon><Delete /></el-icon>
            清空
          </el-button>
        </div>
      </div>

      <!-- 参数抽屉 -->
      <el-collapse-transition>
        <div v-show="showSettings" class="settings-bar">
          <div class="setting-item">
            <span class="setting-label">证据条数</span>
            <el-input-number
              v-model="chat.options.topK"
              :min="1"
              :max="10"
              size="small"
              controls-position="right"
              style="width: 90px"
            />
          </div>
          <div class="setting-item">
            <span class="setting-label">交叉编码器重排</span>
            <el-switch v-model="chat.options.useRerank" size="small" />
          </div>
          <div class="setting-item">
            <span class="setting-label">证据等级加权</span>
            <el-switch v-model="chat.options.useEvidenceBoost" size="small" />
          </div>
          <div class="setting-item">
            <span class="setting-label">召回模式</span>
            <el-select v-model="chat.options.recallMode" size="small" style="width: 110px">
              <el-option label="多路融合" value="hybrid" />
              <el-option label="仅向量" value="vector" />
              <el-option label="仅关键词" value="keyword" />
            </el-select>
          </div>
        </div>
      </el-collapse-transition>

      <!-- 消息列表 -->
      <div ref="scrollRef" class="chat-scroll">
        <!-- 传输不完整警告（流式累积与服务端答案长度不一致） -->
        <el-alert
          v-if="chat.truncatedWarning"
          type="warning"
          :closable="true"
          show-icon
          class="truncate-warning"
          @close="chat.clearTruncatedWarning()"
        >
          <template #title>答案可能不完整</template>
          <div class="truncate-body">
            {{ chat.truncatedWarning }}
          </div>
          <el-button size="small" type="warning" plain @click="chat.regenerate()">
            <el-icon><Refresh /></el-icon>
            重新生成
          </el-button>
        </el-alert>

        <!-- 空状态：引导 + 示例问题 -->
        <div v-if="!chat.messages.length" class="chat-empty">
          <div class="empty-logo">
            <el-icon><ChatDotRound /></el-icon>
          </div>
          <h2>结直肠手术围术期知识问答</h2>
          <p class="empty-desc">
            从问题出发，检索围术期文献，对照来源理解答案。
            <span v-if="documentCount !== null" class="library-count">知识库已收录 <strong>{{ documentCount }} 篇文献</strong></span>
            <span v-else-if="statsLoading">正在读取知识库统计…</span>
            <span v-else-if="statsError">知识库统计暂不可用 <el-button link type="primary" @click="loadStats">重试统计</el-button></span>
          </p>
          <div class="empty-note">
            <el-icon><InfoFilled /></el-icon>
            请核对引用原文与证据等级，输出仅供教学科研参考
          </div>

          <div class="scenario-cards">
            <button v-for="scene in perioperativeScenarios" :key="scene.phase" class="scenario-card" :class="{ chosen: chat.options.phase === scene.phase }" @click="chooseScenario(scene)">
              <el-icon><component :is="scene.icon" /></el-icon><strong>{{ scene.label }}</strong>
              <span>{{ scene.phase === 'preop' ? '检查、适应证与风险评估' : scene.phase === 'intraop' ? '解剖、血供与术中处理' : '并发症、康复与随访' }}</span>
            </button>
          </div>
          <div class="example-list">
            <div class="example-title">试试这些问题：</div>
            <button
              v-for="ex in examples"
              :key="ex"
              class="example-item"
              @click="useExample(ex)"
            >
              <el-icon><ArrowRight /></el-icon>
              <span>{{ ex }}</span>
            </button>
          </div>
        </div>

        <!-- 消息 -->
        <MessageBubble
          v-for="msg in chat.messages"
          :key="msg.id"
          :message="msg"
          :active-index="activeAnswer?.id === msg.id ? activeCiteIndex : null"
          @cite-click="onCiteClick($event, msg.id)"
        />
      </div>

      <!-- 输入区 -->
      <div class="chat-input">
        <el-input
          ref="inputRef"
          v-model="inputText"
          type="textarea"
          :rows="3"
          resize="none"
          maxlength="2000"
          show-word-limit
          placeholder="请输入临床问题…（Enter 发送，Shift+Enter 换行）"
          :disabled="chat.isBusy"
          @keydown="onKeydown"
        />
        <div class="input-actions">
          <el-button v-if="chat.isBusy" type="danger" plain @click="chat.abort()">
            <el-icon><VideoPause /></el-icon>
            停止生成
          </el-button>
          <el-button
            v-else-if="chat.messages.length"
            plain
            @click="chat.regenerate()"
          >
            <el-icon><Refresh /></el-icon>
            重新生成
          </el-button>
          <div class="spacer" />
          <el-button type="primary" :disabled="!inputText.trim() || chat.isBusy" @click="send">
            <el-icon><Promotion /></el-icon>
            发送
          </el-button>
        </div>
      </div>
    </div>

  </div>
  <template #evidence>
      <CitationPanel
        ref="citationPanelRef"
        :citations="currentCitations"
        :active-index="activeCiteIndex"
        :loading="chat.isBusy"
        :has-question="chat.messages.length > 0"
        :failed="answerFailed"
        :no-evidence="activeAnswer?.noEvidence"
        @locate="onLocate"
      />
  </template>
  </EvidenceWorkspace>
</template>

<style scoped>
.qa-layout {
  display: flex;
  height: 100%;
  overflow: hidden;
}

/* ---------- 左列 ---------- */
.chat-column {
  display: flex;
  flex: 1;
  flex-direction: column;
  min-width: 0;
}

.chat-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 8px 16px;
  background: var(--cr-surface);
  border-bottom: 1px solid var(--cr-border);
}

.toolbar-left,
.toolbar-right {
  display: flex;
  gap: 8px;
  align-items: center;
}

.settings-bar {
  display: flex;
  flex-wrap: wrap;
  gap: 20px;
  padding: 10px 16px;
  background: #fafbfc;
  border-bottom: 1px solid var(--cr-border);
}

.scenario-bar {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
  padding: 7px 16px;
  background: #f6f9fc;
  border-bottom: 1px solid var(--cr-border);
}

.scenario-label {
  margin-right: 2px;
  color: var(--cr-text-secondary);
  font-size: 12px;
}

.setting-item {
  display: flex;
  gap: 8px;
  align-items: center;
}

.setting-label {
  font-size: 12.5px;
  color: var(--cr-text-secondary);
}

/* ---------- 滚动区 ---------- */
.chat-scroll {
  flex: 1;
  padding: 8px 20px 16px;
  overflow-y: auto;
  scroll-behavior: smooth;
}

/* ---------- 传输不完整警告 ---------- */
.truncate-warning {
  margin-bottom: 12px;
}

.truncate-body {
  margin-bottom: 8px;
  font-size: 12.5px;
  line-height: 1.6;
}

/* 补偿进行中的旋转图标 */
.spin {
  margin-right: 3px;
  animation: cr-spin 1s linear infinite;
}

@keyframes cr-spin {
  from {
    transform: rotate(0deg);
  }
  to {
    transform: rotate(360deg);
  }
}

/* ---------- 空状态 ---------- */
.chat-empty {
  max-width: 620px;
  padding: 40px 0;
  margin: 0 auto;
  text-align: center;
}

.empty-logo {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 60px;
  height: 60px;
  margin: 0 auto 16px;
  font-size: 30px;
  color: #fff;
  background: linear-gradient(135deg, var(--cr-primary), var(--cr-primary-light));
  border-radius: 16px;
}

.chat-empty h2 {
  margin: 0 0 10px;
  font-size: 19px;
  font-weight: 600;
  color: var(--cr-primary-dark);
}

.empty-desc {
  margin: 0 0 14px;
  font-size: 13.5px;
  line-height: 1.8;
  color: var(--cr-text-secondary);
}

.empty-desc strong {
  color: var(--cr-primary);
}

.empty-note {
  display: inline-flex;
  gap: 6px;
  align-items: center;
  padding: 6px 12px;
  font-size: 12px;
  color: #8a6d3b;
  background: #fdf6e3;
  border-radius: 14px;
}

.example-list {
  margin-top: 26px;
  text-align: left;
}

.example-title {
  margin-bottom: 8px;
  font-size: 12.5px;
  color: var(--cr-text-muted);
}

.example-item {
  display: flex;
  gap: 8px;
  align-items: center;
  padding: 9px 12px;
  margin-bottom: 7px;
  font-size: 13.5px;
  color: var(--cr-text);
  cursor: pointer;
  background: var(--cr-surface);
  border: 1px solid var(--cr-border);
  border-radius: var(--cr-radius);
  transition: border-color 0.15s, transform 0.15s, box-shadow 0.15s;
}

.example-item:hover {
  border-color: var(--cr-primary-light);
  box-shadow: var(--cr-shadow-sm);
  transform: translateX(2px);
}

.example-item .el-icon {
  flex-shrink: 0;
  color: var(--cr-primary-light);
}

/* ---------- 输入区 ---------- */
.chat-input {
  padding: 10px 20px 14px;
  background: var(--cr-surface);
  border-top: 1px solid var(--cr-border);
}

.input-actions {
  display: flex;
  align-items: center;
  margin-top: 8px;
}

.spacer {
  flex: 1;
}

/* ---------- 右列 ---------- */
.citation-column {
  width: 360px;
  flex-shrink: 0;
}

@media (max-width: 1200px) {
  .citation-column {
    width: 300px;
  }
}
.chat-column, .chat-scroll { min-height: 0; }
.chat-input { flex-shrink: 0; box-shadow: 0 -4px 20px #17252a05; }
.chat-input :deep(.el-textarea__inner) { font-size: 16px; padding: 12px; }
.chat-toolbar, .toolbar-left, .toolbar-right { flex-wrap: wrap; gap: 8px; }
.library-count { display: block; margin-top: 8px; }
.scenario-cards { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; margin-top: 22px; }
.scenario-card { display: flex; flex-direction: column; align-items: flex-start; gap: 8px; padding: 18px; font: inherit; text-align: left; background: white; color: var(--cr-text); border: 1px solid var(--cr-border); border-radius: 12px; cursor: pointer; }
.scenario-card .el-icon { color: var(--cr-primary); font-size: 24px; }
.scenario-card span { color: var(--cr-text-secondary); font-size: 13px; }
.scenario-card:hover, .scenario-card.chosen { border-color: var(--cr-primary); background: #eff8f7; }
.chat-empty { max-width: 800px; padding: 26px 0 16px; }
.example-item { width: 100%; font: inherit; text-align: left; }
@media (max-width: 600px) {
 .chat-scroll { padding: 12px; } .chat-input { padding: 10px 12px; }
 .chat-empty { padding-top: 12px; } .chat-empty h2 { font-size: 20px; }
 .scenario-cards { grid-template-columns: 1fr; gap: 8px; }
 .scenario-card { padding: 12px; display: grid; grid-template-columns: 24px 1fr; column-gap: 10px; }
 .scenario-card span { grid-column: 2; }
 .empty-note { border-radius: 8px; text-align: left; }
 .settings-bar { max-height: 28dvh; overflow: auto; gap: 10px; }
}
</style>
