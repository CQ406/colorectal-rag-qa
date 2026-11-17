"""检索接口。

阶段三起使用「多路召回 + RRF 融合 + 重排」的完整链路（app/services/hybrid_search.py）。
`recall_mode` 参数可切换召回策略，用于消融实验：

    hybrid   向量 + 关键词融合（默认，推荐）
    vector   仅向量语义召回
    keyword  仅关键词召回
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.repositories import search_repo
from app.schemas.common import SearchRequest, SearchResponse
from app.services import hybrid_search
from app.services.search_service import EmbeddingInvalidError

router = APIRouter(tags=["检索"])


@router.post("/search", response_model=SearchResponse, summary="多路召回语义检索")
def search(req: SearchRequest, db: Session = Depends(get_db)) -> SearchResponse:
    """根据查询文本检索知识库，返回带来源与页码的文本块。

    参数要点：
    - **recall_mode**: `hybrid` 多路融合（默认）/ `vector` 仅向量 / `keyword` 仅关键词
    - **phase**: 按围术期分期过滤，可显著降低跨期噪声
    - **use_rerank**: 是否用交叉编码器精排（更准，约增加 0.5~1 秒）
    - **use_evidence_boost**: 是否让指南/系统评价优先于病例报告

    响应中的 `recall_paths` 会列出各路的召回条数与耗时，
    便于分析每一路对最终结果的贡献（消融实验用）。
    """
    try:
        return hybrid_search.search(db, req)
    except EmbeddingInvalidError as exc:
        # 向量模型数值异常（NaN），已自动尝试 fp32 重载仍失败
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        # 模型目录缺失，属环境配置问题
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=500, detail=f"检索失败: {type(exc).__name__}: {exc}"
        ) from exc


@router.get("/stats", summary="知识库统计")
def stats(db: Session = Depends(get_db)) -> dict:
    """返回知识库规模与元数据覆盖率，用于确认入库质量。

    **口径说明**：本接口只统计**上架**文献及其文本块，因为检索链路只使用上架文献。
    此前误用全库计数，导致报告的块数远大于实际可检索量
    （实测报 8,849，可检索仅 3,828）；如需全库进度请查 `/stats/library`。

    `phase_distribution` 中 non-general 占比反映分期标注覆盖率；
    `embedded_chunks` 与 `total_chunks` 的差距说明上架文献是否全部向量化。
    """
    return {
        "documents": search_repo.count_active_documents(db),
        "total_chunks": search_repo.count_active_chunks(db),
        "embedded_chunks": search_repo.count_active_embedded_chunks(db),
        "phase_distribution": search_repo.phase_distribution(db, active_only=True),
        "evidence_distribution": search_repo.evidence_distribution(db, active_only=True),
    }


@router.get("/stats/library", summary="全库统计（含已下架）")
def library_stats(db: Session = Depends(get_db)) -> dict:
    """返回全库（含已下架文献）的文本块统计，用于核对入库与向量化进度。"""
    return {
        "total_chunks_all": search_repo.count_chunks(db),
        "embedded_chunks_all": search_repo.count_embedded_chunks(db),
        "phase_distribution_all": search_repo.phase_distribution(db),
        "evidence_distribution_all": search_repo.evidence_distribution(db),
    }


@router.get("/chunks/{chunk_id}", summary="取文本块全文")
def chunk_detail(chunk_id: str, db: Session = Depends(get_db)) -> dict:
    """按文本块 ID 返回**完整原文**及所在位置。

    为什么需要这个端点
    ------------------
    检索结果里的 `content_snippet` 为控制响应体大小被截断到约 300 字，
    前端「展开原文」只解除显示行数限制，仍然看不到 300 字之后的内容，
    用户无法核对整段原文。

    因此引用面板展开时改为调用本端点取全文。这里**不限制文献是否上架**：
    被引用过的文本块即使后来下架，也应允许查看其原文，否则历史答案的
    引用会失效。
    """
    import uuid
    try:
        uuid.UUID(chunk_id)
    except ValueError:
        raise HTTPException(status_code=422, detail='无效的文本块 ID')
    found = search_repo.get_chunk_with_doc(db, chunk_id)
    if found is None:
        from app.services.document_versions import archived_citation
        archived = archived_citation(db, chunk_id)
        if archived is not None:
            return archived
        raise HTTPException(status_code=404, detail="文本块不存在")

    chunk, doc = found
    return {
        "chunk_id": str(chunk.id),
        "document_id": str(doc.id),
        "doc_title": doc.title,
        "doc_journal": doc.journal,
        "doc_year": doc.year,
        "section": chunk.section,
        "page_start": chunk.page_start,
        "page_end": chunk.page_end,
        "chunk_index": chunk.chunk_index,
        "evidence_level": chunk.evidence_level,
        "phase": chunk.phase,
        "content": chunk.content or "",
        "char_count": len(chunk.content or ""),
        "doc_is_active": bool(doc.is_active),
    }


@router.get("/phases", summary="围术期分期选项")
def phases() -> dict:
    """返回可用于过滤的围术期分期与证据等级枚举。"""
    from app.core.constants import EvidenceLevel, PerioperativePhase

    return {
        "phases": [{"value": p.value, "label": p.zh} for p in PerioperativePhase],
        "evidence_levels": [
            {"value": e.value, "label": e.zh, "weight": e.weight} for e in EvidenceLevel
        ],
    }
