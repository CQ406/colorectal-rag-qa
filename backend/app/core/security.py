"""认证与授权的基础设施：口令哈希、令牌签发与校验。

为什么直接调用 bcrypt 而不用 passlib
------------------------------------
项目环境同时装有 passlib 1.7.4 与 bcrypt 5.0.0。passlib 1.7.4 在初始化
bcrypt 后端时会读取 `bcrypt.__about__.__version__`，而该属性自 bcrypt 4.1
起已被移除，虽多数情况仅产生告警，但属于已知的不兼容点。
直接调用 bcrypt 只有三个 API（gensalt / hashpw / checkpw），
逻辑清晰、无中间层，也便于精确控制 rounds 参数。

⚠️ bcrypt 5.0 的行为变更（实测确认）
-----------------------------------
bcrypt 5.0.0 **不再自动截断超过 72 字节的口令，而是抛出 ValueError**：

    ValueError: password cannot be longer than 72 bytes,
                truncate manually if necessary

旧版本会静默截断，因此这一变更在升级后才会暴露，容易造成
「短口令能登录、长口令报 500」的线上故障。本模块在哈希与校验前
统一按 72 字节截断，使长短口令行为一致。

为什么用 bcrypt 而不是 PBKDF2
----------------------------
两者都能满足需求。bcrypt 的优势是内置盐值管理与计算成本参数，
且在本环境中已验证可用（rounds=12 约 440 ms，对登录接口可接受）。
刻意提高单次哈希成本是为了抬高离线暴力破解的代价。

令牌方案
--------
采用 JWT（HS256）而非服务端会话表：
  - 无状态，重启后端不会导致所有用户掉线
  - 便于将来多实例部署时横向扩展

代价是**无法主动吊销单个令牌**。为此做了两点补偿：
  1. 有效期较短（默认 8 小时，覆盖一个工作班次）
  2. 每次请求都校验用户当前状态（is_active / 锁定），
     用户被停用或锁定后，其未过期令牌立即失效
第 2 点使吊销能力基本恢复，同时保留了无状态的好处。
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

import bcrypt
from jose import JWTError, jwt

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger("core.security")

# bcrypt 的口令长度上限（字节）。这是算法本身的限制，非可配置项。
BCRYPT_MAX_BYTES = 72

# 计算成本：2^rounds 次迭代。12 在安全性与延迟间取平衡
# （实测约 440 ms，低于用户可感知的 1 秒阈值）。
DEFAULT_ROUNDS = 12


class TokenError(Exception):
    """令牌无效或已过期。"""


# ----------------------------------------------------------------------
#  口令哈希
# ----------------------------------------------------------------------
def _prepare(password: str) -> bytes:
    """把口令转换为 bcrypt 可接受的字节串。

    按 72 字节截断而非报错，原因：
      - 用户无感知：注册与登录使用同一规则，行为一致
      - 避免 500：bcrypt 5.0 会直接抛 ValueError

    截断按字节而非字符，需保证不破坏 UTF-8 编码：
    直接在 72 字节处切片可能切断多字节字符，因此逐步回退到合法边界。
    """
    raw = password.encode("utf-8")
    if len(raw) <= BCRYPT_MAX_BYTES:
        return raw
    truncated = raw[:BCRYPT_MAX_BYTES]
    # 回退到合法的 UTF-8 边界（连续字节 10xxxxxx 属于多字节字符的后续字节）
    while truncated and (truncated[-1] & 0xC0) == 0x80:
        truncated = truncated[:-1]
    return truncated


def hash_password(password: str, rounds: int = DEFAULT_ROUNDS) -> str:
    """生成口令哈希。

    Returns:
        形如 `$2b$12$...` 的 60 字符哈希串（含算法标识、成本参数与盐值）。
    """
    if not password:
        raise ValueError("口令不能为空")
    salt = bcrypt.gensalt(rounds=rounds)
    return bcrypt.hashpw(_prepare(password), salt).decode("ascii")


def verify_password(password: str, password_hash: str) -> bool:
    """校验口令。

    任何异常（哈希格式非法、算法不匹配）都返回 False 而不抛出，
    避免把内部错误细节暴露给调用方，也防止异常被用作账号探测手段。
    """
    if not password or not password_hash:
        return False
    try:
        return bcrypt.checkpw(_prepare(password), password_hash.encode("ascii"))
    except (ValueError, TypeError) as exc:
        logger.warning("口令校验异常: %s", str(exc)[:100])
        return False


def needs_rehash(password_hash: str, rounds: int = DEFAULT_ROUNDS) -> bool:
    """判断哈希是否使用了低于当前标准的成本参数。

    用于在用户成功登录时透明地升级旧哈希（无需强制改密）。
    """
    try:
        parts = password_hash.split("$")
        # 格式: ['', '2b', '12', 'salt+hash']
        if len(parts) < 3:
            return True
        return int(parts[2]) < rounds
    except (ValueError, IndexError):
        return True


# ----------------------------------------------------------------------
#  访问令牌
# ----------------------------------------------------------------------
ACCESS_TOKEN_TYPE = "access"


def _secret() -> str:
    """取令牌签名密钥。

    未配置时使用启动时随机生成的密钥并给出告警：
      - 开发环境无需额外配置即可运行
      - 后端重启后旧令牌失效，这是可接受的（也提醒了未配置的事实）
    生产环境必须显式配置，否则多实例之间无法互认令牌。
    """
    configured = (settings.auth.secret_key or "").strip()
    if configured:
        return configured
    global _EPHEMERAL_SECRET
    try:
        return _EPHEMERAL_SECRET
    except NameError:
        _EPHEMERAL_SECRET = secrets.token_urlsafe(48)
        logger.warning(
            "AUTH_SECRET_KEY 未配置，已生成临时密钥；"
            "后端重启后所有登录状态将失效。生产环境请在 .env 中配置固定密钥。"
        )
        return _EPHEMERAL_SECRET


def create_access_token(
    user_id: str,
    username: str,
    role: str,
    expires_minutes: int | None = None,
) -> tuple[str, int]:
    """签发访问令牌。

    Returns:
        (令牌字符串, 有效期秒数)
    """
    minutes = expires_minutes or settings.auth.token_expire_minutes
    now = datetime.now(timezone.utc)
    expire = now + timedelta(minutes=minutes)

    payload: dict[str, Any] = {
        "sub": str(user_id),          # 主体：用户 ID
        "username": username,
        "role": role,
        "type": ACCESS_TOKEN_TYPE,    # 区分用途，便于将来引入刷新令牌
        "iat": int(now.timestamp()),
        "exp": int(expire.timestamp()),
        "jti": secrets.token_urlsafe(8),   # 令牌唯一标识，便于日志关联
    }
    token = jwt.encode(payload, _secret(), algorithm=settings.auth.algorithm)
    return token, minutes * 60


def decode_access_token(token: str) -> dict:
    """解码并校验访问令牌。

    Raises:
        TokenError: 令牌格式错误、签名不符或已过期
    """
    if not token:
        raise TokenError("未提供令牌")
    try:
        payload = jwt.decode(
            token, _secret(), algorithms=[settings.auth.algorithm]
        )
    except JWTError as exc:
        # 不把底层错误细节直接抛给前端
        logger.debug("令牌校验失败: %s", str(exc)[:120])
        raise TokenError("令牌无效或已过期") from exc

    if payload.get("type") != ACCESS_TOKEN_TYPE:
        raise TokenError("令牌类型不符")
    if not payload.get("sub"):
        raise TokenError("令牌缺少主体信息")
    return payload


# ----------------------------------------------------------------------
#  其他工具
# ----------------------------------------------------------------------
def content_digest(text: str) -> str:
    """计算文本内容摘要（SHA256 前 32 位十六进制）。

    用途：问答日志中记录答案摘要哈希而非全文，
    既能校验前端展示内容与日志一致，又不让日志表随答案长度膨胀。
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


def generate_password(length: int = 12) -> str:
    """生成随机初始口令（管理员创建账号时使用）。"""
    alphabet = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKMNPQRSTUVWXYZ23456789"
    # 排除易混淆字符（i/l/o/0/1），降低人工抄录出错概率
    return "".join(secrets.choice(alphabet) for _ in range(length))


def constant_time_compare(a: str, b: str) -> bool:
    """恒定时间字符串比较（防止通过响应时间推断内容）。"""
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))
