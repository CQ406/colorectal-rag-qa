/**
 * 后端 API 客户端。
 *
 * 两类调用：
 *  - 普通请求用 axios（自动 JSON、拦截错误、注入令牌）
 *  - SSE 流式用 fetch + ReadableStream（axios 不支持浏览器端流式读取）
 *
 * SSE 解析要点：
 *  TCP 分片可能把一条事件切成两半，因此必须用缓冲区累积文本，
 *  按 "\n\n" 切出完整事件后再解析。直接对每个 chunk 做 JSON.parse 会随机失败。
 */

import axios, { type AxiosInstance } from 'axios'
import type {
  ChatParams,
  CaseListResponse,
  HealthResponse,
  PhasesResponse,
  QaParams,
  QaResponse,
  SearchResponse,
  SseEvent,
  StatsResponse,
  SimilarImageItem,
} from './types'

/** 后端地址：开发环境走 Vite 代理（vite.config.ts），故用相对路径 */
const BASE = '/api'

const http: AxiosInstance = axios.create({
  baseURL: BASE,
  timeout: 300_000, // 问答涉及 LLM 推理，超时设长
  headers: { 'Content-Type': 'application/json' },
})

/* ------------------------------------------------------------------ */
/*  拦截器                                                            */
/* ------------------------------------------------------------------ */

/**
 * 请求拦截器：注入认证令牌（阶段九）。
 *
 * 直接读 localStorage 而**不 import auth store**：
 * store 依赖本模块的 api 对象，互相 import 会形成循环依赖，
 * 导致初始化顺序问题。令牌只是一串文本，直接读取没有副作用。
 */
http.interceptors.request.use((config) => {
  const token = localStorage.getItem('cr_access_token')
  if (token) {
    config.headers = config.headers ?? {}
    config.headers.Authorization = 'Bearer ' + token
  }
  return config
})

/** 统一错误处理：把后端的 detail 提取为可读信息 */
http.interceptors.response.use(
  (resp) => resp,
  (error) => {
    const detail = error?.response?.data?.detail
    const status = error?.response?.status

    // 401：令牌无效、过期，或账号被停用。
    // 清理本地登录态并广播事件，由界面决定如何跳转
    // （用事件而非直接改 location，避免与路由守卫相互打断）。
    if (status === 401) {
      const url = error?.config?.url ?? ''
      // 登录接口的 401 是「账号或口令错误」，不是会话过期。
      // 此前一律改写成"登录已过期"，用户看不到真实原因（也无法区分账号锁定）。
      if (url.includes('/auth/login')) {
        return Promise.reject(
          new Error(typeof detail === 'string' ? detail : '账号或口令错误'),
        )
      }
      localStorage.removeItem('cr_access_token')
      localStorage.removeItem('cr_user')
      window.dispatchEvent(new CustomEvent('cr:unauthorized'))
      return Promise.reject(new Error('登录已过期，请重新登录'))
    }

    if (detail) {
      return Promise.reject(
        new Error(typeof detail === 'string' ? detail : JSON.stringify(detail)),
      )
    }
    if (error?.code === 'ECONNABORTED') {
      return Promise.reject(new Error('请求超时，请稍后重试'))
    }
    return Promise.reject(
      new Error(
        status
          ? '请求失败 (HTTP ' + status + ')'
          : '无法连接后端服务，请确认服务已启动',
      ),
    )
  },
)

/* ------------------------------------------------------------------ */
/*  普通接口                                                          */
/* ------------------------------------------------------------------ */

export const api = {
  /** 服务信息 */
  async root() {
    const { data } = await axios.get('/')
    return data
  },

  /** 健康检查 */
  async health(): Promise<HealthResponse> {
    const { data } = await http.get<HealthResponse>('/health')
    return data
  },

  /** 知识库统计 */
  async stats(): Promise<StatsResponse> {
    const { data } = await http.get<StatsResponse>('/stats')
    return data
  },

  /** 围术期分期选项 */
  async phases(): Promise<PhasesResponse> {
    const { data } = await http.get<PhasesResponse>('/phases')
    return data
  },

  /** 检索（不生成答案，用于调试与对照） */
  async search(params: Record<string, unknown>): Promise<SearchResponse> {
    const { data } = await http.post<SearchResponse>('/search', params)
    return data
  },

  /**
   * 取文本块全文。
   *
   * 检索结果里的 `content_snippet` 后端已截断到约 300 字，
   * 引用面板「展开原文」需要另取一次全文才能看到整段内容。
   */
  async chunkDetail(chunkId: string) {
    const { data } = await http.get('/chunks/' + chunkId)
    return data
  },

  /** 问答（非流式） */
  async qa(params: QaParams): Promise<QaResponse> {
    const { data } = await http.post<QaResponse>('/qa', params)
    return data
  },

  /** 预加载检索模型（答辩演示前调用可消除首次加载等待） */
  async loadModels() {
    const { data } = await http.post('/models/load')
    return data
  },

  /** 检索模型状态 */
  async modelStatus() {
    const { data } = await http.get('/models/status')
    return data
  },

  /* ---------------- 多轮对话（阶段七） ---------------- */

  /** 多轮对话问答（首次不传 sessionId，响应返回会话 ID） */
  async chat(params: ChatParams) {
    const { data } = await http.post('/chat', params)
    return data
  },

  /** 查看会话历史 */
  async conversation(sessionId: string) {
    const { data } = await http.get('/chat/' + sessionId)
    return data
  },

  /** 清除会话 */
  async clearConversation(sessionId: string) {
    const { data } = await http.delete('/chat/' + sessionId)
    return data
  },

  /** 会话统计 */
  async chatStats() {
    const { data } = await http.get('/chat/stats')
    return data
  },

  /* ---------------- 影像知识库（阶段六） ---------------- */

  /** 影像列表 */
  async images(params?: {
    phase?: string
    surgery_type?: string
    keyword?: string
    limit?: number
    offset?: number
  }) {
    const { data } = await http.get('/images', { params })
    return data
  },

  async manageImages(params?: Record<string, unknown>) {
    const { data } = await http.get('/images/manage', { params })
    return data
  },

  async manageImageStats() {
    const { data } = await http.get('/images/manage/stats')
    return data
  },

  async manageImageDetail(id: string) {
    const { data } = await http.get('/images/manage/' + id)
    return data
  },

  async privateImageBlob(id: string): Promise<Blob> {
    const { data } = await http.get('/images/manage/' + id + '/raw', { responseType: 'blob' })
    return data
  },

  /** 影像库统计（含视觉模型配置状态） */
  async imageStats() {
    const { data } = await http.get('/images/stats')
    return data
  },

  /** 影像详情（含已缓存的解读） */
  async imageDetail(id: string) {
    const { data } = await http.get('/images/' + id)
    return data
  },

  /** 上传影像 */
  async uploadImage(form: FormData) {
    const { data } = await http.post('/images', form, {
      headers: { 'Content-Type': 'multipart/form-data' },
    })
    return data
  },

  /** 更新影像元数据 */
  async updateImage(id: string, patch: Record<string, unknown>) {
    const { data } = await http.patch('/images/' + id, patch)
    return data
  },

  /** 删除影像 */
  async deleteImage(id: string) {
    const { data } = await http.delete('/images/' + id)
    return data
  },

  /** 影像辅助解读（VLM 描述 + 检索 + 综合，耗时较长） */
  async analyzeImage(payload: {
    image_id?: string
    image_base64?: string
    mime_type?: string
    top_k?: number
    phase?: string | null
    image_context?: string
    use_cache?: boolean
  }) {
    const { data } = await http.post('/images/analyze', payload, {
      timeout: 600_000,
    })
    return data
  },

  /** 影像特征相似检索 */
  async similarImages(payload: {
    image_id?: string
    image_base64?: string
    mime_type?: string
    top_k?: number
  }): Promise<{ query_image_id?: string | null; items: SimilarImageItem[]; total: number }> {
    const { data } = await http.post('/images/similar', payload)
    return data
  },

  /* ---------------- 典型病例库 ---------------- */
  async cases(params?: {
    keyword?: string
    phase?: string
    surgery?: string
    limit?: number
    offset?: number
  }): Promise<CaseListResponse> {
    const { data } = await http.get<CaseListResponse>('/cases', { params })
    return data
  },

  async manageCases(params?: {
    keyword?: string
    phase?: string
    surgery?: string
    limit?: number
    offset?: number
  }): Promise<CaseListResponse> {
    const { data } = await http.get<CaseListResponse>('/cases/manage', { params })
    return data
  },

  async caseDetail(id: string) {
    const { data } = await http.get('/cases/' + id)
    return data
  },

  async searchCases(payload: { query: string; top_k?: number; phase?: string; surgery?: string }) {
    const { data } = await http.post<CaseListResponse>('/cases/search', payload)
    return data
  },

  async createCase(payload: Record<string, unknown>) {
    const { data } = await http.post('/cases', payload)
    return data
  },

  async updateCase(id: string, payload: Record<string, unknown>) {
    const { data } = await http.patch('/cases/' + id, payload)
    return data
  },

  async deleteCase(id: string) {
    const { data } = await http.delete('/cases/' + id)
    return data
  },

  async caseReviews(id: string) {
    const { data } = await http.get('/cases/manage/' + id + '/reviews')
    return data
  },

  /* ---------------- 认证与用户管理（阶段九） ---------------- */

  /** 登录 */
  async login(payload: { username: string; password: string }) {
    const { data } = await http.post('/auth/login', payload)
    return data
  },

  /** 当前用户信息 */
  async me() {
    const { data } = await http.get('/auth/me')
    return data
  },

  /** 修改自己的口令 */
  async changePassword(payload: { old_password: string; new_password: string }) {
    const { data } = await http.post('/auth/change-password', payload)
    return data
  },

  /** 用户列表 */
  async users(params?: {
    keyword?: string
    role?: string
    is_active?: boolean
    limit?: number
    offset?: number
  }) {
    const { data } = await http.get('/users', { params })
    return data
  },

  /** 用户统计 */
  async userStats() {
    const { data } = await http.get('/users/stats')
    return data
  },

  /** 创建用户 */
  async createUser(payload: Record<string, unknown>) {
    const { data } = await http.post('/users', payload)
    return data
  },

  /** 更新用户资料 */
  async updateUser(id: string, payload: Record<string, unknown>) {
    const { data } = await http.patch('/users/' + id, payload)
    return data
  },

  /** 变更角色 */
  async setUserRole(id: string, role: string) {
    const { data } = await http.put('/users/' + id + '/role', { role })
    return data
  },

  /** 启用/停用 */
  async setUserActive(id: string, isActive: boolean) {
    const { data } = await http.put('/users/' + id + '/active', { is_active: isActive })
    return data
  },

  /** 重置口令 */
  async resetUserPassword(id: string, newPassword: string) {
    const { data } = await http.put('/users/' + id + '/password', {
      new_password: newPassword,
    })
    return data
  },

  /** 解锁账号 */
  async unlockUser(id: string) {
    const { data } = await http.post('/users/' + id + '/unlock')
    return data
  },

  /** 角色与权限清单 */
  async roles() {
    const { data } = await http.get('/roles')
    return data
  },

  /* ---------------- 问答质量监控（阶段九） ---------------- */

  /** 问答日志 */
  async qaLogs(params?: Record<string, unknown>) {
    const { data } = await http.get('/admin/qa/logs', { params })
    return data
  },

  /** 质量统计 */
  async qaStats(days = 7) {
    const { data } = await http.get('/admin/qa/stats', { params: { days } })
    return data
  },

  /** 低质量样本 */
  async qaLowQuality(limit = 20) {
    const { data } = await http.get('/admin/qa/low-quality', { params: { limit } })
    return data
  },

  /** 提交反馈 */
  async submitFeedback(payload: {
    qa_log_id: string
    rating: number
    issue_type?: string
    comment?: string
  }) {
    const { data } = await http.post('/feedback', payload)
    return data
  },

  /** 反馈列表 */
  async feedbacks(params?: Record<string, unknown>) {
    const { data } = await http.get('/admin/feedback', { params })
    return data
  },

  /** 处理反馈 */
  async resolveFeedback(id: string, note?: string) {
    const { data } = await http.put('/admin/feedback/' + id + '/resolve', {
      resolved_note: note,
    })
    return data
  },

  /* ---------------- 审计与配置（阶段九） ---------------- */

  /** 审计日志 */
  async auditLogs(params?: Record<string, unknown>) {
    const { data } = await http.get('/admin/audit/logs', { params })
    return data
  },

  /** 审计统计 */
  async auditStats(days = 7) {
    const { data } = await http.get('/admin/audit/stats', { params: { days } })
    return data
  },

  /** 最近登录记录 */
  async recentLogins(limit = 10) {
    const { data } = await http.get('/admin/audit/logins', { params: { limit } })
    return data
  },

  /** 系统配置列表 */
  async configs(category?: string) {
    const { data } = await http.get('/admin/config', { params: { category } })
    return data
  },

  /** 更新配置 */
  async updateConfig(key: string, value: unknown) {
    const { data } = await http.put('/admin/config', { key, value })
    return data
  },

  /** 恢复默认配置 */
  async resetConfig(key?: string) {
    const { data } = await http.post('/admin/config/reset', null, {
      params: { key },
    })
    return data
  },

  /** 系统概览 */
  async overview() {
    const { data } = await http.get('/admin/overview')
    return data
  },
  /* ---------------- 知识库维护（阶段九） ---------------- */

  /** 文献列表 */
  async kbDocuments(params?: Record<string, unknown>) {
    const { data } = await http.get('/admin/kb/documents', { params })
    return data
  },

  /** 文献详情（含文本块） */
  async kbDocument(id: string) {
    const { data } = await http.get('/admin/kb/documents/' + id)
    return data
  },

  /** 审核文献 */
  async kbDocumentRevisions(id: string) {
    const { data } = await http.get('/admin/kb/documents/' + id + '/revisions')
    return data
  },

  /** 审核文献 */
  async kbReview(id: string, approved: boolean, note?: string) {
    const { data } = await http.put('/admin/kb/documents/' + id + '/review', {
      approved, note,
    })
    return data
  },

  /** 批量审核 */
  async kbBatchReview(documentIds: string[], approved: boolean, note?: string) {
    const { data } = await http.post('/admin/kb/documents/batch-review', {
      document_ids: documentIds, approved, note,
    })
    return data
  },

  /** 上架 / 下架 */
  async kbSetActive(id: string, isActive: boolean) {
    const { data } = await http.put('/admin/kb/documents/' + id + '/active', {
      is_active: isActive,
    })
    return data
  },

  /** 彻底删除文献 */
  async kbDeleteDocument(id: string) {
    const { data } = await http.delete('/admin/kb/documents/' + id)
    return data
  },

  /** 知识库质量评估 */
  async kbQuality() {
    const { data } = await http.get('/admin/kb/quality')
    return data
  },

  /** 按预定义检索式采集（后台任务） */
  async kbCollect(payload?: Record<string, unknown>) {
    const { data } = await http.post('/admin/kb/collect', payload ?? {})
    return data
  },

  /** 按自定义检索式采集（后台任务） */
  async kbSearchCollect(payload: {
    query: string; limit?: number; mindate?: string; embed?: boolean
  }) {
    const { data } = await http.post('/admin/kb/search', payload)
    return data
  },

  /** 补齐向量（后台任务） */
  async kbVectorize(payload?: Record<string, unknown>) {
    const { data } = await http.post('/admin/kb/vectorize', payload ?? {})
    return data
  },

  /** 任务进度 */
  async kbTask(jobId: string) {
    const { data } = await http.get('/admin/kb/tasks/' + jobId)
    return data
  },

  /** 最近任务 */
  async kbTasks(limit = 20) {
    const { data } = await http.get('/admin/kb/tasks', { params: { limit } })
    return data
  },

  /** 更新历史（批次） */
  async kbBatches(params?: Record<string, unknown>) {
    const { data } = await http.get('/admin/kb/batches', { params })
    return data
  },

  /** 批次详情 */
  async kbBatch(id: string) {
    const { data } = await http.get('/admin/kb/batches/' + id)
    return data
  },
}

/* ------------------------------------------------------------------ */
/*  SSE 流式问答                                                      */
/* ------------------------------------------------------------------ */

export interface StreamHandlers {
  /** 多轮对话专有：会话与路由信息（先于检索到达） */
  onMeta?: (event: Extract<SseEvent, { type: 'meta' }>['data']) => void
  onRetrieval?: (event: Extract<SseEvent, { type: 'retrieval' }>['data']) => void
  onDelta?: (text: string) => void
  onDone?: (event: Extract<SseEvent, { type: 'done' }>['data']) => void
  onError?: (message: string, reason?: string) => void
}

/**
 * 通用 SSE POST 读取器（内部使用）。
 *
 * 单轮问答与多轮对话共用同一套读取与切分逻辑，差别只在路径与事件类型，
 * 因此抽成一个函数，避免两处各写一遍缓冲切分（历史上两处实现不一致
 * 曾导致其中一处丢事件）。
 */
function ssePost(path: string, body: unknown, handlers: StreamHandlers): () => void {
  const controller = new AbortController()

  const run = async () => {
    let response: Response
    // 流式请求同样要带令牌。
    //
    // 此前只有 axios 拦截器注入 Authorization，fetch 没带：后端按令牌判定会话归属，
    // 于是 /chat/stream 建出的会话归到 guest:<cookie>，而历史查询走 axios（带令牌）
    // 按 user:<id> 过滤 → 登录用户刷新后历史必然 404，前端还会因此删掉本地 session id。
    const headers: Record<string, string> = {
      'Content-Type': 'application/json',
      Accept: 'text/event-stream',
    }
    const token = localStorage.getItem('cr_access_token')
    if (token) headers.Authorization = 'Bearer ' + token

    try {
      response = await fetch(BASE + path, {
        method: 'POST',
        headers,
        body: JSON.stringify(body),
        signal: controller.signal,
      })
    } catch (err) {
      if ((err as Error).name !== 'AbortError') {
        handlers.onError?.('无法连接后端: ' + (err as Error).message)
      }
      return
    }

    if (!response.ok) {
      handlers.onError?.('请求失败 (HTTP ' + response.status + ')')
      return
    }
    if (!response.body) {
      handlers.onError?.('响应体为空，无法流式读取')
      return
    }

    const reader = response.body.getReader()
    const decoder = new TextDecoder('utf-8')
    let buffer = ''
    // 是否收到过终态事件（done / error）。
    //
    // 代理或服务端可能发完 delta 就直接关流：循环正常结束却没有任何完成回调，
    // 调用方 await 的 Promise 永不落地 —— 界面会永久停在「生成中」。
    // 因此循环结束后必须补一次判定。
    let terminated = false
    let aborted = false

    const wrapped: StreamHandlers = {
      ...handlers,
      onDone: (data) => {
        terminated = true
        handlers.onDone?.(data)
      },
      onError: (message) => {
        terminated = true
        handlers.onError?.(message)
      },
    }

    try {
      while (true) {
        const { done, value } = await reader.read()
        if (done) break

        buffer += decoder.decode(value, { stream: true })

        // 按 SSE 事件分隔符切分（两个换行）
        let sepIndex: number
        while ((sepIndex = buffer.indexOf('\n\n')) !== -1) {
          const rawEvent = buffer.slice(0, sepIndex)
          buffer = buffer.slice(sepIndex + 2)
          dispatchSseEvent(rawEvent, wrapped)
        }
      }
      if (buffer.trim()) {
        dispatchSseEvent(buffer, wrapped)
      }
    } catch (err) {
      if ((err as Error).name === 'AbortError') {
        aborted = true
      } else {
        terminated = true
        handlers.onError?.('流式读取失败: ' + (err as Error).message)
      }
    } finally {
      reader.releaseLock?.()
    }

    // 通道关闭但没有终态事件：明确告知「回答可能不完整」并让调用方复位状态。
    if (!terminated && !aborted) {
      handlers.onError?.('流式连接提前中断，回答可能不完整')
    }
  }

  void run()
  return () => controller.abort()
}

/**
 * 流式问答（单轮）。
 *
 * 返回一个 abort 函数，用于用户主动停止生成。
 */
export function streamQa(params: QaParams, handlers: StreamHandlers): () => void {
  return ssePost('/qa/stream', params, handlers)
}

/**
 * 流式多轮对话。
 *
 * 事件比单轮多一个 `meta`（会话与路由信息先到），
 * 其余（retrieval / delta / done / error）与单轮一致。
 */
export function streamChat(params: ChatParams, handlers: StreamHandlers): () => void {
  return ssePost('/chat/stream', params, handlers)
}

/** 解析并分发单条 SSE 事件 */
function dispatchSseEvent(rawEvent: string, handlers: StreamHandlers): void {
  // 一条事件可能有多行，取所有 data: 行拼接
  const dataLines = rawEvent
    .split('\n')
    .filter((line) => line.startsWith('data:'))
    .map((line) => line.slice(5).trimStart())

  if (dataLines.length === 0) return

  let payload: SseEvent
  try {
    payload = JSON.parse(dataLines.join('\n')) as SseEvent
  } catch {
    // 单条事件解析失败不应中断整个流
    console.warn('[SSE] 无法解析事件:', rawEvent.slice(0, 200))
    return
  }

  switch (payload.type) {
    case 'meta':
      handlers.onMeta?.(payload.data)
      break
    case 'retrieval':
      handlers.onRetrieval?.(payload.data)
      break
    case 'delta':
      handlers.onDelta?.(payload.data.text)
      break
    case 'done':
      handlers.onDone?.(payload.data)
      break
    case 'error':
      handlers.onError?.(payload.data.message, payload.data.reason)
      break
  }
}
