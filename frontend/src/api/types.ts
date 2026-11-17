/**
 * 后端接口类型定义。
 *
 * 与 backend/app/schemas/common.py 保持一致。
 * 修改后端 Schema 时须同步此处。
 */

/** 健康检查中的单个组件状态 */
export interface ComponentStatus {
  name: string
  ok: boolean
  detail: string
  latency_ms: number | null
}

/** 显存信息 */
export interface VramInfo {
  available: boolean
  device: string
  total_gb?: number
  used_gb?: number
  free_gb?: number
  torch_allocated_gb?: number
}

/** 模型加载状态 */
export interface ModelStatus {
  device: string
  embedding: {
    name: string
    path: string
    loaded: boolean
    load_seconds: number
    path_exists: boolean
    last_error: string | null
  }
  reranker: {
    name: string
    path: string
    loaded: boolean
    load_seconds: number
    path_exists: boolean
    last_error: string | null
  }
  vram: VramInfo
}

/** 健康检查响应 */
export interface HealthResponse {
  status: string
  app_name: string
  environment: string
  version: string
  components: ComponentStatus[]
  models: ModelStatus
  disclaimer: string
}

/** 知识库统计 */
export interface StatsResponse {
  documents: number
  total_chunks: number
  embedded_chunks: number
  phase_distribution: Record<string, number>
  evidence_distribution: Record<string, number>
}

/** 单路召回元信息 */
export interface RecallPathInfo {
  name: string
  count: number
  weight: number
  elapsed_ms: number
}

/** 检索结果 */
export interface RetrievedChunk {
  chunk_id: string
  document_id: string
  content: string
  score: number
  vector_score: number | null
  rerank_score: number | null
  keyword_hits: number | null
  rrf_score: number | null
  rank: number
  doc_title: string
  doc_year: number | null
  doc_journal: string | null
  doc_authors: string | null
  phase: string
  evidence_level: string
  page_start: number | null
  page_end: number | null
  section: string | null
}

/** 检索响应 */
export interface SearchResponse {
  query: string
  recall_mode: string
  total_candidates: number
  fused_count: number
  returned: number
  reranked: boolean
  recall_paths: RecallPathInfo[]
  elapsed_ms: number
  timings: Record<string, number>
  results: RetrievedChunk[]
  disclaimer: string
}

/** 一条引用溯源信息 */
export interface CitationItem {
  index: number
  chunk_id: string
  document_id: string
  doc_title: string
  doc_authors: string | null
  doc_journal: string | null
  doc_year: number | null
  page_start: number | null
  page_end: number | null
  section: string | null
  evidence_level: string
  evidence_level_zh: string
  phase: string
  content_snippet: string
  positions: number[]
}

/** 问答响应（非流式） */
export interface QaResponse {
  question: string
  answer: string
  citations: CitationItem[]
  invalid_citations: number[]
  /** 答案中出现但检索证据中找不到的数值（后端独立核验） */
  unsupported_numbers?: string[]
  /** 无据数值的提示语 */
  numeric_warning?: string
  has_evidence: boolean
  evidence_count: number
  model: string
  timings: Record<string, number>
  usage: Record<string, number>
  disclaimer: string
}

/** 问答请求参数 */
export interface QaParams {
  question: string
  phase?: string | null
  top_k?: number
  recall_mode?: string
  use_rerank?: boolean
  use_evidence_boost?: boolean
  min_evidence_score?: number
}

/* ------------------------------------------------------------------ */
/*  SSE 流式事件                                                       */
/* ------------------------------------------------------------------ */

/** 检索完成事件：携带引用元数据，前端可立即渲染引用面板 */
export interface SseRetrievalEvent {
  type: 'retrieval'
  data: {
    count: number
    top_score: number
    elapsed_ms: number
    /** 证据列表，字段比 CitationItem 少（无 positions，因为是先于答案到达的） */
    evidences: Array<Omit<CitationItem, 'positions'> & { score: number; rerank_score: number | null }>
  }
}

/** 文本增量事件 */
export interface SseDeltaEvent {
  type: 'delta'
  data: { text: string }
}

/** 完成事件 */
export interface SseDoneEvent {
  type: 'done'
  data: {
    answer: string
    citations: CitationItem[]
    invalid_citations: number[]
    /**
     * 答案中出现但检索证据中找不到的数值。
     *
     * 后端独立核验（不依赖提示词），因为实测提示词约束对数值编造无效。
     * 前端应显式提示用户核实这些数值。
     */
    unsupported_numbers?: string[]
    /** 无据数值的提示语，可直接展示 */
    numeric_warning?: string
    elapsed_ms: number
    disclaimer: string
    model?: string

    /* ---- 多轮对话专有字段（/chat/stream 会附带，单轮为空） ---- */
    session_id?: string
    question?: string
    rewritten_query?: string
    rewritten?: boolean
    is_followup?: boolean
    phase?: string
    intent?: string
    intent_confidence?: number
    timings?: Record<string, number>
  }
}

/**
 * 多轮对话的路由信息事件。
 *
 * 在检索之前到达，因此前端可以立即展示
 * 「通用 · 知识提问（置信度 0.82）」与「已改写为：…」，
 * 不必等答案生成完。
 */
export interface SseMetaEvent {
  type: 'meta'
  data: {
    session_id: string
    question: string
    rewritten_query: string
    rewritten: boolean
    is_followup: boolean
    phase: string
    intent: string
    intent_confidence: number
  }
}

/** 多轮对话请求参数 */
export interface ChatParams {
  question: string
  session_id?: string | null
  top_k?: number
  phase?: string | null
  use_intent_routing?: boolean
}

/** 错误事件 */
export interface SseErrorEvent {
  type: 'error'
  data: { message: string; reason?: string }
}

export type SseEvent =
  | SseMetaEvent
  | SseRetrievalEvent
  | SseDeltaEvent
  | SseDoneEvent
  | SseErrorEvent

/** 分期选项 */
export interface PhaseOption {
  value: string
  label: string
}

/** 证据等级选项 */
export interface EvidenceOption {
  value: string
  label: string
  weight: number
}

export interface PhasesResponse {
  phases: PhaseOption[]
  evidence_levels: EvidenceOption[]
}

export interface CaseItem {
  id: string
  case_code: string
  title: string
  age?: number | null
  sex?: string | null
  diagnosis?: string | null
  surgery?: string | null
  phase: string
  history?: string | null
  findings?: string | null
  operative_note?: string | null
  prognosis?: string | null
  complications?: unknown[] | null
  tags: string[]
  image_ids: string[]
  source_title?: string | null
  source_url?: string | null
  source_doi?: string | null
  source_license?: string | null
  source_note?: string | null
  prepared_by?: string | null
  reviewed_by?: string | null
  reviewed_at?: string | null
  review_note?: string | null
  is_public: boolean
  has_embedding: boolean
  similarity?: number | null
  created_at?: string | null
  updated_at?: string | null
}

export interface CaseListResponse {
  items: CaseItem[]
  total: number
  limit: number
  offset: number
}

export interface SimilarImageItem {
  id: string
  filename: string
  title?: string | null
  description?: string | null
  surgery_type?: string | null
  anatomy_site?: string | null
  phase: string
  url: string
  similarity: number
}
