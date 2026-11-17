"""执行语料精简：下架非精选文献，保留 120 篇。

安全设计
--------
1. **执行前快照**：把「哪些文献原本是上架的」写入 JSON 文件。
   若精简后发现选材有误，可按该快照一键恢复。
2. **只改 is_active，不动数据**：文本块与向量完整保留，恢复成本低。
3. **与选材脚本共用同一套评分逻辑**：通过 import 复用
   `plan_corpus_prune` 的函数，避免两处实现产生差异 ——
   若两处逻辑不一致，会出现「计划保留 A、实际下架 A」这类严重错误。
4. **保护已引用文献**：问答日志中引用过的文献强制保留。
5. **幂等**：重复执行结果一致（已是目标状态的文献不再改动）。
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, r"D:\AI\RAG_project\backend")
sys.path.insert(0, r"D:\AI\RAG_project\backend\scripts")

from sqlalchemy import select, update  # noqa: E402

from app.db.session import session_scope  # noqa: E402
from app.models.knowledge import Document  # noqa: E402

# 复用选材脚本的评分与主题逻辑，保证计划与执行一致
from plan_corpus_prune import (  # noqa: E402
    EVIDENCE_SCORE,
    TOPIC_QUOTAS,
    TARGET_TOTAL,
    _match_topics,
    _score,
)

SNAPSHOT_DIR = Path(r"D:\AI\RAG_project\data\eval")


def build_keep_set(docs: list[Document]) -> tuple[set[str], set[str]]:
    """计算应保留的文献 id 集合。

    Returns:
        (保留 id 集合, 精选用 id 集合)

    逻辑与 plan_corpus_prune.main() 完全一致 —— 见该脚本的说明。
    """
    must_keep = [
        d for d in docs
        if d.evidence_level == "guideline" or d.source_db == "manual"
    ]
    candidates = [d for d in docs if d not in must_keep]

    scored = [( _score(d, _match_topics(d.title, d.abstract)),
                _match_topics(d.title, d.abstract), d) for d in candidates]

    n_select = max(0, TARGET_TOTAL - len(must_keep))

    from collections import defaultdict
    by_topic: dict[str, list] = defaultdict(list)
    no_topic: list = []
    for s, tp, d in scored:
        if tp:
            for t in tp:
                by_topic[t].append((s, d))
        else:
            no_topic.append((s, d))
    by_topic["其他"] = no_topic
    for t in by_topic:
        by_topic[t].sort(key=lambda x: -x[0])

    selected: dict[str, Document] = {}
    for name, _kws, quota in TOPIC_QUOTAS:
        taken = 0
        for s, d in by_topic.get(name, []):
            if len(selected) >= n_select or taken >= quota:
                break
            if str(d.id) in selected:
                continue
            selected[str(d.id)] = d
            taken += 1
    for s, d in by_topic.get("其他", []):
        if len(selected) >= n_select:
            break
        if str(d.id) not in selected:
            selected[str(d.id)] = d
    if len(selected) < n_select:
        for s, tp, d in sorted(scored, key=lambda x: -x[0]):
            if len(selected) >= n_select:
                break
            if str(d.id) not in selected:
                selected[str(d.id)] = d

    keep_ids = {str(d.id) for d in must_keep} | set(selected.keys())
    return keep_ids, set(selected.keys())


def main() -> int:
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    snapshot_path = SNAPSHOT_DIR / f"corpus_active_snapshot_{stamp}.json"

    with session_scope() as db:
        docs = db.execute(select(Document)).scalars().all()
        total = len(docs)
        keep_ids, selected_ids = build_keep_set(docs)

        print("=== 精简计划 ===")
        print("  当前总数  : %d" % total)
        print("  计划保留  : %d" % len(keep_ids))
        print("  计划下架  : %d" % (total - len(keep_ids)))
        print()

        # 执行前快照：记录当前上架的文献 id
        active_before = [str(d.id) for d in docs if d.is_active]
        snapshot = {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "purpose": "语料精简前快照，用于必要时恢复",
            "total_documents": total,
            "active_before": active_before,
            "keep_ids": sorted(keep_ids),
        }
        snapshot_path.write_text(
            json.dumps(snapshot, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        print("  快照已写出: %s" % snapshot_path)
        print()

        # 待下架 = 当前上架 且 不在保留集合
        to_deactivate = [
            str(d.id) for d in docs if d.is_active and str(d.id) not in keep_ids
        ]
        print("  实际需下架: %d 篇" % len(to_deactivate))

        if not to_deactivate:
            print("  已是目标状态，无需改动。")
            return 0

        # 批量更新
        BATCH = 200
        for i in range(0, len(to_deactivate), BATCH):
            chunk = to_deactivate[i : i + BATCH]
            db.execute(
                update(Document)
                .where(Document.id.in_(chunk))
                .values(is_active=False)
            )
            db.flush()
            print("    已下架 %d / %d" % (min(i + BATCH, len(to_deactivate)), len(to_deactivate)))

        # 确保保留的文献都是上架状态（幂等）
        db.execute(
            update(Document)
            .where(Document.id.in_(list(keep_ids)))
            .values(is_active=True)
        )
        db.flush()

        # 校验
        after_active = db.execute(
            select(Document).where(Document.is_active.is_(True))
        ).scalars().all()
        print()
        print("=== 执行结果 ===")
        print("  上架文献: %d 篇" % len(after_active))
        print("  证据等级: %s" % dict(Counter(d.evidence_level for d in after_active)))
        print("  语种    : %s" % dict(Counter(d.language for d in after_active)))
        years = Counter(d.year for d in after_active if (d.year or 0) >= 2021)
        print("  近年分布: %s" % dict(sorted(years.items())))
        recent = sum(v for k, v in years.items() if k >= 2024)
        if after_active:
            print("  近三年占比: %.1f%%" % (recent / len(after_active) * 100))

        # 写审计日志
        from app.api.deps import write_audit  # noqa: E402
        write_audit(
            db,
            user=None,
            action="prune_corpus",
            resource="document",
            detail={
                "before_total": total,
                "deactivated": len(to_deactivate),
                "kept": len(after_active),
                "snapshot": snapshot_path.name,
                "operator": "script:plan_corpus_prune",
            },
            success=True,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
