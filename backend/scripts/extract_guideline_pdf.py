"""从中文指南/共识 PDF 抽取正文，输出可校对的结构化草稿。

流程（关键：合并必须发生在标题识别之前）
--------------------------------------
    PDF
     → 逐页提取文本（PyMuPDF）
     → 去掉每页重复的页眉/页脚
     → 去上标引用标记
     → **第一步合并**：行尾未结束则与下一行拼接（修复 PDF 固定宽度换行）
     → 第二步：按中文编号识别章节与子节
     → 输出 Markdown 草稿

为什么合并要先于标题识别
------------------------
初版实现未合并就判定标题，导致两类错误：
  - 「2. 美国麻醉师协会（American Society of Anesthesiologists，」被当成
    完整标题，续行「ASA）分级：……」却变成正文
  - 段落以「。」开头（上一句被切断后，续行成了新段落）
把合并放在最前面，这两类问题一并消除。

行合并规则
----------
  行尾是句末标点（。！？；）→ 结束，不合并
  行尾是逗号、顿号、冒号、或**无标点** → 未结束，与下一行拼接
  拼接时按字符类型决定是否加空格：
    汉字↔汉字：不加（否则出现「发生 率」）
    拉丁↔拉丁：加空格（否则出现「anastomoticleakage」）

例外：冒号结尾的行如果是短标题（如「1. 性别：」），**仍与下一行合并** ——
中文文献里冒号后接换行很常见，不能一律当标题。

仍需人工校对的内容
------------------
  - 表格：PDF 表格会被抽成错乱的文本流，**本脚本不还原表格**，
    含关键数据的表格需人工补录
  - 图片：图注可提取，图本身丢失
  - 数值：必须逐处核对，这是医学文本最关键的一点
"""

from __future__ import annotations

import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import pymupdf

_CITE_RE = re.compile(r"[［\[]\s*\d+(?:\s*[-–—⁃,，、]\s*\d+)*\s*[］\]]")

_H1_RE = re.compile(r"^([一二三四五六七八九十]+)\s*[、.．]\s*(\S.*)?$")
_H2_RE = re.compile(r"^[（(]\s*([一二三四五六七八九十]+)\s*[）)]\s*(\S.*)?$")
_H2_NUM_RE = re.compile(r"^(\d{1,2})\s*[.．、]\s*([\u4e00-\u9fff].{0,60})$")

_SKIP_PATTERNS = [
    re.compile(r"^参考文献\s*$"),
    re.compile(r"^参\s*考\s*文\s*献"),
    re.compile(r"^通信作者"),
    re.compile(r"^DOI[:：]"),
    re.compile(r"^基金项目"),
    re.compile(r"^收稿日期"),
    re.compile(r"^利益冲突"),
    re.compile(r"^作者单位"),
    re.compile(r"^本文编辑"),
    re.compile(r"^\(\s*收稿"),
]

_SENT_END = "。！？；!?;"
# 未结束信号：逗号、顿号、冒号、或行尾无标点
_OPEN_END = "，,、：:"
_CJK = re.compile(r"[\u4e00-\u9fff\u3000-\u303f\uff00-\uffef]")
_LATIN = re.compile(r"[A-Za-z0-9]")


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


def _clean_line(line: str) -> str:
    t = line.replace("\u00ad", "")
    t = t.replace("\u3000", " ")
    t = _CITE_RE.sub("", t)
    t = re.sub(r"[ \t]+", " ", t)
    return t.strip()


def _ends_sentence(text: str) -> bool:
    """判断该行是否以终止标点结束。

    只认真正的句末标点。**不把「行尾是数字或字母」当作截断信号** ——
    初版这样做，导致「发生率2.4%」这类正常结尾也被判为未结束，
    进而跨标题一路合并，整篇被压成一个段落（768 行→38 行）。

    行尾仅剩 `。` `；` 等终止符时视为结束；其余情况继续合并。
    """
    t = text.rstrip()
    if not t:
        return True
    ch = t[-1]
    if ch in _SENT_END:
        return True
    # 「％）」这类调查结果标记常单独成行，也应视为可结束
    if t.endswith("）") and re.search(r"[%％]\s*[）)]$", t):
        return True
    return False


def _starts_with_continuation(text: str) -> bool:
    """判断该行是否显然是上一行的续行。

    中文期刊 PDF 常见现象：句末标点被切到下一行行首，例如

        ...男性是术后吻合口漏的独立危险因素
        。男性患者吻合口漏发生率高于女性患者...

    第一行以「素」结尾（无标点）→ 本应与下一行合并，
    但第二行以「。」开头，容易被误判为新段落的开始。
    行首出现终止标点是**续行的强信号**，直接合并。
    """
    t = text.lstrip()
    return bool(t) and t[0] in "。；！？）"


def _is_heading_candidate(text: str) -> bool:
    """是否是编号标题（用于判断能否与其后的行合并）。"""
    t = text.strip()
    if len(t) > 90:
        return False
    return bool(_H1_RE.match(t) or _H2_RE.match(t) or _H2_NUM_RE.match(t))


def _merge_heading_continuations(lines: list[str]) -> list[str]:
    """把跨行的编号标题接回一行。

    中文期刊 PDF 里标题常跨行，例如：

        2. 美国麻醉师协会（American Society of Anesthesiologists，
        ASA）分级：ASA 分级与吻合口漏的发生密切相关。

    第一行以「，」结尾（未结束），第二行以「ASA）」开头（不是编号标题），
    两者应合成「2. 美国麻醉师协会（...，ASA）分级：...」才算完整标题。

    本函数在通用行合并**之前**执行，先把标题行的续行接回去；
    否则通用合并会把续行当作独立正文，标题与正文错位。
    """
    out: list[str] = []
    i = 0
    while i < len(lines):
        cur = lines[i]
        if _is_heading_candidate(cur) and not _ends_sentence(cur):
            # 只是一行编号开头的片段，向后并入知道出现终止标点或遇到新标题
            while i + 1 < len(lines) and not _ends_sentence(cur):
                nxt = lines[i + 1]
                if _is_heading_candidate(nxt) or _starts_with_continuation(nxt):
                    # 新标题：停止；续行：仍属本标题（如「ASA）分级：...」）
                    if _is_heading_candidate(nxt):
                        break
                cur = _join(cur, nxt.strip())
                i += 1
            out.append(cur)
        else:
            out.append(cur)
        i += 1
    return out


def merge_wrapped_lines(lines: list[str]) -> list[str]:
    """把 PDF 换行造成的断行合并回完整句子/标题。

    反复扫描直到无变化：一次合并可能让结果又满足合并条件
    （例如标题跨三行的情况）。
    """
    out = list(lines)
    changed = True
    guard = 0
    while changed and guard < 20:
        changed = False
        guard += 1
        merged: list[str] = []
        i = 0
        while i < len(out):
            cur = out[i]
            if not cur:
                i += 1
                continue
            # 判断是否与下一行合并
            if i + 1 < len(out) and out[i + 1]:
                nxt = out[i + 1]
                # 下一行以终止标点开头 → 这是上一行的续行，强制合并。
                # 必须先于「下一行是标题」的判断：续行可能以「）」等开头。
                if _starts_with_continuation(nxt):
                    merged.append(_join(cur, nxt.strip()))
                    changed = True
                    i += 2
                    continue
                # 下一行是新的编号标题 → 不合并（标题必须独立成行）
                nxt_is_head = _is_heading_candidate(nxt)
                # 当前行是**短**编号标题 → 保留为独立标题
                # 注意：中文期刊常见「1. 性别：男性是...危险因素。」这种
                # 「标题+首句同行」的排版。这类行以句号结尾，若按「已结束」
                # 处理，其后的正文会被切成新段落，形成「。男性患者...」这种
                # 以句号开头的畸形段落。因此标题一律不与其后内容合并，
                # 由后续的段落归组逻辑处理。
                cur_is_head = _is_heading_candidate(cur)
                # 当前行以终止标点结束 → 不合并（标题除外，见上）
                cur_done = _ends_sentence(cur) and not cur_is_head
                if not nxt_is_head and not cur_done and not cur_is_head:
                    merged.append(_join(cur, nxt.strip()))
                    changed = True
                    i += 2
                    continue
            merged.append(cur)
            i += 1
        out = merged
    return out


def _detect_headers(pages: list[list[str]], threshold: float = 0.5) -> set[str]:
    if len(pages) < 3:
        return set()
    counter: Counter[str] = Counter()
    for lines in pages:
        for ln in lines[:2] + lines[-2:]:
            key = re.sub(r"\d+", "#", _CITE_RE.sub("", ln).strip())
            if len(key) >= 6:
                counter[key] += 1
    limit = max(2, int(len(pages) * threshold))
    return {k for k, v in counter.items() if v >= limit}


@dataclass
class Section:
    number: str
    title: str
    level: int
    paragraphs: list[str] = field(default_factory=list)
    subsections: list[tuple[str, list[str]]] = field(default_factory=list)

    @property
    def chars(self) -> int:
        n = sum(len(p) for p in self.paragraphs)
        n += sum(len(t) + sum(len(x) for x in ps) for t, ps in self.subsections)
        return n


def extract_lines(pdf_path: str | Path) -> tuple[list[str], dict]:
    """抽取、清理并合并 PDF 正文行。"""
    path = Path(pdf_path)
    doc = pymupdf.open(path)
    raw_pages: list[list[str]] = []
    for page in doc:
        text = page.get_text() or ""
        raw_pages.append([ln for ln in text.split("\n") if ln.strip()])
    pages_count = doc.page_count
    doc.close()

    headers = _detect_headers(raw_pages)

    cleaned: list[str] = []
    for page_lines in raw_pages:
        for ln in page_lines:
            key = re.sub(r"\d+", "#", _CITE_RE.sub("", ln).strip())
            if key in headers:
                continue
            if len(ln.strip()) <= 2:
                continue
            if any(p.match(ln.strip()) for p in _SKIP_PATTERNS):
                continue
            c = _clean_line(ln)
            if c:
                cleaned.append(c)

    # 先接回跨行标题，再做通用行合并。顺序不能颠倒：
    # 通用合并会把标题的续行当成独立正文，导致标题与正文错位。
    headings_fixed = _merge_heading_continuations(cleaned)
    # 截断：一旦遇到「编审委员会」这类名单标题，其后内容全部丢弃。
    # 仅靠逐行模式匹配不够 —— 合并会把名单并成巨行（实测单行 1 万字符），
    # 巨行首部不匹配任何模式，于是整份名单混入正文。
    # 因此按出现位置直接截断，比逐行判断更可靠。
    for _idx, _ln in enumerate(headings_fixed):
        if re.match(r"^\s*(编审|编写|编辑|专家|顾问)委员会", _ln) or re.match(
            r"^\s*参\s*考\s*文\s*献", _ln
        ):
            headings_fixed = headings_fixed[:_idx]
            break

    merged = merge_wrapped_lines(headings_fixed)
    meta = {
        "file": path.name,
        "pages": pages_count,
        "headers_removed": len(headers),
        "lines_raw": len(cleaned),
        "lines_merged": len(merged),
    }
    return merged, meta


def build_sections(lines: list[str]) -> list[Section]:
    """按编号切分章节，并把正文行按标点聚成段落。"""
    sections: list[Section] = []
    cur: Section | None = None
    buf: list[str] = []
    sub_idx = -1

    def flush_para() -> None:
        nonlocal buf
        if not buf:
            return
        para = ""
        for piece in buf:
            para = _join(para, piece)
        buf = []
        para = para.strip()
        if not para or cur is None:
            return
        if 0 <= sub_idx < len(cur.subsections):
            cur.subsections[sub_idx][1].append(para)
        else:
            cur.paragraphs.append(para)

    def start_section(number: str, title: str) -> None:
        nonlocal cur, sub_idx
        flush_para()
        cur = Section(number=number, title=title, level=1)
        sub_idx = -1
        sections.append(cur)

    def start_subsection(number: str, title: str) -> None:
        nonlocal sub_idx
        flush_para()
        if cur is None:
            start_section("", "前言")
        cur.subsections.append((f"{number} {title}".strip(), []))
        sub_idx = len(cur.subsections) - 1

    for line in lines:
        m1 = _H1_RE.match(line)
        if m1 and (m1.group(2) or "").strip():
            start_section(m1.group(1), m1.group(2).strip())
            continue
        m2 = _H2_RE.match(line)
        if m2:
            start_subsection(m2.group(1), (m2.group(2) or "").strip())
            continue
        m2n = _H2_NUM_RE.match(line)
        if m2n:
            start_subsection(m2n.group(1) + ".", m2n.group(2).strip())
            continue
        buf.append(line)
        if _ends_sentence(line):
            flush_para()

    flush_para()
    return [s for s in sections if s.chars > 0]


def to_markdown_draft(pdf_path: str | Path, meta_extra: dict | None = None) -> tuple[str, dict]:
    lines, meta = extract_lines(pdf_path)
    sections = build_sections(lines)
    extra = meta_extra or {}

    out: list[str] = []
    out.append(f"# {extra.get('title') or Path(pdf_path).stem}")
    out.append("")
    out.append(f"**作者**：{extra.get('authors') or '（待填）'}")
    out.append(f"**期刊**：{extra.get('journal') or '（待填）'}")
    out.append(f"**年份**：{extra.get('year') or '（待填）'}")
    out.append(f"**证据等级**：{extra.get('evidence') or 'guideline'}")
    out.append(f"**分期**：{extra.get('phase') or '（待填 preop/intraop/postop/general）'}")
    out.append("")
    out.append("> ⚠️ 本文件由 PDF 自动抽取，是**草稿**，需人工核对后导入。")
    out.append("> 核对要点：**数值是否准确**、表格内容是否丢失、章节切分是否合理。")
    out.append("")

    for sec in sections:
        head = f"{sec.number}、" if sec.number else ""
        out.append(f"## {head}{sec.title or '前言'}")
        out.append("")
        for p in sec.paragraphs:
            out.append(p)
            out.append("")
        for sub_title, paras in sec.subsections:
            out.append(f"### {sub_title}")
            out.append("")
            for p in paras:
                out.append(p)
                out.append("")

    meta["sections"] = len(sections)
    meta["draft_chars"] = sum(s.chars for s in sections)
    return "\n".join(out), meta


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: python extract_guideline_pdf.py <pdf路径> [输出路径]")
        raise SystemExit(1)
    md, info = to_markdown_draft(sys.argv[1])
    if len(sys.argv) >= 3:
        Path(sys.argv[2]).write_text(md, encoding="utf-8")
        print("已写出: %s" % sys.argv[2])
    else:
        print(md[:4000])
    print()
    print("--- 统计 ---")
    for k, v in info.items():
        print("  %s: %s" % (k, v))
