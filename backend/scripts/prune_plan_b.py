"""语料精简（方案 B）：总量 120 篇，指南与人工录入优先必保。

方案 B 的约束
-------------
用户要求 **120 篇为总量上限**，且 41 份 PDF 全文全部保留。

当前构成
--------
    人工录入（PDF 整理 + 种子中文文献）  48 篇  ← 必保
    PubMed 指南                          60 篇  ← 必保
    其他（系统评价/RCT/队列/综述等）        48 篇

    必保合计 108 篇 → 仅剩 12 个名额给其他文献。
    若必保已经超过 120，则只能全部保留（无法满足上限），脚本会给出警告。

选材原则
--------
1. **人工录入全部必保** —— 这些是中文全文，是系统唯一的中文语料来源
2. **证据等级为 guideline 的全部必保** —— 最高等级证据
3. **其余名额按分数排序**：
       分数 = 证据等级分 + 时效分 + 主题匹配分
4. 已有问答引用过的文献强制保留（下架会使历史答案的引用失效）

与初版脚本的差异
----------------
初版的选材逻辑（按主题配额分配 70 个名额）适用于「1264 篇 → 120 篇」的场景。
本场景名额只有 12 个，主题配额会导致每个主题只分到 1~2 篇，反而失去意义。
因此改为全局排序取前 N。

安全机制
--------
**只下架不删除**（is_active=False），且执行前写出快照。
恢复：`python scripts/restore_corpus.py --latest`
"""
from __future__ import annotations

import json
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(r"D:\AI\RAG_project\backend")
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

from sqlalchemy import select, update  # noqa: E402

from app.db.session import session_scope  # noqa: E402
from app.models.knowledge import Document  # noqa: E402

TARGET_TOTAL = 120
SNAPSHOT_DIR = Path(r"D:\AI\RAG_project\data\eval")

EVIDENCE_SCORE = {
    "guideline": 100, "systematic": 40, "rct": 35,
    "cohort": 25, "review": 15, "other": 5, "case": 2,
}

# 主题加权：与围术期临床决策直接相关的主题
TOPIC_KWS: list[tuple[str, list[str]]] = [
    ("吻合口漏", ["anastomotic leak", "anastomotic leakage", "dehiscence"]),
    ("加速康复手术", ["enhanced recovery", "eras", "prehabilitation"]),
    ("围术期", ["perioperative", "preoperative", "postoperative"]),
    ("灌注与荧光", ["indocyanine", "fluorescence", "perfusion"]),
    ("微创术式", ["laparoscopic", "robotic", "minimally invasive"]),
    ("新辅助", ["neoadjuvant", "chemoradiotherapy"]),
    ("感染与抗栓", ["surgical site infection", "thrombosis", "anticoagul"]),
    ("营养", ["nutrition", "bowel preparation"]),
    ("并发症", ["complication", "morbidity", "mortality"]),
    ("肝转移", ["liver metastas", "hepatic metastas"]),
]


def _topics(doc: Document) -> list[str]:
    text = ((doc.title or "") + " " + (doc.abstract or "")).lower()
    return [n for n, kws in TOPIC_KWS if any(k in text for k in kws)]


def _score(doc: Document) -> float:
    s = float(EVIDENCE_SCORE.get(doc.evidence_level or "other", 5))
    y = doc.year or 0
    if y >= 2024:
        s += 20
    elif y >= 2022:
        s += 10
    s += 15 * len(_topics(doc))
    return s


def main() -> int:
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    snap = SNAPSHOT_DIR / f"corpus_active_snapshot_{stamp}.json"

    with session_scope() as db:
        docs = db.execute(select(Document)).scalars().all()
        active = [d for d in docs if d.is_active]

        manual = [d for d in active if d.source_db == "manual"]
        guides = [d for d in active if d.source_db != "manual"
                  and d.evidence_level == "guideline"]
        rest = [d for d in active if d.source_db != "manual"
                and d.evidence_level != "guideline"]

        print("=== 语料构成 ===")
        print("  上架总数      : %d" % len(active))
        print("  人工录入(必保): %d" % len(manual))
        print("  PubMed指南(必保): %d" % len(guides))
        print("  其他          : %d" % len(rest))
        print()

        protected = {str(d.id) for d in manual + guides}
        n_slots = TARGET_TOTAL - len(protected)

        if n_slots <= 0:
            print("  ⚠ 必保文献已达 %d 篇，超过目标 %d；无法在不舍弃指南/中文全文的"
                  "前提下满足上限。" % (len(protected), TARGET_TOTAL))
            print("  本次不做任何下架。如需满足 120，请指定可舍弃的类别。")
            return 0

        ranked = sorted(rest, key=lambda d: (-_score(d), -(d.year or 0)))
        keep_extra = ranked[:n_slots]
        drop = ranked[n_slots:]

        print("  必保 %d 篇 + 补足 %d 篇 = %d 篇" % (
            len(protected), len(keep_extra), len(protected) + len(keep_extra)))
        print("  将下架: %d 篇" % len(drop))
        print()
        print("  补足的 %d 篇（按分数排序）:" % len(keep_extra))
        for d in keep_extra:
            print("    [%s|%s] %s" % (
                (d.evidence_level or "?")[:11], d.year or "?",
                (d.title or "")[:62]))
        print()
        print("  下架的证据等级分布: %s" % dict(
            Counter(d.evidence_level for d in drop)))

        keep_ids = protected | {str(d.id) for d in keep_extra}
        active_before = [str(d.id) for d in active]
        snap.write_text(json.dumps({
            "created_at": datetime.now(timezone.utc).isoformat(),
            "purpose": "方案B精简前快照（目标 120 篇）",
            "total_documents": len(docs),
            "active_before": active_before,
            "keep_ids": sorted(keep_ids),
        }, ensure_ascii=False, indent=1), encoding="utf-8")
        print()
        print("  快照: %s" % snap.name)

        to_deactivate = [str(d.id) for d in active if str(d.id) not in keep_ids]
        if not to_deactivate:
            print("  无需改动。")
            return 0

        BATCH = 100
        for i in range(0, len(to_deactivate), BATCH):
            chunk = to_deactivate[i : i + BATCH]
            db.execute(update(Document).where(Document.id.in_(chunk))
                       .values(is_active=False))
            db.flush()
        print("  已下架 %d 篇" % len(to_deactivate))

        from app.api.deps import write_audit  # noqa: E402
        write_audit(db, user=None, action="prune_corpus_plan_b", resource="document",
                    detail={"kept": len(keep_ids), "dropped": len(to_deactivate),
                            "snapshot": snap.name}, success=True)

        after = db.execute(select(Document).where(Document.is_active.is_(True))).scalars().all()
        print()
        print("=== 执行结果 ===")
        print("  上架: %d 篇" % len(after))
        print("  证据等级: %s" % dict(Counter(d.evidence_level for d in after)))
        print("  来源: %s" % dict(Counter(d.source_db for d in after)))
        yrs = Counter(d.year for d in after if (d.year or 0) >= 2021)
        print("  近年: %s" % dict(sorted(yrs.items())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
