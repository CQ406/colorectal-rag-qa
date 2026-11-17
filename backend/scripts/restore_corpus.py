"""从快照恢复语料的上架状态（精简操作的回滚工具）。

使用场景
--------
执行 `apply_corpus_prune.py` 精简语料后，若发现：
  - 选材有误，某些重要文献被下架了
  - 需要重新调整保留范围

可用本脚本按快照恢复。

用法
----
    # 1. 查看有哪些快照
    python scripts/restore_corpus.py --list

    # 2. 恢复最新快照
    python scripts/restore_corpus.py --latest

    # 3. 恢复指定快照
    python scripts/restore_corpus.py --snapshot corpus_active_snapshot_20261013_131320.json

    # 4. 先预览（不实际改动）
    python scripts/restore_corpus.py --latest --dry-run

也可以不用脚本：知识库界面「文献管理」→ 筛选「已下架」→ 勾选 → 批量上架。
脚本适合「一次性恢复大量文献」的场景。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, r"D:\AI\RAG_project\backend")

from sqlalchemy import select, update  # noqa: E402

from app.db.session import session_scope  # noqa: E402
from app.models.knowledge import Document  # noqa: E402

SNAPSHOT_DIR = Path(r"D:\AI\RAG_project\data\eval")


def list_snapshots() -> list[Path]:
    if not SNAPSHOT_DIR.exists():
        return []
    return sorted(SNAPSHOT_DIR.glob("corpus_active_snapshot_*.json"))


def main() -> int:
    ap = argparse.ArgumentParser(description="从快照恢复语料上架状态")
    ap.add_argument("--snapshot", help="快照文件名（在 data/eval 下）")
    ap.add_argument("--latest", action="store_true", help="使用最新的快照")
    ap.add_argument("--list", action="store_true", help="列出可用快照")
    ap.add_argument("--dry-run", action="store_true", help="只预览，不实际改动")
    args = ap.parse_args()

    snaps = list_snapshots()

    if args.list or (not args.snapshot and not args.latest):
        if not snaps:
            print("没有可用快照。目录: %s" % SNAPSHOT_DIR)
            return 1
        print("可用快照（新→旧）:")
        for p in reversed(snaps):
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                n = len(data.get("active_before", []))
                print("  %-52s %4d 篇  创建于 %s" % (
                    p.name, n, data.get("created_at", "?")[:19]))
            except Exception:  # noqa: BLE001
                print("  %-52s （读取失败）" % p.name)
        return 0

    path = None
    if args.latest:
        if not snaps:
            print("没有可用快照")
            return 1
        path = snaps[-1]
    elif args.snapshot:
        path = SNAPSHOT_DIR / args.snapshot
    if path is None or not path.exists():
        print("快照文件不存在: %s" % path)
        return 1

    data = json.loads(path.read_text(encoding="utf-8"))
    active_before = data.get("active_before", [])
    print("使用快照: %s" % path.name)
    print("  创建时间      : %s" % data.get("created_at", "?")[:19])
    print("  当时上架文献数: %d" % len(active_before))
    print("  当时总数      : %s" % data.get("total_documents"))

    with session_scope() as db:
        docs = db.execute(select(Document)).scalars().all()
        current_active = {str(d.id) for d in docs if d.is_active}
        target_active = set(active_before)

        to_activate = target_active - current_active
        to_deactivate = current_active - target_active

        print()
        print("  需要恢复上架: %d 篇" % len(to_activate))
        print("  需要重新下架: %d 篇" % len(to_deactivate))

        if args.dry_run:
            print()
            print("[预览模式] 未做任何改动")
            return 0

        BATCH = 200
        for ids, flag, label in ((list(to_activate), True, "上架"),
                                 (list(to_deactivate), False, "下架")):
            for i in range(0, len(ids), BATCH):
                db.execute(
                    update(Document)
                    .where(Document.id.in_(ids[i : i + BATCH]))
                    .values(is_active=flag)
                )
                db.flush()

        from sqlalchemy import func
        n_active = db.execute(
            select(func.count()).select_from(Document).where(Document.is_active.is_(True))
        ).scalar_one()
        print()
        print("  恢复完成，当前上架: %d 篇" % n_active)

        from app.api.deps import write_audit  # noqa: E402
        write_audit(
            db, user=None, action="restore_corpus", resource="document",
            detail={"snapshot": path.name, "activated": len(to_activate),
                    "deactivated": len(to_deactivate), "now_active": n_active},
            success=True,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
