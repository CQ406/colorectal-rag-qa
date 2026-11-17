"""RAG 问答服务 —— LangChain LCEL 编排与引用溯源。

流程：

    用户提问
      ↓
    ① 检索（多路召回 + RRF 融合 + 重排）  ← 阶段三成果
      ↓
    ② 证据组装（编号 + 元数据，供 LLM 引用）
      ↓
    ③ LLM 生成（约束：仅依据证据、内联标注 [n]）
      ↓
    ④ 引用解析（[n] → 真实文献、页码、证据等级）
      ↓
    带引用溯源的答案

两种模式：
  - answer()        非流式，返回完整结果（含 token 用量，便于统计）
  - answer_stream() 流式，逐字产出（前端体验好，但拿不到用量）

「无证据」的处理
----------------
检索为空或分数过低时，**不调用 LLM**，直接返回明确提示。
理由：让 LLM 在没有证据的情况下作答，等于放任其依赖内部知识，
既无法溯源也无法控制幻觉——这与本系统的设计目标冲突。
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.schemas.common import QaRequest
from app.services import citation as citation_mod
from app.services import prompts
from app.services.llm_client import LLMClient, LLMError
from app.services import rag_chain
from langsmith import tracing_context

logger = get_logger("services.rag")

# 检索结果的重排分低于此值时，视为证据不足。
# 依据：重排模型归一化输出，实测不相关文本得分通常 < 0.05，
# 而相关文本 > 0.2。取 0.05 作为「完全无关」的保守门槛。
MIN_EVIDENCE_SCORE = 0.05


@dataclass
class QaAnswer:
    """一次问答的完整结果。"""

    question: str
    answer: str
    citations: list[citation_mod.Citation] = field(default_factory=list)
    invalid_citations: list[int] = field(default_factory=list)
    # 答案中出现但检索证据中找不到的数值（独立校验层，见 citation.verify_numeric_claims）
    unsupported_numbers: list[str] = field(default_factory=list)
    numeric_warning: str = ""
    evidences: list[dict] = field(default_factory=list)
    has_evidence: bool = True
    # 阶段耗时
    timings: dict = field(default_factory=dict)
    usage: dict = field(default_factory=dict)
    model: str = ""
    disclaimer: str = prompts.DISCLAIMER

    @property
    def cited_titles(self) -> list[str]:
        """被引用的文献标题（去重，保持顺序）。"""
        seen: set[str] = set()
        out: list[str] = []
        for c in self.citations:
            if c.doc_title and c.doc_title not in seen:
                seen.add(c.doc_title)
                out.append(c.doc_title)
        return out


def _no_evidence_answer(question: str, timings: dict, search_resp) -> QaAnswer:
    """构造「未找到证据」的回答（不调用 LLM）。"""
    return QaAnswer(
        question=question,
        answer=(
            "知识库中未检索到与该问题相关的证据资料，因此无法给出有依据的回答。\n\n"
            "建议您：\n"
            "1. 换用更具体的专业术语重新提问（如使用完整术式名称或并发症名称）\n"
            "2. 缩小或移除围术期分期筛选条件\n"
            "3. 确认该主题是否已在知识库覆盖范围内"
        ),
        citations=[],
        evidences=[],
        has_evidence=False,
        timings=timings,
        model="",
    )


def _elapsed(state: dict) -> float:
    return round((time.perf_counter() - state["started"]) * 1000, 1)


def _meta_result(state: dict) -> QaAnswer:
    return QaAnswer(question=state["req"].question, answer=state["meta_answer"],
                    timings={"total_ms": _elapsed(state)})


def _refuse(state: dict) -> QaAnswer:
    state["timings"]["total_ms"] = _elapsed(state)
    result = _no_evidence_answer(state["req"].question, state["timings"], state["search"])
    if state["reason"] == "low_evidence":
        result.answer = (
            f"检索到的资料与您的问题相关性不足（最高相关度 {state['top_score']:.2f}），"
            "无法给出可靠回答。\n\n建议换用更贴近文献表述的专业术语重新提问。"
        )
    return result


def _resolve_temperature(state: dict) -> float | None:
    """采样温度：请求显式给了就用请求的，否则读运行时配置。

    此前直接透传 `req.temperature`（默认 None），由 LLMClient 用自身默认值，
    于是管理界面把 `llm.temperature` 改掉对生成毫无影响 —— 界面却标着"立即生效"。
    """
    provided = state["req"].temperature
    if provided is not None:
        return provided
    session = state.get("db")
    if session is None:
        return None
    try:
        from app.repositories import config_repo

        value = config_repo.effective(session).get("llm.temperature")
        return float(value) if value is not None else None
    except Exception:  # noqa: BLE001
        return None


def _generate(state: dict) -> dict:
    started = time.perf_counter()
    client = LLMClient()
    try:
        result = client.chat(state["messages"], temperature=_resolve_temperature(state))
    finally:
        client.close()
    if not result.content.strip():
        raise LLMError("模型返回空内容，无法生成有效回答")
    state.update(answer=result.content, model=result.model, usage=result.usage.as_dict)
    state["timings"]["generation_ms"] = round((time.perf_counter() - started) * 1000, 1)
    return state


def _finish(state: dict) -> QaAnswer:
    parsed = state["parsed"]
    state["timings"]["total_ms"] = _elapsed(state)
    return QaAnswer(
        question=state["req"].question, answer=state["answer"],
        citations=parsed.citations, invalid_citations=parsed.invalid_indices,
        unsupported_numbers=state["unsupported"], numeric_warning=state["numeric_warning"],
        evidences=state["evidences"], timings=state["timings"],
        usage=state.get("usage", {}), model=state.get("model", ""),
    )


def _done_event(result: QaAnswer) -> dict:
    return {"type": "done", "data": {
        "answer": result.answer, "citations": [c.to_dict() for c in result.citations],
        "invalid_citations": result.invalid_citations,
        "unsupported_numbers": result.unsupported_numbers, "numeric_warning": result.numeric_warning,
        "elapsed_ms": result.timings["total_ms"], "timings": result.timings,
        "model": result.model, "disclaimer": result.disclaimer,
    }}


def _stream_meta(states: Iterator[dict]) -> Iterator[dict]:
    for state in states:
        result = _meta_result(state)
        yield {"type": "retrieval", "data": {"count": 0, "top_score": 0.0, "elapsed_ms": 0.0, "evidences": []}}
        yield {"type": "delta", "data": {"text": result.answer}}
        yield _done_event(result)


def _stream_refuse(states: Iterator[dict]) -> Iterator[dict]:
    for state in states:
        result = _refuse(state)
        yield {"type": "error", "data": {"message": result.answer, "reason": state["reason"]}}


def _release_connection(state: dict) -> None:
    """检索完成后立刻归还数据库连接，避免整个流式生成期间占着池。

    问题：流式问答里 `db` 在检索阶段开启了事务，之后生成要几十秒到几分钟
    （llm_timeout=180s），而事务结束后连接才归还。`pool_size=10` 的情况下，
    10 个并发流式问答就能把池占满，其他接口全部排队到 pool_timeout。

    做法：检索与证据打包完成后主动结束事务（commit 空事务即可），
    生成阶段不再持有连接；末尾写日志时再短暂申请一次。
    """
    session = state.get("db")
    if session is None:
        return
    try:
        if session.in_transaction():
            session.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning("归还数据库连接失败（不影响生成）: %s", str(exc)[:120])
        try:
            session.rollback()
        except Exception:  # noqa: BLE001
            pass


def _stream_generate(states: Iterator[dict]) -> Iterator[dict]:
    for state in states:
        evidences = [{**{k: v for k, v in e.items() if k != "content"},
                      "content_snippet": citation_mod._snippet(e["content"])}
                     for e in state["evidences"]]
        # 证据已取到，先把后续要用的参数（温度）读出来，再归还连接。
        # 顺序很关键：_resolve_temperature 会查配置表，若放在释放之后，
        # 又会新开一个事务，生成期间照样占着连接（实测确实如此）。
        state["_temperature"] = _resolve_temperature(state)
        _release_connection(state)
        yield {"type": "retrieval", "data": {
            "count": len(evidences), "top_score": round(state["top_score"], 4),
            "elapsed_ms": state["timings"]["retrieval_ms"], "evidences": evidences}}
        started = time.perf_counter()
        buffer: list[str] = []
        client = LLMClient()
        try:
            chunks = client.chat_stream(
                state["messages"], temperature=state.get("_temperature")
            )
            try:
                for text in chunks:
                    buffer.append(text)
                    yield {"type": "delta", "data": {"text": text}}
            finally:
                close = getattr(chunks, "close", None)
                if close:
                    close()
        finally:
            client.close()
        state["answer"] = "".join(buffer)
        if not state["answer"].strip():
            raise LLMError("模型返回空内容，无法生成有效回答")
        state["model"] = getattr(client, "model", "")
        state["timings"]["generation_ms"] = round((time.perf_counter() - started) * 1000, 1)
        yield _done_event(_finish(rag_chain.VALIDATION_CHAIN.invoke(state)))


def build_qa_chain(*, streaming: bool = False):
    return rag_chain.build_chain(
        generate=_stream_generate if streaming else _generate, finish=_finish,
        refuse=_stream_refuse if streaming else _refuse,
        meta_result=_stream_meta if streaming else _meta_result, stream=streaming,
    ).with_config({"run_name": "perioperative_rag_stream" if streaming else "perioperative_rag"})


def answer(db: Session, req: QaRequest) -> QaAnswer:
    """Run the production LCEL graph; disable cloud tracing for evidence text."""
    with tracing_context(enabled=False):
        return build_qa_chain().invoke({"db": db, "req": req})


def answer_stream(db: Session, req: QaRequest) -> Iterator[dict]:
    """LCEL streaming without buffering or changing the public SSE protocol."""
    events = build_qa_chain(streaming=True).stream({"db": db, "req": req})
    try:
        while True:
            try:
                # StreamingResponse can resume next() in different worker contexts.
                # Never hold the tracing context open across a yield.
                with tracing_context(enabled=False):
                    event = next(events)
            except StopIteration:
                return
            yield event
    except LLMError as exc:
        yield {"type": "error", "data": {"message": f"答案生成失败: {exc}", "reason": "llm_error"}}
    except rag_chain.RetrievalError as exc:
        yield {"type": "error", "data": {"message": f"检索失败: {exc}"}}
    finally:
        with tracing_context(enabled=False):
            events.close()


def check_llm() -> tuple[bool, str]:
    from app.services.llm_client import check_llm_available
    return check_llm_available()


