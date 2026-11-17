<script setup lang="ts">
/**
 * 引用溯源面板。
 *
 * 展示答案引用的文献来源，支持：
 *  - 按编号定位（与答案中的 [n] 标记对应）
 *  - 展开查看原文片段
 *  - 显示证据等级、期刊、年份、页码、分期
 *
 * 证据等级用颜色与中文名双重标识：颜色便于快速扫视，
 * 中文名避免色盲用户无法区分（医学场景对可访问性要求更高）。
 */
import { computed, ref, watch } from 'vue'

import type { CitationItem } from '@/api/types'
import { api } from '@/api/client'

const props = defineProps<{
  citations: CitationItem[]
  /** 当前高亮的引用编号（点击答案中的 [n] 时设置） */
  activeIndex?: number | null
  /** 是否处于生成中（引用可能还会更新） */
  loading?: boolean
  hasQuestion?: boolean
  failed?: boolean
  noEvidence?: boolean
}>()

const emit = defineEmits<{
  (e: 'locate', index: number): void
}>()

/** 展开状态：记录哪些引用被展开了原文片段 */
const expanded = ref<Set<number>>(new Set())

/**
 * 已取回的**完整原文**缓存（按 chunk_id）。
 *
 * 为什么需要另取：检索结果里的 `content_snippet` 后端已截断到约 300 字，
 * 前端只是解除显示行数限制，仍然看不到 300 字之后的内容。
 * 展开时按 chunk_id 调 `/api/chunks/{id}` 取全文，取回后缓存避免重复请求。
 */
const fullText = ref<Record<string, string>>({})
const archived = ref<Record<string, boolean>>({})
/** 正在取全文的 chunk_id */
const loadingFull = ref<Set<string>>(new Set())
const fullErrors = ref<Record<string, boolean>>({})
watch(() => props.citations.map(c => c.chunk_id).join('|'), () => {
  expanded.value = new Set()
})

async function toggleExpand(index: number): Promise<void> {
  const next = new Set(expanded.value)
  if (next.has(index)) {
    next.delete(index)
    expanded.value = next
    return
  }
  next.add(index)
  expanded.value = next

  await loadFull(index)
}

async function loadFull(index: number): Promise<void> {
  const c = props.citations.find((x) => x.index === index)
  if (!c || !c.chunk_id) return
  if (fullText.value[c.chunk_id] || loadingFull.value.has(c.chunk_id)) return

  const pending = new Set(loadingFull.value)
  pending.add(c.chunk_id)
  loadingFull.value = pending
  fullErrors.value[c.chunk_id] = false
  try {
    const d = await api.chunkDetail(c.chunk_id)
    fullText.value = { ...fullText.value, [c.chunk_id]: d?.content || '' }
    archived.value = { ...archived.value, [c.chunk_id]: d?.archived === true }
  } catch {
    fullErrors.value[c.chunk_id] = true
  } finally {
    const p = new Set(loadingFull.value)
    p.delete(c.chunk_id)
    loadingFull.value = p
  }
}

/** 该引用要显示的原文：优先完整原文，未取到则退回首屏摘要 */
function snippetOf(c: CitationItem): string {
  const full = fullText.value[c.chunk_id]
  return (full && full.length > 0) ? full : c.content_snippet
}

/** 原文是否已取到全文 */
function isFullLoaded(c: CitationItem): boolean {
  return (fullText.value[c.chunk_id] || '').length > 0
}

/** 证据等级 → 颜色变量名 */
function evidenceColor(level: string): string {
  const map: Record<string, string> = {
    guideline: 'var(--cr-evidence-guideline)',
    consensus: 'var(--cr-evidence-consensus)',
    systematic: 'var(--cr-evidence-systematic)',
    rct: 'var(--cr-evidence-rct)',
    cohort: 'var(--cr-evidence-cohort)',
    case: 'var(--cr-evidence-case)',
    review: 'var(--cr-evidence-review)',
    other: 'var(--cr-evidence-other)',
  }
  return map[level] ?? 'var(--cr-evidence-other)'
}

/** 分期中文名 */
function phaseLabel(phase: string): string {
  return { preop: '术前', intraop: '术中', postop: '术后', general: '通用' }[phase] ?? phase
}

/** 页码展示：起止页相同时只显示一个 */
function pageLabel(c: CitationItem): string {
  if (!c.page_start) return ''
  if (c.page_end && c.page_end !== c.page_start) {
    return `p.${c.page_start}-${c.page_end}`
  }
  return `p.${c.page_start}`
}

/** 被引用次数统计：同一文献可能被多个编号引用 */
const uniqueDocCount = computed(() => new Set(props.citations.map((c) => c.document_id)).size)

/** 证据等级分布（用于面板顶部概览） */
const evidenceSummary = computed(() => {
  const counts = new Map<string, { label: string; level: string; count: number }>()
  for (const c of props.citations) {
    const existing = counts.get(c.evidence_level)
    if (existing) {
      existing.count += 1
    } else {
      counts.set(c.evidence_level, {
        level: c.evidence_level,
        label: c.evidence_level_zh,
        count: 1,
      })
    }
  }
  return [...counts.values()].sort((a, b) => b.count - a.count)
})
</script>

<template>
  <div class="citation-panel">
    <div class="panel-header">
      <div class="panel-title">
        <el-icon><Link /></el-icon>
        <span>引用溯源</span>
        <el-tag v-if="citations.length" size="small" type="success" effect="plain">
          {{ citations.length }} 条
        </el-tag>
      </div>
      <el-tag v-if="uniqueDocCount > 1" size="small" type="info" effect="plain">
        {{ uniqueDocCount }} 篇文献
      </el-tag>
    </div>

    <!-- 证据等级概览 -->
    <div v-if="evidenceSummary.length" class="evidence-summary">
      <span
        v-for="item in evidenceSummary"
        :key="item.level"
        class="evidence-chip"
        :style="{ borderColor: evidenceColor(item.level), color: evidenceColor(item.level) }"
      >
        {{ item.label }} ×{{ item.count }}
      </span>
    </div>

    <!-- 空状态 -->
    <div v-if="!citations.length" class="panel-empty">
      <el-icon class="empty-icon"><DocumentRemove /></el-icon>
      <p v-if="loading">正在检索证据…</p>
      <p v-else-if="!hasQuestion">提问后，在这里核对文献原文</p>
      <p v-else-if="noEvidence">未检索到足够证据，请补充术式、阶段或具体问题</p>
      <p v-else-if="failed">回答生成失败，暂无可核对的引用</p>
      <p v-else>本次回答暂无可核验的文献引用</p>
      <p class="empty-hint">
        答案中的
        <sup class="demo-cite">[n]</sup>
        可点击定位到对应来源
      </p>
    </div>

    <!-- 引用列表 -->
    <div v-else class="citation-list">
      <div
        v-for="c in citations"
        :key="`${c.index}-${c.chunk_id}`"
        class="citation-item"
        :class="{ 'is-active': activeIndex === c.index }"
        @click="emit('locate', c.index)"
      >
        <div class="item-head">
          <span class="cite-badge" :style="{ background: evidenceColor(c.evidence_level) }">
            {{ c.index }}
          </span>
          <div class="item-meta">
            <div class="item-title" :title="c.doc_title">{{ c.doc_title }}</div>
            <div class="item-sub">
              <span
                class="evidence-level"
                :style="{ color: evidenceColor(c.evidence_level) }"
              >
                {{ c.evidence_level_zh }}
              </span>
              <span v-if="c.doc_journal" class="sep">·</span>
              <span v-if="c.doc_journal">{{ c.doc_journal }}</span>
              <span v-if="c.doc_year" class="sep">·</span>
              <span v-if="c.doc_year">{{ c.doc_year }}</span>
              <span v-if="pageLabel(c)" class="sep">·</span>
              <span v-if="pageLabel(c)" class="mono">{{ pageLabel(c) }}</span>
            </div>
            <div class="item-tags">
              <el-tag v-if="archived[c.chunk_id]" size="small" type="warning" effect="plain">历史版本原文</el-tag>
              <el-tag size="small" effect="plain" type="info">{{ phaseLabel(c.phase) }}</el-tag>
              <el-tag v-if="c.section" size="small" effect="plain">{{ c.section }}</el-tag>
              <el-tag v-if="c.doc_authors" size="small" effect="plain" class="author-tag">
                {{ c.doc_authors.split(',')[0] }}
              </el-tag>
            </div>
          </div>
        </div>

        <!-- 原文片段 / 全文 -->
        <div class="item-snippet">
          <div v-if="expanded.has(c.index) && fullErrors[c.chunk_id]" class="full-error" role="alert">
            原文加载失败，当前仅显示摘要。
            <el-button link type="primary" @click.stop="loadFull(c.index)">重试原文</el-button>
          </div>
          <p :class="{ 'is-clamped': !expanded.has(c.index) }">
            {{ snippetOf(c) }}
          </p>
          <div class="snippet-bar">
            <span v-if="expanded.has(c.index)" class="snippet-len">
              {{ snippetOf(c).length }} 字
              <template v-if="isFullLoaded(c)">· 完整片段</template>
              <template v-else-if="loadingFull.has(c.chunk_id)">· 正在加载原文…</template>
              <template v-else>· 仅摘要</template>
            </span>
            <el-button
              v-if="c.chunk_id"
              link
              type="primary"
              size="small"
              @click.stop="toggleExpand(c.index)"
            >
              {{ expanded.has(c.index) ? '收起' : '展开原文' }}
            </el-button>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.citation-panel {
  display: flex;
  flex-direction: column;
  height: 100%;
  overflow: hidden;
  background: var(--cr-surface);
  border-left: 1px solid var(--cr-border);
}
.full-error { padding: 8px; color: #9b3b24; background: #fff5ef; font-size: 13px; }
.item-title { overflow-wrap: anywhere; }

/* ---------- 头部 ---------- */
.panel-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 12px 14px;
  border-bottom: 1px solid var(--cr-border);
}

.panel-title {
  display: flex;
  gap: 6px;
  align-items: center;
  font-size: 14px;
  font-weight: 600;
  color: var(--cr-primary-dark);
}

.evidence-summary {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  padding: 8px 14px;
  border-bottom: 1px solid var(--cr-border);
  background: #fafbfc;
}

.evidence-chip {
  padding: 1px 7px;
  font-size: 11px;
  border: 1px solid;
  border-radius: 10px;
}

/* ---------- 空状态 ---------- */
.panel-empty {
  display: flex;
  flex: 1;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  padding: 24px;
  text-align: center;
  color: var(--cr-text-muted);
}

.empty-icon {
  margin-bottom: 10px;
  font-size: 34px;
  opacity: 0.4;
}

.empty-hint {
  margin-top: 10px;
  font-size: 12px;
}

.demo-cite {
  padding: 0 3px;
  font-size: 10px;
  color: #fff;
  background: var(--cr-primary);
  border-radius: 2px;
}

/* ---------- 引用列表 ---------- */
.citation-list {
  flex: 1;
  padding: 10px;
  overflow-y: auto;
}

.citation-item {
  padding: 10px 11px;
  margin-bottom: 8px;
  cursor: pointer;
  background: #fff;
  border: 1px solid var(--cr-border);
  border-left: 3px solid transparent;
  border-radius: var(--cr-radius);
  transition: border-color 0.15s, box-shadow 0.15s, background 0.15s;
}

.citation-item:hover {
  box-shadow: var(--cr-shadow-sm);
}

.citation-item.is-active {
  background: #f0f9f8;
  border-left-color: var(--cr-primary);
  box-shadow: 0 0 0 2px rgba(43, 122, 120, 0.12);
}

.item-head {
  display: flex;
  gap: 8px;
}

.cite-badge {
  display: flex;
  flex-shrink: 0;
  align-items: center;
  justify-content: center;
  width: 20px;
  height: 20px;
  font-size: 11px;
  font-weight: 700;
  color: #fff;
  border-radius: 4px;
}

.item-meta {
  min-width: 0;
}

.item-title {
  display: -webkit-box;
  overflow: hidden;
  font-size: 13px;
  font-weight: 600;
  line-height: 1.4;
  color: var(--cr-text);
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 2;
}

.item-sub {
  margin-top: 3px;
  font-size: 11.5px;
  color: var(--cr-text-secondary);
}

.sep {
  margin: 0 3px;
  color: var(--cr-text-muted);
}

.evidence-level {
  font-weight: 600;
}

.item-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  margin-top: 5px;
}

.author-tag {
  max-width: 110px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.item-snippet {
  padding-top: 8px;
  margin-top: 8px;
  border-top: 1px dashed var(--cr-border);
}

.item-snippet p {
  margin: 0;
  font-size: 12.5px;
  line-height: 1.6;
  color: var(--cr-text-secondary);
}

.item-snippet p.is-clamped {
  display: -webkit-box;
  overflow: hidden;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 3;
}

/* 展开后显示「N 字 · 全文/仅摘要」与收起按钮 */
.snippet-bar {
  display: flex;
  gap: 8px;
  align-items: center;
  justify-content: space-between;
  margin-top: 4px;
}

.snippet-len {
  font-size: 11px;
  color: var(--cr-text-tertiary, #9aa8b4);
}
.item-title { font-size: 15px; line-height: 1.6; display: block; }
.item-sub { font-size: 13px; }
.item-snippet p { font-size: 14px; line-height: 1.8; color: var(--cr-text); overflow-wrap: anywhere; }
.panel-header, .evidence-summary { flex-shrink: 0; }
.citation-list { min-height: 0; }
</style>
