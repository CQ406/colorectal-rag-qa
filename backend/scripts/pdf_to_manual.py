"""PDF → 知识库录入文件的完整流程（版面块版）。

取代早期「按标点合并」的流程。原因见 extract_by_blocks 的模块文档：
按标点判断行结束在中文期刊排版下不可靠，且循环合并无边界约束，
一旦判定失误会把整篇文档并成数行（实测 1080 行 → 2 行）。

流程
----
    PDF
     → 版面块抽取（extract_by_blocks）  ← 用 PDF 自身的版面分析，不猜标点
     → 剔除非正文（strip_noncontent）    ← 编委会名单、图片乱码、页脚
     → 段落长度规整                      ← 切到 400~800 字，保持句子完整
     → 识别章节标题（编号模式）
     → 生成 data/manual/*.md

与旧流程的关键差异
------------------
1. **不再做「行合并直到稳定」** —— 块边界即段落边界，这是硬约束
2. **不再做「超长段落截断」** —— 早期加的 4000 字截断在合并失控时
   直接删内容，是灾难的放大器；现改为按句末标点**拆分**（不丢内容）
3. 标题识别从编号模式推断，但因版面块已把标题独立成块，
   粘连问题大幅减少

标题识别
--------
中文指南的章节用「一、二、」「（一）（二）」「1. 2.」编号。
版面块抽取后，标题通常独立成块（短、不含句末标点），因此据
「块文本不含句末标点且长度 < 40」判定为标题。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(r"D:\AI\RAG_project\backend")
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

from extract_by_blocks import to_paragraphs  # noqa: E402
from strip_noncontent import clean_text  # noqa: E402

# 中文编号标题
_H1 = re.compile(r"^([一二三四五六七八九十]+)\s*[、.．]\s*(.+)?$")
_H2 = re.compile(r"^[（(]\s*([一二三四五六七八九十]+)\s*[）)]\s*(.+)?$")
_H3 = re.compile(r"^(\d{1,2})\s*[.．]\s*([\u4e00-\u9fff][^。！？；]{1,24})$")

_SENT_END = "。！？；"

# 章节标题长度上限（超过则不认为是标题）
_TITLE_MAX = 42


def _is_title(text: str) -> tuple[bool, str]:
    """判断段落是否为章节标题，返回 (是否, 编号前缀)。"""
    t = text.strip()
    if len(t) > _TITLE_MAX:
        return False, ""
    # 标题通常不含句末标点
    if t and t[-1] in _SENT_END:
        return False, ""
    # 中文编号标题：编号后的标题文字需短（长的是正文被误当标题）
    m = _H1.match(t)
    if m:
        tp = (m.group(2) or "").strip()
        if 1 <= len(tp) <= 24:
            return True, f"{m.group(1)}、"
    m = _H2.match(t)
    if m:
        tp = (m.group(2) or "").strip()
        if 1 <= len(tp) <= 24:
            return True, f"（{m.group(1)}）"
    m = _H3.match(t)
    if m:
        return True, f"{m.group(1)}. "
    return False, ""


def _split_long(text: str, target: int = 700, hard_max: int = 950) -> list[str]:
    """按句末标点把超长段落切成多段（**不丢内容**）。"""
    if len(text) <= hard_max:
        return [text]
    sentences = re.split(r"(?<=[。！？；])", text)
    out: list[str] = []
    buf = ""
    for s in sentences:
        if not s:
            continue
        if buf and len(buf) + len(s) > target:
            out.append(buf)
            buf = s
        else:
            buf += s
    if buf:
        out.append(buf)
    # 单句仍超长时按硬上限切（极少见，且不丢弃字符）
    final: list[str] = []
    for p in out:
        while len(p) > hard_max * 2:
            final.append(p[: hard_max * 2])
            p = p[hard_max * 2 :]
        final.append(p)
    return final


def build(pdf_path: str | Path, meta: dict) -> tuple[str, dict]:
    """生成录入用 Markdown。

    Args:
        meta: 至少包含 title；可选 authors/journal/year/evidence/phase
    """
    paras, emeta = to_paragraphs(pdf_path)

    # 1) 剔除编委会名单、图片乱码等非正文
    joined = "\n".join(paras)
    joined, noise = clean_text(joined)
    paras = [x.strip() for x in joined.split("\n") if x.strip()]

    # 2) 组织章节
    out: list[str] = []
    out.append(f"# {meta.get('title') or Path(pdf_path).stem}")
    out.append("")
    for k, label in (("authors", "作者"), ("journal", "期刊"), ("year", "年份"),
                     ("evidence", "证据等级"), ("phase", "分期"),
                     ("version", "版本")):
        if meta.get(k):
            out.append(f"**{label}**：{meta[k]}")
    out.append("")
    out.append("> 本文件由 PDF 全文整理而成，保留原文结构与内容。")
    out.append("> **使用前请核对原文数值**，具体临床决策以最新版指南与患者个体情况为准。")
    out.append("")

    stats = {"sections": 0, "paras": 0, "chars": 0, "titles": 0}
    pending_title: str | None = None

    for p in paras:
        ok, prefix = _is_title(p)
        if ok:
            if pending_title:
                out.append(f"## {pending_title}")
                out.append("")
                stats["sections"] += 1
            # 封面页信息不作为章节：
            #   页眉特征 —— 同时含「杂志/期刊/学报」与「卷/期/Vol/No」
            #   封面特征 —— 含 Email、通信作者、编辑委员会
            if re.search(r"Email|通信作者|编辑委员会", p):
                pending_title = None
                continue
            if re.search(r"(杂志|期刊|学报)", p) and re.search(r"卷|期|Vol|No", p):
                pending_title = None
                continue
            pending_title = p.strip()
            stats["titles"] += 1
            continue

        body = p
        for seg in _split_long(body):
            out.append(seg)
            out.append("")
            stats["paras"] += 1
            stats["chars"] += len(seg)

    if pending_title:
        out.append(f"## {pending_title}")
        out.append("")
        stats["sections"] += 1

    text = re.sub(r"\n{3,}", "\n\n", "\n".join(out))
    stats["han"] = len(re.findall(r"[\u4e00-\u9fff]", text))
    stats["noise_removed_chars"] = noise.get("chars_removed", 0)
    stats.update({f"src_{k}": v for k, v in emeta.items()})
    return text, stats


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="PDF → 录入 Markdown（版面块版）")
    ap.add_argument("pdf")
    ap.add_argument("out")
    ap.add_argument("--title", default=None)
    ap.add_argument("--authors", default=None)
    ap.add_argument("--journal", default=None)
    ap.add_argument("--year", default=None)
    ap.add_argument("--evidence", default="guideline")
    ap.add_argument("--phase", default="general")
    a = ap.parse_args()

    md, st = build(a.pdf, {
        "title": a.title, "authors": a.authors, "journal": a.journal,
        "year": a.year, "evidence": a.evidence, "phase": a.phase,
    })
    Path(a.out).write_text(md, encoding="utf-8")
    print("已写出: %s" % a.out)
    for k in ("sections", "titles", "paras", "chars", "han",
              "noise_removed_chars", "src_blocks", "src_pages"):
        if k in st:
            print("  %-18s %s" % (k, st[k]))
