"""结直肠手术围术期多模态知识库智能问答系统 —— FastAPI 应用入口。"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# 允许「直接把本文件当脚本运行」（IDE 的 Run/Debug 按钮、python backend/app/main.py）。
#
# 正常启动方式是 `uvicorn app.main:app`（工作目录 backend，PYTHONPATH=backend），
# 此时 `app` 包在 sys.path 上。但直接运行本文件时，Python 只把
# `backend/app` 放进 sys.path，`from app.api...` 就会报
# ModuleNotFoundError: No module named 'app'。
# 这里在包上下文缺失时把 backend 目录补上，并同步 PYTHONPATH（uvicorn 的
# reload 子进程会重新导入模块，需要继承同一路径）。
if __package__ in (None, ""):
    _backend_root = str(Path(__file__).resolve().parents[1])
    if _backend_root not in sys.path:
        sys.path.insert(0, _backend_root)
    os.environ["PYTHONPATH"] = os.pathsep.join(
        [_backend_root] + [p for p in os.environ.get("PYTHONPATH", "").split(os.pathsep) if p]
    )

from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.v1.router import api_router
from app.core.config import settings
from app.core.logging import get_logger, setup_logging

logger = get_logger("main")


class UTF8JSONResponse(JSONResponse):
    """显式声明 charset=utf-8 的 JSON 响应。

    背景：FastAPI 的默认 JSONResponse 返回 `application/json`，
    不带 charset 参数。而 Windows PowerShell 5.1 的 Invoke-RestMethod
    在缺少 charset 时会按 ISO-8859-1 解码响应体，
    导致中文变成乱码（如启动脚本里显示的 `?¨?????`）。

    虽然浏览器与 httpx 会正确按 UTF-8 解码，但为了让
    PowerShell 脚本、curl 等各类客户端都能正确显示中文，
    此处显式声明字符集。属于无副作用的兼容性改进。
    """

    media_type = "application/json; charset=utf-8"

    def render(self, content: Any) -> bytes:
        # 复用父类实现，仅覆盖 media_type
        return super().render(content)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期：启动时打印关键配置，关闭时释放资源。"""
    setup_logging()
    logger.info("=" * 70)
    logger.info("%s 启动中...", settings.app.app_name)
    logger.info("环境: %s | 调试: %s", settings.app.app_env, settings.app.debug)
    logger.info("数据库: %s:%s/%s",
                settings.postgres.postgres_host,
                settings.postgres.postgres_port,
                settings.postgres.postgres_db)

    # 模型不在启动时加载——首次调用时才载入，避免占用显存影响其他任务
    from app.services.model_registry import registry
    logger.info("检索模型: %s (device=%s)", settings.model.embedding_model_path,
                settings.model.embedding_device)
    logger.info("模型加载策略: %s", registry.status_text)
    logger.info("LLM: %s | 密钥已配置: %s | 视觉模型密钥已配置: %s",
                settings.llm.llm_model, settings.llm.is_configured,
                settings.vlm.is_configured)
    logger.info("=" * 70)

    # ---------- 初始化系统数据（阶段九）----------
    # 放在启动流程中而非首次请求时：若数据库不可用，应在启动日志中
    # 立刻暴露，而不是等到管理员打开界面才发现缺少账号与配置。
    _init_system_data()

    yield

    logger.info("关闭中，释放模型显存...")
    from app.services.model_registry import registry
    registry.unload_all()
    logger.info("已退出")




def _init_system_data() -> None:
    """初始化用户账号与系统配置。

    两件事都必须**幂等**（可重复执行而不产生副作用）：
      - ensure_defaults 只补缺失的配置项，不覆盖管理员调过的值
      - ensure_admin 仅在完全没有管理员时才创建，不重置已有口令

    因此每次启动调用都是安全的。
    """
    from app.db.session import session_scope
    from app.repositories import config_repo, user_repo

    try:
        with session_scope() as db:
            added = config_repo.ensure_defaults(db)
            admin, created = user_repo.ensure_admin(db)
            if created:
                logger.warning(
                    "已创建初始管理员：%s（默认口令见 .env 的 AUTH_ADMIN_PASSWORD）"
                    "，请首次登录后立即修改",
                    admin.username,
                )
            logger.info(
                "系统数据就绪：配置项新增 %d 个，用户 %d 个",
                added, user_repo.stats(db)["total"],
            )
            # 配置表与代码默认值不一致时告警。
            #
            # 检索链路现在以配置表为准（config_repo.effective）。历史上出现过
            # "代码把 graph_weight 默认值从 0.6 改成 0.2，但配置表仍是 0.6"的情况，
            # 结果线上生效的是旧的 0.6。这条日志让下次改默认值时能立刻发现。
            from app.core.config import settings as _s

            drift = []
            eff = config_repo.effective(db)
            for key, want in (
                ("retrieval.vector_weight", _s.retrieval.vector_weight),
                ("retrieval.keyword_weight", _s.retrieval.keyword_weight),
                ("retrieval.graph_weight", _s.retrieval.graph_weight),
                ("retrieval.evidence_boost_strength", _s.retrieval.evidence_boost_strength),
                ("retrieval.max_chunks_per_document", _s.retrieval.max_chunks_per_document),
            ):
                have = eff.get(key)
                if isinstance(want, (int, float)) and isinstance(have, (int, float)):
                    if abs(float(have) - float(want)) > 1e-9:
                        drift.append(f"{key}: 配置表={have} 代码={want}")
            if drift:
                logger.warning(
                    "运行时配置与代码默认值不一致（生效的是配置表）：%s；"
                    "如需对齐可执行 python -m scripts.align_runtime_config --apply",
                    "；".join(drift),
                )
    except Exception as exc:  # noqa: BLE001
        # 初始化失败不应阻止服务启动：数据库短暂不可用时，
        # 服务仍应起来并在恢复后正常工作，由管理员从日志发现问题。
        logger.error("系统数据初始化失败（服务仍将启动）: %s", str(exc)[:200])

app = FastAPI(
    title=settings.app.app_name,
    description=(
        "基于术中影像与RAG的结直肠手术围术期多模态知识库智能问答系统。\n\n"
        "**注意**：本系统输出仅供教学与科研参考，不构成任何诊疗建议。"
    ),
    version="0.1.0",
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
    lifespan=lifespan,
    # 统一使用带 charset 的 JSON 响应，保证各类客户端中文显示正常
    default_response_class=UTF8JSONResponse,
)

# 认证中间件（阶段九）。
#
# ⚠️ 中间件注册顺序与执行顺序相反：后注册的先执行。
# CORS 必须在认证**之前**执行，否则未通过认证的跨域请求
# 不会带上 CORS 响应头，浏览器只会报 CORS 错误而看不到 401，
# 使前端无法正确识别「需要登录」这一状态。
# 因此先注册 Auth，再注册 CORS，让 CORS 包裹认证失败时的直接响应。
if settings.auth.enabled:
    from app.middleware.auth import AuthMiddleware

    app.add_middleware(AuthMiddleware)
    logger.info("认证中间件已启用（令牌有效期 %d 分钟）",
                settings.auth.token_expire_minutes)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.app.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router, prefix=settings.app.api_prefix)


@app.get("/", tags=["根"], summary="服务信息")
def root() -> dict:
    """返回服务基本信息与文档地址。"""
    return {
        "name": settings.app.app_name,
        "version": "0.1.0",
        "environment": settings.app.app_env,
        "docs": "/docs",
        "api_prefix": settings.app.api_prefix,
        "disclaimer": "本系统输出仅供教学与科研参考，不构成诊疗建议。",
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=8000,
        reload=settings.app.is_dev,
    )
