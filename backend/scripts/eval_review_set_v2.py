"""用评测集 v2 跑一次检索评测（不写入冻结报告，只出指标）。

为什么单独一个脚本：`run_review_ablation.py` 绑定的是 v1 冻结集与固定报告路径，
直接改它会影响历史报告的可复现性。这里只读 v2、只算指标、单独落盘。

输出 data/eval/review_v2_metrics.json
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

SET = Path(r"D:\AI\RAG_project\data\eval\review_set_v2.json")
OUT = Path(r"D:\AI\RAG_project\data\eval\review_v2_metrics.json")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://127.0.0.1:8000")
    ap.add_argument("--configs", default="hybrid,hybrid_rerank")
    ap.add_argument("--top-k", type=int, default=10)
    args = ap.parse_args()

    data = json.loads(SET.read_text(encoding="utf-8"))
    items = data["items"]

    # 前置校验：金标准块必须都还在库里
    from app.db.session import session_scope
    from app.models.knowledge import Chunk
    from sqlalchemy import select
    gold = [g for it in items for g in it["gold_chunk_ids"]]
    with session_scope() as db:
        exist = {str(x) for x in db.execute(select(Chunk.id).where(Chunk.id.in_(gold))).scalars().all()}
    missing = [g for g in gold if g not in exist]
    print(f"金标准块 {len(gold)} 个，失效 {len(missing)} 个")
    if missing:
        print("  失效示例:", missing[:3])

    results = []
    for cfg in args.configs.split(","):
        use_rerank = cfg.endswith("rerank")
        ok5 = ok10 = 0
        mrr = 0.0
        lat = []
        for it in items:
            body = {
                "query": it["question"], "top_k": args.top_k,
                "recall_mode": "hybrid", "use_graph": True, "use_rerank": use_rerank,
            }
            req = Request(args.base_url + "/api/search",
                          data=json.dumps(body).encode(),
                          headers={"Content-Type": "application/json"})
            t0 = time.perf_counter()
            with urlopen(req, timeout=600) as resp:  # noqa: S310
                d = json.loads(resp.read())
            lat.append((time.perf_counter() - t0) * 1000)

            ids = [r.get("chunk_id") for r in (d.get("results") or [])]
            golds = set(it["gold_chunk_ids"])
            rank = next((i + 1 for i, cid in enumerate(ids) if cid in golds), None)
            if rank and rank <= 5:
                ok5 += 1
            if rank and rank <= 10:
                ok10 += 1
            if rank:
                mrr += 1.0 / rank
            it.setdefault("_ranks", {})[cfg] = rank

        n = len(items)
        results.append({
            "label": cfg, "total": n,
            "recall_at_5": round(ok5 / n, 3),
            "recall_at_10": round(ok10 / n, 3),
            "mrr_at_10": round(mrr / n, 3),
            "mean_ms": round(sum(lat) / n, 1),
        })
        print(f"  {cfg:20s} R@5={ok5/n:.3f} R@10={ok10/n:.3f} MRR={mrr/n:.3f} 平均 {sum(lat)/n:.0f}ms")

    payload = {
        "dataset": data.get("dataset"), "version": data.get("version"),
        "corpus": data.get("corpus"), "results": results,
        "per_question": [
            {"qid": it["qid"], "relational": it["relational"], "ranks": it.get("_ranks", {})}
            for it in items
        ],
        "missing_gold": len(missing),
    }
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"结果已写入 {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
