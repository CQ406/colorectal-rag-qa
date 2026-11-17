"""LangChain LCEL stages shared by synchronous and streaming RAG.

The framework orchestrates business stages; hybrid retrieval and the compatible
HTTP client remain adapters, not a second, bypassable implementation of RAG.
"""
from __future__ import annotations

import time

from langchain_core.messages import SystemMessage
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import RunnableBranch, RunnableGenerator, RunnableLambda

from app.services import citation, hybrid_search, meta_intent, prompts
from app.repositories import search_repo


def initialize(inputs: dict) -> dict:
    return {**inputs, "started": time.perf_counter(), "timings": {}}


def meta(state: dict) -> dict:
    state["meta_answer"] = meta_intent.build_meta_answer(
        search_repo.count_active_documents(state["db"]),
        search_repo.count_active_chunks(state["db"]),
    )
    return state


class RetrievalError(RuntimeError):
    """Preserve the distinction between retrieval and model failures in SSE."""


def retrieve(state: dict) -> dict:
    try:
        response = hybrid_search.search(state["db"], state["req"].to_search_request())
    except Exception as exc:
        raise RetrievalError(str(exc)) from exc
    state["search"] = response
    state["timings"]["retrieval_ms"] = round(response.elapsed_ms, 1)
    return state


def evidence_gate(state: dict) -> dict:
    req, response = state["req"], state["search"]
    score = max((r.rerank_score or r.score or 0.0 for r in response.results), default=0.0)
    state["top_score"] = score
    state["reason"] = ("no_evidence" if not response.results else
                       "low_evidence" if req.min_evidence_score > 0 and score < req.min_evidence_score else "")
    state["evidences"] = citation.evidences_from_search(response.results) if not state["reason"] else []
    return state


def prompt_values(state: dict) -> dict:
    return {"evidence_prompt": prompts.build_qa_prompt(
        state["req"].question, state["evidences"], state["req"].phase)}


# Literal system message and variable values preserve braces in documents.
PROMPT = ChatPromptTemplate.from_messages([
    SystemMessage(content=prompts.SYSTEM_PROMPT), ("human", "{evidence_prompt}"),
])


def messages(value) -> list[dict]:
    roles = {"system": "system", "human": "user", "ai": "assistant"}
    return [{"role": roles[m.type], "content": m.content} for m in value.to_messages()]


PROMPT_CHAIN = (RunnableLambda(prompt_values, name="assemble_evidence_prompt")
                | PROMPT | RunnableLambda(messages, name="transport_messages"))


def attach_prompt(state: dict) -> dict:
    state["messages"] = PROMPT_CHAIN.invoke(state)
    return state


def prepare_chain():
    return RunnableLambda(initialize, name="request_state") | RunnableBranch(
        (lambda s: meta_intent.is_meta_question(s["req"].question),
         RunnableLambda(meta, name="assistant_identity")),
        RunnableLambda(retrieve, name="hybrid_retrieval")
        | RunnableLambda(evidence_gate, name="evidence_gate"),
    )


def parse_citations(state: dict) -> dict:
    state["parsed"] = citation.build_citations(state["answer"], state["evidences"])
    return state


def verify_numbers(state: dict) -> dict:
    _, claims = citation.verify_numeric_claims(
        state["answer"], [e["content"] for e in state["evidences"]])
    state["unsupported"] = [c.raw.strip() for c in claims]
    state["numeric_warning"] = citation.unsupported_numeric_summary(claims)
    return state


VALIDATION_CHAIN = (RunnableLambda(parse_citations, name="citation_validation")
                    | RunnableLambda(verify_numbers, name="numeric_validation"))


def build_chain(*, generate, finish, refuse, meta_result, stream=False):
    """Production LCEL graph with explicit short-circuit and evidence branches."""
    accepted = RunnableLambda(attach_prompt, name="format_prompt")
    if stream:
        accepted = accepted | RunnableGenerator(generate, name="stream_model_and_validate")
        blocked = RunnableGenerator(refuse, name="stream_evidence_refusal")
        identity = RunnableGenerator(meta_result, name="stream_identity")
    else:
        accepted = (accepted | RunnableLambda(generate, name="model_generation")
                    | VALIDATION_CHAIN | RunnableLambda(finish, name="answer_result"))
        blocked = RunnableLambda(refuse, name="evidence_refusal")
        identity = RunnableLambda(meta_result, name="identity_result")
    return prepare_chain() | RunnableBranch(
        (lambda s: "meta_answer" in s, identity),
        RunnableBranch((lambda s: bool(s["reason"]), blocked), accepted),
    )
