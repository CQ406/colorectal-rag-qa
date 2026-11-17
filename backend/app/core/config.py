"""应用配置管理。

所有配置项集中于此，通过 pydantic-settings 从环境变量 / .env 读取。
分层设计：每个外部依赖一个独立的 Settings 类，便于单独测试与替换。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict

# 项目根目录（backend/ 的上一级）
PROJECT_ROOT = Path(__file__).resolve().parents[3]
BACKEND_ROOT = Path(__file__).resolve().parents[2]


class AppSettings(BaseSettings):
    """应用基础配置。"""

    model_config = SettingsConfigDict(
        env_file=(PROJECT_ROOT / ".env", BACKEND_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_name: str = "结直肠手术围术期多模态知识库智能问答系统"
    app_env: Literal["dev", "test", "prod"] = "dev"
    debug: bool = True
    api_prefix: str = "/api"
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_dev(self) -> bool:
        return self.app_env == "dev"


class PostgresSettings(BaseSettings):
    """PostgreSQL + pgvector 配置。"""

    model_config = SettingsConfigDict(
        env_file=(PROJECT_ROOT / ".env", BACKEND_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_user: str = "rag"
    # ⚠ 本地开发默认值，部署前务必用 .env 的 POSTGRES_PASSWORD 覆盖
    postgres_password: str = "rag_dev_pwd"
    postgres_db: str = "colorectal_rag"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def dsn(self) -> str:
        """同步 DSN —— 供 Alembic 迁移使用（psycopg3 同步驱动）。"""
        from urllib.parse import quote_plus

        pwd = quote_plus(self.postgres_password)
        return (
            f"postgresql+psycopg://{self.postgres_user}:{pwd}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )


class Neo4jSettings(BaseSettings):
    """Neo4j 知识图谱配置。"""

    model_config = SettingsConfigDict(
        env_file=(PROJECT_ROOT / ".env", BACKEND_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = "neo4j_dev_pwd"


class RedisSettings(BaseSettings):
    """Redis 配置。"""

    model_config = SettingsConfigDict(
        env_file=(PROJECT_ROOT / ".env", BACKEND_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    redis_host: str = "localhost"
    redis_port: int = 6379
    redis_db: int = 0

    @computed_field  # type: ignore[prop-decorator]
    @property
    def url(self) -> str:
        return f"redis://{self.redis_host}:{self.redis_port}/{self.redis_db}"


class MinioSettings(BaseSettings):
    """MinIO 对象存储配置。"""

    model_config = SettingsConfigDict(
        env_file=(PROJECT_ROOT / ".env", BACKEND_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    minio_endpoint: str = "localhost:9000"
    minio_user: str = "minioadmin"
    minio_password: str = "minio_dev_pwd"
    minio_secure: bool = False
    minio_bucket_docs: str = "literature"
    minio_bucket_images: str = "images"


class ModelSettings(BaseSettings):
    """检索侧模型配置（BGE-M3 / reranker，跑在宿主机 GPU）。"""

    model_config = SettingsConfigDict(
        env_file=(PROJECT_ROOT / ".env", BACKEND_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # 注意：必须是本地目录路径。传 'BAAI/bge-m3' 会触发 snapshot_download
    # 拉取仓库全部文件，而仓库中 imgs/.DS_Store 被镜像站拒绝(403)，导致加载失败。
    embedding_model_path: str = r"D:\models\bge-m3"
    reranker_model_path: str = r"D:\models\bge-reranker-v2-m3"
    embedding_device: str = "cuda"
    embedding_dim: int = 1024
    embedding_batch_size: int = 32
    embedding_max_length: int = 512
    # 显存紧张时可设为 true，把检索模型放 CPU（慢约 20~35 倍）
    force_cpu: bool = False


class LLMSettings(BaseSettings):
    """大语言模型配置（答案生成）。"""

    model_config = SettingsConfigDict(
        env_file=(PROJECT_ROOT / ".env", BACKEND_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    llm_provider: str = "deepseek"
    llm_base_url: str = "https://api.deepseek.com/v1"
    llm_api_key: str = ""
    # 注意：DeepSeek 当前可用模型为 deepseek-flash / deepseek-v4-pro（可通过
    # GET /models 查询）。写 deepseek-chat 等旧名会**静默回退**到 deepseek-flash。
    #
    # 实测（真实 RAG 提示词 3108 字符 / 5 条证据，各 2 次采样）：
    #   deepseek-flash   首字 2.02/3.16s  总 5.48/6.72s   内容 689/727 块
    #   deepseek-v4-pro  首字 13.6/8.85s  总 15.5/9.98s   内容 236/138 块
    # flash 在质量（结构更完整、证据边界声明更充分）与速度上均优于 v4-pro，
    # 故默认选用。详见 docs/模型选型与延迟优化决策.md
    llm_model: str = "deepseek-flash"
    # 保留推理内容（reasoning_content）供审计；医学场景可复盘模型推理过程
    llm_keep_reasoning: bool = True
    llm_temperature: float = 0.2
    # 关键：推理模型的推理 token 与正式回答共用 max_tokens。
    # 实测数据（deepseek-flash）：
    #   =512  时 100% 返回空答案
    #   =1024 时 4 次中出现 1 次空答案
    #   =4096 时 复杂医学问题（送 5 条证据）仍偶发耗尽（推理内容可达 14000 字符）
    #   =8192 时 稳定
    # 故取 8192。空答案会被客户端显式识别并重试，不会静默返回空白。
    llm_max_tokens: int = 8192
    llm_timeout: int = 180

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_configured(self) -> bool:
        """密钥是否已填写（未填写时相关接口返回明确提示而非报错）。"""
        return bool(self.llm_api_key) and "在此填写" not in self.llm_api_key


class VLMSettings(BaseSettings):
    """多模态大模型配置（影像解读）。"""

    model_config = SettingsConfigDict(
        env_file=(PROJECT_ROOT / ".env", BACKEND_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    vlm_base_url: str = "https://open.bigmodel.cn/api/paas/v4"
    vlm_api_key: str = ""
    vlm_model: str = "glm-4v-flash"
    vlm_timeout: int = 120

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_configured(self) -> bool:
        return bool(self.vlm_api_key) and "在此填写" not in self.vlm_api_key


class RetrievalSettings(BaseSettings):
    """检索策略配置。"""

    model_config = SettingsConfigDict(
        env_file=(PROJECT_ROOT / ".env", BACKEND_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    retrieve_top_k_vector: int = 50
    retrieve_top_k_keyword: int = 50
    retrieve_top_k_graph: int = 20
    # RRF（倒数排名融合）常数，经验值 60
    rrf_k: int = 60
    # RRF 融合后保留的候选数（送入重排的池子）
    rrf_limit: int = 80
    # 送入重排的候选上限。重排是候选数的线性开销（实测约 105 对/秒），
    # 该值直接决定检索延迟上限：80 条约 0.8 秒
    rerank_candidate_limit: int = 80
    rerank_top_n: int = 5
    enable_graph_retrieval: bool = True
    # 证据等级加权强度：0 关闭，1 完全按证据等级排序
    evidence_boost_strength: float = 0.15
    # 同一文献最多贡献的文本块数（避免单一来源占满结果）
    max_chunks_per_document: int = 3
    # 关键词召回的最小 bigram 重叠比例。低于此值的候选被丢弃。
    # 必要性：中文文本普遍共享高频词元，不过滤会让长查询召回大量无关文档，
    # 并在 RRF 中被误当作「多路共识」提权。详见 search_repo.search_by_keyword
    # 设为 0 可完全关闭关键词路（用于消融实验对比）
    min_keyword_overlap: float = 0.3
    # 各召回路径在 RRF 融合中的权重。
    #
    # 默认值依据阶段十实测（195 中文 + 189 英文口语化临床问题）：
    #   仅关键词 R@5 = 0.000(中) / 0.005(英) —— 几乎完全失效
    #   关键词权重 0.7 时，融合反而**低于**纯向量（英文 R@5 0.487→0.466）
    # 故关键词权重降到 0.3，仅保留其「精确术语补充命中」的作用。
    # 若实测发现其完全无贡献，可将 keyword_weight 设为 0 直接关闭该路。
    vector_weight: float = 1.0
    keyword_weight: float = 0.3
    # 图谱权重：2026-11-08 由 0.6 下调到 0.2。
    #
    # 依据：12 题冻结集复核（docs/检索评测与复核报告.md）显示，图谱当前
    # 只覆盖 23/3772 个块（0.6%），加入后 MRR 从 0.542 掉到 0.340。
    # 权重 0.6 高于关键词（0.3），会把少量陈旧链接的候选抬到更相关的块之上。
    # 下调后图谱仍可用（use_graph 显式开启），但不再主导排序；
    # 若扩图后覆盖率显著提升，应在同一冻结集上重测再决定是否回调。
    graph_weight: float = 0.2


class PubMedSettings(BaseSettings):
    """PubMed 文献采集配置。"""

    model_config = SettingsConfigDict(
        env_file=(PROJECT_ROOT / ".env", BACKEND_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    pubmed_email: str = "your_email@example.com"
    pubmed_api_key: str = ""
    pubmed_tool: str = "colorectal-rag-thesis"
    pubmed_max_results: int = 500

    @computed_field  # type: ignore[prop-decorator]
    @property
    def eutils_base(self) -> str:
        return "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"


class CelerySettings(BaseSettings):
    """后台任务配置。"""

    model_config = SettingsConfigDict(
        env_file=(PROJECT_ROOT / ".env", BACKEND_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    celery_broker_url: str = "redis://localhost:6379/1"
    celery_result_backend: str = "redis://localhost:6379/2"


class AuthSettings(BaseSettings):
    """认证与授权配置（阶段九）。"""

    # ⚠️ env_file 与 env_prefix 必须**同时**存在，缺一不可。
    #
    # 实测对照（同一 .env 文件，仅改配置）：
    #   env_file 有 + env_prefix 有  -> 读到 64 字符  ✓
    #   env_file 有 + env_prefix 无  -> 读到 0 字符   ✗
    #   env_file 无 + env_prefix 有  -> 读到 0 字符   ✗
    #
    # 即：env_prefix 让 pydantic 去找 AUTH_SECRET_KEY 这样的键名，
    # env_file 告诉它去哪里找；少了 env_file 就只在系统环境变量里找。
    model_config = SettingsConfigDict(
        env_file=(PROJECT_ROOT / ".env", BACKEND_ROOT / ".env"),
        env_file_encoding="utf-8",
        env_prefix="AUTH_",
        extra="ignore",
    )

    # 令牌签名密钥。未配置时后端启动会生成临时密钥并告警，
    # 但重启后所有登录状态失效 —— 生产环境必须显式配置。
    # 生成方式：python -c "import secrets;print(secrets.token_urlsafe(48))"
    secret_key: str = ""

    # 签名算法。HS256 为对称算法，单实例部署足够；
    # 若将来需要第三方验证令牌，应改为 RS256 并配置公私钥。
    algorithm: str = "HS256"

    # 令牌有效期（分钟）。默认 8 小时，覆盖一个工作班次。
    # 有效期越长越方便，但令牌泄露的风险窗口也越大。
    token_expire_minutes: int = 480

    # 是否启用认证。设为 False 时后端不做鉴权，便于脚本与评测直接调用接口。
    #
    # ⚠️ 默认 True：认证必须显式关闭，而非默认关闭。
    # 阶段十的评测脚本依赖 /api/qa 等接口的无状态调用，
    # 这些接口设计为**不需要认证**（见 permissions.PUBLIC_PATHS），
    # 因此不需要为此关闭认证。
    enabled: bool = True

    # 连续登录失败达到此次数后锁定账号
    max_failed_attempts: int = 5
    # 锁定时长（分钟）
    lockout_minutes: int = 15

    # 初始管理员账号（首次初始化数据库时创建）
    admin_username: str = "admin"
    admin_password: str = "Admin@12345"

class Settings(BaseSettings):
    """聚合配置入口。

    用法::

        from app.core.config import settings
        settings.postgres.dsn
        settings.app.app_name
    """

    model_config = SettingsConfigDict(
        env_file=(PROJECT_ROOT / ".env", BACKEND_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app: AppSettings = Field(default_factory=AppSettings)
    postgres: PostgresSettings = Field(default_factory=PostgresSettings)
    neo4j: Neo4jSettings = Field(default_factory=Neo4jSettings)
    redis: RedisSettings = Field(default_factory=RedisSettings)
    minio: MinioSettings = Field(default_factory=MinioSettings)
    model: ModelSettings = Field(default_factory=ModelSettings)
    llm: LLMSettings = Field(default_factory=LLMSettings)
    vlm: VLMSettings = Field(default_factory=VLMSettings)
    retrieval: RetrievalSettings = Field(default_factory=RetrievalSettings)
    pubmed: PubMedSettings = Field(default_factory=PubMedSettings)
    celery: CelerySettings = Field(default_factory=CelerySettings)
    auth: AuthSettings = Field(default_factory=AuthSettings)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """获取全局配置单例（带缓存，避免重复解析 .env）。"""
    return Settings()


settings = get_settings()
