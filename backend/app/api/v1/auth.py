"""认证与用户管理接口（阶段九「用户权限管理模块」）。

端点
----
  POST   /api/auth/login             登录，签发令牌
  GET    /api/auth/me                当前用户信息（前端据此控制界面）
  POST   /api/auth/change-password   修改自己的口令

  GET    /api/users                  用户列表（需 user:view）
  POST   /api/users                  创建账号（需 user:manage）
  GET    /api/users/stats            用户统计
  GET    /api/users/{id}             用户详情
  PATCH  /api/users/{id}             更新资料
  PUT    /api/users/{id}/role        变更角色
  PUT    /api/users/{id}/active      启用/停用
  PUT    /api/users/{id}/password    重置口令
  POST   /api/users/{id}/unlock      解锁账号

安全设计要点
------------
**登录失败不区分原因**
  对外统一返回「用户名或口令错误」。区分「用户不存在」与「口令错误」
  会让攻击者能枚举有效用户名，是常见的账户探测手段。
  真实原因写入审计日志，供管理员排查。

**不允许操作自己的高危属性**
  管理员不能停用自己、不能降低自己的角色 —— 否则可能把自己锁在系统外，
  且这类误操作难以恢复（需要直接改数据库）。这类限制在服务端强制，
  不依赖前端隐藏按钮。

**最后一个管理员保护**
  禁止停用或降级系统中最后一个可用管理员，避免系统失去管理入口。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, require_permission, write_audit, path_uuid
from app.core.logging import get_logger
from app.core.permissions import ROLE_LABELS, Permission, permissions_for, role_label
from app.core.security import create_access_token
from app.db.session import get_db
from app.models.system import User
from app.repositories import audit_repo, user_repo
from app.schemas.common import (
    LoginRequest,
    LoginResponse,
    PasswordChangeRequest,
    PasswordResetRequest,
    UserActiveRequest,
    UserCreateRequest,
    UserInfo,
    UserListResponse,
    UserRoleRequest,
    UserUpdateRequest,
)

logger = get_logger("api.auth")

router = APIRouter(tags=["认证与用户"])


# ----------------------------------------------------------------------
#  序列化
# ----------------------------------------------------------------------
def _to_info(user: User) -> UserInfo:
    """把 ORM 用户转为对外信息（绝不包含 password_hash）。"""
    return UserInfo(
        id=str(user.id),
        username=user.username,
        display_name=user.display_name,
        role=user.role,
        role_label=role_label(user.role),
        department=user.department,
        email=user.email,
        is_active=user.is_active,
        is_locked=user.is_locked,
        last_login_at=(
            user.last_login_at.isoformat() if user.last_login_at else None
        ),
        last_login_ip=user.last_login_ip,
        login_count=user.login_count or 0,
        note=user.note,
        created_at=user.created_at.isoformat() if user.created_at else None,
        permissions=sorted(permissions_for(user.role)),
    )


def _admin_count(db: Session) -> int:
    """当前可用管理员数量（用于「最后一个管理员」保护）。"""
    return sum(
        1
        for u in db.query(User).filter(User.role == "admin").all()
        if u.is_active
    )


# ======================================================================
#  认证
# ======================================================================
@router.post("/auth/login", response_model=LoginResponse, summary="登录")
def login(
    req: LoginRequest,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
) -> LoginResponse:
    """登录并签发访问令牌。

    连续失败达到阈值后账号会被临时锁定。
    无论成功失败都写审计日志 —— 失败记录是发现暴力破解的主要线索。
    """
    ip = None
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        ip = forwarded.split(",")[0].strip()
    elif request.client is not None:
        ip = request.client.host

    user, reason = user_repo.authenticate(db, req.username, req.password, ip=ip)

    if user is None:
        # 审计：记录尝试的用户名（可能是攻击者猜测的不存在账号）
        write_audit(
            db,
            user=None,
            action="login",
            resource="auth",
            resource_id=req.username[:64],
            detail={"result": "failed", "reason": reason},
            request=request,
            success=False,
            error_message=reason,
        )
        db.commit()
        # 对外统一提示，避免账号枚举
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="用户名或口令错误",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token, expires_in = create_access_token(
        user_id=str(user.id), username=user.username, role=user.role
    )
    write_audit(
        db,
        user=user,
        action="login",
        resource="auth",
        resource_id=str(user.id),
        detail={"result": "success"},
        request=request,
    )
    db.commit()
    logger.info("用户 %s 登录成功（角色 %s）", user.username, role_label(user.role))

    return LoginResponse(
        access_token=token, token_type="bearer",
        expires_in=expires_in, user=_to_info(user),
    )


@router.get("/auth/me", response_model=UserInfo, summary="当前用户信息")
def me(user: Annotated[User, Depends(get_current_user)]) -> UserInfo:
    """返回当前登录用户及其权限集。

    前端据此控制按钮显隐。**注意**：这只是界面优化，
    真正的权限校验在服务端每个写接口上，前端隐藏不构成安全边界。
    """
    return _to_info(user)


@router.post("/auth/change-password", response_model=dict, summary="修改自己的口令")
def change_password(
    req: PasswordChangeRequest,
    request: Request,
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> dict:
    """用户修改自己的口令（需提供原口令）。"""
    ok, reason = user_repo.change_password(
        db, user, req.old_password, req.new_password
    )
    write_audit(
        db, user=user, action="change_password", resource="auth",
        resource_id=str(user.id), request=request,
        success=ok, error_message=None if ok else reason,
    )
    if not ok:
        db.commit()
        raise HTTPException(status_code=400, detail=reason)

    db.commit()
    return {"changed": True, "message": "口令已修改，请使用新口令重新登录"}


# ======================================================================
#  用户管理
# ======================================================================
@router.get("/users", response_model=UserListResponse, summary="用户列表")
def list_users(
    user: Annotated[User, Depends(require_permission(Permission.USER_VIEW))],
    db: Annotated[Session, Depends(get_db)],
    keyword: str | None = Query(None, description="按登录名/姓名/科室搜索"),
    role: str | None = Query(None, description="按角色筛选"),
    is_active: bool | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> UserListResponse:
    items, total = user_repo.list_users(
        db, keyword=keyword, role=role, is_active=is_active,
        limit=limit, offset=offset,
    )
    return UserListResponse(
        items=[_to_info(u) for u in items], total=total,
        limit=limit, offset=offset,
    )


@router.get("/users/stats", response_model=dict, summary="用户统计")
def user_stats(
    _: Annotated[User, Depends(require_permission(Permission.USER_VIEW))],
    db: Annotated[Session, Depends(get_db)],
) -> dict:
    """用户统计。

    ⚠️ 路由顺序：必须定义在 `/{user_id}` 之前，否则会被路径参数匹配。
    """
    s = user_repo.stats(db)
    s["by_role_label"] = {
        ROLE_LABELS.get(k, k): v for k, v in s.get("by_role", {}).items()
    }
    return s


@router.post(
    "/users", response_model=UserInfo,
    status_code=status.HTTP_201_CREATED, summary="创建账号",
)
def create_user(
    req: UserCreateRequest,
    request: Request,
    admin: Annotated[User, Depends(require_permission(Permission.USER_MANAGE))],
    db: Annotated[Session, Depends(get_db)],
) -> UserInfo:
    """创建账号（仅管理员）。"""
    if req.role not in ROLE_LABELS:
        raise HTTPException(
            status_code=400,
            detail=f"角色必须是 {' 或 '.join(ROLE_LABELS)} 之一",
        )
    if user_repo.username_exists(db, req.username):
        raise HTTPException(status_code=409, detail=f"登录名 {req.username} 已被占用")

    user = user_repo.create(
        db,
        username=req.username,
        password=req.password,
        role=req.role,
        display_name=req.display_name,
        department=req.department,
        email=req.email,
        note=req.note,
    )
    write_audit(
        db, user=admin, action="create", resource="user",
        resource_id=str(user.id),
        detail={"username": user.username, "role": user.role},
        request=request,
    )
    db.commit()
    return _to_info(user)


@router.get("/users/{user_id}", response_model=UserInfo, summary="用户详情")
def get_user(
    user_id: str,
    _: Annotated[User, Depends(require_permission(Permission.USER_VIEW))],
    db: Annotated[Session, Depends(get_db)],
    _uuid: Annotated[str, Depends(path_uuid("user_id"))] = None,
) -> UserInfo:
    user = user_repo.get(db, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    return _to_info(user)


@router.patch("/users/{user_id}", response_model=UserInfo, summary="更新用户资料")
def update_user(
    user_id: str,
    req: UserUpdateRequest,
    request: Request,
    admin: Annotated[User, Depends(require_permission(Permission.USER_MANAGE))],
    db: Annotated[Session, Depends(get_db)],
    _uuid: Annotated[str, Depends(path_uuid("user_id"))] = None,
) -> UserInfo:
    user = user_repo.get(db, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="用户不存在")

    user_repo.update_profile(
        db, user,
        display_name=req.display_name, department=req.department,
        email=req.email, note=req.note,
    )
    write_audit(
        db, user=admin, action="update", resource="user",
        resource_id=user_id,
        detail=req.model_dump(exclude_none=True),
        request=request,
    )
    db.commit()
    return _to_info(user)


@router.put("/users/{user_id}/role", response_model=UserInfo, summary="变更角色")
def change_role(
    user_id: str,
    req: UserRoleRequest,
    request: Request,
    admin: Annotated[User, Depends(require_permission(Permission.USER_MANAGE))],
    db: Annotated[Session, Depends(get_db)],
) -> UserInfo:
    """变更用户角色。

    两道保护：
      1. 不能修改自己的角色（防止把自己降级后失去管理权限）
      2. 不能把最后一个管理员降级（防止系统失去管理入口）
    """
    if req.role not in ROLE_LABELS:
        raise HTTPException(
            status_code=400,
            detail=f"角色必须是 {' 或 '.join(ROLE_LABELS)} 之一",
        )
    user = user_repo.get(db, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="用户不存在")

    if str(user.id) == str(admin.id):
        raise HTTPException(
            status_code=400, detail="不能修改自己的角色，请由其他管理员操作",
        )
    if user.role == "admin" and req.role != "admin" and _admin_count(db) <= 1:
        raise HTTPException(
            status_code=400,
            detail="系统中至少需保留一名可用管理员，无法降级最后一个管理员",
        )

    old_role = user.role
    user_repo.set_role(db, user, req.role)
    write_audit(
        db, user=admin, action="update_role", resource="user",
        resource_id=user_id,
        detail={"username": user.username, "from": old_role, "to": req.role},
        request=request,
    )
    db.commit()
    return _to_info(user)


@router.put("/users/{user_id}/active", response_model=UserInfo, summary="启用/停用")
def set_user_active(
    user_id: str,
    req: UserActiveRequest,
    request: Request,
    admin: Annotated[User, Depends(require_permission(Permission.USER_MANAGE))],
    db: Annotated[Session, Depends(get_db)],
    _uuid: Annotated[str, Depends(path_uuid("user_id"))] = None,
) -> UserInfo:
    """启用或停用账号。

    停用后该用户手中未过期的令牌立即失效（鉴权时会校验 is_active）。
    """
    user = user_repo.get(db, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="用户不存在")

    if str(user.id) == str(admin.id) and not req.is_active:
        raise HTTPException(
            status_code=400, detail="不能停用自己的账号",
        )
    if not req.is_active and user.role == "admin" and _admin_count(db) <= 1:
        raise HTTPException(
            status_code=400,
            detail="系统中至少需保留一名可用管理员，无法停用最后一个管理员",
        )

    user_repo.set_active(db, user, req.is_active)
    write_audit(
        db, user=admin,
        action="activate" if req.is_active else "deactivate",
        resource="user", resource_id=user_id,
        detail={"username": user.username},
        request=request,
    )
    db.commit()
    return _to_info(user)


@router.put("/users/{user_id}/password", response_model=dict, summary="重置口令")
def reset_password(
    user_id: str,
    req: PasswordResetRequest,
    request: Request,
    admin: Annotated[User, Depends(require_permission(Permission.USER_MANAGE))],
    db: Annotated[Session, Depends(get_db)],
    _uuid: Annotated[str, Depends(path_uuid("user_id"))] = None,
) -> dict:
    """管理员重置用户口令（无需原口令）。

    审计日志**不记录口令内容**，只记录操作事实。
    """
    user = user_repo.get(db, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="用户不存在")

    user_repo.reset_password(db, user, req.new_password)
    write_audit(
        db, user=admin, action="reset_password", resource="user",
        resource_id=user_id, detail={"username": user.username},
        request=request,
    )
    db.commit()
    return {"reset": True, "message": f"已重置 {user.username} 的口令"}


@router.post("/users/{user_id}/unlock", response_model=UserInfo, summary="解锁账号")
def unlock_user(
    user_id: str,
    request: Request,
    admin: Annotated[User, Depends(require_permission(Permission.USER_MANAGE))],
    db: Annotated[Session, Depends(get_db)],
) -> UserInfo:
    """解除因连续登录失败导致的临时锁定。"""
    user = user_repo.get(db, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="用户不存在")

    user_repo.unlock(db, user)
    write_audit(
        db, user=admin, action="unlock", resource="user",
        resource_id=user_id, detail={"username": user.username},
        request=request,
    )
    db.commit()
    return _to_info(user)


@router.get("/roles", response_model=dict, summary="角色与权限清单")
def list_roles(
    _: Annotated[User, Depends(require_permission(Permission.USER_VIEW))],
) -> dict:
    """返回角色定义与各自的权限集，供界面展示「该角色能做什么」。"""
    from app.core.permissions import ROLE_PERMISSIONS

    return {
        "roles": [
            {
                "value": role,
                "label": ROLE_LABELS[role],
                "permissions": sorted(perms),
                "user_count": None,
            }
            for role, perms in ROLE_PERMISSIONS.items()
        ]
    }
