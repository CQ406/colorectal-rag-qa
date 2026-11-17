"""知识库维护数据访问（阶段九「知识库维护模块」）。

对应报审表四项要求：知识的**审核、更新、版本管理和质量评估**。

  kb_repo.list_documents      文献列表（支持多维筛选）
  kb_repo.review_document     审核（通过 / 驳回）
  kb_repo.set_active          上架 / 下架（软删除）
  kb_repo.delete_document     硬删除（连带文本块，谨慎使用）
  kb_repo.quality_report      质量评估
  batch_repo.*                更新批次（版本管理）

关于「软删除」与「硬删除」的区分
--------------------------------
默认提供**软删除**（置 `is_active=False`）：
  - 删除文献会让历史问答的引用失效，破坏可追溯性
  - 误删的代价不对称：多留一篇只是占用少量空间，删错一篇则无法还原
因此界面上把「下架」作为主操作，「彻底删除」作为需要二次确认的次要操作。

检索链路只使用 `is_active=True` 的文献
（见 search_repo._base_query），所以下架即代表不再被检索到。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.core.logging import get_logger
from app.models.batch import KbUpdateBatch
from app.models.knowledge import Chunk, Document

logger = get_logger("repositories.kb")


# ======================================================================
#  文献列表
# ======================================================================
def list_documents(
    db: Session,
    *,
    keyword: str | None = None,
    phase: str | None = None,
    evidence_level: str | None = None,
    language: str | None = None,
    year_from: int | None = None,
    year_to: int | None = None,
    is_reviewed: bool | None = None,
    is_active: bool | None = None,
    batch_id: str | None = None,
    has_abstract: bool | None = None,
    limit: int = 50,
    offset: int = 0,
    order_by: str = "created_desc",
) -> tuple[list[Document], int]:
    """分页查询文献题录。

    不加载文本块（`chunks` 关系是 selectin，会带来大量数据），
    列表页只需要题录信息。
    """
    stmt = select(Document)
    count_stmt = select(func.count()).select_from(Document)

    conditions = []
    if keyword:
        pattern = f"%{keyword.strip()}%"
        conditions.append(
            or_(
                Document.title.ilike(pattern),
                Document.title_en.ilike(pattern),
                Document.authors.ilike(pattern),
                Document.journal.ilike(pattern),
                Document.doi.ilike(pattern),
                Document.pmid.ilike(pattern),
            )
        )
    if phase:
        conditions.append(Document.phase == phase)
    if evidence_level:
        conditions.append(Document.evidence_level == evidence_level)
    if language:
        conditions.append(Document.language == language)
    if year_from is not None:
        conditions.append(Document.year >= year_from)
    if year_to is not None:
        conditions.append(Document.year <= year_to)
    if is_reviewed is not None:
        conditions.append(Document.is_reviewed.is_(is_reviewed))
    if is_active is not None:
        conditions.append(Document.is_active.is_(is_active))
    if batch_id:
        conditions.append(Document.batch_id == batch_id)
    if has_abstract is not None:
        if has_abstract:
            conditions.append(Document.abstract.isnot(None))
        else:
            conditions.append(Document.abstract.is_(None))

    if conditions:
        stmt = stmt.where(*conditions)
        count_stmt = count_stmt.where(*conditions)

    total = db.execute(count_stmt).scalar_one()

    order_map = {
        "created_desc": Document.created_at.desc(),
        "created_asc": Document.created_at.asc(),
        "year_desc": Document.year.desc().nullslast(),
        "year_asc": Document.year.asc().nullsfirst(),
        "title": Document.title.asc(),
    }
    stmt = stmt.order_by(order_map.get(order_by, Document.created_at.desc()))
    stmt = stmt.limit(limit).offset(offset)

    return list(db.execute(stmt).scalars().all()), total


def get_document(db: Session, doc_id: str) -> Document | None:
    """取单篇文献（含文本块，用于详情与引用核对）。"""
    stmt = (
        select(Document)
        .options(selectinload(Document.chunks))
        .where(Document.id == doc_id)
    )
    return db.execute(stmt).scalar_one_or_none()


# ======================================================================
#  审核
# ======================================================================
def review_document(
    db: Session,
    doc: Document,
    *,
    approved: bool,
    reviewer: str | None,
    note: str | None = None,
) -> Document:
    """审核文献。

    Args:
        approved: True 通过（保留上架）；False 驳回（自动下架）

    **驳回时自动下架**是刻意的设计：
      审核的目的是过滤掉不适用语料。若驳回后文献仍在检索池中，
      审核就失去了作用。因此驳回与下架绑定。
    """
    doc.is_reviewed = True
    doc.reviewed_by = reviewer
    doc.reviewed_at = datetime.now(timezone.utc)
    doc.review_note = note
    if not approved:
        doc.is_active = False
    db.flush()
    logger.info(
        "文献审核%s: %s（审核人 %s）",
        "通过" if approved else "驳回",
        (doc.title or "")[:50], reviewer,
    )
    return doc


def batch_review(
    db: Session,
    doc_ids: list[str],
    *,
    approved: bool,
    reviewer: str | None,
) -> int:
    """批量审核。返回实际处理的条数。"""
    if not doc_ids:
        return 0
    now = datetime.now(timezone.utc)
    docs = db.execute(
        select(Document).where(Document.id.in_(doc_ids))
    ).scalars().all()

    for doc in docs:
        doc.is_reviewed = True
        doc.reviewed_by = reviewer
        doc.reviewed_at = now
        if not approved:
            doc.is_active = False
    db.flush()
    logger.info("批量审核 %d 篇（%s）", len(docs), "通过" if approved else "驳回")
    return len(docs)


def set_active(db: Session, doc: Document, is_active: bool) -> Document:
    """上架 / 下架（软删除）。"""
    doc.is_active = is_active
    db.flush()
    logger.info("文献%s: %s", "上架" if is_active else "下架", (doc.title or "")[:50])
    return doc


def delete_document(db: Session, doc: Document) -> int:
    """彻底删除文献及其文本块。返回删除的文本块数。

    ⚠️ 不可恢复。文本块通过 cascade 一并删除
    （模型上已声明 cascade="all, delete-orphan"）。

    注意：图数据库中由该文献抽取的实体**不会被自动删除**，
    图谱需要重建才能反映删除（见 batch_repo 的说明）。
    """
    chunk_count = db.execute(
        select(func.count()).select_from(Chunk).where(Chunk.document_id == doc.id)
    ).scalar_one()
    title = (doc.title or "")[:50]
    db.delete(doc)
    db.flush()
    logger.warning("已彻底删除文献: %s（连带 %d 个文本块）", title, chunk_count)
    return chunk_count


# ======================================================================
#  质量评估
# ======================================================================
def quality_report(db: Session) -> dict:
    """知识库质量评估。

    评估维度与对应的维护建议：

      1. 规模           文献数、文本块数、向量化完成率
      2. 时效性         年份分布、近 3 年占比、最早年份
      3. 证据结构       各证据等级占比（指南/RCT 占比过低说明证据强度不足）
      4. 分期覆盖       各围术期分期的文本块数（某期过少说明覆盖不全）
      5. 语种结构       中文文献占比（中文提问场景下中文语料过少会限制效果）
      6. 审核进度       已审核比例
      7. 异常项         无摘要、无向量、未完成状态、缺关键元数据的文献

    第 7 项是**可执行的检查清单**：列出具体哪些文献有问题，
    管理员可以直接定位并处理，而不是只看到一个笼统的分数。
    """
    now = datetime.now(timezone.utc)
    recent_cutoff = now.year - 2   # 近 3 年（含今年）

    total_docs = db.execute(select(func.count()).select_from(Document)).scalar_one()
    active_docs = db.execute(
        select(func.count()).select_from(Document).where(Document.is_active.is_(True))
    ).scalar_one()
    reviewed_docs = db.execute(
        select(func.count()).select_from(Document).where(Document.is_reviewed.is_(True))
    ).scalar_one()

    total_chunks = db.execute(select(func.count()).select_from(Chunk)).scalar_one()
    embedded_chunks = db.execute(
        select(func.count()).select_from(Chunk).where(Chunk.embedding.is_not(None))
    ).scalar_one()

    # --- 时效性 ---
    year_rows = db.execute(
        select(Document.year, func.count())
        .where(Document.year.is_not(None))
        .group_by(Document.year)
        .order_by(Document.year)
    ).all()
    year_dist = {int(y): int(c) for y, c in year_rows}
    recent_docs = sum(c for y, c in year_dist.items() if y >= recent_cutoff)
    min_year = min(year_dist) if year_dist else None
    max_year = max(year_dist) if year_dist else None

    # --- 证据等级 ---
    ev_rows = db.execute(
        select(Document.evidence_level, func.count()).group_by(Document.evidence_level)
    ).all()
    evidence_dist = {(k or "other"): int(v) for k, v in ev_rows}

    # --- 分期（按文本块，反映实际检索覆盖）---
    phase_rows = db.execute(
        select(Document.phase, func.count()).group_by(Document.phase)
    ).all()
    phase_dist = {(k or "general"): int(v) for k, v in phase_rows}

    # --- 语种 ---
    lang_rows = db.execute(
        select(Document.language, func.count()).group_by(Document.language)
    ).all()
    lang_dist = {(k or "unknown"): int(v) for k, v in lang_rows}
    chinese_docs = lang_dist.get("chi", 0) + lang_dist.get("zh", 0)

    # --- 异常项（可执行清单）---
    no_abstract = db.execute(
        select(func.count()).select_from(Document).where(Document.abstract.is_(None))
    ).scalar_one()

    # 有未向量化文本块的文献
    unembedded_doc_ids = db.execute(
        select(Document.id)
        .join(Chunk, Chunk.document_id == Document.id)
        .where(Chunk.embedding.is_(None))
        .group_by(Document.id)
        .limit(50)
    ).scalars().all()

    # 状态异常的文献
    bad_status = list(
        db.execute(
            select(Document.id, Document.title, Document.status)
            .where(Document.status.notin_(["embedded", "ready"]))
            .limit(50)
        ).all()
    )

    # 缺关键元数据的文献（无 DOI 且无 PMID，无法溯源）
    no_identifier = db.execute(
        select(func.count()).select_from(Document).where(
            Document.doi.is_(None), Document.pmid.is_(None)
        )
    ).scalar_one()

    # --- 综合健康度评分 ---
    # 说明：这是一个**便于展示的合成指标**，权重为经验设定，
    # 不代表临床证据质量。论文中引用时应说明其定义而非当作绝对标准。
    scores: dict[str, float] = {}

    scores["向量化完成率"] = (
        round(embedded_chunks / total_chunks, 4) if total_chunks else 1.0
    )
    scores["审核覆盖率"] = (
        round(reviewed_docs / total_docs, 4) if total_docs else 1.0
    )
    scores["近三年文献占比"] = (
        round(recent_docs / total_docs, 4) if total_docs else 0.0
    )
    # 高等级证据（指南 + 系统评价 + RCT）占比
    high_evidence = (
        evidence_dist.get("guideline", 0)
        + evidence_dist.get("systematic", 0)
        + evidence_dist.get("rct", 0)
    )
    scores["高等级证据占比"] = (
        round(high_evidence / total_docs, 4) if total_docs else 0.0
    )
    scores["摘要完整率"] = (
        round((total_docs - no_abstract) / total_docs, 4) if total_docs else 1.0
    )
    scores["溯源完整率"] = (
        round((total_docs - no_identifier) / total_docs, 4) if total_docs else 1.0
    )

    # 健康度 = 各项均值（仅用于快速判断，不参与任何检索决策）
    health = round(sum(scores.values()) / len(scores), 4) if scores else 0.0

    # --- 维护建议（根据评估结果自动生成）---
    suggestions: list[str] = []
    if scores["向量化完成率"] < 1.0:
        suggestions.append(
            f"有 {total_chunks - embedded_chunks} 个文本块未生成向量，"
            "这些内容无法被检索到，建议执行「补齐向量」"
        )
    if scores["审核覆盖率"] < 0.5:
        suggestions.append(
            f"仅 {reviewed_docs}/{total_docs} 篇完成审核，"
            "建议在「文献管理」中筛出未审核文献逐批处理"
        )
    if scores["近三年文献占比"] < 0.3:
        suggestions.append(
            f"近三年文献占 {scores['近三年文献占比']*100:.1f}%，"
            "时效性偏低，建议补充近期文献"
        )
    if scores["高等级证据占比"] < 0.4:
        suggestions.append(
            f"指南与系统评价、RCT 合计占 {scores['高等级证据占比']*100:.1f}%，"
            "高等级证据偏少，可能影响结论可靠性"
        )
    if chinese_docs < 20:
        suggestions.append(
            f"中文文献仅 {chinese_docs} 篇。系统主要面向中文提问，"
            "中文语料偏少会限制跨语言检索效果"
        )
    if no_abstract:
        suggestions.append(f"有 {no_abstract} 篇文献缺少摘要，其内容无法被切分检索")
    if no_identifier:
        suggestions.append(
            f"有 {no_identifier} 篇文献既无 DOI 也无 PMID，无法核验来源"
        )

    return {
        "scale": {
            "documents": total_docs,
            "documents_active": active_docs,
            "documents_inactive": total_docs - active_docs,
            "documents_reviewed": reviewed_docs,
            "chunks": total_chunks,
            "chunks_embedded": embedded_chunks,
            "chunks_unembedded": total_chunks - embedded_chunks,
        },
        "timeliness": {
            "year_range": [min_year, max_year],
            "year_distribution": year_dist,
            "recent_cutoff_year": recent_cutoff,
            "recent_documents": recent_docs,
            "recent_ratio": scores["近三年文献占比"],
        },
        "evidence": evidence_dist,
        "phase": phase_dist,
        "language": lang_dist,
        "chinese_documents": chinese_docs,
        "issues": {
            "no_abstract": no_abstract,
            "no_identifier": no_identifier,
            "unembedded_document_ids": [str(x) for x in unembedded_doc_ids],
            "bad_status": [
                {"id": str(i), "title": (t or "")[:60], "status": s}
                for i, t, s in bad_status
            ],
        },
        "scores": scores,
        "health": health,
        "suggestions": suggestions,
    }


# ======================================================================
#  更新批次（版本管理）
# ======================================================================
def create_batch(
    db: Session,
    *,
    operation: str,
    query: str | None = None,
    params: dict | None = None,
    operator_id: str | None = None,
    operator_name: str | None = None,
    note: str | None = None,
) -> KbUpdateBatch:
    """创建一条更新批次记录（状态 pending）。"""
    from app.models.batch import BATCH_PENDING

    batch = KbUpdateBatch(
        operation=operation,
        status=BATCH_PENDING,
        query=query,
        params=params,
        operator_id=operator_id,
        operator_name=operator_name,
        note=note,
    )
    db.add(batch)
    db.flush()
    logger.info("已创建更新批次 %s（%s）", str(batch.id)[:8], operation)
    return batch


def update_batch(db: Session, batch: KbUpdateBatch, **fields) -> KbUpdateBatch:
    """更新批次字段。"""
    for key, value in fields.items():
        if hasattr(batch, key):
            setattr(batch, key, value)
    db.flush()
    return batch


def finish_batch(
    db: Session,
    batch: KbUpdateBatch,
    *,
    status: str,
    stats: dict | None = None,
    errors: list[str] | None = None,
    error_message: str | None = None,
) -> KbUpdateBatch:
    """结束批次，写入结果与耗时。"""
    now = datetime.now(timezone.utc)
    batch.status = status
    batch.finished_at = now
    if batch.started_at is not None:
        batch.elapsed_seconds = (now - batch.started_at).total_seconds()
    if stats:
        for key in (
            "documents_created", "documents_updated", "documents_skipped",
            "chunks_created", "chunks_embedded", "chunks_vectorized",
        ):
            if key in stats:
                setattr(batch, key, stats[key])
    # 截断错误列表：一个坏批次可能产生上千条错误，
    # 全量写入会让单个 JSONB 字段膨胀到不可用
    if errors:
        batch.errors = errors[:20]
    batch.error_message = error_message
    db.flush()
    logger.info(
        "批次 %s 结束: %s（耗时 %.1f 秒）",
        str(batch.id)[:8], status, batch.elapsed_seconds or 0,
    )
    return batch


def list_batches(
    db: Session,
    *,
    operation: str | None = None,
    status: str | None = None,
    limit: int = 30,
    offset: int = 0,
) -> tuple[list[KbUpdateBatch], int]:
    """分页查询更新批次历史。"""
    stmt = select(KbUpdateBatch)
    count_stmt = select(func.count()).select_from(KbUpdateBatch)

    conditions = []
    if operation:
        conditions.append(KbUpdateBatch.operation == operation)
    if status:
        conditions.append(KbUpdateBatch.status == status)
    if conditions:
        stmt = stmt.where(*conditions)
        count_stmt = count_stmt.where(*conditions)

    total = db.execute(count_stmt).scalar_one()
    stmt = stmt.order_by(KbUpdateBatch.created_at.desc()).limit(limit).offset(offset)
    return list(db.execute(stmt).scalars().all()), total


def get_batch(db: Session, batch_id: str) -> KbUpdateBatch | None:
    return db.get(KbUpdateBatch, batch_id)


def batches_for_document(db: Session, doc: Document) -> KbUpdateBatch | None:
    """取该文献所属批次（用于详情页展示来源）。"""
    if not doc.batch_id:
        return None
    return db.get(KbUpdateBatch, doc.batch_id)


def mark_graph_stale(db: Session) -> None:
    """标记最近成功的批次为「图谱待重建」。

    调用时机：任何新增文献的操作完成后。
    新入库文献不会自动进入图谱，图谱检索路径看不到它们，
    因此需要在界面上持续提示，直到管理员重建图谱。
    """
    batch = db.execute(
        select(KbUpdateBatch)
        .where(KbUpdateBatch.status == "success")
        .order_by(KbUpdateBatch.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    if batch is not None:
        batch.graph_rebuilt = False
        db.flush()
