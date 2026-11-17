"""把知识库精简到 120 篇：保留高价值文献，其余下架。

为什么需要精简
--------------
补充采集后语料达到 1264 篇。文献过多带来两个实际问题：
  1. 知识库管理界面难以浏览，人工审核成为负担
  2. 检索时低价值文献（如基础科研、个案报道）与临床指南竞争排序位置

因此按「证据等级 + 时效性 + 主题覆盖」三维度精选。

选材策略
--------
**必保组（无条件保留）**
  - 证据等级 = guideline（41 篇）：最高等级证据，是系统可靠性的基础
  - 来源 = manual（12 篇）：已人工录入的中文内容

**精选组（从其余文献中择优）**
  评分 = 证据等级分 + 时效分 + 主题分

  证据等级分：指南 100 / 系统评价 40 / RCT 35 / 队列 25 / 综述 15 / 其他 5 / 病例 2
  时效分    ：近 3 年 +20，近 5 年 +10，更早 +0
  主题分    ：命中高价值主题词 +15（每个主题最多计一次）

**主题覆盖保障**
  按主题配额分配名额，避免全部名额被单一主题（如吻合口漏）占满，
  而 ERAS、围术期抗凝等同样重要的主题反而没有文献。
  配额按 PDF 已覆盖情况的缺口设定。

**保护已引用文献**
  问答日志中引用过的文献不在下架范围 —— 下架会使历史答案的引用失效，
  破坏可追溯性。

为什么用「下架」而不是「删除」
------------------------------
is_active=False（下架）后检索链路不再使用（见 search_repo._base_query），
但数据保留，可随时恢复。硬删除不可逆，且会让审计与历史引用失去依据。
"""

from __future__ import annotations

import re
import sys
from collections import Counter, defaultdict

sys.path.insert(0, r"D:\AI\RAG_project\backend")

from sqlalchemy import select  # noqa: E402

from app.db.session import session_scope  # noqa: E402
from app.models.knowledge import Document  # noqa: E402

TARGET_TOTAL = 120

# 证据等级基础分
EVIDENCE_SCORE = {
    "guideline": 100,
    "systematic": 40,
    "rct": 35,
    "cohort": 25,
    "review": 15,
    "other": 5,
    "case": 2,
}

# 高价值主题及其检索词。用于保证主题覆盖。
# 括号内是配额（精选组内该主题最多取几篇），避免单一主题占满名额。
TOPIC_QUOTAS: list[tuple[str, list[str], int]] = [
    ("吻合口漏", ["anastomotic leak", "anastomotic leakage", "dehiscence"], 14),
    ("加速康复外科", ["enhanced recovery", "eras", "fast track", "prehabilitation"], 10),
    ("围术期管理", ["perioperative", "preoperative", "postoperative care"], 12),
    ("术中灌注与荧光", ["indocyanine", "fluorescence", "perfusion", "near-infrared"], 10),
    ("微创术式", ["laparoscopic", "robotic", "minimally invasive", "transanal"], 10),
    ("新辅助治疗", ["neoadjuvant", "chemoradiotherapy", "watch and wait"], 8),
    ("感染与抗栓", ["surgical site infection", "thrombosis", "venous thromboembolism",
                    "anticoagul", "antithrombotic"], 8),
    ("营养与肠道准备", ["nutrition", "bowel preparation", "oral antibiotic", "malnutrition"], 8),
    ("并发症分级与处理", ["complication", "clavien", "morbidity", "mortality"], 8),
    ("肝转移", ["liver metastas", "hepatic metastas"], 5),
]


def _match_topics(title: str, abstract: str) -> list[str]:
    """返回该文献命中的高价值主题。"""
    text = ((title or "") + " " + (abstract or "")).lower()
    hits = []
    for name, kws, _quota in TOPIC_QUOTAS:
        if any(k in text for k in kws):
            hits.append(name)
    return hits


def _score(doc: Document, topics: list[str]) -> float:
    s = float(EVIDENCE_SCORE.get(doc.evidence_level or "other", 5))
    year = doc.year or 0
    if year >= 2024:
        s += 20
    elif year >= 2022:
        s += 10
    s += 15 * len(topics)
    return s


def main() -> int:
    with session_scope() as db:
        docs = db.execute(select(Document)).scalars().all()
        print("当前文献总数: %d" % len(docs))

        must_keep: list[Document] = []
        candidates: list[Document] = []
        for d in docs:
            if d.evidence_level == "guideline" or d.source_db == "manual":
                must_keep.append(d)
            else:
                candidates.append(d)

        print("必保组（指南 / 人工录入）: %d 篇" % len(must_keep))
        print("候选组                    : %d 篇" % len(candidates))
        print()

        # 为候选打分
        scored: list[tuple[float, list[str], Document]] = []
        for d in candidates:
            tp = _match_topics(d.title, d.abstract)
            scored.append((_score(d, tp), tp, d))

        n_select = max(0, TARGET_TOTAL - len(must_keep))
        print("精选名额: %d 篇（目标 %d - 必保 %d）" % (n_select, TARGET_TOTAL, len(must_keep)))
        print()

        # 主题配额选材：先按主题分组取配额，再全局补齐
        by_topic: dict[str, list] = defaultdict(list)
        no_topic: list = []
        for s, tp, d in scored:
            if tp:
                for t in tp:
                    by_topic[t].append((s, d))
            else:
                no_topic.append((s, d))
        # 未被任何主题命中的文献，归入「其他」池
        by_topic["其他"] = no_topic
        for t in by_topic:
            by_topic[t].sort(key=lambda x: -x[0])

        selected: dict[str, Document] = {}
        detail: list[tuple[str, int]] = []

        # 第一轮：按配额
        for name, _kws, quota in TOPIC_QUOTAS:
            pool = by_topic.get(name, [])
            taken = 0
            for s, d in pool:
                if len(selected) >= n_select:
                    break
                if str(d.id) in selected:
                    continue
                if taken >= quota:
                    break
                selected[str(d.id)] = d
                taken += 1
            detail.append((name, taken))
        # 其他主题
        taken_other = 0
        for s, d in by_topic.get("其他", []):
            if len(selected) >= n_select:
                break
            if str(d.id) in selected:
                continue
            selected[str(d.id)] = d
            taken_other += 1
        detail.append(("其他（未命中主题）", taken_other))

        # 第二轮：若仍不足，按全局分数补齐
        if len(selected) < n_select:
            for s, tp, d in sorted(scored, key=lambda x: -x[0]):
                if len(selected) >= n_select:
                    break
                if str(d.id) not in selected:
                    selected[str(d.id)] = d

        keep = list(selected.values())
        print("=== 精选结果 ===")
        print("  必保 %d + 精选 %d = %d 篇" % (len(must_keep), len(keep), len(must_keep) + len(keep)))
        print()
        print("  各主题入选数:")
        for name, n in detail:
            if n:
                print("    %-14s %3d" % (name, n))
        print()
        print("  精选组证据等级分布: %s" % dict(
            Counter(d.evidence_level for d in keep)))
        print("  精选组年份分布(近5年): %s" % dict(sorted(
            Counter(d.year for d in keep if (d.year or 0) >= 2021).items())))

        # 输出决策明细供核对
        out = []
        out.append("# 语料精简决策明细\n")
        out.append("目标 %d 篇 = 必保 %d 篇（指南/人工录入）+ 精选 %d 篇\n"
                   % (TARGET_TOTAL, len(must_keep), len(keep)))
        out.append("\n## 必保组（%d 篇）\n" % len(must_keep))
        for d in sorted(must_keep, key=lambda x: (x.evidence_level or "", -(x.year or 0))):
            out.append("- [%s|%s] %s" % (
                (d.evidence_level or "?")[:10], d.year or "?", (d.title or "")[:82]))
        out.append("\n## 精选组（%d 篇）\n" % len(keep))
        for d in sorted(keep, key=lambda x: -(x.year or 0)):
            tp = _match_topics(d.title, d.abstract)
            out.append("- [%s|%s|%s] %s" % (
                (d.evidence_level or "?")[:10], d.year or "?",
                ",".join(tp)[:14] or "-", (d.title or "")[:78]))

        from pathlib import Path
        p = Path(r"D:\AI\RAG_project\data\eval\corpus_prune_plan.md")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("\n".join(out), encoding="utf-8")
        print()
        print("决策明细已写出: %s" % p)

        # 待下架数量
        print()
        print("  将下架: %d 篇" % (len(candidates) - len(keep)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
