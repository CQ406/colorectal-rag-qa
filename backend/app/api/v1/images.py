"""影像知识库与辅助解读接口（阶段六）。

端点
----
  POST   /api/images                 上传影像
  GET    /api/images                 影像列表（支持分期/术式/关键词筛选）
  GET    /api/images/stats           影像库统计
  GET    /api/images/{id}            影像详情（含已缓存的解读）
  GET    /api/images/{id}/raw        影像文件本体（供前端 <img> 直接引用）
  PATCH  /api/images/{id}            更新元数据
  DELETE /api/images/{id}            删除影像
  POST   /api/images/analyze         辅助解读（入库影像或即时 base64）

合规设计
--------
影像解读定位为**辅助解读**而非诊断：VLM 只做客观描述，
结论部分必须由检索到的文献证据支撑并带引用编号，
响应强制附带免责声明（disclaimer）。
"""

from __future__ import annotations

import base64
import binascii
from uuid import UUID
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.api.deps import get_optional_user, require_permission, write_audit
from app.core.permissions import Permission, has_permission
from app.core.config import settings
from app.core.logging import get_logger
from app.db.session import get_db
from app.models.system import User
from app.repositories import image_repo
from app.schemas.common import (
    CitationItem,
    ImageAnalysisResponse,
    ImageAnalyzeRequest,
    ImageItem,
    ImageListResponse,
    ImageStatsResponse,
    ImageUpdateRequest,
    ImageSimilarRequest,
    SimilarImageItem,
    SimilarImageResponse,
)
from app.services import image_analysis, qa_logging
from app.services import vision_client
from app.services.llm_client import LLMError
from app.services.vision_client import VisionError

logger = get_logger("api.images")

router = APIRouter(prefix="/images", tags=["影像知识库"])

VISION_HINT = (
    "未配置视觉模型密钥。请到 https://bigmodel.cn 注册并创建 API Key，"
    "填入 .env 的 VLM_API_KEY 后重启后端。GLM-4V-Flash 为免费模型。"
)


# ----------------------------------------------------------------------
#  上传
# ----------------------------------------------------------------------
@router.post("", response_model=ImageItem, summary="上传影像")
async def upload_image(
    file: UploadFile = File(..., description="影像文件（jpg/png/webp/bmp）"),
    title: str | None = Form(None),
    description: str | None = Form(None),
    surgery_type: str | None = Form(None),
    anatomy_site: str | None = Form(None),
    phase: str = Form("intraop"),
    source: str | None = Form(None),
    is_public: bool = Form(False),
    db: Session = Depends(get_db),
    user: User = Depends(require_permission(Permission.IMAGE_UPLOAD)),
) -> ImageItem:
    """上传影像到知识库。

    同一文件（按 SHA256）重复上传时返回已有记录，不重复占用空间。
    """
    data = await file.read(vision_client.MAX_IMAGE_BYTES + 1)
    if len(data) > vision_client.MAX_IMAGE_BYTES:
        raise HTTPException(status_code=413, detail="单张影像不能超过 8 MB")
    if not data:
        raise HTTPException(status_code=400, detail="上传文件为空")
    if is_public and not has_permission(user.role, Permission.IMAGE_PUBLISH):
        raise HTTPException(status_code=403, detail="当前角色无权发布公开影像")

    try:
        asset, duplicated = image_repo.create(
            db,
            filename=file.filename or "image",
            data=data,
            mime_type=file.content_type,
            title=title,
            description=description,
            surgery_type=surgery_type,
            anatomy_site=anatomy_site,
            phase=phase,
            source=source,
            is_public=is_public,
        )
    except VisionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    write_audit(
        db, user=user, action="image_upload", resource="image",
        resource_id=str(asset.id), detail={"filename": asset.filename, "is_public": asset.is_public},
    )
    try:
        db.commit()
    except Exception as exc:  # noqa: BLE001
        # 补偿：文件已经落盘但记录没写成，删掉文件避免孤儿。
        # （重复上传复用已有记录时不能删——那份文件属于原记录）
        db.rollback()
        if not duplicated:
            image_repo.remove_stored_file(asset.storage_path)
        logger.warning("影像记录写入失败，已回滚并清理文件: %s", str(exc)[:150])
        raise HTTPException(status_code=500, detail="影像入库失败，请重试") from exc
    if duplicated:
        logger.info("影像重复上传，复用已有记录: %s", asset.filename)

    brief = image_repo._to_brief(asset)
    return ImageItem(**brief.to_dict())


# ----------------------------------------------------------------------
#  列表与统计
# ----------------------------------------------------------------------
@router.get("", response_model=ImageListResponse, summary="影像列表")
def list_images(
    phase: str | None = Query(None, description="围术期分期筛选"),
    surgery_type: str | None = Query(None, description="术式筛选"),
    keyword: str | None = Query(None, description="标题/说明/部位关键词"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> ImageListResponse:
    items, total = image_repo.list_images(
        db, phase=phase, surgery_type=surgery_type,
        keyword=keyword, limit=limit, offset=offset,
    )
    return ImageListResponse(
        items=[ImageItem(**i.to_dict()) for i in items],
        total=total, limit=limit, offset=offset,
    )


@router.get("/manage", response_model=ImageListResponse, summary="影像草稿与管理列表")
def manage_images(
    phase: str | None = Query(None),
    surgery_type: str | None = Query(None),
    keyword: str | None = Query(None, max_length=200),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    user: User = Depends(require_permission(Permission.IMAGE_UPDATE)),
) -> ImageListResponse:
    """维护人员共享的草稿工作区；公开列表仍只返回已发布影像。"""
    items, total = image_repo.list_images(
        db, phase=phase, surgery_type=surgery_type, keyword=keyword,
        limit=limit, offset=offset, include_private=True,
    )
    return ImageListResponse(
        items=[ImageItem(**item.to_dict()) for item in items],
        total=total, limit=limit, offset=offset,
    )


@router.get("/manage/stats", response_model=ImageStatsResponse, summary="影像管理统计")
def manage_image_stats(
    db: Session = Depends(get_db),
    user: User = Depends(require_permission(Permission.IMAGE_UPDATE)),
) -> ImageStatsResponse:
    counts = image_repo.stats(db, include_private=True)
    configured = vision_client.is_configured()
    return ImageStatsResponse(
        **counts, vision_configured=configured,
        vision_hint="" if configured else VISION_HINT,
    )


@router.get("/manage/{image_id}", response_model=dict, summary="影像管理详情")
def manage_image_detail(
    image_id: UUID,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission(Permission.IMAGE_UPDATE)),
) -> dict:
    asset = image_repo.get(db, str(image_id))
    if asset is None:
        raise HTTPException(status_code=404, detail="影像不存在")
    return {**image_repo._to_brief(asset).to_dict(),
            "vlm_analysis": asset.vlm_analysis,
            "vlm_interpretation": asset.vlm_interpretation}


@router.get("/manage/{image_id}/raw", summary="影像管理原图")
def manage_image_raw(
    image_id: UUID,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission(Permission.IMAGE_UPDATE)),
) -> Response:
    asset = image_repo.get(db, str(image_id))
    if asset is None:
        raise HTTPException(status_code=404, detail="影像不存在")
    try:
        data = image_repo.read_bytes(asset)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="影像文件缺失") from exc
    return Response(content=data, media_type=asset.mime_type or "image/jpeg",
                    headers={"Cache-Control": "private, no-store", "Vary": "Authorization"})


@router.get("/stats", response_model=ImageStatsResponse, summary="影像库统计")
def image_stats(db: Session = Depends(get_db)) -> ImageStatsResponse:
    """影像库统计。

    ⚠️ 路由顺序：本端点必须定义在 `/{image_id}` **之前**，
    否则 `/api/images/stats` 会被 `/{image_id}` 匹配，
    导致 stats 被当作影像 ID 查询而返回 404。
    """
    s = image_repo.stats(db)
    configured = vision_client.is_configured()
    return ImageStatsResponse(
        total=s["total"],
        analyzed=s["analyzed"],
        by_phase=s["by_phase"],
        total_bytes=s["total_bytes"],
        vision_configured=configured,
        vision_hint="" if configured else VISION_HINT,
    )


@router.post("/similar", response_model=SimilarImageResponse, summary="影像特征相似检索")
def similar_image_search(
    req: ImageSimilarRequest,
    db: Session = Depends(get_db),
    user: User | None = Depends(get_optional_user),
) -> SimilarImageResponse:
    """按影像颜色直方图基线特征检索相似公开样例。"""
    query_id = req.image_id
    if req.image_id:
        asset = image_repo.get(db, req.image_id)
        if asset is None or (not asset.is_public and not (user and has_permission(user.role, Permission.IMAGE_UPDATE))):
            raise HTTPException(status_code=404, detail="影像不存在或无权访问")
        vector = image_repo.ensure_feature(db, asset)
        if vector is None:
            raise HTTPException(status_code=422, detail="无法提取该影像特征")
        exclude_id = str(asset.id)
    elif req.image_base64:
        if len(req.image_base64) > 4 * ((vision_client.MAX_IMAGE_BYTES + 2) // 3):
            raise HTTPException(status_code=413, detail="单张影像不能超过 8 MB")
        try:
            raw = base64.b64decode(req.image_base64, validate=True)
            if len(raw) > vision_client.MAX_IMAGE_BYTES:
                raise HTTPException(status_code=413, detail="单张影像不能超过 8 MB")
            from app.services.image_features import extract_feature

            vector = extract_feature(raw)
        except HTTPException:
            raise
        except (binascii.Error, ValueError) as exc:
            raise HTTPException(status_code=400, detail=f"base64 图片解码失败: {exc}") from exc
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=422, detail=f"无法提取影像特征: {exc}") from exc
        exclude_id = None
    else:
        raise HTTPException(status_code=400, detail="需提供 image_id 或 image_base64")

    matches = image_repo.similar_images(db, vector, top_k=req.top_k, exclude_id=exclude_id)
    return SimilarImageResponse(
        query_image_id=query_id,
        total=len(matches),
        items=[
            SimilarImageItem(
                id=str(asset.id), filename=asset.filename, title=asset.title,
                description=asset.description, surgery_type=asset.surgery_type,
                anatomy_site=asset.anatomy_site, phase=asset.phase,
                url=image_repo._to_brief(asset).to_dict()["url"],
                similarity=round(score, 6),
            )
            for asset, score in matches
        ],
    )


# ----------------------------------------------------------------------
#  详情 / 原图 / 更新 / 删除
#  注意：以下含路径参数的端点必须放在具名端点（stats）之后
# ----------------------------------------------------------------------
@router.get("/{image_id}", response_model=dict, summary="影像详情")
def get_image(image_id: UUID, db: Session = Depends(get_db)) -> dict:
    asset = image_repo.get(db, str(image_id))
    if asset is None or not asset.is_public:
        raise HTTPException(status_code=404, detail="公开影像不存在")

    brief = image_repo._to_brief(asset)
    data = brief.to_dict()
    # 缓存过解读结果时一并返回，前端无需再次调用（节省云端调用）
    data["vlm_analysis"] = asset.vlm_analysis
    # 综合解读快照：有则可直接渲染完整解读（含引用），无需重跑检索与生成
    data["vlm_interpretation"] = asset.vlm_interpretation
    return data


@router.get("/{image_id}/raw", summary="影像原图")
def get_image_raw(image_id: UUID, db: Session = Depends(get_db)) -> Response:
    """返回影像文件本体。

    前端 <img src="/api/images/{id}/raw"> 直接引用，
    避免把 base64 塞进 JSON 造成响应体膨胀。
    """
    asset = image_repo.get(db, str(image_id))
    if asset is None or not asset.is_public:
        raise HTTPException(status_code=404, detail="公开影像不存在")
    try:
        data = image_repo.read_bytes(asset)
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=404, detail="影像文件缺失（存储可能被清理）"
        ) from exc

    return Response(
        content=data,
        media_type=asset.mime_type or "image/jpeg",
        headers={"Cache-Control": "no-cache"},
    )


@router.patch("/{image_id}", response_model=ImageItem, summary="更新影像元数据")
def update_image(
    image_id: UUID,
    req: ImageUpdateRequest,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission(Permission.IMAGE_UPDATE)),
) -> ImageItem:
    asset = image_repo.get(db, str(image_id))
    if asset is None:
        raise HTTPException(status_code=404, detail="影像不存在")

    if asset.is_public and not has_permission(user.role, Permission.IMAGE_PUBLISH):
        raise HTTPException(status_code=403, detail="公开影像仅管理员可修改")
    if req.is_public is True and not has_permission(user.role, Permission.IMAGE_PUBLISH):
        raise HTTPException(status_code=403, detail="当前角色无权发布公开影像")

    for field in ("title", "description", "surgery_type", "anatomy_site",
                  "phase", "tags", "source", "is_public"):
        value = getattr(req, field)
        if value is not None:
            setattr(asset, field, value)
    write_audit(
        db, user=user, action="image_update", resource="image",
        resource_id=str(image_id),
        detail={"fields": [name for name in req.model_fields_set]},
    )
    db.commit()

    return ImageItem(**image_repo._to_brief(asset).to_dict())


@router.delete("/{image_id}", response_model=dict, summary="删除影像")
def delete_image(
    image_id: UUID,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission(Permission.IMAGE_DELETE)),
) -> dict:
    asset = image_repo.get(db, str(image_id))
    if asset is None:
        raise HTTPException(status_code=404, detail="影像不存在")
    filename = asset.filename
    # 先记下路径：提交后 asset 已过期，再访问属性会触发对已删除行的懒加载
    stored_path = asset.storage_path
    image_repo.delete(db, asset)
    write_audit(
        db, user=user, action="image_delete", resource="image",
        resource_id=str(image_id), detail={"filename": filename},
    )
    # 顺序：先提交删除行，成功后再删文件。
    # 反过来（先删文件再提交）一旦提交失败，记录还在但文件已丢，
    # /raw 会返回 404「文件缺失」，数据自相矛盾。
    db.commit()
    image_repo.remove_stored_file(stored_path)
    return {"deleted": True, "filename": filename}


# ----------------------------------------------------------------------
#  辅助解读
# ----------------------------------------------------------------------
@router.post("/analyze", response_model=ImageAnalysisResponse, summary="影像辅助解读")
def analyze_image(
    req: ImageAnalyzeRequest,
    db: Session = Depends(get_db),
    user: User = Depends(require_permission(Permission.IMAGE_ANALYZE)),
) -> ImageAnalysisResponse:
    """对影像做辅助解读。

    三段式流程：
      ① VLM 客观描述 → ② 由描述检索文献证据 → ③ LLM 综合解读

    结论部分必须由检索到的证据支撑并带引用编号；
    VLM 描述仅作为「所见」转述，不作为知识来源。

    未配置视觉模型密钥时返回 503 并附申请指引，
    因为「未填密钥」是部署常态而非程序错误。
    """
    if not vision_client.is_configured():
        raise HTTPException(status_code=503, detail=VISION_HINT)

    # ---------- 取图 ----------
    asset = None
    if req.image_id:
        asset = image_repo.get(db, req.image_id)
        if asset is None or (not asset.is_public and not has_permission(user.role, Permission.IMAGE_UPDATE)):
            raise HTTPException(status_code=404, detail="影像不存在或无权访问")
        try:
            image_bytes = image_repo.read_bytes(asset)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="影像文件缺失") from exc
        mime = asset.mime_type or "image/jpeg"
    elif req.image_base64:
        if len(req.image_base64) > 4 * ((vision_client.MAX_IMAGE_BYTES + 2) // 3):
            raise HTTPException(status_code=413, detail="单张影像不能超过 8 MB")
        try:
            image_bytes = base64.b64decode(req.image_base64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise HTTPException(
                status_code=400, detail=f"base64 图片解码失败: {exc}"
            ) from exc
        mime = req.mime_type
        if len(image_bytes) > vision_client.MAX_IMAGE_BYTES:
            raise HTTPException(status_code=413, detail="单张影像不能超过 8 MB")
    else:
        raise HTTPException(
            status_code=400, detail="需提供 image_id 或 image_base64"
        )

    # ---------- 缓存命中 ----------
    # 描述已缓存时不必再调视觉模型，但仍需跑「检索 + 综合」，
    # 因为用户可能换了证据条数或补充了术式背景。
    if asset is not None and req.use_cache and (asset.vlm_analysis or "").strip():
        cached_desc = asset.vlm_analysis or ""
        analysis = image_analysis.synthesize(
            db, cached_desc, top_k=req.top_k, phase=req.phase,
            image_context=req.image_context,
        )
        analysis.description = cached_desc
        analysis.vlm_model = asset.vlm_model or ""
        # 落库快照：下次打开可直接渲染，无需重跑。
        # 解读失败时跳过——否则会把"生成失败"的提示覆盖掉此前成功的结果。
        if analysis.error is None:
            image_repo.save_interpretation(db, asset, _snapshot(analysis))
            db.commit()
        _log_image_analysis(db, asset, analysis, req)
        return _to_response(analysis, image_id=str(asset.id), cached=True)

    # ---------- ① VLM 描述 ----------
    try:
        description, vlm_model, vlm_ms = image_analysis.describe_image(
            image_bytes, mime=mime
        )
    except VisionError as exc:
        raise HTTPException(status_code=502, detail=f"影像解读失败: {exc}") from exc

    # 缓存描述（描述可复用，综合解读每次可用不同参数重跑）
    if asset is not None:
        image_repo.save_analysis(
            db, asset, analysis_text=description, model=vlm_model
        )
        db.commit()

    # ---------- ②③ 检索 + 综合 ----------
    try:
        analysis = image_analysis.synthesize(
            db, description, top_k=req.top_k, phase=req.phase,
            image_context=req.image_context,
        )
    except LLMError as exc:
        raise HTTPException(status_code=503, detail=f"LLM 不可用: {exc}") from exc

    analysis.vlm_model = vlm_model
    analysis.timings["vision_ms"] = vlm_ms

    # 落库快照，便于再次打开时直接渲染
    if asset is not None and analysis.error is None:
        image_repo.save_interpretation(db, asset, _snapshot(analysis))
        db.commit()

    logger.info(
        "影像解读完成: %s | VLM %.1fs | 总计 %.1fs",
        asset.filename if asset else "(即时)",
        vlm_ms / 1000,
        analysis.timings.get("total_ms", 0) / 1000,
    )

    # 写问答日志（mode=image）。
    #
    # 此前只有单轮 /api/qa 落 sys_qa_log，影像解读一次都不记：
    # 管理端的质量监控里 mode 永远没有 image，影像解读的引用准确率、
    # 失败率、耗时分布都无从统计。日志失败不影响解读结果。
    _log_image_analysis(db, asset, analysis, req, fallback_model=vlm_model)

    return _to_response(
        analysis, image_id=str(asset.id) if asset else None, cached=False
    )


def _log_image_analysis(
    db: Session,
    asset,
    analysis: image_analysis.ImageAnalysis,
    req,
    *,
    fallback_model: str = "",
) -> None:
    """把一次影像解读写进 sys_qa_log（mode=image）。

    两条返回路径（缓存命中 / 新解读）都要调用 —— 只在末尾加会漏掉缓存路径，
    而缓存路径恰恰是用户重复查看时最常走的。

    db 为空时（单元测试）直接跳过；写日志失败只告警，不影响解读结果。
    """
    if db is None:
        return
    try:
        qa_logging.log_qa(
            db,
            user=None,
            question=(
                f"[影像解读] {asset.title or asset.filename}"
                if asset is not None else "[影像解读] 即时上传"
            ),
            answer=analysis.interpretation or "",
            citations=analysis.citations,
            invalid_citations=analysis.invalid_citations,
            unsupported_numbers=analysis.unsupported_numbers,
            timings=analysis.timings,
            model=analysis.llm_model or fallback_model,
            phase=getattr(req, "phase", None),
            mode="image",
            has_evidence=analysis.evidence_count > 0,
        )
        db.commit()
    except Exception as log_exc:  # noqa: BLE001
        logger.warning("影像解读日志写入失败（不影响结果）: %s", str(log_exc)[:150])
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass


def _snapshot(analysis: image_analysis.ImageAnalysis) -> dict:
    """把综合解读压成可存 JSONB 的快照。

    字段与 `ImageAnalysisResponse` 对齐，前端取回后可原样渲染
    （引用项需能在 JSON 中往返，因此用 to_dict()）。
    """
    return {
        "description": analysis.description,
        "interpretation": analysis.interpretation,
        "citations": [c.to_dict() for c in analysis.citations],
        "invalid_citations": analysis.invalid_citations,
        "unsupported_numbers": analysis.unsupported_numbers,
        "numeric_warning": analysis.numeric_warning,
        "retrieval_query": analysis.retrieval_query,
        "evidence_count": analysis.evidence_count,
        "vlm_model": analysis.vlm_model,
        "llm_model": analysis.llm_model,
        "related_cases": analysis.related_cases,
        "timings": analysis.timings,
        "disclaimer": analysis.disclaimer,
    }


def _to_response(
    analysis: image_analysis.ImageAnalysis,
    *,
    image_id: str | None,
    cached: bool,
) -> ImageAnalysisResponse:
    """把内部结果转为 API 响应。"""
    # 引用项统一转为 CitationItem（与问答接口保持一致的结构）
    citations: list[CitationItem] = []
    for c in analysis.citations:
        d = c.to_dict()
        citations.append(CitationItem(**d))

    return ImageAnalysisResponse(
        image_id=image_id,
        description=analysis.description,
        interpretation=analysis.interpretation,
        citations=citations,
        invalid_citations=analysis.invalid_citations,
        unsupported_numbers=analysis.unsupported_numbers,
        numeric_warning=analysis.numeric_warning,
        retrieval_query=analysis.retrieval_query,
        evidence_count=analysis.evidence_count,
        has_evidence=analysis.evidence_count > 0,
        vlm_model=analysis.vlm_model,
        llm_model=analysis.llm_model,
        related_cases=analysis.related_cases,
        timings=analysis.timings,
        cached=cached,
        disclaimer=analysis.disclaimer,
    )
