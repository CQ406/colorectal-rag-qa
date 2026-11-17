"""多路召回 + RRF 融合的检索编排。

流程：
    查询
     ├─ 向量召回（BGE-M3 + pgvector HNSW）      —— 语义相近
     ├─ 关键词召回（中文 bigram + GIN 倒排索引） —— 字面精确
     └─ 图谱路径召回（Neo4j）                    —— 结构化推理
            ↓
        RRF 融合（按名次，不用分数，跨数据集稳定）
            ↓
        交叉编码器重排（可选）+ 证据等级加权 + 同文献限流
            ↓
        最终结果（带引用溯源元数据）

与阶段一的差异
--------------
阶段一只有单路向量召回。本阶段引入多路融合，是论文的核心创新点之一，
配套消融实验见 scripts/eval_retrieval.py 的 --recall-mode 参数。
"""

from __future__ import annotations

import time

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.logging import get_logger
from app.repositories import config_repo, search_repo
from app.repositories.search_repo import ScoredChunk
from app.schemas.common import (
    RecallPathInfo,
    SearchRequest,
    SearchResponse,
    RetrievedChunk,
)
from app.services import fusion as fusion_mod
from app.services.fusion import RecallPath
from app.services.model_registry import registry

logger = get_logger("services.hybrid_search")

EMBEDDING_MODEL_TAG = "bge-m3-fp16"


# ----------------------------------------------------------------------
#  显存保护：控制送入重排的候选数
# ----------------------------------------------------------------------
def _rerank_budget(limit: int | None = None) -> int:
    """根据设备能力决定重排候选上限。

    重排是候选数的线性开销（实测约 105 对/秒）。CPU 上慢 20~35 倍，
    故显著降低候选数，避免用户等待过久。
    """
    cap = limit if limit is not None else settings.retrieval.rerank_candidate_limit
    if registry.device == "cuda":
        return cap
    # CPU 上重排慢 20~35 倍，把候选压到 20 以内，否则单次查询要等十几秒
    return min(20, cap)


# ----------------------------------------------------------------------
#  各路召回
# ----------------------------------------------------------------------
def recall_vector(
    db: Session,
    query_vec: list[float],
    limit: int,
    phase: str | None,
    doc_type: str | None,
    weight: float | None = None,
) -> RecallPath:
    """向量语义召回。"""
    t0 = time.perf_counter()
    chunks = search_repo.search_by_vector(
        db, query_embedding=query_vec, limit=limit, phase=phase, doc_type=doc_type
    )
    return RecallPath(
        name="vector",
        weight=weight if weight is not None else settings.retrieval.vector_weight,
        results=chunks,
        elapsed_ms=round((time.perf_counter() - t0) * 1000, 1),
    )


def recall_keyword(
    db: Session,
    query: str,
    limit: int,
    phase: str | None,
    doc_type: str | None,
    weight: float | None = None,
    min_overlap: float | None = None,
) -> RecallPath:
    """关键词召回（中文 bigram 倒排索引）。

    权重默认 0.3，**远低于向量路**。依据阶段十实测：
    在 195 题中文与 189 题英文的口语化临床问题评测集上，
    本路单独使用 R@5 仅 0.000 与 0.005 —— 几乎完全失效。

    原因不是跨语言，而是**口语提问与学术文本的词汇体系不同**：
    医师问"这两个术式在并发症上有什么区别"，
    文献写"robotic versus conventional laparoscopic surgery"。
    bigram 为精确词元匹配，无法跨越这种表述差异。

    实测权重 0.7 时融合结果反而低于纯向量（英文 R@5 0.487→0.466），
    因为该路引入的噪声被 RRF 当作「多路共识」提权。
    降低权重后融合不再劣化。

    本路的正确定位是「补充精确术语命中」（如用户直接输入
    "ICG"、"taTME"、"ERAS" 等缩写时），而非主要召回手段。
    """
    t0 = time.perf_counter()
    chunks = search_repo.search_by_keyword(
        db,
        query=query,
        limit=limit,
        phase=phase,
        min_overlap=(
            min_overlap if min_overlap is not None
            else settings.retrieval.min_keyword_overlap
        ),
        doc_type=doc_type,
    )
    return RecallPath(
        name="keyword",
        weight=weight if weight is not None else settings.retrieval.keyword_weight,
        results=chunks,
        elapsed_ms=round((time.perf_counter() - t0) * 1000, 1),
    )


def recall_graph(
    db: Session,
    query: str,
    limit: int,
    phase: str | None = None,
    doc_type: str | None = None,
    weight: float | None = None,
) -> RecallPath:
    """通过 Neo4j 知识图谱召回关联证据。"""
    t0 = time.perf_counter()
    chunks: list[ScoredChunk] = []
    try:
        from app.services.graph_search import graph_recall

        chunks = graph_recall(db, query=query, limit=limit, phase=phase, doc_type=doc_type)
    except ImportError as exc:
        logger.warning("图谱检索模块不可用，跳过该路: %s", exc)
    except Exception as exc:  # noqa: BLE001
        logger.warning("图谱召回失败，跳过该路: %s", exc)

    return RecallPath(
        name="graph",
        weight=weight if weight is not None else settings.retrieval.graph_weight,
        results=chunks,
        elapsed_ms=round((time.perf_counter() - t0) * 1000, 1),
    )


# ----------------------------------------------------------------------
#  重排
# ----------------------------------------------------------------------
def rerank(
    query: str, candidates: list[ScoredChunk], top_n: int
) -> tuple[list[ScoredChunk], float]:
    """交叉编码器重排。"""
    if not candidates:
        return [], 0.0

    model = registry.get_reranker()
    pairs = [[query, c.content] for c in candidates]

    t0 = time.perf_counter()
    scores = model.compute_score(pairs, normalize=True)
    elapsed = time.perf_counter() - t0

    if not isinstance(scores, list):
        scores = [scores]

    for candidate, score in zip(candidates, scores, strict=False):
        candidate.rerank_score = float(score)
        candidate.score = float(score)

    ranked = sorted(candidates, key=lambda c: c.rerank_score or 0.0, reverse=True)
    return ranked[:top_n], elapsed


# ----------------------------------------------------------------------
#  响应组装
# ----------------------------------------------------------------------
def _to_response(chunk: ScoredChunk, rank: int) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=chunk.chunk_id,
        document_id=chunk.document_id,
        content=chunk.content,
        score=round(chunk.score, 6),
        vector_score=round(chunk.vector_score, 6) if chunk.vector_score is not None else None,
        rerank_score=round(chunk.rerank_score, 6) if chunk.rerank_score is not None else None,
        keyword_hits=chunk.keyword_hits,
        rrf_score=chunk.rrf_score,
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


# ----------------------------------------------------------------------
#  主流程
# ----------------------------------------------------------------------
def search(db: Session, req: SearchRequest) -> SearchResponse:
    """多路召回 + RRF 融合 + 重排的完整检索。"""
    t_start = time.perf_counter()
    timings: dict[str, float] = {}

    # 运行时配置（带 2 秒缓存）：检索参数以配置表为准，回落 settings。
    #
    # 此前一律读 settings.retrieval.*（来自 .env），管理界面改了权重后
    # 只写入数据库、运行行为不变 —— 界面上标着"立即生效"，实际要重启才生效。
    cfg = config_repo.effective(db)

    def _num(key: str, fallback: float) -> float:
        value = cfg.get(key)
        try:
            return float(value)
        except (TypeError, ValueError):
            return fallback

    # ---------- 1. 查询向量化（仅向量路需要） ----------
    from app.services.search_service import embed_query

    query_vec: list[float] | None = None
    if req.recall_mode in ("hybrid", "vector"):
        query_vec, t_embed = embed_query(req.query)
        timings["embed_ms"] = round(t_embed * 1000, 1)

    # ---------- 2. 多路召回 ----------
    paths: list[RecallPath] = []
    if req.recall_mode == "hybrid":
        if query_vec is not None:
            paths.append(
                recall_vector(
                    db, query_vec, req.recall_limit, req.phase, req.doc_type,
                    weight=_num("retrieval.vector_weight", settings.retrieval.vector_weight),
                )
            )
        paths.append(
            recall_keyword(
                db, req.query, req.recall_limit, req.phase, req.doc_type,
                weight=_num("retrieval.keyword_weight", settings.retrieval.keyword_weight),
                min_overlap=_num(
                    "retrieval.min_keyword_overlap", settings.retrieval.min_keyword_overlap
                ),
            )
        )
        graph_enabled = bool(cfg.get("system.enable_graph_retrieval",
                                     settings.retrieval.enable_graph_retrieval))
        if graph_enabled and req.use_graph:
            paths.append(
                recall_graph(
                    db, req.query, settings.retrieval.retrieve_top_k_graph,
                    req.phase, req.doc_type,
                    weight=_num("retrieval.graph_weight", settings.retrieval.graph_weight),
                )
            )
    elif req.recall_mode == "vector":
        paths.append(
            recall_vector(db, query_vec, req.recall_limit, req.phase, req.doc_type)
        )
    elif req.recall_mode == "keyword":
        paths.append(
            recall_keyword(db, req.query, req.recall_limit, req.phase, req.doc_type)
        )
    else:
        raise ValueError(f"未知的召回模式: {req.recall_mode}")

    timings["recall_ms"] = round((time.perf_counter() - t_start) * 1000 - timings.get("embed_ms", 0), 1)

    path_infos = [
        RecallPathInfo(name=p.name, count=p.count, weight=p.weight, elapsed_ms=p.elapsed_ms)
        for p in paths
    ]
    total_candidates = sum(p.count for p in paths)

    if not any(p.results for p in paths):
        return SearchResponse(
            query=req.query,
            recall_mode=req.recall_mode,
            total_candidates=0,
            fused_count=0,
            returned=0,
            reranked=False,
            recall_paths=path_infos,
            elapsed_ms=round((time.perf_counter() - t_start) * 1000, 1),
            timings=timings,
            results=[],
        )

    # ---------- 3. RRF 融合 ----------
    t0 = time.perf_counter()
    fused = fusion_mod.reciprocal_rank_fusion(
        paths, k=req.rrf_k or settings.retrieval.rrf_k, limit=req.rrf_limit
    )
    timings["fusion_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    candidates = fused.chunks

    # ---------- 4. 重排 ----------
    reranked = False
    if req.use_rerank and candidates:
        budget = min(
            _rerank_budget(
                int(_num("retrieval.rerank_candidate_limit",
                         settings.retrieval.rerank_candidate_limit))
            ),
            len(candidates),
        )
        pool = candidates[:budget]
        final, t_rerank = rerank(req.query, pool, top_n=req.top_k * 2)
        timings["rerank_ms"] = round(t_rerank * 1000, 1)
        reranked = True

        # 证据等级加权（乘法加权，上限 +15%，不会颠倒相关性排序）
        final = fusion_mod.boost_by_evidence(
            final,
            enabled=req.use_evidence_boost,
            strength=_num("retrieval.evidence_boost_strength",
                          settings.retrieval.evidence_boost_strength),
        )
        # 同文献限流
        final = fusion_mod.dedup_by_document(final, req.max_per_document)
        final = final[: req.top_k]
    else:
        final = candidates[: req.top_k]

    timings["total_ms"] = round((time.perf_counter() - t_start) * 1000, 1)

    return SearchResponse(
        query=req.query,
        recall_mode=req.recall_mode,
        total_candidates=total_candidates,
        fused_count=len(candidates),
        returned=len(final),
        reranked=reranked,
        recall_paths=path_infos,
        elapsed_ms=timings["total_ms"],
        timings=timings,
        results=[_to_response(c, i + 1) for i, c in enumerate(final)],
    )
