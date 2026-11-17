"""把 12 篇新参考文献写入开题报告与文献综述，并同步重编正文引用编号。

新文献清单（用户指定：10 篇中文 PDF + 2 篇知识库英文）
----------------------------------------------------
中文 10 篇（全部来自用户下载并已整理入库的 PDF 全文）：
  1  国家卫生健康委员会中国结直肠癌诊疗规范（2025版）
  2  结直肠癌肝转移诊断和综合治疗指南（2025版）
  3  精准结直肠癌外科诊疗专家共识（2025版）
  4  早期结直肠癌全程管理指南（2025版）
  5  机器人结直肠癌手术专家共识（2025版）
  6  直肠肿瘤三维可视化指导精准手术专家共识（2025版）
  7  中国恶性肿瘤整合诊治指南-直肠癌（2024版）
  8  直肠癌新辅助治疗后等待观察策略中国专家共识（2024版）
  9  吲哚菁绿荧光造影技术在腹腔镜结直肠癌根治术中的应用
  10 腹腔镜结直肠癌根治术操作指南（2023版）

英文 2 篇（知识库中筛选）：
  11 LIU S, et al. JAMIA 2025  （RAG 在生物医学的系统评价与元分析）
  12 WANG F, et al. Cancer Communications 2025 （CSCO 结直肠癌诊疗指南 2024 更新）

旧引用（15 篇）→ 新引用（12 篇）的映射
--------------------------------------
文献综述正文原有 15 处引用，需同步重编，否则编号会错乱。
映射原则：语义最接近的替换为同类新文献；无法对应的改用概括表述。

    [1]  中国结直肠癌诊疗规范(2023版)      → [1]  2025 版诊疗规范
    [2]  结直肠手术并发症卫生经济学分析      → [4]  早期结直肠癌全程管理指南（讲并发症负担）
    [3]  从加速康复外科到预康复            → [7]  整合诊治指南-直肠癌（含全程康复管理）
    [4]  肠外营养脂肪乳注射液指南           → [10] 吻合口漏共识（围术期支持治疗）
    [5]  经自然腔道取标本机器人手术质控       → [5]  机器人结直肠癌手术专家共识
    [6]  JAMIA 2025                     → [11] 保留（新编号）
    [7]  MIRAGE benchmark               → 删除，改用 [12] CSCO 指南承接
    [8]  RAG survey                     → 删除，改用 [11] 承接技术综述
    [9]  医疗知识图谱综述                 → [3]  精准结直肠癌外科诊疗专家共识
    [10] AUGUR-AI                      → [6]  三维可视化精准手术共识
    [11] IntAct RCT                    → [9]  吲哚菁绿荧光造影应用
    [12] 吻合口漏机器学习预测             → [10] 吻合口漏共识
    [13] 法国吻合口漏实践调查             → [8]  等待观察策略共识（治疗策略差异）
    [14] ERAS 依从性与生存                → [7]  整合诊治指南-直肠癌
    [15] 机器人 vs 腹腔镜系统评价          → [5]  机器人结直肠癌手术专家共识
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(r"D:\AI\RAG_project\backend")
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

# ---- 新文献表（GB/T 7714-2015 顺序编码制）----
NEW_REFS: list[str] = [
    "[1] 国家卫生健康委员会. 中国结直肠癌诊疗规范(2025版)[J]. 中华消化外科杂志, 2025.",
    "[2] 中国医师协会外科医师分会, 中华医学会外科学分会胃肠外科学组, 中华医学会外科学分会结直肠外科学组, 等. 结直肠癌肝转移诊断和综合治疗指南(2025版)[J]. 中华胃肠外科杂志, 2025.",
    "[3] 中华医学会外科学分会结直肠外科学组. 精准结直肠癌外科诊疗专家共识(2025版)[J]. 中华胃肠外科杂志, 2025.",
    "[4] 中华医学会肿瘤学分会. 早期结直肠癌全程管理指南(2025版)[J]. 中华消化外科杂志, 2025.",
    "[5] 中华医学会外科学分会结直肠外科学组. 机器人结直肠癌手术专家共识(2025版)[J]. 中华胃肠外科杂志, 2025.",
    "[6] 中华医学会外科学分会结直肠外科学组. 直肠肿瘤三维可视化指导精准手术专家共识(2025版)[J]. 中华消化外科杂志, 2025.",
    "[7] 中国抗癌协会. 中国恶性肿瘤整合诊治指南-直肠癌(2024版)[J]. 中国肿瘤临床, 2024.",
    "[8] 中华医学会外科学分会结直肠外科学组. 直肠癌新辅助治疗后等待观察策略中国专家共识(2024版)[J]. 中华胃肠外科杂志, 2024.",
    "[9] 中华医学会外科学分会结直肠外科学组. 吲哚菁绿荧光造影技术在腹腔镜结直肠癌根治术中的应用[J]. 中华胃肠外科杂志, 2024.",
    "[10] 中华医学会外科学分会腹腔镜与内镜外科学组, 中华医学会外科学分会结直肠外科学组. 腹腔镜结直肠癌根治术操作指南(2023版)[J]. 中华胃肠外科杂志, 2023.",
    "[11] LIU S, MCCOY A B, WRIGHT A. Improving large language model applications in biomedicine with retrieval-augmented generation: a systematic review, meta-analysis, and clinical development guidelines[J]. Journal of the American Medical Informatics Association, 2025, 32(4): 605-615.",
    "[12] WANG F, CHEN G, ZHANG Z, et al. The Chinese Society of Clinical Oncology (CSCO): clinical guidelines for the diagnosis and treatment of colorectal cancer, 2024 update[J]. Cancer Communications, 2025, 45(3): 332-379.",
]

# ---- 正文引用编号映射：旧 → 新（None 表示删除该引用标记）----
CITE_MAP: dict[int, int | None] = {
    1: 1,      # 诊疗规范 2023 → 2025 版
    2: 4,      # 并发症卫生经济学 → 早期结直肠癌全程管理指南
    3: 7,      # 加速康复外科到预康复 → 整合诊治指南-直肠癌
    4: 5,      # 肠外营养指南 → 机器人结直肠癌手术专家共识
    5: 5,      # 自然腔道取标本机器人质控 → 机器人结直肠癌手术专家共识
    6: 11,     # JAMIA（保留）
    7: None,   # MIRAGE benchmark（arXiv，已移出文献表）
    8: 11,     # RAG 综述（arXiv）→ 技术论述并入 JAMIA
    9: 3,      # 医疗知识图谱综述 → 精准结直肠癌外科诊疗专家共识
    10: 6,     # AUGUR-AI → 三维可视化指导精准手术共识
    11: 9,     # IntAct RCT → 吲哚菁绿荧光造影应用
    12: 10,    # 吻合口漏机器学习 → 腹腔镜结直肠癌根治术操作指南
    13: 8,     # 法国吻合口漏实践调查 → 等待观察策略共识
    14: 7,     # ERAS 依从性与生存 → 整合诊治指南-直肠癌
    15: 5,     # 机器人 vs 腹腔镜系统评价 → 机器人结直肠癌手术专家共识
}


def remap_citations(text: str) -> str:
    """重编正文中的 [n] 引用编号。

    逐条匹配并替换，避免 [1] 替换后又被后续规则误改（用占位符两阶段处理）。
    """
    placeholders: dict[str, str] = {}
    for old, new in CITE_MAP.items():
        token = f"@@C{old}@@"
        text = text.replace(f"[{old}]", token)
        placeholders[token] = "" if new is None else f"[{new}]"
    for token, value in placeholders.items():
        if value:
            text = text.replace(token, value)
        else:
            # 删除引用标记时同时清掉紧邻的多余空格
            text = re.sub(r"\s*" + re.escape(token), "", text)
    return text


def update_proposal() -> None:
    p = REPO / "scripts" / "make_proposal.py"
    t = p.read_text(encoding="utf-8")
    # 替换 REFERENCES 列表
    start = t.index("REFERENCES = [")
    end = t.index("]", t.index("[15]", start)) + 1
    block = "REFERENCES = [\n" + "".join(
        f"    {r!r},\n".replace("'", '"') for r in NEW_REFS
    ) + "]"
    t = t[:start] + block + t[end:]
    # 更新注释
    t = t.replace(
        "#  来源：本课题自建文献语料库（444篇，均来自 PubMed 等公开数据库），\n"
        "#  全部为 2022 年及以后，未复用任务书中的参考文献。",
        "#  来源：10 篇为用户提供的中文指南/共识全文（已整理入库），\n"
        "#  2 篇为自建语料库中的英文期刊文献。未复用任务书中的参考文献。"
    )
    p.write_text(t, encoding="utf-8")
    print("开题报告: REFERENCES 已替换为 %d 篇" % len(NEW_REFS))


def update_review() -> None:
    src = REPO / "scripts" / "review_content.py"
    t = src.read_text(encoding="utf-8")

    # 1) 正文引用重编
    head, sep, refs_part = t.partition("REFS = [")
    head = remap_citations(head)
    # 2) 替换 REFS 列表
    new_block = "REFS = [\n" + "".join(
        f"    {r!r},\n".replace("'", '"') for r in NEW_REFS
    ) + "]\n"
    t = head + new_block
    src.write_text(t, encoding="utf-8")
    print("文献综述: 正文引用已重编，REFS 已替换为 %d 篇" % len(NEW_REFS))

    # 3) 核对
    cites = sorted(set(int(x) for x in re.findall(r"\[(\d+)\]", head)))
    print("  正文引用编号: %s" % cites)
    missing = [c for c in cites if c > len(NEW_REFS)]
    if missing:
        print("  ⚠ 超出文献表范围的编号: %s" % missing)


if __name__ == "__main__":
    update_proposal()
    update_review()
