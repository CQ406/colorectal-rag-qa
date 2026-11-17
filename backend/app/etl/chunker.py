"""文本切分器。

设计原则
--------
1. **保留溯源信息**：每个块必须带页码/章节/字符偏移，这是引用溯源的前提。
2. **章节感知**：结构化摘要按小节切分，不用固定窗口硬切，
   避免把「方法」和「结论」切进同一块导致语义混杂。
3. **块大小自适应**：短文本不切；长文本按句子边界聚合到目标 token 数。

与后续全文 PDF 的关系
---------------------
PubMed 摘要通常 200~400 词，多数为单块。当补充全文 PDF 后，
`split_document` 会处理多页、多章节的长文本，接口保持不变。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.core.config import settings
from app.core.text_utils import estimate_tokens

# 句子结束符（中英文）
_SENTENCE_END = re.compile(r"(?<=[。！？；.!?;])\s*")

DEFAULT_MAX_TOKENS = settings.model.embedding_max_length  # 512
DEFAULT_OVERLAP_TOKENS = 64


@dataclass
class TextChunk:
    """切分后的文本块。"""

    content: str
    index: int                                    # 在文档内的序号
    section: str | None = None                    # 章节标题
    page_start: int | None = None
    page_end: int | None = None
    char_start: int | None = None
    char_end: int | None = None
    token_count: int = 0
    char_count: int = 0
    metadata: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.char_count = len(self.content)
        self.token_count = estimate_tokens(self.content)


def split_sentences(text: str) -> list[str]:
    """按句子边界切分，保留标点。"""
    if not text.strip():
        return []
    parts = _SENTENCE_END.split(text)
    return [p.strip() for p in parts if p.strip()]



def sentence_spans(text: str) -> list[tuple[str, int, int]]:
    """切句并返回每句的 (句子, 绝对起点, 绝对终点)。

    为什么单独实现而不复用 `split_sentences`：后者返回去空白后的字符串，
    丢失了位置信息；而切块需要 char_start/char_end 做原文定位。
    这里按顺序在原文中查找每句，保证偏移与原文一致。
    """
    spans: list[tuple[str, int, int]] = []
    cursor = 0
    for part in _SENTENCE_END.split(text):
        clean = part.strip()
        if not clean:
            continue
        idx = text.find(clean, cursor)
        if idx < 0:
            idx = cursor
        spans.append((clean, idx, idx + len(clean)))
        cursor = idx + len(clean)
    return spans

def chunk_by_tokens(
    text: str,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    overlap_tokens: int = DEFAULT_OVERLAP_TOKENS,
    section: str | None = None,
    page_start: int | None = None,
) -> list[TextChunk]:
    """按句子边界把长文本聚合为不超过 max_tokens 的块。

    先按句子切分，再贪心聚合；超过上限时另起一块，
    并保留 overlap_tokens 的重叠以维持上下文连贯。

    Args:
        text: 待切分文本
        max_tokens: 单块 token 上限
        overlap_tokens: 相邻块的重叠 token 数
        section: 章节标题（写入每个块）
        page_start: 起始页
    """
    text = text.strip()
    if not text:
        return []

    # 短文本直接成块，不做无意义的切分
    if estimate_tokens(text) <= max_tokens:
        return [
            TextChunk(
                content=text, index=0, section=section,
                page_start=page_start, page_end=page_start,
                char_start=0, char_end=len(text),
            )
        ]

    spans = sentence_spans(text)
    sentences = [s for s, _, _ in spans]
    if not sentences:
        return []

    chunks: list[TextChunk] = []
    buffer: list[str] = []
    buffer_starts: list[int] = []
    buffer_tokens = 0
    char_cursor = 0

    def flush() -> None:
        nonlocal buffer, buffer_starts, buffer_tokens, char_cursor
        if not buffer:
            return
        content = "".join(buffer).strip()
        if not content:
            buffer, buffer_tokens = [], 0
            return
        # 用首句的真实起点与末句的真实终点，而不是事后 find。
        # 带 overlap 时 content 以"上一块尾部"开头，find 会返回 -1。
        start = buffer_starts[0] if buffer_starts else char_cursor
        end = start + sum(len(s) for s in buffer)
        # 去掉首尾空白造成的偏差
        start += len(buffer[0]) - len(buffer[0].lstrip())
        end -= len(buffer[-1]) - len(buffer[-1].rstrip())
        chunks.append(
            TextChunk(
                content=content,
                index=len(chunks),
                section=section,
                page_start=page_start,
                page_end=page_start,
                char_start=start,
                char_end=end,
            )
        )
        char_cursor = end
        # 保留尾部作为下一块的重叠上下文
        overlap_text = ""
        if overlap_tokens > 0:
            kept: list[str] = []
            kept_tokens = 0
            for sent in reversed(buffer):
                t = estimate_tokens(sent)
                if kept_tokens + t > overlap_tokens:
                    break
                kept.insert(0, sent)
                kept_tokens += t
            overlap_text = "".join(kept)
        if overlap_text:
            # 重叠部分对应末尾若干句，起点取这些句子在原文中的位置
            kept_count = len(kept)
            buffer = [overlap_text]
            buffer_starts = buffer_starts[-kept_count:] if kept_count else []
        else:
            buffer = []
            buffer_starts = []
        buffer_tokens = estimate_tokens(overlap_text) if overlap_text else 0

    for sentence, sent_start, _sent_end in spans:
        tokens = estimate_tokens(sentence)
        # 单句超限：强制断开，避免死循环
        if tokens > max_tokens:
            flush()
            for piece in _hard_split(sentence, max_tokens):
                chunks.append(
                    TextChunk(
                        content=piece,
                        index=len(chunks),
                        section=section,
                        page_start=page_start,
                        page_end=page_start,
                    )
                )
            continue

        if buffer_tokens + tokens > max_tokens and buffer:
            flush()

        buffer.append(sentence)
        buffer_starts.append(sent_start)
        buffer_tokens += tokens

    # 收尾：最后一块不要重复的重叠内容
    if buffer:
        content = "".join(buffer).strip()
        if content and (not chunks or content not in (chunks[-1].content if chunks else "")):
            start = buffer_starts[0] if buffer_starts else char_cursor
            end_pos = start + sum(len(s) for s in buffer)
            start += len(buffer[0]) - len(buffer[0].lstrip())
            end_pos -= len(buffer[-1]) - len(buffer[-1].rstrip())
            chunks.append(
                TextChunk(
                    content=content,
                    index=len(chunks),
                    section=section,
                    page_start=page_start,
                    page_end=page_start,
                    char_start=start,
                    char_end=end_pos,
                )
            )

    # 修正序号
    for i, c in enumerate(chunks):
        c.index = i
    return chunks


def _hard_split(text: str, max_tokens: int) -> list[str]:
    """对超长单句强制按字符切分。"""
    # 按 token 上限估算字符数（中文 1 字≈1 token，英文 4 字符≈1 token）
    approx_chars = max_tokens
    return [
        text[i : i + approx_chars]
        for i in range(0, len(text), approx_chars)
        if text[i : i + approx_chars].strip()
    ]


def split_structured_abstract(
    sections: list[tuple[str, str]],
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> list[TextChunk]:
    """切分结构化摘要。

    结构化摘要（Background/Methods/Results/Conclusions）按小节切分，
    保留小节名作为 section 元数据，便于检索时区分信息类型。

    Args:
        sections: [(小节名, 正文), ...]
    """
    chunks: list[TextChunk] = []
    for label, body in sections:
        if not body.strip():
            continue
        effective_label = label or None
        for c in chunk_by_tokens(body, max_tokens=max_tokens, section=effective_label):
            c.index = len(chunks)
            chunks.append(c)
    return chunks


def split_document(
    sections: list[tuple[str, str]],
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> list[TextChunk]:
    """统一入口：切分一份文档的多个章节。

    sections 中每项为 (章节标题, 正文)。摘要场景传入结构化小节，
    全文 PDF 场景传入按页/按标题拆分的内容。
    """
    return split_structured_abstract(sections, max_tokens=max_tokens)
