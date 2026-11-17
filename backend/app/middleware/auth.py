"""认证中间件（阶段九）。

在请求进入路由之前完成令牌校验，落地「默认拒绝」策略：

    未在 permissions.is_public_path 中声明为公开的路径 → 一律需要有效令牌

这样处理的好处是**新增接口时忘记声明权限，出错方向是安全的**
（会要求登录，而不是无保护地暴露出去）。

与 `app/api/deps.py` 的关系
---------------------------
中间件负责**第一道**粗粒度拦截（是否有有效令牌）。
细粒度权限（该角色能否执行该操作）由各接口的
`require_permission(...)` 依赖完成，因为它需要知道具体的权限点。

**为什么不用 FastAPI 的全局依赖实现**
全局依赖同样可行，但中间件能在路由匹配之前就返回 401，
避免为未授权请求做多余的路径解析与依赖注入；
且中间件里可以直接按「方法 + 路径」判断，表达力更强。

会话级的用户对象挂在 `request.state.user` 上，
接口可通过 `Request` 取用（多数场景仍建议用 `get_current_user` 依赖，
类型更明确）。
"""

from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

from app.core.config import settings
from app.core.logging import get_logger
from app.core.permissions import is_public_path
from app.core.security import TokenError, decode_access_token

logger = get_logger("middleware.auth")


class AuthMiddleware(BaseHTTPMiddleware):
    """令牌校验中间件。"""

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        path = request.url.path
        method = request.method

        # 认证总开关关闭时直接放行（便于脚本与评测）
        if not settings.auth.enabled:
            request.state.user = None
            return await call_next(request)

        # CORS 预检请求不带 Authorization 头，必须放行，
        # 否则浏览器端所有跨域写操作都会失败。
        if method == "OPTIONS":
            return await call_next(request)

        if is_public_path(method, path):
            request.state.user = None
            return await call_next(request)

        authorization = request.headers.get("authorization")
        token = None
        if authorization:
            parts = authorization.split(None, 1)
            if len(parts) == 2 and parts[0].lower() == "bearer":
                token = parts[1].strip()

        if not token:
            return JSONResponse(
                status_code=401,
                content={
                    "detail": "未登录或登录已过期，请重新登录",
                    "path": path,
                },
                headers={"WWW-Authenticate": "Bearer"},
            )

        # 这里只做令牌本身的校验（签名、有效期、类型）。
        # 用户当前状态（是否被停用）需要查库，放在 deps 中做，
        # 避免中间件每次请求都访问数据库。
        try:
            payload = decode_access_token(token)
        except TokenError as exc:
            logger.debug("令牌校验失败 path=%s: %s", path, str(exc)[:80])
            return JSONResponse(
                status_code=401,
                content={"detail": str(exc), "path": path},
                headers={"WWW-Authenticate": "Bearer"},
            )

        # 把令牌载荷挂在 request.state，供后续依赖复用，避免重复解码
        request.state.token_payload = payload
        request.state.user = None
        return await call_next(request)
