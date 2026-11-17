"""Pydantic 请求/响应模型。

统一约定：
  - 所有响应模型继承 APIModel，自动带 JSON Schema 示例
  - 时间字段统一用 ISO8601，序列化为字符串
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Generic, TypeVar

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

T = TypeVar("T")


def _uuid_string(value: str) -> str:
    return str(uuid.UUID(value))


UUIDString = Annotated[str, AfterValidator(_uuid_string)]

DISCLAIMER = "本系统输出仅供教学与科研参考，不构成任何诊疗建议。"


class APIModel(BaseModel):
    """API 模型基类。"""

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)


# ============================================================
#  通用响应包装
# ============================================================
class PageMeta(APIModel):
    """分页元信息。"""

    total: int = Field(description="总记录数")
    page: int = Field(description="当前页码，从 1 开始")
    page_size: int = Field(description="每页条数")
    total_pages: int = Field(description="总页数")


class PageResponse(APIModel, Generic[T]):
    """分页响应。"""

    items: list[T]
    meta: PageMeta


class MessageResponse(APIModel):
    """简单消息响应。"""

    success: bool = True
    message: str


# ============================================================
#  健康检查
# ============================================================
class ComponentStatus(APIModel):
    """单个依赖组件的状态。"""

    name: str = Field(description="组件名")
    ok: bool = Field(description="是否可用")
    detail: str = Field(description="状态描述或错误信息")
    latency_ms: float | None = Field(default=None, description="检查耗时(毫秒)")


class HealthResponse(APIModel):
    """健康检查响应。"""

    status: str = Field(description="overall/degraded —— 全部可用为 overall")
    app_name: str
    environment: str
    version: str
    components: list[ComponentStatus]
    models: dict = Field(description="检索模型状态与显存信息")
    disclaimer: str = DISCLAIMER


# ============================================================
#  检索
# ============================================================
class SearchRequest(APIModel):
    """检索请求。"""

    query: str = Field(min_length=1, max_length=2000, description="查询文本")
    top_k: int = Field(default=5, ge=1, le=50, description="返回条数")
    phase: str | None = Field(
        default=None, description="围术期分期过滤: preop/intraop/postop/general，不传则不限制",
    )
    doc_type: str | None = Field(default=None, description="文献类型过滤")
    use_rerank: bool = Field(default=True, description="是否启用交叉编码器重排")
    recall_limit: int = Field(
        default=50, ge=10, le=200, description="每一路召回的候选数",
    )
    recall_mode: str = Field(
        default="hybrid",
        description="召回模式：hybrid(多路融合) / vector(仅向量) / keyword(仅关键词)。"
                    "用于消融实验对比各路的贡献",
    )
    rrf_k: int | None = Field(
        default=None, ge=1, le=1000, description="RRF 平滑常数，不传用配置默认值 60",
    )
    rrf_limit: int = Field(
        default=80, ge=5, le=500, description="RRF 融合后保留的候选数（送入重排的池子）",
    )
    use_graph: bool = Field(
        default=True,
        description=(
            "是否启用知识图谱召回；默认启用。早期版本曾默认为 False，"
            "导致图谱路径未执行，当前默认值已修正。"
        ),
    )
    use_evidence_boost: bool = Field(
        default=True, description="是否按证据等级加权（指南/系统评价优先）",
    )
    max_per_document: int = Field(
        default=3, ge=0, le=20,
        description="同一文献最多贡献的文本块数，0 表示不限制",
    )


class RecallPathInfo(APIModel):
    """单路召回的元信息（用于分析融合贡献）。"""

    name: str = Field(description="召回路径名: vector / keyword / graph")
    count: int = Field(description="该路召回条数")
    weight: float = Field(description="该路在 RRF 中的权重")
    elapsed_ms: float = Field(description="该路耗时(毫秒)")


class RetrievedChunk(APIModel):
    """一条检索结果。"""

    chunk_id: uuid.UUID
    document_id: uuid.UUID
    content: str = Field(description="文本块正文")
    score: float = Field(description="最终得分（重排分或融合分）")
    vector_score: float | None = Field(default=None, description="向量余弦相似度")
    rerank_score: float | None = Field(default=None, description="交叉编码器重排得分")
    keyword_hits: int | None = Field(default=None, description="命中的关键词词元数")
    rrf_score: float | None = Field(default=None, description="RRF 融合得分")
    rank: int = Field(description="最终排名，从 1 开始")

    # --- 引用溯源所需元数据 ---
    doc_title: str = Field(description="来源文献标题")
    doc_year: int | None = None
    doc_journal: str | None = None
    doc_authors: str | None = None
    phase: str = Field(description="围术期分期")
    evidence_level: str = Field(description="证据等级")
    page_start: int | None = None
    page_end: int | None = None
    section: str | None = None


class SearchResponse(APIModel):
    """检索响应。"""

    query: str
    recall_mode: str = Field(default="hybrid", description="实际使用的召回模式")
    total_candidates: int = Field(description="各路召回的候选总数（含重复）")
    fused_count: int = Field(default=0, description="RRF 融合并去重后的候选数")
    returned: int = Field(description="实际返回条数")
    reranked: bool = Field(description="是否经过重排")
    recall_paths: list[RecallPathInfo] = Field(
        default_factory=list, description="各召回路径的贡献明细",
    )
    elapsed_ms: float = Field(description="总耗时(毫秒)")
    timings: dict = Field(default_factory=dict, description="各阶段耗时明细")
    results: list[RetrievedChunk]
    disclaimer: str = DISCLAIMER


# ============================================================
#  问答（RAG）
# ============================================================
class QaRequest(APIModel):
    """问答请求。"""

    question: str = Field(min_length=1, max_length=2000, description="临床问题")
    phase: str | None = Field(
        default=None, description="限定围术期分期: preop/intraop/postop/general",
    )
    top_k: int = Field(default=5, ge=1, le=10, description="送入 LLM 的证据条数")
    recall_mode: str = Field(default="hybrid", description="召回模式，用于消融对比")
    use_rerank: bool = Field(default=True, description="是否启用重排")
    use_evidence_boost: bool = Field(default=True, description="是否按证据等级加权")
    temperature: float | None = Field(
        default=None, ge=0.0, le=2.0, description="采样温度，不传用配置默认值 0.2",
    )
    min_evidence_score: float = Field(
        default=0.05, ge=0.0, le=1.0,
        description="最低证据相关度门槛，低于此值拒绝作答（防止无依据生成）",
    )

    def to_search_request(self) -> "SearchRequest":
        """转换为检索请求。"""
        return SearchRequest(
            query=self.question,
            top_k=self.top_k,
            phase=self.phase,
            use_rerank=self.use_rerank,
            use_evidence_boost=self.use_evidence_boost,
            recall_mode=self.recall_mode,
            max_per_document=2,   # 问答时限制单一文献占比，保证证据多样性
        )


class CitationItem(APIModel):
    """一条引用溯源信息。"""

    index: int = Field(description="答案中的引用编号")
    chunk_id: str
    document_id: str
    doc_title: str = Field(description="文献标题")
    doc_authors: str | None = None
    doc_journal: str | None = None
    doc_year: int | None = None
    page_start: int | None = Field(default=None, description="起始页")
    page_end: int | None = None
    section: str | None = Field(default=None, description="所属章节")
    evidence_level: str
    evidence_level_zh: str = Field(description="证据等级中文名")
    phase: str
    content_snippet: str = Field(description="原文片段，供前端展开查看")
    positions: list[int] = Field(default_factory=list, description="在答案中的字符位置")


class QaResponse(APIModel):
    """问答响应。"""

    question: str
    answer: str = Field(description="生成的答案，正文中含 [n] 形式的引用标记")
    citations: list[CitationItem] = Field(
        default_factory=list, description="引用溯源列表",
    )
    invalid_citations: list[int] = Field(
        default_factory=list,
        description="LLM 引用但实际不存在的编号（幻觉信号，已剔除）",
    )
    # 数值主张核验（针对实测发现的高危幻觉类型）
    #
    # 背景：阶段十用 LLM 裁判评测 40 题发现，最危险的幻觉是**编造精确数值**
    # （如证据中没有的「中转率 7.1% vs 3.4%」）。而在提示词中明确禁止后重测，
    # 数值编造占比不降反升（22%→26%），说明该问题无法仅靠提示词解决。
    # 因此增加这一独立的结构化校验层。
    unsupported_numbers: list[str] = Field(
        default_factory=list,
        description="答案中出现但检索证据中找不到的数值（需人工核实）",
    )
    numeric_warning: str = Field(
        default="", description="无据数值的提示语，可直接展示给用户",
    )
    has_evidence: bool = Field(description="是否检索到有效证据")
    evidence_count: int = Field(default=0, description="送入 LLM 的证据条数")
    model: str = Field(default="", description="实际使用的模型")
    timings: dict = Field(default_factory=dict, description="各阶段耗时(毫秒)")
    usage: dict = Field(default_factory=dict, description="token 用量")
    disclaimer: str = DISCLAIMER


# ============================================================
#  多轮对话（阶段七）
# ============================================================
class ChatRequest(APIModel):
    """多轮对话请求。"""

    question: str = Field(min_length=1, max_length=2000)
    session_id: str | None = Field(
        default=None,
        max_length=64,
        description="会话 ID；不传则新建会话，后续追问需带上此 ID",
    )
    top_k: int = Field(default=5, ge=1, le=20)
    phase: str | None = Field(
        default=None, description="强制指定围术期分期，不传则由意图识别自动路由",
    )
    use_intent_routing: bool = Field(
        default=True, description="是否启用意图识别与分期路由",
    )


class ChatTurnItem(APIModel):
    """一轮对话记录。"""

    question: str
    answer: str = ""
    rewritten_query: str = ""
    phase: str = "general"
    intent: str = "knowledge"
    citations_count: int = 0
    timestamp: str = ""
    # 引用明细，供前端从历史恢复会话时重建右侧引用面板
    citations: list[dict] = []


class ChatResponse(APIModel):
    """多轮对话响应。"""

    session_id: str
    question: str
    answer: str
    citations: list[CitationItem] = Field(default_factory=list)
    invalid_citations: list[int] = Field(default_factory=list)
    unsupported_numbers: list[str] = Field(default_factory=list)
    numeric_warning: str = ""

    # 对话理解过程（对用户透明，便于验证上下文感知是否生效）
    rewritten_query: str = Field(
        default="", description="改写后的检索查询（未改写时为空）"
    )
    rewritten: bool = Field(default=False, description="是否执行了查询改写")
    is_followup: bool = Field(default=False, description="是否为追问")
    phase: str = Field(default="general", description="识别出的围术期分期")
    intent: str = Field(default="knowledge", description="识别出的问题类型")
    intent_confidence: float = 0.0

    evidence_count: int = 0
    has_evidence: bool = True
    model: str = ""
    timings: dict = Field(default_factory=dict)
    disclaimer: str = DISCLAIMER


class ConversationDetail(APIModel):
    """会话详情。"""

    session_id: str
    created_at: str = ""
    updated_at: str = ""
    turn_count: int = 0
    turns: list[ChatTurnItem] = Field(default_factory=list)


class ChatStatsResponse(APIModel):
    """会话统计。"""

    active_sessions: int = 0
    total_turns: int = 0


# ============================================================
#  影像知识库（阶段六）
# ============================================================
class ImageItem(APIModel):
    """影像元数据（列表项）。"""

    id: str
    filename: str
    title: str | None = None
    description: str | None = None
    surgery_type: str | None = None
    anatomy_site: str | None = None
    phase: str = "intraop"
    tags: list[str] = Field(default_factory=list)
    mime_type: str | None = None
    file_size: int | None = None
    width: int | None = None
    height: int | None = None
    source: str | None = None
    is_public: bool = False
    has_analysis: bool = Field(default=False, description="是否已缓存解读结果")
    vlm_model: str | None = None
    vlm_analyzed_at: str | None = None
    url: str = Field(description="影像访问地址")
    created_at: str | None = None


class ImageListResponse(APIModel):
    """影像列表响应。"""

    items: list[ImageItem] = Field(default_factory=list)
    total: int = 0
    limit: int = 50
    offset: int = 0


class ImageUpdateRequest(APIModel):
    """更新影像元数据。"""

    title: str | None = None
    description: str | None = None
    surgery_type: str | None = None
    anatomy_site: str | None = None
    phase: str | None = None
    tags: list[str] | None = None
    source: str | None = None
    is_public: bool | None = None


class ImageAnalyzeRequest(APIModel):
    """影像解读请求。"""

    image_id: UUIDString | None = Field(
        default=None, description="已入库影像的 ID；与 image_base64 二选一",
    )
    image_base64: str | None = Field(
        default=None, description="直接传入的 base64 图片（不落库，用于即时解读）",
    )
    mime_type: str = Field(default="image/jpeg", description="base64 图片的 MIME 类型")
    top_k: int = Field(default=5, ge=1, le=20, description="送入解读的证据条数")
    phase: str | None = Field(default=None, description="限定围术期分期")
    image_context: str = Field(
        default="", max_length=2000,
        description="用户提供的影像背景（术式、解剖部位等）",
    )
    use_cache: bool = Field(
        default=True, description="是否复用已缓存的解读结果",
    )


class ImageAnalysisResponse(APIModel):
    """影像辅助解读响应。"""

    image_id: str | None = None
    description: str = Field(description="视觉模型生成的客观描述")
    interpretation: str = Field(description="结合文献证据的综合解读")
    citations: list[CitationItem] = Field(default_factory=list)
    invalid_citations: list[int] = Field(default_factory=list)
    unsupported_numbers: list[str] = Field(default_factory=list)
    numeric_warning: str = ""
    retrieval_query: str = Field(default="", description="由影像描述构造的检索查询")
    evidence_count: int = 0
    has_evidence: bool = True
    vlm_model: str = ""
    llm_model: str = ""
    related_cases: list[dict] = Field(default_factory=list, description="相关公开病例")
    timings: dict = Field(default_factory=dict)
    cached: bool = Field(default=False, description="是否来自缓存")
    disclaimer: str = DISCLAIMER


class ImageStatsResponse(APIModel):
    """影像库统计。"""

    total: int = 0
    analyzed: int = 0
    by_phase: dict = Field(default_factory=dict)
    total_bytes: int = 0
    vision_configured: bool = Field(
        default=False, description="是否已配置视觉模型密钥",
    )
    vision_hint: str = Field(default="", description="未配置时的申请指引")


class SimilarImageItem(APIModel):
    """影像相似检索结果。"""

    id: str
    filename: str
    title: str | None = None
    description: str | None = None
    surgery_type: str | None = None
    anatomy_site: str | None = None
    phase: str = "intraop"
    url: str
    similarity: float


class SimilarImageResponse(APIModel):
    feature_model: str = 'histogram-rgb8-v1'
    similarity_scope: str = 'RGB color distribution only; not clinical or anatomical equivalence'
    query_image_id: str | None = None
    items: list[SimilarImageItem] = Field(default_factory=list)
    total: int = 0


class ImageSimilarRequest(APIModel):
    image_id: UUIDString | None = None
    image_base64: str | None = None
    mime_type: str = "image/jpeg"
    top_k: int = Field(default=10, ge=1, le=50)


# ============================================================
#  典型病例库
# ============================================================
class CaseItem(APIModel):
    """病例库列表项。仅返回公开、脱敏病例。"""

    id: str
    case_code: str
    title: str
    age: int | None = None
    sex: str | None = None
    diagnosis: str | None = None
    surgery: str | None = None
    phase: str = "general"
    history: str | None = None
    findings: str | None = None
    operative_note: str | None = None
    prognosis: str | None = None
    complications: list | None = None
    tags: list = Field(default_factory=list)
    image_ids: list[UUIDString] = Field(default_factory=list)
    source_title: str | None = None
    source_url: str | None = None
    source_doi: str | None = None
    source_license: str | None = None
    source_note: str | None = None
    prepared_by: str | None = None
    reviewed_by: str | None = None
    reviewed_at: str | None = None
    review_note: str | None = None
    is_public: bool = False
    has_embedding: bool = False
    similarity: float | None = None
    created_at: str | None = None
    updated_at: str | None = None


class CaseListResponse(APIModel):
    items: list[CaseItem] = Field(default_factory=list)
    total: int = 0
    limit: int = 50
    offset: int = 0


class CaseCreateRequest(APIModel):
    case_code: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=500)
    age: int | None = Field(default=None, ge=0, le=130)
    sex: str | None = Field(default=None, max_length=10)
    diagnosis: str | None = None
    surgery: str | None = None
    phase: str = "general"
    history: str | None = None
    findings: str | None = None
    operative_note: str | None = None
    prognosis: str | None = None
    complications: list = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    image_ids: list[UUIDString] = Field(default_factory=list)
    source_title: str | None = Field(default=None, max_length=1000)
    source_url: str | None = Field(default=None, max_length=2048)
    source_doi: str | None = Field(default=None, max_length=200)
    source_license: str | None = Field(default=None, max_length=200)
    source_note: str | None = Field(default=None, max_length=6000)
    review_note: str | None = Field(default=None, max_length=2000)
    is_public: bool = False


class CaseUpdateRequest(APIModel):
    case_code: str | None = Field(default=None, min_length=1, max_length=64)
    title: str | None = Field(default=None, min_length=1, max_length=500)
    age: int | None = Field(default=None, ge=0, le=130)
    sex: str | None = Field(default=None, max_length=10)
    diagnosis: str | None = None
    surgery: str | None = None
    phase: str | None = None
    history: str | None = None
    findings: str | None = None
    operative_note: str | None = None
    prognosis: str | None = None
    complications: list | None = None
    tags: list[str] | None = None
    image_ids: list[UUIDString] | None = None
    source_title: str | None = Field(default=None, max_length=1000)
    source_url: str | None = Field(default=None, max_length=2048)
    source_doi: str | None = Field(default=None, max_length=200)
    source_license: str | None = Field(default=None, max_length=200)
    source_note: str | None = Field(default=None, max_length=6000)
    review_note: str | None = Field(default=None, max_length=2000)
    is_public: bool | None = None


class CaseSearchRequest(APIModel):
    query: str = Field(min_length=1, max_length=2000)
    top_k: int = Field(default=10, ge=1, le=50)
    phase: str | None = None
    surgery: str | None = None


# ============================================================
#  模型管理
# ============================================================
class ModelStatusResponse(APIModel):
    """检索模型状态。"""

    device: str
    embedding: dict
    reranker: dict
    vram: dict


class EmbedRequest(APIModel):
    """向量化请求（用于验证模型可用性与性能）。"""

    texts: list[str] = Field(min_length=1, max_length=256, description="待向量化文本")
    batch_size: int = Field(default=32, ge=1, le=128)


class EmbedResponse(APIModel):
    """向量化响应（不返回完整向量，避免响应过大）。"""

    count: int
    dim: int
    elapsed_ms: float
    per_second: float = Field(description="吞吐，条/秒")
    preview: list[list[float]] = Field(description="前 2 条的向量前 8 维预览")


# ============================================================
#  用户与认证（阶段九）
# ============================================================
class LoginRequest(APIModel):
    """登录请求。"""

    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class UserInfo(APIModel):
    """用户信息（对外返回，不含口令哈希）。"""

    id: str
    username: str
    display_name: str | None = None
    role: str
    role_label: str = Field(default="", description="角色中文名")
    department: str | None = None
    email: str | None = None
    is_active: bool = True
    is_locked: bool = False
    last_login_at: str | None = None
    last_login_ip: str | None = None
    login_count: int = 0
    note: str | None = None
    created_at: str | None = None
    permissions: list[str] = Field(
        default_factory=list, description="该角色拥有的权限点，供前端控制按钮显隐"
    )


class LoginResponse(APIModel):
    """登录响应。"""

    access_token: str
    token_type: str = "bearer"
    expires_in: int = Field(description="有效期秒数")
    user: UserInfo


class UserCreateRequest(APIModel):
    """创建用户请求。"""

    username: str = Field(min_length=3, max_length=64, description="登录名，至少 3 位")
    password: str = Field(min_length=8, max_length=256, description="口令，至少 8 位")
    role: str = Field(default="physician", description="admin / physician")
    display_name: str | None = Field(default=None, max_length=100)
    department: str | None = Field(default=None, max_length=100)
    email: str | None = Field(default=None, max_length=200)
    note: str | None = None


class UserUpdateRequest(APIModel):
    """更新用户请求（不含口令与角色，二者有独立接口）。"""

    display_name: str | None = Field(default=None, max_length=100)
    department: str | None = Field(default=None, max_length=100)
    email: str | None = Field(default=None, max_length=200)
    note: str | None = None


class UserRoleRequest(APIModel):
    """变更角色请求。"""

    role: str = Field(description="admin / physician")


class UserActiveRequest(APIModel):
    """启用/停用请求。"""

    is_active: bool


class PasswordResetRequest(APIModel):
    """管理员重置口令。"""

    new_password: str = Field(min_length=8, max_length=256)


class PasswordChangeRequest(APIModel):
    """用户修改自己的口令。"""

    old_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=8, max_length=256)


class UserListResponse(APIModel):
    """用户列表响应。"""

    items: list[UserInfo] = Field(default_factory=list)
    total: int = 0
    limit: int = 50
    offset: int = 0


# ============================================================
#  审计日志（阶段九）
# ============================================================
class AuditLogItem(APIModel):
    """审计日志条目。"""

    id: str
    created_at: str | None = None
    username: str | None = None
    role: str | None = None
    action: str
    resource: str
    resource_id: str | None = None
    detail: dict | None = None
    ip_address: str | None = None
    user_agent: str | None = None
    success: bool = True
    error_message: str | None = None


class AuditLogListResponse(APIModel):
    """审计日志列表响应。"""

    items: list[AuditLogItem] = Field(default_factory=list)
    total: int = 0
    limit: int = 50
    offset: int = 0


# ============================================================
#  问答质量监控（阶段九）
# ============================================================
class QaLogItem(APIModel):
    """问答日志条目。"""

    id: str
    created_at: str | None = None
    username: str | None = None
    session_id: str | None = None
    trace_id: str | None = None
    question: str
    rewritten_query: str | None = None
    phase: str | None = None
    intent: str | None = None
    mode: str = "qa"
    answer_chars: int = 0
    citation_count: int = 0
    invalid_citations: int = 0
    unsupported_numbers: int = 0
    evidence_count: int = 0
    has_evidence: bool = True
    retrieval_ms: float | None = None
    generation_ms: float | None = None
    total_ms: float | None = None
    model: str | None = None
    error_message: str | None = None
    # 关联反馈（若有）
    rating: int | None = None
    issue_type: str | None = None
    feedback_comment: str | None = None
    is_resolved: bool | None = None


class QaLogListResponse(APIModel):
    """问答日志列表响应。"""

    items: list[QaLogItem] = Field(default_factory=list)
    total: int = 0
    limit: int = 50
    offset: int = 0


class FeedbackRequest(APIModel):
    """提交反馈请求。"""

    qa_log_id: str
    rating: int = Field(ge=1, le=5, description="1~5 分")
    issue_type: str | None = Field(
        default=None,
        description="accurate / incomplete / irrelevant / hallucination / citation_error / other",
    )
    comment: str | None = Field(default=None, max_length=2000)


class FeedbackItem(APIModel):
    """反馈条目。"""

    id: str
    qa_log_id: str
    question: str = ""
    rating: int
    issue_type: str | None = None
    issue_label: str | None = None
    comment: str | None = None
    is_resolved: bool = False
    resolved_note: str | None = None
    created_at: str | None = None


class FeedbackListResponse(APIModel):
    """反馈列表响应。"""

    items: list[FeedbackItem] = Field(default_factory=list)
    total: int = 0


class FeedbackResolveRequest(APIModel):
    """处理反馈请求。"""

    resolved_note: str | None = Field(default=None, max_length=2000)


class QaStatsResponse(APIModel):
    """问答质量统计。"""

    since_days: int = 7
    total: int = 0
    no_evidence: int = 0
    no_evidence_rate: float = 0.0
    avg_citations: float = 0.0
    avg_total_ms: float = 0.0
    with_invalid_citations: int = 0
    with_unsupported_numbers: int = 0
    hallucination_rate: float = 0.0
    by_phase: dict = Field(default_factory=dict)
    by_mode: dict = Field(default_factory=dict)
    feedback: dict = Field(default_factory=dict)
    top_questions: list[dict] = Field(default_factory=list)


# ============================================================
#  系统配置（阶段九）
# ============================================================
class ConfigItem(APIModel):
    """配置项。"""

    key: str
    value: str | None = None
    value_type: str = "str"
    category: str = "general"
    label: str | None = None
    description: str | None = None
    min_value: float | None = None
    max_value: float | None = None
    is_default: bool = True
    updated_by: str | None = None
    updated_at: str | None = None
    # 当前生效的解析值（按 value_type 转换后），便于前端直接展示
    parsed_value: object | None = None


class ConfigListResponse(APIModel):
    """配置列表响应。"""

    items: list[ConfigItem] = Field(default_factory=list)
    categories: dict = Field(default_factory=dict)


class ConfigUpdateRequest(APIModel):
    """更新配置请求。"""

    key: str
    value: object = Field(description="新值，按配置项类型转换")


class ConfigBatchUpdateRequest(APIModel):
    """批量更新配置。"""

    items: dict = Field(description="键值对，如 {'retrieval.keyword_weight': 0.3}")


# ============================================================
#  系统概览（阶段九）
# ============================================================
class SystemOverviewResponse(APIModel):
    """系统概览（管理首页用）。"""

    users: dict = Field(default_factory=dict)
    qa: dict = Field(default_factory=dict)
    audit: dict = Field(default_factory=dict)
    knowledge: dict = Field(default_factory=dict)
    images: dict = Field(default_factory=dict)
    auth_enabled: bool = True


# ============================================================
#  知识库维护（阶段九）
# ============================================================
class DocumentItem(APIModel):
    """文献题录（列表用，不含摘要正文与文本块）。"""

    id: str
    title: str = ""
    title_en: str | None = None
    authors: str | None = None
    journal: str | None = None
    year: int | None = None
    volume: str | None = None
    issue: str | None = None
    pages: str | None = None
    doi: str | None = None
    pmid: str | None = None
    language: str | None = None
    doc_type: str | None = None
    evidence_level: str | None = None
    phase: str | None = None
    source_db: str | None = None
    chunk_count: int = 0
    status: str | None = None
    is_reviewed: bool = False
    is_active: bool = True
    has_abstract: bool = False
    batch_id: str | None = None
    reviewed_by: str | None = None
    reviewed_at: str | None = None
    review_note: str | None = None
    created_at: str | None = None


class DocumentDetail(DocumentItem):
    """文献详情（含摘要与文本块概要）。"""

    abstract: str | None = None
    mesh_terms: list | None = None
    keywords: list | None = None
    publication_types: list | None = None
    error_message: str | None = None
    chunks: list[dict] = Field(default_factory=list, description="文本块概要")
    batch: dict | None = Field(default=None, description="来源批次信息")


class DocumentListResponse(APIModel):
    """文献列表响应。"""

    items: list[DocumentItem] = Field(default_factory=list)
    total: int = 0
    limit: int = 50
    offset: int = 0


class DocumentReviewRequest(APIModel):
    """审核文献。"""

    approved: bool = Field(description="True 通过；False 驳回（自动下架）")
    note: str | None = Field(default=None, max_length=1000, description="审核意见")


class DocumentBatchReviewRequest(APIModel):
    """批量审核。"""

    document_ids: list[str] = Field(min_length=1, max_length=500)
    approved: bool
    note: str | None = Field(default=None, max_length=1000)


class DocumentActiveRequest(APIModel):
    """上架 / 下架。"""

    is_active: bool


class QualityReportResponse(APIModel):
    """知识库质量评估报告。"""

    scale: dict = Field(default_factory=dict)
    timeliness: dict = Field(default_factory=dict)
    evidence: dict = Field(default_factory=dict)
    phase: dict = Field(default_factory=dict)
    language: dict = Field(default_factory=dict)
    chinese_documents: int = 0
    issues: dict = Field(default_factory=dict)
    scores: dict = Field(default_factory=dict)
    health: float = 0.0
    suggestions: list[str] = Field(default_factory=list)


# ---- 更新批次（版本管理）----
class BatchItem(APIModel):
    """更新批次。"""

    id: str
    operation: str
    status: str
    query: str | None = None
    params: dict | None = None
    documents_created: int = 0
    documents_updated: int = 0
    documents_skipped: int = 0
    chunks_created: int = 0
    chunks_embedded: int = 0
    chunks_vectorized: int = 0
    started_at: str | None = None
    finished_at: str | None = None
    elapsed_seconds: float | None = None
    errors: list | None = None
    error_message: str | None = None
    graph_rebuilt: bool = False
    operator_name: str | None = None
    note: str | None = None
    created_at: str | None = None


class BatchListResponse(APIModel):
    """批次列表响应。"""

    items: list[BatchItem] = Field(default_factory=list)
    total: int = 0


class CollectRequest(APIModel):
    """按预定义检索式采集。"""

    mode: str = Field(
        default="default",
        description=(
            "default=主检索式(9个) / supplement=补充检索式(17个，"
            "中文文献+近三年高证据) / all=两者合并"
        ),
    )
    max_total: int | None = Field(default=None, ge=1, le=5000, description="采集总量上限")
    mindate: str | None = Field(default=None, description="起始日期 YYYY/MM/DD")
    embed: bool = Field(default=True, description="是否同时生成向量")
    batch_size: int = Field(default=32, ge=1, le=128, description="向量化批大小")


class SearchCollectRequest(APIModel):
    """按自定义检索式采集。"""

    query: str = Field(min_length=3, max_length=2000, description="PubMed 检索式")
    limit: int = Field(default=50, ge=1, le=500, description="最多条数")
    mindate: str | None = None
    embed: bool = True
    batch_size: int = Field(default=32, ge=1, le=128)


class VectorizeRequest(APIModel):
    """补齐向量。"""

    batch_size: int = Field(default=32, ge=1, le=128)


class JobProgressResponse(APIModel):
    """后台任务进度。"""

    job_id: str
    operation: str
    status: str
    stage: str = ""
    current: int = 0
    total: int = 0
    percent: float = 0.0
    message: str = ""
    elapsed_seconds: float = 0.0
    result: dict = Field(default_factory=dict)
    error: str | None = None
