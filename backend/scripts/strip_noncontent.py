"""剔除录入文件中的非正文内容：编委会名单、图片乱码。

实测两类污染
------------
1. **编委会名单**（1560 字）
   `《中国恶性肿瘤整合诊治指南-直肠癌（2024版）》编委会名单名誉主编：樊代明...`
   含数十位专家姓名与单位，对问答无任何价值，且因体量大、
   含大量机构名，检索时会被高频词意外命中。

2. **图片乱码**（636 字）
   流程图在 PDF 中由矢量文字组成，抽取后成为
   `ဩ෴శ ᄯᐥ჻಩෴శ உӐႤ cCR ...` 这类无意义字符序列
   （缅文/藏文/高棉文码点，实为字体的私有编码）。
   这类内容必须删除 —— 留在语料中会污染向量并产生无意义的检索命中。

判据
----
不靠固定关键词（不同文献的名单标题写法各异），而用**特征**判断：
  - 名单：出现「主编」「副主编」「编委」「名誉主编」等词且长度 > 400 字
  - 乱码：非中日韩、非拉丁、非标点的罕见 Unicode 字符占比 > 15%
"""
from __future__ import annotations

import re
import sys
import unicodedata
from pathlib import Path

# 小语种码点区间（PDF 抽取图片文字时的典型乱码来源）
_RARE_RANGES = [
    (0x1000, 0x109F),   # 缅甸文
    (0x0F00, 0x0FFF),   # 藏文
    (0x1780, 0x17FF),   # 高棉文
    (0x0E80, 0x0EFF),   # 老挝文
    (0x0900, 0x097F),   # 天城文
    (0x0400, 0x04FF),   # 西里尔文（偶尔来自俄文参考文献，此处保守处理）
]

_COMMITTEE_WORDS = ["名誉主编", "主编", "副主编", "编委会", "编委名单",
                    "编写组", "执笔", "通讯作者", "通信作者"]


def _rare_ratio(text: str) -> float:
    """罕见码点占比。"""
    if not text:
        return 0.0
    rare = 0
    total = 0
    for ch in text:
        if ch.isspace():
            continue
        total += 1
        cp = ord(ch)
        for lo, hi in _RARE_RANGES:
            if lo <= cp <= hi:
                rare += 1
                break
    return rare / total if total else 0.0


def _looks_like_committee(text: str) -> bool:
    """判断是否为编委会/作者名单。"""
    if len(text) < 400:
        return False
    hits = sum(1 for w in _COMMITTEE_WORDS if w in text)
    return hits >= 2


def _cjk_ratio(text: str) -> float:
    if not text:
        return 0.0
    n = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    return n / len(text)


def clean_text(text: str) -> tuple[str, dict]:
    lines = text.split("\n")
    out: list[str] = []
    stats = {"committee_removed": 0, "garbled_removed": 0, "chars_removed": 0}

    for ln in lines:
        s = ln.strip()
        # 保留标题行与元数据
        if s.startswith("#") or s.startswith("**") or s.startswith(">"):
            out.append(ln)
            continue
        if not s:
            out.append(ln)
            continue

        if _looks_like_committee(s):
            stats["committee_removed"] += 1
            stats["chars_removed"] += len(s)
            continue

        if _rare_ratio(s) > 0.15:
            stats["garbled_removed"] += 1
            stats["chars_removed"] += len(s)
            continue

        out.append(ln)

    # 清理因删除而产生的空行
    result = re.sub(r"\n{3,}", "\n\n", "\n".join(out))
    return result, stats


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("用法: python strip_noncontent.py <输入md> <输出md>")
        raise SystemExit(1)
    src, dst = Path(sys.argv[1]), Path(sys.argv[2])
    text = src.read_text(encoding="utf-8-sig")
    cleaned, st = clean_text(text)
    dst.write_text(cleaned, encoding="utf-8")
    han = len(re.findall(r"[\u4e00-\u9fff]", cleaned))
    print("剔除编委会 %d 段、乱码 %d 段，共 %d 字符" % (
        st["committee_removed"], st["garbled_removed"], st["chars_removed"]))
    print("  剩余汉字 %d | 字节 %d" % (han, len(cleaned.encode("utf-8"))))
