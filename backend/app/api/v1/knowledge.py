"""知识库维护接口（阶段九「知识库维护模块」）。

对应报审表：「提供知识的审核、更新、版本管理和质量评估功能」

端点
----
  文献管理
    GET    /api/admin/kb/documents              文献列表（多维筛选）
    GET    /api/admin/kb/documents/{id}         文献详情（含文本块）
    PUT    /api/admin/kb/documents/{id}/review  审核（通过/驳回）
    PUT    /api/admin/kb/documents/{id}/active  上架/下架
    DELETE /api/admin/kb/documents/{id}         彻底删除
    POST   /api/admin/kb/documents/batch-review 批量审核

  质量评估
    GET    /api/admin/kb/quality                质量报告

  更新（后台任务）
    POST   /api/admin/kb/collect                按预定义检索式采集
    POST   /api/admin/kb/search                 按自定义检索式采集
    POST   /api/admin/kb/vectorize              补齐向量
    GET    /api/admin/kb/tasks                  最近任务
    GET    /api/admin/kb/tasks/{job_id}         任务进度

  版本管理
    GET    /api/admin/kb/batches                更新批次历史
    GET    /api/admin/kb/batches/{id}           批次详情（含该批新增文献）

权限划分
--------
查阅（KB_VIEW）对所有登录用户开放 —— 医师应当能看到知识库内容与质量状况。

**所有写操作仅限管理员**，理由如下：

  审核（KB_REVIEW）  审核决定一篇文献是否进入检索池，会直接影响
                     全系统所有用户的答题依据。这是一项**发布决策**，
                     不是单纯的个人判断，因此收归管理员。
                     （我的第一版接口文档曾写成"医师也能参与"，
                      但权限表的实际配置就是管理员专属；实测 403
                      后确认应以代码为准，故修正此说明。）
  上架/下架（KB_UPDATE）   同样影响全系统语料构成
  彻底删除（KB_DELETE）    不可恢复
  触发更新（KB_UPDATE）    会占用 GPU 并改变语料，需管理员协调

医师的定位是**知识库的使用者与反馈者**：可以查看文献、查看质量报告，
通过问答反馈机制提出改进意见，但不直接改动语料。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_user, require_permission, write_audit, path_uuid
from app.core.logging import get_logger
from app.core.permissions import Permission
from app.db.session import get_db
from app.models.knowledge import Document
from app.models.system import User
from app.repositories import kb_repo
from app.schemas.common import (
    BatchItem,
    BatchListResponse,
    CollectRequest,
    DocumentActiveRequest,
    DocumentBatchReviewRequest,
    DocumentDetail,
    DocumentItem,
    DocumentListResponse,
    DocumentReviewRequest,
    JobProgressResponse,
    QualityReportResponse,
    SearchCollectRequest,
    VectorizeRequest,
)
from app.services import kb_jobs
from app.models.knowledge import ChunkRevision
from sqlalchemy import select, func
from sqlalchemy.dialects.postgresql import aggregate_order_by

logger = get_logger("api.knowledge")

router = APIRouter(tags=["知识库维护"])


# ----------------------------------------------------------------------
#  序列化
# ----------------------------------------------------------------------
def _to_item(doc: Document) -> DocumentItem:
    return DocumentItem(
        id=str(doc.id),
        title=doc.title or "",
        title_en=doc.title_en,
        authors=doc.authors,
        journal=doc.journal,
        year=doc.year,
        volume=doc.volume,
        issue=doc.issue,
        pages=doc.pages,
        doi=doc.doi,
        pmid=doc.pmid,
        language=doc.language,
        doc_type=doc.doc_type,
        evidence_level=doc.evidence_level,
        phase=doc.phase,
        source_db=doc.source_db,
        chunk_count=doc.chunk_count or 0,
        status=doc.status,
        is_reviewed=doc.is_reviewed,
        is_active=doc.is_active,
        has_abstract=doc.abstract is not None,
        batch_id=str(doc.batch_id) if doc.batch_id else None,
        reviewed_by=doc.reviewed_by,
        reviewed_at=doc.reviewed_at.isoformat() if doc.reviewed_at else None,
        review_note=doc.review_note,
        created_at=doc.created_at.isoformat() if doc.created_at else None,
    )


def _batch_to_item(b) -> BatchItem:
    return BatchItem(
        id=str(b.id),
        operation=b.operation,
        status=b.status,
        query=b.query,
        params=b.params,
        documents_created=b.documents_created or 0,
        documents_updated=b.documents_updated or 0,
        documents_skipped=b.documents_skipped or 0,
        chunks_created=b.chunks_created or 0,
        chunks_embedded=b.chunks_embedded or 0,
        chunks_vectorized=b.chunks_vectorized or 0,
        started_at=b.started_at.isoformat() if b.started_at else None,
        finished_at=b.finished_at.isoformat() if b.finished_at else None,
        elapsed_seconds=b.elapsed_seconds,
        errors=b.errors,
        error_message=b.error_message,
        graph_rebuilt=b.graph_rebuilt,
        operator_name=b.operator_name,
        note=b.note,
        created_at=b.created_at.isoformat() if b.created_at else None,
    )


# ======================================================================
#  文献管理
# ======================================================================
@router.get(
    "/admin/kb/documents", response_model=DocumentListResponse,
    summary="文献列表",
)
def list_documents(
    _: Annotated[User, Depends(require_permission(Permission.KB_VIEW))],
    db: Annotated[Session, Depends(get_db)],
    keyword: str | None = Query(None, description="标题/作者/期刊/DOI/PMID"),
    phase: str | None = Query(None, description="preop / intraop / postop / general"),
    evidence_level: str | None = Query(None),
    language: str | None = Query(None),
    year_from: int | None = Query(None, ge=1900, le=2100),
    year_to: int | None = Query(None, ge=1900, le=2100),
    is_reviewed: bool | None = Query(None, description="审核状态筛选"),
    is_active: bool | None = Query(None, description="上架状态筛选"),
    batch_id: str | None = Query(None, description="按更新批次筛选"),
    has_abstract: bool | None = Query(None),
    order_by: str = Query("created_desc"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> DocumentListResponse:
    """文献列表。

    支持按审核状态、上架状态、批次、分期、证据等级等多维筛选，
    这是「审核」功能可用的前提：管理员需要能筛出「未审核」的文献批量处理。
    """
    items, total = kb_repo.list_documents(
        db, keyword=keyword, phase=phase, evidence_level=evidence_level,
        language=language, year_from=year_from, year_to=year_to,
        is_reviewed=is_reviewed, is_active=is_active, batch_id=batch_id,
        has_abstract=has_abstract, limit=limit, offset=offset, order_by=order_by,
    )
    return DocumentListResponse(
        items=[_to_item(x) for x in items], total=total, limit=limit, offset=offset,
    )


@router.get(
    "/admin/kb/documents/{doc_id}", response_model=DocumentDetail,
    summary="文献详情",
)
def get_document(
    doc_id: str,
    _: Annotated[User, Depends(require_permission(Permission.KB_VIEW))],
    db: Annotated[Session, Depends(get_db)],
    _uuid: Annotated[str, Depends(path_uuid("doc_id"))] = None,
) -> DocumentDetail:
    """文献详情，含文本块概要与来源批次。

    文本块只返回前 200 字预览：完整内容可能数千字，
    详情页的目的是核对切分是否合理，不需要全文。
    """
    doc = kb_repo.get_document(db, doc_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="文献不存在")

    base = _to_item(doc)
    batch = kb_repo.batches_for_document(db, doc)
    return DocumentDetail(
        **base.model_dump(),
        abstract=doc.abstract,
        mesh_terms=doc.mesh_terms,
        keywords=doc.keywords,
        publication_types=doc.publication_types,
        error_message=doc.error_message,
        chunks=[
            {
                "id": str(c.id),
                "index": c.chunk_index,
                "section": c.section,
                "page_start": c.page_start,
                "page_end": c.page_end,
                "phase": c.phase,
                "evidence_level": c.evidence_level,
                "char_count": c.char_count,
                "token_count": c.token_count,
                "embedded": c.embedding is not None,
                "preview": (c.content or "")[:200],
            }
            for c in sorted(doc.chunks, key=lambda x: x.chunk_index or 0)
        ],
        batch=_batch_to_item(batch).model_dump() if batch else None,
    )


@router.get('/admin/kb/documents/{doc_id}/revisions', summary='文献历史证据版本')
def document_revisions(
    doc_id: str,
    _: Annotated[User, Depends(require_permission(Permission.KB_VIEW))],
    db: Annotated[Session, Depends(get_db)],
) -> dict:
    import uuid
    try:
        identity = uuid.UUID(doc_id)
    except ValueError:
        raise HTTPException(status_code=422, detail='无效的文献 ID')
    rows = db.execute(select(ChunkRevision.revision_id,
                             func.min(ChunkRevision.created_at).label('archived_at'),
                             func.array_agg(aggregate_order_by(
                                 ChunkRevision.id, ChunkRevision.snapshot['chunk_index'].as_integer())).label('chunk_ids'),
                             func.count().label('chunk_count'))
                       .where(ChunkRevision.document_id == identity)
                       .group_by(ChunkRevision.revision_id)
                       .order_by(func.min(ChunkRevision.created_at).desc())).all()
    return {'document_id': str(identity), 'revisions': [
        {'revision_id': str(row.revision_id), 'archived_at': row.archived_at.isoformat(),
         'chunk_count': row.chunk_count,
         'chunk_ids': [str(identity) for identity in row.chunk_ids]} for row in rows]}


@router.put(
    "/admin/kb/documents/{doc_id}/review", response_model=DocumentItem,
    summary="审核文献",
)
def review_document(
    doc_id: str,
    req: DocumentReviewRequest,
    request: Request,
    user: Annotated[User, Depends(require_permission(Permission.KB_REVIEW))],
    db: Annotated[Session, Depends(get_db)],
    _uuid: Annotated[str, Depends(path_uuid("doc_id"))] = None,
) -> DocumentItem:
    """审核文献。

    驳回时自动下架 —— 审核的目的是过滤不适用语料，
    若驳回后仍在检索池中，审核就失去意义。
    """
    doc = kb_repo.get_document(db, doc_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="文献不存在")

    kb_repo.review_document(
        db, doc, approved=req.approved, reviewer=user.username, note=req.note,
    )
    write_audit(
        db, user=user,
        action="review_approve" if req.approved else "review_reject",
        resource="document", resource_id=doc_id,
        detail={"title": (doc.title or "")[:80], "note": req.note},
        request=request,
    )
    db.commit()
    return _to_item(doc)


@router.post(
    "/admin/kb/documents/batch-review", response_model=dict,
    summary="批量审核",
)
def batch_review(
    req: DocumentBatchReviewRequest,
    request: Request,
    user: Annotated[User, Depends(require_permission(Permission.KB_REVIEW))],
    db: Annotated[Session, Depends(get_db)],
) -> dict:
    """批量审核。

    审核是逐篇的内容判断，但语料常按批采集（同一检索式来源质量相近），
    因此批量操作有实际价值：管理员可先抽查几篇，再整批处理。
    """
    count = kb_repo.batch_review(
        db, req.document_ids, approved=req.approved, reviewer=user.username,
    )
    write_audit(
        db, user=user,
        action="batch_review_approve" if req.approved else "batch_review_reject",
        resource="document",
        detail={"count": count, "approved": req.approved},
        request=request,
    )
    db.commit()
    return {
        "reviewed": count,
        "approved": req.approved,
        "message": f"已{'通过' if req.approved else '驳回'} {count} 篇文献",
    }


@router.put(
    "/admin/kb/documents/{doc_id}/active", response_model=DocumentItem,
    summary="上架/下架",
)
def set_active(
    doc_id: str,
    req: DocumentActiveRequest,
    request: Request,
    user: Annotated[User, Depends(require_permission(Permission.KB_UPDATE))],
    db: Annotated[Session, Depends(get_db)],
    _uuid: Annotated[str, Depends(path_uuid("doc_id"))] = None,
) -> DocumentItem:
    """上架或下架文献（软删除）。

    下架后该文献不再被检索到，但历史问答的引用记录仍然有效 ——
    这比彻底删除更安全，也是默认推荐的操作。
    """
    doc = kb_repo.get_document(db, doc_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="文献不存在")

    kb_repo.set_active(db, doc, req.is_active)
    write_audit(
        db, user=user,
        action="activate" if req.is_active else "deactivate",
        resource="document", resource_id=doc_id,
        detail={"title": (doc.title or "")[:80]},
        request=request,
    )
    db.commit()
    return _to_item(doc)


@router.delete(
    "/admin/kb/documents/{doc_id}", response_model=dict, summary="彻底删除文献",
)
def delete_document(
    doc_id: str,
    request: Request,
    user: Annotated[User, Depends(require_permission(Permission.KB_DELETE))],
    db: Annotated[Session, Depends(get_db)],
    _uuid: Annotated[str, Depends(path_uuid("doc_id"))] = None,
) -> dict:
    """彻底删除文献及其文本块。

    ⚠️ **不可恢复**。界面上需要二次确认。
    图数据库中由该文献抽取的实体不会自动删除，图谱需重建才能反映。
    """
    doc = kb_repo.get_document(db, doc_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="文献不存在")

    title = (doc.title or "")[:80]
    chunks = kb_repo.delete_document(db, doc)
    write_audit(
        db, user=user, action="delete", resource="document", resource_id=doc_id,
        detail={"title": title, "chunks_deleted": chunks},
        request=request,
    )
    db.commit()
    return {
        "deleted": True,
        "chunks_deleted": chunks,
        "message": f"已删除文献及其 {chunks} 个文本块",
        "warning": "图数据库中的相关实体未删除，重建图谱后才会生效",
    }


# ======================================================================
#  质量评估
# ======================================================================
@router.get(
    "/admin/kb/quality", response_model=QualityReportResponse,
    summary="知识库质量评估",
)
def quality_report(
    _: Annotated[User, Depends(require_permission(Permission.KB_VIEW))],
    db: Annotated[Session, Depends(get_db)],
) -> QualityReportResponse:
    """知识库质量评估。

    返回规模、时效性、证据结构、分期覆盖、语种结构、异常项与自动生成的维护建议。
    `health` 是各项指标的均值，**仅用于快速判断，不参与任何检索决策**。
    """
    return QualityReportResponse(**kb_repo.quality_report(db))


# ======================================================================
#  更新任务
# ======================================================================
def _start_batch(
    db: Session, db_user: User, operation: str, *,
    query: str | None, params: dict,
) -> object:
    """创建批次记录并提交后台任务。"""
    batch = kb_repo.create_batch(
        db, operation=operation, query=query, params=params,
        operator_id=str(db_user.id), operator_name=db_user.username,
    )
    db.commit()
    return batch


@router.post(
    "/admin/kb/collect", response_model=JobProgressResponse,
    status_code=status.HTTP_202_ACCEPTED, summary="按预定义检索式采集",
)
def collect(
    req: CollectRequest,
    request: Request,
    user: Annotated[User, Depends(require_permission(Permission.KB_UPDATE))],
    db: Annotated[Session, Depends(get_db)],
) -> JobProgressResponse:
    """按预定义检索式批量采集 PubMed 文献（后台执行）。

    返回 **202 Accepted** 与 task_id，前端据此轮询进度。

    采集 + 向量化是数十分钟级的操作，因此不在请求内同步完成。
    同一时刻只允许一个重任务，避免并发争抢 GPU。
    """
    if kb_jobs.registry.is_busy():
        active = kb_jobs.registry.active_job
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"已有知识库更新任务正在执行（{str(active)[:8]}），请等待其完成",
        )

    params = req.model_dump()
    mode_desc = {"default": "主检索式", "supplement": "补充检索式（中文+近三年）", "all": "全部检索式"}.get(
        req.mode, req.mode,
    )
    batch = _start_batch(
        db, user, kb_jobs.JOB_COLLECT, query=f"（预定义检索式集合：{mode_desc}）", params=params,
    )
    write_audit(
        db, user=user, action="kb_collect", resource="batch",
        resource_id=str(batch.id), detail=params, request=request,
    )
    db.commit()

    progress = kb_jobs.run_collect(
        batch_id=str(batch.id), mode=req.mode, max_total=req.max_total,
        mindate=req.mindate, embed=req.embed, batch_size=req.batch_size,
    )
    return JobProgressResponse(**progress)


@router.post(
    "/admin/kb/search", response_model=JobProgressResponse,
    status_code=status.HTTP_202_ACCEPTED, summary="按自定义检索式采集",
)
def search_collect(
    req: SearchCollectRequest,
    request: Request,
    user: Annotated[User, Depends(require_permission(Permission.KB_UPDATE))],
    db: Annotated[Session, Depends(get_db)],
) -> JobProgressResponse:
    """按自定义 PubMed 检索式采集（后台执行）。

    适用于针对某个具体主题补充文献，例如发现系统对某类问题回答不佳时。
    """
    if kb_jobs.registry.is_busy():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="已有知识库更新任务正在执行，请等待其完成",
        )

    params = req.model_dump()
    batch = _start_batch(db, user, kb_jobs.JOB_SEARCH, query=req.query, params=params)
    write_audit(
        db, user=user, action="kb_search", resource="batch",
        resource_id=str(batch.id), detail={"query": req.query, "limit": req.limit},
        request=request,
    )
    db.commit()

    progress = kb_jobs.run_search(
        batch_id=str(batch.id), query=req.query, limit=req.limit,
        mindate=req.mindate, embed=req.embed, batch_size=req.batch_size,
    )
    return JobProgressResponse(**progress)


@router.post(
    "/admin/kb/vectorize", response_model=JobProgressResponse,
    status_code=status.HTTP_202_ACCEPTED, summary="补齐向量",
)
def vectorize(
    req: VectorizeRequest,
    request: Request,
    user: Annotated[User, Depends(require_permission(Permission.KB_UPDATE))],
    db: Annotated[Session, Depends(get_db)],
) -> JobProgressResponse:
    """为未向量化的文本块补齐向量（后台执行）。

    入库时若中断，会有块存在但没有向量；这些块在向量检索路径下
    永远检索不到。本接口用于断点续跑。
    """
    if kb_jobs.registry.is_busy():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="已有知识库更新任务正在执行，请等待其完成",
        )

    params = req.model_dump()
    batch = _start_batch(db, user, kb_jobs.JOB_VECTORIZE, query=None, params=params)
    write_audit(
        db, user=user, action="kb_vectorize", resource="batch",
        resource_id=str(batch.id), detail=params, request=request,
    )
    db.commit()

    progress = kb_jobs.run_vectorize(batch_id=str(batch.id), batch_size=req.batch_size)
    return JobProgressResponse(**progress)


@router.get(
    "/admin/kb/tasks", response_model=list[JobProgressResponse],
    summary="最近更新任务",
)
def list_tasks(
    _: Annotated[User, Depends(require_permission(Permission.KB_VIEW))],
    limit: int = Query(20, ge=1, le=50),
) -> list[JobProgressResponse]:
    """最近的后台任务进度（含已完成的）。"""
    return [
        JobProgressResponse(**j.to_dict())
        for j in kb_jobs.registry.list_recent(limit=limit)
    ]


@router.get(
    "/admin/kb/tasks/{job_id}", response_model=JobProgressResponse,
    summary="任务进度",
)
def get_task(
    job_id: str,
    _: Annotated[User, Depends(require_permission(Permission.KB_VIEW))],
) -> JobProgressResponse:
    """查询单个任务进度。前端每 2 秒轮询此接口。"""
    job = kb_jobs.registry.get(job_id)
    if job is None:
        raise HTTPException(
            status_code=404,
            detail="任务不存在（可能因后端重启而丢失，请查看更新历史）",
        )
    return JobProgressResponse(**job.to_dict())


# ======================================================================
#  版本管理
# ======================================================================
@router.get(
    "/admin/kb/batches", response_model=BatchListResponse, summary="更新历史",
)
def list_batches(
    _: Annotated[User, Depends(require_permission(Permission.KB_VIEW))],
    db: Annotated[Session, Depends(get_db)],
    operation: str | None = Query(None, description="collect / search / vectorize"),
    status: str | None = Query(None, description="pending / running / success / failed"),
    limit: int = Query(30, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> BatchListResponse:
    """更新批次历史（版本管理）。

    回答「知识库是什么时候、由谁、用什么检索式、更新了多少内容」。
    """
    items, total = kb_repo.list_batches(
        db, operation=operation, status=status, limit=limit, offset=offset,
    )
    return BatchListResponse(items=[_batch_to_item(x) for x in items], total=total)


@router.get(
    "/admin/kb/batches/{batch_id}", response_model=dict, summary="批次详情",
)
def get_batch(
    batch_id: str,
    _: Annotated[User, Depends(require_permission(Permission.KB_VIEW))],
    db: Annotated[Session, Depends(get_db)],
    doc_limit: int = Query(20, ge=1, le=200),
    _uuid: Annotated[str, Depends(path_uuid("batch_id"))] = None,
) -> dict:
    """批次详情，含该批次引入的文献列表。

    支持按批次追溯来源，也是「回滚」操作的依据 ——
    管理员可据此确认该批新增了哪些文献，再决定是否下架或删除。
    """
    batch = kb_repo.get_batch(db, batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="批次不存在")

    docs, doc_total = kb_repo.list_documents(
        db, batch_id=batch_id, limit=doc_limit, offset=0,
    )
    return {
        "batch": _batch_to_item(batch).model_dump(),
        "documents": [_to_item(d).model_dump() for d in docs],
        "documents_total": doc_total,
    }
