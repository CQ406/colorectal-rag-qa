"""多路召回融合（RRF）与重排融合策略。

为什么需要多路召回
------------------
单一向量检索的短板很明确：
  - 对**精确术语**不敏感（如 ICD 编码、药物商品名、术式缩写 "taTME"）
  - 对**生僻词**召回差（训练语料中罕见的专业词向量表示不准）

关键词检索恰好补这个短板，但对同义改写无能为力。
两者融合可覆盖「语义相近」与「字面精确」两类需求。

RRF（Reciprocal Rank Fusion）为什么优于加权分数求和
---------------------------------------------------
各路召回的分数**不可直接比较**：
  - 向量相似度是余弦值，集中在 0.4~0.9
  - 关键词得分是命中比例，集中在 0~1 但分布形态完全不同

直接加权求和需要反复调参，且换数据集就失效。RRF 只用**排名**不用分数：

    RRF(d) = Σ_i  weight_i / (k + rank_i(d))

性质：
  - 对分数尺度不敏感，无需归一化，跨数据集稳定
  - k 起平滑作用，默认 60（原论文经验值）：k 越大，靠前名次的优势越被削弱
  - 在多个召回列表中同时靠前的文档获得最高分，天然实现「共识投票」

参考：Cormack et al., "Reciprocal Rank Fusion outperforms Condorcet and
individual Rank Learning Methods", SIGIR 2009.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Iterable

from app.core.logging import get_logger
from app.repositories.search_repo import ScoredChunk

logger = get_logger("services.fusion")

# RRF 平滑常数（原论文经验值）
DEFAULT_RRF_K = 60


@dataclass
class RecallPath:
    """一路召回结果。"""

    name: str                       # vector / keyword / graph
    weight: float                   # 该路在 RRF 中的权重
    results: list[ScoredChunk] = field(default_factory=list)
    elapsed_ms: float = 0.0

    @property
    def count(self) -> int:
        return len(self.results)


@dataclass
class FusionResult:
    """融合结果。"""

    chunks: list[ScoredChunk]
    paths: list[RecallPath]
    # 每个 chunk 命中了哪几路，便于分析融合贡献
    hit_paths: dict[str, list[str]] = field(default_factory=dict)

    @property
    def path_summary(self) -> dict[str, int]:
        return {p.name: p.count for p in self.paths}


def reciprocal_rank_fusion(
    paths: Iterable[RecallPath],
    k: int = DEFAULT_RRF_K,
    limit: int | None = None,
) -> FusionResult:
    """多路召回结果做 RRF 融合。

    Args:
        paths: 各路的召回结果（须已按相关性降序排列）
        k: RRF 平滑常数，越大越弱化头部优势
        limit: 返回条数上限

    Returns:
        FusionResult，chunks 按 RRF 得分降序
    """
    paths = list(paths)
    rrf_scores: dict[str, float] = {}
    # chunk_id -> ScoredChunk（保留首次出现时的实例，字段最完整）
    registry: dict[str, ScoredChunk] = {}
    # chunk_id -> 命中的路径名列表
    hit_paths: dict[str, list[str]] = {}

    for path in paths:
        if not path.results:
            continue
        for rank, chunk in enumerate(path.results, start=1):
            key = str(chunk.chunk_id)
            # RRF 核心公式：名次越靠前贡献越大，与分数尺度无关
            contribution = path.weight / (k + rank)
            rrf_scores[key] = rrf_scores.get(key, 0.0) + contribution

            if key not in registry:
                registry[key] = chunk
            hit_paths.setdefault(key, []).append(path.name)

    # 按 RRF 得分排序
    ordered = sorted(rrf_scores.items(), key=lambda kv: kv[1], reverse=True)
    if limit:
        ordered = ordered[:limit]

    fused: list[ScoredChunk] = []
    for key, score in ordered:
        # 用 replace 生成副本而非就地改写：RRF 不应有副作用。
        # 若直接修改传入对象，同一批对象被多次融合时（如消融实验复用样本）
        # 后一次会覆盖前一次的得分，导致结果不可复现。
        chunk = replace(registry[key], rrf_score=round(score, 6))
        fused.append(chunk)

    return FusionResult(chunks=fused, paths=paths, hit_paths=hit_paths)


def boost_by_evidence(
    chunks: list[ScoredChunk],
    enabled: bool = True,
    strength: float = 0.15,
) -> list[ScoredChunk]:
    """按证据等级对重排结果做**乘法**加权。

    医学问答中，同样相关的证据，来自指南/系统评价的结论比病例报告更可信。

    公式（乘法加权，上限 +15%）：

        factor = 1 + strength * (weight - min) / (max - min)
        score' = score * factor

    **为什么用乘法而不是线性插值**
    初版实现采用线性插值 `base*(1-s) + base*norm*s`，实测反而降低准确率。
    原因：该式会把「低相关但高证据等级」的文档抬到「高相关」文档之上。
    例如强度 0.8 时，指南分 0.1 → 0.42，而综述分 0.95 → 0.36，
    完全颠倒相关性排序。

    乘法加权保证：**加分幅度正比于原分**，低相关文档即使证据等级高
    也只会被小幅提升，不会跨越大量高相关文档。上限 +15% 也确保了
    微调而非重排。

    参考消融实验（摘要片段反查）：
      ④ 多路融合+重排         R@1=0.900 MRR=0.915
      ⑤ +证据加权(线性,0.15)  R@1=0.775 MRR=0.847  ← 劣化
      ⑤ +证据加权(乘法,0.15)  见 README 最新消融表

    Args:
        chunks: 已重排的结果
        enabled: 是否启用
        strength: 最大加价比例，0 表示不加权，0.15 表示最多 +15%
    """
    if not chunks:
        return chunks

    if not enabled or strength <= 0:
        # 即使不加权也需按现有分数排序，否则调用方拿到的是融合顺序而非重排顺序，
        # 会导致「关闭加权」与「开启加权」的结果不可比（消融实验会失真）。
        return sorted(
            chunks,
            key=lambda c: c.rerank_score if c.rerank_score is not None else c.score,
            reverse=True,
        )

    from app.core.constants import EvidenceLevel

    weights = [e.weight for e in EvidenceLevel]
    max_weight, min_weight = max(weights), min(weights)
    span = max_weight - min_weight

    boosted: list[ScoredChunk] = []
    for chunk in chunks:
        base = chunk.rerank_score if chunk.rerank_score is not None else chunk.score
        try:
            level = EvidenceLevel(chunk.evidence_level)
            norm = (level.weight - min_weight) / span if span > 0 else 0.0
        except (ValueError, AttributeError):
            # 未知证据等级：不加权
            norm = 0.0

        new_score = base * (1.0 + strength * norm)
        boosted.append(replace(chunk, rerank_score=new_score, score=new_score))

    boosted.sort(key=lambda c: c.rerank_score or 0.0, reverse=True)
    return boosted


def dedup_by_document(
    chunks: list[ScoredChunk],
    max_per_document: int = 3,
) -> list[ScoredChunk]:
    """限制单篇文献贡献的文本块数量。

    问题：一篇长综述可能占据结果前 5 条中的 4 条，导致证据来源单一，
    用户看不到其他研究的观点。

    策略：同一文献最多保留 max_per_document 条（取分数最高者）。

    注意：这会影响 Recall 指标（正确文档仍被召回，只是块数减少），
    故在评测中应作为可选策略单独报告。
    """
    if max_per_document <= 0:
        return chunks

    counts: dict[str, int] = {}
    kept: list[ScoredChunk] = []
    for chunk in chunks:
        key = str(chunk.document_id)
        if counts.get(key, 0) >= max_per_document:
            continue
        counts[key] = counts.get(key, 0) + 1
        kept.append(chunk)
    return kept
