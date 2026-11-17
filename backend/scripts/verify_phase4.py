"""阶段四端到端验证：RAG 问答闭环与引用溯源。

验证内容：
  1. 引用标记解析（含区间、逗号分隔、幻觉编号剔除）
  2. 非流式问答：检索 → 生成 → 引用溯源
  3. 引用元数据完整性（标题、页码、证据等级）
  4. 流式问答：逐字输出
  5. 无证据场景的拒绝作答行为

用法:
    python -m scripts.verify_phase4
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.logging import get_logger, setup_logging
from app.db.session import session_scope
from app.schemas.common import QaRequest
from app.services import citation as citation_mod
from app.services import rag_service

logger = get_logger("scripts.verify_phase4")


def hr(title: str) -> None:
    logger.info("=" * 88)
    logger.info(title)
    logger.info("=" * 88)


def step1_parse() -> bool:
    """验证 1：引用标记解析。"""
    hr("验证 1：引用标记解析")

    evidences = [
        {"index": 1, "chunk_id": "c1", "document_id": "d1", "content": "吻合口漏发生率约3%~15%。",
         "doc_title": "诊疗规范", "evidence_level": "guideline", "phase": "postop"},
        {"index": 2, "chunk_id": "c2", "document_id": "d2", "content": "处理原则包括充分引流。",
         "doc_title": "研究进展", "evidence_level": "review", "phase": "postop"},
        {"index": 3, "chunk_id": "c3", "document_id": "d3", "content": "抗生素治疗。",
         "doc_title": "专家共识", "evidence_level": "consensus", "phase": "postop"},
    ]

    cases = [
        ("吻合口漏发生率约3%~15%[1]。", [1]),
        ("处理包括引流与抗生素[1][3]。", [1, 3]),
        ("相关研究见[1,2]。", [1, 2]),
        ("综合多项证据[1-3]。", [1, 2, 3]),
        ("这是幻觉引用[99]。", []),
        ("无引用的答案。", []),
    ]

    ok = True
    for text, expected in cases:
        parsed = citation_mod.build_citations(text, evidences)
        got = [c.index for c in parsed.citations]
        match = got == expected
        if not match:
            ok = False
        logger.info("  [%s] %-32s -> 有效引用=%s 无效=%s",
                    "OK  " if match else "FAIL", text, got, parsed.invalid_indices)

    # 验证元数据是否完整映射
    parsed = citation_mod.build_citations("结论[1][3]。", evidences)
    meta_ok = all(
        c.doc_title and c.evidence_level_zh for c in parsed.citations
    )
    logger.info("  引用元数据完整性: %s", "OK" if meta_ok else "FAIL")
    logger.info("    示例: [%d] %s | 证据等级=%s | 页码=%s",
                parsed.citations[0].index, parsed.citations[0].doc_title,
                parsed.citations[0].evidence_level_zh, parsed.citations[0].page_start)

    return ok and meta_ok


def step2_qa(question: str, phase: str | None = None) -> bool:
    """验证 2：完整问答 + 引用溯源。"""
    logger.info("")
    logger.info("问: %s%s", question, f"  [分期={phase}]" if phase else "")

    with session_scope() as db:
        req = QaRequest(question=question, top_k=5, phase=phase)
        t0 = time.perf_counter()
        result = rag_service.answer(db, req)
        wall = time.perf_counter() - t0

    logger.info("答:\n%s", result.answer)
    logger.info("")
    logger.info("  证据条目: %d | 引用数: %d | 幻觉引用: %s",
                len(result.evidences), len(result.citations),
                result.invalid_citations or "无")
    logger.info("  模型: %s | token: %s", result.model, result.usage)
    logger.info("  阶段耗时: %s", result.timings)
    logger.info("  端到端: %.2f 秒", wall)

    if result.citations:
        logger.info("  引用溯源:")
        for c in result.citations:
            page = f"p.{c.page_start}" if c.page_start else "页码未标注"
            logger.info("    [%d] %s", c.index, c.doc_title[:60])
            logger.info("        %s (%s) | %s | %s | 证据等级=%s",
                        c.doc_journal or "—", c.doc_year or "—", page,
                        c.section or "—", c.evidence_level_zh)

    # 判定：应至少有 1 条引用，且引用都带元数据
    ok = bool(result.citations) and all(c.doc_title for c in result.citations)
    logger.info("  结果判定: %s", "通过" if ok else "未通过（无引用或元数据缺失）")
    return ok


def step3_stream(question: str) -> bool:
    """验证 3：流式问答。"""
    hr("验证 3：流式问答（SSE）")
    logger.info("问: %s", question)

    with session_scope() as db:
        req = QaRequest(question=question, top_k=3)
        chunks: list[str] = []
        retrieval_info: dict = {}
        done_info: dict = {}
        t0 = time.perf_counter()
        first_delta_at: float | None = None

        for event in rag_service.answer_stream(db, req):
            etype = event.get("type")
            if etype == "retrieval":
                retrieval_info = event["data"]
            elif etype == "delta":
                if first_delta_at is None:
                    first_delta_at = time.perf_counter() - t0
                chunks.append(event["data"]["text"])
            elif etype == "done":
                done_info = event["data"]
            elif etype == "error":
                logger.error("  流式错误: %s", event["data"])
                return False

        wall = time.perf_counter() - t0

    answer = "".join(chunks)
    logger.info("  首字延迟: %s", f"{first_delta_at:.2f}s" if first_delta_at else "—")
    logger.info("  文本块数: %d | 总耗时: %.2f 秒", len(chunks), wall)
    logger.info("  检索证据: %d 条", retrieval_info.get("count", 0))
    logger.info("  答案长度: %d 字符", len(answer))
    logger.info("  解析引用: %d 条", len(done_info.get("citations", [])))
    logger.info("")
    logger.info("  答案前 200 字:\n%s", answer[:200])

    ok = bool(answer) and len(done_info.get("citations", [])) > 0
    logger.info("  结果判定: %s", "通过" if ok else "未通过")
    return ok


def step4_no_evidence() -> bool:
    """验证 4：知识库未覆盖的主题，系统应明确拒答而非编造。

    注意判定标准
    ------------
    本系统对「无证据」有两道防线，二者都是正确行为：

      防线 1（检索层）：检索结果为空，或最高相关度低于 min_evidence_score
                        → 不调用 LLM，直接返回提示，has_evidence=False
      防线 2（生成层）：检索到了一些弱相关证据，LLM 被要求「仅依据证据作答」，
                        于是明确回答「提供的资料中未包含相关信息」

    现实中防线 2 更常触发（弱相关证据总能召回一些）。
    因此本验证检查的是「答案是否明确表达了资料不足」，
    而不是「是否调用了 LLM」——后者会误判防线 2 为失败。
    """
    hr("验证 4：知识库未覆盖的主题应明确拒答")

    # 明确挑一个知识库不可能覆盖的主题（中医针灸辨证选穴）
    question = "如何用针灸治疗结直肠癌术后肠梗阻的五行辨证选穴？"
    logger.info("问（知识库未覆盖的主题）: %s", question)

    with session_scope() as db:
        req = QaRequest(question=question, top_k=3, min_evidence_score=0.05)
        result = rag_service.answer(db, req)

    logger.info("答:\n%s", result.answer)
    logger.info("")
    logger.info("  has_evidence = %s", result.has_evidence)
    logger.info("  是否调用 LLM = %s", "是（防线2：生成层拒答）" if result.model else "否（防线1：检索层拒绝）")
    logger.info("  引用数 = %d", len(result.citations))
    logger.info("  token 用量: %s", result.usage or "无")

    # 拒答关键词：答案必须明确表达「资料不足/无法回答」
    refusal_markers = [
        "未包含", "未检索到", "无法回答", "无法给出", "未明确",
        "缺少", "不足", "没有相关", "未找到", "未提及",
    ]
    has_refusal = any(m in result.answer for m in refusal_markers)

    logger.info("")
    logger.info("  明确表达资料不足: %s", has_refusal)
    logger.info("  无编造编号(invalid_citations):   %s", not result.invalid_citations)

    # 判定标准说明：
    # 拒答时引用数可能 > 0 —— 这是**合理且正确**的行为。
    # LLM 会写明"证据1是诊疗规范，但其中未涉及针灸内容[1]"，
    # 即引用它**实际检查过**的证据来说明为什么不适用，这是诚实的溯源表述。
    # 真正要防的是「引用了不存在的编号」——那才是编造，记入 invalid_citations。
    ok = has_refusal and not result.invalid_citations
    logger.info("  结果判定: %s", "通过（正确拒答且未编造证据）" if ok else "未通过")
    return ok


def main() -> int:
    setup_logging()
    results: dict[str, bool] = {}

    results["1.引用解析"] = step1_parse()

    hr("验证 2：完整问答闭环")
    qa_cases = [
        ("结直肠癌术后吻合口漏应该如何处理？", None),
        ("加速康复外科的核心措施有哪些？", "postop"),
        ("术中如何评估肠道吻合口的血供？", "intraop"),
    ]
    qa_ok = True
    for q, phase in qa_cases:
        if not step2_qa(q, phase):
            qa_ok = False
    results["2.问答闭环"] = qa_ok

    results["3.流式问答"] = step3_stream("术前机械性肠道准备是否必要？")
    results["4.无证据拒绝"] = step4_no_evidence()

    hr("阶段四验证汇总")
    for name, ok in results.items():
        logger.info("  [%s] %s", "通过" if ok else "未通过", name)
    passed = sum(results.values())
    logger.info("  结果: %d/%d 通过", passed, len(results))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
