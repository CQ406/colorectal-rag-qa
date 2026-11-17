"""批量抽取 PDF 指南，生成草稿并汇总质量指标。

用途：一次性看清哪些 PDF 能顺利抽取、哪些格式特殊需单独处理，
避免逐份手工试探。
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, r"D:\AI\RAG_project\backend\scripts")

from extract_guideline_pdf import to_markdown_draft  # noqa: E402

PDF_DIR = Path(r"D:\AI\毕设\PDF")
OUT_DIR = Path(r"D:\AI\RAG_project\data\manual_draft")
OUT_DIR.mkdir(parents=True, exist_ok=True)

rows = []
files = sorted(PDF_DIR.glob("*.pdf"))
print("共 %d 个 PDF" % len(files))
print()

for i, pdf in enumerate(files, 1):
    try:
        md, info = to_markdown_draft(pdf)
        out = OUT_DIR / (pdf.stem + ".草稿.md")
        out.write_text(md, encoding="utf-8")

        han = len(re.findall(r"[\u4e00-\u9fff]", md))
        lo = len(re.findall(r"[a-z]", md))
        total = len(md)
        ratio = han / total * 100 if total else 0
        max_line = max((len(x) for x in md.split("\n")), default=0)
        # 质量判定：汉字占比高、无异常巨行、章节数合理
        if ratio >= 60 and max_line < 3000 and info.get("sections", 0) >= 3:
            grade = "好"
        elif ratio >= 40 and max_line < 8000:
            grade = "可"
        else:
            grade = "差"
        rows.append({
            "file": pdf.stem, "pages": info["pages"],
            "sections": info.get("sections", 0), "han": han,
            "ratio": ratio, "max_line": max_line, "grade": grade,
            "chars": info.get("draft_chars", 0),
        })
    except Exception as exc:  # noqa: BLE001
        rows.append({"file": pdf.stem, "error": f"{type(exc).__name__}: {exc}"})

print("%-52s %4s %4s %7s %6s %7s %s" % (
    "文件", "页", "章", "汉字", "占比", "最长行", "评级"))
print("-" * 96)
for r in rows:
    if "error" in r:
        print("%-52s 错误: %s" % (r["file"][:50], r["error"][:36]))
    else:
        print("%-52s %4d %4d %7d %5.1f%% %7d %s" % (
            r["file"][:50], r["pages"], r["sections"], r["han"],
            r["ratio"], r["max_line"], r["grade"]))

print()
from collections import Counter
c = Counter(r.get("grade", "错误") for r in rows)
print("汇总: %s" % dict(c))
total_han = sum(r.get("han", 0) for r in rows)
print("全部草稿汉字合计: %d" % total_han)
print("草稿目录: %s" % OUT_DIR)
