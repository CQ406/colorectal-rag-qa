"""知识库版本管理：更新批次模型。

对应报审表「知识库维护模块：提供知识的审核、更新、版本管理和质量评估功能」
中的**版本管理**部分。

为什么要记录批次，而不是只看文献总数
------------------------------------
只知道「现在有 444 篇」无法回答下面这些问题，而这些恰恰是维护知识库时
最需要回答的：

  - 这次新增的 50 篇是**什么检索式**采集的？（判断语料是否偏斜）
  - 上次更新是**什么时候**？（判断语料是否过时）
  - 新增的文献**向量化成功了吗**？（失败的话检索不到，但总数看着正常）
  - 某篇文献是**哪次更新带进来的**？（追溯来源）

因此每次更新都记录一条批次，包含来源、参数、结果计数与耗时。
这相当于给知识库做一个**追加式的变更日志**。

为什么不引入真正的版本快照
--------------------------
完整版本管理（每次更新存一份全量快照、支持回滚）代价很高：
444 篇的向量数据约 1661×1024 维浮点，快照既占空间又拖慢每次写入。
本课题的实际需求是「可追溯 + 可评估」，批次日志已满足；
需要回滚时可按批次删除该批新增的文献（`batch_id` 关联可支持这一点）。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDMixin


# 批次状态
BATCH_PENDING = "pending"
BATCH_RUNNING = "running"
BATCH_SUCCESS = "success"
BATCH_FAILED = "failed"
BATCH_CANCELLED = "cancelled"


class KbUpdateBatch(UUIDMixin, TimestampMixin, Base):
    """一次知识库更新的记录。

    生命周期：
      pending → running → success / failed

    **为什么允许 pending 状态**
      批量采集可能耗时数十分钟（现有 444 篇采集 + 向量化约 1 小时）。
      采用后台任务执行，创建批次时立即返回一个 pending 记录，
      界面据此轮询进度，而不是让 HTTP 请求挂住几十分钟。
    """

    __tablename__ = "kb_update_batch"

    # 操作类型：collect（按检索式采集）/ search（自定义检索式）/ vectorize（补向量）
    operation: Mapped[str] = mapped_column(String(20), nullable=False, index=True)

    status: Mapped[str] = mapped_column(
        String(20), default=BATCH_PENDING, nullable=False, index=True
    )

    # ---- 本次更新的参数（用于复现）----
    # 采集用的检索式；向量化任务为空
    query: Mapped[str | None] = mapped_column(Text)
    params: Mapped[dict | None] = mapped_column(
        JSONB, comment="完整参数快照：limit / mindate / max_total / batch_size 等"
    )

    # ---- 执行结果 ----
    documents_created: Mapped[int] = mapped_column(Integer, default=0)
    documents_updated: Mapped[int] = mapped_column(Integer, default=0)
    documents_skipped: Mapped[int] = mapped_column(Integer, default=0)
    chunks_created: Mapped[int] = mapped_column(Integer, default=0)
    chunks_embedded: Mapped[int] = mapped_column(Integer, default=0)

    # 向量化任务的结果（该任务只处理块，不新增文献）
    chunks_vectorized: Mapped[int] = mapped_column(Integer, default=0)

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    elapsed_seconds: Mapped[float | None] = mapped_column(Float)

    # 错误摘要：只存前若干条，避免一个坏批次写入巨量文本
    errors: Mapped[list | None] = mapped_column(
        JSONB, comment="错误信息列表（截断至 20 条）"
    )
    error_message: Mapped[str | None] = mapped_column(Text, comment="致命错误")

    # 是否已重建知识图谱。
    # 单独记一个标志的原因：新入库的文献**不会自动进图谱**，
    # 图谱路径会看不到它们。维护界面需要据此提醒管理员。
    graph_rebuilt: Mapped[bool] = mapped_column(
        default=False, comment="本次更新后是否已重建知识图谱"
    )

    operator_id: Mapped[str | None] = mapped_column(
        ForeignKey("sys_user.id", ondelete="SET NULL"), index=True
    )
    operator_name: Mapped[str | None] = mapped_column(String(64))

    note: Mapped[str | None] = mapped_column(Text, comment="管理员备注")

    __table_args__ = (
        Index("ix_batch_status_created", "status", "created_at"),
    )

    @property
    def is_running(self) -> bool:
        return self.status in (BATCH_PENDING, BATCH_RUNNING)
