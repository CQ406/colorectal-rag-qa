"""文献采集入库命令行工具。

用法示例:
    # 载入报审表的 15 篇种子文献（中文直接入库，英文尝试从 PubMed 补充）
    python -m app.etl.cli seed

    # 仅载入，不生成向量（快速，稍后用 vectorize 补）
    python -m app.etl.cli seed --no-embed

    # 从 PubMed 检索并入库
    python -m app.etl.cli search --query "colorectal surgery AND anastomotic leak" --limit 50

    # 为尚未向量化的文本块补向量（断点续跑）
    python -m app.etl.cli vectorize

    # 查看知识库统计
    python -m app.etl.cli stats

    # 评估自动标注准确率（对比种子清单的预期值）
    python -m app.etl.cli evaluate
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

# 允许以脚本方式运行
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from sqlalchemy import func, select

from app.core.constants import EvidenceLevel, PerioperativePhase
from app.core.logging import get_logger, setup_logging
from app.db.session import session_scope
from app.etl import ingest as ingest_mod
from app.etl.pubmed_client import AbstractSection, ParsedArticle, PubMedClient
from app.etl.seed_references import SEED_REFERENCES
from app.models.knowledge import Chunk, Document

logger = get_logger("etl.cli")


# ======================================================================
#  种子文献
# ======================================================================
def cmd_seed(args: argparse.Namespace) -> int:
    """载入报审表的 15 篇种子文献。"""
    logger.info("载入种子文献，共 %d 篇", len(SEED_REFERENCES))

    articles: list[ParsedArticle] = []
    pubmed_lookup_ok = 0
    fallback_used = 0

    def build_manual(ref) -> ParsedArticle:
        """把种子清单条目转为 ParsedArticle（PubMed 未命中时的退路）。

        来源标记为 manual 而非 pubmed，保证溯源信息真实。
        """
        sections: list[AbstractSection] = []
        if ref.abstract:
            sections.append(AbstractSection(label="", text=ref.abstract))
        return ParsedArticle(
            pmid=None,
            title=ref.title,
            abstract_sections=sections,
            authors=ref.authors.split(", ") if ref.authors else [],
            journal=ref.journal,
            year=ref.year,
            language="chi" if ref.language == "zh" else "eng",
            source="manual",
        )

    with PubMedClient() as client:
        for ref in SEED_REFERENCES:
            # 中文文献 PubMed 不收录，直接手工录入
            is_zh = ref.language == "zh"
            # --pubmed-all: 对英文文献一律尝试 PubMed（即使清单标为 manual），
            # 目的是补全 publication_types —— 这是证据等级判定最可靠的依据。
            should_lookup = (
                not is_zh
                and not args.no_pubmed
                and (ref.source == "pubmed" or args.pubmed_all)
            )

            if not should_lookup:
                articles.append(build_manual(ref))
                logger.info("  [%2d] 手工录入: %s", ref.index, ref.title[:52])
                continue

            logger.info("  [%2d] 查询 PubMed: %s", ref.index, ref.title[:48])
            try:
                article = client.fetch_by_title(ref.title)
            except Exception as exc:  # noqa: BLE001
                logger.warning("      查询失败: %s", exc)
                article = None

            if article:
                articles.append(article)
                pubmed_lookup_ok += 1
                logger.info(
                    "      命中 PMID=%s (%s, %s) 摘要 %d 字符 类型=%s",
                    article.pmid, article.journal, article.year,
                    len(article.abstract_text),
                    ",".join(article.publication_types[:2]) or "-",
                )
            else:
                # 未命中不丢弃：用清单中的信息兜底入库，保证覆盖率
                logger.info("      PubMed 未命中，使用清单信息兜底")
                articles.append(build_manual(ref))
                fallback_used += 1

    if not articles:
        logger.error("没有可入库的文献")
        return 1

    logger.info(
        "\n待入库 %d 篇（PubMed 命中 %d，清单兜底 %d）",
        len(articles), pubmed_lookup_ok, fallback_used,
    )

    with session_scope() as db:
        stats = ingest_mod.ingest_articles(
            db, articles, embed=not args.no_embed, batch_size=args.batch_size
        )
    logger.info("\n入库完成: %s", stats.summary())
    if stats.errors:
        logger.warning("失败 %d 篇:", len(stats.errors))
        for e in stats.errors[:10]:
            logger.warning("  %s", e)

    evaluate_annotations()
    return 0


def evaluate_annotations() -> None:
    """对比种子清单的预期标注与系统自动标注，输出准确率。

    这是论文实验素材：自动标注的准确率需可量化。
    """
    expected = {
        ref.title: (ref.expected_phase, ref.expected_evidence)
        for ref in SEED_REFERENCES
    }
    if not expected:
        return

    logger.info("\n" + "=" * 70)
    logger.info("自动标注准确率评估（对比种子清单预期值）")
    logger.info("=" * 70)

    phase_ok = phase_total = 0
    evid_ok = evid_total = 0

    with session_scope() as db:
        docs = db.execute(select(Document)).scalars().all()
        for doc in docs:
            if doc.title not in expected:
                continue
            exp_phase, exp_evid = expected[doc.title]

            p_match = doc.phase == exp_phase
            e_match = doc.evidence_level == exp_evid
            phase_total += 1
            evid_total += 1
            phase_ok += int(p_match)
            evid_ok += int(e_match)

            logger.info(
                "  %s | 分期 %s(期望%s) | 证据 %s(期望%s)",
                "OK " if (p_match and e_match) else "差异",
                doc.phase, exp_phase,
                doc.evidence_level, exp_evid,
            )

    if phase_total:
        logger.info("\n  分期标注准确率:   %d/%d = %.0f%%",
                    phase_ok, phase_total, phase_ok / phase_total * 100)
        logger.info("  证据等级准确率:   %d/%d = %.0f%%",
                    evid_ok, evid_total, evid_ok / evid_total * 100)
        logger.info(
            "\n  说明：规则法对边界案例会误判，故保留 needs_review 标记供人工复核。"
        )


# ======================================================================
#  PubMed 检索
# ======================================================================
def cmd_search(args: argparse.Namespace) -> int:
    """从 PubMed 检索并入库。"""
    logger.info("PubMed 检索: %s", args.query)
    logger.info("最多 %d 条，排序: %s", args.limit, args.sort)

    with PubMedClient() as client:
        t0 = time.perf_counter()
        pmids = client.search(
            args.query,
            max_results=args.limit,
            sort=args.sort,
            mindate=args.mindate,
        )
        if not pmids:
            logger.warning("检索无结果")
            return 1
        logger.info("获得 %d 条 PMID，耗时 %.1f 秒", len(pmids), time.perf_counter() - t0)

        articles = client.fetch(pmids)

    usable = [a for a in articles if a.is_usable]
    logger.info("解析 %d 篇，其中有可用摘要 %d 篇", len(articles), len(usable))

    if not usable:
        logger.error("没有可用文献")
        return 1

    with session_scope() as db:
        stats = ingest_mod.ingest_articles(
            db, usable, embed=not args.no_embed, batch_size=args.batch_size
        )
    logger.info("\n入库完成: %s", stats.summary())
    if stats.errors:
        logger.warning("失败 %d 篇:", len(stats.errors))
        for e in stats.errors[:10]:
            logger.warning("  %s", e)
    return 0


# ======================================================================
#  向量化补全
# ======================================================================
def cmd_vectorize(args: argparse.Namespace) -> int:
    """为尚未向量化的文本块补向量。"""
    with session_scope() as db:
        pending = db.execute(
            select(func.count()).select_from(Chunk).where(Chunk.embedding.is_(None))
        ).scalar_one()
        if pending == 0:
            logger.info("所有文本块均已向量化，无需补全")
            return 0
        logger.info("待向量化 %d 条", pending)

        total = 0
        total_seconds = 0.0
        while True:
            count, elapsed = ingest_mod.vectorize_missing(
                db, limit=args.batch, batch_size=args.batch_size
            )
            if count == 0:
                break
            total += count
            total_seconds += elapsed
            logger.info("  进度: 累计 %d 条", total)
            if args.once:
                break

    logger.info(
        "\n向量化完成: %d 条，耗时 %.1f 秒 (%.0f 条/秒)",
        total, total_seconds, total / total_seconds if total_seconds else 0,
    )
    return 0


# ======================================================================
#  统计
# ======================================================================
def cmd_stats(args: argparse.Namespace) -> int:
    """输出知识库统计信息。"""
    from app.repositories import search_repo

    with session_scope() as db:
        docs = search_repo.count_active_documents(db)
        total_chunks = search_repo.count_chunks(db)
        embedded = search_repo.count_embedded_chunks(db)
        phase_dist = search_repo.phase_distribution(db)
        evid_dist = search_repo.evidence_distribution(db)

        logger.info("=" * 70)
        logger.info("知识库统计")
        logger.info("=" * 70)
        logger.info("  文献数:     %d", docs)
        logger.info("  文本块:     %d（已向量化 %d，未完成 %d）",
                    total_chunks, embedded, total_chunks - embedded)

        logger.info("\n  围术期分期分布:")
        for phase in PerioperativePhase:
            cnt = phase_dist.get(phase.value, 0)
            bar = "█" * min(40, cnt // max(1, total_chunks // 40)) if total_chunks else ""
            logger.info("    %-8s %-6s %4d  %s", phase.value, phase.zh, cnt, bar)

        logger.info("\n  证据等级分布:")
        for level in sorted(EvidenceLevel, key=lambda e: -e.weight):
            cnt = evid_dist.get(level.value, 0)
            if cnt:
                logger.info("    %-12s %-16s %4d", level.value, level.zh, cnt)

        # 向量化进度
        if total_chunks and embedded < total_chunks:
            logger.info("\n  提示: 还有 %d 条未向量化，执行 vectorize 补全",
                        total_chunks - embedded)
    return 0


# ======================================================================
#  批量采集
# ======================================================================
def cmd_manual(args: argparse.Namespace) -> int:
    """从 Markdown、DOCX、PDF 目录导入人工整理的指南/共识。

    与 collect 的区别：collect 从 PubMed 采集（只有英文题录与摘要），
    manual 导入**人工整理的中文正文**。这是系统获取中文语料的唯一可靠途径 ——
    实测 PubMed 对 200 篇 language=chi 文献的响应中汉字数为 0。

    可反复执行：按标题归一化去重（见 ingest._find_existing），
    修改某一章后重新导入会更新该文献而非新建重复记录。
    """
    import re as _re
    from pathlib import Path

    from app.etl import manual_import

    default_dir = Path(__file__).resolve().parents[3] / "data" / "manual"
    directory = Path(args.dir) if args.dir else default_dir

    logger.info("=" * 72)
    logger.info("人工录入导入: %s", directory)
    logger.info("=" * 72)

    entries, errors = manual_import.load_directory(directory)

    if errors:
        logger.warning("解析失败 %d 个文件:", len(errors))
        for e in errors:
            logger.warning("  %s", e)

    if not entries:
        logger.error("没有可导入的条目。请检查目录与文件格式（见目录下 README.md）")
        return 1

    info = manual_import.stats(entries)
    logger.info(
        "共 %d 篇，%d 个章节，%d 字",
        info["documents"], info["sections"], info["characters"],
    )
    for d in info["documents_detail"]:
        logger.info(
            "  %-8s %5d 字  %2d 章  %s",
            d["year"] or "—", d["characters"], d["sections"], d["title"][:44],
        )

    if args.dry_run:
        logger.info("\n[预览模式] 未写入数据库")
        return 0

    articles = [manual_import.to_parsed_article(e) for e in entries]
    with session_scope() as db:
        st = ingest_mod.ingest_articles(
            db, articles, embed=not args.no_embed, batch_size=args.batch_size
        )
    logger.info("\n" + "=" * 72)
    logger.info("导入完成: %s", st.summary())
    if st.errors:
        logger.warning("失败 %d 条（前 5 条）:", len(st.errors))
        for e in st.errors[:5]:
            logger.warning("  %s", e)
    if args.no_embed:
        logger.info("提示：未生成向量，请执行 python -m app.etl.cli vectorize 后才能被检索")
    return 0


def cmd_collect(args: argparse.Namespace) -> int:
    """按预定义检索式批量采集 PubMed 文献。

    支持三组检索式（--mode）：
      default     主检索式 9 条，覆盖术前/术中/术后/通用
      supplement  补充检索式 17 条，针对中文文献与近三年高证据
      all         两者合并共 26 条

    补充检索式的必要性见 `collect_queries_supplement` 模块文档：
    主检索式无时间与语言过滤，导致初始语料中文仅占 3.8%、近三年仅占 11.0%。
    """
    from app.etl.collect_queries import COLLECT_QUERIES
    from app.etl.collect_queries_supplement import CN_QUERIES, RECENT_QUERIES

    mode = getattr(args, "mode", "default")
    if mode == "supplement":
        queries = list(CN_QUERIES) + list(RECENT_QUERIES)
        logger.info("检索式组: 补充检索式（中文文献 + 近三年高证据）")
    elif mode == "all":
        queries = list(COLLECT_QUERIES) + list(CN_QUERIES) + list(RECENT_QUERIES)
        logger.info("检索式组: 主检索式 + 补充检索式")
    else:
        queries = list(COLLECT_QUERIES)
        logger.info("检索式组: 主检索式")

    logger.info("=" * 72)
    logger.info(
        "批量采集计划: %d 条检索式，上限合计 %d 篇",
        len(queries), sum(q.limit for q in queries),
    )
    for q in queries:
        logger.info("  [%-6s] %-22s 上限 %3d  %s", q.phase, q.name, q.limit, ",".join(q.tags))
    logger.info("=" * 72)

    if args.dry_run:
        logger.info("\n[预览模式] 未发起任何请求")
        return 0

    # 步骤 1：逐条检索并收集题录，按 PMID 全局去重
    collected: dict[str, ParsedArticle] = {}
    seen_pmids: set[str] = set()

    with PubMedClient() as client:
        for q in COLLECT_QUERIES:
            logger.info("\n--- 检索: %s (%s) ---", q.name, q.phase)
            try:
                pmids = client.search(
                    q.query, max_results=q.limit, mindate=q.mindate or args.mindate
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("检索失败，跳过: %s", exc)
                continue

            # 跨检索式去重，避免同一篇文献重复入库
            fresh = [p for p in pmids if p not in seen_pmids]
            dup = len(pmids) - len(fresh)
            if dup:
                logger.info("  %d 条与之前检索重复，去重后剩 %d 条", dup, len(fresh))
            if not fresh:
                continue

            seen_pmids.update(fresh)
            try:
                articles = client.fetch(fresh)
            except Exception as exc:  # noqa: BLE001
                logger.warning("拉取题录失败，跳过: %s", exc)
                continue

            for a in articles:
                if a.is_usable:
                    collected[a.pmid or a.title[:40]] = a
            logger.info("  累计可用文献 %d 篇", len(collected))

            if args.max_total and len(collected) >= args.max_total:
                logger.info("已达总量上限 %d，停止检索", args.max_total)
                break

    if not collected:
        logger.error("未采集到可用文献")
        return 1

    articles = list(collected.values())
    if args.max_total:
        articles = articles[: args.max_total]
    logger.info("\n共采集 %d 篇可用文献（含摘要）", len(articles))

    # 步骤 2：分批入库，每批单独提交，便于中断后重跑
    total_stats = ingest_mod.IngestStats()
    batch = args.commit_batch
    for i in range(0, len(articles), batch):
        chunk_articles = articles[i : i + batch]
        logger.info(
            "\n入库第 %d-%d / %d 篇...", i + 1, i + len(chunk_articles), len(articles)
        )
        with session_scope() as db:
            stats = ingest_mod.ingest_articles(
                db, chunk_articles, embed=not args.no_embed, batch_size=args.batch_size
            )
        logger.info("  %s", stats.summary())
        total_stats.documents_created += stats.documents_created
        total_stats.documents_updated += stats.documents_updated
        total_stats.documents_skipped += stats.documents_skipped
        total_stats.chunks_created += stats.chunks_created
        total_stats.chunks_embedded += stats.chunks_embedded
        total_stats.embed_seconds += stats.embed_seconds
        total_stats.errors.extend(stats.errors)

    logger.info("\n" + "=" * 72)
    logger.info("采集完成汇总: %s", total_stats.summary())
    if total_stats.errors:
        logger.warning("失败 %d 篇（前 5 条）:", len(total_stats.errors))
        for e in total_stats.errors[:5]:
            logger.warning("  %s", e)
    logger.info("=" * 72)

    cmd_stats(args)
    return 0


# ======================================================================
#  入口
# ======================================================================
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.etl.cli",
        description="文献采集与入库工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_seed = sub.add_parser("seed", help="载入报审表的 15 篇种子文献")
    p_seed.add_argument("--no-embed", action="store_true", help="不入向量，仅入库")
    p_seed.add_argument("--no-pubmed", action="store_true", help="不查询 PubMed，全部手工录入")
    p_seed.add_argument(
        "--pubmed-all", action="store_true",
        help="对所有英文文献都尝试 PubMed（补全 publication_types，提升证据等级准确率）",
    )
    p_seed.add_argument("--batch-size", type=int, default=32, help="向量化批大小")
    p_seed.set_defaults(func=cmd_seed)

    p_search = sub.add_parser("search", help="从 PubMed 检索并入库")
    p_search.add_argument("--query", required=True, help="PubMed 查询式")
    p_search.add_argument("--limit", type=int, default=50, help="最多条数")
    p_search.add_argument("--sort", default="relevance", choices=["relevance", "pub_date"])
    p_search.add_argument("--mindate", default=None, help="起始日期 YYYY/MM/DD")
    p_search.add_argument("--no-embed", action="store_true", help="不入向量")
    p_search.add_argument("--batch-size", type=int, default=32)
    p_search.set_defaults(func=cmd_search)

    p_vec = sub.add_parser("vectorize", help="为未向量化的文本块补向量")
    p_vec.add_argument("--batch", type=int, default=500, help="每批条数")
    p_vec.add_argument("--batch-size", type=int, default=32, help="GPU 编码批大小")
    p_vec.add_argument("--once", action="store_true", help="只跑一批")
    p_vec.set_defaults(func=cmd_vectorize)

    p_manual = sub.add_parser(
        "manual",
        help="人工录入指南/共识（从 Markdown/DOCX/PDF 目录导入，可反复执行）",
    )
    p_manual.add_argument(
        "--dir", default=None,
        help="录入文件目录，默认 data/manual",
    )
    p_manual.add_argument(
        "--dry-run", action="store_true", help="只解析并预览，不入库",
    )
    p_manual.add_argument(
        "--no-embed", action="store_true", help="不入向量，稍后用 vectorize 补",
    )
    p_manual.add_argument(
        "--batch-size", type=int, default=32, help="向量化批大小",
    )
    p_manual.set_defaults(func=cmd_manual)

    p_stats = sub.add_parser("stats", help="查看知识库统计")
    p_stats.set_defaults(func=cmd_stats)

    p_collect = sub.add_parser(
        "collect",
        help="按预定义检索式批量采集 PubMed 文献"
             "（--mode supplement 采中文文献与近三年高证据）",
    )
    p_collect.add_argument(
        "--mode", default="default", choices=["default", "supplement", "all"],
        help="检索式组：default=主检索式9条 / supplement=补充检索式17条"
             "（中文文献+近三年高证据）/ all=两者合并26条",
    )
    p_collect.add_argument("--dry-run", action="store_true", help="只显示采集计划")
    p_collect.add_argument("--max-total", type=int, default=None, help="采集总量上限")
    p_collect.add_argument("--mindate", default=None, help="起始日期 YYYY/MM/DD")
    p_collect.add_argument("--no-embed", action="store_true", help="不入向量")
    p_collect.add_argument("--batch-size", type=int, default=32, help="向量化批大小")
    p_collect.add_argument(
        "--commit-batch", type=int, default=50,
        help="每入库多少篇提交一次（便于中断后重跑）",
    )
    p_collect.set_defaults(func=cmd_collect)

    return parser


def main() -> int:
    setup_logging()
    args = build_parser().parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
