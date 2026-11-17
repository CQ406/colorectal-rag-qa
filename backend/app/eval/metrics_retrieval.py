"""检索质量指标。

指标定义与选择理由
------------------
本系统每个问题只标注**一个** gold chunk（正确答案所在文本块），
因此适用单答案检索指标：

  **Recall@K**：前 K 条结果中是否含 gold chunk。
    最贴近实际需求 —— 只要证据被召回，LLM 就能据此作答。
    论文中报告 K = 1, 3, 5, 10。

  **MRR**（Mean Reciprocal Rank）：首个正确结果的排名倒数均值。
    反映「答案排得多靠前」，对排序质量敏感。

  **NDCG@K**（Normalized Discounted Cumulative Gain）：
    考虑位置折扣的排序质量指标，对「正确结果排在更前」给更高分。
    单答案场景下与 MRR 互补：MRR 只看首个正确项，NDCG 还考虑
    结果列表整体是否把相关内容排在前列。

  **Precision@K**：前 K 条中相关内容的比例。
    在单答案标注下该值上限为 1/K，故仅作参考，用于观察
    「结果是否被大量无关内容占据」。

为什么不报 MAP / F1
-------------------
这两者需要多相关等级标注（每个结果标注相关程度）。
本评测集为单答案标注，报 MAP 会退化为 MRR 的变体，
反而误导读者。论文中应明确说明标注粒度与指标选择的对应关系。
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field


@dataclass
class RetrievalMetrics:
    """一组检索评测的汇总指标。"""

    label: str
    total: int = 0
    # 命中计数：K -> 命中数
    hits: dict[int, int] = field(default_factory=dict)
    # 各样本的首个正确排名（未命中记 None）
    ranks: list[int | None] = field(default_factory=list)
    # 各样本的 NDCG@K
    ndcg: dict[int, list[float]] = field(default_factory=dict)
    latencies: list[float] = field(default_factory=list)
    # 检索上下文中的无关内容占比（用于 Precision 观察）
    precision: dict[int, list[float]] = field(default_factory=dict)
    config: dict = field(default_factory=dict)

    def recall_at(self, k: int) -> float:
        """Recall@K：前 K 条含 gold chunk 的比例。"""
        return self.hits.get(k, 0) / self.total if self.total else 0.0

    @property
    def mrr(self) -> float:
        """MRR：首个正确结果排名倒数的均值。"""
        if not self.total:
            return 0.0
        return sum(1.0 / r for r in self.ranks if r) / self.total

    def ndcg_at(self, k: int) -> float:
        """NDCG@K 均值。"""
        vals = self.ndcg.get(k, [])
        return statistics.mean(vals) if vals else 0.0

    def precision_at(self, k: int) -> float:
        """Precision@K 均值（单答案标注下上限为 1/K，仅作观察）。"""
        vals = self.precision.get(k, [])
        return statistics.mean(vals) if vals else 0.0

    @property
    def hit_at_1(self) -> float:
        return (
            sum(1 for r in self.ranks if r == 1) / self.total
            if self.total
            else 0.0
        )

    @property
    def avg_latency_ms(self) -> float:
        return statistics.mean(self.latencies) if self.latencies else 0.0

    @property
    def p95_latency_ms(self) -> float:
        if len(self.latencies) < 2:
            return self.avg_latency_ms
        return statistics.quantiles(self.latencies, n=20)[18]

    @property
    def miss_count(self) -> int:
        """完全未命中的样本数（前 K 条均不含 gold chunk）。"""
        return sum(1 for r in self.ranks if r is None)

    def to_dict(self) -> dict:
        return {
            "label": self.label,
            "config": self.config,
            "total": self.total,
            "recall@1": round(self.recall_at(1), 4),
            "recall@3": round(self.recall_at(3), 4),
            "recall@5": round(self.recall_at(5), 4),
            "recall@10": round(self.recall_at(10), 4),
            "mrr": round(self.mrr, 4),
            "ndcg@5": round(self.ndcg_at(5), 4),
            "ndcg@10": round(self.ndcg_at(10), 4),
            "hit@1": round(self.hit_at_1, 4),
            "miss_count": self.miss_count,
            "avg_latency_ms": round(self.avg_latency_ms, 1),
            "p95_latency_ms": round(self.p95_latency_ms, 1),
        }

    def report(self) -> str:
        return (
            f"  {self.label:<30} n={self.total:<4} "
            f"R@1={self.recall_at(1):.3f} R@3={self.recall_at(3):.3f} "
            f"R@5={self.recall_at(5):.3f} R@10={self.recall_at(10):.3f} "
            f"MRR={self.mrr:.3f} NDCG@5={self.ndcg_at(5):.3f} "
            f"延迟={self.avg_latency_ms:.0f}ms"
        )


# ----------------------------------------------------------------------
#  NDCG 计算
# ----------------------------------------------------------------------
def dcg_at_k(relevances: list[float], k: int) -> float:
    """Discounted Cumulative Gain。

    使用标准公式：DCG = Σ (2^rel - 1) / log2(i + 1)，i 从 1 开始。
    """
    total = 0.0
    for i, rel in enumerate(relevances[:k], start=1):
        total += (2**rel - 1) / math.log2(i + 1)
    return total


def ndcg_at_k(rank: int | None, k: int, total_relevant: int = 1) -> float:
    """单答案场景下的 NDCG@K。

    相关性为二值（gold chunk 相关 = 1，其余 = 0）。

    Args:
        rank: gold chunk 的排名（1 起），None 表示前 K 条未命中
        k: 截断位置
        total_relevant: 理想情况下应命中的相关文档数（本评测集为 1）

    Returns:
        NDCG@K ∈ [0, 1]
    """
    if rank is None or rank > k:
        return 0.0

    # 实际排序：第 rank 位相关
    actual = [0.0] * (rank - 1) + [1.0]
    dcg = dcg_at_k(actual, k)

    # 理想排序：前 total_relevant 位相关
    ideal = [1.0] * total_relevant
    idcg = dcg_at_k(ideal, k)

    return dcg / idcg if idcg > 0 else 0.0
