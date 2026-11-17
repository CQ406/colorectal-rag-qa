"""用户账号数据访问（阶段九「用户权限管理模块」）。

职责：账号增删改查、登录状态维护（失败计数、锁定、登录审计）。

登录安全设计
------------
**连续失败锁定**：口令被暴力猜测时，仅靠 bcrypt 的计算成本不足以阻止
在线爆破（攻击者可以持续尝试）。因此累计失败达到阈值后临时锁定账号。

**锁定策略的取舍**：锁定会带来拒绝服务风险（攻击者故意输错口令
把他人账号锁住）。缓解办法是把锁定时长设为有限值（默认 15 分钟）
而非永久锁定，并在日志中记录触发来源 IP，便于识别异常。

**不删除账号**：只置 is_active=False。原因见模型注释 ——
删除会让审计日志失去归属，破坏可追溯性。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.logging import get_logger
from app.core.security import hash_password, needs_rehash, verify_password
from app.models.system import User

logger = get_logger("repositories.user")


# ----------------------------------------------------------------------
#  查询
# ----------------------------------------------------------------------
def get(db: Session, user_id: str) -> User | None:
    return db.get(User, user_id)


def get_by_username(db: Session, username: str) -> User | None:
    """按登录名查用户（不区分大小写）。

    登录名不区分大小写可避免「Admin 与 admin 是两个账号」这类混乱。
    """
    if not username:
        return None
    return db.execute(
        select(User).where(func.lower(User.username) == username.strip().lower())
    ).scalar_one_or_none()


def username_exists(db: Session, username: str, exclude_id: str | None = None) -> bool:
    """判断登录名是否已被占用。"""
    stmt = select(User.id).where(func.lower(User.username) == username.strip().lower())
    if exclude_id:
        stmt = stmt.where(User.id != exclude_id)
    return db.execute(stmt.limit(1)).scalar_one_or_none() is not None


def list_users(
    db: Session,
    *,
    keyword: str | None = None,
    role: str | None = None,
    is_active: bool | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[User], int]:
    """分页查询用户列表。"""
    stmt = select(User)
    count_stmt = select(func.count()).select_from(User)

    if keyword:
        pattern = f"%{keyword.strip()}%"
        cond = or_(
            User.username.ilike(pattern),
            User.display_name.ilike(pattern),
            User.department.ilike(pattern),
        )
        stmt = stmt.where(cond)
        count_stmt = count_stmt.where(cond)
    if role:
        stmt = stmt.where(User.role == role)
        count_stmt = count_stmt.where(User.role == role)
    if is_active is not None:
        stmt = stmt.where(User.is_active.is_(is_active))
        count_stmt = count_stmt.where(User.is_active.is_(is_active))

    total = db.execute(count_stmt).scalar_one()
    stmt = stmt.order_by(User.created_at.desc()).limit(limit).offset(offset)
    return list(db.execute(stmt).scalars().all()), total


def stats(db: Session) -> dict:
    """用户统计（供系统管理界面展示）。"""
    total = db.execute(select(func.count()).select_from(User)).scalar_one()
    active = db.execute(
        select(func.count()).select_from(User).where(User.is_active.is_(True))
    ).scalar_one()
    by_role = dict(
        db.execute(select(User.role, func.count()).group_by(User.role)).all()
    )
    return {"total": total, "active": active, "by_role": by_role}


# ----------------------------------------------------------------------
#  创建与修改
# ----------------------------------------------------------------------
def create(
    db: Session,
    *,
    username: str,
    password: str,
    role: str = "physician",
    display_name: str | None = None,
    department: str | None = None,
    email: str | None = None,
    note: str | None = None,
) -> User:
    """创建账号。口令在此处即被哈希，明文不进入调用栈之外。"""
    user = User(
        username=username.strip(),
        password_hash=hash_password(password),
        role=role,
        display_name=display_name,
        department=department,
        email=email,
        note=note,
        is_active=True,
    )
    db.add(user)
    db.flush()
    logger.info("已创建用户 %s（角色 %s）", user.username, user.role)
    return user


def update_profile(
    db: Session,
    user: User,
    *,
    display_name: str | None = None,
    department: str | None = None,
    email: str | None = None,
    note: str | None = None,
) -> User:
    """更新账号资料（不含口令与角色）。"""
    if display_name is not None:
        user.display_name = display_name
    if department is not None:
        user.department = department
    if email is not None:
        user.email = email
    if note is not None:
        user.note = note
    db.flush()
    return user


def set_role(db: Session, user: User, role: str) -> User:
    """变更角色。"""
    old = user.role
    user.role = role
    db.flush()
    logger.info("用户 %s 角色变更: %s -> %s", user.username, old, role)
    return user


def set_active(db: Session, user: User, is_active: bool) -> User:
    """启用/停用账号。

    停用不影响已存在的审计与问答日志，只是阻止后续登录。
    """
    user.is_active = is_active
    if is_active:
        # 重新启用时清除失败计数，否则用户一登录就因历史失败被锁
        user.failed_attempts = 0
        user.locked_until = None
    db.flush()
    logger.info("用户 %s 已%s", user.username, "启用" if is_active else "停用")
    return user


def reset_password(db: Session, user: User, new_password: str) -> User:
    """重置口令。"""
    user.password_hash = hash_password(new_password)
    user.failed_attempts = 0
    user.locked_until = None
    db.flush()
    logger.info("用户 %s 口令已重置", user.username)
    return user


def change_password(
    db: Session, user: User, old_password: str, new_password: str
) -> tuple[bool, str]:
    """用户自行修改口令。

    Returns:
        (是否成功, 失败原因)
    """
    if not verify_password(old_password, user.password_hash):
        return False, "原口令不正确"
    if len(new_password) < 8:
        return False, "新口令长度至少 8 位"
    user.password_hash = hash_password(new_password)
    user.failed_attempts = 0
    db.flush()
    logger.info("用户 %s 已修改口令", user.username)
    return True, ""


# ----------------------------------------------------------------------
#  登录状态维护
# ----------------------------------------------------------------------
def authenticate(
    db: Session, username: str, password: str, ip: str | None = None
) -> tuple[User | None, str]:
    """校验登录凭据。

    Returns:
        (用户对象或 None, 失败原因)。成功时原因为空串。

    失败原因对调用方可见，以便审计记录；但**对外返回给前端的消息会做统一化**，
    避免泄露「用户名存在但口令错误」这类可被用于账号探测的信息。
    """
    user = get_by_username(db, username)
    if user is None:
        # 不区分「用户不存在」与「口令错误」，防止账号枚举
        return None, "用户名或口令错误"

    if user.is_locked:
        remain = int((user.locked_until - datetime.now(timezone.utc)).total_seconds() // 60) + 1
        return None, f"账号已被锁定，请 {remain} 分钟后重试"

    if not user.is_active:
        return None, "账号已被停用"

    if not verify_password(password, user.password_hash):
        record_failed_attempt(db, user)
        return None, "用户名或口令错误"

    # 登录成功
    now = datetime.now(timezone.utc)
    user.last_login_at = now
    user.last_login_ip = ip
    user.login_count = (user.login_count or 0) + 1
    user.failed_attempts = 0
    user.locked_until = None

    # 口令哈希成本低于当前标准时透明升级，用户无感知
    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(password)
        logger.info("已升级用户 %s 的口令哈希成本", user.username)

    db.flush()
    return user, ""


def record_failed_attempt(db: Session, user: User) -> None:
    """记录一次登录失败，达到阈值则锁定账号。"""
    user.failed_attempts = (user.failed_attempts or 0) + 1
    limit = settings.auth.max_failed_attempts
    if user.failed_attempts >= limit:
        user.locked_until = datetime.now(timezone.utc) + timedelta(
            minutes=settings.auth.lockout_minutes
        )
        logger.warning(
            "用户 %s 连续失败 %d 次，已锁定至 %s",
            user.username, user.failed_attempts, user.locked_until,
        )
    db.flush()


def unlock(db: Session, user: User) -> User:
    """管理员手动解锁。"""
    user.failed_attempts = 0
    user.locked_until = None
    db.flush()
    logger.info("用户 %s 已解锁", user.username)
    return user


# ----------------------------------------------------------------------
#  初始化
# ----------------------------------------------------------------------
def ensure_admin(db: Session) -> tuple[User, bool]:
    """确保存在一个可用的管理员账号。

    Returns:
        (管理员对象, 是否为本次新建)

    首次部署时如果没有管理员，系统将无法通过界面完成任何管理操作，
    因此启动流程中调用本函数兜底。**已存在管理员时不做任何改动**，
    不会覆盖管理员自行修改过的口令。
    """
    existing = db.execute(
        select(User).where(User.role == "admin").order_by(User.created_at).limit(1)
    ).scalar_one_or_none()
    if existing is not None:
        return existing, False

    admin = create(
        db,
        username=settings.auth.admin_username,
        password=settings.auth.admin_password,
        role="admin",
        display_name="系统管理员",
        note="由系统初始化自动创建",
    )
    logger.warning(
        "已创建初始管理员账号 %s。请首次登录后立即修改默认口令。",
        admin.username,
    )
    return admin, True
