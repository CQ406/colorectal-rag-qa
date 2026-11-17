"""基于版面块（layout blocks）的 PDF 文本抽取。

为什么必须换掉「按标点合并」的做法
----------------------------------
初版实现 `merge_wrapped_lines` 反复扫描并合并行，直到不再变化。
问题在于：中文期刊 PDF 里大量行以数字、英文字母或括号结尾，
`_ends_sentence` 判定为「未结束」，于是与下一行合并；
合并后的行**仍然**不满足结束条件，继续合并 —— 一路吞掉整篇文档。

实测后果：《直肠肿瘤三维可视化指导精准手术专家共识（2025版）》
1080 行被合并成 **2 行**，汉字从数千降到 178。

**根因是设计缺陷**：靠标点推断「行是否结束」在中文期刊排版下不可靠，
且循环合并没有边界约束，一旦判定失误就是灾难性的。

正确做法：用 PDF 自身的版面块
------------------------------
`page.get_text("blocks")` 返回 PDF 解析器识别出的文本块，每个块带坐标边框。
块边界就是天然的段落边界 —— 这是 PDF 解析器已经做好的版面分析，
比按标点猜测可靠得多。

分栏处理
--------
双栏排版时，块坐标会显示明显的左右分区。按 x 坐标把块分组到栏，
栏内按 y 排序，栏间按页内阅读顺序（先左栏后右栏）。
不做这一步会把左右栏交错拼接，产生语义错乱。

块内仍可能有换行（PDF 按固定宽度断行），因此块内做行合并，
但**块边界不再跨越** —— 这是关键约束。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import pymupdf

_CITE_RE = re.compile(r"[［\[]\s*\d+(?:\s*[-–—⁃,，、]\s*\d+)*\s*[］\]]")
_CJK = re.compile(r"[\u4e00-\u9fff\u3000-\u303f\uff00-\uffef]")
_LATIN = re.compile(r"[A-Za-z0-9]")

# 行尾终止标点：出现即认为该行是完整句子
_SENT_END = "。！？；!?;"


@dataclass
class Block:
    """一个版面块。"""

    page: int
    x0: float
    y0: float
    x1: float
    y1: float
    text: str
    lines: list[str] = field(default_factory=list)

    @property
    def x_center(self) -> float:
        return (self.x0 + self.x1) / 2


def _is_cjk(ch: str) -> bool:
    return bool(ch) and bool(_CJK.match(ch))


def _join(prev: str, nxt: str) -> str:
    """合并两行，按字符类型决定是否插空格。"""
    if not prev:
        return nxt
    if not nxt:
        return prev
    last, first = prev[-1], nxt[0]
    if _LATIN.match(last) and _LATIN.match(first):
        return prev + " " + nxt
    if _is_cjk(last) and _is_cjk(first):
        return prev + nxt
    return prev + " " + nxt


def _clean(s: str) -> str:
    t = s.replace("\u00ad", "").replace("\u3000", " ")
    t = _CITE_RE.sub("", t)
    return re.sub(r"[ \t]+", " ", t).strip()


def _merge_block_lines(lines: list[str]) -> str:
    """块内合并行（块边界不跨越）。

    块内换行是 PDF 按宽度断行造成的，应合并为完整段落。
    但行尾若已是终止标点，说明该块内含多个句子，保留分段。
    """
    out: list[str] = []
    buf = ""
    for ln in lines:
        if not ln:
            continue
        buf = _join(buf, ln)
        if ln[-1] in _SENT_END:
            out.append(buf)
            buf = ""
    if buf:
        out.append(buf)
    return "\n".join(out)


def extract_blocks(pdf_path: str | Path, min_chars: int = 12) -> tuple[list[Block], dict]:
    """抽取版面块，并按分栏情况排序。

    Returns:
        (按阅读顺序排列的块列表, 元信息)
    """
    path = Path(pdf_path)
    doc = pymupdf.open(path)
    pages_blocks: list[list[Block]] = []
    page_widths: list[float] = []

    for pno, page in enumerate(doc):
        page_widths.append(page.rect.width)
        blocks: list[Block] = []
        for b in page.get_text("blocks"):
            # blocks 返回 (x0, y0, x1, y1, text, block_no, block_type)
            x0, y0, x1, y1, text = b[0], b[1], b[2], b[3], b[4]
            btype = b[6] if len(b) > 6 else 0
            if btype != 0:          # 非文本块（图片）跳过
                continue
            t = (text or "").strip()
            if len(t) < min_chars:
                continue
            lines = [_clean(x) for x in t.split("\n")]
            lines = [x for x in lines if x]
            if not lines:
                continue
            blocks.append(Block(page=pno, x0=x0, y0=y0, x1=x1, y1=y1,
                                text=t, lines=lines))
        pages_blocks.append(blocks)

    doc.close()
    avg_width = sum(page_widths) / len(page_widths) if page_widths else 600.0

    ordered: list[Block] = []
    for blocks in pages_blocks:
        ordered.extend(_order_page(blocks, avg_width))

    meta = {
        "file": path.name,
        "pages": len(pages_blocks),
        "blocks": len(ordered),
        "avg_page_width": round(avg_width, 1),
    }
    return ordered, meta


def _order_page(blocks: list[Block], page_width: float) -> list[Block]:
    """把一页的块按阅读顺序排列，处理分栏。

    判据：若存在块的右边界明显小于页宽一半（左栏），
    且存在块的左边界明显大于页宽一半（右栏），则认为是双栏。
    """
    if not blocks:
        return []

    mid = page_width / 2
    left = [b for b in blocks if b.x1 <= mid * 1.05]
    right = [b for b in blocks if b.x0 >= mid * 0.95]
    spanning = [b for b in blocks if b not in left and b not in right]

    two_col = (
        len(left) >= 2 and len(right) >= 2
        and len(left) + len(right) >= len(blocks) * 0.6
    )
    if not two_col:
        return sorted(blocks, key=lambda b: (round(b.y0, 1), b.x0))

    # 双栏：跨栏块（标题、通栏图注）按 y 插入阅读顺序
    def col_key(b: Block) -> tuple:
        in_left = b.x1 <= mid * 1.05
        return (0 if in_left else 1, round(b.y0, 1))

    result: list[Block] = []
    for b in sorted(spanning, key=lambda x: x.y0):
        result.append(b)
    result.extend(sorted(left, key=lambda b: b.y0))
    result.extend(sorted(right, key=lambda b: b.y0))
    return result


def to_paragraphs(pdf_path: str | Path) -> tuple[list[str], dict]:
    """抽取为段落列表：块内合并行，块即段落。"""
    blocks, meta = extract_blocks(pdf_path)
    paras: list[str] = []
    for b in blocks:
        text = _merge_block_lines(b.lines)
        for seg in text.split("\n"):
            seg = seg.strip()
            if len(seg) >= 12:
                paras.append(seg)
    meta["paragraphs"] = len(paras)
    meta["chars"] = sum(len(p) for p in paras)
    meta["han"] = sum(1 for p in paras for ch in p if "\u4e00" <= ch <= "\u9fff")
    return paras, meta


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("用法: python extract_by_blocks.py <pdf路径>")
        raise SystemExit(1)
    paras, meta = to_paragraphs(sys.argv[1])
    print("文件: %s" % meta["file"])
    print("  页 %d | 块 %d | 段落 %d | 字符 %d | 汉字 %d" % (
        meta["pages"], meta["blocks"], meta["paragraphs"],
        meta["chars"], meta["han"]))
    print()
    for p in paras[:8]:
        print("  · %s" % p[:92])
