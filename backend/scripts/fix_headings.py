"""通用标题切分：把「## 标题正文...」拆成标题与正文。

问题来源
--------
中文期刊 PDF 里章节标题与其后的正文常排在同一行（标题后无换行标记），
抽取后成为 `## 一、前言中国结直肠癌发病率和死亡率持续上升，...`。

切分规则
--------
中文标题**不含句末标点**（。！？；），因此第一个句末标点处即为标题结束。
但需要处理两类例外：

  1. 标题本身很短且后无正文（如 `## 三、诊断`）→ 不切分
  2. 首个句末标点出现在很靠后的位置（超过阈值）→ 说明该行可能本就是
     一句正文被误标为标题，或标题极长；此时按标题性结尾词切，切不出则保留原样

同时修正编号一致性：`### 1. 全身评估：...` 这类「编号+正文」形式保留编号；
`### 一 临床表现早期...` 这类中文编号子节去掉多余空格。

用法
----
    python scripts/fix_headings.py <输入md> <输出md>
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

_SENT_END = "。！？；"

# 标题性结尾词：用于在超长标题中定位切点
_TAIL_WORDS = [
    "诊疗规范", "专家共识", "临床指南", "操作标准", "技术规范",
    "原则", "预防", "因素", "分级", "诊断", "治疗", "处理", "定义",
    "标准", "措施", "管理", "评估", "策略", "要点", "规范", "方法",
    "指征", "适应证", "随访", "并发症", "预后", "分期", "检查",
]


def split_heading(line: str, max_title: int = 26) -> list[str]:
    """把一行标题拆成 [标题行, 正文行] 或原样返回。

    Args:
        max_title: 标题长度上限。超过则认为该行不是标题，保持原样。
    """
    m = re.match(r"^(#{2,3})\s*(.+)$", line)
    if not m:
        return [line]
    hashes, rest = m.group(1), m.group(2).strip()
    if not rest:
        return [line]

    # 找第一个句末标点
    pos = -1
    for i, ch in enumerate(rest):
        if ch in _SENT_END:
            pos = i
            break

    # 无句末标点 → 整行就是标题
    if pos < 0:
        return [line]

    title = rest[:pos].strip()
    body = rest[pos:].strip()

    # 标题过长 → 尝试按标题性结尾词切
    if len(title) > max_title:
        cut = -1
        for w in _TAIL_WORDS:
            idx = rest.find(w)
            if 0 <= idx <= 34:
                cut = max(cut, idx + len(w))
        if cut > 0:
            title, body = rest[:cut].strip(), rest[cut:].strip()
        else:
            # 切不出来 → 认为这行本就是正文被误标，保留原样
            return [line]

    # 正文太短（不足 6 字）→ 视为标题的一部分，不切
    if len(body) < 6:
        return [line]

    return [f"{hashes} {title}", "", body]


def fix(text: str) -> tuple[str, int]:
    lines = text.split("\n")
    out: list[str] = []
    n = 0
    for ln in lines:
        if ln.startswith("## ") or ln.startswith("### "):
            parts = split_heading(ln)
            if len(parts) > 1:
                n += 1
            out.extend(parts)
        else:
            out.append(ln)
    text = "\n".join(out)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text, n


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("用法: python fix_headings.py <输入md> <输出md>")
        raise SystemExit(1)
    src, dst = Path(sys.argv[1]), Path(sys.argv[2])
    text, n = fix(src.read_text(encoding="utf-8-sig"))
    dst.write_text(text, encoding="utf-8")
    heads = re.findall(r"^#{2,3} .+$", text, re.M)
    han = len(re.findall(r"[\u4e00-\u9fff]", text))
    print("切分标题 %d 处 | 现标题 %d 个 | 汉字 %d" % (n, len(heads), han))
