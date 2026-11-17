<script setup lang="ts">
/**
 * 知识库维护（阶段九）。
 *
 * 对应报审表「知识库维护模块：提供知识的审核、更新、版本管理和质量评估功能」，
 * 四个标签页逐一对应这四项要求：
 *
 *   语料概览   → 质量评估（规模、时效、证据结构、分期覆盖 + 自动维护建议）
 *   文献管理   → 审核（逐篇/批量）、上架下架、彻底删除
 *   更新知识库 → 更新（按检索式采集 PubMed、补齐向量，后台任务带进度）
 *   更新历史   → 版本管理（每次更新的参数、结果、耗时、操作人）
 *
 * 权限说明
 * --------
 * 查阅对所有登录用户开放；**所有写操作仅限管理员**。
 * 审核会决定文献是否进入检索池，属发布决策而非个人判断，因此收归管理员。
 * 界面据 `auth.can()` 隐藏按钮，但真正的校验在服务端。
 *
 * 关于「下架」与「彻底删除」
 * --------------------------
 * 界面上把「下架」作为主操作（软删除，可恢复，历史问答的引用仍有效），
 * 「彻底删除」放在详情对话框内并要求二次确认（不可恢复，且破坏引用追溯）。
 */
import { computed, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'

import { api } from '@/api/client'
import type { StatsResponse } from '@/api/types'
import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()

/* ===================== 常量 ===================== */
const PHASE_LABELS: Record<string, string> = {
  preop: '术前', intraop: '术中', postop: '术后', general: '通用',
}

const EVIDENCE_META: Record<string, { label: string; color: string }> = {
  guideline: { label: '临床指南', color: 'var(--cr-evidence-guideline)' },
  consensus: { label: '专家共识', color: 'var(--cr-evidence-consensus)' },
  systematic: { label: '系统评价/Meta分析', color: 'var(--cr-evidence-systematic)' },
  rct: { label: '随机对照试验', color: 'var(--cr-evidence-rct)' },
  cohort: { label: '队列研究', color: 'var(--cr-evidence-cohort)' },
  review: { label: '综述', color: 'var(--cr-evidence-review)' },
  case: { label: '病例报告', color: 'var(--cr-evidence-case)' },
  other: { label: '其他', color: 'var(--cr-evidence-other)' },
}

const OP_LABELS: Record<string, string> = {
  collect: '批量采集', search: '检索式采集', vectorize: '补齐向量',
}

const STATUS_META: Record<string, { label: string; type: string }> = {
  pending: { label: '等待中', type: 'info' },
  running: { label: '执行中', type: 'warning' },
  success: { label: '成功', type: 'success' },
  failed: { label: '失败', type: 'danger' },
  cancelled: { label: '已取消', type: 'info' },
}

const canView = computed(() => auth.can('kb:view'))
let ownerScope = 0
const canWrite = computed(() => auth.can('kb:update'))
const canReview = computed(() => auth.can('kb:review'))
const canDelete = computed(() => auth.can('kb:delete'))

/* ===================== 状态 ===================== */
const activeTab = ref('overview')
const loading = ref(true)

const stats = ref<StatsResponse | null>(null)
const quality = ref<any>(null)

/** 文献管理 */
const docs = ref<any[]>([])
const docTotal = ref(0)
const docQuery = reactive({
  keyword: '', phase: '', evidence_level: '', language: '',
  is_reviewed: '' as '' | 'true' | 'false',
  is_active: '' as '' | 'true' | 'false',
  year_from: undefined as number | undefined,
  limit: 20, offset: 0,
})
const selectedDocs = ref<any[]>([])
const docDetail = ref<any>(null)
const docRevisions = ref<any[]>([])
const revisionEvidence = ref<any>(null)
let detailRequest = 0
let evidenceRequest = 0
watch(() => auth.user?.id, () => {
  ownerScope++
  detailRequest++
  evidenceRequest++
  stopPolling()
  docs.value = []
  docTotal.value = 0
  quality.value = null
  batches.value = []
  batchTotal.value = 0
  recentJobs.value = []
  activeJob.value = null
  batchDialog.value = false
  searchDialog.value = false
  collectDialog.value = false
  activeTab.value = 'overview'
  void loadAll()
  detailDialog.value = false
  docDetail.value = null
  docRevisions.value = []
  revisionEvidence.value = null
})
const detailDialog = ref(false)
const detailLoading = ref(false)

/** 更新任务 */
const activeJob = ref<any>(null)
const recentJobs = ref<any[]>([])
let pollTimer: number | null = null

/** 自定义检索式 */
const searchDialog = ref(false)
const searchForm = reactive({ query: '', limit: 50, embed: true })

/** 采集确认 */
const collectDialog = ref(false)
const collectForm = reactive({
  mode: 'supplement' as 'default' | 'supplement' | 'all',
  max_total: 400 as number | undefined,
  mindate: '',
  embed: true,
})

/** 版本历史 */
const batches = ref<any[]>([])
const batchTotal = ref(0)
const batchDetail = ref<any>(null)
const batchDialog = ref(false)

/* ===================== 概览统计 ===================== */
const embedRate = computed(() => {
  const s = stats.value
  if (!s || !s.total_chunks) return 0
  return Math.round((s.embedded_chunks / s.total_chunks) * 1000) / 10
})

const phaseRows = computed(() => {
  const s = stats.value
  if (!s) return []
  const total = Object.values(s.phase_distribution).reduce((a, b) => a + b, 0) || 1
  return Object.entries(s.phase_distribution)
    .map(([key, count]) => ({
      key, label: PHASE_LABELS[key] ?? key, count,
      percent: Math.round((count / total) * 1000) / 10,
    }))
    .sort((a, b) => b.count - a.count)
})

const evidenceRows = computed(() => {
  const s = stats.value
  if (!s) return []
  const total = Object.values(s.evidence_distribution).reduce((a, b) => a + b, 0) || 1
  return Object.entries(s.evidence_distribution)
    .map(([key, count]) => ({
      key, label: EVIDENCE_META[key]?.label ?? key,
      color: EVIDENCE_META[key]?.color ?? 'var(--cr-evidence-other)',
      count, percent: Math.round((count / total) * 1000) / 10,
    }))
    .sort((a, b) => b.count - a.count)
})

const maxPhaseCount = computed(() => Math.max(1, ...phaseRows.value.map((r) => r.count)))
const maxEvidenceCount = computed(() => Math.max(1, ...evidenceRows.value.map((r) => r.count)))

/** 健康度等级（用于配色） */
const healthLevel = computed(() => {
  const h = quality.value?.health ?? 0
  if (h >= 0.8) return { label: '良好', type: 'success' }
  if (h >= 0.6) return { label: '一般', type: 'warning' }
  return { label: '需改进', type: 'danger' }
})

/* ===================== 数据加载 ===================== */
async function loadStats() {
  try {
    stats.value = await api.stats()
  } catch (err) {
    ElMessage.error((err as Error).message)
  }
}

async function loadQuality() {
  if (!canView.value) return
  const scope = ownerScope
  try {
    const data = await api.kbQuality()
    if (scope === ownerScope) quality.value = data
  } catch { /* 无权限时静默 */ }
}

async function loadDocs() {
  if (!canView.value) return
  const scope = ownerScope
  try {
    const params: Record<string, unknown> = {
      limit: docQuery.limit, offset: docQuery.offset,
    }
    if (docQuery.keyword) params.keyword = docQuery.keyword
    if (docQuery.phase) params.phase = docQuery.phase
    if (docQuery.evidence_level) params.evidence_level = docQuery.evidence_level
    if (docQuery.language) params.language = docQuery.language
    if (docQuery.year_from) params.year_from = docQuery.year_from
    if (docQuery.is_reviewed !== '') params.is_reviewed = docQuery.is_reviewed === 'true'
    if (docQuery.is_active !== '') params.is_active = docQuery.is_active === 'true'

    const d = await api.kbDocuments(params)
    if (scope !== ownerScope) return
    docs.value = d.items
    docTotal.value = d.total
  } catch (err) {
    ElMessage.error((err as Error).message)
  }
}

async function loadBatches() {
  if (!canView.value) return
  const scope = ownerScope
  try {
    const d = await api.kbBatches({ limit: 30 })
    if (scope !== ownerScope) return
    batches.value = d.items
    batchTotal.value = d.total
  } catch { /* ignore */ }
}

async function loadTasks() {
  if (!canView.value) return
  const scope = ownerScope
  try {
    const data = await api.kbTasks(10)
    if (scope === ownerScope) recentJobs.value = data
  } catch { /* ignore */ }
}

async function loadAll() {
  loading.value = true
  try {
    await Promise.all([
      loadStats(), loadQuality(), loadDocs(), loadBatches(), loadTasks(),
    ])
  } finally {
    loading.value = false
  }
}

/* ===================== 文献操作 ===================== */
function resetDocQuery() {
  Object.assign(docQuery, {
    keyword: '', phase: '', evidence_level: '', language: '',
    is_reviewed: '', is_active: '', year_from: undefined, offset: 0,
  })
  loadDocs()
}

async function reviewDoc(row: any, approved: boolean) {
  if (!canReview.value) {
    ElMessage.warning('无权审核文献')
    return
  }
  try {
    let note: string | undefined
    if (!approved) {
      const r = await ElMessageBox.prompt(
        '请填写驳回原因（便于后续追溯）', '驳回文献',
        { inputPlaceholder: '例如：与课题范围无关' },
      )
      note = r.value
    }
    await api.kbReview(row.id, approved, note)
    ElMessage.success(approved ? '已通过' : '已驳回并自动下架')
    await Promise.all([loadDocs(), loadQuality(), loadStats()])
  } catch (err) {
    if (err !== 'cancel' && err !== 'close') ElMessage.error((err as Error).message)
  }
}

async function batchReview(approved: boolean) {
  if (!selectedDocs.value.length) {
    ElMessage.warning('请先勾选文献')
    return
  }
  const n = selectedDocs.value.length
  try {
    await ElMessageBox.confirm(
      `确认${approved ? '通过' : '驳回'}所选的 ${n} 篇文献？` +
      (approved ? '' : '驳回会同时将其下架。'),
      '批量审核', { type: 'warning' },
    )
  } catch { return }
  try {
    const d = await api.kbBatchReview(selectedDocs.value.map((x) => x.id), approved)
    ElMessage.success(d.message)
    selectedDocs.value = []
    await Promise.all([loadDocs(), loadQuality(), loadStats()])
  } catch (err) {
    ElMessage.error((err as Error).message)
  }
}

async function toggleActive(row: any) {
  if (!canWrite.value) {
    ElMessage.warning('无权上架/下架文献')
    return
  }
  try {
    await api.kbSetActive(row.id, !row.is_active)
    ElMessage.success(row.is_active ? '已下架（不再被检索到）' : '已上架')
    await Promise.all([loadDocs(), loadQuality(), loadStats()])
  } catch (err) {
    ElMessage.error((err as Error).message)
  }
}

async function deleteDoc(row: any) {
  if (!canDelete.value) {
    ElMessage.warning('无权彻底删除文献')
    return
  }
  try {
    await ElMessageBox.confirm(
      `确认彻底删除《${(row.title || '').slice(0, 40)}》及其 ${row.chunk_count} 个文本块？` +
      '此操作不可恢复，且历史问答对该文献的引用将失效。',
      '彻底删除', { type: 'error', confirmButtonText: '确认删除' },
    )
  } catch { return }
  try {
    const d = await api.kbDeleteDocument(row.id)
    ElMessage.success(d.message)
    detailDialog.value = false
    await Promise.all([loadDocs(), loadQuality(), loadStats()])
  } catch (err) {
    ElMessage.error((err as Error).message)
  }
}

async function openDetail(row: any) {
  const request = ++detailRequest
  detailDialog.value = true
  detailLoading.value = true
  docDetail.value = null
  docRevisions.value = []
  revisionEvidence.value = null
  try {
    const [detail, history] = await Promise.all([api.kbDocument(row.id), api.kbDocumentRevisions(row.id)])
    if (request !== detailRequest) return
    docDetail.value = detail
    docRevisions.value = history.revisions
  } catch (err) {
    ElMessage.error((err as Error).message)
  } finally {
    if (request === detailRequest) detailLoading.value = false
  }
}

async function openRevisionEvidence(chunkId: string) {
  const request = detailRequest
  const evidence = ++evidenceRequest
  try {
    const result = await api.chunkDetail(chunkId)
    if (request === detailRequest && evidence === evidenceRequest) revisionEvidence.value = result
  } catch (err) {
    ElMessage.error((err as Error).message)
  }
}

/* ===================== 更新任务 ===================== */
function stopPolling() {
  if (pollTimer !== null) {
    window.clearInterval(pollTimer)
    pollTimer = null
  }
}

function startPolling(jobId: string) {
  const scope = ownerScope
  stopPolling()
  activeJob.value = {
    job_id: jobId, status: 'running', stage: '已提交', percent: 0, elapsed_seconds: 0,
  }
  pollTimer = window.setInterval(async () => {
    try {
      const p = await api.kbTask(jobId)
      if (scope !== ownerScope) return
      activeJob.value = p
      if (p.status === 'success' || p.status === 'failed') {
        stopPolling()
        if (p.status === 'success') {
          ElMessage.success(`更新完成：${p.message || '任务完成'}`)
        } else {
          ElMessage.error(`更新失败：${p.error}`)
        }
        await Promise.all([
          loadStats(), loadQuality(), loadDocs(), loadBatches(), loadTasks(),
        ])
      }
    } catch {
      if (scope !== ownerScope) return
      // 任务丢失（后端重启）：停止轮询并提示
      stopPolling()
      ElMessage.warning('任务状态已丢失（可能后端重启），请查看更新历史')
      loadBatches()
    }
  }, 2000)
}

async function doCollect() {
  try {
    const payload: Record<string, unknown> = {
      mode: collectForm.mode, embed: collectForm.embed,
    }
    if (collectForm.max_total) payload.max_total = collectForm.max_total
    if (collectForm.mindate) payload.mindate = collectForm.mindate
    const job = await api.kbCollect(payload)
    collectDialog.value = false
    startPolling(job.job_id)
    ElMessage.info('采集任务已提交，正在后台执行')
  } catch (err) {
    ElMessage.error((err as Error).message)
  }
}

async function doSearchCollect() {
  if (!searchForm.query.trim()) {
    ElMessage.warning('请输入 PubMed 检索式')
    return
  }
  try {
    const job = await api.kbSearchCollect({
      query: searchForm.query, limit: searchForm.limit, embed: searchForm.embed,
    })
    searchDialog.value = false
    startPolling(job.job_id)
    ElMessage.info('采集任务已提交，正在后台执行')
  } catch (err) {
    ElMessage.error((err as Error).message)
  }
}

async function doVectorize() {
  try {
    const job = await api.kbVectorize({ batch_size: 32 })
    startPolling(job.job_id)
    ElMessage.info('向量化任务已提交')
  } catch (err) {
    ElMessage.error((err as Error).message)
  }
}

async function openBatch(row: any) {
  batchDialog.value = true
  batchDetail.value = null
  try {
    batchDetail.value = await api.kbBatch(row.id)
  } catch (err) {
    ElMessage.error((err as Error).message)
  }
}

/* ===================== 工具 ===================== */
function fmtTime(s?: string | null) {
  if (!s) return '—'
  return s.replace('T', ' ').slice(0, 19)
}

function fmtElapsed(sec?: number | null) {
  if (!sec) return '—'
  if (sec < 60) return `${sec.toFixed(0)} 秒`
  if (sec < 3600) return `${(sec / 60).toFixed(1)} 分`
  return `${(sec / 3600).toFixed(1)} 小时`
}

function evLabel(k?: string | null) {
  return EVIDENCE_META[k ?? 'other']?.label ?? (k || '—')
}

onMounted(() => {
  loadAll()
  // 页面加载时若已有任务在执行，自动接上进度
  const scope = ownerScope
  if (canView.value) api.kbTasks(5).then((jobs: any[]) => {
    if (scope !== ownerScope) return
    const running = jobs.find((j) => j.status === 'running')
    if (running) {
      startPolling(running.job_id)
      activeTab.value = 'update'
    }
  }).catch(() => { /* ignore */ })
})

onUnmounted(() => {
  ownerScope++
  detailRequest++
  evidenceRequest++
  stopPolling()
})
</script>

<template>
  <div v-loading="loading" class="kb-view">
    <el-alert v-if="!canView" title="当前展示公开语料统计；登录授权账户后可查阅文献详情与管理记录" type="info" :closable="false" class="mb14" />
    <el-tabs v-model="activeTab">
      <!-- ==================== 质量评估 ==================== -->
      <el-tab-pane label="语料概览" name="overview">
        <div v-if="quality" class="health-row">
          <div class="health-card">
            <el-progress
              type="dashboard" :percentage="Math.round(quality.health * 100)"
              :width="118" :stroke-width="9"
              :color="quality.health >= 0.8 ? '#2e9e6b' : quality.health >= 0.6 ? '#d99b2b' : '#d9534f'"
            >
              <template #default>
                <div class="health-num">{{ Math.round(quality.health * 100) }}</div>
                <div class="health-cap">健康度</div>
              </template>
            </el-progress>
            <div class="health-meta">
              <el-tag :type="healthLevel.type as any" size="small" effect="dark">
                {{ healthLevel.label }}
              </el-tag>
              <div class="health-hint">
                各项质量指标的均值，用于快速判断，<b>不参与检索决策</b>
              </div>
            </div>
          </div>

          <div class="score-card">
            <div class="score-title">分项指标</div>
            <div v-for="(v, k) in quality.scores" :key="k" class="score-line">
              <span class="score-name">{{ k }}</span>
              <el-progress
                :percentage="Math.round(Number(v) * 100)"
                :stroke-width="7" :show-text="false"
                :color="Number(v) >= 0.8 ? '#2e9e6b' : Number(v) >= 0.5 ? '#d99b2b' : '#d9534f'"
                style="flex: 1; margin: 0 10px"
              />
              <span class="score-val">{{ (Number(v) * 100).toFixed(1) }}%</span>
            </div>
          </div>
        </div>

        <el-alert
          v-if="quality && quality.suggestions && quality.suggestions.length"
          type="warning" :closable="false" show-icon class="mb14"
        >
          <template #title>维护建议（{{ quality.suggestions.length }} 条）</template>
          <ul class="sug-list">
            <li v-for="(s, i) in quality.suggestions" :key="i">{{ s }}</li>
          </ul>
        </el-alert>

        <div v-if="stats" class="stat-cards">
          <div class="sc">
            <div class="sc-label">文献</div>
            <div class="sc-value">{{ stats.documents }}</div>
            <div class="sc-sub">
              <template v-if="quality">
                上架 {{ quality.scale.documents_active }} ·
                下架 {{ quality.scale.documents_inactive }}
              </template>
            </div>
          </div>
          <div class="sc">
            <div class="sc-label">文本块</div>
            <div class="sc-value">{{ stats.total_chunks }}</div>
            <div class="sc-sub">向量化完成 {{ embedRate }}%</div>
          </div>
          <div v-if="quality" class="sc">
            <div class="sc-label">最新文献年份</div>
            <div class="sc-value">{{ quality.timeliness.year_range[1] }}</div>
            <div class="sc-sub">
              {{ quality.timeliness.year_range[0] }}–{{ quality.timeliness.year_range[1] }} ·
              近三年 {{ (quality.timeliness.recent_ratio * 100).toFixed(1) }}%
            </div>
          </div>
          <div v-if="quality" class="sc">
            <div class="sc-label">中文文献</div>
            <div class="sc-value">{{ quality.chinese_documents }}</div>
            <div class="sc-sub">共 {{ Object.keys(quality.language).length }} 个语种</div>
          </div>
          <div v-if="quality" class="sc">
            <div class="sc-label">已审核</div>
            <div class="sc-value">{{ quality.scale.documents_reviewed }}</div>
            <div class="sc-sub">/ {{ quality.scale.documents }} 篇</div>
          </div>
        </div>

        <div class="charts">
          <div class="chart-block">
            <div class="chart-title">围术期分期分布</div>
            <div v-for="r in phaseRows" :key="r.key" class="bar-line">
              <span class="bar-name">{{ r.label }}</span>
              <div class="bar-track">
                <div
                  class="bar-fill"
                  :style="{ width: (r.count / maxPhaseCount * 100) + '%', background: 'var(--cr-primary)' }"
                />
              </div>
              <span class="bar-val">{{ r.count }} · {{ r.percent }}%</span>
            </div>
          </div>

          <div class="chart-block">
            <div class="chart-title">证据等级分布</div>
            <div v-for="r in evidenceRows" :key="r.key" class="bar-line">
              <span class="bar-name">{{ r.label }}</span>
              <div class="bar-track">
                <div
                  class="bar-fill"
                  :style="{ width: (r.count / maxEvidenceCount * 100) + '%', background: r.color }"
                />
              </div>
              <span class="bar-val">{{ r.count }} · {{ r.percent }}%</span>
            </div>
          </div>
        </div>

        <div v-if="quality" class="issues-block">
          <div class="chart-title">异常项</div>
          <el-descriptions :column="3" border size="small">
            <el-descriptions-item label="缺摘要">
              {{ quality.issues.no_abstract }} 篇
            </el-descriptions-item>
            <el-descriptions-item label="缺 DOI/PMID">
              {{ quality.issues.no_identifier }} 篇
            </el-descriptions-item>
            <el-descriptions-item label="未向量化文本块">
              {{ quality.scale.chunks_unembedded }} 个
            </el-descriptions-item>
          </el-descriptions>
        </div>
      </el-tab-pane>

      <!-- ==================== 文献管理 ==================== -->
      <el-tab-pane v-if="canView" label="文献管理" name="documents">
        <div class="filter-bar">
          <el-input
            v-model="docQuery.keyword" placeholder="标题 / 作者 / 期刊 / DOI / PMID"
            size="small" clearable style="width: 230px" @keyup.enter="loadDocs"
          />
          <el-select v-model="docQuery.phase" placeholder="分期" size="small" clearable style="width: 100px">
            <el-option v-for="(v, k) in PHASE_LABELS" :key="k" :label="v" :value="k" />
          </el-select>
          <el-select v-model="docQuery.evidence_level" placeholder="证据等级" size="small" clearable style="width: 138px">
            <el-option v-for="(v, k) in EVIDENCE_META" :key="k" :label="v.label" :value="k" />
          </el-select>
          <el-select v-model="docQuery.language" placeholder="语种" size="small" clearable style="width: 95px">
            <el-option label="英文" value="en" />
            <el-option label="中文" value="chi" />
            <el-option label="德文" value="ger" />
            <el-option label="日文" value="jpn" />
          </el-select>
          <el-select v-model="docQuery.is_reviewed" placeholder="审核" size="small" clearable style="width: 100px">
            <el-option label="已审核" value="true" />
            <el-option label="未审核" value="false" />
          </el-select>
          <el-select v-model="docQuery.is_active" placeholder="上架" size="small" clearable style="width: 100px">
            <el-option label="已上架" value="true" />
            <el-option label="已下架" value="false" />
          </el-select>
          <el-input-number
            v-model="docQuery.year_from" :controls="false" placeholder="起始年"
            size="small" style="width: 96px"
          />
          <el-button size="small" type="primary" @click="docQuery.offset = 0; loadDocs()">查询</el-button>
          <el-button size="small" @click="resetDocQuery">重置</el-button>
        </div>

        <el-alert type="info" :closable="false" show-icon class="mb12">
          <template #title>关于「原文语种」</template>
          语种按文献<strong>标题与正文的实际文字</strong>自动判定（中文文献可人工复核）：
          <strong>中文</strong>为人工整理的中文指南与期刊全文，可直接做中文字面匹配；
          <strong>英文</strong>为外文文献全文，中文提问依靠向量模型的跨语言能力匹配。
          若个别文献判定有误，可在元数据中写 <code>**语种**：zh</code> 或 <code>**语种**：en</code> 强制指定后重新导入。
        </el-alert>

        <div class="tab-toolbar">
          <span class="muted">共 {{ docTotal }} 篇</span>
          <div v-if="canReview">
            <el-button
              size="small" type="success" :disabled="!selectedDocs.length"
              @click="batchReview(true)"
            >
              批量通过 ({{ selectedDocs.length }})
            </el-button>
            <el-button
              size="small" type="danger" :disabled="!selectedDocs.length"
              @click="batchReview(false)"
            >
              批量驳回
            </el-button>
          </div>
        </div>

        <el-table
          :data="docs" size="small" stripe
          @selection-change="(v: any[]) => (selectedDocs = v)"
        >
          <el-table-column v-if="canReview" type="selection" width="38" />
          <el-table-column label="标题" min-width="260">
            <template #default="{ row }">
              <a class="doc-title" @click="openDetail(row)">{{ row.title }}</a>
              <div class="doc-sub">
                {{ row.journal || '—' }} · {{ row.year || '—' }}
                <template v-if="row.pmid"> · PMID {{ row.pmid }}</template>
              </div>
            </template>
          </el-table-column>
          <el-table-column label="分期" width="62">
            <template #default="{ row }">{{ PHASE_LABELS[row.phase] || '—' }}</template>
          </el-table-column>
          <el-table-column label="证据" width="118">
            <template #default="{ row }">
              <el-tag size="small" effect="plain">{{ evLabel(row.evidence_level) }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="原文语种" width="86">
            <template #default="{ row }">
              <el-tooltip
                v-if="row.language === 'chi'"
                content="原文为中文，语料含中文正文，可直接做中文字面匹配"
                placement="top"
              >
                <el-tag size="small" type="success" effect="plain">中文</el-tag>
              </el-tooltip>
              <el-tooltip
                v-else-if="row.language === 'en' || row.language === 'eng'"
                content="原文为英文，中文提问由向量模型跨语言匹配"
                placement="top"
              >
                <el-tag size="small" type="info" effect="plain">英文</el-tag>
              </el-tooltip>
              <span v-else>{{ row.language || '—' }}</span>
            </template>
          </el-table-column>
          <el-table-column label="块" width="46">
            <template #default="{ row }">{{ row.chunk_count }}</template>
          </el-table-column>
          <el-table-column label="状态" width="86">
            <template #default="{ row }">
              <el-tag v-if="!row.is_active" type="info" size="small">已下架</el-tag>
              <el-tag v-else-if="row.is_reviewed" type="success" size="small">已审核</el-tag>
              <el-tag v-else type="warning" size="small">待审核</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="操作" width="172" fixed="right">
            <template #default="{ row }">
              <template v-if="canReview && !row.is_reviewed">
                <el-button link size="small" type="success" @click="reviewDoc(row, true)">通过</el-button>
                <el-button link size="small" type="danger" @click="reviewDoc(row, false)">驳回</el-button>
              </template>
              <el-button v-if="canWrite" link size="small" @click="toggleActive(row)">
                {{ row.is_active ? '下架' : '上架' }}
              </el-button>
              <el-button link size="small" @click="openDetail(row)">详情</el-button>
            </template>
          </el-table-column>
        </el-table>

        <el-pagination
          class="pager" size="small" layout="prev, pager, next, total"
          :total="docTotal" :page-size="docQuery.limit"
          :current-page="Math.floor(docQuery.offset / docQuery.limit) + 1"
          @current-change="(p: number) => { docQuery.offset = (p - 1) * docQuery.limit; loadDocs() }"
        />
      </el-tab-pane>

      <!-- ==================== 更新知识库 ==================== -->
      <el-tab-pane v-if="canWrite" label="更新知识库" name="update">
        <el-alert type="info" :closable="false" show-icon class="mb14">
          <template #title>更新的三步，与一个最容易忽略的点</template>
          ① 采集入库 → ② 补齐向量（入库时未向量化则需执行）→ ③ <b>重建知识图谱</b>。<br>
          <b>第 ③ 步最易被忽略</b>：新入库的文献不会自动进入图谱，
          图谱检索路径将看不到它们。重建需在命令行执行
          <code>python scripts\build_graph.py build</code>（现有规模约 1 小时）。
        </el-alert>

        <div v-if="activeJob" class="job-card">
          <div class="job-head">
            <el-tag
              :type="(STATUS_META[activeJob.status]?.type || 'info') as any"
              size="small" effect="dark"
            >
              {{ STATUS_META[activeJob.status]?.label || activeJob.status }}
            </el-tag>
            <span class="job-op">{{ OP_LABELS[activeJob.operation] || activeJob.operation }}</span>
            <span class="muted">已用 {{ fmtElapsed(activeJob.elapsed_seconds) }}</span>
          </div>
          <el-progress
            :percentage="activeJob.percent"
            :status="activeJob.status === 'failed' ? 'exception' : undefined"
            :stroke-width="12" striped
            :striped-flow="activeJob.status === 'running'"
          />
          <div class="job-stage">{{ activeJob.stage }}</div>
          <div v-if="activeJob.message" class="job-msg">{{ activeJob.message }}</div>
          <div v-if="activeJob.error" class="job-err">{{ activeJob.error }}</div>
          <div v-if="activeJob.status === 'success' && activeJob.result" class="job-result">
            结果：新增 {{ activeJob.result.documents_created ?? 0 }} 篇 ·
            新增文本块 {{ activeJob.result.chunks_created ?? 0 }} ·
            补向量 {{ activeJob.result.chunks_vectorized ?? 0 }} 块
          </div>
        </div>

        <div v-if="canWrite" class="update-actions">
          <el-button type="primary" @click="collectDialog = true">
            <el-icon><Download /></el-icon>按预定义检索式采集
          </el-button>
          <el-button @click="searchDialog = true">
            <el-icon><Search /></el-icon>自定义检索式采集
          </el-button>
          <el-button @click="doVectorize">
            <el-icon><Refresh /></el-icon>补齐向量
          </el-button>
        </div>
        <el-alert v-else type="info" :closable="false" show-icon class="mb14">
          当前角色为「{{ auth.user?.role_label }}」，仅可查看更新状态。
          触发更新需要管理员权限（kb:update）。
        </el-alert>

        <div class="chart-title" style="margin-top: 22px">最近任务</div>
        <el-table :data="recentJobs" size="small" stripe>
          <el-table-column label="时间" width="150">
            <template #default="{ row }">{{ fmtTime(row.started_at) }}</template>
          </el-table-column>
          <el-table-column label="操作" width="110">
            <template #default="{ row }">{{ OP_LABELS[row.operation] || row.operation }}</template>
          </el-table-column>
          <el-table-column label="状态" width="90">
            <template #default="{ row }">
              <el-tag :type="(STATUS_META[row.status]?.type || 'info') as any" size="small">
                {{ STATUS_META[row.status]?.label || row.status }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="stage" label="阶段" min-width="200" show-overflow-tooltip />
          <el-table-column label="耗时" width="90">
            <template #default="{ row }">{{ fmtElapsed(row.elapsed_seconds) }}</template>
          </el-table-column>
        </el-table>
      </el-tab-pane>

      <!-- ==================== 版本历史 ==================== -->
      <el-tab-pane v-if="canView" label="更新历史" name="history">
        <div class="tab-toolbar">
          <span class="muted">
            共 {{ batchTotal }} 次更新 · 记录「何时、由谁、用什么检索式、更新了多少」
          </span>
        </div>
        <el-table :data="batches" size="small" stripe>
          <el-table-column label="时间" width="150">
            <template #default="{ row }">{{ fmtTime(row.created_at) }}</template>
          </el-table-column>
          <el-table-column label="操作" width="110">
            <template #default="{ row }">{{ OP_LABELS[row.operation] || row.operation }}</template>
          </el-table-column>
          <el-table-column label="状态" width="86">
            <template #default="{ row }">
              <el-tag :type="(STATUS_META[row.status]?.type || 'info') as any" size="small">
                {{ STATUS_META[row.status]?.label || row.status }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column label="新增" width="82">
            <template #default="{ row }">{{ row.documents_created }} 篇</template>
          </el-table-column>
          <el-table-column label="文本块" width="82">
            <template #default="{ row }">
              {{ row.chunks_created || row.chunks_vectorized || 0 }}
            </template>
          </el-table-column>
          <el-table-column label="耗时" width="90">
            <template #default="{ row }">{{ fmtElapsed(row.elapsed_seconds) }}</template>
          </el-table-column>
          <el-table-column prop="operator_name" label="操作人" width="90">
            <template #default="{ row }">{{ row.operator_name || '—' }}</template>
          </el-table-column>
          <el-table-column label="图谱" width="90">
            <template #default="{ row }">
              <el-tag v-if="row.graph_rebuilt" type="success" size="small">已重建</el-tag>
              <el-tag v-else-if="row.documents_created > 0" type="warning" size="small">待重建</el-tag>
              <span v-else class="muted">—</span>
            </template>
          </el-table-column>
          <el-table-column label="操作" width="80">
            <template #default="{ row }">
              <el-button link size="small" @click="openBatch(row)">详情</el-button>
            </template>
          </el-table-column>
        </el-table>
      </el-tab-pane>
    </el-tabs>

    <!-- ============ 文献详情 ============ -->
    <el-dialog v-model="detailDialog" title="文献详情" width="min(780px, 94vw)" top="6vh">
      <div v-loading="detailLoading">
        <template v-if="docDetail">
          <h4 class="d-title">{{ docDetail.title }}</h4>
          <div class="d-sub">
            {{ docDetail.authors || '—' }}<br>
            {{ docDetail.journal || '—' }} · {{ docDetail.year || '—' }}
            <template v-if="docDetail.volume"> · {{ docDetail.volume }}({{ docDetail.issue }})</template>
            <template v-if="docDetail.pages">: {{ docDetail.pages }}</template>
          </div>
          <el-descriptions :column="3" border size="small" class="mb10">
            <el-descriptions-item label="分期">
              {{ PHASE_LABELS[docDetail.phase] || '—' }}
            </el-descriptions-item>
            <el-descriptions-item label="证据等级">{{ evLabel(docDetail.evidence_level) }}</el-descriptions-item>
            <el-descriptions-item label="语种">{{ docDetail.language || '—' }}</el-descriptions-item>
            <el-descriptions-item label="DOI">{{ docDetail.doi || '—' }}</el-descriptions-item>
            <el-descriptions-item label="PMID">{{ docDetail.pmid || '—' }}</el-descriptions-item>
            <el-descriptions-item label="文本块">{{ docDetail.chunk_count }}</el-descriptions-item>
            <el-descriptions-item label="审核状态">
              <el-tag :type="docDetail.is_reviewed ? 'success' : 'warning'" size="small">
                {{ docDetail.is_reviewed ? `已审核（${docDetail.reviewed_by}）` : '待审核' }}
              </el-tag>
            </el-descriptions-item>
            <el-descriptions-item label="上架状态">
              <el-tag :type="docDetail.is_active ? 'success' : 'info'" size="small">
                {{ docDetail.is_active ? '已上架' : '已下架' }}
              </el-tag>
            </el-descriptions-item>
            <el-descriptions-item label="来源批次">
              {{ docDetail.batch ? OP_LABELS[docDetail.batch.operation] : '初始采集' }}
            </el-descriptions-item>
          </el-descriptions>

          <div class="d-section">摘要</div>
          <div class="d-abstract">{{ docDetail.abstract || '（无摘要）' }}</div>

          <div class="d-section">文本块（{{ (docDetail.chunks || []).length }} 个）</div>
          <el-collapse v-if="docRevisions.length">
            <el-collapse-item title="查看修订前的历史证据" name="revisions">
              <div v-for="revision in docRevisions" :key="revision.revision_id">
                <p>{{ revision.archived_at }} · {{ revision.chunk_count }} 个文本块</p>
                <el-button v-for="(id, index) in revision.chunk_ids" :key="id" link @click="openRevisionEvidence(id)">
                  原文 {{ Number(index) + 1 }}
                </el-button>
              </div>
              <div v-if="revisionEvidence" class="chunk-text">
                <strong>{{ revisionEvidence.doc_title }} · 历史版本 · {{ revisionEvidence.page_start ? `第${revisionEvidence.page_start}页` : '页码未提供' }}</strong>
                <p>{{ revisionEvidence.content }}</p>
              </div>
            </el-collapse-item>
          </el-collapse>
          <div v-for="c in docDetail.chunks" :key="c.id" class="chunk-item">
            <div class="chunk-head">
              <el-tag size="small" effect="plain">#{{ c.index }}</el-tag>
              <span class="chunk-sec">{{ c.section || '正文' }}</span>
              <el-tag v-if="c.page_start" size="small" effect="plain">PDF 第{{ c.page_start }}页</el-tag>
              <span class="muted">{{ c.char_count }} 字 · {{ c.token_count }} token</span>
              <el-tag v-if="c.embedded" type="success" size="small" effect="plain">已向量化</el-tag>
              <el-tag v-else type="danger" size="small" effect="plain">未向量化</el-tag>
            </div>
            <div class="chunk-text">{{ c.preview }}</div>
          </div>
        </template>
      </div>
      <template #footer>
        <el-button v-if="canWrite && docDetail" @click="toggleActive(docDetail)">
          {{ docDetail.is_active ? '下架此文' : '重新上架' }}
        </el-button>
        <el-button
          v-if="canDelete && docDetail" type="danger" plain @click="deleteDoc(docDetail)"
        >
          彻底删除
        </el-button>
        <el-button @click="detailDialog = false">关闭</el-button>
      </template>
    </el-dialog>

    <!-- ============ 采集确认 ============ -->
    <el-dialog v-model="collectDialog" title="按预定义检索式采集" width="min(480px, 94vw)">
      <el-alert type="warning" :closable="false" show-icon class="mb12">
        任务在后台执行，可关闭页面。采集 + 向量化预计数十分钟。
      </el-alert>
      <el-form label-width="110px" size="small">
        <el-form-item label="检索式组">
          <el-select v-model="collectForm.mode" style="width: 100%">
            <el-option
              label="补充检索式（17 个：中文文献 + 近三年高证据）" value="supplement"
            />
            <el-option label="主检索式（9 个：术前/术中/术后/通用）" value="default" />
            <el-option label="全部（26 个）" value="all" />
          </el-select>
          <div class="form-hint">
            初始语料中文仅 17 篇（3.8%）、近三年仅 49 篇（11.0%）——
            因主检索式无时间与语言过滤。补充检索式针对性修正这两点。
          </div>
        </el-form-item>
        <el-form-item label="总量上限">
          <el-input-number v-model="collectForm.max_total" :min="1" :max="5000" placeholder="不填用默认" />
        </el-form-item>
        <el-form-item label="起始日期">
          <el-input v-model="collectForm.mindate" placeholder="YYYY/MM/DD，如 2022/01/01" />
        </el-form-item>
        <el-form-item label="同时向量化">
          <el-switch v-model="collectForm.embed" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="collectDialog = false">取消</el-button>
        <el-button type="primary" @click="doCollect">开始采集</el-button>
      </template>
    </el-dialog>

    <!-- ============ 自定义检索式 ============ -->
    <el-dialog v-model="searchDialog" title="自定义检索式采集" width="min(600px, 94vw)">
      <el-form label-width="100px" size="small">
        <el-form-item label="PubMed 检索式">
          <el-input
            v-model="searchForm.query" type="textarea" :rows="4"
            placeholder="例如：colorectal surgery AND anastomotic leak AND (fluorescence OR indocyanine green)"
          />
        </el-form-item>
        <el-form-item label="最多条数">
          <el-input-number v-model="searchForm.limit" :min="1" :max="500" />
        </el-form-item>
        <el-form-item label="同时向量化">
          <el-switch v-model="searchForm.embed" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="searchDialog = false">取消</el-button>
        <el-button type="primary" @click="doSearchCollect">开始采集</el-button>
      </template>
    </el-dialog>

    <!-- ============ 批次详情 ============ -->
    <el-dialog v-model="batchDialog" title="批次详情" width="min(760px, 94vw)" top="8vh">
      <template v-if="batchDetail">
        <el-descriptions :column="2" border size="small">
          <el-descriptions-item label="操作">
            {{ OP_LABELS[batchDetail.batch.operation] || batchDetail.batch.operation }}
          </el-descriptions-item>
          <el-descriptions-item label="状态">
            {{ STATUS_META[batchDetail.batch.status]?.label || batchDetail.batch.status }}
          </el-descriptions-item>
          <el-descriptions-item label="操作人">
            {{ batchDetail.batch.operator_name || '—' }}
          </el-descriptions-item>
          <el-descriptions-item label="耗时">
            {{ fmtElapsed(batchDetail.batch.elapsed_seconds) }}
          </el-descriptions-item>
          <el-descriptions-item label="检索式" :span="2">
            <span class="query-text">{{ batchDetail.batch.query || '（无）' }}</span>
          </el-descriptions-item>
        </el-descriptions>

        <div class="d-section">本次新增文献（{{ batchDetail.documents_total }} 篇）</div>
        <el-table :data="batchDetail.documents" size="small" max-height="300">
          <el-table-column prop="title" label="标题" min-width="240" show-overflow-tooltip />
          <el-table-column prop="year" label="年" width="60" />
          <el-table-column label="证据" width="118">
            <template #default="{ row }">{{ evLabel(row.evidence_level) }}</template>
          </el-table-column>
        </el-table>

        <template v-if="batchDetail.batch.errors && batchDetail.batch.errors.length">
          <div class="d-section">
            错误（{{ batchDetail.batch.errors.length }} 条，最多显示 20 条）
          </div>
          <ul class="sug-list">
            <li v-for="(e, i) in batchDetail.batch.errors" :key="i">{{ e }}</li>
          </ul>
        </template>
      </template>
      <template #footer>
        <el-button @click="batchDialog = false">关闭</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.kb-view {
  padding: 16px 20px 28px;
  height: 100%;
  overflow-y: auto;
}

.mb10 { margin-bottom: 10px; }
.mb12 { margin-bottom: 12px; }
.mb14 { margin-bottom: 14px; }
.muted { color: var(--cr-text-muted, #8a99a8); font-size: 12px; }

/* ---------- 健康度区 ---------- */
.health-row {
  display: grid;
  grid-template-columns: 320px 1fr;
  gap: 16px;
  margin-bottom: 16px;
}

.health-card,
.score-card {
  padding: 16px;
  background: #fff;
  border: 1px solid var(--cr-border);
  border-radius: 8px;
}

.health-card {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 10px;
}

.health-num {
  font-size: 26px;
  font-weight: 700;
  color: var(--cr-primary);
}

.health-cap {
  font-size: 12px;
  color: var(--cr-text-muted, #8a99a8);
}

.health-meta { text-align: center; }

.health-hint {
  margin-top: 6px;
  font-size: 11px;
  line-height: 1.6;
  color: var(--cr-text-muted, #8a99a8);
}

.score-title {
  margin-bottom: 12px;
  font-size: 13px;
  font-weight: 600;
  color: var(--cr-primary);
}

.score-line {
  display: flex;
  align-items: center;
  margin-bottom: 9px;
}

.score-name {
  width: 108px;
  font-size: 12px;
  color: #5a6a78;
}

.score-val {
  width: 52px;
  font-size: 12px;
  text-align: right;
  color: #5a6a78;
}

.sug-list {
  margin: 4px 0 0;
  padding-left: 18px;
  font-size: 12.5px;
  line-height: 1.9;
}

/* ---------- 统计卡 ---------- */
.stat-cards {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(172px, 1fr));
  gap: 12px;
  margin-bottom: 18px;
}

.sc {
  padding: 13px 15px;
  background: #fff;
  border: 1px solid var(--cr-border);
  border-radius: 8px;
}

.sc-label { font-size: 12px; color: var(--cr-text-muted, #8a99a8); }

.sc-value {
  margin: 5px 0 3px;
  font-size: 22px;
  font-weight: 700;
  color: var(--cr-primary);
}

.sc-sub { font-size: 11px; line-height: 1.5; color: var(--cr-text-muted, #8a99a8); }

/* ---------- 分布图 ---------- */
.charts {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(360px, 1fr));
  gap: 18px;
  margin-bottom: 18px;
}

.chart-block {
  padding: 15px 17px;
  background: #fff;
  border: 1px solid var(--cr-border);
  border-radius: 8px;
}

.chart-title {
  margin-bottom: 12px;
  font-size: 13px;
  font-weight: 600;
  color: var(--cr-primary);
}

.bar-line {
  display: flex;
  align-items: center;
  margin-bottom: 9px;
}

.bar-name {
  width: 124px;
  font-size: 12px;
  color: #5a6a78;
}

.bar-track {
  flex: 1;
  height: 15px;
  margin-right: 10px;
  background: #f2f5f7;
  border-radius: 3px;
  overflow: hidden;
}

.bar-fill {
  height: 100%;
  border-radius: 3px;
  transition: width 0.4s ease;
}

.bar-val {
  width: 88px;
  font-size: 11.5px;
  text-align: right;
  color: var(--cr-text-muted, #8a99a8);
}

.issues-block { margin-top: 4px; }

/* ---------- 筛选与工具条 ---------- */
.filter-bar {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
  padding: 12px 0;
}

.tab-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 6px 0 12px;
}

.pager { margin-top: 14px; justify-content: flex-end; }

/* ---------- 文献表 ---------- */
.doc-title {
  color: var(--cr-primary);
  cursor: pointer;
  font-size: 12.5px;
  line-height: 1.5;
}

.doc-title:hover { text-decoration: underline; }

.doc-sub {
  margin-top: 2px;
  font-size: 11px;
  color: var(--cr-text-muted, #8a99a8);
}

/* ---------- 更新任务 ---------- */
.job-card {
  padding: 16px 18px;
  margin-bottom: 16px;
  background: #fff;
  border: 1px solid var(--cr-border);
  border-left: 3px solid var(--cr-primary);
  border-radius: 8px;
}

.job-head {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 12px;
}

.job-op { font-size: 13px; font-weight: 600; }

.job-stage { margin-top: 8px; font-size: 12.5px; color: #5a6a78; }
.job-msg { margin-top: 4px; font-size: 12px; color: var(--cr-text-muted, #8a99a8); }
.job-err { margin-top: 6px; font-size: 12px; color: #d9534f; }
.job-result { margin-top: 8px; font-size: 12px; color: #2e9e6b; }

.update-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  margin-bottom: 6px;
}

/* ---------- 详情对话框 ---------- */
.d-title {
  margin: 0 0 6px;
  font-size: 14.5px;
  line-height: 1.5;
  color: #2b3a47;
}

.d-sub {
  margin-bottom: 12px;
  font-size: 12px;
  line-height: 1.7;
  color: var(--cr-text-muted, #8a99a8);
}

.d-section {
  margin: 15px 0 8px;
  font-size: 13px;
  font-weight: 600;
  color: var(--cr-primary);
}

.d-abstract {
  padding: 11px 13px;
  font-size: 12px;
  line-height: 1.8;
  color: #4a5a68;
  background: #f9fbfc;
  border-radius: 6px;
  max-height: 220px;
  overflow-y: auto;
}

.chunk-item {
  padding: 10px 12px;
  margin-bottom: 8px;
  background: #f9fbfc;
  border-radius: 6px;
}

.chunk-head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 6px;
}

.chunk-sec { font-size: 12px; font-weight: 600; color: #5a6a78; }

.chunk-text {
  font-size: 11.5px;
  line-height: 1.7;
  color: #5a6a78;
}

.form-hint {
  margin-top: 6px;
  font-size: 11px;
  line-height: 1.6;
  color: var(--cr-text-muted, #8a99a8);
}

.query-text {
  font-family: Consolas, monospace;
  font-size: 11px;
  word-break: break-all;
}
@media (max-width: 900px) {
 .health-row { grid-template-columns: 1fr; }
 .charts { grid-template-columns: minmax(0, 1fr); }
 .health-row > *, .charts > * { min-width: 0; }
}
@media (max-width: 600px) {
 .kb-view { padding: 12px; }
 .stat-cards { grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; }
 .chart-block { padding: 12px; }
 .bar-name { width: 100px; flex-shrink: 0; }
 .bar-val { width: 82px; flex-shrink: 0; }
 .sc { padding: 12px; }
 .chunk-text, .d-abstract { font-size: 14px; }
}
</style>
