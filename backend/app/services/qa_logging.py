"""问答日志记录服务（阶段九「问答质量监控模块」）。

为什么单独抽一个模块
--------------------
问答入口有三个（`/api/qa` 单轮、`/api/qa/stream` 流式、`/api/chat` 多轮），
影像解读也可视为一种问答（`/api/images/analyze`）。
若在每个入口各写一遍日志逻辑，会出现：
  - 字段口径不一致（有的记 citation_count，有的漏了）
  - 新增指标时要改四处，容易漏改

因此统一为 `log_qa()` 一个函数，各入口只负责把已有结果传进来。

设计原则
--------
**日志失败绝不影响问答**：日志是观测手段，不是业务链路的一部分。
任何写入异常都被捕获并降级为告警，用户仍能正常拿到答案。

**关闭开关**：`system.qa_log_enabled` 可关闭日志记录。
关闭后质量监控将无数据，因此默认开启；仅在性能敏感或隐私要求下关闭。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.models.system import User
from app.repositories import qa_log_repo

logger = get_logger("services.qa_logging")


def log_qa(
    db: Session,
    *,
    user: User | None,
    question: str,
    answer: str = "",
    citations: list[Any] | None = None,
    invalid_citations: list[int] | None = None,
    unsupported_numbers: list[str] | None = None,
    evidences: list[dict] | None = None,
    timings: dict | None = None,
    model: str | None = None,
    session_id: str | None = None,
    trace_id: str | None = None,
    rewritten_query: str | None = None,
    phase: str | None = None,
    intent: str | None = None,
    mode: str = "qa",
    has_evidence: bool = True,
    error_message: str | None = None,
) -> None:
    """记录一次问答。

    参数与 QaAnswer / ChatResult 的字段一一对应，调用方直接透传即可。
    """
    try:
        from app.repositories import config_repo

        # 尊重配置开关（读取失败时按开启处理，不因配置问题丢日志）
        try:
            enabled = config_repo.get_value(db, "system.qa_log_enabled")
        except Exception:  # noqa: BLE001
            enabled = True
        if enabled is False:
            return

        timings = timings or {}
        qa_log_repo.record(
            db,
            question=question,
            user_id=str(user.id) if user is not None else None,
            username=user.username if user is not None else None,
            session_id=session_id,
            trace_id=trace_id,
            rewritten_query=rewritten_query,
            phase=phase,
            intent=intent,
            mode=mode,
            answer=answer,
            citation_count=len(citations or []),
            invalid_citations=len(invalid_citations or []),
            unsupported_numbers=len(unsupported_numbers or []),
            evidence_count=len(evidences or []),
            has_evidence=has_evidence,
            retrieval_ms=timings.get("retrieval_ms"),
            generation_ms=timings.get("generation_ms"),
            total_ms=timings.get("total_ms"),
            model=model,
            error_message=error_message,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("问答日志记录失败（不影响回答）: %s", str(exc)[:150])
