"""评测相关命令行工具。

用法:
    # 生成评测集（LLM 从文本块反向出题）
    python -m app.eval.cli generate --target 200

    # 查看评测集统计
    python -m app.eval.cli stats --path data/eval/qa_dataset.json

    # 运行检索指标评测
    python -m app.eval.cli retrieval --path data/eval/qa_dataset.json

    # 运行生成质量评测（LLM 裁判，较慢且消耗 token）
    python -m app.eval.cli generation --path data/eval/qa_dataset.json --sample 40
"""

from __future__ import annotations

from dataclasses import asdict

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.core.logging import get_logger, setup_logging
from app.db.session import session_scope
from app.eval.dataset import QUESTION_TYPE_ZH, EvalDataset

logger = get_logger("eval.cli")

DEFAULT_DATASET = r"D:\AI\RAG_project\data\eval\qa_dataset.json"


def cmd_generate(args: argparse.Namespace) -> int:
    """生成评测集。"""
    from app.eval.generator import generate_dataset

    logger.info("=" * 80)
    logger.info("生成评测集：目标 %d 题，每批 %d 个片段，语言 %s",
                args.target, args.batch_size, args.language)
    logger.info("=" * 80)

    with session_scope() as db:
        dataset = generate_dataset(
            db,
            target=args.target,
            batch_size=args.batch_size,
            seed=args.seed,
            out_path=args.path,
            language=args.language,
        )

    _print_distribution(dataset)
    return 0 if dataset.items else 1


def cmd_stats(args: argparse.Namespace) -> int:
    """查看评测集统计。"""
    dataset = EvalDataset.load(args.path)
    _print_distribution(dataset)

    logger.info("\n样本示例（前 5 条）:")
    for item in dataset.items[:5]:
        logger.info("  [%s] %s", item.qid, item.question)
        logger.info("        类型=%s 分期=%s 证据=%s",
                    QUESTION_TYPE_ZH.get(item.question_type, item.question_type),
                    item.phase, item.gold_evidence_level)
        logger.info("        来源=%s p.%s", item.gold_doc_title[:50], item.gold_page)
    return 0


def _print_distribution(dataset: EvalDataset) -> None:
    """打印评测集分布（论文需报告）。"""
    dist = dataset.distribution()
    logger.info("\n" + "=" * 80)
    logger.info("评测集统计")
    logger.info("=" * 80)
    logger.info("  名称      : %s (v%s)", dataset.name, dataset.version)
    logger.info("  题目数    : %d", dist["total"])
    logger.info("  语料规模  : %d 篇文献 / %d 文本块",
                dataset.corpus_documents, dataset.corpus_chunks)

    logger.info("\n  围术期分期分布:")
    for k, v in sorted(dist["by_phase"].items(), key=lambda kv: -kv[1]):
        logger.info("    %-8s %4d  (%.0f%%)", k, v, v / max(1, dist["total"]) * 100)

    logger.info("\n  问题类型分布:")
    for k, v in sorted(dist["by_question_type"].items(), key=lambda kv: -kv[1]):
        logger.info("    %-12s %4d  (%.0f%%)",
                    QUESTION_TYPE_ZH.get(k, k), v, v / max(1, dist["total"]) * 100)

    logger.info("\n  答案所在文献的证据等级分布:")
    for k, v in sorted(dist["by_evidence_level"].items(), key=lambda kv: -kv[1]):
        logger.info("    %-12s %4d  (%.0f%%)", k, v, v / max(1, dist["total"]) * 100)


def cmd_retrieval(args: argparse.Namespace) -> int:
    """运行检索指标评测（含消融实验）。"""
    from app.eval.runner import evaluate_retrieval

    dataset = EvalDataset.load(args.path)
    logger.info("=" * 92)
    logger.info("检索指标评测")
    logger.info("=" * 92)
    logger.info("评测集: %s | %d 题", dataset.name, len(dataset.items))
    logger.info("指标口径: Recall@K / MRR / NDCG@K（单答案标注，gold chunk 精确匹配）")
    logger.info("=" * 92)

    # 消融配置：(标签, recall_mode, rerank, evidence_boost, keyword_weight)
    # keyword_weight 为 None 时用配置默认值
    if args.ablation:
        configs = [
            ("① 仅向量", "vector", False, False, None),
            ("② 仅关键词", "keyword", False, False, None),
            ("③ 多路融合(w=0.7)", "hybrid", False, False, 0.7),
            ("④ 多路融合(w=0.3)", "hybrid", False, False, 0.3),
            ("⑤ 融合(w=0) +重排", "hybrid", True, False, 0.0),
            ("⑥ 融合(w=0.3) +重排", "hybrid", True, False, 0.3),
            ("⑦ ⑥+证据加权", "hybrid", True, True, 0.3),
        ]
    else:
        configs = [
            (f"配置({args.recall_mode})", args.recall_mode,
             not args.no_rerank, not args.no_evidence_boost, None),
        ]

    results = []
    with session_scope() as db:
        for label, mode, rerank, boost, kw_weight in configs:
            logger.info("\n>>> %s", label)
            m = evaluate_retrieval(
                db, dataset, label,
                recall_mode=mode, use_rerank=rerank,
                use_evidence_boost=boost,
                recall_limit=args.recall_limit, rrf_limit=args.rrf_limit,
                keyword_weight=kw_weight,
            )
            results.append(m)
            logger.info(m.report())

    # 汇总表（论文可直接使用）
    logger.info("\n" + "=" * 92)
    logger.info("检索指标汇总表")
    logger.info("=" * 92)
    logger.info(
        "%-22s %7s %7s %7s %8s %8s %8s %9s",
        "配置", "R@1", "R@3", "R@5", "R@10", "MRR", "NDCG@5", "延迟ms",
    )
    logger.info("-" * 92)
    for m in results:
        logger.info(
            "%-22s %7.3f %7.3f %7.3f %8.3f %8.3f %8.3f %9.0f",
            m.label, m.recall_at(1), m.recall_at(3), m.recall_at(5),
            m.recall_at(10), m.mrr, m.ndcg_at(5), m.avg_latency_ms,
        )
    logger.info("-" * 92)

    if len(results) >= 2:
        base = results[0]
        logger.info("\n相对「%s」的提升:", base.label)
        for m in results[1:]:
            logger.info(
                "  %-22s R@5 %+6.1f 个百分点   MRR %+6.1f 个百分点   NDCG@5 %+6.1f 个百分点",
                m.label,
                (m.recall_at(5) - base.recall_at(5)) * 100,
                (m.mrr - base.mrr) * 100,
                (m.ndcg_at(5) - base.ndcg_at(5)) * 100,
            )

    # 未命中分析
    logger.info("\n未命中情况（配置 ① 前 10 条均不含 gold chunk 的样本数）: %d / %d",
                results[0].miss_count, results[0].total)

    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(
                {
                    "dataset": dataset.name,
                    "total": len(dataset.items),
                    "ablation": args.ablation,
                    "runs": [m.to_dict() for m in results],
                },
                ensure_ascii=False, indent=2,
            ),
            encoding="utf-8",
        )
        logger.info("\n结果已写入 %s", out)

    return 0


def cmd_generation(args: argparse.Namespace) -> int:
    """运行生成质量评测（LLM 裁判）。"""
    from app.eval.runner import collect_answers, judge_answers

    dataset = EvalDataset.load(args.path)
    sample = args.sample if args.sample > 0 else len(dataset.items)

    logger.info("=" * 92)
    logger.info("生成质量评测（LLM 裁判）")
    logger.info("=" * 92)
    logger.info("评测集: %s | 抽样 %d / %d 题", dataset.name, sample, len(dataset.items))
    logger.info("指标: 忠实度 / 相关性 / 引用准确率 / 幻觉率")
    logger.info("=" * 92)

    with session_scope() as db:
        logger.info("\n[阶段 1/2] 收集答案（调用完整问答链路）…")
        records = collect_answers(db, dataset, sample=sample, top_k=args.top_k)

    logger.info("\n[阶段 2/2] LLM 裁判评分…")
    metrics = judge_answers(records, label="完整系统", config={"top_k": args.top_k})

    logger.info("\n" + "=" * 92)
    logger.info("生成质量结果")
    logger.info("=" * 92)
    logger.info(metrics.report())
    logger.info(metrics.numeric_report())
    logger.info("\n  有效评判: %d / %d（失败 %d）",
                len(metrics.valid), metrics.total,
                metrics.total - len(metrics.valid))
    logger.info("  平均引用数: %.2f | 平均答案长度: %.0f 字符",
                metrics.avg_citations, metrics.avg_answer_chars)

    if metrics.numeric_error_rate > 0:
        logger.info("\n  ⚠ 数值核验发现 %d 处无据数值，涉及 %d 条答案。",
                    metrics.total_numeric_unsupported,
                    metrics.answers_with_bad_numbers)
        logger.info("    这是**独立于 LLM 裁判**的结构化检查结果，"
                    "提示词约束无法阻止此类错误，故需运行期显式提示用户。")

    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(
                {
                    "dataset": dataset.name,
                    "sample": sample,
                    "metrics": metrics.to_dict(),
                    "scores": [asdict(s) for s in metrics.scores],
                    # 注意：**必须保留 evidences**，不能为了瘦身而丢弃。
                    # 数值主张核验需要完整证据正文作为参照物，
                    # 若缺失会导致所有数值被判为「无据」（实测误报率 100%）。
                    # 体积代价可接受（该文件不进版本库，仅本地分析用）。
                    "records": records,
                },
                ensure_ascii=False, indent=2,
            ),
            encoding="utf-8",
        )
        logger.info("\n结果已写入 %s", out)

    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.eval.cli", description="评测工具"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_gen = sub.add_parser("generate", help="生成评测集")
    p_gen.add_argument("--target", type=int, default=200, help="目标题目数")
    p_gen.add_argument("--batch-size", type=int, default=4, help="每批片段数")
    p_gen.add_argument("--seed", type=int, default=42, help="随机种子")
    p_gen.add_argument(
        "--language", default="zh", choices=["zh", "en"],
        help="出题语言。zh=中文口语提问（跨语言场景）；en=英文提问（同语言场景，"
             "用于公平评估关键词检索）",
    )
    p_gen.add_argument("--path", default=DEFAULT_DATASET, help="输出路径")
    p_gen.set_defaults(func=cmd_generate)

    p_stats = sub.add_parser("stats", help="查看评测集统计")
    p_stats.add_argument("--path", default=DEFAULT_DATASET)
    p_stats.set_defaults(func=cmd_stats)

    p_ret = sub.add_parser("retrieval", help="检索指标评测")
    p_ret.add_argument("--path", default=DEFAULT_DATASET)
    p_ret.add_argument("--ablation", action="store_true", help="运行完整消融实验")
    p_ret.add_argument("--recall-mode", default="hybrid",
                       choices=["hybrid", "vector", "keyword"])
    p_ret.add_argument("--no-rerank", action="store_true")
    p_ret.add_argument("--no-evidence-boost", action="store_true")
    p_ret.add_argument("--recall-limit", type=int, default=50)
    p_ret.add_argument("--rrf-limit", type=int, default=80)
    p_ret.add_argument("--out", default=None, help="结果 JSON 输出路径")
    p_ret.set_defaults(func=cmd_retrieval)

    p_genq = sub.add_parser("generation", help="生成质量评测（LLM 裁判）")
    p_genq.add_argument("--path", default=DEFAULT_DATASET)
    p_genq.add_argument("--sample", type=int, default=40, help="抽样题数，0 表示全部")
    p_genq.add_argument("--top-k", type=int, default=5, help="送入 LLM 的证据条数")
    p_genq.add_argument("--out", default=None, help="结果 JSON 输出路径")
    p_genq.set_defaults(func=cmd_generation)

    return parser


def main() -> int:
    setup_logging()
    args = build_parser().parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
