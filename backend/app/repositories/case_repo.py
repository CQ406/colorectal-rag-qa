"""典型病例库数据访问与相似病例检索。"""

from __future__ import annotations

import uuid

from sqlalchemy import Text, cast, func, or_, select
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.core.text_utils import tokenize_query
from app.models.knowledge import CaseRecord

logger = get_logger("repositories.case_repo")


def case_text(case: CaseRecord | object) -> str:
    """生成病例语义向量使用的稳定文本。"""
    fields = (
        "title", "age", "sex", "diagnosis", "surgery", "phase", "history",
        "findings", "operative_note", "prognosis", "complications", "tags",
    )
    values: list[str] = []
    for field in fields:
        value = getattr(case, field, None)
        if value:
            if isinstance(value, list):
                value = ", ".join(str(x) for x in value)
            values.append(f"{field}: {value}")
    return "\n".join(values)


def _embedding(text: str) -> list[float] | None:
    """尽力生成向量；模型未安装/未加载时保留文本检索可用。"""
    if not text.strip():
        return None
    try:
        from app.services.search_service import embed_texts

        vectors, _ = embed_texts([text], batch_size=1)
        return vectors[0] if vectors else None
    except Exception as exc:  # noqa: BLE001 - 病例录入不能因可选模型失败
        logger.warning("病例向量生成失败，将使用文本匹配: %s", str(exc)[:180])
        return None


def _filters(*, phase: str | None = None, surgery: str | None = None):
    conditions = [CaseRecord.is_public.is_(True)]
    if phase:
        conditions.append(CaseRecord.phase == phase)
    if surgery:
        conditions.append(CaseRecord.surgery.ilike(f"%{surgery}%"))
    return conditions


def list_cases(
    db: Session,
    *,
    keyword: str | None = None,
    phase: str | None = None,
    surgery: str | None = None,
    limit: int = 50,
    offset: int = 0,
    include_private: bool = False,
) -> tuple[list[CaseRecord], int]:
    """分页列出病例；默认只暴露已确认公开的脱敏病例。"""
    conditions = [] if include_private else [CaseRecord.is_public.is_(True)]
    if phase:
        conditions.append(CaseRecord.phase == phase)
    if surgery:
        conditions.append(CaseRecord.surgery.ilike(f"%{surgery}%"))
    if keyword:
        pattern = f"%{keyword}%"
        conditions.append(or_(
            CaseRecord.case_code.ilike(pattern),
            CaseRecord.title.ilike(pattern),
            CaseRecord.diagnosis.ilike(pattern),
            CaseRecord.surgery.ilike(pattern),
            CaseRecord.history.ilike(pattern),
            CaseRecord.findings.ilike(pattern),
            cast(CaseRecord.tags, Text).ilike(pattern),
        ))

    stmt = select(CaseRecord)
    count_stmt = select(func.count()).select_from(CaseRecord)
    if conditions:
        stmt = stmt.where(*conditions)
        count_stmt = count_stmt.where(*conditions)
    total = int(db.execute(count_stmt).scalar_one())
    rows = db.execute(
        stmt.order_by(CaseRecord.created_at.desc()).limit(limit).offset(offset)
    ).scalars().all()
    return rows, total


def get(db: Session, case_id: str | uuid.UUID) -> CaseRecord | None:
    return db.get(CaseRecord, case_id)


def get_by_code(db: Session, case_code: str) -> CaseRecord | None:
    return db.execute(
        select(CaseRecord).where(CaseRecord.case_code == case_code)
    ).scalar_one_or_none()


def get_public(db: Session, case_id: str | uuid.UUID) -> CaseRecord | None:
    case = get(db, case_id)
    return case if case is not None and case.is_public else None


def create(db: Session, **data) -> CaseRecord:
    case = CaseRecord(**data)
    case.embedding = _embedding(case_text(case))
    db.add(case)
    db.flush()
    return case


def update(db: Session, case: CaseRecord, **data) -> CaseRecord:
    for key, value in data.items():
        setattr(case, key, value)
    # 内容变化后必须重建向量，否则相似检索会继续使用旧病例内容。
    case.embedding = _embedding(case_text(case))
    db.flush()
    return case


def delete(db: Session, case: CaseRecord) -> None:
    db.delete(case)


def search_similar(
    db: Session,
    query: str,
    *,
    top_k: int = 10,
    phase: str | None = None,
    surgery: str | None = None,
) -> list[tuple[CaseRecord, float]]:
    """优先使用 pgvector 语义检索，向量不可用时降级为文本命中。"""
    conditions = _filters(phase=phase, surgery=surgery)
    query_vec = _embedding(query)
    if query_vec:
        distance = CaseRecord.embedding.cosine_distance(query_vec).label("distance")
        rows = db.execute(
            select(CaseRecord, distance)
            .where(*conditions, CaseRecord.embedding.is_not(None))
            .order_by(distance)
            .limit(top_k)
        ).all()
        if rows:
            return [(row[0], max(0.0, min(1.0, 1.0 - float(row[1])))) for row in rows]

    terms = [term.strip().lower() for term in tokenize_query(query) if term.strip()]
    if not terms:
        return []
    stmt = select(CaseRecord).where(*conditions)
    rows = db.execute(stmt.order_by(CaseRecord.created_at.desc()).limit(500)).scalars().all()
    ranked: list[tuple[CaseRecord, float]] = []
    for case in rows:
        text = case_text(case).lower()
        hits = sum(1 for term in terms if term in text)
        if hits:
            ranked.append((case, hits / len(terms)))
    ranked.sort(key=lambda item: item[1], reverse=True)
    return ranked[:top_k]


def related_cases(db: Session, query: str, *, top_k: int = 3, phase: str | None = None) -> list[tuple[CaseRecord, float]]:
    """影像描述到公开病例库的推荐入口。"""
    return search_similar(db, query, top_k=top_k, phase=phase)
