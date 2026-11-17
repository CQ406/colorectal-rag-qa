<script setup lang="ts">
/**
 * 术中影像知识库与辅助解读。
 *
 * 对应报审表「（一）3 术中影像知识库模块」与「（三）4 影像辅助解读模块」。
 *
 * 设计要点
 * --------
 * 1. **合规定位**：影像解读是**辅助**而非诊断。界面显著位置展示免责声明，
 *    并明确说明"视觉模型描述可能有误、结论需医师判断"。
 * 2. **过程透明**：展示 VLM 的客观描述与「由描述构造的检索查询」，
 *    让用户看到系统的推理链路，而不是直接给一个黑箱结论。
 * 3. **未配置密钥时给出指引**：视觉模型走云端 API，未配置密钥是部署常态，
 *    界面应引导用户去申请而不是报错。
 */
import { computed, h, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useRoute } from 'vue-router'

import { api } from '@/api/client'
import { renderAnswer } from '@/utils/markdown'
import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()
const route = useRoute()
const canManage = computed(() => auth.can('image:update'))
const canPublish = computed(() => auth.can('image:publish'))
const page = ref(1)
const pageSize = 24
const publishing = ref(false)
const previewUrls = new Map<string, string>()
let previewScope = 0
let listRequest = 0
let detailRequest = 0
let similarRequest = 0

async function withPreview(item: any) {
  if (item.is_public !== false) return item
  let url = previewUrls.get(item.id)
  if (!url) {
    const scope = previewScope
    let blob: Blob
    try {
      blob = await api.privateImageBlob(item.id)
    } catch {
      return { ...item, url: '', preview_error: true }
    }
    if (scope !== previewScope) return { ...item, url: '' }
    url = previewUrls.get(item.id)
    if (!url) {
      url = URL.createObjectURL(blob)
      previewUrls.set(item.id, url)
    }
  }
  return { ...item, url }
}

function clearPrivatePreviews() {
  viewerOpen.value = false
  previewScope++
  for (const url of previewUrls.values()) URL.revokeObjectURL(url)
  previewUrls.clear()
}

/* ----------------------------- 状态 ----------------------------- */
const loading = ref(false)
const viewerOpen = ref(false)
const mobileDetail = ref(false)
const detailError = ref('')
const listError = ref('')
/**
 * 正在解读的**影像 ID**（而不是一个全局布尔）。
 *
 * 为什么不能用布尔：解读要 15~20 秒，用户很可能中途切到别的影像。
 * 用一个全局 `analyzing` 会让每张影像都显示「正在解读影像…」，
 * 而且请求返回后无条件写 `analysis` 会把结果错配到当前选中的影像上
 * （在 A 上点解读、切到 B，B 最后显示出 A 的结果）。
 * 绑到 ID 上即可：只有被解读的那张显示进度，结果也只回填给那张。
 */
const analyzingId = ref<string | null>(null)
const analyzing = computed(
  () => !!selected.value && analyzingId.value === selected.value.id,
)
const uploading = ref(false)

const images = ref<any[]>([])
const total = ref(0)
const stats = ref<any>(null)

/** 当前选中的影像与其解读结果 */
const selected = ref<any>(null)
const detail = ref<any>(null)
const analysis = ref<any>(null)
const similarImages = ref<any[]>([])
const similarLoadingId = ref<string | null>(null)
const similarLoading = computed(() => !!selected.value && similarLoadingId.value === selected.value.id)

/** 筛选条件 */
const filterPhase = ref<string | null>(null)
const filterKeyword = ref('')

/** 上传表单 */
const uploadVisible = ref(false)
const editingId = ref<string | null>(null)
const uploadForm = ref({
  file: null as File | null,
  title: '',
  surgery_type: '',
  anatomy_site: '',
  phase: 'intraop',
  description: '',
  source: '',
  is_public: false,
})
const fileInput = ref<HTMLInputElement | null>(null)
const previewUrl = ref('')

/** 解读参数 */
const topK = ref(5)
const imageContext = ref('')

const PHASE_LABELS: Record<string, string> = {
  preop: '术前',
  intraop: '术中',
  postop: '术后',
  general: '通用',
}

/** 视觉模型是否已配置 */
const visionReady = computed(() => stats.value?.vision_configured === true)

/** 解读结果的渲染 HTML（复用问答的 Markdown 渲染器，保证引用可点击） */
const analysisHtml = computed(() =>
  analysis.value?.interpretation ? renderAnswer(analysis.value.interpretation) : '',
)

const descriptionHtml = computed(() =>
  analysis.value?.description ? renderAnswer(analysis.value.description) : '',
)

/**
 * 「已解读」角标的说明文字。
 *
 * 该角标只表示**该张影像已缓存过客观描述**，是逐张独立的状态，
 * 与是否刚点过「辅助解读」无关。带上时间可避免误以为
 * 「点了一张，结果全部都解读了」。
 */
function analyzedHint(item: any): string {
  const t = item?.vlm_analyzed_at
  if (!t) return '该影像已缓存客观描述，可随时生成综合解读'
  const d = new Date(t)
  const pad = (n: number) => String(n).padStart(2, '0')
  const when = `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
  return `该影像已于 ${when} 解读并缓存客观描述`
}

/* ----------------------------- 数据加载 ----------------------------- */
async function loadStats() {
  const scope = previewScope
  try {
    const data = canManage.value ? await api.manageImageStats() : await api.imageStats()
    if (scope === previewScope) stats.value = data
  } catch {
    if (scope === previewScope) stats.value = null
  }
}

async function loadImages() {
  const requestId = ++listRequest
  loading.value = true
  listError.value = ''
  try {
    const list = canManage.value ? api.manageImages : api.images
    const data = await list({
      phase: filterPhase.value || undefined,
      keyword: filterKeyword.value || undefined,
      limit: pageSize,
      offset: (page.value - 1) * pageSize,
    })
    if (requestId !== listRequest) return
    const previews = await Promise.all(data.items.map(withPreview))
    if (requestId !== listRequest) return
    images.value = previews
    total.value = data.total
    if (!route.query.imageId && !selected.value && previews.length) void selectImage(previews[0], false)
  } catch (err) {
    if (requestId === listRequest) { listError.value = (err as Error).message; images.value = []; total.value = 0 }
  } finally {
    if (requestId === listRequest) loading.value = false
  }
}

function filterImages() {
  detailRequest++
  similarRequest++
  viewerOpen.value = false
  selected.value = null
  detail.value = null
  analysis.value = null
  similarImages.value = []
  mobileDetail.value = false
  page.value = 1
  void loadImages()
}

function resetImageFilters() {
  filterPhase.value = null
  filterKeyword.value = ''
  filterImages()
}
function imageTitle(item: any) {
  const title = String(item.title || '').trim()
  if (title && !/^[a-f0-9]{24,64}(?:\.[a-z]+)?$/i.test(title)) return title
  return item.anatomy_site ? `${item.anatomy_site} · 教学影像` : '未命名教学影像'
}
async function selectImage(item: any, showDetail = true) {
  mobileDetail.value = showDetail
  viewerOpen.value = false
  detailError.value = '' 
  const requestId = ++detailRequest
  similarRequest++
  similarLoadingId.value = null
  selected.value = item
  analysis.value = null
  similarImages.value = []
  detail.value = null
  try {
    const d = canManage.value ? await api.manageImageDetail(item.id) : await api.imageDetail(item.id)
    if (requestId !== detailRequest || selected.value?.id !== item.id) return
    const current = await withPreview(d)
    if (requestId !== detailRequest || selected.value?.id !== item.id) return
    selected.value = current
    detail.value = d

    // ① 有完整解读快照 → 直接原样渲染（含引用），切走再回来也不丢
    if (d.vlm_interpretation && d.vlm_interpretation.interpretation) {
      analysis.value = { ...d.vlm_interpretation, cached: true }
      return
    }

    // ② 只有客观描述 → 先展示描述，并提示可手动生成综合解读
    if (d.vlm_analysis) {
      analysis.value = {
        description: d.vlm_analysis,
        interpretation: '',
        citations: [],
        evidence_count: 0,
        cached: true,
        vlm_model: d.vlm_model,
      }
    }
  } catch (err) {
    if (requestId === detailRequest) detailError.value = (err as Error).message
  }
}

async function findSimilarImages() {
  if (!selected.value) return
  const targetId = selected.value.id
  const requestId = ++similarRequest
  similarLoadingId.value = targetId
  try {
    const result = await api.similarImages({ image_id: targetId, top_k: 8 })
    if (requestId !== similarRequest || selected.value?.id !== targetId) return
    similarImages.value = result.items.map(item => ({ ...item, is_public: true }))
    if (!result.items.length) ElMessage.info('暂无已生成特征的其他影像')
  } catch (err) {
    if (requestId === similarRequest) ElMessage.error((err as Error).message)
  } finally {
    if (requestId === similarRequest) similarLoadingId.value = null
  }
}

/* ----------------------------- 上传 ----------------------------- */
function pickFile() {
  fileInput.value?.click()
}

function onFileChange(e: Event) {
  const input = e.target as HTMLInputElement
  const file = input.files?.[0]
  if (!file) return

  // 前端先校验格式与大小，避免无谓的上传往返
  const allowed = ['image/jpeg', 'image/png', 'image/webp', 'image/bmp']
  if (!allowed.includes(file.type)) {
    ElMessage.error(`不支持的格式 ${file.type || '未知'}，请上传 JPG / PNG / WebP / BMP`)
    input.value = ''
    return
  }
  if (file.size > 8 * 1024 * 1024) {
    ElMessage.error(`图片过大（${(file.size / 1024 / 1024).toFixed(1)} MB），上限 8 MB`)
    input.value = ''
    return
  }

  uploadForm.value.file = file
  if (!uploadForm.value.title) {
    uploadForm.value.title = file.name.replace(/\.[^.]+$/, '')
  }
  // 本地预览
  if (previewUrl.value) URL.revokeObjectURL(previewUrl.value)
  previewUrl.value = URL.createObjectURL(file)
}

async function submitUpload() {
  if (!editingId.value && !uploadForm.value.file) {
    ElMessage.warning('请先选择影像文件')
    return
  }
  uploading.value = true
  try {
    if (editingId.value) {
      const targetId = editingId.value
      const { file: _file, ...patch } = uploadForm.value
      // 发布与取消公开在独立的审核操作中处理。
      const { is_public: _public, ...metadata } = patch
      const item = await api.updateImage(targetId, metadata)
      uploadVisible.value = false
      resetUploadForm()
      await Promise.all([loadImages(), loadStats()])
      if (selected.value?.id === targetId) await selectImage(await withPreview(item))
      ElMessage.success('影像资料已更新')
      return
    }
    const form = new FormData()
    form.append('file', uploadForm.value.file!)
    form.append('title', uploadForm.value.title)
    form.append('phase', uploadForm.value.phase)
    if (uploadForm.value.surgery_type) form.append('surgery_type', uploadForm.value.surgery_type)
    if (uploadForm.value.anatomy_site) form.append('anatomy_site', uploadForm.value.anatomy_site)
    if (uploadForm.value.description) form.append('description', uploadForm.value.description)
    if (uploadForm.value.source) form.append('source', uploadForm.value.source)
    form.append('is_public', String(uploadForm.value.is_public))

    const item = await api.uploadImage(form)
    ElMessage.success(`已上传：${item.filename}`)

    uploadVisible.value = false
    resetUploadForm()
    await Promise.all([loadImages(), loadStats()])
    await selectImage(await withPreview(item))
  } catch (err) {
    ElMessage.error((err as Error).message)
  } finally {
    uploading.value = false
  }
}

function resetUploadForm() {
  editingId.value = null
  uploadForm.value = {
    file: null, title: '', surgery_type: '', anatomy_site: '',
    phase: 'intraop', description: '', source: '', is_public: false,
  }
  if (previewUrl.value) {
    URL.revokeObjectURL(previewUrl.value)
    previewUrl.value = ''
  }
  if (fileInput.value) fileInput.value.value = ''
}

function openEdit() {
  if (!selected.value) return
  resetUploadForm()
  const item = selected.value
  editingId.value = item.id
  uploadForm.value = {
    file: null, title: item.title || '', surgery_type: item.surgery_type || '',
    anatomy_site: item.anatomy_site || '', phase: item.phase,
    description: item.description || '', source: item.source || '', is_public: item.is_public,
  }
  uploadVisible.value = true
}

/* ----------------------------- 解读 ----------------------------- */
async function runAnalysis(useCache = true) {
  if (!selected.value) return
  if (!visionReady.value) {
    ElMessage.warning('辅助解读服务暂不可用，请联系管理员检查系统状态')
    return
  }

  // 锁定本次请求对应的影像：后续回填与进度显示都以它为准，
  // 避免用户中途切换影像时结果错配。
  const targetId: string = selected.value.id
  analyzingId.value = targetId
  try {
    const result = await api.analyzeImage({
      image_id: targetId,
      top_k: topK.value,
      image_context: imageContext.value,
      use_cache: useCache,
    })
    // 只有当前仍停留在被解读的那张影像上才回填；否则丢弃结果
    // （用户已切走，那张的解读已由后端落库，切回时会从快照恢复）
    if (selected.value?.id === targetId) {
      analysis.value = result
    }
    // 列表与统计需要刷新：本张影像会新增「已解读」标记
    await Promise.all([loadStats(), loadImages()])
    if (result.cached && selected.value?.id === targetId) {
      ElMessage.info('使用了缓存的影像描述（未重复调用视觉模型）')
    }
  } catch (err) {
    if (selected.value?.id === targetId) {
      ElMessage.error((err as Error).message)
    }
  } finally {
    // 仅清除自己设置的状态；若已开始新一轮解读，不要误清
    if (analyzingId.value === targetId) {
      analyzingId.value = null
    }
  }
}

async function removeImage(item: any) {
  try {
    await ElMessageBox.confirm(
      `确认删除影像「${imageTitle(item)}」？此操作不可恢复。`,
      '删除确认',
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  try {
    await api.deleteImage(item.id)
    ElMessage.success('已删除')
    if (selected.value?.id === item.id) {
      detailRequest++
      similarRequest++
      selected.value = null
      analysis.value = null
      similarImages.value = []
    }
    const preview = previewUrls.get(item.id)
    if (preview) URL.revokeObjectURL(preview)
    previewUrls.delete(item.id)
    await Promise.all([loadImages(), loadStats()])
  } catch (err) {
    ElMessage.error((err as Error).message)
  }
}

async function togglePublish() {
  if (!selected.value || !canPublish.value) return
  const item = selected.value
  const nextPublic = !item.is_public
  try {
    await ElMessageBox.confirm(
      nextPublic ? '确认来源及公开脱敏条件已审核？发布后访客可查看原图和解读。' : '确认取消公开发布？此后仅维护人员可查看该影像。',
      nextPublic ? '发布影像' : '取消公开', { type: 'warning' },
    )
  } catch { return }
  publishing.value = true
  try {
    const updated = await api.updateImage(item.id, { is_public: nextPublic })
    const preview = previewUrls.get(item.id)
    if (preview) URL.revokeObjectURL(preview)
    previewUrls.delete(item.id)
    const ready = await withPreview(updated)
    if (selected.value?.id === item.id) await selectImage(ready)
    await Promise.all([loadImages(), loadStats()])
    ElMessage.success(nextPublic ? '影像已发布' : '影像已转为草稿')
  } catch (err) {
    ElMessage.error((err as Error).message)
  } finally { publishing.value = false }
}

function onCiteClick(e: MouseEvent) {
  const target = e.target as HTMLElement
  const cite = target.closest('.cite') as HTMLElement | null
  if (!cite) return
  const idx = Number(cite.dataset.cite)
  if (!idx) return
  const found = (analysis.value?.citations || []).find((c: any) => c.index === idx)
  if (found) {
    ElMessageBox.alert(
      h('div', { style: 'line-height:1.7' }, [
        h('b', String(found.doc_title || '未标题文献')),
        h('p', `${found.doc_journal || ''} ${found.doc_year || ''}${found.page_start ? ' · 第 ' + found.page_start + ' 页' : ''}`),
        h('hr'),
        h('div', { style: 'white-space:pre-wrap' }, String(found.content_snippet || '')),
      ]),
      `引用 ${idx} · 来源`,
      { confirmButtonText: '关闭' },
    )
  } else {
    ElMessage.warning(`引用 ${idx} 的原文片段不可用`)
  }
}

onMounted(async () => {
  await Promise.all([loadStats(), loadImages()])
})

watch(() => route.query.imageId, async value => {
  const imageId = typeof value === 'string' ? value : null
  if (!imageId) return
  const scope = previewScope
  const request = ++detailRequest
  try {
    const item = await api.imageDetail(imageId)
    if (scope !== previewScope || request !== detailRequest) return
    await selectImage(item)
  } catch (err) {
    if (scope === previewScope && request === detailRequest) ElMessage.error(`无法打开关联影像：${(err as Error).message}`)
  }
}, { immediate: true })

watch(() => auth.token, async () => {
  clearPrivatePreviews()
  detailRequest++
  similarRequest++
  selected.value = null
  analysis.value = null
  detail.value = null
  images.value = []
  stats.value = null
  similarImages.value = []
  uploadVisible.value = false
  resetUploadForm()
  page.value = 1
  await Promise.all([loadStats(), loadImages()])
})

onBeforeUnmount(() => {
  clearPrivatePreviews()
  listRequest++
  detailRequest++
  similarRequest++
  if (previewUrl.value) URL.revokeObjectURL(previewUrl.value)
})
</script>

<template>
  <div class="images-view">
    <el-image-viewer v-if="viewerOpen && selected?.url" :url-list="[selected.url]" :hide-on-click-modal="true" @close="viewerOpen = false" />
    <!-- 顶部工具栏 -->
    <div class="toolbar">
      <div class="toolbar-left">
        <el-select
          v-model="filterPhase"
          placeholder="全部分期"
          clearable
          size="small"
          style="width: 110px"
          @change="filterImages"
        >
          <el-option label="术前" value="preop" />
          <el-option label="术中" value="intraop" />
          <el-option label="术后" value="postop" />
          <el-option label="通用" value="general" />
        </el-select>
        <el-input
          v-model="filterKeyword"
          placeholder="搜索标题 / 部位 / 说明"
          size="small"
          clearable
          style="width: 210px"
          @keyup.enter="filterImages"
          @clear="filterImages"
        >
          <template #prefix><el-icon><Search /></el-icon></template>
        </el-input>
        <el-button size="small" @click="loadImages">
          <el-icon><Refresh /></el-icon>刷新
        </el-button>
      </div>
      <div class="toolbar-right">
        <el-tag v-if="stats" size="small" type="info" effect="plain">
          共 {{ stats.total }} 张 · 已解读 {{ stats.analyzed }}
        </el-tag>
        <el-button v-if="auth.can('image:upload')" type="primary" size="small" @click="resetUploadForm(); uploadVisible = true">
          <el-icon><Upload /></el-icon>上传影像
        </el-button>
      </div>
    </div>

    <!-- 未配置视觉模型时的申请指引 -->
    <el-alert
      v-if="stats && !visionReady"
      type="info"
      :closable="false"
      show-icon
      class="vision-hint"
    >
      <template #title>辅助解读暂不可用，仍可浏览影像资料</template>
      <div v-if="canManage" class="hint-body">管理员可在系统状态页检查视觉服务配置，完成配置后重启后端。<el-button link type="primary" @click="$router.push('/system')">查看系统状态</el-button></div>
    </el-alert>

    <div class="mobile-image-tabs"><el-button :type="!mobileDetail ? 'primary' : 'default'" @click="mobileDetail = false">影像列表</el-button><el-button :disabled="!selected" :type="mobileDetail ? 'primary' : 'default'" @click="mobileDetail = true">当前详情</el-button></div>
    <div class="images-body" :class="{ 'show-detail': mobileDetail }">
      <!-- 左侧：影像网格 -->
      <div class="gallery-pane">
        <el-empty v-if="!loading && images.length === 0" :description="listError ? '影像列表加载失败' : filterPhase || filterKeyword ? '没有符合筛选条件的影像' : canManage ? '暂无影像，可上传资料并完成审核' : '暂无已审核公开的教学影像'">
          <el-button v-if="listError" type="primary" @click="loadImages">重试加载</el-button>
          <el-button v-else-if="filterPhase || filterKeyword" @click="resetImageFilters">清除筛选</el-button>
        </el-empty>
        <div v-else v-loading="loading" class="grid">
          <div
            v-for="item in images"
            :key="item.id"
            class="card"
            :class="{ active: selected?.id === item.id }"
            tabindex="0" role="button" :aria-label="imageTitle(item)" :aria-pressed="selected?.id === item.id"
            @keydown.enter.prevent="selectImage(item)" @keydown.space.prevent="selectImage(item)"
            @click="selectImage(item)"
          >
            <div class="thumb">
              <img v-if="item.url" :src="item.url" :alt="imageTitle(item)" loading="lazy" />
              <span v-else>原图暂不可用</span>
              <span
                v-if="item.has_analysis"
                class="badge-analyzed"
                :title="analyzedHint(item)"
              >已解读</span>
            </div>
            <div class="card-meta">
              <div class="card-title" :title="imageTitle(item)">
                {{ imageTitle(item) }}
              </div>
              <div class="card-tags">
                <el-tag v-if="!item.is_public" size="small" type="warning">待发布</el-tag>
                <el-tag size="small" effect="plain">
                  {{ PHASE_LABELS[item.phase] || item.phase }}
                </el-tag>
                <el-tag v-if="item.surgery_type" size="small" type="success" effect="plain">
                  {{ item.surgery_type }}
                </el-tag>
              </div>
            </div>
            <el-button
              v-if="auth.can('image:delete')"
              class="card-del"
              link
              size="small"
              @click.stop="removeImage(item)"
            >
              <el-icon><Delete /></el-icon>
            </el-button>
          </div>
        </div>
        <el-pagination
          v-if="total > pageSize" v-model:current-page="page" :page-size="pageSize"
          :total="total" layout="prev, pager, next" small
          @current-change="loadImages"
        />
      </div>

      <!-- 右侧：详情与解读 -->
      <div class="detail-pane">
        <el-empty v-if="!selected" description="从左侧选择一张影像查看详情与辅助解读" />

        <template v-else>
          <div class="detail-head">
            <div class="detail-title">{{ imageTitle(selected) }}</div>
            <el-tag v-if="!selected.is_public" size="small" type="warning">草稿</el-tag>
            <el-button v-if="canManage && (!selected.is_public || canPublish)" size="small" @click="openEdit">编辑资料</el-button>
            <el-button v-if="canPublish" size="small" :loading="publishing" @click="togglePublish">
              {{ selected.is_public ? '取消公开' : '审核并发布' }}
            </el-button>
            <el-tag size="small" effect="plain">
              {{ PHASE_LABELS[selected.phase] || selected.phase }}
            </el-tag>
          </div>

          <el-alert v-if="detailError" title="影像详情加载失败" type="error" :closable="false"><el-button link type="primary" @click="selectImage(selected)">重试详情</el-button></el-alert>
          <el-button v-if="selected.url" class="zoom-button" @click="viewerOpen = true"><el-icon><ZoomIn /></el-icon>放大查看原图</el-button>
          <img v-if="selected.url" class="detail-img" :src="selected.url" :alt="selected.title" />
          <el-alert v-else title="原图暂不可用，请检查影像文件或重新登录后重试" type="warning" :closable="false" />

          <div class="detail-attrs">
            <span v-if="selected.surgery_type">术式：{{ selected.surgery_type }}</span>
            <span v-if="selected.anatomy_site">部位：{{ selected.anatomy_site }}</span>
            <span v-if="selected.width">
              尺寸：{{ selected.width }}×{{ selected.height }}
            </span>
            <span v-if="selected.file_size">
              {{ (selected.file_size / 1024).toFixed(0) }} KB
            </span>
            <span>来源：{{ selected.source || '尚未填写' }}</span>
          </div>

          <!-- 解读参数 -->
          <div class="analyze-bar">
            <span class="bar-label">证据条数</span>
            <el-input-number v-model="topK" :min="1" :max="10" size="small" style="width: 96px" />
            <el-button
              type="primary"
              size="small"
              :loading="analyzing"
              :disabled="!visionReady || !auth.can('image:analyze')"
              @click="runAnalysis(true)"
            >
              <el-icon v-if="!analyzing"><MagicStick /></el-icon>
              {{ analyzing ? '解读中…' : '辅助解读' }}
            </el-button>
            <el-button
              v-if="analysis && auth.can('image:analyze')"
              size="small"
              :disabled="analyzing || !visionReady"
              @click="runAnalysis(false)"
            >
              重新解读
            </el-button>
            <el-button size="small" :loading="similarLoading" @click="findSimilarImages">
              <el-icon><Connection /></el-icon>相似影像
            </el-button>
          </div>

          <el-input
            v-model="imageContext"
            type="textarea"
            :rows="2"
            size="small"
            placeholder="可选：补充术式、解剖部位等背景信息，有助于提高解读相关性"
            class="context-input"
          />

          <p class="similar-explainer">相似检索依据颜色分布，仅供浏览参考；百分比不代表诊断概率或解剖结构一致。</p>
          <div v-if="similarImages.length" class="section similar-images">
            <div class="section-head"><el-icon><Connection /></el-icon><span>颜色分布相似影像</span></div>
            <div class="similar-grid">
              <div v-for="image in similarImages" :key="image.id" class="similar-card" @click="selectImage(image)">
                <img :src="image.url" :alt="imageTitle(image)" />
                <div class="similar-caption"><span>{{ imageTitle(image) }}</span><el-tag size="small" type="success">{{ (image.similarity * 100).toFixed(1) }}%</el-tag></div>
              </div>
            </div>
          </div>

          <!-- 解读中 -->
          <div v-if="analyzing" class="analyzing">
            <el-icon class="spin"><Loading /></el-icon>
            <div class="analyzing-text">
              正在解读影像…<br />
              <span class="analyzing-note">
                流程：视觉模型客观描述 → 构造检索查询 → 检索文献证据 → 综合解读
              </span>
            </div>
          </div>

          <!-- 解读结果 -->
          <template v-if="analysis && !analyzing">
            <div v-if="analysis.cached" class="cache-note">已保存的历史解读，仅供参考，请对照原图核实。{{ selected.vlm_analyzed_at ? '缓存时间：' + new Date(selected.vlm_analyzed_at).toLocaleString() : '' }}</div>
            <el-alert
              v-if="!analysis.interpretation"
              type="info"
              :closable="false"
              show-icon
              class="cached-alert"
            >
              <template #title>尚未生成综合解读</template>
              <div class="cached-note">
                以下为已保存的视觉模型描述。点上方「辅助解读」按钮，
                系统会据此构造检索查询、检索文献证据并生成带引用的辅助解读（约 15~20 秒），
                结果会保存，下次打开该影像可直接查看。
              </div>
            </el-alert>

            <!-- 客观描述 -->
            <div class="section">
              <div class="section-head">
                <el-icon><View /></el-icon>
                <span>视觉模型描述</span>
                <el-tag v-if="analysis.vlm_model" size="small" effect="plain" class="model-tag">
                  {{ analysis.vlm_model }}
                </el-tag>
              </div>
              <div class="section-body md" v-html="descriptionHtml" />
            </div>

            <!-- 检索查询（过程透明） -->
            <div v-if="analysis.retrieval_query" class="section">
              <div class="section-head">
                <el-icon><Search /></el-icon>
                <span>由描述构造的检索查询</span>
                <el-tag size="small" type="info" effect="plain" class="model-tag">
                  证据 {{ analysis.evidence_count }} 条
                </el-tag>
              </div>
              <div class="query-box">{{ analysis.retrieval_query }}</div>
            </div>

            <!-- 综合解读 -->
            <div v-if="analysis.interpretation" class="section">
              <div class="section-head">
                <el-icon><Document /></el-icon>
                <span>结合文献证据的辅助解读</span>
                <el-tag
                  v-if="analysis.timings?.total_ms"
                  size="small"
                  effect="plain"
                  class="model-tag"
                >
                  {{ (analysis.timings.total_ms / 1000).toFixed(1) }}s
                </el-tag>
              </div>
              <div class="section-body md" v-html="analysisHtml" @click="onCiteClick" />

              <el-alert
                v-if="analysis.numeric_warning"
                type="warning"
                :closable="false"
                show-icon
                class="numeric-alert"
              >
                <template #title>数值需核实</template>
                {{ analysis.numeric_warning }}
              </el-alert>
            </div>

            <!-- 引用来源 -->
            <div v-if="analysis.citations?.length" class="section">
              <div class="section-head">
                <el-icon><Link /></el-icon>
                <span>引用来源（{{ analysis.citations.length }} 条）</span>
              </div>
              <div
                v-for="c in analysis.citations"
                :key="c.index"
                class="cite-item"
              >
                <span class="cite-idx">{{ c.index }}</span>
                <div class="cite-main">
                  <div class="cite-title">{{ c.doc_title }}</div>
                  <div class="cite-meta">
                    {{ c.doc_journal || '' }} {{ c.doc_year || '' }}
                    <template v-if="c.page_start">· 第 {{ c.page_start }} 页</template>
                  </div>
                </div>
              </div>
            </div>

            <div v-if="analysis.related_cases?.length" class="section">
              <div class="section-head"><el-icon><Tickets /></el-icon><span>相关典型病例</span></div>
              <div v-for="caseItem in analysis.related_cases" :key="caseItem.id" class="related-case">
                <span class="related-title">{{ caseItem.title }}</span>
                <el-tag v-if="caseItem.similarity != null" size="small" type="success">{{ (caseItem.similarity * 100).toFixed(1) }}%</el-tag>
                <span class="related-meta">{{ caseItem.diagnosis || '未标注诊断' }} · {{ caseItem.surgery || '未标注术式' }}</span>
              </div>
            </div>

            <!-- 合规声明 -->
            <div class="disclaimer">
              <el-icon><WarningFilled /></el-icon>
              <span>
                {{ analysis.disclaimer }}
                视觉模型对影像的描述可能存在误判，本解读仅供教学与科研参考，
                最终判断须由临床医师结合患者完整资料完成。
              </span>
            </div>
          </template>
        </template>
      </div>
    </div>

    <!-- 上传对话框 -->
    <el-dialog v-model="uploadVisible" :title="editingId ? '编辑影像资料' : '上传术中影像'" width="min(560px, 94vw)" @closed="resetUploadForm">
      <input
        ref="fileInput"
        type="file"
        accept="image/jpeg,image/png,image/webp,image/bmp"
        style="display: none"
        @change="onFileChange"
      />

      <div v-if="!editingId" class="upload-area" @click="pickFile">
        <img v-if="previewUrl" :src="previewUrl" class="upload-preview" alt="预览" />
        <div v-else class="upload-placeholder">
          <el-icon class="upload-icon"><Plus /></el-icon>
          <div>点击选择影像文件</div>
          <div class="upload-note">支持 JPG / PNG / WebP / BMP，单张不超过 8 MB</div>
        </div>
      </div>

      <el-form label-width="80px" size="small" class="upload-form">
        <el-form-item label="标题">
          <el-input v-model="uploadForm.title" placeholder="如：腹腔镜直肠癌根治术 - 吻合口血供" />
        </el-form-item>
        <el-form-item label="分期">
          <el-select v-model="uploadForm.phase" style="width: 100%">
            <el-option label="术前" value="preop" />
            <el-option label="术中" value="intraop" />
            <el-option label="术后" value="postop" />
            <el-option label="通用" value="general" />
          </el-select>
        </el-form-item>
        <el-form-item label="术式">
          <el-input v-model="uploadForm.surgery_type" placeholder="如：腹腔镜直肠癌根治术" />
        </el-form-item>
        <el-form-item label="解剖部位">
          <el-input v-model="uploadForm.anatomy_site" placeholder="如：直肠、乙状结肠" />
        </el-form-item>
        <el-form-item label="说明">
          <el-input v-model="uploadForm.description" type="textarea" :rows="2" />
        </el-form-item>
        <el-form-item label="来源">
          <el-input
            v-model="uploadForm.source"
            placeholder="影像来源（须为公开可引用资料）"
          />
        </el-form-item>
        <el-form-item v-if="canPublish && !editingId" label="公开">
          <el-switch v-model="uploadForm.is_public" />
          <span class="form-note">确认该影像为公开脱敏资料</span>
        </el-form-item>
        <el-form-item v-if="!canPublish" label="发布方式">
          影像保存为草稿，由管理员审核并发布。
        </el-form-item>
      </el-form>

      <template #footer>
        <el-button @click="uploadVisible = false; resetUploadForm()">取消</el-button>
        <el-button type="primary" :loading="uploading" @click="submitUpload">
          {{ editingId ? '保存' : '上传' }}
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.images-view {
  display: flex;
  flex-direction: column;
  height: 100%;
  overflow: hidden;
}

/* ---------- 工具栏 ---------- */
.toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 10px 16px;
  border-bottom: 1px solid var(--cr-border);
  flex-shrink: 0;
}

.toolbar-left,
.toolbar-right {
  display: flex;
  align-items: center;
  gap: 8px;
}

.vision-hint {
  margin: 10px 16px 0;
  flex-shrink: 0;
}

.hint-body {
  font-size: 12.5px;
  line-height: 1.75;
}

.hint-body p {
  margin: 2px 0;
}

.hint-body ol {
  margin: 4px 0;
  padding-left: 20px;
}

.hint-body code {
  padding: 1px 5px;
  background: var(--cr-bg-soft, #f2f5f7);
  border-radius: 3px;
  font-family: Consolas, monospace;
}

.hint-note {
  color: var(--cr-text-muted, #8a99a8);
}

/* ---------- 主体 ---------- */
.images-body {
  display: flex;
  flex: 1;
  min-height: 0;
  gap: 0;
}

.gallery-pane {
  flex: 1;
  min-width: 0;
  padding: 14px 16px;
  overflow-y: auto;
}

.grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(178px, 1fr));
  gap: 12px;
}

.card {
  position: relative;
  border: 1px solid var(--cr-border);
  border-radius: 8px;
  overflow: hidden;
  cursor: pointer;
  background: #fff;
  transition: box-shadow 0.15s, border-color 0.15s;
}

.card:hover {
  border-color: var(--cr-primary);
  box-shadow: 0 2px 10px rgba(43, 122, 120, 0.13);
}

.card.active {
  border-color: var(--cr-primary);
  box-shadow: 0 0 0 2px rgba(43, 122, 120, 0.18);
}

.thumb {
  position: relative;
  aspect-ratio: 4 / 3;
  background: #f5f7f8;
  overflow: hidden;
}

.thumb img {
  width: 100%;
  height: 100%;
  object-fit: cover;
  display: block;
}

.badge-analyzed {
  position: absolute;
  top: 6px;
  right: 6px;
  padding: 1px 6px;
  font-size: 10.5px;
  color: #fff;
  background: rgba(43, 122, 120, 0.9);
  border-radius: 3px;
}

.card-meta {
  padding: 7px 9px 9px;
}

.card-title {
  font-size: 12.5px;
  font-weight: 600;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.card-tags {
  display: flex;
  gap: 4px;
  margin-top: 5px;
  flex-wrap: wrap;
}

.card-del {
  position: absolute;
  bottom: 6px;
  right: 6px;
  color: #b0bcc6;
}

.card-del:hover {
  color: #e05252;
}

/* ---------- 详情面板 ---------- */
.detail-pane {
  width: 46%;
  max-width: 620px;
  min-width: 320px;
  padding: 14px 16px;
  border-left: 1px solid var(--cr-border);
  overflow-y: auto;
}

.detail-head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 10px;
}

.detail-title {
  flex: 1;
  font-size: 15px;
  font-weight: 600;
}

.detail-img {
  width: 100%;
  max-height: 300px;
  object-fit: contain;
  background: #f5f7f8;
  border: 1px solid var(--cr-border);
  border-radius: 6px;
}

.detail-attrs {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  margin: 9px 0;
  font-size: 12px;
  color: var(--cr-text-muted, #6b7c8c);
}

.analyze-bar {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
}

.bar-label {
  font-size: 12.5px;
  color: var(--cr-text-muted, #6b7c8c);
}

.context-input {
  margin-bottom: 10px;
}

.similar-images { margin-top: 12px; }
.similar-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(120px, 1fr)); gap: 8px; }
.similar-card { cursor: pointer; overflow: hidden; border: 1px solid var(--cr-border); border-radius: 6px; background: var(--cr-surface); }
.similar-card img { display: block; width: 100%; height: 84px; object-fit: cover; }
.similar-caption { display: flex; align-items: center; justify-content: space-between; gap: 4px; padding: 5px 6px; font-size: 11px; }
.similar-caption span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.related-case { display: flex; align-items: center; flex-wrap: wrap; gap: 7px; padding: 8px 0; border-bottom: 1px solid var(--cr-border); font-size: 13px; }
.related-case:last-child { border-bottom: none; }
.related-title { font-weight: 600; color: var(--cr-primary-dark); }
.related-meta { flex-basis: 100%; color: var(--cr-text-secondary); font-size: 12px; }

.analyzing {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 14px;
  margin-top: 8px;
  background: var(--cr-bg-soft, #f7fafb);
  border-radius: 6px;
}

.analyzing-text {
  font-size: 13px;
}

.analyzing-note {
  font-size: 11.5px;
  color: var(--cr-text-muted, #8a99a8);
}

.spin {
  animation: cr-spin 1s linear infinite;
}

@keyframes cr-spin {
  from { transform: rotate(0deg); }
  to { transform: rotate(360deg); }
}

.cached-alert {
  margin-top: 10px;
}

.cached-note {
  font-size: 12.5px;
}

/* ---------- 结果分区 ---------- */
.section {
  margin-top: 14px;
}

.section-head {
  display: flex;
  align-items: center;
  gap: 6px;
  padding-bottom: 6px;
  margin-bottom: 8px;
  font-size: 13px;
  font-weight: 600;
  color: var(--cr-primary);
  border-bottom: 1px solid var(--cr-border);
}

.model-tag {
  margin-left: auto;
  font-weight: 400;
}

.section-body {
  font-size: 13px;
  line-height: 1.75;
}

.query-box {
  padding: 8px 10px;
  font-family: 'Cascadia Code', Consolas, monospace;
  font-size: 12px;
  color: #4a5a68;
  background: var(--cr-bg-soft, #f7fafb);
  border-left: 3px solid var(--cr-primary);
  border-radius: 4px;
  word-break: break-word;
}

.numeric-alert {
  margin-top: 10px;
}

/* ---------- 引用 ---------- */
.cite-item {
  display: flex;
  gap: 9px;
  padding: 8px 0;
  border-bottom: 1px dashed var(--cr-border);
}

.cite-idx {
  flex-shrink: 0;
  width: 20px;
  height: 20px;
  font-size: 11.5px;
  line-height: 20px;
  color: #fff;
  text-align: center;
  background: var(--cr-primary);
  border-radius: 50%;
}

.cite-main {
  min-width: 0;
}

.cite-title {
  font-size: 12.5px;
  font-weight: 600;
  line-height: 1.5;
}

.cite-meta {
  margin-top: 2px;
  font-size: 11.5px;
  color: var(--cr-text-muted, #8a99a8);
}

/* ---------- 免责声明 ---------- */
.disclaimer {
  display: flex;
  gap: 7px;
  align-items: flex-start;
  padding: 10px 12px;
  margin-top: 14px;
  font-size: 11.5px;
  line-height: 1.7;
  color: #8a6d3b;
  background: #fdf8ec;
  border: 1px solid #f5e3c0;
  border-radius: 6px;
}

/* ---------- 上传对话框 ---------- */
.upload-area {
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 150px;
  margin-bottom: 14px;
  cursor: pointer;
  background: var(--cr-bg-soft, #f7fafb);
  border: 1.5px dashed var(--cr-border);
  border-radius: 8px;
  transition: border-color 0.15s;
}

.upload-area:hover {
  border-color: var(--cr-primary);
}

.upload-preview {
  max-width: 100%;
  max-height: 210px;
  object-fit: contain;
}

.upload-placeholder {
  text-align: center;
  color: var(--cr-text-muted, #8a99a8);
  font-size: 13px;
}

.upload-icon {
  font-size: 26px;
  color: var(--cr-primary);
}

.upload-note {
  margin-top: 4px;
  font-size: 11.5px;
}

.form-note {
  margin-left: 9px;
  font-size: 11.5px;
  color: var(--cr-text-muted, #8a99a8);
}

/* Markdown 渲染样式（与问答页保持一致） */
.md :deep(h1),
.md :deep(h2),
.md :deep(h3) {
  margin: 12px 0 6px;
  font-size: 13.5px;
  font-weight: 700;
  color: var(--cr-primary-dark, #1f5c5a);
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
.toolbar, .toolbar-left, .toolbar-right, .detail-head, .analyze-bar { flex-wrap: wrap; }
.detail-title { overflow-wrap: anywhere; min-width: 160px; font-size: 18px; }
.detail-attrs { font-size: 13px; overflow-wrap: anywhere; }
.card-title { font-size: 14px; }
.zoom-button { margin-bottom: 8px; }
.similar-explainer { font-size: 13px; color: var(--cr-text-secondary); background: #edf5f4; border-radius: 6px; padding: 10px; }
.mobile-image-tabs { display: none; }
.gallery-pane .grid { grid-template-columns: repeat(auto-fill, minmax(178px, 240px)); }
@media (max-width: 900px) {
 .mobile-image-tabs { display: flex; padding: 8px 12px; background: white; }
 .images-body .detail-pane { display: none; }
 .images-body.show-detail .gallery-pane { display: none; }
 .images-body.show-detail .detail-pane { display: block; width: 100%; max-width: none; min-width: 0; border-left: 0; }
 .toolbar { padding: 10px 12px; gap: 8px; }
 .toolbar-left { flex: 1; }
 .toolbar-left :deep(.el-input) { max-width: 100%; }
 .gallery-pane .grid { grid-template-columns: repeat(auto-fill, minmax(145px, 1fr)); }
}
@media (max-height: 650px) { .vision-hint { display: none; } }
.cache-note { padding: 10px; margin: 10px 0; border: 1px solid #ead5ad; border-radius: 6px; background: #fff9ed; font-size: 13px; color: #80541e; }
</style>
