"""引用解析与溯源的单元测试。

不依赖数据库、GPU 与 LLM，可快速运行：
    python -m pytest tests/test_citation.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.citation import (
    build_citations,
    extract_citation_indices,
    strip_citation_markers,
    verify_numeric_claims,
)


@pytest.fixture
def evidences() -> list[dict]:
    """三条测试证据。"""
    return [
        {
            "index": 1, "chunk_id": "c1", "document_id": "d1",
            "content": "吻合口漏发生率约3%~15%。处理原则包括充分引流与广谱抗生素。",
            "doc_title": "中国结直肠癌诊疗规范(2023版)",
            "doc_journal": "中华消化外科杂志", "doc_year": 2023,
            "page_start": 850, "page_end": 851, "section": "术后并发症处理",
            "evidence_level": "guideline", "phase": "postop",
        },
        {
            "index": 2, "chunk_id": "c2", "document_id": "d2",
            "content": "推荐术后24小时内开始经口进食。",
            "doc_title": "加速康复外科应用进展",
            "doc_journal": "中国实用外科杂志", "doc_year": 2024,
            "page_start": 146, "page_end": 146, "section": "术后管理",
            "evidence_level": "review", "phase": "postop",
        },
        {
            "index": 3, "chunk_id": "c3", "document_id": "d3",
            "content": "吻合口漏的处理需个体化决策。",
            "doc_title": "Anastomotic leakage in colorectal cancer surgery",
            "doc_journal": "Surgical Oncology", "doc_year": 2022,
            "page_start": 1, "page_end": 1, "section": "Abstract",
            "evidence_level": "systematic", "phase": "postop",
        },
    ]


# ======================================================================
#  标记提取
# ======================================================================
class TestExtractIndices:
    def test_single_marker(self):
        order, positions = extract_citation_indices("结论如下[1]。")
        assert order == [1]
        assert positions[1] == [4]

    def test_multiple_markers(self):
        order, _ = extract_citation_indices("证据[1]和[3]支持该结论。")
        assert order == [1, 3]

    def test_adjacent_markers(self):
        order, _ = extract_citation_indices("处理包括引流与抗生素[1][3]。")
        assert order == [1, 3]

    def test_comma_separated(self):
        order, _ = extract_citation_indices("相关研究见[1,2]。")
        assert order == [1, 2]

    def test_full_width_comma(self):
        """中文全角逗号也应识别。"""
        order, _ = extract_citation_indices("相关研究见[1，2]。")
        assert order == [1, 2]

    def test_range(self):
        order, _ = extract_citation_indices("综合多项证据[1-3]。")
        assert order == [1, 2, 3]

    def test_range_size_limited(self):
        """超大区间应被拒绝，防止恶意输入导致内存膨胀。"""
        order, _ = extract_citation_indices("异常引用[1-9999]。")
        assert order == []

    def test_duplicate_markers_dedup_order(self):
        """重复标记应去重，但记录全部位置。"""
        order, positions = extract_citation_indices("结论[1]，另有支持[1]。")
        assert order == [1]
        assert len(positions[1]) == 2

    def test_no_markers(self):
        order, positions = extract_citation_indices("这是没有任何引用的答案。")
        assert order == []
        assert positions == {}

    def test_malformed_markers_ignored(self):
        """非数字或格式错误的方括号不应被当作引用。"""
        order, _ = extract_citation_indices("见[abc]与[]与[注1]。")
        assert order == []


# ======================================================================
#  引用映射
# ======================================================================
class TestBuildCitations:
    def test_valid_citations_mapped(self, evidences):
        parsed = build_citations("发生率约3%~15%[1]。", evidences)
        assert len(parsed.citations) == 1
        c = parsed.citations[0]
        assert c.index == 1
        assert c.chunk_id == "c1"
        assert c.doc_title == "中国结直肠癌诊疗规范(2023版)"
        assert c.evidence_level_zh == "临床指南"
        assert c.page_start == 850
        assert c.section == "术后并发症处理"

    def test_invalid_index_recorded(self, evidences):
        """引用了不存在的编号应记入 invalid_indices（幻觉信号）。"""
        parsed = build_citations("这是编造引用[99]。", evidences)
        assert parsed.citations == []
        assert parsed.invalid_indices == [99]

    def test_mixed_valid_and_invalid(self, evidences):
        """有效与无效引用应分别处理，不互相干扰。"""
        parsed = build_citations("依据[1]与编造[99]。", evidences)
        assert [c.index for c in parsed.citations] == [1]
        assert parsed.invalid_indices == [99]

    def test_positions_recorded(self, evidences):
        """应记录引用在答案中的字符位置，供前端渲染可点击标记。"""
        text = "第一处[1]，第二处[2]。"
        parsed = build_citations(text, evidences)
        by_index = {c.index: c for c in parsed.citations}
        assert text[by_index[1].positions[0] : by_index[1].positions[0] + 3] == "[1]"
        assert text[by_index[2].positions[0] : by_index[2].positions[0] + 3] == "[2]"

    def test_snippet_truncated(self, evidences):
        """原文片段应截断，避免响应体过大。"""
        long_ev = [dict(evidences[0], content="吻合口漏" * 200)]
        parsed = build_citations("结论[1]。", long_ev)
        assert len(parsed.citations[0].content_snippet) <= 320
        assert parsed.citations[0].content_snippet.endswith("…")

    def test_short_snippet_unchanged(self, evidences):
        parsed = build_citations("结论[1]。", evidences)
        assert "充分引流" in parsed.citations[0].content_snippet
        assert not parsed.citations[0].content_snippet.endswith("…")

    def test_empty_answer(self, evidences):
        parsed = build_citations("", evidences)
        assert parsed.citations == []
        assert parsed.cited_count == 0

    def test_empty_evidences(self):
        """没有证据时，任何引用都是无效的。"""
        parsed = build_citations("结论[1]。", [])
        assert parsed.citations == []
        assert parsed.invalid_indices == [1]

    def test_cited_count(self, evidences):
        """cited_count 应统计标记出现总次数（含重复）。"""
        parsed = build_citations("结论[1]，另有[1]与[2]。", evidences)
        assert parsed.cited_count == 3

    def test_to_dict_serializable(self, evidences):
        """引用对象应可序列化为 JSON（接口返回需要）。"""
        import json

        parsed = build_citations("结论[1]。", evidences)
        data = json.dumps(parsed.citations[0].to_dict(), ensure_ascii=False)
        assert "中国结直肠癌诊疗规范" in data


# ======================================================================
#  工具函数
# ======================================================================
class TestStripMarkers:
    def test_strip_all_markers(self):
        assert strip_citation_markers("结论[1]与[2]。") == "结论与。"

    def test_strip_range(self):
        assert strip_citation_markers("依据[1-3]。") == "依据。"

    def test_no_markers_unchanged(self):
        text = "没有引用的文本。"
        assert strip_citation_markers(text) == text

    def test_preserves_other_brackets(self):
        """非引用格式的方括号应保留。"""
        text = "见[附录A]与[1]。"
        assert strip_citation_markers(text) == "见[附录A]与。"

class TestVerifyNumericClaims:
    """数值核验的回归测试。

    背景：原实现用子串包含判断，实测把「7.1%」判成被「17.1%」支撑、
    把「3%」判成被「2023 例」支撑，等于第三道防线失效。
    改为按数值相等比较后，需要这些用例锁定行为。
    """

    def test_does_not_match_substring_of_larger_number(self):
        """7.1 不应被 17.1 命中。"""
        sup, uns = verify_numeric_claims("发生率为 7.1%", ["报告发生率 17.1%"])
        assert len(sup) == 0
        assert len(uns) == 1

    def test_does_not_match_digit_inside_year(self):
        """3% 不应被 2023 命中。"""
        sup, uns = verify_numeric_claims("发生率约 3%", ["该研究纳入 2023 例患者"])
        assert len(sup) == 0
        assert len(uns) == 1

    def test_exact_number_is_supported(self):
        sup, uns = verify_numeric_claims(
            "吻合口漏发生率 7.1%", ["本组吻合口漏发生率 7.1%（12/169）"]
        )
        assert len(sup) == 1 and not uns

    def test_percent_decimal_equivalence(self):
        """30% 与 0.30 应视为同一数值。"""
        sup, uns = verify_numeric_claims("约占 30%", ["该比例为 0.30"])
        assert len(sup) == 1 and not uns

    def test_thousands_separator(self):
        """1,234 与 1234 应视为同一数值。"""
        sup, uns = verify_numeric_claims("样本量 1234 例", ["共纳入 1,234 例患者"])
        assert len(sup) == 1 and not uns

    def test_no_claims_returns_empty(self):
        assert verify_numeric_claims("无具体数值的结论。", ["证据"]) == ([], [])