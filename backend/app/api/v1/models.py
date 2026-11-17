"""模型管理与自检接口。

用途：
  - 查看检索模型加载状态与显存占用
  - 手动触发加载/卸载（调试显存问题）
  - 向量化性能实测（验证 GPU 是否正常工作）
"""

from __future__ import annotations

import time

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException

from app.api.deps import require_permission
from app.core.permissions import Permission
from app.schemas.common import EmbedRequest, EmbedResponse, MessageResponse
from app.services import search_service
from app.services.model_registry import registry

# 写操作（加载/卸载/向量化自检）需要 MODEL_MANAGE 权限。
#
# 此前四个端点都没有任何依赖：任何登录用户（含医师）都能 POST /models/unload
# 把两个检索模型卸掉，之后全站检索要重新加载（约 60 秒）；/models/embed 还能
# 被当作免费 GPU 算力接口使用。权限表里定义了 MODEL_MANAGE 却从未被引用。
router = APIRouter(prefix="/models", tags=["模型管理"])


@router.get("/status", summary="检索模型状态")
def status() -> dict:
    """返回模型加载状态、设备与显存信息。

    注意 `loaded=false` 是正常的——模型采用懒加载，首次调用才载入显存。
    """
    return registry.status()


@router.post("/load", response_model=MessageResponse, summary="预加载检索模型")
def load(
    _: Annotated[object, Depends(require_permission(Permission.MODEL_MANAGE))],
) -> MessageResponse:
    """手动预加载 BGE-M3 与重排模型。

    适合在答辩演示前调用，避免首次提问时等待模型加载（约 6 秒）。
    """
    try:
        t0 = time.perf_counter()
        registry.get_embedder()
        registry.get_reranker()
        return MessageResponse(
            success=True,
            message=f"模型已加载，耗时 {time.perf_counter() - t0:.1f} 秒",
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=500, detail=f"加载失败: {type(exc).__name__}: {exc}"
        ) from exc


@router.post("/unload", response_model=MessageResponse, summary="卸载检索模型")
def unload(
    _: Annotated[object, Depends(require_permission(Permission.MODEL_MANAGE))],
) -> MessageResponse:
    """卸载模型并释放显存（跑本地训练或需要腾显存时使用）。"""
    registry.unload_all()
    return MessageResponse(success=True, message="模型已卸载，显存已释放")


@router.post("/embed", response_model=EmbedResponse, summary="向量化自检")
def embed(
    req: EmbedRequest,
    _: Annotated[object, Depends(require_permission(Permission.MODEL_MANAGE))],
) -> EmbedResponse:
    """把文本向量化，用于验证模型可用性并实测吞吐。

    只返回前 2 条向量的前 8 维作为预览，避免响应体过大。
    """
    try:
        vectors, elapsed = search_service.embed_texts(req.texts, req.batch_size)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    if not vectors:
        raise HTTPException(status_code=400, detail="未产生向量")

    return EmbedResponse(
        count=len(vectors),
        dim=len(vectors[0]),
        elapsed_ms=round(elapsed * 1000, 1),
        per_second=round(len(vectors) / elapsed, 1) if elapsed > 0 else 0.0,
        preview=[[round(x, 4) for x in v[:8]] for v in vectors[:2]],
    )
