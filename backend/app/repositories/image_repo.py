"""影像知识库数据访问。

职责：影像的存取、元数据管理与去重。
     - 文件本体走 StorageBackend（本地文件系统，见 services/storage.py）
     - 元数据与 VLM 解读结果存 kb_image 表
     - 按文件 SHA256 去重，避免同一张图重复上传占空间

为什么单独抽出仓储层：API 层只做参数校验与响应组装，
数据访问集中在仓储层便于单独测试，也避免 SQL 散落在路由函数中。
"""

from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.logging import get_logger
from app.models.knowledge import ImageAsset
from app.services import storage as storage_mod
from app.services.vision_client import SUPPORTED_MIME, VisionError
from app.services.image_features import FEATURE_VERSION

logger = get_logger("repositories.image_repo")

# 图片尺寸读取上限（避免异常大图占用内存）
_PIL_MAX_PIXELS = 50_000_000


@dataclass
class ImageBrief:
    """影像元数据（列表用，不含解读正文）。"""

    id: str
    filename: str
    title: str | None
    description: str | None
    surgery_type: str | None
    anatomy_site: str | None
    phase: str
    tags: list | None
    mime_type: str | None
    file_size: int | None
    width: int | None
    height: int | None
    source: str | None
    is_public: bool
    has_analysis: bool
    vlm_model: str | None
    vlm_analyzed_at: datetime | None
    url: str
    created_at: datetime | None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "filename": self.filename,
            "title": self.title,
            "description": self.description,
            "surgery_type": self.surgery_type,
            "anatomy_site": self.anatomy_site,
            "phase": self.phase,
            "tags": self.tags or [],
            "mime_type": self.mime_type,
            "file_size": self.file_size,
            "width": self.width,
            "height": self.height,
            "source": self.source,
            "is_public": self.is_public,
            "has_analysis": self.has_analysis,
            "vlm_model": self.vlm_model,
            "vlm_analyzed_at": (
                self.vlm_analyzed_at.isoformat() if self.vlm_analyzed_at else None
            ),
            "url": self.url,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


def _to_brief(asset: ImageAsset) -> ImageBrief:
    # 影像访问地址指向本模块的原图端点，而不是 storage.url_for。
    #
    # 原因：storage.url_for 生成的是通用文件下载路径（/files/{key}），
    # 该端点当前未实现；而 /api/images/{id}/raw 已实现且带缓存头，
    # 前端可直接用于 <img src>，无需把图片编码进 JSON。
    prefix = settings.app.api_prefix.rstrip("/")
    return ImageBrief(
        id=str(asset.id),
        filename=asset.filename,
        title=asset.title,
        description=asset.description,
        surgery_type=asset.surgery_type,
        anatomy_site=asset.anatomy_site,
        phase=asset.phase,
        tags=asset.tags,
        mime_type=asset.mime_type,
        file_size=asset.file_size,
        width=asset.width,
        height=asset.height,
        source=asset.source,
        is_public=asset.is_public,
        has_analysis=bool(asset.vlm_analysis),
        vlm_model=asset.vlm_model,
        vlm_analyzed_at=asset.vlm_analyzed_at,
        url=f"{prefix}/images/{asset.id}/raw",
        created_at=asset.created_at,
    )


def image_dimensions(data: bytes) -> tuple[int | None, int | None]:
    """读取图片宽高。Pillow 不可用时返回 (None, None)。"""
    try:
        from PIL import Image

        Image.MAX_IMAGE_PIXELS = _PIL_MAX_PIXELS
        with Image.open(io.BytesIO(data)) as im:
            return im.width, im.height
    except Exception as exc:  # noqa: BLE001 - 尺寸读不到不应阻断上传
        logger.warning("读取图片尺寸失败: %s", str(exc)[:120])
        return None, None


def create(
    db: Session,
    *,
    filename: str,
    data: bytes,
    mime_type: str | None = None,
    title: str | None = None,
    description: str | None = None,
    surgery_type: str | None = None,
    anatomy_site: str | None = None,
    phase: str = "intraop",
    tags: list | None = None,
    source: str | None = None,
    is_public: bool = False,
) -> tuple[ImageAsset, bool]:
    """保存影像。

    Returns:
        (影像记录, 是否为重复文件)。重复时返回已有记录且不重复写盘。
    """
    if not data:
        raise VisionError("上传内容为空")

    if mime_type and mime_type not in SUPPORTED_MIME:
        raise VisionError(
            f"不支持的图片格式 {mime_type}，"
            f"支持：{'、'.join(sorted(set(SUPPORTED_MIME)))}"
        )

    digest = hashlib.sha256(data).hexdigest()

    # 去重：同一张图重复上传时直接返回已有记录
    existing = db.execute(
        select(ImageAsset).where(ImageAsset.file_hash == digest)
    ).scalar_one_or_none()
    if existing is not None:
        logger.info("影像已存在（SHA256 重复）: %s", existing.filename)
        return existing, True

    ext = SUPPORTED_MIME.get(mime_type or "image/jpeg", "jpg")
    key = f"images/{digest[:2]}/{digest}.{ext}"
    stored = storage_mod.get_storage().save(data, key)
    width, height = image_dimensions(data)
    feature_vector = None
    try:
        from app.services.image_features import extract_feature

        feature_vector = extract_feature(data)
    except Exception as exc:  # noqa: BLE001 - 特征是可选增强，不阻塞上传
        logger.warning("影像特征生成失败，上传仍继续: %s", str(exc)[:120])

    asset = ImageAsset(
        filename=filename or f"image.{ext}",
        storage_path=stored.key,
        file_hash=digest,
        mime_type=mime_type or "image/jpeg",
        file_size=len(data),
        width=width,
        height=height,
        title=title or Path(filename).stem,
        description=description,
        surgery_type=surgery_type,
        anatomy_site=anatomy_site,
        phase=phase,
        tags=tags or [],
        feature_vector=feature_vector,
        feature_model=FEATURE_VERSION if feature_vector is not None else None,
        feature_updated_at=datetime.now(timezone.utc) if feature_vector is not None else None,
        source=source,
        is_public=is_public,
    )
    db.add(asset)
    db.flush()
    logger.info(
        "已保存影像 %s (%dx%d, %.1f KB)",
        asset.filename, width or 0, height or 0, len(data) / 1024,
    )
    return asset, False


def get(db: Session, image_id: str) -> ImageAsset | None:
    return db.get(ImageAsset, image_id)


def list_images(
    db: Session,
    *,
    phase: str | None = None,
    surgery_type: str | None = None,
    keyword: str | None = None,
    limit: int = 50,
    offset: int = 0,
    include_private: bool = False,
) -> tuple[list[ImageBrief], int]:
    """分页查询影像列表。

    Returns:
        (当前页列表, 总条数)
    """
    # 列表端点是公开读取接口，只展示已确认公开的脱敏样例。
    stmt = select(ImageAsset)
    count_stmt = select(func.count()).select_from(ImageAsset)
    if not include_private:
        stmt = stmt.where(ImageAsset.is_public.is_(True))
        count_stmt = count_stmt.where(ImageAsset.is_public.is_(True))

    if phase:
        stmt = stmt.where(ImageAsset.phase == phase)
        count_stmt = count_stmt.where(ImageAsset.phase == phase)
    if surgery_type:
        stmt = stmt.where(ImageAsset.surgery_type == surgery_type)
        count_stmt = count_stmt.where(ImageAsset.surgery_type == surgery_type)
    if keyword:
        pattern = f"%{keyword}%"
        cond = (
            ImageAsset.title.ilike(pattern)
            | ImageAsset.description.ilike(pattern)
            | ImageAsset.anatomy_site.ilike(pattern)
            | ImageAsset.filename.ilike(pattern)
        )
        stmt = stmt.where(cond)
        count_stmt = count_stmt.where(cond)

    total = db.execute(count_stmt).scalar_one()
    stmt = stmt.order_by(ImageAsset.created_at.desc()).limit(limit).offset(offset)
    rows = db.execute(stmt).scalars().all()
    return [_to_brief(a) for a in rows], total


def read_bytes(asset: ImageAsset) -> bytes:
    """读取影像文件的原始字节。"""
    return storage_mod.get_storage().read(asset.storage_path)


def ensure_feature(db: Session, asset: ImageAsset) -> list[float] | None:
    """为历史影像懒计算特征并写回数据库。"""
    if asset.feature_vector is not None and asset.feature_model == FEATURE_VERSION:
        return list(asset.feature_vector)
    try:
        from app.services.image_features import extract_feature

        vector = extract_feature(read_bytes(asset))
        asset.feature_vector = vector
        asset.feature_model = FEATURE_VERSION
        asset.feature_updated_at = datetime.now(timezone.utc)
        db.flush()
        return vector
    except Exception as exc:  # noqa: BLE001
        logger.warning("影像特征补算失败 %s: %s", asset.filename, str(exc)[:120])
        return None


def similar_images(
    db: Session,
    feature_vector: list[float],
    *,
    top_k: int = 10,
    exclude_id: str | None = None,
) -> list[tuple[ImageAsset, float]]:
    """使用 pgvector 余弦相似度检索影像。"""
    distance = ImageAsset.feature_vector.cosine_distance(feature_vector).label("distance")
    stmt = (
        select(ImageAsset, distance)
        .where(ImageAsset.feature_vector.is_not(None), ImageAsset.is_public.is_(True),
               ImageAsset.feature_model == FEATURE_VERSION)
        .order_by(distance)
        .limit(top_k + (1 if exclude_id else 0))
    )
    if exclude_id:
        stmt = stmt.where(ImageAsset.id != exclude_id)
    rows = db.execute(stmt).all()
    return [(row[0], max(0.0, min(1.0, 1.0 - float(row[1])))) for row in rows[:top_k]]


def save_analysis(
    db: Session,
    asset: ImageAsset,
    *,
    analysis_text: str,
    model: str,
) -> None:
    """缓存 VLM 解读结果，避免重复调用（云端调用有成本与延迟）。"""
    asset.vlm_analysis = analysis_text
    asset.vlm_model = model
    asset.vlm_analyzed_at = datetime.now(timezone.utc)
    db.flush()
    logger.info("已缓存影像解读结果: %s（模型 %s）", asset.filename, model)


def save_interpretation(db: Session, asset: ImageAsset, payload: dict) -> None:
    """缓存「综合解读」快照（检索证据 + LLM 结论 + 引用）。

    与 `save_analysis` 的区别：后者只存视觉模型的客观描述，
    本函数存的是用户真正要看的辅助解读。持久化后可随时取回，
    不必每次重新跑 15~20 秒的检索与生成。
    """
    asset.vlm_interpretation = payload
    db.flush()
    logger.info(
        "已缓存综合解读: %s（引用 %d 条）",
        asset.filename,
        len(payload.get("citations") or []),
    )


def delete(db: Session, asset: ImageAsset) -> bool:
    """删除影像记录（**不删文件**）。

    为什么拆开：先删文件再提交数据库，一旦提交失败，库里记录还在、
    文件却已丢失，`/raw` 变成 404「文件缺失」。
    正确顺序是「先提交删除行，成功后再删文件」——失败最多留下一个
    可回收的孤儿文件，不会出现"记录指向不存在的文件"。

    保留本函数名以兼容既有调用；文件由调用方在提交后调用
    `remove_stored_file()` 删除。
    """
    db.delete(asset)
    return True


def remove_stored_file(storage_path: str) -> None:
    """删除影像的物理文件。失败只告警（孤儿文件可后续清理）。"""
    if not storage_path:
        return
    try:
        storage_mod.get_storage().delete(storage_path)
    except Exception as exc:  # noqa: BLE001
        logger.warning("删除影像文件失败（孤儿文件，可后续清理）: %s", str(exc)[:120])


def stats(db: Session, *, include_private: bool = False) -> dict:
    """影像库统计（供系统状态页展示）。"""
    conditions = [] if include_private else [ImageAsset.is_public.is_(True)]
    total = db.execute(select(func.count()).select_from(ImageAsset).where(*conditions)).scalar_one()
    analyzed = db.execute(
        select(func.count()).select_from(ImageAsset)
        .where(*conditions, ImageAsset.vlm_analysis.is_not(None))
    ).scalar_one()
    by_phase = dict(
        db.execute(
            select(ImageAsset.phase, func.count()).where(*conditions).group_by(ImageAsset.phase)
        ).all()
    )
    total_bytes = db.execute(
        select(func.coalesce(func.sum(ImageAsset.file_size), 0)).where(*conditions)
    ).scalar_one()
    return {
        "total": total,
        "analyzed": analyzed,
        "by_phase": by_phase,
        "total_bytes": int(total_bytes),
    }
