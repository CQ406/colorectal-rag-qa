"""清理整理后的草稿：修断裂标题、拆超长段落、剔除表格乱文与页脚残留。

要解决的四类问题（均来自 PDF 抽取的固有缺陷）
----------------------------------------------
1. **标题与正文粘连**
   原 PDF 中「四、直肠癌术后吻合口漏的分级处理原则」与紧随的正文在同一行，
   抽取后成为 `## 四、...原则外科医生应该...`。需在标题末尾切回。

2. **编号小节丢失标题**
   抽取时若行尾无终止标点，会把后续内容并入同一行，
   导致 `### 1. 性别：...` 后面粘上了 `（专家赞成率68.18%）2.美国麻醉师协会...`。
   需识别其中的 `N.` 编号，重新断行并提升为小节标题。

3. **段落过长**
   单段可达 2000+ 字，检索命中时返回的片段信息被稀释。
   按句末标点切到 400~700 字，并保持语义完整（不切断句子）。

4. **表格与页脚乱文**
   PDF 表格会被抽成错乱文本（如「表1」内容与正文混排），
   页脚「·指南与共识·」「扫码阅读电子版」「页码」等也需剔除。
   表格内容**不尝试自动还原**，改为插入提示，由人工按原文补录 ——
   自动还原表格极易造成数值错位，风险高于收益。

用法
----
    python scripts/clean_draft.py <输入md> <输出md>
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# 页脚/水印/期刊页眉残留
_NOISE_RE = [
    re.compile(r"[·•]\s*指南与共识\s*[·•]"),
    re.compile(r"扫码阅读电子版"),
    re.compile(r"中华医学会外科学分会结直肠外科学组国际直肠癌研究组"),
    re.compile(r"^\s*\d{2,3}\s*$"),                 # 孤立页码
    re.compile(r"^\s*中华\S*杂志\s*\d{4}"),          # 期刊页眉
]

# 中文一级编号（一、二、）
_H1 = re.compile(r"^(#{2})\s*([一二三四五六七八九十]+)、(.+)$")
# 中文二级编号（（一）（二））
_H2 = re.compile(r"^(#{3})\s*[（(]([一二三四五六七八九十]+)[）)]\s*(.+)$")
# 阿拉伯编号小节：1. 2. ……（要求后接中文）
_NUM_SUB = re.compile(r"(\d{1,2})\s*[.．]\s*([\u4e00-\u9fff][^。；]{2,50}?[：:])")

_SENT_END = "。！？；"


def strip_noise(text: str) -> str:
    for pat in _NOISE_RE:
        text = pat.sub("", text)
    return text


def split_heading_and_body(line: str) -> list[str]:
    """把「## 标题正文...」切回标题与正文两行。

    判据：一级标题通常是「N、短标题」形式，长度在 25 字以内，
    其后若直接续正文，则在第 25 字附近寻找切点。
    这里改用更可靠的方式：在标题后第一个句末标点前，
    取到「原则」「处理」「预防」「因素」「分级」等标题性结尾词为止。
    """
    m = _H1.match(line)
    if not m:
        return [line]
    prefix, num, rest = m.group(1), m.group(2), m.group(3)
    # 标题性结尾词：出现后即为标题末尾
    tail_words = ["原则", "预防", "因素", "分级", "诊断", "处理", "定义", "标准",
                  "措施", "管理", "评估", "策略", "要点", "规范"]
    for w in tail_words:
        idx = rest.find(w)
        if 0 <= idx <= 30:
            cut = idx + len(w)
            title = rest[:cut]
            body = rest[cut:].strip()
            out = [f"{prefix} {num}、{title}"]
            if body:
                out.append("")
                out.append(body)
            return out
    return [line]


def break_inline_numbered(line: str) -> list[str]:
    """把正文中粘连的 `N.标题：` 断成独立的 ### 小节。

    只处理「行内出现 `数字.中文...：`」的情况，
    且要求该编号与前文之间有明显分隔（赞成率括号、句末标点），
    避免把「BMI 30」这类正常数字误判为编号。
    """
    # 在「（专家赞成率XX%）」或句末标点之后紧跟的数字编号才断行
    pattern = re.compile(
        r"(?<=[）)。；])\s*(\d{1,2})\s*[.．]\s*([\u4e00-\u9fff][^。]{2,60}?[：:])"
    )
    parts: list[str] = []
    last = 0
    for m in pattern.finditer(line):
        seg = line[last : m.start()].strip()
        if seg:
            parts.append(seg)
        parts.append("__SUB__" + m.group(1) + ". " + m.group(2))
        last = m.end()
    tail = line[last:].strip()
    if tail:
        parts.append(tail)
    return parts if parts else [line]


def split_long_paragraph(text: str, target: int = 600, hard_max: int = 900) -> list[str]:
    """按句末标点把超长段落切成多段，保持句子完整。"""
    if len(text) <= hard_max:
        return [text]
    # 先按句末标点切句
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
    # 若某段仍过长（单句超长），按硬上限强制切
    final: list[str] = []
    for p in out:
        while len(p) > hard_max * 2:
            final.append(p[: hard_max * 2])
            p = p[hard_max * 2 :]
        final.append(p)
    return final


def clean(src: Path, dst: Path) -> dict:
    raw = src.read_text(encoding="utf-8")
    lines = raw.split("\n")

    out: list[str] = []
    stats = {"heading_fixed": 0, "subs_restored": 0, "paras_split": 0, "noise_removed": 0}
    in_meta = True

    for line in lines:
        if not line.strip():
            out.append("")
            continue

        # 元数据与说明块保持原样
        if line.startswith("**") or line.startswith("> ") or line.startswith("# "):
            out.append(line)
            continue

        s = strip_noise(line)
        if s != line:
            stats["noise_removed"] += 1
        if not s.strip():
            continue

        # 标题正文粘连
        if s.startswith("## "):
            fixed = split_heading_and_body(s)
            if len(fixed) > 1:
                stats["heading_fixed"] += 1
            out.extend(fixed)
            out.append("")
            continue
        if s.startswith("### "):
            fixed = split_heading_and_body("##" + s[3:])
            if len(fixed) > 1:
                stats["heading_fixed"] += 1
                out.append("###" + fixed[0][2:].lstrip(" #"))
                for extra in fixed[1:]:
                    if extra.strip():
                        out.append(extra)
                out.append("")
            else:
                out.append(s)
                out.append("")
            continue

        # 行内粘连的编号小节
        parts = break_inline_numbered(s)
        if len(parts) > 1:
            stats["subs_restored"] += len([p for p in parts if p.startswith("__SUB__")])
            for p in parts:
                if p.startswith("__SUB__"):
                    out.append("### " + p[len("__SUB__"):])
                    out.append("")
                else:
                    for seg in split_long_paragraph(p):
                        out.append(seg)
                        out.append("")
                        if len(seg) != len(p):
                            stats["paras_split"] += 1
            continue

        # 普通段落 → 超长则拆
        segs = split_long_paragraph(s)
        if len(segs) > 1:
            stats["paras_split"] += 1
        for seg in segs:
            out.append(seg)
            out.append("")

    # 压缩多余空行
    text = "\n".join(out)
    text = re.sub(r"\n{3,}", "\n\n", text)
    # 表格乱文提示：原文「表1」内容无法可靠还原
    if "表1" in text and "分级" in text:
        text = text.replace(
            "具体见表1",
            "具体见表1（**表1 为吻合口漏分级表，PDF 表格无法可靠还原，"
            "请按原文补录**）",
        )
    dst.write_text(text, encoding="utf-8")
    return stats


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("用法: python clean_draft.py <输入md> <输出md>")
        raise SystemExit(1)
    st = clean(Path(sys.argv[1]), Path(sys.argv[2]))
    print("清理完成: %s" % st)
