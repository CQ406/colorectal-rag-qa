"""评测运行器：检索指标与生成质量评测。

两类评测
--------
**检索评测**（`evaluate_retrieval`）：完全客观、无需 LLM、可复现。
对每条评测样本执行检索，检查 gold chunk 是否被召回及其排名。

**生成质量评测**（`evaluate_generation`）：需 LLM 裁判，较慢且消耗 token。
分两阶段执行 —— 先收集答案，再批量评判，便于中断后续跑。

指标定义
--------
  Recall@K  前 K 条含 gold chunk 的比例（最贴近实际需求）
  MRR       首个正确结果排名倒数均值（对排序敏感）
  NDCG@K    位置折扣的排序质量（与 MRR 互补）
  忠实度    答案的结论是否有检索证据支撑（医学场景最关键）
  相关性    答案是否切题
  引用准确率 引用编号是否真实对应、且确实支撑所述内容
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.eval.dataset import EvalDataset, EvalItem
from app.eval.metrics_retrieval import RetrievalMetrics, ndcg_at_k
from app.services.citation import verify_numeric_claims
from app.schemas.common import QaRequest, SearchRequest

logger = get_logger("eval.runner")


# ======================================================================
#  检索评测
# ======================================================================
def evaluate_retrieval(
    db: Session,
    dataset: EvalDataset,
    label: str,
    *,
    recall_mode: str = "hybrid",
    use_rerank: bool = True,
    use_graph: bool = False,
    use_evidence_boost: bool = True,
    top_k: int = 10,
    recall_limit: int = 50,
    rrf_limit: int = 80,
    phase: str | None = None,
    keyword_weight: float | None = None,
) -> RetrievalMetrics:
    """在评测集上执行一次检索评测。

    Args:
        dataset: 评测集
        label: 该配置的名称（用于论文表格）
        recall_mode: hybrid / vector / keyword
        use_rerank: 是否启用交叉编码器重排
        use_evidence_boost: 是否按证据等级加权
        top_k: 取前 K 条用于计算指标
        phase: 限定围术期分期（None 表示不限制）
        keyword_weight: 临时覆盖关键词路权重（消融实验用），None 表示用配置值
    """
    from app.core.config import settings
    from app.services import hybrid_search

    metrics = RetrievalMetrics(
        label=label,
        config={
            "recall_mode": recall_mode,
            "rerank": use_rerank,
            "use_graph": use_graph,
            "evidence_boost": use_evidence_boost,
            "top_k": top_k,
            "recall_limit": recall_limit,
            "rrf_limit": rrf_limit,
            "keyword_weight": keyword_weight,
        },
    )
    ks = [1, 3, 5, 10]

    # 临时覆盖权重（评测结束后恢复，避免影响后续配置）
    original_kw_weight = settings.retrieval.keyword_weight
    if keyword_weight is not None:
        settings.retrieval.keyword_weight = keyword_weight

    try:
        for i, item in enumerate(dataset.items, start=1):
            req = SearchRequest(
                query=item.question,
                top_k=top_k,
                recall_mode=recall_mode,
                use_rerank=use_rerank,
                use_graph=use_graph,
                use_evidence_boost=use_evidence_boost,
                recall_limit=recall_limit,
                rrf_limit=rrf_limit,
                phase=phase,
                max_per_document=0,   # 评测时不限制单文献块数，避免干扰召回评估
            )
            t0 = time.perf_counter()
            resp = hybrid_search.search(db, req)
            metrics.latencies.append((time.perf_counter() - t0) * 1000)

            # 定位 gold chunk 的排名（按 chunk_id 匹配，比文档级更精确）
            rank: int | None = None
            for r in resp.results:
                if str(r.chunk_id) == item.gold_chunk_id:
                    rank = r.rank
                    break

            metrics.total += 1
            metrics.ranks.append(rank)
            for k in ks:
                if rank is not None and rank <= k:
                    metrics.hits[k] = metrics.hits.get(k, 0) + 1
                metrics.ndcg.setdefault(k, []).append(ndcg_at_k(rank, k))
                # Precision@K：单答案标注下至多 1 个相关项
                metrics.precision.setdefault(k, []).append(
                    1.0 / k if (rank is not None and rank <= k) else 0.0
                )

            if i % 25 == 0:
                logger.info(
                    "    %s 进度 %d/%d (R@5=%.3f)",
                    label, i, len(dataset.items), metrics.recall_at(5),
                )
    finally:
        settings.retrieval.keyword_weight = original_kw_weight

    return metrics


# ======================================================================
#  生成质量评测（LLM 裁判）
# ======================================================================
JUDGE_SYSTEM_PROMPT = """你是医学问答系统的质量评审专家，负责严格评判 AI 答案。

评分维度与标准：

**faithfulness（忠实度，最重要）**
  答案中的每个结论是否都能在「检索到的证据」中找到依据？
  - 1.0：全部有据，无任何编造
  - 0.7~0.9：主体有据，个别表述略有外推但无害
  - 0.4~0.6：部分结论缺乏证据支撑
  - 0.0~0.3：存在明显编造，或答案主要依赖模型自身知识而非给定证据

**relevance（相关性）**
  答案是否切题、是否直接回答了所问？
  - 1.0：完全切题，直接回答
  - 0.5：部分偏题或答非所问
  - 0.0：完全无关

**citation_accuracy（引用准确率）**
  答案中的 [n] 标注是否真实对应证据编号，且该证据确实支撑所述内容？
  - 1.0：引用全部准确
  - 0.5：部分引用不支撑所述内容
  - 0.0：引用普遍错误或引用了不存在的编号

评判原则：
- 医学场景对编造**零容忍**，发现即给低分
- 答案明确说明「资料未包含」是**正确行为**，不应因信息少而扣分
- 若答案指出证据适用人群不符（如把膀胱癌研究用于结直肠），视为加分项

只输出 JSON，不要其他文字：
{"faithfulness": 0.0, "relevance": 0.0, "citation_accuracy": 0.0, "comment": "简要理由"}"""


@dataclass
class GenerationScore:
    """单条答案的评分。"""

    qid: str
    question: str
    faithfulness: float = 0.0
    relevance: float = 0.0
    citation_accuracy: float = 0.0
    comment: str = ""
    answer_chars: int = 0
    citation_count: int = 0
    invalid_citations: int = 0
    elapsed_ms: float = 0.0
    judge_failed: bool = False

    # 数值主张核验（结构化检查，不依赖 LLM 裁判）
    numeric_claims: int = 0          # 答案中的数值主张总数
    numeric_unsupported: int = 0     # 其中证据中找不到的


@dataclass
class GenerationMetrics:
    """生成质量汇总。"""

    label: str
    scores: list[GenerationScore] = field(default_factory=list)
    config: dict = field(default_factory=dict)

    @property
    def total(self) -> int:
        return len(self.scores)

    @property
    def valid(self) -> list[GenerationScore]:
        return [s for s in self.scores if not s.judge_failed]

    def _mean(self, attr: str) -> float:
        vals = [getattr(s, attr) for s in self.valid]
        return sum(vals) / len(vals) if vals else 0.0

    @property
    def faithfulness(self) -> float:
        return self._mean("faithfulness")

    @property
    def relevance(self) -> float:
        return self._mean("relevance")

    @property
    def citation_accuracy(self) -> float:
        return self._mean("citation_accuracy")

    @property
    def hallucination_rate(self) -> float:
        """出现编造引用的样本比例（引用不存在的编号）。"""
        if not self.valid:
            return 0.0
        n = sum(1 for s in self.valid if s.invalid_citations > 0)
        return n / len(self.valid)

    @property
    def avg_answer_chars(self) -> float:
        chars = [s.answer_chars for s in self.valid]
        return sum(chars) / len(chars) if chars else 0.0

    @property
    def avg_citations(self) -> float:
        vals = [s.citation_count for s in self.valid]
        return sum(vals) / len(vals) if vals else 0.0

    @property
    def avg_latency_ms(self) -> float:
        vals = [s.elapsed_ms for s in self.valid]
        return sum(vals) / len(vals) if vals else 0.0

    @property
    def total_numeric_claims(self) -> int:
        return sum(s.numeric_claims for s in self.valid)

    @property
    def total_numeric_unsupported(self) -> int:
        return sum(s.numeric_unsupported for s in self.valid)

    @property
    def numeric_error_rate(self) -> float:
        """无据数值占全部数值主张的比例（结构化检查，非 LLM 判断）。"""
        total = self.total_numeric_claims
        return self.total_numeric_unsupported / total if total else 0.0

    @property
    def answers_with_bad_numbers(self) -> int:
        """含无据数值的答案条数。"""
        return sum(1 for s in self.valid if s.numeric_unsupported > 0)

    def to_dict(self) -> dict:
        return {
            "label": self.label,
            "config": self.config,
            "total": self.total,
            "valid": len(self.valid),
            "faithfulness": round(self.faithfulness, 4),
            "relevance": round(self.relevance, 4),
            "citation_accuracy": round(self.citation_accuracy, 4),
            "hallucination_rate": round(self.hallucination_rate, 4),
            "avg_answer_chars": round(self.avg_answer_chars, 1),
            "avg_citations": round(self.avg_citations, 2),
            "avg_latency_ms": round(self.avg_latency_ms, 1),
            # 数值核验
            "numeric_claims": self.total_numeric_claims,
            "numeric_unsupported": self.total_numeric_unsupported,
            "numeric_error_rate": round(self.numeric_error_rate, 4),
            "answers_with_bad_numbers": self.answers_with_bad_numbers,
        }

    def report(self) -> str:
        return (
            f"  {self.label:<24} n={len(self.valid):<4} "
            f"忠实度={self.faithfulness:.3f} 相关性={self.relevance:.3f} "
            f"引用准确={self.citation_accuracy:.3f} "
            f"幻觉率={self.hallucination_rate:.3f} "
            f"答案={self.avg_answer_chars:.0f}字 延迟={self.avg_latency_ms:.0f}ms"
        )

    def numeric_report(self) -> str:
        """数值核验结果（结构化检查）。"""
        if not self.total_numeric_claims:
            return "  数值核验            : 答案中未出现可核验的数值"
        return (
            f"  数值核验            : 数值主张 {self.total_numeric_claims} 处, "
            f"其中无据 {self.total_numeric_unsupported} 处 "
            f"({self.numeric_error_rate * 100:.1f}%), "
            f"涉及 {self.answers_with_bad_numbers}/{len(self.valid)} 条答案"
        )


def _parse_judge_output(text: str) -> dict | None:
    """解析裁判模型输出的 JSON。"""
    if not text:
        return None
    cleaned = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", cleaned, re.S)
    if fence:
        cleaned = fence.group(1).strip()
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        return json.loads(cleaned[start : end + 1])
    except json.JSONDecodeError:
        return None


def _clamp01(value) -> float:
    """把任意输入裁剪到 [0, 1]。"""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, v))


def collect_answers(
    db: Session,
    dataset: EvalDataset,
    sample: int | None = None,
    top_k: int = 5,
    phase: str | None = None,
) -> list[dict]:
    """第一阶段：对评测集跑完整问答，收集答案与证据。

    与评判分离的原因：问答调用较慢，先全部收集再统一评判，
    便于中断后续跑，也便于人工查看真实回答。
    """
    from app.services import rag_service

    items = dataset.items[:sample] if sample else dataset.items
    records: list[dict] = []

    for i, item in enumerate(items, start=1):
        req = QaRequest(
            question=item.question,
            top_k=top_k,
            phase=phase or (None if item.phase == "general" else None),
        )
        t0 = time.perf_counter()
        try:
            result = rag_service.answer(db, req)
            elapsed = (time.perf_counter() - t0) * 1000
            records.append(
                {
                    "qid": item.qid,
                    "question": item.question,
                    "phase": item.phase,
                    "question_type": item.question_type,
                    "answer": result.answer,
                    "citations": [c.to_dict() for c in result.citations],
                    "invalid_citations": result.invalid_citations,
                    "evidences": [
                        {
                            "index": e["index"],
                            "doc_title": e["doc_title"],
                            # 截断仅用于送入裁判模型的提示词（控制 token）
                            "content": e["content"][:900],
                            # 核验数值主张必须用**完整**证据正文。
                            # 若用截断文本核对，证据中真实存在的数字会因
                            # 落在截断之外而被判为「无据」，造成大量误报
                            # （实测误报率曾达 100%）。
                            "full_content": e["content"],
                        }
                        for e in result.evidences
                    ],
                    "has_evidence": result.has_evidence,
                    "elapsed_ms": round(elapsed, 1),
                    "gold_chunk_id": item.gold_chunk_id,
                    "gold_span": item.gold_span[:900],
                    "reference_answer": item.reference_answer,
                    "judge": None,
                }
            )
        except Exception as exc:  # noqa: BLE001 - 单条失败不中断整批
            logger.warning("[%s] 问答失败: %s", item.qid, str(exc)[:120])
            records.append(
                {
                    "qid": item.qid,
                    "question": item.question,
                    "phase": item.phase,
                    "question_type": item.question_type,
                    "answer": "",
                    "citations": [],
                    "invalid_citations": [],
                    "evidences": [],
                    "has_evidence": False,
                    "elapsed_ms": 0.0,
                    "gold_chunk_id": item.gold_chunk_id,
                    "gold_span": item.gold_span[:900],
                    "reference_answer": item.reference_answer,
                    "judge": None,
                    "error": str(exc)[:200],
                }
            )

        if i % 5 == 0:
            logger.info("    已收集 %d/%d", i, len(items))

    return records


def judge_answers(
    records: list[dict],
    label: str = "完整系统",
    config: dict | None = None,
) -> GenerationMetrics:
    """第二阶段：用 LLM 裁判对已收集的答案评分。"""
    from app.services.llm_client import LLMClient, LLMError

    metrics = GenerationMetrics(label=label, config=config or {})
    client = LLMClient()

    try:
        for i, rec in enumerate(records, start=1):
            score = GenerationScore(
                qid=rec["qid"],
                question=rec["question"],
                answer_chars=len(rec.get("answer", "")),
                citation_count=len(rec.get("citations", [])),
                invalid_citations=len(rec.get("invalid_citations", [])),
                elapsed_ms=rec.get("elapsed_ms", 0.0),
            )

            # 数值主张核验（结构化检查，独立于 LLM 裁判）。
            # 使用**完整**证据正文（full_content 优先），
            # 否则证据中真实存在的数字会因落在截断之外而被误判为无据
            # （实测误报率曾因证据缺失达到 100%）。
            evidence_texts = [
                e.get("full_content") or e.get("content", "")
                for e in rec.get("evidences", [])
            ]
            supported, unsupported = verify_numeric_claims(
                rec.get("answer", ""), evidence_texts
            )
            score.numeric_claims = len(supported) + len(unsupported)
            score.numeric_unsupported = len(unsupported)
            rec["numeric"] = {
                "claims": score.numeric_claims,
                "unsupported": [c.raw.strip() for c in unsupported],
            }

            # 无答案或无证据时直接给 0，无需调用裁判
            if not rec.get("answer", "").strip():
                score.judge_failed = True
                score.comment = "无答案"
                metrics.scores.append(score)
                continue

            prompt = _build_judge_prompt(rec)
            try:
                result = client.chat(
                    [
                        {"role": "system", "content": JUDGE_SYSTEM_PROMPT},
                        {"role": "user", "content": prompt},
                    ],
                    temperature=0.0,   # 评判需确定性
                    # 裁判同样是推理模型（deepseek-flash），推理 token 与输出
                    # 共用额度。实测 2048 时有约 50% 的样本因推理耗尽额度而
                    # 返回空内容（推理内容可达 8000+ 字符），故取 8192。
                    max_tokens=8192,
                    response_format={"type": "json_object"},
                )
                parsed = _parse_judge_output(result.content)
                if parsed:
                    score.faithfulness = _clamp01(parsed.get("faithfulness"))
                    score.relevance = _clamp01(parsed.get("relevance"))
                    score.citation_accuracy = _clamp01(
                        parsed.get("citation_accuracy")
                    )
                    score.comment = str(parsed.get("comment", ""))[:400]
                    rec["judge"] = {
                        "faithfulness": score.faithfulness,
                        "relevance": score.relevance,
                        "citation_accuracy": score.citation_accuracy,
                        "comment": score.comment,
                    }
                else:
                    score.judge_failed = True
                    score.comment = "裁判输出无法解析"
            except LLMError as exc:
                logger.warning("[%s] 裁判失败: %s", rec["qid"], str(exc)[:100])
                score.judge_failed = True
                score.comment = f"裁判失败: {str(exc)[:100]}"

            metrics.scores.append(score)
            if i % 10 == 0:
                logger.info(
                    "    已评判 %d/%d (忠实度=%.3f)",
                    i, len(records), metrics.faithfulness,
                )
    finally:
        client.close()

    return metrics


def _build_judge_prompt(rec: dict) -> str:
    """构造裁判提示词。"""
    evidence_lines = []
    for e in rec.get("evidences", []):
        evidence_lines.append(f"[证据 {e['index']}] {e['doc_title']}\n{e['content']}")
    evidence_text = "\n\n".join(evidence_lines) or "（未检索到证据）"

    citations_text = "、".join(
        f"[{c['index']}] {c.get('doc_title', '')}" for c in rec.get("citations", [])
    ) or "（无引用）"

    return f"""## 用户问题

{rec['question']}

## 检索到的证据

{evidence_text}

## AI 答案

{rec.get('answer', '')}

## 系统解析出的引用

{citations_text}
（引用编号中，实际不存在的编号为：{rec.get('invalid_citations') or '无'}）

请按评分标准评判，只输出 JSON。"""
