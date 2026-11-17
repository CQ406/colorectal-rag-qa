"""系统管理接口（阶段九：问答质量监控、知识库维护、系统配置）。

端点
----
  质量监控
    GET    /api/admin/qa/logs              问答日志（管理员看全部，医师看自己）
    GET    /api/admin/qa/logs/{id}         单条日志详情
    GET    /api/admin/qa/stats             质量统计
    GET    /api/admin/qa/low-quality       低质量样本（供人工分析）
    GET    /api/admin/qa/top-questions     高频问题排行
    POST   /api/feedback                   提交反馈（所有登录用户）
    GET    /api/admin/feedback             反馈列表
    PUT    /api/admin/feedback/{id}/resolve 处理反馈

  审计日志
    GET    /api/admin/audit/logs           审计日志
    GET    /api/admin/audit/stats          审计统计
    GET    /api/admin/audit/logins         最近登录记录
    POST   /api/admin/audit/purge          清理过期日志

  系统配置
    GET    /api/admin/config               配置列表
    PUT    /api/admin/config               更新单个配置
    PUT    /api/admin/config/batch         批量更新
    POST   /api/admin/config/reset         恢复默认值

  系统概览
    GET    /api/admin/overview             管理首页汇总数据

权限边界
--------
医师可查看**自己的**问答记录并提交反馈（LOG_VIEW_SELF / FEEDBACK_SUBMIT），
但不能查看他人记录、审计日志与系统配置的修改权限。
这一区分在 `_resolve_scope()` 中实现：管理员不加用户过滤，医师强制过滤。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, require_permission, write_audit, path_uuid
from app.core.logging import get_logger
from app.core.permissions import Permission, has_permission
from app.db.session import get_db
from app.models.system import QaLog, User
from app.repositories import (
    audit_repo,
    config_repo,
    image_repo,
    qa_log_repo,
    user_repo,
)
from app.schemas.common import (
    AuditLogItem,
    AuditLogListResponse,
    ConfigBatchUpdateRequest,
    ConfigItem,
    ConfigListResponse,
    ConfigUpdateRequest,
    FeedbackItem,
    FeedbackListResponse,
    FeedbackRequest,
    FeedbackResolveRequest,
    QaLogItem,
    QaLogListResponse,
    QaStatsResponse,
    SystemOverviewResponse,
)

logger = get_logger("api.admin")

router = APIRouter(tags=["系统管理"])


# ======================================================================
#  序列化
# ======================================================================
def _qa_item(log: QaLog) -> QaLogItem:
    fb = log.feedback
    return QaLogItem(
        id=str(log.id),
        created_at=log.created_at.isoformat() if log.created_at else None,
        username=log.username,
        session_id=log.session_id,
        trace_id=log.trace_id,
        question=log.question,
        rewritten_query=log.rewritten_query,
        phase=log.phase,
        intent=log.intent,
        mode=log.mode,
        answer_chars=log.answer_chars or 0,
        citation_count=log.citation_count or 0,
        invalid_citations=log.invalid_citations or 0,
        unsupported_numbers=log.unsupported_numbers or 0,
        evidence_count=log.evidence_count or 0,
        has_evidence=log.has_evidence,
        retrieval_ms=log.retrieval_ms,
        generation_ms=log.generation_ms,
        total_ms=log.total_ms,
        model=log.model,
        error_message=log.error_message,
        rating=fb.rating if fb else None,
        issue_type=fb.issue_type if fb else None,
        feedback_comment=fb.comment if fb else None,
        is_resolved=fb.is_resolved if fb else None,
    )


def _feedback_item(fb) -> FeedbackItem:
    return FeedbackItem(
        id=str(fb.id),
        qa_log_id=str(fb.qa_log_id),
        question=(fb.qa_log.question if fb.qa_log else "")[:200],
        rating=fb.rating,
        issue_type=fb.issue_type,
        issue_label=qa_log_repo.ISSUE_LABELS.get(fb.issue_type or "", None),
        comment=fb.comment,
        is_resolved=fb.is_resolved,
        resolved_note=fb.resolved_note,
        created_at=fb.created_at.isoformat() if fb.created_at else None,
    )


def _resolve_scope(user: User, requested_user_id: str | None) -> str | None:
    """确定问答日志的查询范围。

    管理员（有 LOG_VIEW_ALL）可指定任意用户或查看全部；
    医师（仅有 LOG_VIEW_SELF）强制限定为自己，即使显式传了别人的 ID。
    """
    if has_permission(user.role, Permission.LOG_VIEW_ALL):
        return requested_user_id
    return str(user.id)


# ======================================================================
#  问答质量监控
# ======================================================================
@router.get(
    "/admin/qa/logs", response_model=QaLogListResponse, summary="问答日志"
)
def list_qa_logs(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
    user_id: str | None = Query(None, description="限定用户（仅管理员可用）"),
    phase: str | None = Query(None),
    mode: str | None = Query(None),
    has_evidence: bool | None = Query(None),
    only_with_feedback: bool = Query(False, description="只看有反馈的记录"),
    max_rating: int | None = Query(None, ge=1, le=5, description="只看评分不高于该值"),
    days: int | None = Query(None, ge=1, le=365, description="只看最近 N 天"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> QaLogListResponse:
    """查询问答日志。

    医师只能看到自己的记录（服务端强制，不依赖前端传参）。
    """
    if not (
        has_permission(user.role, Permission.LOG_VIEW_SELF)
        or has_permission(user.role, Permission.LOG_VIEW_ALL)
    ):
        raise HTTPException(status_code=403, detail="无权查看问答日志")

    start = None
    if days:
        start = datetime.now(timezone.utc) - timedelta(days=days)

    items, total = qa_log_repo.list_logs(
        db,
        user_id=_resolve_scope(user, user_id),
        phase=phase, mode=mode, has_evidence=has_evidence,
        only_with_feedback=only_with_feedback, max_rating=max_rating,
        start=start, limit=limit, offset=offset,
    )
    return QaLogListResponse(
        items=[_qa_item(x) for x in items], total=total,
        limit=limit, offset=offset,
    )


@router.get(
    "/admin/qa/stats", response_model=QaStatsResponse, summary="问答质量统计"
)
def qa_stats(
    _: Annotated[User, Depends(require_permission(Permission.LOG_VIEW_ALL))],
    db: Annotated[Session, Depends(get_db)],
    days: int = Query(7, ge=1, le=365),
) -> QaStatsResponse:
    """质量统计（需管理权限）。

    指标包含无证据回答占比、幻觉引用率、平均引用数与高频问题排行，
    这些数据可直接用于论文的系统运行质量分析。
    """
    s = qa_log_repo.stats(db, days=days)
    s["top_questions"] = qa_log_repo.top_questions(db, days=max(days, 30))
    return QaStatsResponse(**s)


@router.get("/admin/qa/low-quality", response_model=list[QaLogItem], summary="低质量样本")
def low_quality(
    _: Annotated[User, Depends(require_permission(Permission.LOG_VIEW_ALL))],
    db: Annotated[Session, Depends(get_db)],
    limit: int = Query(20, ge=1, le=100),
) -> list[QaLogItem]:
    """列出需要改进的问答样本。

    筛选条件：用户评分 ≤ 2 分、出现幻觉引用、或完全无证据命中。
    集中列出便于逐条分析并定位是检索问题还是生成问题。
    """
    return [_qa_item(x) for x in qa_log_repo.low_quality_samples(db, limit=limit)]


@router.get(
    "/admin/qa/logs/{log_id}", response_model=QaLogItem, summary="日志详情"
)
def get_qa_log(
    log_id: str,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
    _uuid: Annotated[str, Depends(path_uuid("log_id"))] = None,
) -> QaLogItem:
    log = qa_log_repo.get_log(db, log_id)
    if log is None:
        raise HTTPException(status_code=404, detail="日志不存在")
    # 医师只能看自己的记录
    if (
        not has_permission(user.role, Permission.LOG_VIEW_ALL)
        and str(log.user_id) != str(user.id)
    ):
        raise HTTPException(status_code=403, detail="无权查看他人的问答记录")
    return _qa_item(log)


# ======================================================================
#  反馈
# ======================================================================
@router.post("/feedback", response_model=FeedbackItem, summary="提交反馈")
def submit_feedback(
    req: FeedbackRequest,
    request: Request,
    user: Annotated[User, Depends(require_permission(Permission.FEEDBACK_SUBMIT))],
    db: Annotated[Session, Depends(get_db)],
) -> FeedbackItem:
    """对某次问答提交评价。

    同一用户对同一问答重复提交视为修改，便于用户修正评价。
    """
    log = qa_log_repo.get_log(db, req.qa_log_id)
    if log is None:
        raise HTTPException(status_code=404, detail="问答记录不存在")
    # 只能评价自己的问答（管理员例外）。
    #
    # 原先只校验"问答记录存在"，不校验归属：任何医师只要拿到别人的 qa_log_id，
    # 就能替他打分并写标签，而 qa_log_repo.submit_feedback 以 qa_log_id 为唯一键
    # 走更新分支，会静默覆盖原提交者的评价，污染质量监控与评测指标。
    if (
        not has_permission(user.role, Permission.LOG_VIEW_ALL)
        and str(log.user_id) != str(user.id)
    ):
        raise HTTPException(status_code=403, detail="无权评价他人的问答记录")

    fb = qa_log_repo.submit_feedback(
        db,
        qa_log_id=req.qa_log_id,
        rating=req.rating,
        issue_type=req.issue_type,
        comment=req.comment,
        user_id=str(user.id),
    )
    write_audit(
        db, user=user, action="feedback", resource="qa_log",
        resource_id=req.qa_log_id,
        detail={"rating": req.rating, "issue_type": req.issue_type},
        request=request,
    )
    db.commit()

    # 直接手工构造响应，而非重新查询：
    # 反馈对象与问题文本都已在手，再查一次数据库是无谓开销，
    # 且容易写出「查最新一条」这种在并发下不可靠的代码。
    return FeedbackItem(
        id=str(fb.id),
        qa_log_id=str(fb.qa_log_id),
        question=log.question[:200],
        rating=fb.rating,
        issue_type=fb.issue_type,
        issue_label=qa_log_repo.ISSUE_LABELS.get(fb.issue_type or "", None),
        comment=fb.comment,
        is_resolved=fb.is_resolved,
        resolved_note=fb.resolved_note,
        created_at=fb.created_at.isoformat() if fb.created_at else None,
    )


@router.get(
    "/admin/feedback", response_model=FeedbackListResponse, summary="反馈列表"
)
def list_feedback(
    _: Annotated[User, Depends(require_permission(Permission.FEEDBACK_MANAGE))],
    db: Annotated[Session, Depends(get_db)],
    is_resolved: bool | None = Query(None),
    max_rating: int | None = Query(None, ge=1, le=5),
    issue_type: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> FeedbackListResponse:
    items, total = qa_log_repo.list_feedbacks(
        db, is_resolved=is_resolved, max_rating=max_rating,
        issue_type=issue_type, limit=limit, offset=offset,
    )
    return FeedbackListResponse(
        items=[_feedback_item(x) for x in items], total=total,
    )


@router.put(
    "/admin/feedback/{feedback_id}/resolve",
    response_model=FeedbackItem, summary="处理反馈",
)
def resolve_feedback(
    feedback_id: str,
    req: FeedbackResolveRequest,
    request: Request,
    admin: Annotated[User, Depends(require_permission(Permission.FEEDBACK_MANAGE))],
    db: Annotated[Session, Depends(get_db)],
    _uuid: Annotated[str, Depends(path_uuid("feedback_id"))] = None,
) -> FeedbackItem:
    """把反馈标记为已处理，并记录处理说明（形成闭环）。"""
    from app.models.system import QaFeedback

    fb = db.get(QaFeedback, feedback_id)
    if fb is None:
        raise HTTPException(status_code=404, detail="反馈不存在")

    qa_log_repo.resolve_feedback(db, fb, req.resolved_note)
    write_audit(
        db, user=admin, action="resolve_feedback", resource="feedback",
        resource_id=feedback_id, detail={"note": req.resolved_note},
        request=request,
    )
    db.commit()
    return _feedback_item(fb)


# ======================================================================
#  审计日志
# ======================================================================
@router.get(
    "/admin/audit/logs", response_model=AuditLogListResponse, summary="审计日志"
)
def list_audit_logs(
    _: Annotated[User, Depends(require_permission(Permission.AUDIT_VIEW))],
    db: Annotated[Session, Depends(get_db)],
    username: str | None = Query(None),
    action: str | None = Query(None),
    resource: str | None = Query(None),
    success: bool | None = Query(None),
    days: int | None = Query(None, ge=1, le=3650),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> AuditLogListResponse:
    start = None
    if days:
        start = datetime.now(timezone.utc) - timedelta(days=days)

    items, total = audit_repo.list_logs(
        db, username=username, action=action, resource=resource,
        success=success, start=start, limit=limit, offset=offset,
    )
    return AuditLogListResponse(
        items=[
            AuditLogItem(
                id=str(x.id),
                created_at=x.created_at.isoformat() if x.created_at else None,
                username=x.username, role=x.role, action=x.action,
                resource=x.resource, resource_id=x.resource_id,
                detail=x.detail, ip_address=x.ip_address,
                user_agent=x.user_agent, success=x.success,
                error_message=x.error_message,
            )
            for x in items
        ],
        total=total, limit=limit, offset=offset,
    )


@router.get("/admin/audit/stats", response_model=dict, summary="审计统计")
def audit_stats(
    _: Annotated[User, Depends(require_permission(Permission.AUDIT_VIEW))],
    db: Annotated[Session, Depends(get_db)],
    days: int = Query(7, ge=1, le=365),
) -> dict:
    return audit_repo.stats(db, days=days)


@router.get("/admin/audit/logins", response_model=list[AuditLogItem], summary="最近登录")
def recent_logins(
    _: Annotated[User, Depends(require_permission(Permission.AUDIT_VIEW))],
    db: Annotated[Session, Depends(get_db)],
    limit: int = Query(10, ge=1, le=100),
) -> list[AuditLogItem]:
    """最近登录记录（含失败）。用于快速发现异常登录尝试。"""
    return [
        AuditLogItem(
            id=str(x.id),
            created_at=x.created_at.isoformat() if x.created_at else None,
            username=x.username, role=x.role, action=x.action,
            resource=x.resource, resource_id=x.resource_id,
            ip_address=x.ip_address, success=x.success,
            error_message=x.error_message,
        )
        for x in audit_repo.recent_logins(db, limit=limit)
    ]


@router.post("/admin/audit/purge", response_model=dict, summary="清理过期日志")
def purge_audit(
    request: Request,
    admin: Annotated[User, Depends(require_permission(Permission.AUDIT_VIEW))],
    db: Annotated[Session, Depends(get_db)],
    days: int = Query(180, ge=7, le=3650),
) -> dict:
    """清理早于保留期的审计日志。"""
    deleted = audit_repo.purge_before(db, days=days)
    write_audit(
        db, user=admin, action="purge", resource="audit",
        detail={"days": days, "deleted": deleted}, request=request,
    )
    db.commit()
    return {"deleted": deleted, "retention_days": days}


# ======================================================================
#  系统配置
# ======================================================================
def _config_item(row, db: Session) -> ConfigItem:
    return ConfigItem(
        key=row.key,
        value=row.value,
        value_type=row.value_type,
        category=row.category,
        label=row.label,
        description=row.description,
        min_value=row.min_value,
        max_value=row.max_value,
        is_default=row.is_default,
        updated_by=row.updated_by,
        updated_at=row.updated_at.isoformat() if row.updated_at else None,
        parsed_value=config_repo._from_stored(row.value, row.value_type, None),
    )


@router.get("/admin/config", response_model=ConfigListResponse, summary="配置列表")
def list_config(
    _: Annotated[User, Depends(require_permission(Permission.CONFIG_VIEW))],
    db: Annotated[Session, Depends(get_db)],
    category: str | None = Query(None, description="retrieval / model / system"),
) -> ConfigListResponse:
    config_repo.ensure_defaults(db)
    db.commit()
    rows = config_repo.list_configs(db, category=category)
    return ConfigListResponse(
        items=[_config_item(r, db) for r in rows],
        categories=config_repo.categories(db),
    )


@router.put("/admin/config", response_model=ConfigItem, summary="更新配置")
def update_config(
    req: ConfigUpdateRequest,
    request: Request,
    admin: Annotated[User, Depends(require_permission(Permission.CONFIG_UPDATE))],
    db: Annotated[Session, Depends(get_db)],
) -> ConfigItem:
    """更新单个配置项。

    只接受白名单内的键（见 config_repo.SPECS），并校验取值范围。
    配置直接作用于检索与生成链路，越界值会让系统表现失控。
    """
    old = config_repo.get_value(db, req.key)
    try:
        row = config_repo.update_value(db, req.key, req.value, updated_by=admin.username)
    except config_repo.ConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    write_audit(
        db, user=admin, action="update", resource="config",
        resource_id=req.key,
        detail={"key": req.key, "from": old, "to": req.value},
        request=request,
    )
    db.commit()
    return _config_item(row, db)


@router.put("/admin/config/batch", response_model=dict, summary="批量更新配置")
def batch_update_config(
    req: ConfigBatchUpdateRequest,
    request: Request,
    admin: Annotated[User, Depends(require_permission(Permission.CONFIG_UPDATE))],
    db: Annotated[Session, Depends(get_db)],
) -> dict:
    """批量更新配置。

    部分失败时**不回滚已成功的项**，而是返回成功与失败的明细：
    管理员通常是成组调参（如同时调三个权重），
    因其中一个值不合法而丢掉其他已生效的修改会造成困惑。
    """
    changed, errors = [], []
    for key, value in req.items.items():
        try:
            old = config_repo.get_value(db, key)
            config_repo.update_value(db, key, value, updated_by=admin.username)
            changed.append({"key": key, "from": old, "to": value})
        except config_repo.ConfigError as exc:
            errors.append({"key": key, "error": str(exc)})

    write_audit(
        db, user=admin, action="update_batch", resource="config",
        detail={"changed": len(changed), "failed": len(errors)},
        request=request,
    )
    db.commit()
    return {"changed": changed, "errors": errors, "ok": len(changed)}


@router.post("/admin/config/reset", response_model=dict, summary="恢复默认值")
def reset_config(
    request: Request,
    admin: Annotated[User, Depends(require_permission(Permission.CONFIG_UPDATE))],
    db: Annotated[Session, Depends(get_db)],
    key: str | None = Query(None, description="只恢复某一项；不传则全部恢复"),
) -> dict:
    """把配置恢复为出厂默认值（取自 .env）。"""
    if key:
        try:
            config_repo.reset_value(db, key, updated_by=admin.username)
        except config_repo.ConfigError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        count = 1
    else:
        count = config_repo.reset_all(db, updated_by=admin.username)

    write_audit(
        db, user=admin, action="reset", resource="config",
        resource_id=key, detail={"count": count}, request=request,
    )
    db.commit()
    return {"reset_count": count}


# ======================================================================
#  系统概览
# ======================================================================
@router.get(
    "/admin/overview", response_model=SystemOverviewResponse, summary="系统概览"
)
def overview(
    _: Annotated[User, Depends(require_permission(Permission.CONFIG_VIEW))],
    db: Annotated[Session, Depends(get_db)],
) -> SystemOverviewResponse:
    """管理首页汇总：用户、问答、审计、知识库、影像五项统计。"""
    from app.core.config import settings
    from app.repositories import search_repo

    try:
        knowledge = {
            "documents": search_repo.count_active_documents(db),
            "chunks": search_repo.count_chunks(db),
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("知识库统计失败: %s", str(exc)[:120])
        knowledge = {"documents": 0, "chunks": 0}

    try:
        images = image_repo.stats(db)
    except Exception:  # noqa: BLE001
        images = {"total": 0, "analyzed": 0}

    return SystemOverviewResponse(
        users=user_repo.stats(db),
        qa=qa_log_repo.stats(db, days=7),
        audit=audit_repo.stats(db, days=7),
        knowledge=knowledge,
        images=images,
        auth_enabled=settings.auth.enabled,
    )
