<script setup lang="ts">
/**
 * 系统管理（阶段九）。
 *
 * 对应报审表「（四）系统管理与用户模块」四个子模块：
 *   1. 用户权限管理 —— 账号增删改、角色配置、启停、解锁
 *   2. 问答质量监控 —— 问答日志、质量指标、人工反馈与处理
 *   3. 知识库维护   —— 内容统计与质量评估入口
 *   4. 系统配置     —— 检索策略与模型参数在线调整
 *
 * 设计说明：为什么配置项用「重启才生效 vs 立即生效」区分展示
 * --------------------------------------------------------
 * 只有部分参数能在运行时不重启就生效：
 *   - retrieval.* 权重类：每次检索时从配置读取 → 立即生效
 *   - llm.temperature 等：每次调用时传入 → 立即生效
 *   - 但模型路径、max_tokens 等与已加载模型绑定的参数需重启
 * 界面必须如实区分，否则管理员会以为调整已生效而实际没有，
 * 这类「看起来生效但没生效」的问题极难排查。
 */
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'

import { api } from '@/api/client'
import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()

/* ----------------------------- 状态 ----------------------------- */
const activeTab = ref('overview')
const loading = ref(false)

const overview = ref<any>(null)
const users = ref<any[]>([])
const userTotal = ref(0)
const qaLogs = ref<any[]>([])
const qaTotal = ref(0)
const qaStats = ref<any>(null)
const feedbacks = ref<any[]>([])
const audits = ref<any[]>([])
const auditTotal = ref(0)
const configs = ref<any[]>([])
const configCats = ref<Record<string, number>>({})
const roles = ref<any[]>([])

/** 登录表单 */
const loginForm = reactive({ username: '', password: '' })
const loginLoading = ref(false)

/** 用户创建表单 */
const userDialog = ref(false)
const userForm = reactive({
  username: '', password: '', role: 'physician',
  display_name: '', department: '', email: '',
})

/** 反馈处理 */
const feedbackDialog = ref(false)
const feedbackForm = reactive({ id: '', note: '' })

const PHASE_LABELS: Record<string, string> = {
  preop: '术前', intraop: '术中', postop: '术后', general: '通用',
}

const isAdmin = computed(() => auth.isAdmin)

/* ----------------------------- 登录 ----------------------------- */
async function doLogin() {
  if (!loginForm.username || !loginForm.password) {
    ElMessage.warning('请输入用户名与口令')
    return
  }
  loginLoading.value = true
  try {
    await auth.login(loginForm.username, loginForm.password)
    ElMessage.success('登录成功')
    loginForm.password = ''
    await loadAll()
  } catch (err) {
    ElMessage.error((err as Error).message)
  } finally {
    loginLoading.value = false
  }
}

function doLogout() {
  auth.logout()
  ElMessage.success('已退出登录')
  overview.value = null
}

/* ----------------------------- 数据加载 ----------------------------- */
async function loadOverview() {
  if (!auth.isLoggedIn) return
  try {
    overview.value = await api.overview()
  } catch { /* 令牌失效时拦截器已处理 */ }
}

async function loadUsers() {
  if (!auth.can('user:view')) return
  try {
    const d = await api.users({ limit: 100 })
    users.value = d.items
    userTotal.value = d.total
  } catch { /* ignore */ }
}

async function loadQaLogs() {
  try {
    const d = await api.qaLogs({ limit: 50 })
    qaLogs.value = d.items
    qaTotal.value = d.total
  } catch { /* ignore */ }
}

async function loadQaStats() {
  if (!isAdmin.value) return
  try {
    qaStats.value = await api.qaStats(7)
  } catch { /* ignore */ }
}

async function loadFeedbacks() {
  if (!isAdmin.value) return
  try {
    const d = await api.feedbacks({ limit: 50 })
    feedbacks.value = d.items
  } catch { /* ignore */ }
}

async function loadAudits() {
  if (!isAdmin.value) return
  try {
    const d = await api.auditLogs({ limit: 60 })
    audits.value = d.items
    auditTotal.value = d.total
  } catch { /* ignore */ }
}

async function loadConfigs() {
  if (!auth.can('config:view')) return
  try {
    const d = await api.configs()
    configs.value = d.items
    configCats.value = d.categories
  } catch { /* ignore */ }
}

async function loadRoles() {
  if (!auth.can('user:view')) return
  try {
    const d = await api.roles()
    roles.value = d.roles
  } catch { /* ignore */ }
}

async function loadAll() {
  loading.value = true
  try {
    await Promise.all([
      loadOverview(), loadUsers(), loadQaLogs(),
      loadQaStats(), loadFeedbacks(), loadAudits(),
      loadConfigs(), loadRoles(),
    ])
  } finally {
    loading.value = false
  }
}

/* ----------------------------- 用户管理 ----------------------------- */
async function submitUser() {
  try {
    await api.createUser({ ...userForm })
    ElMessage.success(`已创建账号 ${userForm.username}`)
    userDialog.value = false
    Object.assign(userForm, {
      username: '', password: '', role: 'physician',
      display_name: '', department: '', email: '',
    })
    await Promise.all([loadUsers(), loadOverview()])
  } catch (err) {
    ElMessage.error((err as Error).message)
  }
}

async function toggleActive(u: any) {
  try {
    await api.setUserActive(u.id, !u.is_active)
    ElMessage.success(u.is_active ? '已停用' : '已启用')
    await loadUsers()
  } catch (err) {
    ElMessage.error((err as Error).message)
  }
}

async function changeRole(u: any, role: string) {
  try {
    await api.setUserRole(u.id, role)
    ElMessage.success(`已将 ${u.username} 设为 ${role === 'admin' ? '管理员' : '医师'}`)
    await loadUsers()
  } catch (err) {
    ElMessage.error((err as Error).message)
    await loadUsers()   // 失败时回滚界面显示
  }
}

async function resetPassword(u: any) {
  try {
    const { value } = await ElMessageBox.prompt(
      `为 ${u.username} 设置新口令（至少 8 位）`,
      '重置口令',
      { inputType: 'password', inputPattern: /.{8,}/, inputErrorMessage: '至少 8 位' },
    )
    await api.resetUserPassword(u.id, value)
    ElMessage.success('口令已重置')
    await loadAudits()
  } catch (err) {
    if (err !== 'cancel' && err !== 'close') ElMessage.error((err as Error).message)
  }
}

async function unlock(u: any) {
  try {
    await api.unlockUser(u.id)
    ElMessage.success(`已解锁 ${u.username}`)
    await loadUsers()
  } catch (err) {
    ElMessage.error((err as Error).message)
  }
}

/* ----------------------------- 配置 ----------------------------- */
async function saveConfig(item: any, newValue: unknown) {
  try {
    await api.updateConfig(item.key, newValue)
    ElMessage.success(`${item.label} 已更新`)
    await Promise.all([loadConfigs(), loadAudits()])
  } catch (err) {
    ElMessage.error((err as Error).message)
    await loadConfigs()
  }
}

async function resetConfig(item?: any) {
  const label = item ? item.label : '全部配置'
  try {
    await ElMessageBox.confirm(
      `确认将${label}恢复为默认值？默认值取自 .env 配置。`,
      '恢复默认',
      { type: 'warning' },
    )
  } catch { return }
  try {
    await api.resetConfig(item?.key)
    ElMessage.success('已恢复默认值')
    await Promise.all([loadConfigs(), loadAudits()])
  } catch (err) {
    ElMessage.error((err as Error).message)
  }
}

/* ----------------------------- 反馈处理 ----------------------------- */
function openResolve(fb: any) {
  feedbackForm.id = fb.id
  feedbackForm.note = ''
  feedbackDialog.value = true
}

async function submitResolve() {
  try {
    await api.resolveFeedback(feedbackForm.id, feedbackForm.note)
    ElMessage.success('已标记为处理完成')
    feedbackDialog.value = false
    await loadFeedbacks()
  } catch (err) {
    ElMessage.error((err as Error).message)
  }
}

/* ----------------------------- 工具 ----------------------------- */
function fmtTime(s?: string | null) {
  if (!s) return '—'
  return s.replace('T', ' ').slice(0, 19)
}

function fmtMs(ms?: number | null) {
  if (!ms) return '—'
  return ms > 1000 ? `${(ms / 1000).toFixed(1)}s` : `${Math.round(ms)}ms`
}

function ratingType(r: number) {
  if (r >= 4) return 'success'
  if (r === 3) return 'warning'
  return 'danger'
}

/** 配置项是否支持运行时热生效 */
function isHotReloadable(item: any) {
  return item.category === 'retrieval' || item.key === 'llm.temperature'
}

onMounted(async () => {
  // 有令牌时先刷新用户信息（可能已过期），成功后再加载数据
  if (auth.isLoggedIn) {
    await auth.refresh()
    if (auth.isLoggedIn) await loadAll()
  }
})
</script>

<template>
  <div class="admin-view">
    <!-- ==================== 未登录 ==================== -->
    <div v-if="!auth.isLoggedIn" class="login-wrap">
      <el-card class="login-card" shadow="never">
        <div class="login-head">
          <div class="login-logo">CR</div>
          <div class="login-title">系统管理登录</div>
          <div class="login-sub">结直肠手术围术期知识库智能问答系统</div>
        </div>

        <el-form label-position="top" @submit.prevent="doLogin">
          <el-form-item label="用户名">
            <el-input
              v-model="loginForm.username"
              placeholder="请输入用户名"
              size="large"
              @keyup.enter="doLogin"
            >
              <template #prefix><el-icon><User /></el-icon></template>
            </el-input>
          </el-form-item>
          <el-form-item label="口令">
            <el-input
              v-model="loginForm.password"
              type="password"
              placeholder="请输入口令"
              size="large"
              show-password
              @keyup.enter="doLogin"
            >
              <template #prefix><el-icon><Lock /></el-icon></template>
            </el-input>
          </el-form-item>
          <el-button
            type="primary"
            size="large"
            style="width: 100%"
            :loading="loginLoading"
            @click="doLogin"
          >
            登录
          </el-button>
        </el-form>

        <div class="login-note">
          请使用管理员分配的账号登录。如需开通或重置账户，请联系项目管理员。
        </div>
        <div class="login-note">
          <b>权限说明</b>：管理员可管理账号与系统配置；
          医师可问答、上传影像、提交反馈，但不能管理用户与配置。
        </div>
      </el-card>
    </div>

    <!-- ==================== 已登录 ==================== -->
    <div v-else v-loading="loading" class="admin-body">
      <!-- 顶部条 -->
      <div class="admin-bar">
        <div class="bar-left">
          <el-tag size="small" :type="isAdmin ? 'danger' : 'success'" effect="plain">
            {{ auth.user?.role_label }}
          </el-tag>
          <span class="bar-user">
            {{ auth.user?.display_name || auth.user?.username }}
            <template v-if="auth.user?.department">· {{ auth.user.department }}</template>
          </span>
          <span class="bar-perms">权限 {{ auth.user?.permissions.length }} 项</span>
        </div>
        <div class="bar-right">
          <el-button link size="small" @click="loadAll">
            <el-icon><Refresh /></el-icon>刷新
          </el-button>
          <el-button link size="small" @click="doLogout">
            <el-icon><SwitchButton /></el-icon>退出
          </el-button>
        </div>
      </div>

      <el-tabs v-model="activeTab" class="admin-tabs">
        <!-- ---------------- 概览 ---------------- -->
        <el-tab-pane label="系统概览" name="overview">
          <div v-if="overview" class="stat-grid">
            <div class="stat-card">
              <div class="stat-label">用户</div>
              <div class="stat-value">{{ overview.users?.total ?? 0 }}</div>
              <div class="stat-sub">
                启用 {{ overview.users?.active ?? 0 }}
                <!-- 后端 user_repo.stats 返回的是 by_role（没有 by_role_label），
                     此前读 by_role_label 恒为 undefined，角色分布永远不显示。 -->
                <template v-if="overview.users?.by_role">
                  · {{ Object.entries(overview.users.by_role).map(([k, v]) => `${k} ${v}`).join(' / ') }}
                </template>
              </div>
            </div>
            <div class="stat-card">
              <div class="stat-label">近 7 天问答</div>
              <div class="stat-value">{{ overview.qa?.total ?? 0 }}</div>
              <div class="stat-sub">
                平均引用 {{ overview.qa?.avg_citations ?? 0 }} 条 ·
                平均 {{ fmtMs(overview.qa?.avg_total_ms) }}
              </div>
            </div>
            <div class="stat-card">
              <div class="stat-label">知识库</div>
              <div class="stat-value">{{ overview.knowledge?.documents ?? 0 }}</div>
              <div class="stat-sub">文献篇数 · {{ overview.knowledge?.chunks ?? 0 }} 个文本块</div>
            </div>
            <div class="stat-card">
              <div class="stat-label">影像库</div>
              <div class="stat-value">{{ overview.images?.total ?? 0 }}</div>
              <div class="stat-sub">已解读 {{ overview.images?.analyzed ?? 0 }} 张</div>
            </div>
            <div class="stat-card">
              <div class="stat-label">近 7 天操作</div>
              <div class="stat-value">{{ overview.audit?.total ?? 0 }}</div>
              <div class="stat-sub">
                失败 {{ overview.audit?.failed ?? 0 }} 次
              </div>
            </div>
            <div class="stat-card">
              <div class="stat-label">质量告警</div>
              <div class="stat-value warn">
                {{ overview.qa?.with_invalid_citations ?? 0 }}
              </div>
              <div class="stat-sub">
                出现编造引用的问答 ·
                无证据 {{ overview.qa?.no_evidence ?? 0 }} 条
              </div>
            </div>
          </div>

          <el-alert
            v-if="overview && !overview.auth_enabled"
            type="warning" :closable="false" show-icon class="mb12"
          >
            <template #title>认证已关闭</template>
            当前 <code>AUTH_ENABLED=false</code>，所有接口无需登录即可访问。
            仅建议在脚本批处理时临时关闭。
          </el-alert>
        </el-tab-pane>

        <!-- ---------------- 质量监控 ---------------- -->
        <el-tab-pane label="问答质量" name="quality">
          <div v-if="qaStats" class="quality-head">
            <el-statistic title="问答总数" :value="qaStats.total" />
            <el-statistic title="无证据回答" :value="qaStats.no_evidence" />
            <el-statistic title="平均引用数" :value="qaStats.avg_citations" :precision="2" />
            <el-statistic title="平均耗时" :value="Math.round(qaStats.avg_total_ms)" suffix="ms" />
            <el-statistic title="编造引用条数" :value="qaStats.with_invalid_citations" />
          </div>

          <el-table :data="qaLogs" size="small" stripe max-height="420">
            <el-table-column prop="created_at" label="时间" width="150">
              <template #default="{ row }">{{ fmtTime(row.created_at) }}</template>
            </el-table-column>
            <el-table-column prop="question" label="问题" min-width="240" show-overflow-tooltip />
            <el-table-column prop="phase" label="分期" width="70">
              <template #default="{ row }">{{ PHASE_LABELS[row.phase] || '—' }}</template>
            </el-table-column>
            <el-table-column label="引用" width="150">
              <template #default="{ row }">
                <span>{{ row.citation_count }}</span>
                <el-tag v-if="row.invalid_citations" type="danger" size="small" class="ml4">
                  编造 {{ row.invalid_citations }}
                </el-tag>
                <el-tag v-if="row.unsupported_numbers" type="warning" size="small" class="ml4">
                  数值 {{ row.unsupported_numbers }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column label="耗时" width="80">
              <template #default="{ row }">{{ fmtMs(row.total_ms) }}</template>
            </el-table-column>
            <el-table-column label="评分" width="90">
              <template #default="{ row }">
                <el-tag v-if="row.rating" :type="ratingType(row.rating)" size="small">
                  {{ row.rating }} 分
                </el-tag>
                <span v-else class="muted">未评价</span>
              </template>
            </el-table-column>
          </el-table>

          <div class="section-title">人工反馈</div>
          <el-table :data="feedbacks" size="small" stripe>
            <el-table-column label="评分" width="80">
              <template #default="{ row }">
                <el-tag :type="ratingType(row.rating)" size="small">{{ row.rating }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column prop="issue_label" label="问题类型" width="120" />
            <el-table-column prop="question" label="对应问题" min-width="200" show-overflow-tooltip />
            <el-table-column prop="comment" label="说明" min-width="180" show-overflow-tooltip />
            <el-table-column label="状态" width="90">
              <template #default="{ row }">
                <el-tag :type="row.is_resolved ? 'success' : 'warning'" size="small">
                  {{ row.is_resolved ? '已处理' : '待处理' }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column label="操作" width="90">
              <template #default="{ row }">
                <el-button
                  v-if="!row.is_resolved" link size="small" type="primary"
                  @click="openResolve(row)"
                >
                  处理
                </el-button>
                <span v-else class="muted">—</span>
              </template>
            </el-table-column>
          </el-table>
        </el-tab-pane>

        <!-- ---------------- 用户管理 ---------------- -->
        <el-tab-pane v-if="auth.can('user:view')" label="用户管理" name="users">
          <div class="tab-toolbar">
            <span class="muted">共 {{ userTotal }} 个账号</span>
            <el-button
              v-if="auth.can('user:manage')"
              type="primary" size="small" @click="userDialog = true"
            >
              <el-icon><Plus /></el-icon>新建账号
            </el-button>
          </div>

          <el-table :data="users" size="small" stripe>
            <el-table-column prop="username" label="登录名" width="130" />
            <el-table-column prop="display_name" label="姓名" width="110" />
            <el-table-column prop="department" label="科室" width="130" />
            <el-table-column label="角色" width="150">
              <template #default="{ row }">
                <el-select
                  v-if="auth.can('user:manage') && row.username !== auth.user?.username"
                  :model-value="row.role" size="small"
                  @change="(v: string) => changeRole(row, v)"
                >
                  <el-option label="管理员" value="admin" />
                  <el-option label="医师" value="physician" />
                </el-select>
                <el-tag v-else :type="row.role === 'admin' ? 'danger' : 'success'" size="small">
                  {{ row.role_label }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column label="状态" width="150">
              <template #default="{ row }">
                <el-tag v-if="!row.is_active" type="info" size="small">已停用</el-tag>
                <el-tag v-else-if="row.is_locked" type="danger" size="small">已锁定</el-tag>
                <el-tag v-else type="success" size="small">正常</el-tag>
              </template>
            </el-table-column>
            <el-table-column label="最近登录" width="150">
              <template #default="{ row }">{{ fmtTime(row.last_login_at) }}</template>
            </el-table-column>
            <el-table-column prop="login_count" label="登录次数" width="90" />
            <el-table-column v-if="auth.can('user:manage')" label="操作" width="200">
              <template #default="{ row }">
                <el-button link size="small" @click="toggleActive(row)">
                  {{ row.is_active ? '停用' : '启用' }}
                </el-button>
                <el-button link size="small" @click="resetPassword(row)">重置口令</el-button>
                <el-button v-if="row.is_locked" link size="small" type="warning" @click="unlock(row)">
                  解锁
                </el-button>
              </template>
            </el-table-column>
          </el-table>

          <div class="section-title">角色权限对照</div>
          <el-table :data="roles" size="small" border>
            <el-table-column prop="label" label="角色" width="100" />
            <el-table-column label="权限点">
              <template #default="{ row }">
                <el-tag
                  v-for="p in row.permissions" :key="p"
                  size="small" effect="plain" class="perm-tag"
                >
                  {{ p }}
                </el-tag>
              </template>
            </el-table-column>
          </el-table>
        </el-tab-pane>

        <!-- ---------------- 系统配置 ---------------- -->
        <el-tab-pane v-if="auth.can('config:view')" label="系统配置" name="config">
          <div class="tab-toolbar">
            <span class="muted">
              共 {{ configs.length }} 项 ·
              {{ Object.entries(configCats).map(([k, v]) => `${k} ${v}`).join(' / ') }}
            </span>
            <el-button
              v-if="auth.can('config:update')"
              size="small" @click="resetConfig()"
            >
              <el-icon><RefreshLeft /></el-icon>全部恢复默认
            </el-button>
          </div>

          <el-alert type="info" :closable="false" show-icon class="mb12">
            <template #title>关于生效时机</template>
            检索权重类参数在每次检索时读取，修改后<b>立即生效</b>；
            标注「需重启」的参数与已加载的模型绑定，修改后需重启后端。
          </el-alert>

          <el-table :data="configs" size="small" stripe>
            <el-table-column prop="label" label="参数" width="180" />
            <el-table-column label="当前值" width="220">
              <template #default="{ row }">
                <el-switch
                  v-if="row.value_type === 'bool'"
                  :model-value="row.value === 'true'"
                  :disabled="!auth.can('config:update')"
                  @change="(v: any) => saveConfig(row, v)"
                />
                <el-input-number
                  v-else-if="row.value_type === 'int' || row.value_type === 'float'"
                  :model-value="Number(row.value)"
                  :min="row.min_value ?? undefined"
                  :max="row.max_value ?? undefined"
                  :step="row.value_type === 'float' ? 0.05 : 1"
                  :precision="row.value_type === 'float' ? 2 : 0"
                  size="small" controls-position="right" style="width: 160px"
                  :disabled="!auth.can('config:update')"
                  @change="(v: any) => saveConfig(row, v)"
                />
                <span v-else>{{ row.value }}</span>
              </template>
            </el-table-column>
            <el-table-column label="范围/默认" width="140">
              <template #default="{ row }">
                <span class="muted">
                  <template v-if="row.min_value !== null">
                    [{{ row.min_value }}, {{ row.max_value }}]
                  </template>
                  <template v-else>—</template>
                </span>
              </template>
            </el-table-column>
            <el-table-column prop="description" label="说明" min-width="300">
              <template #default="{ row }">
                <div class="cfg-desc">{{ row.description }}</div>
                <el-tag
                  :type="isHotReloadable(row) ? 'success' : 'info'"
                  size="small" effect="plain" class="mt2"
                >
                  {{ isHotReloadable(row) ? '立即生效' : '需重启后端' }}
                </el-tag>
                <!-- ConfigItem 没有 default 字段（只有 value / parsed_value / is_default），
                     原先显示「原值 —」，等于告诉管理员没有默认值。改为直接展示当前生效值。 -->
                <el-tag v-if="!row.is_default" type="warning" size="small" effect="plain" class="mt2 ml4">
                  已修改（当前 {{ row.parsed_value ?? row.value ?? '—' }}）
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column v-if="auth.can('config:update')" label="操作" width="80">
              <template #default="{ row }">
                <el-button link size="small" @click="resetConfig(row)">恢复</el-button>
              </template>
            </el-table-column>
          </el-table>
        </el-tab-pane>

        <!-- ---------------- 审计日志 ---------------- -->
        <el-tab-pane v-if="auth.can('audit:view')" label="审计日志" name="audit">
          <div class="tab-toolbar">
            <span class="muted">共 {{ auditTotal }} 条 · 记录所有写操作与登录尝试</span>
          </div>
          <el-table :data="audits" size="small" stripe max-height="560">
            <el-table-column label="时间" width="150">
              <template #default="{ row }">{{ fmtTime(row.created_at) }}</template>
            </el-table-column>
            <el-table-column prop="username" label="操作者" width="110">
              <template #default="{ row }">{{ row.username || '—' }}</template>
            </el-table-column>
            <el-table-column prop="action" label="动作" width="130" />
            <el-table-column prop="resource" label="资源" width="100" />
            <el-table-column prop="resource_id" label="对象" width="150" show-overflow-tooltip />
            <el-table-column prop="ip_address" label="来源 IP" width="130">
              <template #default="{ row }">{{ row.ip_address || '—' }}</template>
            </el-table-column>
            <el-table-column label="结果" width="90">
              <template #default="{ row }">
                <el-tag :type="row.success ? 'success' : 'danger'" size="small">
                  {{ row.success ? '成功' : '失败' }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column prop="error_message" label="失败原因" min-width="150" show-overflow-tooltip>
              <template #default="{ row }">{{ row.error_message || '—' }}</template>
            </el-table-column>
          </el-table>
        </el-tab-pane>
      </el-tabs>
    </div>

    <!-- 新建用户对话框 -->
    <el-dialog v-model="userDialog" title="新建账号" width="min(460px, 94vw)">
      <el-form label-width="80px" size="small">
        <el-form-item label="登录名" required>
          <el-input v-model="userForm.username" placeholder="至少 3 位，登录用" />
        </el-form-item>
        <el-form-item label="初始口令" required>
          <el-input v-model="userForm.password" placeholder="至少 8 位" show-password />
        </el-form-item>
        <el-form-item label="角色">
          <el-select v-model="userForm.role" style="width: 100%">
            <el-option label="医师（可问答、上传影像、提交反馈）" value="physician" />
            <el-option label="管理员（含用户与配置管理）" value="admin" />
          </el-select>
        </el-form-item>
        <el-form-item label="姓名">
          <el-input v-model="userForm.display_name" />
        </el-form-item>
        <el-form-item label="科室">
          <el-input v-model="userForm.department" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="userDialog = false">取消</el-button>
        <el-button type="primary" @click="submitUser">创建</el-button>
      </template>
    </el-dialog>

    <!-- 处理反馈对话框 -->
    <el-dialog v-model="feedbackDialog" title="处理反馈" width="min(440px, 94vw)">
      <el-input
        v-model="feedbackForm.note" type="textarea" :rows="3"
        placeholder="记录处理方式，例如：已补充该分期的指南类文献"
      />
      <template #footer>
        <el-button @click="feedbackDialog = false">取消</el-button>
        <el-button type="primary" @click="submitResolve">标记已处理</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.admin-view {
  height: 100%;
  overflow: hidden;
}

/* ---------- 登录 ---------- */
.login-wrap {
  display: flex;
  align-items: center;
  justify-content: center;
  height: 100%;
  background: linear-gradient(135deg, #f5f8f9 0%, #e8f0f1 100%);
}

.login-card {
  width: 400px;
  padding: 8px 12px;
  border-radius: 10px;
}

.login-head {
  text-align: center;
  margin-bottom: 20px;
}

.login-logo {
  width: 46px;
  height: 46px;
  margin: 0 auto 10px;
  font-size: 17px;
  font-weight: 700;
  line-height: 46px;
  color: #fff;
  background: var(--cr-primary);
  border-radius: 10px;
}

.login-title {
  font-size: 17px;
  font-weight: 700;
  color: #2b3a47;
}

.login-sub {
  margin-top: 4px;
  font-size: 12px;
  color: var(--cr-text-muted, #8a99a8);
}

.login-note {
  margin-top: 14px;
  font-size: 11.5px;
  line-height: 1.7;
  color: var(--cr-text-muted, #8a99a8);
}

.login-note code {
  padding: 1px 4px;
  background: #f2f5f7;
  border-radius: 3px;
  font-family: Consolas, monospace;
}

/* ---------- 主体 ---------- */
.admin-body {
  display: flex;
  flex-direction: column;
  height: 100%;
  overflow: hidden;
}

.admin-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 10px 18px;
  border-bottom: 1px solid var(--cr-border);
  flex-shrink: 0;
}

.bar-left,
.bar-right {
  display: flex;
  align-items: center;
  gap: 10px;
}

.bar-user {
  font-size: 13px;
  font-weight: 600;
}

.bar-perms {
  font-size: 11.5px;
  color: var(--cr-text-muted, #8a99a8);
}

.admin-tabs {
  flex: 1;
  min-height: 0;
  padding: 0 18px;
  overflow-y: auto;
}

/* ---------- 统计卡片 ---------- */
.stat-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(215px, 1fr));
  gap: 12px;
  padding: 8px 0 16px;
}

.stat-card {
  padding: 14px 16px;
  background: #fff;
  border: 1px solid var(--cr-border);
  border-radius: 8px;
}

.stat-label {
  font-size: 12px;
  color: var(--cr-text-muted, #8a99a8);
}

.stat-value {
  margin: 6px 0 4px;
  font-size: 24px;
  font-weight: 700;
  color: var(--cr-primary);
}

.stat-value.warn {
  color: #d9534f;
}

.stat-sub {
  font-size: 11.5px;
  line-height: 1.6;
  color: var(--cr-text-muted, #8a99a8);
}

/* ---------- 其他 ---------- */
.quality-head {
  display: flex;
  flex-wrap: wrap;
  gap: 34px;
  padding: 12px 4px 20px;
}

.tab-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 10px 0;
}

.section-title {
  margin: 20px 0 10px;
  font-size: 13px;
  font-weight: 600;
  color: var(--cr-primary);
}

.muted {
  color: var(--cr-text-muted, #8a99a8);
  font-size: 12px;
}

.mb12 {
  margin-bottom: 12px;
}

.ml4 {
  margin-left: 4px;
}

.mt2 {
  margin-top: 2px;
}

.perm-tag {
  margin: 0 4px 4px 0;
}

.cfg-desc {
  font-size: 11.5px;
  line-height: 1.6;
  color: #5a6a78;
}
.login-card { max-width: calc(100% - 24px); }
.login-wrap { overflow-y: auto; padding: 24px 0; }
.admin-bar, .tab-toolbar { flex-wrap: wrap; gap: 10px; }
@media (max-height: 700px) { .login-wrap { align-items: flex-start; } }
@media (max-width: 600px) { .login-card { padding: 0; } .admin-body { padding: 12px; } .stat-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
</style>
