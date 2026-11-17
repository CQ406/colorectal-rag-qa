"""多轮对话服务（阶段七）。

报审表要求：「支持上下文感知的多轮对话，理解用户追问意图并保持对话连贯性」

核心问题
--------
RAG 检索依赖**独立的查询串**，但多轮对话中的追问大量使用指代与省略：

    用户：结直肠癌术后吻合口漏怎么处理？
    助手：（回答……）
    用户：**它**的发生率是多少？          ← "它" 指什么？

若直接把「它的发生率是多少？」送去检索，语义检索与关键词检索都会失效
（查询里没有任何医学实体）。这是多轮 RAG 最常见的失败点。

解决方案：查询改写（Query Rewriting）
------------------------------------
在检索**之前**插入一步：结合对话历史，把追问改写为独立完整的查询。

    改写前：它的发生率是多少？
    改写后：结直肠癌术后吻合口漏的发生率是多少

改写通过 LLM 完成（提示词见 prompts.REWRITE_SYSTEM_PROMPT）。
这一步骤对最终效果影响显著，且可独立评测（见下）。

**何时跳过改写**：首轮对话、或以新主题开头的长问题（本身已完整）。
改写本身要消耗一次 LLM 调用，不能无脑对所有问题执行。

低成本启发式判断
----------------
用规则判断"是否需要改写"比再用一次 LLM 分类更划算：
  - 首轮 → 不改写
  - 含明显指代词（它/这个/该/上述/此/这些）且句长较短 → 改写
  - 句长足够长且不含指代词 → 视为独立问题，不改写

这是「成本 vs 准确率」的权衡：规则会漏判少数需要改写的情况，
但省下一次 LLM 调用（约 1~3 秒延迟）。对交互式系统，延迟很关键。
"""

from __future__ import annotations

import re
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.schemas.common import QaRequest
from app.repositories import search_repo
from app.services import meta_intent, prompts, qa_logging, rag_service
from app.services.llm_client import LLMClient, LLMError

logger = get_logger("services.conversation")

# 指代词表（用于启发式判断是否需要改写）
REFERENTIAL_WORDS = (
    "它", "他", "她", "这个", "那个", "这些", "那些", "该", "此", "上述",
    "前者", "后者", "这种", "那种", "上述的", "其", "该术式", "该病",
    "这", "那", "此病", "该患者", "这类", "那类",
)

# 无需改写的情况：以这些词开头通常是在开新话题
NEW_TOPIC_MARKERS = ("另外", "还有一个问题", "换个话题", "再问一个", "请问一下")

# 超过此长度且不含指代词的问题，视为已独立完整
INDEPENDENT_LENGTH = 18


def needs_rewrite(question: str, history: list[dict]) -> bool:
    """启发式判断是否需要查询改写。

    判断逻辑（按优先级）：
      1. 无历史 → 不需要（首轮）
      2. 明确的新话题标记 → 不需要
      3. 含指代词 → 需要
      4. 问题很短（<18 字）→ 需要（短问题通常依赖上下文）
      5. 其余 → 不需要
    """
    if not history:
        return False

    q = (question or "").strip()
    if not q:
        return False

    if any(q.startswith(m) for m in NEW_TOPIC_MARKERS):
        return False

    if any(w in q for w in REFERENTIAL_WORDS):
        return True

    # 短问题往往省略了主语或限定条件
    if len(q) < INDEPENDENT_LENGTH:
        return True

    return False


def rewrite_query(
    question: str,
    history: list[dict],
    client: LLMClient | None = None,
) -> tuple[str, bool, float]:
    """把追问改写为独立查询。

    Returns:
        (改写后的查询, 是否实际改写, 耗时毫秒)

    改写失败时**返回原问题**而不是抛错：改写是增强手段，
    不应因它失败而中断整个问答流程。
    """
    if not needs_rewrite(question, history):
        return question, False, 0.0

    own_client = client is None
    if own_client:
        client = LLMClient()

    t0 = time.perf_counter()
    try:
        result = client.chat(
            [
                {"role": "system", "content": prompts.REWRITE_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": prompts.build_rewrite_prompt(question, history),
                },
            ],
            temperature=0.0,   # 改写要求稳定，不要创造性
            max_tokens=1024,
        )
        rewritten = (result.content or "").strip()
        # 清理模型可能加的引号或前缀
        rewritten = re.sub(r'^["「『]|["」』]$', "", rewritten).strip()
        rewritten = re.sub(r"^(改写后|查询|输出)[:：]\s*", "", rewritten).strip()
        elapsed = (time.perf_counter() - t0) * 1000

        # 合理性校验：改写结果不应为空，也不应异常长
        if not rewritten or len(rewritten) > 500:
            logger.warning("改写结果异常，回退原问题: %r", rewritten[:80])
            return question, False, elapsed

        logger.info("查询改写: %r -> %r", question[:40], rewritten[:60])
        return rewritten, True, elapsed
    except LLMError as exc:
        elapsed = (time.perf_counter() - t0) * 1000
        logger.warning("查询改写失败（回退原问题）: %s", str(exc)[:120])
        return question, False, elapsed
    finally:
        if own_client:
            client.close()


def classify_intent(
    question: str,
    history: list[dict] | None = None,
) -> tuple[str, str, float]:
    """识别问题的围术期分期与类型。

    Returns:
        (phase, intent, confidence)

    用于两处：
      1. 分期路由：把检索限定在对应分期，提升精度
      2. 界面展示：让用户知道系统如何理解该问题

    解析失败时返回 (general, knowledge, 0.0)，不中断流程。
    """
    client = LLMClient()
    try:
        result = client.chat(
            [
                {"role": "system", "content": prompts.INTENT_SYSTEM_PROMPT},
                {"role": "user", "content": prompts.build_intent_prompt(question)},
            ],
            temperature=0.0,
            max_tokens=512,
            response_format={"type": "json_object"},
        )
        text = result.content or ""
        import json as _json

        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            return "general", "knowledge", 0.0
        data = _json.loads(text[start : end + 1])
        phase = data.get("phase") if data.get("phase") in (
            "preop", "intraop", "postop", "general"
        ) else "general"
        intent = data.get("intent") or "knowledge"
        try:
            confidence = float(data.get("confidence") or 0.0)
        except (TypeError, ValueError):
            confidence = 0.0
        return phase, intent, confidence
    except (LLMError, ValueError) as exc:
        logger.warning("意图识别失败: %s", str(exc)[:120])
        return "general", "knowledge", 0.0
    finally:
        client.close()


# ======================================================================
#  会话管理
# ======================================================================
@dataclass
class ConversationTurn:
    """一轮对话。"""

    question: str
    answer: str
    rewritten_query: str = ""
    phase: str = "general"
    intent: str = "knowledge"
    citations_count: int = 0
    timestamp: str = ""
    # 引用明细（与问答接口的 CitationItem 同构）。
    #
    # 为什么要存：前端切换到别的页面时组件卸载，返回后靠本历史重建对话。
    # 若只存 citations_count，恢复出来的答案正文里有 [1][2] 标记，
    # 右侧引用面板却显示「未引用任何文献」——自相矛盾。
    citations: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "question": self.question,
            "answer": self.answer,
            "rewritten_query": self.rewritten_query,
            "phase": self.phase,
            "intent": self.intent,
            "citations_count": self.citations_count,
            "timestamp": self.timestamp,
            "citations": self.citations,
        }


@dataclass
class Conversation:
    """一个会话（含多轮历史）。"""

    session_id: str
    turns: list[ConversationTurn] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""

    # 保留的最大轮数：超出后丢弃最早的轮次，避免上下文无限增长
    # （token 成本与延迟都会随历史增长而上升）
    max_turns: int = 10
    _persist_turn: Callable | None = field(default=None, repr=False)

    def history(self, max_messages: int = 6) -> list[dict]:
        """返回供 LLM 使用的对话历史（含助手回答，供指代解析）。"""
        msgs: list[dict] = []
        for turn in self.turns[-3:]:   # 最近 3 轮
            msgs.append({"role": "user", "content": turn.question})
            if turn.answer:
                msgs.append({"role": "assistant", "content": turn.answer})
        return msgs[-max_messages:]

    def add_turn(self, turn: ConversationTurn) -> None:
        if self._persist_turn:
            latest = self._persist_turn(turn)
            self.turns = latest.turns
            self.created_at = latest.created_at
            self.updated_at = latest.updated_at
            return
        self.turns.append(turn)
        if len(self.turns) > self.max_turns:
            # 丢弃最早轮次
            self.turns = self.turns[-self.max_turns :]
        now = datetime.now(timezone.utc).isoformat()
        self.updated_at = now
        if not self.created_at:
            self.created_at = now

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "turn_count": len(self.turns),
            "turns": [t.to_dict() for t in self.turns],
        }


class ConversationStore:
    """PostgreSQL storage; short independent transactions survive request aborts.

    Append takes a row lock so concurrent completed turns cannot overwrite each
    other. Deleted sessions are never recreated by late generation callbacks.
    """

    def __init__(self, session_factory=None) -> None:
        if session_factory is None:
            from app.db.session import SessionLocal
            session_factory = SessionLocal
        self.session_factory = session_factory

    def _conversation(self, row, owner_key):
        conv = Conversation(session_id=row.session_id,
            turns=[ConversationTurn(**t) for t in (row.turns or [])],
            created_at=row.created_at.isoformat(), updated_at=row.updated_at.isoformat())
        conv._persist_turn = lambda turn: self.append(row.session_id, owner_key, turn)
        return conv

    def get_or_create(self, session_id: str | None, owner_key: str = 'internal') -> Conversation:
        from app.models.system import ChatSession
        if session_id:
            result = self.get(session_id, owner_key)
            if result is None:
                raise ConversationNotFound('会话不存在或不属于当前用户')
            return result
        now = datetime.now(timezone.utc)
        with self.session_factory.begin() as db:
            row = ChatSession(session_id=uuid.uuid4().hex, owner_key=owner_key,
                turns=[], created_at=now, updated_at=now)
            db.add(row)
            db.flush()
            return self._conversation(row, owner_key)

    def get(self, session_id: str, owner_key: str = 'internal') -> Conversation | None:
        from sqlalchemy import select
        from app.models.system import ChatSession
        with self.session_factory() as db:
            row = db.scalar(select(ChatSession).where(ChatSession.session_id == session_id,
                ChatSession.owner_key == owner_key))
            return self._conversation(row, owner_key) if row else None

    def append(self, session_id, owner_key, turn):
        from sqlalchemy import select
        from app.models.system import ChatSession
        with self.session_factory.begin() as db:
            row = db.scalar(select(ChatSession).where(ChatSession.session_id == session_id,
                ChatSession.owner_key == owner_key).with_for_update())
            if row is None:
                raise ConversationNotFound('会话已清除')
            row.turns = [*(row.turns or []), turn.to_dict()][-10:]
            row.updated_at = datetime.now(timezone.utc)
            return self._conversation(row, owner_key)

    def clear(self, session_id: str, owner_key: str = 'internal') -> bool:
        from sqlalchemy import delete
        from app.models.system import ChatSession
        with self.session_factory.begin() as db:
            result = db.execute(delete(ChatSession).where(ChatSession.session_id == session_id,
                ChatSession.owner_key == owner_key))
            return bool(result.rowcount)

    def stats(self, owner_key: str = 'internal') -> dict:
        from sqlalchemy import select
        from app.models.system import ChatSession
        with self.session_factory() as db:
            turns = db.scalars(select(ChatSession.turns).where(ChatSession.owner_key == owner_key)).all()
            return {'active_sessions': len(turns), 'total_turns':sum(len(t or []) for t in turns)}


class ConversationNotFound(Exception):
    pass


# 全局单例（应用生命周期内复用）
_store = ConversationStore()


def get_store() -> ConversationStore:
    return _store


# ======================================================================
#  多轮问答主流程
# ======================================================================
@dataclass
class ChatResult:
    """一次多轮问答的结果。"""

    session_id: str
    question: str
    rewritten_query: str
    answer: str
    citations: list = field(default_factory=list)
    invalid_citations: list[int] = field(default_factory=list)
    unsupported_numbers: list[str] = field(default_factory=list)
    numeric_warning: str = ""
    evidences: list[dict] = field(default_factory=list)
    phase: str = "general"
    intent: str = "knowledge"
    intent_confidence: float = 0.0
    rewritten: bool = False
    is_followup: bool = False
    timings: dict = field(default_factory=dict)
    model: str = ""
    disclaimer: str = prompts.DISCLAIMER


def chat_stream(
    db: Session,
    question: str,
    session_id: str | None = None,
    *,
    top_k: int = 5,
    use_intent_routing: bool = True,
    phase_override: str | None = None,
    owner_key: str = 'internal',
    user: object | None = None,
) -> Iterator[dict]:
    """多轮问答（流式）。

    事件序列：
      `meta`      会话与路由信息（session_id、改写后查询、分期、意图、置信度）
      `retrieval` 检索完成，携带证据列表
      `delta`     答案增量
      `done`      完成，携带完整答案、引用与核验结果，并已写入会话历史
      `error`     出错

    为什么先发 `meta`
    -----------------
    意图识别与查询改写在检索之前完成，前端可以立即展示
    「通用 · 知识提问（置信度 0.82）」与「已改写为：…」，
    不必等到答案生成完才显示。这与非流式接口把这些字段放在响应体里
    是等价的，只是提前了。

    用法（FastAPI）::

        for event in conversation.chat_stream(db, req.question, req.session_id):
            yield f"data: {json.dumps(event, ensure_ascii=False)}\\n\\n"
    """
    t_start = time.perf_counter()
    store = get_store()
    conv = store.get_or_create(session_id, owner_key)
    history = conv.history()
    is_followup = bool(history)


    # ---------- ⓪ 助手指令 / 身份类问题（不检索） ----------
    # 「你是谁」「你能做什么」不属于知识提问：查询改写与检索都无意义。
    # 缺了这一步时，它会被判成知识提问（置信度约 0.1）并走完整检索链路，
    # 最终答出「资料中未包含与"我是谁"相关的信息」，属答非所问。
    if meta_intent.is_meta_question(question):
        answer_text = meta_intent.build_meta_answer(
            search_repo.count_active_documents(db),
            search_repo.count_active_chunks(db),
        )
        elapsed = round((time.perf_counter() - t_start) * 1000, 1)
        conv.add_turn(
            ConversationTurn(
                question=question,
                answer=answer_text,
                phase="general",
                intent="meta",
                citations_count=0,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )
        base = {
            "session_id": conv.session_id,
            "question": question,
            "rewritten_query": "",
            "rewritten": False,
            "is_followup": is_followup,
            "phase": "general",
            "intent": "meta",
            "intent_confidence": 1.0,
        }
        yield {"type": "meta", "data": base}
        yield {
            "type": "retrieval",
            "data": {"count": 0, "top_score": 0.0, "elapsed_ms": 0.0, "evidences": []},
        }
        yield {"type": "delta", "data": {"text": answer_text}}
        yield {
            "type": "done",
            "data": {
                **base,
                "answer": answer_text,
                "citations": [],
                "invalid_citations": [],
                "unsupported_numbers": [],
                "numeric_warning": "",
                "elapsed_ms": elapsed,
                "timings": {"total_ms": elapsed},
                "disclaimer": prompts.DISCLAIMER,
            },
        }
        return
    timings: dict[str, float] = {}

    # ---------- ② 查询改写 ----------
    t0 = time.perf_counter()
    rewritten_query, rewritten, rewrite_ms = rewrite_query(question, history)
    timings["rewrite_ms"] = round(rewrite_ms, 1)
    if rewritten:
        timings["rewrite_total_ms"] = round((time.perf_counter() - t0) * 1000, 1)

    # ---------- ③ 意图识别与分期路由 ----------
    phase = phase_override
    intent = "knowledge"
    confidence = 0.0
    if use_intent_routing and not phase_override:
        t0 = time.perf_counter()
        phase, intent, confidence = classify_intent(rewritten_query, history)
        timings["intent_ms"] = round((time.perf_counter() - t0) * 1000, 1)
        if confidence < 0.6:
            phase = None

    yield {
        "type": "meta",
        "data": {
            "session_id": conv.session_id,
            "question": question,
            "rewritten_query": rewritten_query if rewritten else "",
            "rewritten": rewritten,
            "is_followup": is_followup,
            "phase": phase or "general",
            "intent": intent,
            "intent_confidence": confidence,
        },
    }

    # ---------- ④ 标准 RAG 链路（流式） ----------
    final_answer = ""
    final_citations: list = []
    final_invalid: list = []
    final_unsupported: list = []
    final_warning = ""
    final_model = ""
    final_disclaimer = prompts.DISCLAIMER

    try:
        for event in rag_service.answer_stream(
            db, QaRequest(question=rewritten_query, top_k=top_k, phase=phase)
        ):
            etype = event.get("type")

            if etype == "done":
                data = event.get("data") or {}
                final_answer = data.get("answer", "")
                final_citations = data.get("citations") or []
                final_invalid = data.get("invalid_citations") or []
                final_unsupported = data.get("unsupported_numbers") or []
                final_warning = data.get("numeric_warning") or ""
                final_model = data.get("model") or ""
                final_disclaimer = data.get("disclaimer") or prompts.DISCLAIMER

                timings["total_ms"] = round((time.perf_counter() - t_start) * 1000, 1)

                # 记录本轮对话（必须在 done 时写，此时才拿到完整答案）
                conv.add_turn(
                    ConversationTurn(
                        question=question,
                        answer=final_answer,
                        rewritten_query=rewritten_query if rewritten else "",
                        phase=phase or "general",
                        intent=intent,
                        citations_count=len(final_citations),
                        citations=final_citations,
                        timestamp=datetime.now(timezone.utc).isoformat(),
                    )
                )

                # 把会话信息并入 done，前端一次拿全
                merged = dict(data)
                merged.update(
                    {
                        "session_id": conv.session_id,
                        "question": question,
                        "rewritten_query": rewritten_query if rewritten else "",
                        "rewritten": rewritten,
                        "is_followup": is_followup,
                        "phase": phase or "general",
                        "intent": intent,
                        "intent_confidence": confidence,
                        "timings": timings,
                        "disclaimer": final_disclaimer,
                    }
                )

                # 写问答日志（mode=chat）。
                #
                # 此前只有单轮 /api/qa 落 sys_qa_log，多轮与影像都不写，
                # 管理端的质量监控只统计单轮 —— mode 永远不出现 chat/image，
                # by_mode 分布、追问质量、幻觉率都缺一大块数据。
                # 日志失败不影响本次回答：吞掉异常并记录告警。
                # db 为空时（单元测试会这样调用）跳过写日志。
                if db is not None:
                    try:
                        qa_logging.log_qa(
                            db,
                            user=user,
                            question=question,
                            answer=final_answer,
                            citations=final_citations,
                            invalid_citations=data.get("invalid_citations") or [],
                            unsupported_numbers=data.get("unsupported_numbers") or [],
                            evidences=[],
                            timings=timings,
                            model=final_model,
                            session_id=conv.session_id,
                            rewritten_query=rewritten_query if rewritten else None,
                            phase=phase or "general",
                            intent=intent,
                            mode="chat",
                            has_evidence=bool(final_citations),
                        )
                        db.commit()
                    except Exception as log_exc:  # noqa: BLE001
                        logger.warning("多轮问答日志写入失败（不影响回答）: %s",
                                       str(log_exc)[:150])
                        try:
                            db.rollback()
                        except Exception:  # noqa: BLE001
                            pass
                    db.rollback()

                yield {"type": "done", "data": merged}
            else:
                # retrieval / delta / error 原样透传
                yield event
    except Exception as exc:  # noqa: BLE001
        # 只把异常抛给路由层，由它统一发一条 error。
        #
        # 此前这里先 yield 一条 error 再 raise，路由层（chat.py）捕获后又 yield 一条，
        # 前端会收到两条 error：按"收到即终止"处理的实现忽略第二条，按计数或
        # 按事件流展示的实现会显示两次错误。
        raise RuntimeError(f"{type(exc).__name__}: {exc}") from exc


def chat(
    db: Session,
    question: str,
    session_id: str | None = None,
    *,
    top_k: int = 5,
    use_intent_routing: bool = True,
    phase_override: str | None = None,
    owner_key: str = 'internal',
) -> ChatResult:
    """多轮问答主流程。

    流程：
      ① 取会话历史
      ② 查询改写（若为追问）
      ③ 意图识别 → 分期路由
      ④ 走标准 RAG 问答链路（检索 → 生成 → 引用解析 → 数值核验）
      ⑤ 记录本轮对话
    """
    t_start = time.perf_counter()
    store = get_store()
    conv = store.get_or_create(session_id, owner_key)
    history = conv.history()
    is_followup = bool(history)


    # ---------- ⓪ 助手指令 / 身份类问题（不检索） ----------
    if meta_intent.is_meta_question(question):
        answer_text = meta_intent.build_meta_answer(
            search_repo.count_active_documents(db),
            search_repo.count_active_chunks(db),
        )
        elapsed = round((time.perf_counter() - t_start) * 1000, 1)
        conv.add_turn(
            ConversationTurn(
                question=question,
                answer=answer_text,
                phase="general",
                intent="meta",
                citations_count=0,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
        )
        return ChatResult(
            session_id=conv.session_id,
            question=question,
            rewritten_query="",
            answer=answer_text,
            citations=[],
            invalid_citations=[],
            unsupported_numbers=[],
            numeric_warning="",
            evidences=[],
            phase="general",
            intent="meta",
            intent_confidence=1.0,
            rewritten=False,
            is_followup=is_followup,
            timings={"total_ms": elapsed},
            model="",
            disclaimer=prompts.DISCLAIMER,
        )
    timings: dict[str, float] = {}

    # ---------- ② 查询改写 ----------
    t0 = time.perf_counter()
    rewritten_query, rewritten, rewrite_ms = rewrite_query(question, history)
    timings["rewrite_ms"] = round(rewrite_ms, 1)
    if rewritten:
        timings["rewrite_total_ms"] = round((time.perf_counter() - t0) * 1000, 1)

    # ---------- ③ 意图识别与分期路由 ----------
    phase = phase_override
    intent = "knowledge"
    confidence = 0.0
    if use_intent_routing and not phase_override:
        t0 = time.perf_counter()
        phase, intent, confidence = classify_intent(rewritten_query, history)
        timings["intent_ms"] = round((time.perf_counter() - t0) * 1000, 1)
        # 仅在置信度足够时才启用分期过滤。
        # 低置信度下强制过滤会漏掉跨分期的正确证据
        # （例如问题提到"术后"，但关键证据在通用指南中）。
        if confidence < 0.6:
            phase = None

    # ---------- ④ 标准 RAG 链路 ----------
    t0 = time.perf_counter()
    qa_result = rag_service.answer(
        db,
        QaRequest(
            question=rewritten_query,
            top_k=top_k,
            phase=phase,
        ),
    )
    timings["qa_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    timings["total_ms"] = round((time.perf_counter() - t_start) * 1000, 1)

    # ---------- ⑤ 记录对话 ----------
    turn = ConversationTurn(
        question=question,
        answer=qa_result.answer,
        rewritten_query=rewritten_query if rewritten else "",
        phase=phase or "general",
        intent=intent,
        citations_count=len(qa_result.citations),
        citations=[c.to_dict() for c in qa_result.citations],
        timestamp=datetime.now(timezone.utc).isoformat(),
    )
    conv.add_turn(turn)

    return ChatResult(
        session_id=conv.session_id,
        question=question,
        rewritten_query=rewritten_query,
        answer=qa_result.answer,
        citations=qa_result.citations,
        invalid_citations=qa_result.invalid_citations,
        unsupported_numbers=qa_result.unsupported_numbers,
        numeric_warning=qa_result.numeric_warning,
        evidences=qa_result.evidences,
        phase=phase or "general",
        intent=intent,
        intent_confidence=confidence,
        rewritten=rewritten,
        is_followup=is_followup,
        timings=timings,
        model=qa_result.model,
        disclaimer=qa_result.disclaimer,
    )
