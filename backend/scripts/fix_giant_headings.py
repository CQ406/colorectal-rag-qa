"""精确切分大章节标题的粘连正文。

背景
----
《中国结直肠癌诊疗规范（2025版）》抽取后有 104 个标题，其中：
  - 74 个是纯标题（无句末标点）→ 无需处理
  - 20 个是原文的列表项（如 `### 2. 生化检查：肝肾功能、电解质等。`）
    → 这些「标题+一句正文同行」是**原文排版本身**，拆开反而破坏列表结构，保留
  - 4 个大章节标题粘连了整段正文 → **需要切割**

本脚本只处理第 3 类：以 `N、` 或 `N ` 中文编号开头、
其后紧跟一段完整正文的章节标题。

判据
----
大章节标题的正文部分以「首句 + 句末标点」形式出现，且首个句末标点
位置在 40 字之后；而列表项的句末标点通常在前 30 字内且本身就是完整条目。

结合原文可知，真正需要切割的是：
  ## 一、前言...
  ## 二、概述...
  ### 一 临床表现...
  ### 一 分型...
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

_SENT_END = "。！？；"

# 需要切割的章节前缀（按原文实际标题确定）
TARGET_PREFIXES = [
    "一、前言",
    "二、概述",
    "一 临床表现",
    "一 分型",
]


def split_target(line: str) -> list[str] | None:
    """若该行是需切割的章节，返回 [标题行, "", 正文]；否则返回 None。"""
    m = re.match(r"^(#{2,3})\s*(.+)$", line)
    if not m:
        return None
    hashes, rest = m.group(1), m.group(2).strip()

    for prefix in TARGET_PREFIXES:
        if not rest.startswith(prefix):
            continue
        # 标题部分：前缀 + 紧跟的短语（到第一个句末标点前的最后一个标题性词）
        body = rest[len(prefix):]
        # 标题剩余部分通常是 2~8 字的名词短语（如「中国结直肠癌发病率...」不是标题）
        # 这里按前缀本身即为完整标题处理，其余全部视为正文
        return [f"{hashes} {prefix}", "", body]
    return None


def fix(text: str) -> tuple[str, int]:
    out: list[str] = []
    n = 0
    for ln in text.split("\n"):
        if ln.startswith("## ") or ln.startswith("### "):
            parts = split_target(ln)
            if parts:
                out.extend(parts)
                n += 1
                continue
        out.append(ln)
    text = "\n".join(out)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text, n


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("用法: python fix_giant_headings.py <输入md> <输出md>")
        raise SystemExit(1)
    src, dst = Path(sys.argv[1]), Path(sys.argv[2])
    text, n = fix(src.read_text(encoding="utf-8-sig"))
    dst.write_text(text, encoding="utf-8")
    heads = re.findall(r"^#{2,3} .+$", text, re.M)
    han = len(re.findall(r"[\u4e00-\u9fff]", text))
    # 标题长度分布，用于确认已无巨长标题
    lens = [len(re.sub(r"^#{2,3}\s+", "", h)) for h in heads]
    print("切割大标题 %d 处 | 标题 %d 个 | 汉字 %d" % (n, len(heads), han))
    print("  标题长度: 最长 %d | >40 字的 %d 个" % (
        max(lens) if lens else 0, sum(1 for x in lens if x > 40)))
