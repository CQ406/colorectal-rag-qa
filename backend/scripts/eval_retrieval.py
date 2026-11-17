"""检索质量评测与消融实验脚本（阶段三升级版）。

评测方法
--------
采用「以文献自身作为查询」的自动评测（无需人工标注即可快速迭代）：

  1. **标题反查**：用文献标题作为查询，检查该文献自身是否被召回
  2. **摘要片段反查**：从文献摘要中截取片段作为查询，更接近真实使用场景

消融实验设计（论文实验章节核心）
--------------------------------
按以下配置逐项对比，量化每个组件的贡献：

  ① 仅向量（vector）
  ② 仅关键词（keyword）
  ③ 多路融合（hybrid）              —— 验证 RRF 融合的收益
  ④ 多路融合 + 重排                  —— 验证交叉编码器重排的收益
  ⑤ 多路融合 + 重排 + 证据等级加权    —— 验证领域知识注入的收益

指标：
  Recall@K   前 K 条结果中包含正确来源的比例
  MRR        首个正确结果的倒数排名均值
  延迟        平均与 P95（毫秒）

用法:
    # 单项评测
    python -m scripts.eval_retrieval --sample 60 --recall-mode hybrid

    # 完整消融实验（会输出论文用表格）
    python -m scripts.eval_retrieval --sample 60 --ablation

    # 关闭重排（快速验证）
    python -m scripts.eval_retrieval --sample 60 --no-rerank
"""

from __future__ import annotations

import argparse
import json
import random
import statistics
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import func, select

from app.core.config import settings
from app.core.logging import get_logger, setup_logging
from app.db.session import session_scope
from app.models.knowledge import Chunk, Document
from app.schemas.common import SearchRequest
from app.services import hybrid_search

logger = get_logger("scripts.eval_retrieval")


@dataclass
class EvalCase:
    """一条评测样本。"""

    query: str
    expected_document_id: str
    expected_title: str
    mode: str
    found_rank: int | None = None

    @property
    def reciprocal_rank(self) -> float:
        return 1.0 / self.found_rank if self.found_rank else 0.0


@dataclass
class EvalResult:
    """一组评测结果。"""

    label: str
    total: int = 0
    hits: dict[int, int] = field(default_factory=dict)
    hit_at_1: int = 0
    mrr_sum: float = 0.0
    latencies: list[float] = field(default_factory=list)
    config: dict = field(default_factory=dict)

    def accuracy_at(self, k: int) -> float:
        return self.hits.get(k, 0) / self.total if self.total else 0.0

    @property
    def mrr(self) -> float:
        return self.mrr_sum / self.total if self.total else 0.0

    @property
    def avg_latency_ms(self) -> float:
        return statistics.mean(self.latencies) if self.latencies else 0.0

    @property
    def p95_latency_ms(self) -> float:
        if len(self.latencies) < 2:
            return self.avg_latency_ms
        return statistics.quantiles(self.latencies, n=20)[18]

    def to_dict(self) -> dict:
        return {
            "label": self.label,
            "config": self.config,
            "total": self.total,
            "recall@1": round(self.accuracy_at(1), 4),
            "recall@3": round(self.accuracy_at(3), 4),
            "recall@5": round(self.accuracy_at(5), 4),
            "recall@10": round(self.accuracy_at(10), 4),
            "mrr": round(self.mrr, 4),
            "avg_latency_ms": round(self.avg_latency_ms, 1),
            "p95_latency_ms": round(self.p95_latency_ms, 1),
        }


def build_cases(sample: int, mode: str, phase: str | None, seed: int = 42) -> list[EvalCase]:
    """从知识库构造评测样本。"""
    with session_scope() as db:
        stmt = select(Document).where(
            Document.is_active.is_(True), Document.abstract.isnot(None)
        )
        if phase:
            stmt = stmt.where(Document.phase == phase)
        docs = list(db.execute(stmt).scalars().all())

    docs = [d for d in docs if d.title and len(d.title) > 20 and d.abstract and len(d.abstract) > 200]
    random.Random(seed).shuffle(docs)
    docs = docs[:sample]

    cases: list[EvalCase] = []
    for doc in docs:
        if mode == "title":
            query = doc.title
        else:
            abstract = doc.abstract
            mid = len(abstract) // 3
            snippet = abstract[mid : mid + 120].strip()
            if len(snippet) < 40:
                snippet = abstract[:120]
            query = snippet
        cases.append(
            EvalCase(
                query=query,
                expected_document_id=str(doc.id),
                expected_title=doc.title,
                mode=mode,
            )
        )
    return cases


def evaluate(
    cases: list[EvalCase],
    label: str,
    *,
    recall_mode: str = "hybrid",
    use_rerank: bool = True,
    use_graph: bool = False,
    use_evidence_boost: bool = True,
    recall_limit: int = 50,
    rrf_limit: int = 80,
    top_k: int = 10,
    phase: str | None = None,
    min_keyword_overlap: float | None = None,
) -> EvalResult:
    """按给定配置执行一组评测。"""
    config = {
        "recall_mode": recall_mode,
        "rerank": use_rerank,
        "use_graph": use_graph,
        "evidence_boost": use_evidence_boost,
        "recall_limit": recall_limit,
        "rrf_limit": rrf_limit,
        "min_keyword_overlap": min_keyword_overlap,
    }
    result = EvalResult(label=label, total=len(cases), config=config)
    ks = [1, 3, 5, 10]

    # 临时覆盖配置（消融实验用），结束后恢复
    original_overlap = settings.retrieval.min_keyword_overlap
    if min_keyword_overlap is not None:
        settings.retrieval.min_keyword_overlap = min_keyword_overlap

    try:
        with session_scope() as db:
            for i, case in enumerate(cases, 1):
                case.found_rank = None
                req = SearchRequest(
                    query=case.query,
                    top_k=top_k,
                    use_rerank=use_rerank,
                    use_graph=use_graph,
                    use_evidence_boost=use_evidence_boost,
                    recall_mode=recall_mode,
                    recall_limit=recall_limit,
                    rrf_limit=rrf_limit,
                    phase=phase,
                    max_per_document=0,   # 评测时不限制单文献块数，避免干扰召回评估
                )
                t0 = time.perf_counter()
                resp = hybrid_search.search(db, req)
                result.latencies.append((time.perf_counter() - t0) * 1000)

                rank = None
                for item in resp.results:
                    if str(item.document_id) == case.expected_document_id:
                        rank = item.rank
                        break
                case.found_rank = rank

                if rank is not None:
                    result.mrr_sum += 1.0 / rank
                    if rank == 1:
                        result.hit_at_1 += 1
                    for k in ks:
                        if rank <= k:
                            result.hits[k] = result.hits.get(k, 0) + 1

                if i % 20 == 0:
                    logger.info("    %s 进度 %d/%d (R@5=%.3f)", label, i, len(cases), result.accuracy_at(5))
    finally:
        settings.retrieval.min_keyword_overlap = original_overlap

    return result


def run_ablation(
    sample: int, phase: str | None, seed: int, recall_limit: int, rrf_limit: int
) -> list[tuple[str, EvalResult]]:
    """执行完整消融实验。

    配置设计（逐项隔离单个组件的贡献）：
      ① 仅向量                      —— 基线
      ② 仅关键词                    —— 关键词路的独立能力
      ③ 多路融合(RRF)               —— 融合的收益/代价
      ④ +重排                       —— 重排的收益
      ⑤ +重排+证据加权               —— 领域知识注入的收益
      ⑥ 多路融合(不过滤关键词)        —— 验证重叠度过滤的必要性
    """
    # 配置列表：(标签, recall_mode, rerank, evidence_boost, min_overlap)
    configs = [
        ("① 仅向量", "vector", False, False, None),
        ("② 仅关键词", "keyword", False, False, None),
        ("③ 多路融合(RRF)", "hybrid", False, False, None),
        ("④ +重排", "hybrid", True, False, None),
        ("⑤ +重排+证据加权", "hybrid", True, True, None),
        ("⑥ 融合-不过滤关键词", "hybrid", True, True, 0.0),
    ]

    output: list[tuple[str, EvalResult]] = []

    for mode_name, mode_cn in [("title", "标题反查"), ("abstract", "摘要片段反查")]:
        cases = build_cases(sample, mode_name, phase, seed)
        if not cases:
            continue
        logger.info("\n" + "#" * 78)
        logger.info("# 消融实验 — %s（%d 条样本）", mode_cn, len(cases))
        logger.info("#" * 78)
        for label, rmode, rerank, boost, overlap in configs:
            full_label = f"{mode_cn} {label}"
            logger.info("\n>>> %s", full_label)
            res = evaluate(
                cases, full_label,
                recall_mode=rmode, use_rerank=rerank, use_evidence_boost=boost,
                recall_limit=recall_limit, rrf_limit=rrf_limit, phase=phase,
                min_keyword_overlap=overlap,
            )
            output.append((mode_name, res))

    return output


def print_ablation_table(results: list[tuple[str, EvalResult]]) -> None:
    """输出论文可直接使用的消融实验表格。"""
    for mode_name, mode_cn in [("title", "标题反查"), ("abstract", "摘要片段反查")]:
        rows = [r for m, r in results if m == mode_name]
        if not rows:
            continue
        logger.info("\n" + "=" * 96)
        logger.info("消融实验表 — %s", mode_cn)
        logger.info("=" * 96)
        header = f"{'配置':<20} {'R@1':>7} {'R@3':>7} {'R@5':>7} {'R@10':>7} {'MRR':>7} {'延迟ms':>9}"
        logger.info(header)
        logger.info("-" * 96)
        for r in rows:
            logger.info(
                "%-20s %7.3f %7.3f %7.3f %7.3f %7.3f %9.0f",
                r.label.replace("标题反查 ", "").replace("摘要片段反查 ", ""),
                r.accuracy_at(1), r.accuracy_at(3), r.accuracy_at(5),
                r.accuracy_at(10), r.mrr, r.avg_latency_ms,
            )
        logger.info("-" * 96)

        # 计算相对基线（仅向量）的提升
        if len(rows) >= 2:
            base = rows[0]
            logger.info("\n相对「① 仅向量」的提升:")
            for r in rows[1:]:
                dr5 = (r.accuracy_at(5) - base.accuracy_at(5)) * 100
                dmrr = (r.mrr - base.mrr) * 100
                name = r.label.replace("标题反查 ", "").replace("摘要片段反查 ", "")
                logger.info("  %-20s R@5 %+6.1f 个百分点   MRR %+6.1f 个百分点", name, dr5, dmrr)


def main() -> int:
    parser = argparse.ArgumentParser(description="检索质量评测与消融实验")
    parser.add_argument("--sample", type=int, default=60, help="样本数")
    parser.add_argument("--phase", default=None, help="限定分期")
    parser.add_argument("--seed", type=int, default=42, help="随机种子")
    parser.add_argument("--recall-limit", type=int, default=50, help="每路召回条数")
    parser.add_argument("--rrf-limit", type=int, default=80, help="RRF 融合后候选数")
    parser.add_argument(
        "--recall-mode", default="hybrid", choices=["hybrid", "vector", "keyword"],
        help="召回模式（单配置评测时使用）",
    )
    parser.add_argument("--no-rerank", action="store_true", help="关闭重排")
    parser.add_argument("--no-evidence-boost", action="store_true", help="关闭证据等级加权")
    parser.add_argument(
        "--min-keyword-overlap", type=float, default=None,
        help="关键词召回最小重叠比例（覆盖配置，用于验证过滤必要性）",
    )
    parser.add_argument("--ablation", action="store_true", help="执行完整消融实验")
    parser.add_argument("--out", default=None, help="结果输出 JSON 路径")
    args = parser.parse_args()

    setup_logging()

    with session_scope() as db:
        n_docs = db.execute(select(func.count()).select_from(Document)).scalar_one()
        n_chunks = db.execute(select(func.count()).select_from(Chunk)).scalar_one()
        n_embedded = db.execute(
            select(func.count()).select_from(Chunk).where(Chunk.embedding.isnot(None))
        ).scalar_one()

    logger.info("=" * 96)
    logger.info("检索质量评测")
    logger.info("=" * 96)
    logger.info("语料: %d 篇文献 / %d 个文本块（已向量化 %d）", n_docs, n_chunks, n_embedded)
    logger.info("采样 %d 条 | 每路召回 %d | RRF 保留 %d | 分期过滤 %s",
                args.sample, args.recall_limit, args.rrf_limit, args.phase or "无")
    logger.info("=" * 96)

    summary: dict = {
        "corpus": {"documents": n_docs, "chunks": n_chunks, "embedded": n_embedded},
        "sample": args.sample,
        "runs": [],
    }

    if args.ablation:
        results = run_ablation(
            args.sample, args.phase, args.seed, args.recall_limit, args.rrf_limit
        )
        print_ablation_table(results)
        summary["runs"] = [
            {**r.to_dict(), "mode": m} for m, r in results
        ]
        summary["experiment"] = "ablation"
    else:
        for mode_name, mode_cn in [("title", "标题反查"), ("abstract", "摘要片段反查")]:
            cases = build_cases(args.sample, mode_name, args.phase, args.seed)
            if not cases:
                continue
            logger.info("\n[%s] %d 条样本", mode_cn, len(cases))
            res = evaluate(
                cases, mode_cn,
                recall_mode=args.recall_mode,
                use_rerank=not args.no_rerank,
                use_evidence_boost=not args.no_evidence_boost,
                recall_limit=args.recall_limit, rrf_limit=args.rrf_limit,
                phase=args.phase,
                min_keyword_overlap=args.min_keyword_overlap,
            )
            logger.info(
                "  %-14s R@1=%.3f R@3=%.3f R@5=%.3f R@10=%.3f MRR=%.3f 延迟=%.0fms/p95=%.0fms",
                res.label, res.accuracy_at(1), res.accuracy_at(3), res.accuracy_at(5),
                res.accuracy_at(10), res.mrr, res.avg_latency_ms, res.p95_latency_ms,
            )
            summary["runs"].append({**res.to_dict(), "mode": mode_name})

    logger.info("\n" + "=" * 96)
    logger.info("说明：以上为自动评测（以文献自身为查询）。阶段十将补充人工标注的临床问题评测集。")

    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info("结果已写入 %s", out_path)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
