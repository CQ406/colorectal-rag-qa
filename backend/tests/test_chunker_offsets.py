"""切块偏移（char_start/char_end）的回归测试。

背景：原实现用 `text.find(content[:20], char_cursor)` 反推位置，
带 overlap 时 content 以"上一块尾部"开头，find 返回 -1 → 回退成 char_cursor，
偏移整体错位。虽然该字段当前只写不读，但一旦用于原文高亮/回链就会定位错。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.etl.chunker import chunk_by_tokens  # noqa: E402


def _text(n_sentences: int) -> str:
    return "".join(f"这是第{i}句用于测试切块边界的示例文本，需要足够长以触发分块。" for i in range(n_sentences))


def test_char_offsets_match_original_text():
    text = _text(30)
    chunks = chunk_by_tokens(text, max_tokens=80, overlap_tokens=10)
    assert len(chunks) > 1, "应切成多块"
    for c in chunks:
        assert c.char_start is not None and c.char_end is not None
        # 关键断言：用记录的偏移回原文取子串，应与块内容一致
        extracted = text[c.char_start:c.char_end]
        assert extracted.strip() == c.content.strip(), (
            f"第 {c.index} 块偏移与内容不符："
            f"偏移取到 {extracted[:20]!r}，内容为 {c.content[:20]!r}"
        )


def test_offsets_monotonic_with_overlap():
    text = _text(30)
    chunks = chunk_by_tokens(text, max_tokens=80, overlap_tokens=10)
    starts = [c.char_start for c in chunks]
    assert starts == sorted(starts), f"起点应单调递增，实际 {starts}"
    assert all(c.char_end > c.char_start for c in chunks)


def test_no_overlap_still_correct():
    text = _text(20)
    chunks = chunk_by_tokens(text, max_tokens=80, overlap_tokens=0)
    for c in chunks:
        assert text[c.char_start:c.char_end].strip() == c.content.strip()
