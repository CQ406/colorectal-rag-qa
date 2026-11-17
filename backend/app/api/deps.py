"""FastAPI 依赖：认证、鉴权与审计。

提供三类依赖：

  get_current_user        解析令牌得到当前用户（强制登录）
  get_optional_user       可选登录（公开接口里想知道是谁在用）
  require_permission(...) 权限工厂，返回校验特定权限的依赖
  record_audit(...)       写审计日志（在写操作成功后调用）

**默认拒绝原则**
  未显式声明为公开的路径一律需要认证。这条规则在 main.py 的中间件中落地，
  因此新增接口时若忘记声明权限，其行为是「要求登录」而非「无保护」——
  出错方向是安全的。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated
from uuid import UUID

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.logging import get_logger
from app.core.permissions import Permission, has_permission, role_label
from app.core.security import TokenError, decode_access_token
from app.db.session import get_db
from app.models.system import AuditLog, User

logger = get_logger("api.deps")


def path_uuid(param: str):
    """校验路径参数是合法 UUID，不合法返回 422 而不是 500。

    为什么需要：路径参数声明成 `str` 时会原样传进 SQL，
    PostgreSQL 对 `WHERE id = 'abc'` 抛 `invalid input syntax for type uuid`，
    接口变成 500「服务器内部错误」，而这是**客户端输入问题**，应为 422。
    项目里 images/cases 用 UUID 类型做对了，知识库/日志/用户几组端点没有，
    属于不一致实现。

    用依赖工厂而不是逐处改注解：调用点只需多一个 `Depends(path_uuid("doc_id"))`，
    函数体与仓储层完全不用动（它们仍拿到字符串）。

    Args:
        param: 路径参数名（如 "doc_id"）
    """

    def _check(request: Request) -> str:
        value = request.path_params.get(param)
        if value is None:
            return ""
        try:
            UUID(str(value))
        except (ValueError, AttributeError, TypeError):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"无效的 {param}：需要 UUID 格式",
            ) from None
        return str(value)

    return _check

# 认证失败时统一返回 401 并带 WWW-Authenticate 头，
# 便于前端识别「需要重新登录」与「权限不足」的区别。
_UNAUTHORIZED = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="未登录或登录已过期，请重新登录",
    headers={"WWW-Authenticate": "Bearer"},
)


def _extract_token(authorization: str | None) -> str | None:
    """从 Authorization 头提取 Bearer 令牌。"""
    if not authorization:
        return None
    parts = authorization.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1].strip() or None


def _load_user(db: Session, token: str) -> User:
    """校验令牌并加载用户。

    除签名与有效期外，还校验用户的**当前状态**：
      - 账号被停用（is_active=False）→ 立即失效
      - 账号被锁定（failed_attempts 超限）→ 立即失效

    这一步弥补了 JWT 无法主动吊销的缺陷：管理员停用某账号后，
    该用户手中尚未过期的令牌立刻不可用。
    """
    try:
        payload = decode_access_token(token)
    except TokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    user = db.get(User, payload["sub"])
    if user is None:
        raise _UNAUTHORIZED
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="账号已被停用，请联系管理员",
        )
    if user.is_locked:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="账号因多次登录失败被临时锁定，请稍后重试",
        )
    return user


def get_current_user(
    db: Annotated[Session, Depends(get_db)],
    authorization: Annotated[str | None, Header()] = None,
) -> User:
    """强制登录依赖。"""
    if not settings.auth.enabled:
        # 认证关闭时返回一个内置的管理员身份，便于脚本与评测调用
        return _anonymous_admin(db)

    token = _extract_token(authorization)
    if not token:
        raise _UNAUTHORIZED
    return _load_user(db, token)


def get_optional_user(
    db: Annotated[Session, Depends(get_db)],
    authorization: Annotated[str | None, Header()] = None,
) -> User | None:
    """可选登录依赖：未带令牌时返回 None，令牌无效时同样返回 None。

    用于公开接口记录「是谁在用」，_token 无效不影响接口正常返回。
    """
    if not settings.auth.enabled:
        return _anonymous_admin(db)
    token = _extract_token(authorization)
    if not token:
        return None
    try:
        return _load_user(db, token)
    except HTTPException:
        return None


# 内置匿名管理员（仅在 auth.enabled=False 时使用）。
# 不写入数据库：它只是权限载体，避免在库里留下一个可被登录的账号。
_ANON_ADMIN: User | None = None


def _anonymous_admin(db: Session) -> User:
    """构造内存中的管理员身份（用于关闭认证的场景）。"""
    global _ANON_ADMIN
    if _ANON_ADMIN is None:
        _ANON_ADMIN = User(
            id="00000000-0000-0000-0000-000000000000",
            username="anonymous",
            password_hash="",
            display_name="匿名（认证已关闭）",
            role="admin",
            is_active=True,
        )
    return _ANON_ADMIN


def require_permission(permission: Permission):
    """权限校验依赖工厂。

    用法::

        @router.post("/users", dependencies=[Depends(require_permission(Permission.USER_MANAGE))])
    """
    def _checker(user: Annotated[User, Depends(get_current_user)]) -> User:
        if not has_permission(user.role, permission):
            logger.warning(
                "权限不足: 用户 %s(角色=%s) 尝试执行 %s",
                user.username, role_label(user.role), permission.value,
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    f"当前角色（{role_label(user.role)}）无权执行该操作"
                    f"（需要权限：{permission.value}）"
                ),
            )
        return user

    return _checker


# ----------------------------------------------------------------------
#  审计日志
# ----------------------------------------------------------------------
def request_context(request: Request) -> tuple[str | None, str | None]:
    """提取客户端 IP 与 User-Agent。

    IP 优先取 X-Forwarded-For 首项（存在反向代理时），
    否则取直连地址。审计日志需要真实来源，因此两者都记录。
    """
    ip: str | None = None
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        ip = forwarded.split(",")[0].strip()
    elif request.client is not None:
        ip = request.client.host
    return ip, request.headers.get("user-agent")


def write_audit(
    db: Session,
    *,
    user: User | None,
    action: str,
    resource: str,
    resource_id: str | None = None,
    detail: dict | None = None,
    request: Request | None = None,
    success: bool = True,
    error_message: str | None = None,
) -> None:
    """写入一条审计日志。

    **不抛异常**：审计日志写失败不应导致业务操作回滚。
    业务已经成功却因日志写入失败而报错，对用户是更糟的体验；
    这里记录告警即可，由运维在日志中发现问题。
    """
    ip = ua = None
    if request is not None:
        ip, ua = request_context(request)

    try:
        db.add(
            AuditLog(
                created_at=datetime.now(timezone.utc),
                user_id=str(user.id) if user is not None else None,
                username=user.username if user is not None else None,
                role=user.role if user is not None else None,
                action=action,
                resource=resource,
                resource_id=str(resource_id) if resource_id else None,
                detail=detail,
                ip_address=ip,
                user_agent=ua[:300] if ua else None,
                success=success,
                error_message=error_message,
            )
        )
        db.flush()
    except Exception as exc:  # noqa: BLE001
        logger.warning("审计日志写入失败（不影响业务）: %s", str(exc)[:150])
