"""检索数据访问层。

把 SQL 集中在此处，服务层只关心业务逻辑。
pgvector 运算符说明：
    <=>  余弦距离（0 表示完全相同），相似度 = 1 - 距离
    <->  欧氏距离
    <#>  负内积
BGE-M3 向量已归一化，故用余弦距离；HNSW 索引按 vector_cosine_ops 建立。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import Select, func, select, bindparam, text, Text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Session, joinedload

from app.core.constants import PerioperativePhase
from app.core.text_utils import tokenize_query
from app.models.knowledge import Chunk, Document


@dataclass
class ScoredChunk:
    """带分数的检索结果（服务层内部使用）。"""

    chunk_id: uuid.UUID
    document_id: uuid.UUID
    content: str
    score: float
    phase: str
    evidence_level: str
    page_start: int | None
    page_end: int | None
    section: str | None
    doc_title: str
    doc_year: int | None
    doc_journal: str | None
    doc_authors: str | None
    vector_score: float | None = None
    rerank_score: float | None = None
    keyword_hits: int | None = None
    # RRF 融合后的排名信息（阶段三使用）
    rrf_score: float | None = None


def _base_query() -> Select:
    """构造带文献题录的检索查询。

    使用 joinedload 一次取出 Document，避免 N+1 查询。
    """
    return (
        select(Chunk)
        .options(joinedload(Chunk.document))
        .join(Document, Chunk.document_id == Document.id)
        .where(Document.is_active.is_(True))
    )


def _to_scored(chunk: Chunk, score: float) -> ScoredChunk:
    """把 ORM 对象转成带分数的数据类。"""
    doc = chunk.document
    return ScoredChunk(
        chunk_id=chunk.id,
        document_id=chunk.document_id,
        content=chunk.content,
        score=score,
        phase=chunk.phase,
        evidence_level=chunk.evidence_level,
        page_start=chunk.page_start,
        page_end=chunk.page_end,
        section=chunk.section,
        doc_title=doc.title if doc else "",
        doc_year=doc.year if doc else None,
        doc_journal=doc.journal if doc else None,
        doc_authors=doc.authors if doc else None,
        vector_score=score,
    )


def search_by_vector(
    db: Session,
    query_embedding: list[float],
    limit: int = 50,
    phase: str | None = None,
    doc_type: str | None = None,
) -> list[ScoredChunk]:
    """向量相似度检索（余弦）。

    Args:
        query_embedding: 查询向量，维度须与 EMBEDDING_DIM 一致
        limit: 返回条数
        phase: 围术期分期过滤，None 表示不限制
        doc_type: 文献类型过滤
    """
    distance = Chunk.embedding.cosine_distance(query_embedding).label("distance")
    stmt = _base_query().add_columns(distance).where(Chunk.embedding.is_not(None))

    if phase:
        stmt = stmt.where(Chunk.phase == phase)
    if doc_type:
        stmt = stmt.where(Chunk.doc_type == doc_type)

    # 先按距离排序取候选。注意：带 WHERE 过滤时 HNSW 走后过滤，
    # 若过滤性极强可考虑 pgvector 0.8+ 的迭代扫描；本课题规模下无需优化。
    stmt = stmt.order_by(distance).limit(limit)
    rows = db.execute(stmt).unique().all()

    results: list[ScoredChunk] = []
    for row in rows:
        chunk: Chunk = row[0]
        dist = float(row[-1])
        results.append(_to_scored(chunk, score=1.0 - dist))
    return results


def search_by_keyword(
    db: Session,
    query: str,
    limit: int = 50,
    phase: str | None = None,
    min_overlap: float = 0.3,
    doc_type: str | None = None,
) -> list[ScoredChunk]:
    """中文关键词检索（bigram 重叠）。

    实现方式：
      1. 把查询切成 bigram 词元（如 '吻合口漏' -> {吻合, 合口, 口漏}）
      2. 用数组重叠运算符 `&&` 召回，走 GIN 倒排索引
      3. 在 Python 中计算交集比例作为得分（比在 SQL 里算交集更清晰可靠）
      4. **按最小重叠比例过滤**（关键，见下）

    为什么必须过滤低重叠结果
    ------------------------
    查询越长，产生的 bigram 越多，而任意两个中文医学文本都会共享若干
    高频词元（"术后""治疗""患者"）。若不过滤，长查询会召回大量仅共享
    1~2 个常见词元的无关文档。

    这在与 RRF 配合时尤其有害：RRF 只使用**名次**不使用分数，这些低重叠
    文档在关键词路排名靠前，会被当成「多路共识」而被提权，反而拉低
    整体准确率（消融实验中标题反查 R@1 由 1.000 跌至 0.250）。

    ⚠️ 本路对**自然语言提问**的实际贡献极低
    ----------------------------------------
    阶段十在 195 题中文 + 189 题英文的口语化临床问题评测集上实测：
    本路单独使用 R@5 分别为 0.000 与 0.005，几乎完全失效。
    原因不是跨语言，而是**口语提问与学术文本的词汇体系不同**：
    医师问"这两个术式在并发症上有什么区别"，
    而文献写"robotic versus conventional laparoscopic surgery"。

    bigram 是精确词元匹配，无法跨越这种表述差异。
    因此本路的权重应设得很低（见 hybrid_search 中的 keyword_weight），
    其定位是「补充精确术语命中」而非「主要召回手段」。

    Args:
        min_overlap: 最小重叠比例，0 表示不过滤。默认 0.3
    """
    if not query.strip():
        return []

    tokens = tokenize_query(query)
    if not tokens:
        return []

    # 用 && 运算符走 GIN 索引召回候选。
    # 召回量放大到 limit 的 3 倍，为后续按重叠度过滤留出余量。
    stmt = _base_query().where(Chunk.bigrams.op("&&")(tokens))
    if phase:
        stmt = stmt.where(Chunk.phase == phase)
    if doc_type:
        stmt = stmt.where(Chunk.doc_type == doc_type)
    # Rank before LIMIT. An unordered candidate cap changes which rows survive
    # between PostgreSQL scans, confounding repeated searches and ablations.
    overlap_order = text('(SELECT count(DISTINCT term) FROM unnest(kb_chunk.bigrams) AS term '
        'WHERE term = ANY(:keyword_tokens)) DESC').bindparams(
            bindparam('keyword_tokens', value=list(set(tokens)), type_=ARRAY(Text)))
    stmt = stmt.order_by(overlap_order, Chunk.id).limit(limit)

    chunks = db.execute(stmt).unique().scalars().all()

    # 得分 = 命中词元数 / 查询词元数，范围 (0, 1]
    results: list[ScoredChunk] = []
    for chunk in chunks:
        hits = term_overlap_score(tokens, chunk.bigrams or [])
        if min_overlap > 0 and hits < min_overlap:
            continue
        scored = _to_scored(chunk, score=hits)
        scored.keyword_hits = round(hits * len(tokens))
        results.append(scored)

    results.sort(key=lambda c: (-c.score, str(c.chunk_id)))
    results = results[:limit]

    # 兜底：bigram 全部未命中时（极短查询）用 ILIKE 原串匹配
    if not results:
        stmt2 = _base_query().where(Chunk.content.ilike(f"%{query}%")).order_by(Chunk.id).limit(limit)
        if phase:
            stmt2 = stmt2.where(Chunk.phase == phase)
        if doc_type:
            stmt2 = stmt2.where(Chunk.doc_type == doc_type)
        results = [
            _to_scored(c, score=0.3)
            for c in db.execute(stmt2).unique().scalars().all()
        ]

    return results


def term_overlap_score(query_terms: list[str], doc_terms: list[str]) -> float:
    """计算查询词元与文档词元的交集比例。

    >>> term_overlap_score(['吻合','合口','口漏'], ['吻合','合口','口漏','引流'])
    1.0
    >>> term_overlap_score(['吻合','合口','口漏'], ['吻合','引流'])
    0.333...
    """
    if not query_terms:
        return 0.0
    doc_set = set(doc_terms)
    hit = sum(1 for t in set(query_terms) if t in doc_set)
    return hit / len(set(query_terms))


def count_active_documents(db: Session) -> int:
    """统计已上架文献数。"""
    return db.execute(
        select(func.count()).select_from(Document).where(Document.is_active.is_(True))
    ).scalar_one()


def get_chunk_with_doc(
    db: Session, chunk_id: str
) -> tuple[Chunk, Document] | None:
    """按文本块 ID 取回完整内容及其文献。

    用途：引用面板的「展开原文」。
    检索结果里的 `content_snippet` 为控制响应体大小被截断到约 300 字，
    用户想核对整段原文时必须另取一次全文，否则永远只能看到前 300 字。
    """
    row = db.execute(
        select(Chunk, Document)
        .join(Document, Chunk.document_id == Document.id)
        .where(Chunk.id == chunk_id)
    ).first()
    if row is None:
        return None
    return row[0], row[1]


def count_chunks(db: Session) -> int:
    """统计文本块总数（**含已下架文献**）。

    注意：本函数用于 ETL 进度统计（需要覆盖全库），
    不能用来表示「可检索语料规模」——检索只使用上架文献的文本块。
    对外的知识库统计请用 `count_active_chunks`。
    """
    return db.execute(select(func.count()).select_from(Chunk)).scalar_one()


def count_active_chunks(db: Session) -> int:
    """统计**可检索**的文本块数（仅上架文献）。

    为什么需要单独一个函数
    ----------------------
    `/stats` 曾用 `count_chunks` 报告规模，而检索链路只查上架文献，
    于是接口报告的块数远大于实际参与检索的块数
    （实测报 8,849，实际检索池 3,828，高估 1.3 倍）。
    这会让人误判语料规模与向量化进度，故拆分为两个口径。
    """
    return db.execute(
        select(func.count())
        .select_from(Chunk)
        .join(Document, Chunk.document_id == Document.id)
        .where(Document.is_active.is_(True))
    ).scalar_one()


def count_embedded_chunks(db: Session) -> int:
    """统计已生成向量的文本块数（用于确认入库进度，覆盖全库）。"""
    return db.execute(
        select(func.count()).select_from(Chunk).where(Chunk.embedding.is_not(None))
    ).scalar_one()


def count_active_embedded_chunks(db: Session) -> int:
    """统计上架文献中已生成向量的文本块数。"""
    return db.execute(
        select(func.count())
        .select_from(Chunk)
        .join(Document, Chunk.document_id == Document.id)
        .where(Document.is_active.is_(True), Chunk.embedding.is_not(None))
    ).scalar_one()


def phase_distribution(db: Session, *, active_only: bool = False) -> dict[str, int]:
    """按围术期分期统计文本块数量。

    Args:
        active_only: 仅统计上架文献（对外统计用）
    """
    stmt = select(Chunk.phase, func.count()).group_by(Chunk.phase)
    if active_only:
        stmt = (
            select(Chunk.phase, func.count())
            .join(Document, Chunk.document_id == Document.id)
            .where(Document.is_active.is_(True))
            .group_by(Chunk.phase)
        )
    rows = db.execute(stmt).all()
    dist = {p.value: 0 for p in PerioperativePhase}
    for phase, cnt in rows:
        dist[phase] = cnt
    return dist


def evidence_distribution(db: Session, *, active_only: bool = False) -> dict[str, int]:
    """按证据等级统计文本块数量。

    Args:
        active_only: 仅统计上架文献（对外统计用）
    """
    stmt = select(Chunk.evidence_level, func.count()).group_by(Chunk.evidence_level)
    if active_only:
        stmt = (
            select(Chunk.evidence_level, func.count())
            .join(Document, Chunk.document_id == Document.id)
            .where(Document.is_active.is_(True))
            .group_by(Chunk.evidence_level)
        )
    rows = db.execute(stmt).all()
    return {level: cnt for level, cnt in rows}
