"""探测 PDF 的可提取性：判断是文字版还是扫描版。

这决定后续处理方式：
  - 文字版：可直接提取正文，按章节整理
  - 扫描版：需 OCR，准确率不可靠，应请用户重新下载文字版

判据：每页平均可提取字符数。
  > 500 字/页  → 文字版
  100~500      → 混合（部分页可能是图）
  < 100        → 扫描版或图文混排
"""
import sys
from pathlib import Path

import pymupdf  # PyMuPDF

PDF_DIR = Path(r"D:\AI\毕设\PDF")


def probe(path: Path) -> dict:
    try:
        doc = pymupdf.open(path)
    except Exception as exc:  # noqa: BLE001
        return {"file": path.name, "error": f"{type(exc).__name__}: {exc}"}

    pages = doc.page_count
    total_chars = 0
    # 只抽前 5 页估算（大文件全量抽取慢）
    sample = min(pages, 5)
    for i in range(sample):
        try:
            total_chars += len(doc[i].get_text() or "")
        except Exception:  # noqa: BLE001
            pass
    doc.close()

    per_page = total_chars / sample if sample else 0
    if per_page >= 500:
        kind = "文字版"
    elif per_page >= 100:
        kind = "图文混排"
    else:
        kind = "扫描版"
    return {
        "file": path.name, "pages": pages, "sample": sample,
        "chars": total_chars, "per_page": per_page, "kind": kind,
    }


files = sorted(PDF_DIR.glob("*.pdf"))
print("=" * 96)
print("PDF 可提取性探测（前 5 页取样）")
print("=" * 96)
print("%-58s %5s %9s  %s" % ("文件", "页数", "字/页", "类型"))
print("-" * 96)

results = []
for f in files:
    r = probe(f)
    results.append(r)
    if "error" in r:
        print("%-58s  %s" % (r["file"][:56], r["error"][:30]))
    else:
        print("%-58s %5d %9.0f  %s" % (
            r["file"][:56], r["pages"], r["per_page"], r["kind"]))

print()
from collections import Counter
c = Counter(r.get("kind", "错误") for r in results)
print("汇总: %s" % dict(c))
