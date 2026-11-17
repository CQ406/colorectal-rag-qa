"""知识图谱构建命令行工具（阶段五）。

用法:
    # 构建图谱（增量，支持断点续跑）
    python -m scripts.build_graph --limit 400

    # 清空后重建
    python -m scripts.build_graph --limit 400 --rebuild

    # 仅查看抽取缓存统计（不访问 Neo4j，秒出）
    python -m scripts.build_graph --report

    # 查看 Neo4j 中的图谱规模
    python -m scripts.build_graph --stats
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.logging import get_logger, setup_logging
from app.db.session import session_scope

logger = get_logger("scripts.build_graph")

DEFAULT_CACHE = r"D:\AI\RAG_project\data\eval\graph_extraction_cache.json"
DEFAULT_REPORT = r"D:\AI\RAG_project\data\eval\graph_report.json"


def cmd_build(args: argparse.Namespace) -> int:
    from app.services.graph_ingest import build_graph

    with session_scope() as db:
        levels = tuple(x.strip() for x in (args.evidence or '').split(',') if x.strip())
        result = build_graph(
            db,
            limit=args.limit,
            evidence_levels=levels or None,
            max_per_document=args.max_per_doc,
            rebuild=args.rebuild,
            cache_path=args.cache,
            resume=not args.no_resume,
            progress_every=args.progress_every,
        )

    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(result, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        logger.info("结果已写入 %s", out)
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    """查看抽取缓存统计（不连 Neo4j）。"""
    from app.services.graph_ingest import graph_report

    r = graph_report(args.cache)

    logger.info("=" * 74)
    logger.info("图谱抽取统计（来自缓存，未访问 Neo4j）")
    logger.info("=" * 74)
    logger.info("  已抽取文本块    : %d", r["chunks_extracted"])
    logger.info("  实体提及总数    : %d", r["entity_mentions"])
    logger.info("  去重后实体数    : %d", r["unique_entities"])
    logger.info("  关系提及总数    : %d", r["relation_mentions"])

    logger.info("\n  实体类型分布（去重后）:")
    for t, n in r["by_type"].items():
        logger.info("    %-14s %4d", t, n)

    logger.info("\n  关系类型分布:")
    for t, n in r["by_relation"].items():
        logger.info("    %-24s %4d", t, n)

    logger.info("\n  出现频次最高的实体（前 25）:")
    for name, cnt in r["top_entities"]:
        logger.info("    %-28s %3d", name, cnt)

    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(r, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        logger.info("\n结果已写入 %s", out)
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    """查看 Neo4j 中的图谱规模。"""
    from app.services.graph_client import GraphClient

    with GraphClient() as g:
        if not g.is_available():
            logger.error("Neo4j 不可用，请确认服务已启动")
            return 1
        stats = g.stats()

    logger.info("=" * 74)
    logger.info("Neo4j 图谱规模")
    logger.info("=" * 74)
    logger.info("  节点总数: %d", stats.get("nodes", 0))
    logger.info("  关系总数: %d", stats.get("relations", 0))

    if stats.get("by_type"):
        logger.info("\n  节点类型分布:")
        for t, n in stats["by_type"].items():
            logger.info("    %-14s %4d", t, n)
    if stats.get("by_relation"):
        logger.info("\n  关系类型分布:")
        for t, n in stats["by_relation"].items():
            logger.info("    %-24s %4d", t, n)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m scripts.build_graph",
                                description="知识图谱构建工具")
    sub = p.add_subparsers(dest="command", required=True)

    b = sub.add_parser("build", help="构建图谱")
    b.add_argument("--limit", type=int, default=400, help="抽取文本块数上限")
    b.add_argument("--rebuild", action="store_true", help="清空后重建")
    b.add_argument("--evidence", default=None,
                   help="只抽取这些证据等级，逗号分隔（如 guideline,consensus）；缺省不限")
    b.add_argument("--max-per-doc", type=int, default=3,
                   help="每篇文献最多抽取的块数（扩图时放大，默认 3）")
    b.add_argument("--cache", default=DEFAULT_CACHE, help="抽取缓存路径")
    b.add_argument("--out", default=None, help="构建结果输出路径")
    b.add_argument("--no-resume", action="store_true", help="忽略缓存，重新抽取")
    b.add_argument("--progress-every", type=int, default=10)
    b.set_defaults(func=cmd_build)

    r = sub.add_parser("report", help="查看抽取缓存统计")
    r.add_argument("--cache", default=DEFAULT_CACHE)
    r.add_argument("--out", default=DEFAULT_REPORT)
    r.set_defaults(func=cmd_report)

    s = sub.add_parser("stats", help="查看 Neo4j 图谱规模")
    s.set_defaults(func=cmd_stats)

    return p


def main() -> int:
    setup_logging()
    args = build_parser().parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
