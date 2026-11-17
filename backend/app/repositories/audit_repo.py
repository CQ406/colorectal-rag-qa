"""审计日志数据访问（阶段九「用户权限管理」的配套能力）。

审计日志的设计目标是**可追溯**：任何写操作都能回答
「谁、在什么时候、对什么、做了什么改动」。

查询模式与索引的对应关系
------------------------
日志表会随时间线性增长，因此索引必须匹配实际查询模式：

  按时间倒序浏览         → created_at 索引
  查某用户的操作历史     → (user_id, created_at) 复合索引
  查某类资源的变更记录   → (resource, created_at) 复合索引
  查失败操作（异常排查） → success 索引

这些索引在模型定义中已声明，此处不再重复。

**日志清理**：日志无上限增长最终会拖慢查询并占用磁盘。
提供 `purge_before()` 供管理员按保留期清理。
默认保留 180 天 —— 覆盖一个完整的毕设周期与答辩追溯窗口。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.models.system import AuditLog

logger = get_logger("repositories.audit")

# 默认保留天数
DEFAULT_RETENTION_DAYS = 180


def list_logs(
    db: Session,
    *,
    username: str | None = None,
    action: str | None = None,
    resource: str | None = None,
    success: bool | None = None,
    keyword: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[AuditLog], int]:
    """分页查询审计日志。"""
    stmt = select(AuditLog)
    count_stmt = select(func.count()).select_from(AuditLog)

    conditions = []
    if username:
        conditions.append(AuditLog.username.ilike(f"%{username.strip()}%"))
    if action:
        conditions.append(AuditLog.action == action)
    if resource:
        conditions.append(AuditLog.resource == resource)
    if success is not None:
        conditions.append(AuditLog.success.is_(success))
    if start is not None:
        conditions.append(AuditLog.created_at >= start)
    if end is not None:
        conditions.append(AuditLog.created_at <= end)
    if keyword:
        pattern = f"%{keyword.strip()}%"
        conditions.append(
            or_(
                AuditLog.resource_id.ilike(pattern),
                AuditLog.error_message.ilike(pattern),
                AuditLog.username.ilike(pattern),
            )
        )

    if conditions:
        stmt = stmt.where(*conditions)
        count_stmt = count_stmt.where(*conditions)

    total = db.execute(count_stmt).scalar_one()
    stmt = stmt.order_by(AuditLog.created_at.desc()).limit(limit).offset(offset)
    return list(db.execute(stmt).scalars().all()), total


def stats(db: Session, days: int = 7) -> dict:
    """最近 N 天的操作统计（供监控面板展示）。"""
    since = datetime.now(timezone.utc) - timedelta(days=days)

    total = db.execute(
        select(func.count()).select_from(AuditLog).where(AuditLog.created_at >= since)
    ).scalar_one()
    failed = db.execute(
        select(func.count()).select_from(AuditLog)
        .where(AuditLog.created_at >= since, AuditLog.success.is_(False))
    ).scalar_one()

    by_resource = dict(
        db.execute(
            select(AuditLog.resource, func.count())
            .where(AuditLog.created_at >= since)
            .group_by(AuditLog.resource)
        ).all()
    )
    by_action = dict(
        db.execute(
            select(AuditLog.action, func.count())
            .where(AuditLog.created_at >= since)
            .group_by(AuditLog.action)
        ).all()
    )
    return {
        "since_days": days,
        "total": total,
        "failed": failed,
        "by_resource": by_resource,
        "by_action": by_action,
    }


def recent_logins(db: Session, limit: int = 10) -> list[AuditLog]:
    """最近登录记录（含失败），用于快速发现异常登录。"""
    stmt = (
        select(AuditLog)
        .where(AuditLog.action == "login")
        .order_by(AuditLog.created_at.desc())
        .limit(limit)
    )
    return list(db.execute(stmt).scalars().all())


def purge_before(db: Session, days: int = DEFAULT_RETENTION_DAYS) -> int:
    """清理早于保留期的日志。

    Returns:
        删除的条数
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    result = db.execute(delete(AuditLog).where(AuditLog.created_at < cutoff))
    deleted = result.rowcount or 0
    logger.info("已清理 %d 条早于 %d 天前的审计日志", deleted, days)
    return deleted
