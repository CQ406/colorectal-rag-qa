"""引用解析与溯源。

作用
----
LLM 在答案正文中标注 `[1][2]`，本模块负责：

  1. **解析**：从答案文本中提取引用标记及其出现位置
  2. **映射**：把编号映射回真实的 chunk / document（含页码、证据等级）
  3. **校验**：剔除 LLM 编造的编号（引用了不存在的证据）
  4. **流式支持**：答案逐字输出时，需处理标记被切分的边界情况

为什么采用「内联标记 + 事后解析」而非结构化 JSON
----------------------------------------------
结构化输出便于解析但**无法流式显示**——用户要等完整 JSON 生成完才看到答案。
内联标记方案下，前端先看到文字（体验好），引用关系事后解析得到（可溯源）。
这是本系统「可溯源」与「响应体验」之间的取舍，论文中需说明。
"""

from __future__ import annotations

import math

import re
from dataclasses import dataclass, field

# 匹配 [1] [12] 形式的引用标记；用 [1,2] 或 [1-3] 的写法也一并支持
_CITATION_PATTERN = re.compile(r"\[(\d+(?:\s*[-,，]\s*\d+)*)\]")


@dataclass
class Citation:
    """一条引用。"""

    index: int                      # 答案中出现的编号
    chunk_id: str = ""
    document_id: str = ""
    doc_title: str = ""
    doc_authors: str | None = None
    doc_journal: str | None = None
    doc_year: int | None = None
    page_start: int | None = None
    page_end: int | None = None
    section: str | None = None
    evidence_level: str = "other"
    evidence_level_zh: str = "其他"
    phase: str = "general"
    content_snippet: str = ""       # 原文片段，供前端展开查看
    positions: list[int] = field(default_factory=list)  # 在答案中的字符位置

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "chunk_id": self.chunk_id,
            "document_id": self.document_id,
            "doc_title": self.doc_title,
            "doc_authors": self.doc_authors,
            "doc_journal": self.doc_journal,
            "doc_year": self.doc_year,
            "page_start": self.page_start,
            "page_end": self.page_end,
            "section": self.section,
            "evidence_level": self.evidence_level,
            "evidence_level_zh": self.evidence_level_zh,
            "phase": self.phase,
            "content_snippet": self.content_snippet,
            "positions": self.positions,
        }


@dataclass
class ParseResult:
    """解析结果。"""

    citations: list[Citation] = field(default_factory=list)
    invalid_indices: list[int] = field(default_factory=list)   # LLM 编造的编号
    cited_count: int = 0                                        # 答案中出现的标记总数

    @property
    def has_citations(self) -> bool:
        return bool(self.citations)

    @property
    def citation_rate(self) -> float:
        """答案中有效引用的比例（用于质量监控）。"""
        if self.cited_count == 0:
            return 0.0
        return len(self.citations) / len(set(c.index for c in self.citations)) if self.citations else 0.0


def extract_citation_indices(text: str) -> tuple[list[int], dict[int, list[int]]]:
    """从答案文本中提取引用编号及其出现位置。

    支持 `[1]`、`[1,2]`、`[1-3]`、`[1，2]` 多种写法。

    Returns:
        (按出现顺序去重的编号列表, {编号: [字符位置, ...]})
    """
    order: list[int] = []
    positions: dict[int, list[int]] = {}

    for m in _CITATION_PATTERN.finditer(text):
        raw = m.group(1)
        # 展开区间与逗号分隔
        indices: list[int] = []
        for part in re.split(r"[，,]", raw):
            part = part.strip()
            if "-" in part:
                bounds = [p.strip() for p in part.split("-", 1)]
                try:
                    lo, hi = int(bounds[0]), int(bounds[1])
                except (ValueError, IndexError):
                    continue
                # 限制区间大小，防止 [1-9999] 这类恶意输入
                if 0 < lo <= hi <= lo + 20:
                    indices.extend(range(lo, hi + 1))
            elif part.isdigit():
                indices.append(int(part))

        for idx in indices:
            if idx not in positions:
                order.append(idx)
            positions.setdefault(idx, []).append(m.start())

    return order, positions


def _snippet(content: str, max_len: int = 300) -> str:
    """截取原文片段，优先在句末截断。"""
    content = content.strip()
    if len(content) <= max_len:
        return content
    cut = content[:max_len]
    # 尝试在最后一个句末标点处截断
    for sep in ("。", "；", ". ", "! ", "? "):
        pos = cut.rfind(sep)
        if pos > max_len // 2:
            return cut[: pos + len(sep)].strip() + "…"
    return cut.strip() + "…"


def build_citations(
    answer: str,
    evidences: list[dict],
) -> ParseResult:
    """把答案中的引用标记映射为完整的引用对象。

    Args:
        answer: LLM 生成的答案文本
        evidences: 送入 LLM 的证据列表，每项含 index / chunk_id / document_id /
                   content / doc_title / ... 等字段

    Returns:
        ParseResult，其中 citations 只包含**真实存在**的证据编号；
        LLM 编造的编号记入 invalid_indices 供质量监控。
    """
    result = ParseResult()
    if not answer:
        return result

    order, positions = extract_citation_indices(answer)
    result.cited_count = sum(len(v) for v in positions.values())

    # 建立编号 -> 证据 的映射
    by_index = {int(ev["index"]): ev for ev in evidences if "index" in ev}

    for idx in order:
        ev = by_index.get(idx)
        if ev is None:
            # LLM 引用了不存在的编号 —— 属于幻觉，需记录
            result.invalid_indices.append(idx)
            continue

        level_raw = ev.get("evidence_level", "other")
        try:
            from app.core.constants import EvidenceLevel

            level_zh = EvidenceLevel(level_raw).zh
        except ValueError:
            level_zh = "其他"

        result.citations.append(
            Citation(
                index=idx,
                chunk_id=str(ev.get("chunk_id", "")),
                document_id=str(ev.get("document_id", "")),
                doc_title=ev.get("doc_title", "") or "",
                doc_authors=ev.get("doc_authors"),
                doc_journal=ev.get("doc_journal"),
                doc_year=ev.get("doc_year"),
                page_start=ev.get("page_start"),
                page_end=ev.get("page_end"),
                section=ev.get("section"),
                evidence_level=level_raw,
                evidence_level_zh=level_zh,
                phase=ev.get("phase", "general"),
                content_snippet=_snippet(ev.get("content", "")),
                positions=positions.get(idx, []),
            )
        )

    return result


def evidences_from_search(results: list) -> list[dict]:
    """把检索结果转为提示词与引用解析所需的证据列表。

    Args:
        results: RetrievedChunk 列表（按排名顺序，第 1 名编号为 1）
    """
    evidences: list[dict] = []
    for i, item in enumerate(results, start=1):
        evidences.append(
            {
                "index": i,
                "chunk_id": str(getattr(item, "chunk_id", "")),
                "document_id": str(getattr(item, "document_id", "")),
                "content": getattr(item, "content", ""),
                "doc_title": getattr(item, "doc_title", ""),
                "doc_authors": getattr(item, "doc_authors", None),
                "doc_journal": getattr(item, "doc_journal", None),
                "doc_year": getattr(item, "doc_year", None),
                "page_start": getattr(item, "page_start", None),
                "page_end": getattr(item, "page_end", None),
                "section": getattr(item, "section", None),
                "evidence_level": getattr(item, "evidence_level", "other"),
                "phase": getattr(item, "phase", "general"),
                "score": getattr(item, "score", 0.0),
                "rerank_score": getattr(item, "rerank_score", None),
            }
        )
    return evidences


def strip_citation_markers(text: str) -> str:
    """移除引用标记（用于生成纯文本答案，如导出或语音朗读）。"""
    return _CITATION_PATTERN.sub("", text)


# ======================================================================
#  数值主张核验（针对实测发现的高危幻觉类型）
# ======================================================================
# 背景：阶段十用 LLM 裁判评测 40 题，发现最危险的幻觉不是整段编造，
# 而是**编造看起来专业的精确细节** —— 例如把文献中没有的
# 「中转率 7.1% vs 3.4%」「病理完全缓解率最高 30%」写得像有据可依。
#
# 更关键的是实测结果：在系统提示词中**明确禁止**写出证据中没有的数字、
# 并要求核对归因后重测（同一批 40 题），
#   - 数值编造占比 22% → 26%（未下降）
#   - 平均忠实度 0.733 → 0.721（无显著变化）
#   - 个别编造案例（PROSPECT/NORAD、30% pCR）**逐字重现**
# 说明该问题**无法仅靠提示词解决**，模型生成临床数字的先验过强。
#
# 因此本模块在答案生成后独立提取数值主张，与检索证据比对，
# 把「证据中找不到的数字」显式标注给用户。
# 这是提示词约束之外的**独立一层防线**，不依赖模型配合。

# 需要核验的数值模式（高信号、低误报优先）
_NUMERIC_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # 百分比：7.1%、30 %
    ("百分比", re.compile(r"(\d+(?:\.\d+)?)\s*%")),
    # 比值与置信区间：OR 0.66、HR 1.23
    ("比值", re.compile(r"\b(?:OR|RR|HR|aOR)\s*[=:]?\s*(\d+(?:\.\d+)?)", re.I)),
    # 样本量：578 例、471 名患者
    ("样本量", re.compile(r"(\d+)\s*(?:例|名|位)(?:患者|病人)?")),
    ("样本量", re.compile(r"\bn\s*=\s*(\d+)", re.I)),
    # 具体时长
    ("时长", re.compile(r"(\d+(?:\.\d+)?)\s*(?:天|周|个月|小时)(?![前后代])")),
]

# 年份不是需要核验的临床数值（如「2024 年」）
_YEAR_PATTERN = re.compile(r"^(?:19|20)\d{2}$")


@dataclass
class NumericClaim:
    """一条数值主张及其核验结果。"""

    kind: str          # 百分比 / 比值 / 样本量 / 时长
    raw: str           # 原文中的完整表述
    value: str         # 提取出的数值
    supported: bool    # 是否能在证据中找到
    position: int      # 在答案中的字符位置

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "raw": self.raw,
            "value": self.value,
            "supported": self.supported,
            "position": self.position,
        }


def extract_numeric_claims(answer: str) -> list[NumericClaim]:
    """从答案中提取需要核验的数值主张。"""
    claims: list[NumericClaim] = []
    seen_spans: list[tuple[int, int]] = []

    for kind, pattern in _NUMERIC_PATTERNS:
        for m in pattern.finditer(answer):
            span = m.span()
            # 避免同一位置被多个模式重复匹配
            if any(s <= span[0] < e for s, e in seen_spans):
                continue
            value = m.group(1)
            if _YEAR_PATTERN.match(value):
                continue
            seen_spans.append(span)
            claims.append(
                NumericClaim(
                    kind=kind, raw=m.group(0), value=value,
                    supported=False, position=span[0],
                )
            )

    claims.sort(key=lambda c: c.position)
    return claims


_NUM_TOKEN_RE = re.compile(r"(?<![\d.])(\d+(?:\.\d+)?)(?![\d])")


def _evidence_numbers(haystack: str) -> set[float]:
    """抽取证据中出现的全部数值（已去千分位）。"""
    out: set[float] = set()
    for m in _NUM_TOKEN_RE.finditer(haystack):
        try:
            out.add(float(m.group(1)))
        except ValueError:
            continue
    return out


def verify_numeric_claims(
    answer: str,
    evidence_texts: list[str],
) -> tuple[list[NumericClaim], list[NumericClaim]]:
    """核验答案中的数值主张是否有证据支撑。

    核验方式：把证据里的数值抽成集合，按**数值相等**比较（而非字符串包含）。
    纯字符串包含会把 7.1 当成 17.1、把 3 当成 2023 命中（已实测复现）；
    而字符串+边界又会把 30% 与 0.30 判成不同值。数值比较同时解决这两类问题。

    这是**召回导向**的检查 —— 宁可漏报也不误报，
    因为把有据的数字误标为「无据」会损害用户对系统的信任。

    等价写法：
      - 原样（7.1）与去千分位（1,234 → 1234）
      - 百分比与小数互转（30% ↔ 0.30，7.5% ↔ 0.075）

    Args:
        answer: LLM 生成的答案
        evidence_texts: 检索到的证据正文列表

    Returns:
        (supported, unsupported) 两个列表
    """
    claims = extract_numeric_claims(answer)
    if not claims:
        return [], []

    haystack = "\n".join(evidence_texts)
    # 归一化：去千分位逗号、压缩空白，提高匹配率
    haystack_norm = re.sub(r"(?<=\d),(?=\d)", "", haystack)
    haystack_norm = re.sub(r"\s+", "", haystack_norm)
    ev_numbers = _evidence_numbers(haystack_norm)

    supported: list[NumericClaim] = []
    unsupported: list[NumericClaim] = []

    for claim in claims:
        value = claim.value
        numeric: set[float] = set()
        try:
            num = float(value.replace(",", ""))
            numeric.add(num)
            # 百分比与小数互换：30 ↔ 0.30；7.5 ↔ 0.075
            numeric.add(num / 100)
            if num <= 1:
                numeric.add(num * 100)
        except ValueError:
            pass

        # 数值相等即视为有据；浮点用极小相对容差，避免 0.30000000000004 这类误差
        hit = any(
            math.isclose(c, e, rel_tol=1e-9, abs_tol=1e-12)
            for c in numeric
            for e in ev_numbers
        )
        claim.supported = hit
        (supported if hit else unsupported).append(claim)

    return supported, unsupported


def unsupported_numeric_summary(
    unsupported: list[NumericClaim],
    limit: int = 6,
) -> str:
    """把无据数值汇总成一句提示语。"""
    if not unsupported:
        return ""
    items = [c.raw.strip() for c in unsupported[:limit]]
    more = "" if len(unsupported) <= limit else f" 等 {len(unsupported)} 处"
    return "以下数值未在检索证据中找到，请谨慎核实：" + "、".join(items) + more
