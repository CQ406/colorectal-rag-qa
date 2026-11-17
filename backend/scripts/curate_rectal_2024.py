"""整理《中国恶性肿瘤整合诊治指南-直肠癌（2024版）》为录入文件。

抽取质量评级「好」，但有两个需处理的问题：
  1. 段落偏长（最长 1560 字）—— 检索命中时返回的片段信息被稀释
  2. 章节「四、治疗」达 18891 字 —— 单章过大，其内部子节需保留为独立结构

处理：保留原文章节与子节结构；按句末标点把超长段落切到 400~700 字。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

BACKEND = Path(r"D:\AI\RAG_project\backend")
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(BACKEND / "scripts"))

from clean_draft import split_long_paragraph  # noqa: E402
from extract_guideline_pdf import extract_lines, build_sections  # noqa: E402

PDF = Path(r"D:\AI\毕设\PDF\中国恶性肿瘤整合诊治指南-直肠癌（2024版）.pdf")
OUT = Path(r"D:\AI\RAG_project\data\manual\中国恶性肿瘤整合诊治指南-直肠癌2024版.md")

META = [
    ("作者", "中国抗癌协会"),
    ("期刊", "中国肿瘤临床"),
    ("年份", "2024"),
    ("证据等级", "guideline"),
    ("分期", "general"),
    ("版本", "2024版"),
]

# 页脚/水印残留
_NOISE = [
    re.compile(r"·\s*指南与共识\s*·"),
    re.compile(r"^\s*\d{2,3}\s*$"),
    re.compile(r"中国肿瘤临床\s*\d{4}"),
    re.compile(r"^\s*引用本文"),
]


def strip_noise(s: str) -> str:
    for p in _NOISE:
        s = p.sub("", s)
    return s.strip()


def main() -> int:
    lines, meta = extract_lines(PDF)
    sections = build_sections(lines)
    print("抽取: %d 页 | %d 行 | %d 章节" % (
        meta["pages"], meta["lines_merged"], len(sections)))

    out: list[str] = []
    title = "中国恶性肿瘤整合诊治指南-直肠癌（2024版）"
    out.append(f"# {title}")
    out.append("")
    for k, v in META:
        out.append(f"**{k}**：{v}")
    out.append("")
    out.append("> 本文件由该指南全文整理而成，保留原文章节与子节结构。")
    out.append("> **使用前请核对原文数值**，具体临床决策以最新版指南与患者个体情况为准。")
    out.append("")

    total = 0
    stats: list[tuple[str, int, int]] = []

    def emit(paras: list[str]) -> int:
        n = 0
        for p in paras:
            p = strip_noise(p)
            if len(p) < 8:
                continue
            for seg in split_long_paragraph(p, target=600, hard_max=900):
                out.append(seg)
                out.append("")
                n += len(seg)
        return n

    for sec in sections:
        head = f"{sec.number}、" if sec.number else ""
        name = sec.title or "前言"
        out.append(f"## {head}{name}")
        out.append("")
        c = emit(sec.paragraphs)
        for sub_name, sub_paras in sec.subsections:
            if sub_name.strip():
                out.append(f"### {sub_name}")
                out.append("")
            c += emit(sub_paras)
        total += c
        stats.append((f"{head}{name}", c, len(sec.subsections)))

    text = "\n".join(out)
    text = re.sub(r"\n{3,}", "\n\n", text)
    OUT.write_text(text, encoding="utf-8")

    han = len(re.findall(r"[\u4e00-\u9fff]", text))
    paras = [x for x in text.split("\n")
             if x.strip() and not x.startswith(("#", "*", ">"))]
    print()
    print("=== 章节 ===")
    for name, c, nsub in stats:
        print("  %-32s %6d 字  子节 %d" % (name[:32], c, nsub))
    print()
    print("已写出: %s" % OUT)
    print("  汉字 %d | 段落 %d | 最长段 %d | 字节 %d" % (
        han, len(paras), max((len(p) for p in paras), default=0),
        OUT.stat().st_size))
    src_chars = sum(s.chars for s in sections)
    print("  保留率 %.1f%% (%d -> %d)" % (
        total / src_chars * 100 if src_chars else 0, src_chars, total))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
