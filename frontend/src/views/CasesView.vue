<script setup lang="ts">
/** 公开脱敏的典型病例库，支持检索、相似匹配和授权维护。 */
import { computed, onMounted, onBeforeUnmount, reactive, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api } from '@/api/client'
import type { CaseItem } from '@/api/types'
import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()
const router = useRouter()
const loading = ref(false)
const listError = ref('')
const saving = ref(false)
const items = ref<CaseItem[]>([])
const total = ref(0)
const page = ref(1)
const pageSize = 20
const keyword = ref('')
const phase = ref('')
const surgery = ref('')
const similarQuery = ref('')
const similarMode = ref(false)
const selected = ref<CaseItem | null>(null)
const detailVisible = ref(false)
const formVisible = ref(false)
const editingId = ref('')
const imageOptions = ref<any[]>([])
const reviews = ref<any[]>([])
const canEdit = computed(() => auth.can('case:update'))
const canDelete = computed(() => auth.can('case:delete'))
const canPublish = computed(() => auth.can('case:publish'))
let listRequest = 0
let detailRequest = 0
let ownerRequest = 0

watch(() => auth.user?.id, () => {
  ownerRequest++
  listRequest++
  detailRequest++
  items.value = []
  total.value = 0
  selected.value = null
  reviews.value = []
  detailVisible.value = false
  formVisible.value = false
  Object.assign(form, emptyForm())
  page.value = 1
  void loadCases()
})
onBeforeUnmount(() => { ownerRequest++; listRequest++; detailRequest++ })

const emptyForm = () => ({
  case_code: '', title: '', age: null as number | null, sex: '', diagnosis: '',
  surgery: '', phase: 'general', history: '', findings: '', operative_note: '',
  prognosis: '', complications_text: '', tags_text: '', image_ids: [] as string[], is_public: false,
  source_title: '', source_url: '', source_doi: '', source_license: '', source_note: '', review_note: '',
})
const form = reactive(emptyForm())
const phaseLabel: Record<string, string> = {
  preop: '术前', intraop: '术中', postop: '术后', general: '通用',
}

async function loadCases() {
  const request = ++listRequest
  loading.value = true
  listError.value = ''
  similarMode.value = false
  try {
    const listMethod = canEdit.value ? api.manageCases : api.cases
    const result = await listMethod({
      keyword: keyword.value || undefined,
      phase: phase.value || undefined,
      surgery: surgery.value || undefined,
      limit: pageSize,
      offset: (page.value - 1) * pageSize,
    })
    if (request !== listRequest) return
    items.value = result.items
    total.value = result.total
  } catch (error) {
    if (request === listRequest) { listError.value = (error as Error).message; items.value = []; total.value = 0 }
  } finally {
    if (request === listRequest) loading.value = false
  }
}

async function searchSimilar() {
  if (!similarQuery.value.trim()) {
    ElMessage.warning('请输入病例描述或临床关键词')
    return
  }
  loading.value = true
  listError.value = ''
  similarMode.value = true
  const request = ++listRequest
  try {
    const result = await api.searchCases({
      query: similarQuery.value.trim(), top_k: 30,
      phase: phase.value || undefined, surgery: surgery.value || undefined,
    })
    if (request !== listRequest) return
    items.value = result.items
    total.value = result.total
    page.value = 1
    similarMode.value = true
  } catch (error) {
    if (request === listRequest) { listError.value = (error as Error).message; items.value = []; total.value = 0 }
  } finally {
    if (request === listRequest) loading.value = false
  }
}

async function openDetail(row: CaseItem) {
  const request = ++detailRequest
  selected.value = row
  detailVisible.value = true
  reviews.value = []
  if (canEdit.value) {
    try {
      const result = await api.caseReviews(row.id)
      if (request === detailRequest) reviews.value = result
    }
    catch (error) { if (request === detailRequest) ElMessage.error((error as Error).message) }
  }
  if (row.image_ids.length) await loadImageOptions()
}

function openLinkedImage(imageId: string) {
  detailVisible.value = false
  void router.push({ path: '/images', query: { imageId } })
}

async function loadImageOptions() {
  try {
    const result = await api.images({ limit: 200 })
    imageOptions.value = result.items
  } catch (error) {
    ElMessage.error(`加载公开影像失败：${(error as Error).message}`)
  }
}

async function openCreate() {
  const scope = ownerRequest
  await loadImageOptions()
  if (scope !== ownerRequest) return
  editingId.value = ''
  Object.assign(form, emptyForm())
  formVisible.value = true
}

async function openEdit(row: CaseItem) {
  const scope = ownerRequest
  await loadImageOptions()
  if (scope !== ownerRequest) return
  editingId.value = row.id
  Object.assign(form, {
    ...emptyForm(), ...row,
    complications_text: (row.complications || []).map(String).join('\n'),
    tags_text: (row.tags || []).join(', '),
    image_ids: [...(row.image_ids || [])],
    source_title: row.source_title || '', source_url: row.source_url || '',
    source_doi: row.source_doi || '', source_license: row.source_license || '',
    source_note: row.source_note || '', review_note: row.review_note || '',
  })
  formVisible.value = true
}

function formPayload() {
  const payload: Record<string, unknown> = {
    case_code: form.case_code.trim(), title: form.title.trim(), age: form.age,
    sex: form.sex || null, diagnosis: form.diagnosis || null, surgery: form.surgery || null,
    phase: form.phase, history: form.history || null, findings: form.findings || null,
    operative_note: form.operative_note || null, prognosis: form.prognosis || null,
    complications: form.complications_text.split(/[\n,，;]/).map(x => x.trim()).filter(Boolean),
    tags: form.tags_text.split(/[,，;]/).map(x => x.trim()).filter(Boolean),
    image_ids: [...form.image_ids],
    source_title: form.source_title || null, source_url: form.source_url || null,
    source_doi: form.source_doi || null, source_license: form.source_license || null,
    source_note: form.source_note || null,
  }
  if (canPublish.value) payload.review_note = form.review_note || null
  if (canPublish.value) payload.is_public = form.is_public
  return payload
}

async function saveCase() {
  if (!form.case_code.trim() || !form.title.trim()) {
    ElMessage.warning('病例编号和标题为必填项')
    return
  }
  saving.value = true
  try {
    if (editingId.value) await api.updateCase(editingId.value, formPayload())
    else await api.createCase(formPayload())
    ElMessage.success(editingId.value ? '病例已更新' : '病例已保存')
    formVisible.value = false
    await loadCases()
  } catch (error) {
    ElMessage.error((error as Error).message)
  } finally {
    saving.value = false
  }
}

async function removeCase(row: CaseItem) {
  try {
    await ElMessageBox.confirm(`确定删除病例“${row.title}”（${row.case_code}）吗？`, '删除病例', {
      type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消',
    })
    await api.deleteCase(row.id)
    ElMessage.success('病例已删除')
    await loadCases()
  } catch (error) {
    if (error !== 'cancel') ElMessage.error((error as Error).message)
  }
}

async function togglePublish(row: CaseItem) {
  try {
    const { value } = await ElMessageBox.prompt(
      row.is_public ? '填写取消发布的原因' : '核对原文、开放许可、脱敏与整理准确性后填写审核意见',
      row.is_public ? '取消发布' : '审核并发布',
      { inputValue: '', inputValidator: value => !!value?.trim() || '请填写审核意见' },
    )
    await api.updateCase(row.id, { is_public: !row.is_public, review_note: value.trim() })
    ElMessage.success(row.is_public ? '病例已设为未公开' : '病例已发布')
    await loadCases()
  } catch (error) {
    if (error !== 'cancel' && error !== 'close') ElMessage.error((error as Error).message)
  }
}

function clearFilters() {
  similarQuery.value = ''
  keyword.value = ''
  surgery.value = ''
  phase.value = ''
  page.value = 1
  void loadCases()
}

function sourceMissing(item: CaseItem): string[] {
  return [!item.source_title && '文献题名', !(item.source_url || item.source_doi) && '原文地址', !item.source_license && '许可', !item.source_note && '整理依据'].filter(Boolean) as string[]
}
onMounted(loadCases)
</script>

<template>
  <div class="cases-page">
    <div class="page-heading">
      <div>
        <h2>典型病例库</h2>
        <p>{{ canEdit ? '管理草稿与公开病例。补全来源、核对资料并审核后，病例才会向访客展示。' : '查阅已审核发布的公开脱敏病例，按诊断、术式或病例描述检索。' }}</p>
      </div>
      <el-button v-if="canEdit" type="primary" @click="openCreate">新增病例</el-button>
    </div>

    <el-card shadow="never" class="filters">
      <div class="filter-row">
        <el-input v-model="keyword" placeholder="病例编号、诊断、术式或病史" clearable @keyup.enter="page = 1; loadCases()" />
        <el-select v-model="phase" placeholder="所有分期" clearable>
          <el-option label="术前" value="preop" /><el-option label="术中" value="intraop" />
          <el-option label="术后" value="postop" /><el-option label="通用" value="general" />
        </el-select>
        <el-input v-model="surgery" placeholder="术式" clearable @keyup.enter="page = 1; loadCases()" />
        <el-button type="primary" @click="page = 1; loadCases()">筛选</el-button>
        <el-button @click="clearFilters">重置</el-button>
      </div>
      <el-divider />
      <div class="filter-row similar-row">
        <el-input v-model="similarQuery" placeholder="输入病例描述，进行文本语义相似匹配" clearable @keyup.enter="searchSimilar" />
        <el-button type="primary" plain :loading="loading" @click="searchSimilar">相似病例检索</el-button>
        <el-button v-if="similarMode" @click="loadCases">返回病例列表</el-button>
      </div>
    </el-card>

    <el-card shadow="never" class="table-card" v-loading="loading">
      <div class="result-count">{{ similarMode ? '相似病例' : canEdit ? '管理范围病例' : '公开病例' }}：{{ listError ? '暂不可用' : total + ' 条' }}</div>
      <el-alert v-if="canEdit && items.some(item => !item.is_public)" title="草稿待补全来源与人工审核，尚未向访客公开" type="info" :closable="false" class="draft-notice" />
      <el-table :data="items" stripe>
        <template #empty><el-empty :description="listError ? '病例加载失败' : similarMode ? '没有匹配的公开病例' : keyword || phase || surgery ? '没有符合筛选条件的病例' : canEdit ? '暂无病例，可新增草稿并补全来源' : '暂无已审核公开的病例，资料经审核发布后会显示在这里'">
          <el-button v-if="listError" type="primary" @click="similarMode ? searchSimilar() : loadCases()">重试加载</el-button>
          <el-button v-else-if="keyword || phase || surgery || similarMode" @click="clearFilters">清除筛选</el-button>
        </el-empty></template>
        <el-table-column prop="case_code" label="病例编号" width="140" />
        <el-table-column label="病例标题" min-width="220">
          <template #default="scope"><el-button link type="primary" @click="openDetail(scope.row)">{{ scope.row.title }}</el-button></template>
        </el-table-column>
        <el-table-column prop="diagnosis" label="诊断" min-width="180" show-overflow-tooltip />
        <el-table-column prop="surgery" label="术式" min-width="160" show-overflow-tooltip />
        <el-table-column label="分期" width="90"><template #default="scope">{{ phaseLabel[scope.row.phase] || scope.row.phase }}</template></el-table-column>
        <el-table-column prop="prognosis" label="结局 / 随访" min-width="180" show-overflow-tooltip><template #default="scope">{{ scope.row.prognosis || '原文未报告 / 待补全' }}</template></el-table-column>
        <el-table-column v-if="canEdit" label="来源资料" width="160"><template #default="scope"><el-tag size="small" :type="sourceMissing(scope.row).length ? 'warning' : 'success'">{{ sourceMissing(scope.row).length ? '待补全' : '资料已填写' }}</el-tag><div class="source-note">{{ sourceMissing(scope.row).join('、') || '仍需人工核对原文' }}</div></template></el-table-column>
        <el-table-column v-if="canEdit" label="状态" width="95"><template #default="scope"><el-tag size="small" :type="scope.row.is_public ? 'success' : 'info'">{{ scope.row.is_public ? '已发布' : '草稿' }}</el-tag></template></el-table-column>
        <el-table-column v-if="similarMode" label="相似度" width="110">
          <template #default="scope">{{ scope.row.similarity == null ? '—' : `${(scope.row.similarity * 100).toFixed(1)}%` }}</template>
        </el-table-column>
        <el-table-column label="标签" min-width="140"><template #default="scope"><el-tag v-for="tag in scope.row.tags.slice(0, 3)" :key="tag" size="small" class="tag">{{ tag }}</el-tag></template></el-table-column>
        <el-table-column v-if="canEdit || canDelete || canPublish" label="操作" width="230" fixed="right">
          <template #default="scope">
            <el-button v-if="canEdit" link type="primary" @click="openEdit(scope.row)">编辑</el-button>
            <el-button v-if="canPublish" link :type="scope.row.is_public ? 'warning' : 'success'" @click="togglePublish(scope.row)">{{ scope.row.is_public ? '取消发布' : '发布' }}</el-button>
            <el-button v-if="canDelete" link type="danger" @click="removeCase(scope.row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
      <div v-if="!similarMode" class="pagination"><el-pagination v-model:current-page="page" :page-size="pageSize" :total="total" layout="total, prev, pager, next" @current-change="loadCases" /></div>
    </el-card>

    <el-dialog v-model="detailVisible" :title="selected?.title || '病例详情'" width="min(760px, 92vw)">
      <template v-if="selected">
        <div class="detail-grid">
          <div><b>病例编号</b>{{ selected.case_code }}</div><div><b>分期</b>{{ phaseLabel[selected.phase] || selected.phase }}</div>
          <div><b>年龄 / 性别</b>{{ selected.age ?? '—' }} / {{ selected.sex || '—' }}</div><div><b>诊断</b>{{ selected.diagnosis || '—' }}</div>
          <div><b>术式</b>{{ selected.surgery || '—' }}</div><div><b>并发症</b>{{ (selected.complications || []).join('、') || '—' }}</div>
        </div>
        <el-descriptions :column="1" border class="case-sections">
          <el-descriptions-item label="病史">{{ selected.history || '—' }}</el-descriptions-item>
          <el-descriptions-item label="检查所见">{{ selected.findings || '—' }}</el-descriptions-item>
          <el-descriptions-item label="手术记录">{{ selected.operative_note || '—' }}</el-descriptions-item>
          <el-descriptions-item label="预后与随访">{{ selected.prognosis || '—' }}</el-descriptions-item>
          <el-descriptions-item label="标签">{{ selected.tags.join('、') || '—' }}</el-descriptions-item>
          <el-descriptions-item label="来源文献">{{ selected.source_title || '未填写' }}</el-descriptions-item>
          <el-descriptions-item label="原文链接"><el-link v-if="selected.source_url && /^https?:\/\//i.test(selected.source_url)" :href="selected.source_url" target="_blank" rel="noopener noreferrer" type="primary">查看原文</el-link><span v-else>未填写</span></el-descriptions-item>
          <el-descriptions-item label="DOI / 许可">{{ selected.source_doi || '未报告' }} / {{ selected.source_license || '未填写' }}</el-descriptions-item>
          <el-descriptions-item label="整理依据">{{ selected.source_note || '未填写' }}</el-descriptions-item>
          <el-descriptions-item label="审核记录">{{ selected.reviewed_by || '尚未审核' }} {{ selected.reviewed_at || '' }} {{ selected.review_note || '' }}</el-descriptions-item>
          <el-descriptions-item label="关联影像">
            <template v-if="selected.image_ids.length">
              <el-button v-for="imageId in selected.image_ids" :key="imageId" link type="primary" @click="openLinkedImage(imageId)">
                {{ imageOptions.find(image => image.id === imageId)?.title || imageOptions.find(image => image.id === imageId)?.filename || '查看影像' }}
              </el-button>
            </template>
            <span v-else>无</span>
          </el-descriptions-item>
        </el-descriptions>
        <el-timeline v-if="reviews.length"><el-timeline-item v-for="(review, i) in reviews" :key="i" :timestamp="review.created_at">{{ review.username }} · {{ review.decision === 'case_publish' ? '审核发布' : '取消公开' }} · {{ review.detail?.review_note }}</el-timeline-item></el-timeline>
        <el-alert type="warning" :closable="false" title="病例仅用于教学与科研参考；不得录入可识别患者身份的信息。" />
      </template>
    </el-dialog>

    <el-dialog v-model="formVisible" :title="editingId ? '编辑病例' : '新增病例'" width="min(840px, 94vw)" top="5vh">
      <el-form label-width="100px" class="case-form">
        <div class="form-grid">
          <el-form-item label="病例编号" required><el-input v-model="form.case_code" /></el-form-item>
          <el-form-item label="病例标题" required><el-input v-model="form.title" /></el-form-item>
          <el-form-item label="年龄"><el-input-number v-model="form.age" :min="0" :max="130" /></el-form-item>
          <el-form-item label="性别"><el-select v-model="form.sex" clearable><el-option label="男" value="M" /><el-option label="女" value="F" /></el-select></el-form-item>
          <el-form-item label="分期"><el-select v-model="form.phase"><el-option label="术前" value="preop" /><el-option label="术中" value="intraop" /><el-option label="术后" value="postop" /><el-option label="通用" value="general" /></el-select></el-form-item>
          <el-form-item label="术式"><el-input v-model="form.surgery" /></el-form-item>
        </div>
        <el-form-item label="诊断"><el-input v-model="form.diagnosis" /></el-form-item>
        <el-form-item label="病史"><el-input v-model="form.history" type="textarea" :rows="3" /></el-form-item>
        <el-form-item label="检查所见"><el-input v-model="form.findings" type="textarea" :rows="3" /></el-form-item>
        <el-form-item label="手术记录"><el-input v-model="form.operative_note" type="textarea" :rows="3" /></el-form-item>
        <el-form-item label="预后随访"><el-input v-model="form.prognosis" type="textarea" :rows="2" /></el-form-item>
        <el-form-item label="并发症"><el-input v-model="form.complications_text" placeholder="每行或逗号分隔" /></el-form-item>
        <el-form-item label="标签"><el-input v-model="form.tags_text" placeholder="逗号分隔" /></el-form-item>
        <el-divider>来源与审核（发布前必须核对）</el-divider>
        <el-form-item label="来源文献"><el-input v-model="form.source_title" placeholder="原始病例报告完整题名" /></el-form-item>
        <el-form-item label="原文链接"><el-input v-model="form.source_url" placeholder="https://..." /></el-form-item>
        <el-form-item label="DOI"><el-input v-model="form.source_doi" /></el-form-item>
        <el-form-item label="开放许可"><el-input v-model="form.source_license" placeholder="例如 CC BY 4.0；以原文声明为准" /></el-form-item>
        <el-form-item label="整理依据"><el-input v-model="form.source_note" type="textarea" :rows="3" placeholder="原文病例章节、整理方式；缺失信息标记原文未报告" /></el-form-item>
        <el-form-item v-if="canPublish" label="审核意见"><el-input v-model="form.review_note" type="textarea" :rows="2" placeholder="核对原文、许可、脱敏及摘录准确性后填写" /></el-form-item>
        <el-form-item label="关联影像">
          <el-select v-model="form.image_ids" multiple filterable clearable style="width:100%" placeholder="选择已发布影像，支持搜索标题或术式">
            <el-option v-for="image in imageOptions" :key="image.id" :value="image.id" :label="`${image.title || image.filename} · ${phaseLabel[image.phase] || image.phase} · ${image.surgery_type || '未标注术式'}`" />
          </el-select>
          <div class="form-hint">病例发布时，关联影像也必须已审核并公开。</div>
        </el-form-item>
        <el-form-item v-if="canPublish" label="公开发布"><el-switch v-model="form.is_public" active-text="已审核脱敏" inactive-text="仅草稿" /></el-form-item>
        <el-alert v-else type="info" :closable="false" title="新建病例默认为草稿，需管理员审核脱敏后发布。" />
      </el-form>
      <template #footer><el-button @click="formVisible = false">取消</el-button><el-button type="primary" :loading="saving" @click="saveCase">保存</el-button></template>
    </el-dialog>
  </div>
</template>

<style scoped>
.cases-page { height: 100%; overflow: auto; padding: 20px; box-sizing: border-box; }
.page-heading { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; margin-bottom: 16px; }
.page-heading h2 { margin: 0 0 6px; color: var(--cr-primary-dark); font-size: 20px; }
.page-heading p { margin: 0; color: var(--cr-text-secondary); font-size: 13px; }
.filters, .table-card { border: 1px solid var(--cr-border); }
.filter-row { display: flex; flex-wrap: wrap; gap: 10px; }
.filter-row :deep(.el-input) { flex: 1 1 220px; }
.filter-row :deep(.el-select) { width: 150px; }
.similar-row :deep(.el-input) { flex: 1 1 400px; }
.filters :deep(.el-card__body) { padding-bottom: 12px; }
.filters :deep(.el-divider) { margin: 14px 0; }
.result-count { margin-bottom: 12px; color: var(--cr-text-secondary); font-size: 13px; }
.tag { margin: 2px 4px 2px 0; }
.pagination { display: flex; justify-content: flex-end; padding-top: 16px; }
.detail-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px 20px; margin-bottom: 18px; }
.detail-grid div { display: flex; gap: 12px; color: var(--cr-text-primary); }
.detail-grid b { min-width: 74px; color: var(--cr-text-secondary); font-weight: 500; }
.case-sections { margin-bottom: 16px; white-space: pre-wrap; }
.form-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); column-gap: 16px; }
.case-form :deep(.el-input-number), .case-form :deep(.el-select) { width: 100%; }
.form-hint { margin: -12px 0 8px; color: var(--cr-text-secondary); font-size: 12px; }
@media (max-width: 760px) { .cases-page { padding: 12px; } .form-grid, .detail-grid { grid-template-columns: 1fr; } .page-heading { align-items: center; } }
.draft-notice { margin: 8px 0 14px; }
.source-note { color: var(--cr-text-secondary); font-size: 12px; margin-top: 4px; }
.table-card :deep(.el-table__empty-text) { width: 100%; line-height: 1.6; }
@media (max-width: 600px) { .page-heading { flex-wrap: wrap; gap: 8px; } .filter-row { flex-wrap: wrap; } .filter-row :deep(.el-input), .filter-row :deep(.el-select) { width: 100%; min-width: 0; flex: 1 1 100%; } .table-card :deep(.el-card__body) { padding: 12px; } }
</style>
