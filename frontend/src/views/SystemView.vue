<script setup lang="ts">
/**
 * 系统状态页。
 *
 * 展示各依赖组件与检索模型的健康状态，并提供模型预热按钮。
 *
 * 「模型预热」是答辩演示的实用功能：
 * 检索模型采用懒加载（首次调用才载入显存，约需 10~15 秒），
 * 演示前点一下即可消除首次提问的额外等待。
 */
import { computed, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'

import { api } from '@/api/client'
import type { HealthResponse } from '@/api/types'

const loading = ref(false)
const loadingModels = ref(false)
const errorMessage = ref('')
const health = ref<HealthResponse | null>(null)

async function refresh(): Promise<void> {
  loading.value = true
  errorMessage.value = ''
  try {
    health.value = await api.health()
  } catch (err) {
    errorMessage.value = (err as Error).message
  } finally {
    loading.value = false
  }
}

/** 预热检索模型 */
async function warmUpModels(): Promise<void> {
  loadingModels.value = true
  try {
    const result = await api.loadModels()
    ElMessage.success(result.message || '模型已加载')
    await refresh()
  } catch (err) {
    ElMessage.error((err as Error).message)
  } finally {
    loadingModels.value = false
  }
}

onMounted(refresh)

/** 核心基础设施是否全部可用（LLM/VLM 密钥未配置不算故障） */
const infraOk = computed(() => {
  const h = health.value
  if (!h) return false
  const infra = new Set(['PostgreSQL+pgvector', 'Neo4j', 'Redis', '文件存储'])
  return h.components.filter((c) => infra.has(c.name)).every((c) => c.ok)
})

/** 分类组件：基础设施 / 外部服务配置 */
const infraComponents = computed(() =>
  health.value?.components.filter((c) =>
    ['PostgreSQL+pgvector', 'Neo4j', 'Redis', '文件存储'].includes(c.name),
  ) ?? [],
)

const configComponents = computed(() =>
  health.value?.components.filter(
    (c) => !['PostgreSQL+pgvector', 'Neo4j', 'Redis', '文件存储'].includes(c.name),
  ) ?? [],
)

const vram = computed(() => health.value?.models.vram)

/** 显存占用比例 */
const vramPercent = computed(() => {
  const v = vram.value
  if (!v?.available || !v.total_gb || !v.used_gb) return 0
  return Math.round((v.used_gb / v.total_gb) * 100)
})
</script>

<template>
  <div class="system-view">
    <el-alert v-if="errorMessage" type="error" :closable="false" show-icon class="mb-16">
      <template #title>无法连接后端服务</template>
      {{ errorMessage }}
      <div class="alert-hint">
        请确认后端已启动：<code>uvicorn app.main:app --port 8000</code>
      </div>
    </el-alert>

    <div v-loading="loading" class="content">
      <!-- 总体状态 -->
      <el-card shadow="never" class="status-card">
        <div class="status-head">
          <div class="status-left">
            <el-tag
              :type="infraOk ? 'success' : 'danger'"
              size="large"
              effect="dark"
              class="status-tag"
            >
              {{ infraOk ? '基础设施正常' : '存在异常' }}
            </el-tag>
            <div class="app-info">
              <div class="app-name">{{ health?.app_name || '—' }}</div>
              <div class="app-meta">
                版本 {{ health?.version || '—' }} · 环境 {{ health?.environment || '—' }}
              </div>
            </div>
          </div>
          <el-button :loading="loading" @click="refresh">
            <el-icon><Refresh /></el-icon>
            刷新
          </el-button>
        </div>
      </el-card>

      <div class="grid">
        <!-- 基础设施 -->
        <el-card shadow="never">
          <template #header>
            <div class="card-head">
              <span>基础设施</span>
              <el-tag size="small" type="info" effect="plain">
                {{ infraComponents.filter((c) => c.ok).length }} / {{ infraComponents.length }}
              </el-tag>
            </div>
          </template>
          <div
            v-for="c in infraComponents"
            :key="c.name"
            class="component-row"
          >
            <el-icon :class="c.ok ? 'ok-icon' : 'fail-icon'">
              <component :is="c.ok ? 'CircleCheckFilled' : 'CircleCloseFilled'" />
            </el-icon>
            <div class="component-body">
              <div class="component-name">{{ c.name }}</div>
              <div class="component-detail">{{ c.detail }}</div>
            </div>
            <div v-if="c.latency_ms !== null" class="component-latency">
              {{ c.latency_ms }}ms
            </div>
          </div>
        </el-card>

        <!-- 外部服务配置 -->
        <el-card shadow="never">
          <template #header>
            <div class="card-head">
              <span>外部服务配置</span>
              <el-tag size="small" type="info" effect="plain">密钥类</el-tag>
            </div>
          </template>
          <div v-for="c in configComponents" :key="c.name" class="component-row">
            <el-icon :class="c.ok ? 'ok-icon' : 'warn-icon'">
              <component :is="c.ok ? 'CircleCheckFilled' : 'WarningFilled'" />
            </el-icon>
            <div class="component-body">
              <div class="component-name">{{ c.name }}</div>
              <div class="component-detail">{{ c.detail }}</div>
            </div>
          </div>
          <div class="card-note">
            未配置的密钥不影响检索功能，但对应的生成/影像解读能力不可用。
          </div>
        </el-card>

        <!-- 检索模型 -->
        <el-card shadow="never">
          <template #header>
            <div class="card-head">
              <span>检索模型</span>
              <el-button
                size="small"
                type="primary"
                plain
                :loading="loadingModels"
                @click="warmUpModels"
              >
                <el-icon><Lightning /></el-icon>
                预热模型
              </el-button>
            </div>
          </template>

          <div class="model-row">
            <div class="model-info">
              <div class="model-name">
                {{ health?.models.embedding.name || 'BGE-M3' }}
                <el-tag
                  size="small"
                  :type="health?.models.embedding.loaded ? 'success' : 'info'"
                  effect="plain"
                >
                  {{ health?.models.embedding.loaded ? '已载入显存' : '未载入（懒加载）' }}
                </el-tag>
              </div>
              <div class="model-path mono">{{ health?.models.embedding.path || '—' }}</div>
            </div>
          </div>

          <div class="model-row">
            <div class="model-info">
              <div class="model-name">
                {{ health?.models.reranker.name || 'bge-reranker-v2-m3' }}
                <el-tag
                  size="small"
                  :type="health?.models.reranker.loaded ? 'success' : 'info'"
                  effect="plain"
                >
                  {{ health?.models.reranker.loaded ? '已载入显存' : '未载入（懒加载）' }}
                </el-tag>
              </div>
              <div class="model-path mono">{{ health?.models.reranker.path || '—' }}</div>
            </div>
          </div>

          <div class="device-row">
            <span class="device-label">推理设备</span>
            <el-tag size="small" effect="plain">{{ health?.models.device || '—' }}</el-tag>
          </div>

          <div class="card-note">
            模型采用<strong>懒加载</strong>：首次调用才载入显存（约 10~15 秒）。
            答辩演示前点击「预热模型」可消除首次提问的额外等待。
          </div>
        </el-card>

        <!-- 显存 -->
        <el-card shadow="never">
          <template #header>
            <div class="card-head"><span>显存占用</span></div>
          </template>
          <template v-if="vram?.available">
            <div class="vram-value">
              <span class="vram-used">{{ vram.used_gb }}</span>
              <span class="vram-sep">/</span>
              <span class="vram-total">{{ vram.total_gb }} GB</span>
            </div>
            <el-progress
              :percentage="vramPercent"
              :stroke-width="14"
              :color="vramPercent > 85 ? '#e74c3c' : vramPercent > 65 ? '#e67e22' : '#2b7a78'"
            />
            <div class="vram-detail">
              <div class="vram-item">
                <span>设备</span><span class="mono">{{ vram.device }}</span>
              </div>
              <div class="vram-item">
                <span>空闲</span><span class="mono">{{ vram.free_gb }} GB</span>
              </div>
              <div class="vram-item">
                <span>PyTorch 已分配</span>
                <span class="mono">{{ vram.torch_allocated_gb }} GB</span>
              </div>
            </div>
            <div class="card-note">
              「已用」含桌面合成等系统常驻占用。实测双模型全开峰值约 4.94 GB。
            </div>
          </template>
          <div v-else class="empty">未检测到可用 GPU（CPU 模式下运行）</div>
        </el-card>
      </div>
    </div>
  </div>
</template>

<style scoped>
.system-view {
  height: 100%;
  padding: 16px 20px;
  overflow-y: auto;
}

.mb-16 {
  margin-bottom: 16px;
}

.alert-hint {
  margin-top: 6px;
  font-size: 12.5px;
}

.alert-hint code {
  padding: 1px 5px;
  background: rgba(0, 0, 0, 0.06);
  border-radius: 3px;
}

/* ---------- 状态头 ---------- */
.status-card :deep(.el-card__body) {
  padding: 16px;
}

.status-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.status-left {
  display: flex;
  gap: 14px;
  align-items: center;
}

.status-tag {
  font-size: 14px;
  font-weight: 600;
}

.app-name {
  font-size: 15px;
  font-weight: 600;
  color: var(--cr-primary-dark);
}

.app-meta {
  margin-top: 2px;
  font-size: 12px;
  color: var(--cr-text-secondary);
}

/* ---------- 网格 ---------- */
.grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(340px, 1fr));
  gap: 14px;
}

.card-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-size: 14px;
  font-weight: 600;
}

/* ---------- 组件行 ---------- */
.component-row {
  display: flex;
  gap: 10px;
  align-items: flex-start;
  padding: 9px 0;
  border-bottom: 1px dashed var(--cr-border);
}

.component-row:last-of-type {
  border-bottom: none;
}

.ok-icon {
  flex-shrink: 0;
  margin-top: 2px;
  font-size: 16px;
  color: #27ae60;
}

.fail-icon {
  flex-shrink: 0;
  margin-top: 2px;
  font-size: 16px;
  color: #e74c3c;
}

.warn-icon {
  flex-shrink: 0;
  margin-top: 2px;
  font-size: 16px;
  color: #e67e22;
}

.component-body {
  flex: 1;
  min-width: 0;
}

.component-name {
  font-size: 13.5px;
  font-weight: 500;
}

.component-detail {
  margin-top: 1px;
  font-size: 12px;
  line-height: 1.5;
  color: var(--cr-text-secondary);
  word-break: break-word;
}

.component-latency {
  flex-shrink: 0;
  font-family: 'Cascadia Code', Consolas, monospace;
  font-size: 11.5px;
  color: var(--cr-text-muted);
}

.card-note {
  padding-top: 10px;
  margin-top: 8px;
  font-size: 12px;
  line-height: 1.6;
  color: var(--cr-text-muted);
  border-top: 1px solid var(--cr-border);
}

.card-note strong {
  color: var(--cr-primary);
}

/* ---------- 模型 ---------- */
.model-row {
  padding: 9px 0;
  border-bottom: 1px dashed var(--cr-border);
}

.model-name {
  display: flex;
  gap: 8px;
  align-items: center;
  font-size: 13.5px;
  font-weight: 500;
}

.model-path {
  margin-top: 3px;
  font-size: 11.5px;
  color: var(--cr-text-muted);
  word-break: break-all;
}

.device-row {
  display: flex;
  gap: 10px;
  align-items: center;
  padding-top: 10px;
}

.device-label {
  font-size: 13px;
  color: var(--cr-text-secondary);
}

/* ---------- 显存 ---------- */
.vram-value {
  margin-bottom: 8px;
  font-family: 'Cascadia Code', Consolas, monospace;
}

.vram-used {
  font-size: 24px;
  font-weight: 700;
  color: var(--cr-primary-dark);
}

.vram-sep {
  margin: 0 4px;
  color: var(--cr-text-muted);
}

.vram-total {
  font-size: 14px;
  color: var(--cr-text-secondary);
}

.vram-detail {
  margin-top: 12px;
}

.vram-item {
  display: flex;
  justify-content: space-between;
  padding: 4px 0;
  font-size: 12.5px;
  color: var(--cr-text-secondary);
}

.empty {
  padding: 20px;
  text-align: center;
  color: var(--cr-text-muted);
}
@media (max-width: 600px) { .system-view { padding: 12px; } .grid { grid-template-columns: minmax(0, 1fr); } .status-bar, .status-left { flex-wrap: wrap; gap: 10px; } }
</style>
