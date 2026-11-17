"""整理《国家卫生健康委员会中国结直肠癌诊疗规范（2025版）（精简版）》。

这篇的抽取质量评级为「好」（汉字占比 60.1%、最长行 508 字符），
是 41 份 PDF 中最容易整理的一篇，且为国家级诊疗规范，权威性最高。

处理流程复用第一篇已验证的步骤：
    extract_guideline_pdf（抽取+合并）→ clean_draft（修结构）
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

BACKEND = Path(r"D:\AI\RAG_project\backend")
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(BACKEND / "scripts"))

from clean_draft import clean  # noqa: E402
from extract_guideline_pdf import to_markdown_draft  # noqa: E402

PDF = Path(r"D:\AI\毕设\PDF\国家卫生健康委员会中国结直肠癌诊疗规范（2025版）（精简版）.pdf")
DRAFT = Path(r"D:\AI\RAG_project\data\manual_draft\结直肠癌诊疗规范2025.草稿.md")
CLEAN = Path(r"D:\AI\RAG_project\data\manual_draft\结直肠癌诊疗规范2025.清理.md")

META = {
    "title": "国家卫生健康委员会中国结直肠癌诊疗规范（2025版）",
    "authors": "国家卫生健康委员会医政司、中华医学会肿瘤学分会",
    "journal": "中华消化外科杂志",
    "year": "2025",
    "evidence": "guideline",
    "phase": "general",     # 诊疗规范覆盖全流程，声明为 general
}

def main() -> int:
    # 步骤 1：抽取
    md, info = to_markdown_draft(PDF, meta_extra=META)
    DRAFT.write_text(md, encoding="utf-8")
    print("步骤1 抽取: %d 页 | %d 行 -> %d 行 | %d 章节 | %d 字" % (
        info["pages"], info["lines_raw"], info["lines_merged"],
        info.get("sections", 0), info.get("draft_chars", 0)))

    # 步骤 2：清理结构
    stats = clean(DRAFT, CLEAN)
    print("步骤2 清理: %s" % stats)

    text = CLEAN.read_text(encoding="utf-8")
    size = len(text.encode("utf-8"))
    heads = re.findall(r"^#{2,3} .+$", text, re.M)
    han = len(re.findall(r"[\u4e00-\u9fff]", text))
    paras = [x for x in text.split("\n")
             if x.strip() and not x.startswith(("#", "*", ">"))]
    print()
    print("=== 清理结果 ===")
    print("  汉字 %d | 字节 %d | 标题 %d | 段落 %d | 最长段 %d" % (
        han, size, len(heads), len(paras), max((len(p) for p in paras), default=0)))
    print()
    print("=== 章节结构 ===")
    for h in heads[:40]:
        print("  %s" % h[:74])
    if len(heads) > 40:
        print("  … 另有 %d 个标题" % (len(heads) - 40))
    print()
    print("  草稿: %s" % DRAFT)
    print("  清理: %s" % CLEAN)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
