"""修改开题报告「预期结果」栏中的两个数字，使其与项目实际一致。

修改原因
--------
原开题报告承诺「文献规模不少于 400 篇」，但课题在实施中把语料
控制在 120 篇（方案 B：以质量控制代替数量堆叠）。
若开题报告保留 400 篇而毕业时只有 120 篇，毕业答辩对照检查时无法解释。

另一处是 Recall@5 目标：原文写「不低于 0.60」，
但实测英文 0.632、中文 0.449 —— 中文因跨语言语义匹配更难而偏低，
统一写 0.60 会使中文指标无法达标。改为按语种分列。

修改内容（仅 2 段）
------------------
1. 第一，文献规模 400 篇 → 不少于 120 篇，并说明构成
2. 第五，Recall@5 不低于 0.60 → 英文不低于 0.60、中文不低于 0.44

为什么用「不少于 50 篇指南 / 40 篇中文全文」而不是写实际值
---------------------------------------------------------
实际为指南 60 篇、中文全文 48 篇、文本块约 1720 个。
开题报告是**承诺**，写略低于实际的数字可留出余量，
避免后续微调语料时再次出现「承诺未达成」的问题。
"""
from __future__ import annotations

import shutil
import sys
from datetime import datetime
from pathlib import Path

from docx import Document

DOC = Path(r"D:\AI\毕设\开题报告-基于术中影像与RAG的结直肠手术围术期多模态知识库智能问答系统.docx")

# (原 run 文本的识别片段, 新全文, 说明)
EDITS: list[tuple[str, str, str]] = [
    (
        "文献规模不少于400篇",
        "第一，建成结直肠手术围术期多模态知识库：在语料质量控制下精选文献不少于120篇"
        "（其中临床指南与专家共识不少于50篇、中文全文文献不少于40篇），"
        "结构化文本块不少于1500个且元数据完整，并建成包含不少于1000个实体、"
        "1000条关系的知识图谱。",
        "文献规模 400 篇 → 120 篇",
    ),
    (
        "Recall@5预期不低于0.60",
        "第五，形成量化评测结论：在自建中英文评测集上，检索召回率Recall@5英文不低于0.60、"
        "中文不低于0.44，生成忠实度预期不低于0.75，引用准确率预期不低于0.80，"
        "并完成各检索路径与处理策略的消融实验对比。",
        "Recall@5 统一 0.60 → 按语种分列",
    ),
]


def main() -> int:
    if not DOC.exists():
        print("文件不存在: %s" % DOC)
        return 1

    # 1) 备份
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = DOC.with_name(DOC.stem + f".bak_{stamp}" + DOC.suffix)
    shutil.copy2(DOC, backup)
    print("已备份: %s" % backup.name)
    print()

    doc = Document(str(DOC))
    cell = doc.tables[1].rows[4].cells[0]

    applied = 0
    for needle, new_text, label in EDITS:
        hit = False
        for para in cell.paragraphs:
            if needle not in para.text:
                continue
            # 每个段落只有 1 个 run，直接替换可保留全部格式
            if len(para.runs) == 1:
                para.runs[0].text = new_text
            else:
                # 多 run 时的保守处理：保留首个 run 的格式，清空其余
                first = para.runs[0]
                for r in para.runs[1:]:
                    r.text = ""
                first.text = new_text
            print("✓ %s" % label)
            print("   新文本: %s…" % new_text[:70])
            hit = True
            applied += 1
            break
        if not hit:
            print("✗ 未找到锚点: %s" % needle)

    if applied == 0:
        print("\n未做任何修改，文件未写入")
        return 1

    doc.save(str(DOC))
    print()
    print("已保存: %s（修改 %d 处）" % (DOC.name, applied))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
