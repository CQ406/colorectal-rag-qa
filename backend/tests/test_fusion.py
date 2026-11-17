"""RRF 融合与后处理策略的单元测试。

这些测试不依赖数据库与 GPU，可在任意环境快速运行：
    python -m pytest tests/test_fusion.py -v
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.repositories.search_repo import ScoredChunk, term_overlap_score
from app.services.fusion import (
    DEFAULT_RRF_K,
    RecallPath,
    boost_by_evidence,
    dedup_by_document,
    reciprocal_rank_fusion,
)


def make_chunk(
    title: str,
    score: float = 1.0,
    evidence: str = "review",
    doc_id: uuid.UUID | None = None,
) -> ScoredChunk:
    """构造测试用检索结果。"""
    return ScoredChunk(
        chunk_id=uuid.uuid4(),
        document_id=doc_id or uuid.uuid4(),
        content=f"{title} 的内容",
        score=score,
        phase="postop",
        evidence_level=evidence,
        page_start=1,
        page_end=1,
        section="Methods",
        doc_title=title,
        doc_year=2024,
        doc_journal="Test Journal",
        doc_authors="Zhang S",
    )


# ======================================================================
#  RRF 融合核心逻辑
# ======================================================================
class TestRRF:
    def test_single_path_preserves_order(self):
        """单路召回时，RRF 应保持原有顺序。"""
        chunks = [make_chunk(f"doc{i}") for i in range(5)]
        result = reciprocal_rank_fusion(
            [RecallPath(name="vector", weight=1.0, results=chunks)]
        )
        assert [c.doc_title for c in result.chunks] == [f"doc{i}" for i in range(5)]

    def test_rrf_score_formula(self):
        """RRF 得分应等于 weight/(k+rank)。

        注意：实现中对得分做了 round(..., 6)，故用绝对容差比较。
        """
        chunk = make_chunk("doc0")
        result = reciprocal_rank_fusion(
            [RecallPath(name="vector", weight=1.0, results=[chunk])],
            k=60,
        )
        # 第 1 名：1.0 / (60 + 1) = 0.0163934426...
        assert result.chunks[0].rrf_score == pytest.approx(1.0 / 61, abs=1e-6)

    def test_consensus_ranking(self):
        """在两路都靠前的文档，应排在只在一路靠前的文档之前。

        这是 RRF 的核心价值：跨路共识 > 单路高分。
        """
        # a 在两路都排第 2；b 在向量路排第 1 但关键词路排第 10
        a = make_chunk("consensus")
        b = make_chunk("single_path_strong")
        others_v = [make_chunk(f"v{i}") for i in range(9)]
        others_k = [make_chunk(f"k{i}") for i in range(9)]

        vector_results = [b, a, *others_v]
        keyword_results = [*others_k[:1], a, *others_k[1:], b]

        result = reciprocal_rank_fusion(
            [
                RecallPath(name="vector", weight=1.0, results=vector_results),
                RecallPath(name="keyword", weight=1.0, results=keyword_results),
            ]
        )
        titles = [c.doc_title for c in result.chunks]
        assert titles.index("consensus") < titles.index("single_path_strong")

    def test_weight_affects_ranking(self):
        """提高某一路权重，应提升该路独有文档的排名。"""
        shared = make_chunk("shared")
        only_kw = make_chunk("only_keyword")

        vector_results = [shared, *[make_chunk(f"v{i}") for i in range(5)]]
        keyword_results = [only_kw, shared]

        # 关键词权重低 → shared 靠前
        low = reciprocal_rank_fusion([
            RecallPath(name="vector", weight=1.0, results=vector_results),
            RecallPath(name="keyword", weight=0.1, results=keyword_results),
        ])
        # 关键词权重极高 → only_keyword 靠前
        high = reciprocal_rank_fusion([
            RecallPath(name="vector", weight=0.1, results=vector_results),
            RecallPath(name="keyword", weight=10.0, results=keyword_results),
        ])

        low_titles = [c.doc_title for c in low.chunks]
        high_titles = [c.doc_title for c in high.chunks]
        assert low_titles.index("shared") < low_titles.index("only_keyword")
        assert high_titles.index("only_keyword") < high_titles.index("shared")

    def test_dedup_across_paths(self):
        """同一文档出现在多路时，只应保留一条。"""
        chunk = make_chunk("dup")
        result = reciprocal_rank_fusion([
            RecallPath(name="vector", weight=1.0, results=[chunk]),
            RecallPath(name="keyword", weight=1.0, results=[chunk]),
        ])
        assert len(result.chunks) == 1
        # 两路都命中，得分应为两路贡献之和 = 2/(60+1) = 0.03278688...
        assert result.chunks[0].rrf_score == pytest.approx(2 / 61, abs=1e-6)

    def test_hit_paths_tracked(self):
        """应记录每个文档命中了哪几路，便于分析融合贡献。"""
        a = make_chunk("both")
        b = make_chunk("vector_only")
        result = reciprocal_rank_fusion([
            RecallPath(name="vector", weight=1.0, results=[a, b]),
            RecallPath(name="keyword", weight=1.0, results=[a]),
        ])
        assert set(result.hit_paths[str(a.chunk_id)]) == {"vector", "keyword"}
        assert result.hit_paths[str(b.chunk_id)] == ["vector"]

    def test_empty_paths(self):
        """空输入不应报错。"""
        result = reciprocal_rank_fusion([
            RecallPath(name="vector", weight=1.0, results=[]),
            RecallPath(name="keyword", weight=1.0, results=[]),
        ])
        assert result.chunks == []
        assert result.path_summary == {"vector": 0, "keyword": 0}

    def test_limit_applied(self):
        """limit 应截断结果。"""
        chunks = [make_chunk(f"doc{i}") for i in range(20)]
        result = reciprocal_rank_fusion(
            [RecallPath(name="vector", weight=1.0, results=chunks)], limit=5
        )
        assert len(result.chunks) == 5

    def test_k_smoothing_effect(self):
        """k 越大，头部名次优势越被削弱。

        验证：k=1 时第1名/第2名 = (1/2)/(1/3) = 1.500
              k=1000 时          = (1/1001)/(1/1002) ≈ 1.001
        即大 k 让名次差异趋于平缓。
        """
        a = make_chunk("first")
        b = make_chunk("second")
        small_k = reciprocal_rank_fusion(
            [RecallPath(name="vector", weight=1.0, results=[a, b])], k=1
        )
        large_k = reciprocal_rank_fusion(
            [RecallPath(name="vector", weight=1.0, results=[a, b])], k=1000
        )
        ratio_small = small_k.chunks[0].rrf_score / small_k.chunks[1].rrf_score
        ratio_large = large_k.chunks[0].rrf_score / large_k.chunks[1].rrf_score
        # 小 k 的头部优势更大
        assert ratio_small > ratio_large
        assert ratio_small == pytest.approx(1.5, abs=1e-3)

    def test_default_k_value(self):
        """默认 k 应为 60（原论文经验值）。"""
        assert DEFAULT_RRF_K == 60


# ======================================================================
#  证据等级加权
# ======================================================================
class TestEvidenceBoost:
    def test_guideline_overtakes_review_at_similar_scores(self):
        """重排分接近时，指南应超过综述（乘法加权应能反转相近分数）。"""
        guideline = make_chunk("guideline", evidence="guideline")
        review = make_chunk("review", evidence="review")
        guideline.rerank_score = 0.80
        review.rerank_score = 0.81

        # 不加权：综述（0.81）在前
        plain = boost_by_evidence([guideline, review], enabled=False)
        assert plain[0].doc_title == "review"

        # 加权：指南应反超（0.80*1.15=0.92 > 0.81*1.087=0.88）
        boosted = boost_by_evidence([guideline, review], enabled=True, strength=0.15)
        assert boosted[0].doc_title == "guideline"

    def test_low_relevance_guideline_does_not_leapfrog(self):
        """低相关但高证据等级的文档，不应跨越大量高相关文档。

        这是初版线性加权公式的真实缺陷：
        强度 0.8 时指南分 0.1 被抬到 0.42，而综述分 0.95 降到 0.36，
        相关性排序被完全颠倒。乘法加权必须避免这一点。
        """
        weak_guideline = make_chunk("weak_guideline", evidence="guideline")
        strong_review = make_chunk("strong_review", evidence="review")
        weak_guideline.rerank_score = 0.05   # 相关性极低
        strong_review.rerank_score = 0.95    # 相关性极高

        # 即使使用很大的强度，低相关文档也不应反超
        out = boost_by_evidence(
            [weak_guideline, strong_review], enabled=True, strength=0.5
        )
        assert out[0].doc_title == "strong_review"
        # 且仍保持原分数顺序
        assert out[0].rerank_score > out[1].rerank_score

    def test_boost_is_multiplicative_and_bounded(self):
        """加分幅度应正比于原分，且不超过设定的上限比例。"""
        guideline = make_chunk("g", evidence="guideline")
        other = make_chunk("o", evidence="other")
        guideline.rerank_score = 1.0
        other.rerank_score = 1.0

        out = boost_by_evidence([guideline, other], enabled=True, strength=0.15)
        scores = {c.doc_title: c.rerank_score for c in out}
        # 指南权重 100（最高）→ +15%
        assert scores["g"] == pytest.approx(1.15, abs=1e-6)
        # 其他权重 30（最低）→ +0%
        assert scores["o"] == pytest.approx(1.0, abs=1e-6)

    def test_proportional_boost_preserves_order_across_levels(self):
        """乘法加权下，相关性差距足够大时排序不受证据等级影响。"""
        # 相邻证据等级最多相差 15%，故原分相差超过 15% 时顺序应保持
        a = make_chunk("a", evidence="other")       # 最低证据
        b = make_chunk("b", evidence="guideline")   # 最高证据
        a.rerank_score = 0.90
        b.rerank_score = 0.70   # 相差 28%，超过最大加分 15%

        out = boost_by_evidence([a, b], enabled=True, strength=0.15)
        assert out[0].doc_title == "a"

    def test_disabled_keeps_order(self):
        """关闭加权时不应改变顺序。"""
        a = make_chunk("a", evidence="case")
        b = make_chunk("b", evidence="guideline")
        a.rerank_score = 0.9
        b.rerank_score = 0.5
        out = boost_by_evidence([a, b], enabled=False)
        assert [c.doc_title for c in out] == ["a", "b"]

    def test_unknown_evidence_level_safe(self):
        """未知证据等级不应抛异常。"""
        c = make_chunk("x", evidence="unknown_level")
        c.rerank_score = 0.5
        out = boost_by_evidence([c], enabled=True)
        assert len(out) == 1

    def test_strong_boost_does_not_erase_score_order(self):
        """加权强度为 0 时等同于不加权。"""
        a = make_chunk("a", evidence="case")
        b = make_chunk("b", evidence="guideline")
        a.rerank_score = 0.9
        b.rerank_score = 0.1
        out = boost_by_evidence([a, b], enabled=True, strength=0.0)
        assert out[0].doc_title == "a"


# ======================================================================
#  同文献限流
# ======================================================================
class TestDedupByDocument:
    def test_limits_chunks_per_document(self):
        """同一文献超过上限的块应被丢弃。"""
        doc = uuid.uuid4()
        chunks = [make_chunk(f"c{i}", doc_id=doc) for i in range(5)]
        chunks.append(make_chunk("other"))
        out = dedup_by_document(chunks, max_per_document=2)
        kept_titles = [c.doc_title for c in out]
        assert kept_titles.count("c0") == 1
        assert sum(1 for t in kept_titles if t.startswith("c")) == 2
        assert "other" in kept_titles

    def test_zero_means_no_limit(self):
        """max_per_document=0 表示不限制。"""
        doc = uuid.uuid4()
        chunks = [make_chunk(f"c{i}", doc_id=doc) for i in range(5)]
        out = dedup_by_document(chunks, max_per_document=0)
        assert len(out) == 5

    def test_keeps_highest_ranked(self):
        """应保留排名靠前（传入顺序靠前）的块。"""
        doc = uuid.uuid4()
        chunks = [make_chunk("first", doc_id=doc), make_chunk("second", doc_id=doc)]
        out = dedup_by_document(chunks, max_per_document=1)
        assert out[0].doc_title == "first"


# ======================================================================
#  关键词重叠打分
# ======================================================================
class TestTermOverlap:
    def test_full_overlap(self):
        assert term_overlap_score(["吻合", "合口", "口漏"], ["吻合", "合口", "口漏", "引流"]) == 1.0

    def test_partial_overlap(self):
        score = term_overlap_score(["吻合", "合口", "口漏"], ["吻合", "引流"])
        assert score == pytest.approx(1 / 3, rel=1e-6)

    def test_no_overlap(self):
        assert term_overlap_score(["吻合"], ["引流"]) == 0.0

    def test_empty_query_terms(self):
        assert term_overlap_score([], ["吻合"]) == 0.0

    def test_duplicate_query_terms_not_double_counted(self):
        """查询词元重复不应提高得分。"""
        score = term_overlap_score(["吻合", "吻合"], ["吻合"])
        assert score == 1.0
