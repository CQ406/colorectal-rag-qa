"""整理《中国直肠癌手术吻合口漏诊断、预防及处理专家共识（2019版）》为录入文件。

设计原则：**不丢弃任何正文内容**

初版实现按主题关键词分桶，结果 69 段中有 35 段因不含主题词被丢弃
（占一半），其中包括「研究显示，肿瘤直径≥5 cm，吻合口漏的发生率增加 4 倍。」
这类关键证据句。这是不可接受的 —— 整理的作用是改善可读性，
不是做内容筛选。

现改为：
  1. **保留原文顺序与结构**，只做两类修正：
     - 把孤立的「（专家赞成率XX%）」并回上一条推荐意见
       （原文里它紧跟推荐意见，PDF 抽取时被拆成独立段）
     - 把同一子主题下的碎片句合并成完整段落
  2. 章节沿用原文的编号结构（一、／（一）／1.），不重新分桶
  3. 修正跨行造成的错误（如标题与正文粘连）

输出到 data/manual/，文件名不带下划线前缀（会被 manual 命令导入）。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, r"D:\AI\RAG_project\backend\scripts")

from extract_guideline_pdf import extract_lines, build_sections  # noqa: E402

PDF = Path(r"D:\AI\毕设\PDF\中国直肠癌手术吻合口漏诊断、预防及处理专家共识（2019版）.pdf")
OUT = Path(r"D:\AI\RAG_project\data\manual\中国直肠癌手术吻合口漏诊断、预防及处理专家共识2019版.md")

# 孤立的专家赞成率（PDF 抽取时被拆出来）
_RATE_RE = re.compile(r"^[（(]\s*专家赞成率\s*[\d.]+\s*%\s*[）)]$")
# 句末标点
_SENT_END = "。！？；"


def merge_rate_fragments(paras: list[str]) -> list[str]:
    """把孤立的「（专家赞成率XX%）」并回上一条推荐意见。

    原文排布：推荐意见一句，随后另起一行标注专家赞成率。
    PDF 抽取后两者成为独立段落，若不合并，检索时赞成率会与推荐意见分离，
    用户看到赞成率却不知道它对应哪条建议。
    """
    out: list[str] = []
    for p in paras:
        if _RATE_RE.match(p.strip()) and out:
            out[-1] = out[-1].rstrip() + p.strip()
        else:
            out.append(p)
    return out


def merge_short_paras(paras: list[str], target: int = 420) -> list[str]:
    """合并过短的段落。

    PDF 抽取产生的段落常只有一两句（40 字以下），单独成段会让
    检索结果碎片化 —— 命中后返回的片段信息量不足。
    按原文顺序合并到目标长度，不改变文字内容与先后次序。
    """
    merged: list[str] = []
    buf = ""
    for p in paras:
        if not buf:
            buf = p
            continue
        # 上一条以句末标点结束 且 已达目标长度 → 断开
        if buf and buf[-1] in _SENT_END and len(buf) >= target:
            merged.append(buf)
            buf = p
        else:
            buf = buf + p
    if buf:
        merged.append(buf)
    return merged


def main() -> int:
    lines, meta = extract_lines(PDF)
    sections = build_sections(lines)
    print("抽取: %d 页 | %d 行 | %d 章节" % (
        meta["pages"], meta["lines_merged"], len(sections)))

    out: list[str] = []
    out.append("# 中国直肠癌手术吻合口漏诊断、预防及处理专家共识（2019版）")
    out.append("")
    out.append("**作者**：中华医学会外科学分会结直肠外科学组、国际直肠癌研究组")
    out.append("**期刊**：中华胃肠外科杂志")
    out.append("**年份**：2019")
    out.append("**证据等级**：guideline")
    out.append("**分期**：postop")
    out.append("**版本**：2019版")
    out.append("")
    out.append("> 本文件由该共识全文整理而成，保留原文结构与「专家赞成率」标注")
    out.append("> （赞成率反映推荐强度，是共识类文献的核心信息）。")
    out.append("> **使用前请核对原文数值**，具体临床决策以最新版指南与患者个体情况为准。")
    out.append("")

    total_chars = 0
    stats: list[tuple[str, int, int]] = []

    for sec in sections:
        # 章节标题：一级用 ##，原文一级编号（一、二、）
        head = f"{sec.number}、" if sec.number else ""
        title = sec.title or "前言"
        out.append(f"## {head}{title}")
        out.append("")

        def emit(paras: list[str], level: int = 3, name: str = "") -> None:
            nonlocal total_chars
            fixed = merge_rate_fragments(paras)
            merged = merge_short_paras(fixed)
            if name:
                out.append(f"{'#' * level} {name}")
                out.append("")
            for m in merged:
                out.append(m)
                out.append("")
                total_chars += len(m)

        emit(sec.paragraphs)
        for sub_name, sub_paras in sec.subsections:
            emit(sub_paras, level=3, name=sub_name)

        chars = sec.chars
        stats.append((f"{head}{title}", chars, len(sec.subsections)))

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(out), encoding="utf-8")

    han = len(re.findall(r"[\u4e00-\u9fff]", "\n".join(out)))
    print()
    print("=== 章节结构 ===")
    for name, chars, nsub in stats:
        print("  %-40s %5d 字  子节 %d" % (name[:40], chars, nsub))
    print()
    print("已写出: %s" % OUT)
    print("  章节 %d | 汉字 %d | 正文字数 %d" % (len(stats), han, total_chars))

    # 完整性检查：抽取的字数与写入的对比
    src_chars = sum(s.chars for s in sections)
    print("  抽取字数 %d -> 写入 %d （保留率 %.1f%%）" % (
        src_chars, total_chars,
        total_chars / src_chars * 100 if src_chars else 0))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
