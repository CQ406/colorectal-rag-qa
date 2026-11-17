"""典型病例库 API。

病例默认只允许读取公开、脱敏记录；写操作需要相应权限并写入审计日志。
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID
from datetime import datetime, timezone
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import require_permission, write_audit
from app.core.permissions import Permission, has_permission
from app.db.session import get_db
from app.models.knowledge import CaseRecord, ImageAsset
from app.models.system import AuditLog, User
from app.repositories import case_repo
from app.schemas.common import (
    CaseCreateRequest,
    CaseItem,
    CaseListResponse,
    CaseSearchRequest,
    CaseUpdateRequest,
    UUIDString,
)

router = APIRouter(prefix="/cases", tags=["典型病例库"])

SOURCE_FIELDS = ("source_title", "source_url", "source_license", "source_note")


def _validate_source(data: dict, *, publishing: bool) -> None:
    url = data.get("source_url")
    if url:
        try:
            parsed = urlsplit(url)
            valid = parsed.scheme in {"http", "https"} and parsed.hostname and not parsed.username and not parsed.password
            _ = parsed.port
        except ValueError:
            valid = False
        if not valid:
            raise HTTPException(status_code=422, detail="来源链接必须是有效的 HTTP/HTTPS 原文地址")
    if publishing:
        missing = [field for field in (*SOURCE_FIELDS, "review_note") if not str(data.get(field) or "").strip()]
        if missing:
            raise HTTPException(status_code=422, detail=f"发布前必须填写来源、许可、整理依据和审核意见: {', '.join(missing)}")


def _source_snapshot(case: CaseRecord) -> dict:
    return {name: getattr(case, name, None) for name in (*SOURCE_FIELDS, "source_doi", "review_note")}


def _validate_image_links(
    db: Session, image_ids: list[str] | None, *, require_public: bool,
) -> None:
    """病例链接必须引用存在的影像；公开病例只能关联已发布影像。"""
    ids = list(dict.fromkeys(image_ids or []))
    if not ids:
        return
    statement = select(ImageAsset.id).where(ImageAsset.id.in_(ids))
    if require_public:
        statement = statement.where(ImageAsset.is_public.is_(True))
    existing = {str(value) for value in db.execute(statement).scalars().all()}
    missing = sorted(set(ids) - existing)
    if missing:
        reason = "不存在" if not require_public else "不存在或尚未公开"
        raise HTTPException(status_code=422, detail=f"关联影像 {reason}: {', '.join(missing)}")


def _item(case: CaseRecord, similarity: float | None = None) -> CaseItem:
    return CaseItem(
        id=str(case.id),
        case_code=case.case_code,
        title=case.title,
        age=case.age,
        sex=case.sex,
        diagnosis=case.diagnosis,
        surgery=case.surgery,
        phase=case.phase,
        history=case.history,
        findings=case.findings,
        operative_note=case.operative_note,
        prognosis=case.prognosis,
        complications=case.complications or [],
        tags=case.tags or [],
        image_ids=case.image_ids or [],
        source_title=case.source_title,
        source_url=case.source_url,
        source_doi=case.source_doi,
        source_license=case.source_license,
        source_note=case.source_note,
        prepared_by=case.prepared_by,
        reviewed_by=case.reviewed_by,
        reviewed_at=case.reviewed_at.isoformat() if case.reviewed_at else None,
        review_note=case.review_note,
        is_public=case.is_public,
        has_embedding=case.embedding is not None,
        similarity=round(similarity, 6) if similarity is not None else None,
        created_at=case.created_at.isoformat() if case.created_at else None,
        updated_at=case.updated_at.isoformat() if case.updated_at else None,
    )


@router.get("", response_model=CaseListResponse, summary="病例列表")
def list_cases(
    keyword: str | None = Query(None, max_length=200),
    phase: str | None = Query(None),
    surgery: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> CaseListResponse:
    rows, total = case_repo.list_cases(
        db, keyword=keyword, phase=phase, surgery=surgery,
        limit=limit, offset=offset,
    )
    return CaseListResponse(
        items=[_item(row) for row in rows], total=total,
        limit=limit, offset=offset,
    )


@router.get("/manage", response_model=CaseListResponse, summary="病例草稿与管理列表")
def manage_cases(
    _: Annotated[User, Depends(require_permission(Permission.CASE_UPDATE))],
    db: Annotated[Session, Depends(get_db)],
    keyword: str | None = Query(None, max_length=200),
    phase: str | None = Query(None),
    surgery: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> CaseListResponse:
    """给有维护权限的用户显示公开病例和未发布草稿。"""
    rows, total = case_repo.list_cases(
        db, keyword=keyword, phase=phase, surgery=surgery,
        limit=limit, offset=offset, include_private=True,
    )
    return CaseListResponse(
        items=[_item(row) for row in rows], total=total,
        limit=limit, offset=offset,
    )


@router.post("/search", response_model=CaseListResponse, summary="相似病例检索")
def search_cases(req: CaseSearchRequest, db: Session = Depends(get_db)) -> CaseListResponse:
    rows = case_repo.search_similar(
        db, req.query, top_k=req.top_k, phase=req.phase, surgery=req.surgery,
    )
    return CaseListResponse(
        items=[_item(case, score) for case, score in rows],
        total=len(rows), limit=req.top_k, offset=0,
    )


@router.get("/manage/{case_id}/reviews", response_model=list[dict], summary="病例审核历史")
def case_reviews(
    case_id: UUID,
    user: Annotated[User, Depends(require_permission(Permission.CASE_UPDATE))],
    db: Annotated[Session, Depends(get_db)],
) -> list[dict]:
    rows = db.execute(
        select(AuditLog).where(AuditLog.resource == "case", AuditLog.resource_id == str(case_id),
                               AuditLog.action.in_(["case_publish", "case_unpublish"]))
        .order_by(AuditLog.created_at.desc()).limit(100)
    ).scalars().all()
    return [{"decision": row.action, "username": row.username,
             "created_at": row.created_at.isoformat(), "detail": row.detail} for row in rows]


@router.get("/{case_id}", response_model=CaseItem, summary="病例详情")
def get_case(case_id: UUID, db: Session = Depends(get_db)) -> CaseItem:
    case = case_repo.get_public(db, str(case_id))
    if case is None:
        raise HTTPException(status_code=404, detail="公开病例不存在")
    return _item(case)


@router.post("", response_model=CaseItem, status_code=status.HTTP_201_CREATED, summary="新增病例")
def create_case(
    req: CaseCreateRequest,
    request: Request,
    user: Annotated[User, Depends(require_permission(Permission.CASE_UPDATE))],
    db: Annotated[Session, Depends(get_db)],
) -> CaseItem:
    if req.is_public and not has_permission(user.role, Permission.CASE_PUBLISH):
        raise HTTPException(status_code=403, detail="仅管理员可发布公开病例")
    _validate_source(req.model_dump(), publishing=req.is_public)
    _validate_image_links(db, req.image_ids, require_public=req.is_public)
    if case_repo.get_by_code(db, req.case_code):
        raise HTTPException(status_code=409, detail="病例编号已存在")
    metadata = req.model_dump()
    metadata["prepared_by"] = user.username
    if req.is_public:
        metadata.update(reviewed_by=user.username, reviewed_at=datetime.now(timezone.utc))
    case = case_repo.create(db, **metadata)
    write_audit(db, user=user, action="case_create", resource="case",
                resource_id=str(case.id), detail={"case_code": case.case_code}, request=request)
    if req.is_public:
        write_audit(db, user=user, action="case_publish", resource="case",
                    resource_id=str(case.id), detail=_source_snapshot(case), request=request)
    db.commit()
    return _item(case)


@router.patch("/{case_id}", response_model=CaseItem, summary="更新病例")
def update_case(
    case_id: UUID,
    req: CaseUpdateRequest,
    request: Request,
    user: Annotated[User, Depends(require_permission(Permission.CASE_UPDATE))],
    db: Annotated[Session, Depends(get_db)],
) -> CaseItem:
    case = case_repo.get(db, str(case_id))
    if case is None:
        raise HTTPException(status_code=404, detail="病例不存在")
    data = req.model_dump(exclude_unset=True)
    for required in ("case_code", "title", "phase"):
        if required in data and data[required] is None:
            raise HTTPException(status_code=422, detail=f"{required} 不能为空")
    if data.get("is_public") is True and not has_permission(user.role, Permission.CASE_PUBLISH):
        raise HTTPException(status_code=403, detail="仅管理员可发布公开病例")
    if case.is_public and not has_permission(user.role, Permission.CASE_PUBLISH):
        raise HTTPException(status_code=403, detail="公开病例仅管理员可修改")
    if data.get("is_public") is False and not has_permission(user.role, Permission.CASE_PUBLISH):
        raise HTTPException(status_code=403, detail="仅管理员可取消公开发布")
    public_after = data.get("is_public", case.is_public)
    if public_after is None:
        raise HTTPException(status_code=422, detail="is_public 不能为空")
    source_data = {**_source_snapshot(case), **data}
    _validate_source(source_data, publishing=public_after)
    public_before = case.is_public
    if public_after:
        data.update(reviewed_by=user.username, reviewed_at=datetime.now(timezone.utc))
    _validate_image_links(
        db, data.get("image_ids", case.image_ids or []),
        require_public=data.get("is_public", case.is_public),
    )
    if "case_code" in data:
        other = case_repo.get_by_code(db, data["case_code"])
        if other is not None and other.id != case.id:
            raise HTTPException(status_code=409, detail="病例编号已存在")
    case_repo.update(db, case, **data)
    write_audit(db, user=user, action="case_update", resource="case",
                resource_id=str(case_id), detail={"fields": list(data)}, request=request)
    if public_after or public_before:
        write_audit(db, user=user, action="case_publish" if public_after else "case_unpublish", resource="case",
                    resource_id=str(case_id), detail=_source_snapshot(case), request=request)
    db.commit()
    return _item(case)


@router.delete("/{case_id}", response_model=dict, summary="删除病例")
def delete_case(
    case_id: UUID,
    request: Request,
    user: Annotated[User, Depends(require_permission(Permission.CASE_DELETE))],
    db: Annotated[Session, Depends(get_db)],
) -> dict:
    case = case_repo.get(db, str(case_id))
    if case is None:
        raise HTTPException(status_code=404, detail="病例不存在")
    code = case.case_code
    case_repo.delete(db, case)
    write_audit(db, user=user, action="case_delete", resource="case",
                resource_id=str(case_id), detail={"case_code": code}, request=request)
    db.commit()
    return {"deleted": True, "case_code": code}
