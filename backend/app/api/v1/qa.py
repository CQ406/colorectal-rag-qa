"""问答接口（RAG 闭环）。

两个端点：
  POST /api/qa        非流式，返回完整答案（含 token 用量，便于统计）
  POST /api/qa/stream 流式 SSE，逐字返回（前端体验好）
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.api.deps import get_optional_user
from app.db.session import get_db
from app.schemas.common import QaRequest, QaResponse, CitationItem
from app.models.system import User
from app.services import qa_logging, rag_service
from app.services.llm_client import LLMError
from app.services.search_service import EmbeddingInvalidError

router = APIRouter(tags=["问答"])


@router.post("/qa", response_model=QaResponse, summary="临床问答（非流式）")
def qa(
    req: QaRequest,
    db: Session = Depends(get_db),
    user: User | None = Depends(get_optional_user),
) -> QaResponse:
    """依据知识库检索结果生成带引用溯源的答案。

    流程：多路召回 → RRF 融合 → 重排 → LLM 生成 → 引用解析

    要点：
    - 答案正文中的 `[n]` 对应响应里 `citations[n-1]`
    - `invalid_citations` 记录 LLM 引用但不存在的编号（幻觉信号）
    - 检索不到证据或相关度过低时**不调用 LLM**，直接返回提示
      （防止模型在无依据情况下依赖内部知识作答）
    """
    try:
        result = rag_service.answer(db, req)
    except EmbeddingInvalidError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except LLMError as exc:
        raise HTTPException(status_code=503, detail=f"LLM 不可用: {exc}") from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=500, detail=f"问答失败: {type(exc).__name__}: {exc}"
        ) from exc

    # 记录问答日志（阶段九质量监控）。日志写入失败不影响本次回答。
    qa_logging.log_qa(
        db,
        user=user,
        question=req.question,
        answer=result.answer,
        citations=result.citations,
        invalid_citations=result.invalid_citations,
        unsupported_numbers=result.unsupported_numbers,
        evidences=result.evidences,
        timings=result.timings,
        model=result.model,
        phase=req.phase,
        mode="qa",
        has_evidence=result.has_evidence,
    )
    db.commit()

    return QaResponse(
        question=result.question,
        answer=result.answer,
        citations=[CitationItem(**c.to_dict()) for c in result.citations],
        invalid_citations=result.invalid_citations,
        unsupported_numbers=result.unsupported_numbers,
        numeric_warning=result.numeric_warning,
        has_evidence=result.has_evidence,
        evidence_count=len(result.evidences),
        model=result.model,
        timings=result.timings,
        usage=result.usage,
        disclaimer=result.disclaimer,
    )


@router.post("/qa/stream", summary="临床问答（流式 SSE）")
def qa_stream(
    req: QaRequest,
    db: Session = Depends(get_db),
    user: User | None = Depends(get_optional_user),
) -> StreamingResponse:
    """流式问答，返回 Server-Sent Events。

    事件类型：
    - `retrieval` 检索完成，含证据列表与引用元数据（前端可立即渲染引用面板）
    - `delta`     答案文本增量（逐字显示）
    - `done`      完成，含完整答案与解析出的引用列表
    - `error`     出错（含无证据、相关度不足、LLM 故障三种原因）

    前端用法::

        const es = new EventSource(...)  // 或用 fetch + ReadableStream
        // 每条事件形如：data: {"type":"delta","data":{"text":"吻合"}}
    """

    def event_stream():
        try:
            for event in rag_service.answer_stream(db, req):
                # done 事件里包含完整答案与最终引用，
                # 是记录问答日志的最佳时机（此时才拿到全部指标）。
                if event.get("type") == "done":
                    data = event.get("data") or {}
                    qa_logging.log_qa(
                        db,
                        user=user,
                        question=req.question,
                        answer=data.get("answer", ""),
                        citations=data.get("citations") or [],
                        invalid_citations=data.get("invalid_citations") or [],
                        unsupported_numbers=data.get("unsupported_numbers") or [],
                        timings={"total_ms": data.get("elapsed_ms")},
                        model=data.get("model"),
                        phase=req.phase,
                        mode="qa_stream",
                        has_evidence=bool(data.get("citations")),
                    )
                    # 流式响应中必须显式提交：整个生成器在请求上下文之外执行，
                    # 依赖 session_scope 的自动提交时机不可控。
                    db.commit()

                # SSE 格式：每条消息以 data: 开头，以两个换行结束
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        except Exception as exc:  # noqa: BLE001
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
            # 禁用 nginx 等反向代理的缓冲，保证逐字推送
            "X-Accel-Buffering": "no",
        },
    )
