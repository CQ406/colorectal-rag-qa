"""用「最大字号」法从英文学术 PDF 中提取真正题名。

为什么需要专门处理
------------------
初版按「首页前几行中最长的一行」猜题名，结果抓到的是作者单位、
期刊页眉、甚至审稿人信息（如「1Department of Artificial Intelligence…」）。
这类字符串做文件名会把引用与检索全部污染。

学术论文排版有稳定规律：**题名使用全页最大字号**。
因此用 `page.get_text("dict")` 取每个文本 span 的字号与文本，
把字号接近最大值、且位于页面上半部的 span 按顺序拼接，即得题名。
这一步比按行长度猜测可靠得多。
"""
from __future__ import annotations

import re
from pathlib import Path

import pymupdf

MAX_TITLE_CHARS = 220


def extract_title(pdf: Path) -> tuple[str, str]:
    """返回 (题名, 提取方式)。"""
    doc = pymupdf.open(pdf)
    try:
        # 1) 先看 PDF 元数据里的 Title（部分出版社写入的是正确题名）
        meta_title = (doc.metadata or {}).get("title") or ""
        meta_title = meta_title.strip()

        if not doc.page_count:
            return (meta_title or pdf.stem), "meta"

        page = doc[0]
        page_h = page.rect.height
        d = page.get_text("dict")

        spans: list[tuple[float, float, float, str]] = []
        for block in d.get("blocks", []):
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    text = (span.get("text") or "").strip()
                    if not text:
                        continue
                    spans.append((span.get("size", 0), span.get("bbox", [0, 0, 0, 0])[1],
                                  span.get("bbox", [0, 0, 0, 0])[0], text))
        if not spans:
            return (meta_title or pdf.stem), "meta"

        max_size = max(s[0] for s in spans)
        # 题名行：字号 ≥ 最大字号的 88%，且位于页面上 45%
        cand = [s for s in spans if s[0] >= max_size * 0.88 and s[1] < page_h * 0.45]
        if not cand:
            cand = [s for s in spans if s[0] >= max_size * 0.88]

        # 按版面顺序（y, x）拼接，去掉重复片段
        cand.sort(key=lambda s: (round(s[1], 1), s[2]))
        parts: list[str] = []
        for _, _, _, t in cand:
            if not parts or t not in parts[-1]:
                parts.append(t)
        title = " ".join(parts)
        title = re.sub(r"\s+", " ", title).strip()

        # 2) 元数据题名更「干净」且长度合理时优先用它
        if meta_title and 20 <= len(meta_title) <= MAX_TITLE_CHARS:
            # 元数据题名与提取题名相似则用元数据
            a = re.sub(r"[^a-z0-9]", "", meta_title.lower())
            b = re.sub(r"[^a-z0-9]", "", title.lower())
            if a and b and (a in b or b in a or len(a) > 30):
                return meta_title, "meta"

        if title and len(title) >= 15:
            return title[:MAX_TITLE_CHARS], "font"

        return (meta_title or pdf.stem), "fallback"
    finally:
        doc.close()


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1:
        t, how = extract_title(Path(sys.argv[1]))
        print("[%s] %s" % (how, t))
    else:
        EN = Path(r"D:\AI\毕设\外网")
        for p in sorted(EN.glob("*.pdf")):
            t, how = extract_title(p)
            print("%-34s [%s] %s" % (p.stem[:34], how, t[:100]))
