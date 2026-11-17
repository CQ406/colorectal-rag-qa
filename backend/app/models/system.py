"""用户、审计与问答日志的 ORM 模型 —— 对应报审表「（四）系统管理与用户模块」。

  User         用户账号（角色：admin 管理员 / physician 医师）
  AuditLog     操作审计日志（谁在何时做了什么）
  QaLog        问答日志（记录每次问答，支撑质量监控与模型优化）
  QaFeedback   人工反馈（针对某次问答的评价，用于持续优化）
  SystemConfig 系统配置项（模型参数、检索策略等运行参数）

设计要点
--------
**为什么审计日志与问答日志分表**
  两者用途不同：AuditLog 记录**谁改了什么**（写操作，追溯责任），
  QaLog 记录**系统回答了什么**（读操作，质量分析）。
  合并会导致两类查询互相拖慢（问答日志量远大于审计日志），
  且会让「查询他人操作记录」与「查询问答质量」两种权限难以分离。

**为什么问答日志不存完整答案**
  答案正文可能上千字，逐条存储会让日志表迅速膨胀。
  实际需要的是：问题、引用数量、耗时、幻觉引用数、忠实度评分等**指标**，
  以及用于回溯的会话与文档标识。完整答案已可通过 trace_id 关联前端会话。
  因此 QaLog 只保存 answer_chars 与 answer_digest（内容哈希），
  既能校验一致性又不会占用大量空间。

**密码存储**
  只存哈希，永不存明文；哈希算法与参数见 app/core/security.py。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy import JSON
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDMixin


class ChatSession(TimestampMixin, Base):
    """Durable conversation, scoped to an authenticated user or anonymous cookie."""

    __tablename__ = 'sys_chat_session'
    session_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_key: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    turns: Mapped[list] = mapped_column(JSON().with_variant(JSONB, 'postgresql'), nullable=False, default=list)


# ============================================================
#  用户与权限
# ============================================================
class User(UUIDMixin, TimestampMixin, Base):
    """系统用户账号。

    角色设计（对应报审表「医师、管理员等不同角色的权限配置」）：
      admin      管理员 —— 全部权限，含用户管理与系统配置
      physician  医师   —— 可问答、可上传与维护影像、可提交反馈，
                          不可管理用户与系统配置

    为什么用固定角色而非自由权限位：
      本系统只面向院内两类使用者，自由权限位会显著增加配置复杂度与
      误配风险。角色到权限的映射集中在 app/core/permissions.py，
      需要更细粒度控制时改该映射即可，无需动数据库结构。
    """

    __tablename__ = "sys_user"

    username: Mapped[str] = mapped_column(
        String(64), unique=True, nullable=False, index=True, comment="登录名"
    )
    # 只存哈希；算法与参数见 app/core/security.py
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(100), comment="姓名")
    role: Mapped[str] = mapped_column(
        String(20), default="physician", index=True, comment="admin / physician"
    )
    department: Mapped[str | None] = mapped_column(String(100), comment="科室")
    email: Mapped[str | None] = mapped_column(String(200))

    is_active: Mapped[bool] = mapped_column(
        Boolean, default=True, comment="停用后无法登录（不删除账号以保留审计记录）"
    )
    # 登录审计
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_login_ip: Mapped[str | None] = mapped_column(String(64))
    login_count: Mapped[int] = mapped_column(Integer, default=0)

    # 连续失败锁定：防止口令暴力猜测
    failed_attempts: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    note: Mapped[str | None] = mapped_column(Text, comment="备注")

    __table_args__ = (
        Index("ix_user_role_active", "role", "is_active"),
    )

    @property
    def is_locked(self) -> bool:
        """当前是否处于锁定状态。"""
        from datetime import timezone

        if self.locked_until is None:
            return False
        return self.locked_until > datetime.now(timezone.utc)


# ============================================================
#  审计日志
# ============================================================
class AuditLog(UUIDMixin, Base):
    """操作审计日志。

    记录所有**写操作**（新增、修改、删除）以及登录尝试，
    用于追溯「谁在什么时候改了什么」。这是权限管理的配套要求：
    有权限就必须有审计，否则权限形同虚设。
    """

    __tablename__ = "sys_audit_log"

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), index=True, nullable=False,
        comment="操作时间（按此字段建立索引，审计查询通常按时间倒序）",
    )

    # 操作者：允许为空（登录失败时用户可能不存在）
    user_id: Mapped[str | None] = mapped_column(
        ForeignKey("sys_user.id", ondelete="SET NULL"), index=True
    )
    username: Mapped[str | None] = mapped_column(
        String(64), comment="冗余存储：用户被删后仍可追溯是谁操作的"
    )
    role: Mapped[str | None] = mapped_column(String(20))

    action: Mapped[str] = mapped_column(
        String(50), index=True, comment="login / create / update / delete 等"
    )
    resource: Mapped[str] = mapped_column(
        String(50), index=True, comment="user / image / config / document 等"
    )
    resource_id: Mapped[str | None] = mapped_column(String(64))
    detail: Mapped[dict | None] = mapped_column(JSONB, comment="变更详情")

    ip_address: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(300))
    success: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    error_message: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        Index("ix_audit_resource_time", "resource", "created_at"),
        Index("ix_audit_user_time", "user_id", "created_at"),
    )


# ============================================================
#  问答质量监控
# ============================================================
class QaLog(UUIDMixin, Base):
    """问答日志（质量监控的数据基础）。

    记录每次问答的**指标**而非全文（见模块 docstring 的说明）。
    支撑三类分析：
      1. 使用情况：谁在用、问了多少、集中在哪个围术期分期
      2. 质量指标：引用数、幻觉引用数、无据数值数、各阶段耗时
      3. 优化依据：低分问题聚类后用于补充语料或调整检索策略
    """

    __tablename__ = "sys_qa_log"

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), index=True, nullable=False
    )
    user_id: Mapped[str | None] = mapped_column(
        ForeignKey("sys_user.id", ondelete="SET NULL"), index=True
    )
    username: Mapped[str | None] = mapped_column(String(64))

    # 会话标识：多轮对话的追问共享同一 session_id，便于还原对话
    session_id: Mapped[str | None] = mapped_column(String(64), index=True)
    # 端到端追踪标识：同一 trace_id 关联检索、生成、日志各环节
    trace_id: Mapped[str | None] = mapped_column(String(64), index=True)

    question: Mapped[str] = mapped_column(Text, nullable=False)
    # 多轮对话中经改写后的检索查询（与原始问题不同时记录，便于分析改写效果）
    rewritten_query: Mapped[str | None] = mapped_column(Text)

    phase: Mapped[str | None] = mapped_column(String(20), index=True)
    intent: Mapped[str | None] = mapped_column(String(20))
    mode: Mapped[str] = mapped_column(
        String(20), default="qa", comment="qa（单轮）/ chat（多轮）/ image（影像）"
    )

    # ---- 质量指标 ----
    answer_chars: Mapped[int] = mapped_column(Integer, default=0, comment="答案长度")
    # 答案内容摘要哈希：用于校验前端展示内容与日志一致，不存全文以控制体积
    answer_digest: Mapped[str | None] = mapped_column(String(64))
    citation_count: Mapped[int] = mapped_column(Integer, default=0)
    invalid_citations: Mapped[int] = mapped_column(
        Integer, default=0, comment="LLM 编造的引用编号数（幻觉信号）"
    )
    unsupported_numbers: Mapped[int] = mapped_column(
        Integer, default=0, comment="证据中找不到的数值个数"
    )
    evidence_count: Mapped[int] = mapped_column(Integer, default=0)
    has_evidence: Mapped[bool] = mapped_column(Boolean, default=True, index=True)

    # ---- 性能指标（毫秒）----
    retrieval_ms: Mapped[float | None] = mapped_column(Float)
    generation_ms: Mapped[float | None] = mapped_column(Float)
    total_ms: Mapped[float | None] = mapped_column(Float, index=True)

    model: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(Text)

    feedback: Mapped["QaFeedback | None"] = relationship(
        back_populates="qa_log", uselist=False, cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("ix_qalog_created_user", "created_at", "user_id"),
        Index("ix_qalog_phase_created", "phase", "created_at"),
    )


class QaFeedback(UUIDMixin, TimestampMixin, Base):
    """人工反馈。

    报审表要求「支持人工反馈与模型效果持续优化」。
    反馈与问答日志一对一：一条问答最多一条反馈（重复提交视为修改）。
    """

    __tablename__ = "sys_qa_feedback"

    qa_log_id: Mapped[str] = mapped_column(
        ForeignKey("sys_qa_log.id", ondelete="CASCADE"),
        unique=True, index=True, nullable=False,
    )
    user_id: Mapped[str | None] = mapped_column(
        ForeignKey("sys_user.id", ondelete="SET NULL")
    )

    # 评分采用 1~5 分制而非简单好评/差评：
    # 二值反馈无法区分「略有不足」与「完全错误」，
    # 而这两者对优化方向的指示完全不同（前者调检索，后者要查幻觉）。
    rating: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="1~5 分，5 为最满意"
    )
    # 分类问题标签，便于聚合统计与定向优化
    issue_type: Mapped[str | None] = mapped_column(
        String(30),
        comment="accurate / incomplete / irrelevant / hallucination / citation_error / other",
    )
    comment: Mapped[str | None] = mapped_column(Text)
    # 是否已被管理员处理（用于跟踪反馈闭环）
    is_resolved: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    resolved_note: Mapped[str | None] = mapped_column(Text)

    qa_log: Mapped[QaLog] = relationship(back_populates="feedback")

    __table_args__ = (
        Index("ix_feedback_rating", "rating"),
        Index("ix_feedback_issue", "issue_type"),
    )


# ============================================================
#  系统配置
# ============================================================
class SystemConfig(UUIDMixin, TimestampMixin, Base):
    """系统运行配置项。

    采用「配置表 + 环境变量默认值」两层结构：
      - 环境变量（.env）提供部署级默认值，重启生效
      - 本表提供**运行时可调**参数，管理员在界面上修改后立即生效

    为什么需要运行时可调：
      检索权重（关键词权重、图谱权重）等参数需要根据实际语料反复调试。
      若每次调整都要改 .env 并重启服务，调试成本过高
      （后端启动含模型加载，一次约 60 秒）。
    """

    __tablename__ = "sys_config"

    key: Mapped[str] = mapped_column(
        String(100), unique=True, nullable=False, index=True
    )
    value: Mapped[str | None] = mapped_column(Text, comment="统一以字符串存储，读取时按类型转换")
    value_type: Mapped[str] = mapped_column(
        String(20), default="str", comment="str / int / float / bool / json"
    )
    category: Mapped[str] = mapped_column(
        String(30), default="general", index=True,
        comment="retrieval / model / system / ui",
    )
    label: Mapped[str | None] = mapped_column(String(200), comment="界面显示名")
    description: Mapped[str | None] = mapped_column(Text, comment="参数说明与调优建议")
    # 取值区间（数值型参数用），用于前端校验与后端兜底
    min_value: Mapped[float | None] = mapped_column(Float)
    max_value: Mapped[float | None] = mapped_column(Float)
    # 是否为出厂默认值（未被管理员修改过）
    is_default: Mapped[bool] = mapped_column(Boolean, default=True)
    updated_by: Mapped[str | None] = mapped_column(String(64), comment="最后修改人")

    __table_args__ = (
        Index("ix_config_category_key", "category", "key"),
    )
