"""知识库 ORM 模型 —— 对应报审表「（一）多模态知识库构建模块」。

  Document   文献/指南/病例的来源题录
  Chunk      可检索的文本块（含向量与检索索引）—— 系统的检索单元
  CaseRecord 典型病例库
  ImageAsset 术中影像知识库

设计要点：
  - Chunk 携带完整元数据（页码、章节、期别、证据等级）。
    没有元数据就无法做引用溯源，这是阶段四的前提，不可省略。
  - 中文关键词检索用 pg_trgm 三元组索引（无需分词词典，对中文有效）。
    英文文献另建 tsvector 全文索引。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.config import settings
from app.models.base import Base, TimestampMixin, UUIDMixin

EMBEDDING_DIM = settings.model.embedding_dim


# ============================================================
#  文献题录
# ============================================================
class Document(UUIDMixin, TimestampMixin, Base):
    """文献 / 指南 / 病例的来源题录。"""

    __tablename__ = "kb_document"

    # --- 标识 ---
    title: Mapped[str] = mapped_column(Text, nullable=False, comment="标题")
    title_en: Mapped[str | None] = mapped_column(Text, comment="英文标题")
    authors: Mapped[str | None] = mapped_column(Text, comment="作者，逗号分隔")
    journal: Mapped[str | None] = mapped_column(String(500), comment="期刊/出版方")
    year: Mapped[int | None] = mapped_column(Integer, comment="发表年份")
    volume: Mapped[str | None] = mapped_column(String(50))
    issue: Mapped[str | None] = mapped_column(String(50))
    pages: Mapped[str | None] = mapped_column(String(100))
    doi: Mapped[str | None] = mapped_column(String(300), comment="DOI")
    pmid: Mapped[str | None] = mapped_column(String(50), comment="PubMed ID")
    language: Mapped[str] = mapped_column(String(20), default="zh", comment="zh / en")

    # --- 分类 ---
    doc_type: Mapped[str] = mapped_column(
        String(30), default="literature", index=True,
        comment="literature/guideline/case/textbook/image",
    )
    evidence_level: Mapped[str] = mapped_column(
        String(30), default="other", index=True,
        comment="guideline/consensus/systematic/rct/cohort/case/review/other",
    )
    phase: Mapped[str] = mapped_column(
        String(20), default="general", index=True,
        comment="preop/intraop/postop/general —— 围术期分期",
    )
    source_db: Mapped[str] = mapped_column(
        String(30), default="manual", comment="pubmed/cnki/manual/guideline_official",
    )
    keywords: Mapped[list | None] = mapped_column(JSONB, comment="关键词列表")

    # --- PubMed 特需字段 ---
    mesh_terms: Mapped[list | None] = mapped_column(
        JSONB, comment="MeSH 主题词，用于领域过滤与实体抽取",
    )
    publication_types: Mapped[list | None] = mapped_column(
        JSONB, comment="PubMed 出版类型（如 Randomized Controlled Trial），证据等级推断依据",
    )

    # --- 文件 ---
    file_path: Mapped[str | None] = mapped_column(Text, comment="原文存储路径")
    file_hash: Mapped[str | None] = mapped_column(
        String(64), unique=True, comment="SHA256，用于去重",
    )
    file_size: Mapped[int | None] = mapped_column(Integer)
    page_count: Mapped[int | None] = mapped_column(Integer)

    # --- 处理状态 ---
    status: Mapped[str] = mapped_column(
        String(20), default="pending", index=True,
        comment="pending/parsing/embedded/ready/failed",
    )
    error_message: Mapped[str | None] = mapped_column(Text)
    chunk_count: Mapped[int] = mapped_column(Integer, default=0)

    # --- 审核（对应「知识库维护模块」） ---
    is_reviewed: Mapped[bool] = mapped_column(Boolean, default=False, comment="是否已人工审核")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, comment="是否上架")

    # --- 来源批次与审核记录（版本管理） ---
    # batch_id 记录该文献由哪次更新引入，支持按批次追溯与回滚。
    # 允许为空：初始采集在批次机制建立之前完成，
    # 那批文献没有 batch_id，属正常情况而非数据缺失。
    batch_id: Mapped[str | None] = mapped_column(
        ForeignKey("kb_update_batch.id", ondelete="SET NULL"), index=True,
        comment="引入该文献的更新批次；历史数据为空",
    )
    reviewed_by: Mapped[str | None] = mapped_column(String(64), comment="审核人用户名")
    reviewed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), comment="审核时间",
    )
    review_note: Mapped[str | None] = mapped_column(Text, comment="审核意见")

    abstract: Mapped[str | None] = mapped_column(Text, comment="摘要")

    chunks: Mapped[list["Chunk"]] = relationship(
        back_populates="document", cascade="all, delete-orphan",
        # 默认不预加载。
        #
        # 原来写 `lazy="selectin"`：任何 `select(Document)` 都会顺带把该文档的
        # 所有文本块（含 1024 维向量）一起拉出来。文献列表接口 `limit=200`
        # 因此加载了数万行向量，实测 2.9 秒、响应 81 KB 只是表头数据。
        # 需要块的场景（详情、引用核对）已显式 `selectinload(Document.chunks)`。
        lazy="select",
    )

    __table_args__ = (
        # 注意：不建 title 的 pg_trgm 索引 —— 实测 pg_trgm 忽略非 ASCII 字符，
        # 对中文标题无效。题录检索走 SQL LIKE/ILIKE（数据量小，无需索引）。
        Index("ix_doc_phase_type", "phase", "doc_type"),
        Index("ix_doc_year", "year"),
    )


# ============================================================
#  文本块（检索单元）
# ============================================================
class Chunk(UUIDMixin, TimestampMixin, Base):
    """可检索文本块。系统检索与引用溯源的最小单元。"""

    __tablename__ = "kb_chunk"

    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("kb_document.id", ondelete="CASCADE"), nullable=False, index=True,
    )

    # --- 内容 ---
    content: Mapped[str] = mapped_column(Text, nullable=False, comment="文本块正文")
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False, comment="在文档中的序号")

    # --- 溯源元数据（引用溯源必需，不可省略） ---
    page_start: Mapped[int | None] = mapped_column(Integer, comment="起始页")
    page_end: Mapped[int | None] = mapped_column(Integer, comment="结束页")
    section: Mapped[str | None] = mapped_column(Text, comment="章节标题")
    char_start: Mapped[int | None] = mapped_column(Integer, comment="原文起始字符偏移")
    char_end: Mapped[int | None] = mapped_column(Integer, comment="原文结束字符偏移")

    # --- 冗余的分类字段（避免检索时 JOIN，提升性能） ---
    phase: Mapped[str] = mapped_column(String(20), default="general", index=True)
    evidence_level: Mapped[str] = mapped_column(String(30), default="other", index=True)
    doc_type: Mapped[str] = mapped_column(String(30), default="literature")

    # --- 向量 ---
    embedding: Mapped[list[float] | None] = mapped_column(
        Vector(EMBEDDING_DIM), comment=f"BGE-M3 稠密向量（{EMBEDDING_DIM} 维）",
    )
    embedding_model: Mapped[str | None] = mapped_column(
        String(100), comment="生成该向量的模型标识（重建索引时校验一致性）",
    )

    # --- 关键词检索字段 ---
    # 中文 bigram 词元数组，配 GIN 索引用 && 运算符做重叠检索。
    # 为什么不用 pg_trgm：实测 show_trgm('结直肠癌') 返回空集 —— pg_trgm
    # 完全忽略非 ASCII 字符，中文检索会静默失效。详见 app/core/text_utils.py
    bigrams: Mapped[list[str] | None] = mapped_column(
        ARRAY(Text), comment="中文bigram+英文词，关键词检索倒排索引",
    )

    # --- 统计 ---
    token_count: Mapped[int | None] = mapped_column(Integer, comment="token 估算数")
    char_count: Mapped[int | None] = mapped_column(Integer)

    document: Mapped["Document"] = relationship(back_populates="chunks")

    __table_args__ = (
        UniqueConstraint("document_id", "chunk_index", name="uq_chunk_doc_index"),
        # 向量近似最近邻索引：HNSW 在召回率与构建速度上优于 IVFFlat，且无需预训练
        Index(
            "ix_chunk_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
        # 中文关键词检索：bigram 数组的 GIN 倒排索引（&& 运算符走此索引）
        Index("ix_chunk_bigrams", "bigrams", postgresql_using="gin"),
        # 域过滤 + 向量检索的复合条件
        Index("ix_chunk_phase_evid", "phase", "evidence_level"),
    )


# ============================================================
#  典型病例库 —— 报审表「（一）4 典型病例库模块」
# ============================================================
class ChunkRevision(UUIDMixin, TimestampMixin, Base):
    """Old citation text retained on replacement, purged on hard deletion."""
    __tablename__ = "kb_chunk_revision"
    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey('kb_document.id', ondelete='CASCADE'), index=True)
    revision_id: Mapped[uuid.UUID] = mapped_column(index=True)
    snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)


class CaseRecord(UUIDMixin, TimestampMixin, Base):
    """典型病例。仅用于教学演示，须为公开脱敏数据。"""

    __tablename__ = "kb_case"

    case_code: Mapped[str] = mapped_column(
        String(64), unique=True, nullable=False, comment="病例编号",
    )
    title: Mapped[str] = mapped_column(Text, nullable=False)
    age: Mapped[int | None] = mapped_column(Integer)
    sex: Mapped[str | None] = mapped_column(String(10), comment="M/F")

    diagnosis: Mapped[str | None] = mapped_column(Text, comment="诊断")
    surgery: Mapped[str | None] = mapped_column(Text, comment="术式")
    phase: Mapped[str] = mapped_column(String(20), default="general", index=True)

    history: Mapped[str | None] = mapped_column(Text, comment="病史")
    findings: Mapped[str | None] = mapped_column(Text, comment="检查所见")
    operative_note: Mapped[str | None] = mapped_column(Text, comment="手术记录")
    prognosis: Mapped[str | None] = mapped_column(Text, comment="预后/随访")

    complications: Mapped[list | None] = mapped_column(JSONB, comment="并发症列表")
    tags: Mapped[list | None] = mapped_column(JSONB, comment="标签，用于相似病例匹配")
    image_ids: Mapped[list | None] = mapped_column(JSONB, comment="关联的公开影像 ID 列表")

    source_title: Mapped[str | None] = mapped_column(Text, comment="公开病例原始文献题名")
    source_url: Mapped[str | None] = mapped_column(Text, comment="原始病例全文链接")
    source_doi: Mapped[str | None] = mapped_column(String(200))
    source_license: Mapped[str | None] = mapped_column(String(200), comment="原始资料许可及版本")
    source_note: Mapped[str | None] = mapped_column(Text, comment="出处章节、整理依据与未报告字段说明")
    prepared_by: Mapped[str | None] = mapped_column(String(100), comment="整理账号")
    reviewed_by: Mapped[str | None] = mapped_column(String(100), comment="最近审核发布账号")
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    review_note: Mapped[str | None] = mapped_column(Text, comment="最近审核意见；历次记录见审计日志")

    # 用于相似病例匹配的语义向量（由病史+诊断+术式拼接后编码）
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIM))

    is_public: Mapped[bool] = mapped_column(
        Boolean, default=False, comment="是否公开脱敏数据（未确认不允许使用）",
    )

    __table_args__ = (
        Index("ix_case_embedding_hnsw", "embedding", postgresql_using="hnsw",
              postgresql_with={"m": 16, "ef_construction": 64},
              postgresql_ops={"embedding": "vector_cosine_ops"}),
        Index("ix_case_tags", "tags", postgresql_using="gin"),
    )


# ============================================================
#  术中影像知识库 —— 报审表「（一）3 术中影像知识库模块」
# ============================================================
class ImageAsset(UUIDMixin, TimestampMixin, Base):
    """术中影像资料。仅存放公开样例影像，不接入真实患者数据。"""

    __tablename__ = "kb_image"

    filename: Mapped[str] = mapped_column(String(500), nullable=False)
    storage_path: Mapped[str] = mapped_column(Text, nullable=False, comment="MinIO 对象键")
    thumbnail_path: Mapped[str | None] = mapped_column(Text)
    file_hash: Mapped[str | None] = mapped_column(String(64), unique=True)
    mime_type: Mapped[str | None] = mapped_column(String(100))
    file_size: Mapped[int | None] = mapped_column(Integer)
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)

    # --- 分类与说明 ---
    title: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text, comment="人工标注的说明")
    surgery_type: Mapped[str | None] = mapped_column(String(200), comment="所属术式")
    anatomy_site: Mapped[str | None] = mapped_column(String(200), comment="解剖部位")
    phase: Mapped[str] = mapped_column(String(20), default="intraop", index=True)
    tags: Mapped[list | None] = mapped_column(JSONB)

    # --- 云端多模态模型的解读结果（缓存，避免重复调用） ---
    vlm_analysis: Mapped[str | None] = mapped_column(Text, comment="GLM-4V 解读文本")
    vlm_model: Mapped[str | None] = mapped_column(String(100))
    vlm_analyzed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # 综合解读（检索证据 + LLM 结论）的完整快照。
    #
    # 为什么需要持久化：`vlm_analysis` 只存视觉模型的客观描述，
    # 而用户真正要看的是「结合文献证据的辅助解读 + 引用」。
    # 早期只把它放在前端组件内存里，切走或刷新就丢失，
    # 再次打开又得重跑 15~20 秒的检索与生成。
    # 这里存 JSONB 快照（interpretation / citations / retrieval_query /
    # evidence_count / 核验结果 / 耗时），取回即可原样渲染。
    vlm_interpretation: Mapped[dict | None] = mapped_column(
        JSONB, comment="综合解读快照（JSON）"
    )

    # --- 影像特征向量：512 维颜色/纹理基线，支持相似样例检索 ---
    feature_vector: Mapped[list[float] | None] = mapped_column(
        Vector(512), comment="影像相似度检索用（RGB 直方图基线，可替换为视觉编码器）",
    )
    feature_model: Mapped[str | None] = mapped_column(String(100), comment='Feature algorithm/version; never mix encoders')
    feature_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    source: Mapped[str | None] = mapped_column(String(300), comment="影像来源（须公开可引用）")
    is_public: Mapped[bool] = mapped_column(Boolean, default=False)

    __table_args__ = (
        Index("ix_image_phase_surgery", "phase", "surgery_type"),
        Index(
            "ix_image_feature_hnsw",
            "feature_vector",
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_ops={"feature_vector": "vector_cosine_ops"},
        ),
    )
