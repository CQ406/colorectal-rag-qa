"""健康检查接口。

集中检查所有外部依赖，便于阶段一验收与后续运维排查。
每个组件独立计时，即使某个依赖不可用也返回完整信息。
"""

from __future__ import annotations

import time

from fastapi import APIRouter

from app.core.config import settings
from app.schemas.common import (
    DISCLAIMER,
    ComponentStatus,
    HealthResponse,
    MessageResponse,
)
from app.services import health as health_svc
from app.services.model_registry import registry

router = APIRouter(tags=["健康检查"])


def _timed(name: str, fn) -> ComponentStatus:
    """执行一次检查并计时，异常也不中断整体健康检查。"""
    t0 = time.perf_counter()
    try:
        ok, detail = fn()
    except Exception as exc:  # noqa: BLE001
        ok, detail = False, f"检查异常: {type(exc).__name__}: {str(exc)[:120]}"
    return ComponentStatus(
        name=name,
        ok=ok,
        detail=detail,
        latency_ms=round((time.perf_counter() - t0) * 1000, 1),
    )


@router.get("/health", response_model=HealthResponse, summary="健康检查（含全部依赖）")
def health() -> HealthResponse:
    """检查数据库、图数据库、缓存、对象存储与模型配置。

    用于阶段一验收：所有组件 ok=true 即表示基础设施就绪。
    """
    components = [
        _timed("PostgreSQL+pgvector", health_svc.check_postgres),
        _timed("Neo4j", health_svc.check_neo4j),
        _timed("Redis", health_svc.check_redis),
        _timed("文件存储", health_svc.check_storage),
        _timed("LLM 配置", health_svc.check_llm_config),
        _timed("多模态模型配置", health_svc.check_vlm_config),
    ]

    all_ok = all(c.ok for c in components)
    return HealthResponse(
        status="overall" if all_ok else "degraded",
        app_name=settings.app.app_name,
        environment=settings.app.app_env,
        version="0.1.0",
        components=components,
        models=registry.status(),
        disclaimer=DISCLAIMER,
    )


@router.get("/health/live", response_model=MessageResponse, summary="存活探针")
def liveness() -> MessageResponse:
    """仅确认进程存活，不检查依赖（用于容器编排的存活探针）。"""
    return MessageResponse(success=True, message="alive")


@router.get("/health/ready", response_model=MessageResponse, summary="就绪探针")
def readiness() -> MessageResponse:
    """确认核心依赖（数据库）就绪。"""
    ok, detail = health_svc.check_postgres()
    return MessageResponse(success=ok, message=detail)
