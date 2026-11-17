"""问答日志与反馈数据访问（阶段九「问答质量监控模块」）。

报审表要求：「记录问答日志，支持人工反馈与模型效果持续优化」

质量监控的分析维度
------------------
日志记录的是**指标**而非答案全文（见模型注释），据此可以回答四类问题：

  1. 使用情况   谁在用、问了多少、集中在哪个围术期分期
  2. 质量水位   引用数、幻觉引用数、无据数值数、无证据回答占比
  3. 性能表现   检索耗时、生成耗时、端到端耗时的分布
  4. 优化方向   低分反馈的问题类型分布 → 定位是检索问题还是生成问题

**关于「持续优化」的落地方式**
  本模块不实现自动训练（那需要模型侧的微调能力，超出本课题范围），
  而是提供可操作的闭环：
    用户反馈 → 按 issue_type 聚合 → 定位问题类别
      · citation_error / hallucination → 检查引用与数值核验规则
      · incomplete / irrelevant     → 扩充语料或调整检索策略
      · 反馈集中在某分期           → 补充该分期的指南类文献
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, joinedload

from app.core.logging import get_logger
from app.models.system import QaFeedback, QaLog

logger = get_logger("repositories.qa_log")

# 反馈问题类型的中文标签（前端展示与统计口径统一在此维护）
ISSUE_LABELS: dict[str, str] = {
    "accurate": "准确",
    "incomplete": "信息不完整",
    "irrelevant": "答非所问",
    "hallucination": "存在编造内容",
    "citation_error": "引用错误",
    "other": "其他",
}


# ----------------------------------------------------------------------
#  写入
# ----------------------------------------------------------------------
def record(
    db: Session,
    *,
    question: str,
    user_id: str | None = None,
    username: str | None = None,
    session_id: str | None = None,
    trace_id: str | None = None,
    rewritten_query: str | None = None,
    phase: str | None = None,
    intent: str | None = None,
    mode: str = "qa",
    answer: str = "",
    citation_count: int = 0,
    invalid_citations: int = 0,
    unsupported_numbers: int = 0,
    evidence_count: int = 0,
    has_evidence: bool = True,
    retrieval_ms: float | None = None,
    generation_ms: float | None = None,
    total_ms: float | None = None,
    model: str | None = None,
    error_message: str | None = None,
) -> QaLog:
    """写入一条问答日志。

    **不抛异常**：日志记录失败不应影响已经生成的回答。
    问答是用户等待的核心路径，绝不能因为日志表写入失败而报错。
    """
    from app.core.security import content_digest

    log = QaLog(
        created_at=datetime.now(timezone.utc),
        user_id=user_id,
        username=username,
        session_id=session_id,
        trace_id=trace_id,
        question=question[:2000],
        rewritten_query=(rewritten_query or None),
        phase=phase,
        intent=intent,
        mode=mode,
        answer_chars=len(answer or ""),
        answer_digest=content_digest(answer) if answer else None,
        citation_count=citation_count,
        invalid_citations=invalid_citations,
        unsupported_numbers=unsupported_numbers,
        evidence_count=evidence_count,
        has_evidence=has_evidence,
        retrieval_ms=retrieval_ms,
        generation_ms=generation_ms,
        total_ms=total_ms,
        model=model,
        error_message=error_message,
    )
    try:
        db.add(log)
        db.flush()
    except Exception as exc:  # noqa: BLE001
        logger.warning("问答日志写入失败（不影响回答）: %s", str(exc)[:150])
    return log


def submit_feedback(
    db: Session,
    *,
    qa_log_id: str,
    rating: int,
    issue_type: str | None = None,
    comment: str | None = None,
    user_id: str | None = None,
) -> QaFeedback:
    """提交或更新对某次问答的反馈。

    同一用户对同一问答重复提交视为修改（表上 qa_log_id 唯一）。
    这样用户可以修正自己的评价，也避免刷评价导致统计失真。
    """
    existing = db.execute(
        select(QaFeedback).where(QaFeedback.qa_log_id == qa_log_id)
    ).scalar_one_or_none()

    if existing is not None:
        existing.rating = rating
        existing.issue_type = issue_type
        existing.comment = comment
        existing.is_resolved = False   # 内容变化后需重新处理
        db.flush()
        logger.info("已更新问答反馈 %s（评分 %d）", qa_log_id[:8], rating)
        return existing

    feedback = QaFeedback(
        qa_log_id=qa_log_id,
        user_id=user_id,
        rating=rating,
        issue_type=issue_type,
        comment=comment,
    )
    db.add(feedback)
    db.flush()
    logger.info("已提交问答反馈 %s（评分 %d）", qa_log_id[:8], rating)
    return feedback


def resolve_feedback(db: Session, feedback: QaFeedback, note: str | None) -> QaFeedback:
    """标记反馈已处理（形成闭环）。"""
    feedback.is_resolved = True
    feedback.resolved_note = note
    db.flush()
    return feedback


# ----------------------------------------------------------------------
#  查询
# ----------------------------------------------------------------------
def list_logs(
    db: Session,
    *,
    user_id: str | None = None,
    phase: str | None = None,
    mode: str | None = None,
    has_evidence: bool | None = None,
    only_with_feedback: bool = False,
    max_rating: int | None = None,
    keyword: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[QaLog], int]:
    """分页查询问答日志。

    Args:
        user_id: 限定某个用户的记录（医师只能看自己的）
        only_with_feedback: 只看有反馈的记录（便于集中处理问题样本）
        max_rating: 只看评分不高于该值的记录（定位低质量回答）
    """
    stmt = select(QaLog).options(joinedload(QaLog.feedback))
    count_stmt = select(func.count()).select_from(QaLog)

    conditions = []
    if user_id:
        conditions.append(QaLog.user_id == user_id)
    if phase:
        conditions.append(QaLog.phase == phase)
    if mode:
        conditions.append(QaLog.mode == mode)
    if has_evidence is not None:
        conditions.append(QaLog.has_evidence.is_(has_evidence))
    if keyword:
        conditions.append(QaLog.question.ilike(f"%{keyword.strip()}%"))
    if start is not None:
        conditions.append(QaLog.created_at >= start)
    if end is not None:
        conditions.append(QaLog.created_at <= end)

    if only_with_feedback or max_rating is not None:
        # 需要按反馈条件过滤时用子查询，避免 JOIN 后计数失真
        sub = select(QaFeedback.qa_log_id)
        if max_rating is not None:
            sub = sub.where(QaFeedback.rating <= max_rating)
        conditions.append(QaLog.id.in_(sub))

    if conditions:
        stmt = stmt.where(*conditions)
        count_stmt = count_stmt.where(*conditions)

    total = db.execute(count_stmt).scalar_one()
    stmt = stmt.order_by(QaLog.created_at.desc()).limit(limit).offset(offset)
    return list(db.execute(stmt).unique().scalars().all()), total


def get_log(db: Session, log_id: str) -> QaLog | None:
    stmt = (
        select(QaLog)
        .options(joinedload(QaLog.feedback))
        .where(QaLog.id == log_id)
    )
    return db.execute(stmt).unique().scalar_one_or_none()


def list_feedbacks(
    db: Session,
    *,
    is_resolved: bool | None = None,
    max_rating: int | None = None,
    issue_type: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[QaFeedback], int]:
    """分页查询反馈列表。"""
    stmt = select(QaFeedback).options(joinedload(QaFeedback.qa_log))
    count_stmt = select(func.count()).select_from(QaFeedback)

    conditions = []
    if is_resolved is not None:
        conditions.append(QaFeedback.is_resolved.is_(is_resolved))
    if max_rating is not None:
        conditions.append(QaFeedback.rating <= max_rating)
    if issue_type:
        conditions.append(QaFeedback.issue_type == issue_type)

    if conditions:
        stmt = stmt.where(*conditions)
        count_stmt = count_stmt.where(*conditions)

    total = db.execute(count_stmt).scalar_one()
    stmt = stmt.order_by(QaFeedback.created_at.desc()).limit(limit).offset(offset)
    return list(db.execute(stmt).unique().scalars().all()), total


# ----------------------------------------------------------------------
#  统计（质量监控面板的核心数据）
# ----------------------------------------------------------------------
def stats(db: Session, days: int = 7) -> dict:
    """问答质量统计。

    返回的指标直接对应论文中「系统运行质量」小节所需的数据。
    """
    since = datetime.now(timezone.utc) - timedelta(days=days)
    base = select(func.count()).select_from(QaLog).where(QaLog.created_at >= since)

    total = db.execute(base).scalar_one()
    if not total:
        return {
            "since_days": days, "total": 0, "no_evidence": 0,
            "avg_citations": 0.0, "avg_total_ms": 0.0,
            "with_invalid_citations": 0, "with_unsupported_numbers": 0,
            "by_phase": {}, "by_mode": {}, "feedback": {},
        }

    no_evidence = db.execute(
        select(func.count()).select_from(QaLog)
        .where(QaLog.created_at >= since, QaLog.has_evidence.is_(False))
    ).scalar_one()

    avg_citations = db.execute(
        select(func.avg(QaLog.citation_count)).where(QaLog.created_at >= since)
    ).scalar_one() or 0.0
    avg_total = db.execute(
        select(func.avg(QaLog.total_ms)).where(QaLog.created_at >= since)
    ).scalar_one() or 0.0

    # 出现幻觉引用 / 无据数值的问答条数（均为负向指标）
    with_invalid = db.execute(
        select(func.count()).select_from(QaLog)
        .where(QaLog.created_at >= since, QaLog.invalid_citations > 0)
    ).scalar_one()
    with_unsupported = db.execute(
        select(func.count()).select_from(QaLog)
        .where(QaLog.created_at >= since, QaLog.unsupported_numbers > 0)
    ).scalar_one()

    by_phase = dict(
        db.execute(
            select(QaLog.phase, func.count())
            .where(QaLog.created_at >= since)
            .group_by(QaLog.phase)
        ).all()
    )
    by_mode = dict(
        db.execute(
            select(QaLog.mode, func.count())
            .where(QaLog.created_at >= since)
            .group_by(QaLog.mode)
        ).all()
    )

    # 反馈统计
    fb_total = db.execute(
        select(func.count()).select_from(QaFeedback)
        .where(QaFeedback.created_at >= since)
    ).scalar_one()
    fb_avg = db.execute(
        select(func.avg(QaFeedback.rating))
        .where(QaFeedback.created_at >= since)
    ).scalar_one()
    fb_by_issue = dict(
        db.execute(
            select(QaFeedback.issue_type, func.count())
            .where(QaFeedback.created_at >= since)
            .group_by(QaFeedback.issue_type)
        ).all()
    )
    fb_unresolved = db.execute(
        select(func.count()).select_from(QaFeedback)
        .where(QaFeedback.created_at >= since, QaFeedback.is_resolved.is_(False))
    ).scalar_one()

    return {
        "since_days": days,
        "total": total,
        "no_evidence": no_evidence,
        "no_evidence_rate": round(no_evidence / total, 4),
        "avg_citations": round(float(avg_citations), 2),
        "avg_total_ms": round(float(avg_total), 1),
        "with_invalid_citations": with_invalid,
        "with_unsupported_numbers": with_unsupported,
        "hallucination_rate": round(with_invalid / total, 4),
        "by_phase": by_phase,
        "by_mode": by_mode,
        "feedback": {
            "total": fb_total,
            "avg_rating": round(float(fb_avg), 2) if fb_avg else 0.0,
            "unresolved": fb_unresolved,
            "by_issue": {
                ISSUE_LABELS.get(k or "other", k or "其他"): v
                for k, v in fb_by_issue.items()
            },
        },
    }


def top_questions(db: Session, days: int = 30, limit: int = 15) -> list[dict]:
    """高频问题排行。

    用途：发现用户真正关心的问题集中在哪里，
    据此判断语料覆盖是否匹配实际需求（若高频问题常无证据命中，
    说明该方向文献不足，应优先补充）。
    """
    since = datetime.now(timezone.utc) - timedelta(days=days)
    rows = db.execute(
        select(
            QaLog.question,
            func.count().label("n"),
            func.avg(QaLog.citation_count).label("avg_cite"),
        )
        .where(QaLog.created_at >= since)
        .group_by(QaLog.question)
        .order_by(func.count().desc())
        .limit(limit)
    ).all()
    return [
        {
            "question": r.question,
            "count": r.n,
            "avg_citations": round(float(r.avg_cite or 0), 2),
        }
        for r in rows
    ]


def low_quality_samples(db: Session, limit: int = 20) -> list[QaLog]:
    """低质量问答样本（供人工分析）。

    筛选条件：评分低、或出现幻觉引用、或完全没有证据命中。
    这三类正是需要改进的地方，集中列出便于逐条排查。
    """
    sub = select(QaFeedback.qa_log_id).where(QaFeedback.rating <= 2)
    stmt = (
        select(QaLog)
        .options(joinedload(QaLog.feedback))
        .where(
            or_(
                QaLog.id.in_(sub),
                QaLog.invalid_citations > 0,
                QaLog.has_evidence.is_(False),
            )
        )
        .order_by(QaLog.created_at.desc())
        .limit(limit)
    )
    return list(db.execute(stmt).unique().scalars().all())
