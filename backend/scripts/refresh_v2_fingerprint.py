"""刷新评测集 v2 的语料指纹，并复核金标准块是否仍然有效。

为什么要单独一步：评测集里的 `corpus` 是冻结时的语料快照（篇数/块数/sha256）。
后来补了 2 篇中文（118 → 120 篇），快照过期。但**金标准块的 id 没变**，
所以只需更新快照 + 记录"金标准未受影响"，不需要重建题目。

若发现有金标准块失效，脚本会拒绝刷新并列出失效项 —— 那种情况必须重建题目，
不能只改指纹糊过去。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db.session import session_scope  # noqa: E402
from app.models.knowledge import Chunk  # noqa: E402
from scripts.prepare_review_set import corpus_snapshot  # noqa: E402
from sqlalchemy import select  # noqa: E402

SET = Path(r"D:\AI\RAG_project\data\eval\review_set_v2.json")


def main() -> int:
    data = json.loads(SET.read_text(encoding="utf-8"))
    gold = [it["gold_chunk_id"] for it in data["items"]]

    with session_scope() as db:
        exist = {str(x) for x in db.execute(
            select(Chunk.id).where(Chunk.id.in_(gold))
        ).scalars().all()}
        snapshot = corpus_snapshot(db)

    missing = [g for g in gold if g not in exist]
    if missing:
        print(f"  ✗ 有 {len(missing)} 个金标准块失效，不能只刷指纹，必须重建题目：")
        for g in missing[:5]:
            print("    ", g)
        return 1

    old = data.get("corpus") or {}
    data["corpus"] = snapshot
    note = (f"[2026-11-14 补语料后刷新] 语料由 {old.get('documents')} 篇 / {old.get('chunks')} 块"
            f"增至 {snapshot.get('documents')} 篇 / {snapshot.get('chunks')} 块（补 2 篇中文），"
            "金标准块 id 全部校验有效，题目未改动。")
    # limitations 在该文件里是字符串，不是数组 —— 兼容两种写法
    lims = data.get("limitations")
    if isinstance(lims, list):
        if not any("补语料后刷新" in str(x) for x in lims):
            lims.append(note)
    else:
        if "补语料后刷新" not in str(lims):
            data["limitations"] = f"{lims} {note}".strip()

    SET.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  ✓ 指纹已刷新：{old.get('documents')} 篇 → {snapshot.get('documents')} 篇"
          f" / {old.get('chunks')} 块 → {snapshot.get('chunks')} 块")
    print(f"  ✓ 金标准块 {len(gold)} 个全部有效，题目未改动")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
