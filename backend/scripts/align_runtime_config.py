"""把配置表里与代码默认值不一致的检索参数对齐，并打印差异。

背景
----
配置表的 `ensure_defaults` 只补缺失键、**从不覆盖已有值**（这是对的：
管理员调过的参数不能被重启重置）。但由此带来一个副作用：

    代码里把 graph_weight 默认值从 0.6 改成 0.2 后，
    配置表里仍留着旧的 0.6；一旦检索链路改为"以配置表为准"，
    生效的就是 0.6 —— 也就是实测会拖累排序的那个值。

所以改代码默认值时，必须同时把配置表对齐。本脚本干这件事，并留下差异记录。

用法：
    python -m scripts.align_runtime_config        # 只看差异
    python -m scripts.align_runtime_config --apply
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import settings  # noqa: E402
from app.db.session import session_scope  # noqa: E402
from app.repositories import config_repo  # noqa: E402

# 配置键 → 代码里的默认值
PAIRS = {
    "retrieval.vector_weight": lambda: settings.retrieval.vector_weight,
    "retrieval.keyword_weight": lambda: settings.retrieval.keyword_weight,
    "retrieval.graph_weight": lambda: settings.retrieval.graph_weight,
    "retrieval.min_keyword_overlap": lambda: settings.retrieval.min_keyword_overlap,
    "retrieval.rerank_candidate_limit": lambda: settings.retrieval.rerank_candidate_limit,
    "retrieval.evidence_boost_strength": lambda: settings.retrieval.evidence_boost_strength,
    "retrieval.max_chunks_per_document": lambda: settings.retrieval.max_chunks_per_document,
    "llm.temperature": lambda: settings.llm.llm_temperature,
    "system.enable_graph_retrieval": lambda: settings.retrieval.enable_graph_retrieval,
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真的写入（默认只打印）")
    args = ap.parse_args()

    changed = []
    with session_scope() as db:
        cfg = config_repo.effective(db)
        for key, getter in PAIRS.items():
            try:
                want = getter()
            except AttributeError:
                continue
            have = cfg.get(key)
            same = (have == want) or (
                isinstance(want, (int, float)) and isinstance(have, (int, float))
                and abs(float(have) - float(want)) < 1e-9
            )
            flag = "✓" if same else "✗"
            print(f"  {flag} {key:36s} 配置表={have!r:<10} 代码={want!r}")
            if not same:
                changed.append({"key": key, "from": have, "to": want})

        if changed and args.apply:
            for item in changed:
                config_repo.update_value(db, item["key"], item["to"], updated_by="align-script")
            db.commit()
            print(f"\n  已对齐 {len(changed)} 项")
        elif changed:
            print(f"\n  有 {len(changed)} 项不一致（加 --apply 才会写入）")
        else:
            print("\n  配置表与代码默认值一致")

    out = Path(r"D:\AI\RAG_project\data\eval\config_alignment.json")
    out.write_text(json.dumps({"changed": changed, "applied": args.apply},
                              ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
