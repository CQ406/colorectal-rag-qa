"""多轮对话接口（阶段七）。

端点
----
  POST   /api/chat                    多轮问答（自动维护会话上下文）
  GET    /api/chat/stats              会话统计
  GET    /api/chat/{session_id}       查看会话历史
  DELETE /api/chat/{session_id}       清除会话

为什么把多轮对话做成独立端点而非给 /api/qa 加参数
--------------------------------------------------
两者的状态语义不同：`/api/qa` 是无状态单轮问答（便于批量评测与脚本调用），
`/api/chat` 是有状态会话。混在一起会让评测脚本意外携带上下文，
导致评测结果不可复现。分开后职责清晰：

  - `/api/qa`    评测、脚本、单次查询
  - `/api/chat`  交互式对话

响应中包含 `rewritten_query` 与 `is_followup`，把系统的上下文理解过程
对用户透明化 —— 这在答辩时也便于演示"系统确实解析了指代"。
"""

from __future__ import annotations

import json
import hashlib
import re
import secrets

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.db.session import get_db
from app.api.deps import get_current_user
from app.schemas.common import (
    ChatRequest,
    ChatResponse,
    ChatStatsResponse,
    ChatTurnItem,
    CitationItem,
    ConversationDetail,
)
from app.services import conversation
from app.services.llm_client import LLMError

logger = get_logger("api.chat")

router = APIRouter(prefix="/chat", tags=["多轮对话"])


def conversation_owner(request: Request, response: Response, db: Session = Depends(get_db)) -> str:
    """Invalid supplied credentials must not silently downgrade to a visitor."""
    authorization = request.headers.get('authorization')
    if authorization:
        user = get_current_user(db, authorization)
        return f'user:{user.id}'
    value = request.cookies.get('cr_chat_owner', '')
    if not re.fullmatch(r'[0-9a-f]{64}', value):
        value = secrets.token_hex(32)
        response.set_cookie('cr_chat_owner', value, httponly=True, samesite='strict',
            secure=request.url.scheme == 'https', max_age=60*60*24*30, path='/api/chat')
    return 'guest:' + hashlib.sha256(value.encode()).hexdigest()


def _session(session_id: str | None, owner_key: str):
    try:
        return conversation.get_store().get_or_create(session_id, owner_key)
    except conversation.ConversationNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("", response_model=ChatResponse, summary="多轮对话问答")
def chat(req: ChatRequest, db: Session = Depends(get_db), owner_key: str = Depends(conversation_owner)) -> ChatResponse:
    """上下文感知的多轮问答。

    流程：查询改写（解析指代）→ 意图识别（分期路由）→ RAG 问答 → 记录历史

    首次调用不传 `session_id`，响应会返回新建的 ID；
    后续追问带上该 ID 即可保持上下文。

    若问题中包含指代（如"它的发生率是多少"），系统会先结合历史
    改写为独立查询再检索 —— 响应中的 `rewritten_query` 会展示改写结果。
    """
    conv = _session(req.session_id, owner_key)
    try:
        result = conversation.chat(
            db,
            req.question,
            session_id=conv.session_id,
            owner_key=owner_key,
            top_k=req.top_k,
            use_intent_routing=req.use_intent_routing,
            phase_override=req.phase,
        )
    except LLMError as exc:
        raise HTTPException(status_code=503, detail=f"LLM 不可用: {exc}") from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=500, detail=f"对话失败: {type(exc).__name__}: {exc}"
        ) from exc

    return ChatResponse(
        session_id=result.session_id,
        question=result.question,
        answer=result.answer,
        citations=[CitationItem(**c.to_dict()) for c in result.citations],
        invalid_citations=result.invalid_citations,
        unsupported_numbers=result.unsupported_numbers,
        numeric_warning=result.numeric_warning,
        rewritten_query=result.rewritten_query if result.rewritten else "",
        rewritten=result.rewritten,
        is_followup=result.is_followup,
        phase=result.phase,
        intent=result.intent,
        intent_confidence=result.intent_confidence,
        evidence_count=len(result.evidences),
        has_evidence=bool(result.evidences),
        model=result.model,
        timings=result.timings,
        disclaimer=result.disclaimer,
    )


@router.post("/stream", summary="多轮对话问答（流式 SSE）")
def chat_stream(req: ChatRequest, request: Request, response: Response,
                db: Session = Depends(get_db),
                owner_key: str = Depends(conversation_owner)) -> StreamingResponse:
    """流式多轮问答。

    与 `POST /api/chat` 的流程完全一致（查询改写 → 意图识别 → RAG → 记录历史），
    区别只在于答案逐字返回，前端可以边生成边显示。

    事件序列：
      - `meta`      会话与路由信息（session_id、改写后查询、分期、意图、置信度）
      - `retrieval` 检索完成，含证据列表（前端可立即渲染引用）
      - `delta`     答案增量
      - `done`      完成，含完整答案、引用与核验结果
      - `error`     出错

    前端用法与 `/api/qa/stream` 相同，只是首次调用不传 `session_id`，
    从 `meta` 或 `done` 事件里取新的 `session_id` 供后续追问使用。
    """

    conv = _session(req.session_id, owner_key)

    # 解析当前用户（可匿名）：用于把多轮问答写进 sys_qa_log 的 username。
    # 登录与否不影响功能，只是质量监控里能不能看到操作者。
    current_user = None
    try:
        authorization = request.headers.get("authorization")
        if authorization:
            current_user = get_current_user(db, authorization)
    except Exception:  # noqa: BLE001
        current_user = None

    def event_stream():
        try:
            for event in conversation.chat_stream(
                db,
                req.question,
                session_id=conv.session_id,
                owner_key=owner_key,
                top_k=req.top_k,
                use_intent_routing=req.use_intent_routing,
                phase_override=req.phase,
                user=current_user,
            ):
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        except Exception as exc:  # noqa: BLE001
            logger.warning("多轮流式问答失败: %s", exc)
            payload = {
                "type": "error",
                "data": {"message": f"服务异常: {type(exc).__name__}: {exc}"},
            }
            yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
            **({'Set-Cookie': response.headers['set-cookie']} if 'set-cookie' in response.headers else {}),
        },
    )


@router.get("/stats", response_model=ChatStatsResponse, summary="会话统计")
def chat_stats(owner_key: str = Depends(conversation_owner)) -> ChatStatsResponse:
    """当前活跃会话数与会话总数。

    ⚠️ 路由顺序：必须定义在 `/{session_id}` 之前。
    """
    s = conversation.get_store().stats(owner_key)
    return ChatStatsResponse(**s)


@router.get("/{session_id}", response_model=ConversationDetail, summary="会话历史")
def get_conversation(session_id: str, owner_key: str = Depends(conversation_owner)) -> ConversationDetail:
    conv = conversation.get_store().get(session_id, owner_key)
    if conv is None:
        raise HTTPException(
            status_code=404,
            detail="会话不存在、已清除或不属于当前用户。",
        )
    data = conv.to_dict()
    return ConversationDetail(
        session_id=data["session_id"],
        created_at=data["created_at"],
        updated_at=data["updated_at"],
        turn_count=data["turn_count"],
        turns=[ChatTurnItem(**t) for t in data["turns"]],
    )


@router.delete("/{session_id}", response_model=dict, summary="清除会话")
def clear_conversation(session_id: str, owner_key: str = Depends(conversation_owner)) -> dict:
    existed = conversation.get_store().clear(session_id, owner_key)
    return {"cleared": existed, "session_id": session_id}
