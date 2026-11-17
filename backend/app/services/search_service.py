"""检索服务 —— 编排「向量化 → 召回 → 重排」流程。

阶段一提供基础向量检索 + 重排，用于验证整条链路可用。
阶段三将在此基础上扩展为多路召回（向量 + 关键词 + 知识图谱）+ RRF 融合。

性能参考（RTX 5060 Laptop 8GB）：
    向量化  694 条/秒
    重排    105 对/秒  → 50 条候选约 0.5 秒
"""

from __future__ import annotations

import time

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.logging import get_logger
from app.repositories import search_repo
from app.repositories.search_repo import ScoredChunk
from app.schemas.common import RetrievedChunk, SearchRequest, SearchResponse
from app.services.model_registry import registry

logger = get_logger("services.search")

# 与模型/索引绑定的标识，写入 chunk.embedding_model 以便重建索引时校验一致性
EMBEDDING_MODEL_TAG = "bge-m3-fp16"


class EmbeddingInvalidError(RuntimeError):
    """向量化结果含 NaN/Inf，无法用于检索。

    典型成因：fp16 数值溢出（部分 GPU/驱动组合下 BGE-M3 偶发），
    或 GPU 状态异常。NaN 向量一旦传给 pgvector 会抛
    `NaN not allowed in vector`，用户侧只看到一长串 SQL 报错。

    本异常把这种情况收敛为一条可操作的提示，并触发 fp32 自愈重载。
    """


def _count_nonfinite(vectors) -> int:
    """统计含 NaN/Inf 的向量条数。"""
    import numpy as np

    return int(sum(0 if np.isfinite(v).all() else 1 for v in vectors))


def embed_texts(
    texts: list[str], batch_size: int | None = None, *, _retry_fp32: bool = True
) -> tuple[list[list[float]], float]:
    """把文本批量向量化。

    Returns:
        (向量列表, 耗时秒)

    Raises:
        EmbeddingInvalidError: 重载 fp32 后仍返回 NaN/Inf
    """
    if not texts:
        return [], 0.0

    model = registry.get_embedder()
    bs = batch_size or settings.model.embedding_batch_size

    t0 = time.perf_counter()
    out = model.encode(
        texts,
        batch_size=bs,
        max_length=settings.model.embedding_max_length,
    )
    elapsed = time.perf_counter() - t0

    # BGE-M3 返回 dict，dense_vecs 为 (N, 1024) 的 numpy 数组
    vectors = out["dense_vecs"]

    # 数值有效性校验：NaN 会让 pgvector 直接拒收，必须在此拦住
    bad = _count_nonfinite(vectors)
    if bad:
        logger.error(
            "向量化结果含 NaN/Inf：%d/%d 条（模型=%s, 精度=%s）",
            bad, len(vectors), EMBEDDING_MODEL_TAG,
            "fp32" if getattr(registry, "_embedder_force_fp32", None) else "fp16",
        )
        if _retry_fp32:
            # 自愈：切到 fp32 重新加载后重试一次。
            # fp16 在部分 GPU/驱动上会溢出，fp32 可稳定复现正确结果，
            # 代价是显存约翻倍、编码稍慢——比整体 500 好得多。
            logger.warning("检测到 NaN 向量，切换 fp32 重新加载向量模型后重试")
            registry.reload_embedder(force_fp32=True)
            return embed_texts(texts, batch_size, _retry_fp32=False)
        raise EmbeddingInvalidError(
            "向量模型输出异常（含 NaN），已尝试切换 fp32 仍未恢复。"
            "请重启后端；若反复出现，请在 .env 中设置 "
            "MODEL_FORCE_CPU=true 或降低 embedding_max_length 后重试。"
        )

    return [v.tolist() for v in vectors], elapsed


def embed_query(query: str) -> tuple[list[float], float]:
    """向量化单条查询。"""
    vectors, elapsed = embed_texts([query], batch_size=1)
    return vectors[0], elapsed


def rerank(
    query: str, candidates: list[ScoredChunk], top_n: int
) -> tuple[list[ScoredChunk], float]:
    """对候选做交叉编码器重排。

    重排模型逐对打分，比向量检索精确但慢，故只用于精排阶段。
    """
    if not candidates:
        return [], 0.0

    model = registry.get_reranker()
    pairs = [[query, c.content] for c in candidates]

    t0 = time.perf_counter()
    scores = model.compute_score(pairs, normalize=True)
    elapsed = time.perf_counter() - t0

    # 单条候选时 compute_score 返回标量而非列表，需统一为列表
    if not isinstance(scores, list):
        scores = [scores]

    for candidate, score in zip(candidates, scores, strict=False):
        candidate.rerank_score = float(score)
        candidate.score = float(score)

    ranked = sorted(candidates, key=lambda c: c.rerank_score or 0.0, reverse=True)
    return ranked[:top_n], elapsed


def _to_response_chunk(chunk: ScoredChunk, rank: int) -> RetrievedChunk:
    """服务层结果 → API 响应模型。"""
    return RetrievedChunk(
        chunk_id=chunk.chunk_id,
        document_id=chunk.document_id,
        content=chunk.content,
        score=round(chunk.score, 6),
        vector_score=round(chunk.vector_score, 6) if chunk.vector_score is not None else None,
        rerank_score=round(chunk.rerank_score, 6) if chunk.rerank_score is not None else None,
        rank=rank,
        doc_title=chunk.doc_title,
        doc_year=chunk.doc_year,
        doc_journal=chunk.doc_journal,
        doc_authors=chunk.doc_authors,
        phase=chunk.phase,
        evidence_level=chunk.evidence_level,
        page_start=chunk.page_start,
        page_end=chunk.page_end,
        section=chunk.section,
    )


def search(db: Session, req: SearchRequest) -> SearchResponse:
    """执行一次完整检索。

    流程：查询向量化 → pgvector 召回 → （可选）重排 → 组装响应
    """
    t_start = time.perf_counter()
    timings: dict[str, float] = {}

    # 1) 向量化查询
    query_vec, t_embed = embed_query(req.query)
    timings["embed_ms"] = round(t_embed * 1000, 1)

    # 2) 向量召回
    t0 = time.perf_counter()
    candidates = search_repo.search_by_vector(
        db,
        query_embedding=query_vec,
        limit=req.recall_limit,
        phase=req.phase,
        doc_type=req.doc_type,
    )
    timings["recall_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    total_candidates = len(candidates)

    if not candidates:
        return SearchResponse(
            query=req.query,
            total_candidates=0,
            returned=0,
            reranked=False,
            elapsed_ms=round((time.perf_counter() - t_start) * 1000, 1),
            timings=timings,
            results=[],
        )

    # 3) 重排
    if req.use_rerank:
        final, t_rerank = rerank(req.query, candidates, top_n=req.top_k)
        timings["rerank_ms"] = round(t_rerank * 1000, 1)
    else:
        candidates.sort(key=lambda c: c.score, reverse=True)
        final = candidates[: req.top_k]

    timings["total_ms"] = round((time.perf_counter() - t_start) * 1000, 1)

    return SearchResponse(
        query=req.query,
        total_candidates=total_candidates,
        returned=len(final),
        reranked=req.use_rerank,
        elapsed_ms=timings["total_ms"],
        timings=timings,
        results=[_to_response_chunk(c, i + 1) for i, c in enumerate(final)],
    )
