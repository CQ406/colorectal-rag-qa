"""补齐文献综述中未被引用的参考文献，并核对引用完整性。

背景
----
替换参考文献后，文献表有 12 条，但正文只引用了其中 10 条，
[2]（结直肠癌肝转移诊疗指南）与 [12]（CSCO 英文版指南）成了「列而不引」的
凑数条目 —— 这是学术写作的硬伤，答辩时被翻到会很难解释。

补充位置
--------
[2]  结直肠癌肝转移诊疗指南 → 放在 1. 背景意义中讨论转移性病灶的决策复杂度。
     肝转移是结直肠癌最主要的远处转移方式，其处理策略涉及外科、内科与
     放疗的多学科协作，是围术期知识「跨专科、格式松散」的典型例证。
[12] CSCO 英文版指南 → 放在 2.2 检索增强生成技术中，作为「国内学会指南
     存在中英双版本」的证据，说明语料存在同源重复，检索系统需处理版本冗余。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(r"D:\AI\RAG_project\backend")
sys.path.insert(0, str(REPO))

CONTENT = REPO / "scripts" / "review_content.py"

# ---- 补 [2]：背景意义中补一句肝转移的决策复杂度 ----
ANCHOR_2 = "魏天笑等在《中华消化外科杂志》上开展的卫生经济学分析进一步表明"
INSERT_2 = (
    "结直肠癌肝转移是最常见的远处转移形式，其处理涉及外科、肿瘤内科与放疗科的"
    "多学科协作，中国医师协会外科医师分会等在《结直肠癌肝转移诊断和综合治疗指南"
    "（2025版）》中系统梳理了可切除性评估、转化治疗与手术时机的决策框架[2]，"
    "其内容横跨多个专科、更新频繁，是围术期知识难以被单篇文献完整覆盖的典型例证。"
)

# ---- 补 [12]：2.2 节讨论语料版本冗余 ----
ANCHOR_12 = "上述研究对本课题的共同启发是"
INSERT_12 = (
    "值得注意的是，同一部指南可能存在多个语言版本 —— 例如 CSCO 结直肠癌诊疗指南"
    "既有中文版也有英文版[12]，若不加区分地采集入库，会在语料中形成同源重复条目，"
    "挤占检索名额并造成引用来源混淆。这提示知识库构建阶段必须做同源识别与去重。"
)


def main() -> int:
    t = CONTENT.read_text(encoding="utf-8")

    # 1) 补 [2]
    if "[2]" not in t.split("REFS = [")[0]:
        if ANCHOR_2 in t:
            t = t.replace(ANCHOR_2, INSERT_2 + ANCHOR_2, 1)
            print("[2] 已补入背景意义节")
        else:
            print("⚠ [2] 锚点未找到")
    else:
        print("[2] 已存在于正文")

    # 2) 补 [12]
    if "[12]" not in t.split("REFS = [")[0]:
        if ANCHOR_12 in t:
            t = t.replace(ANCHOR_12, INSERT_12 + ANCHOR_12, 1)
            print("[12] 已补入 2.2 节")
        else:
            print("⚠ [12] 锚点未找到")
    else:
        print("[12] 已存在于正文")

    CONTENT.write_text(t, encoding="utf-8")

    # 3) 核对完整性
    body = t.split("REFS = [")[0]
    cites = sorted(set(int(x) for x in re.findall(r"\[(\d+)\]", body)))
    n_refs = len(re.findall(r'^\s*"\[\d+\]', t.split("REFS = [")[1], re.M))
    print()
    print("文献表条目: %d" % n_refs)
    print("正文引用编号: %s" % cites)
    missing = [i for i in range(1, n_refs + 1) if i not in cites]
    extra = [c for c in cites if c > n_refs]
    print("未被引用的文献: %s" % (missing if missing else "无 ✓"))
    print("超出范围的引用: %s" % (extra if extra else "无 ✓"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
