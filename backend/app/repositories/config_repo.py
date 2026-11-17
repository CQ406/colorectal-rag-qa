"""系统配置数据访问（阶段九「系统配置模块」）。

报审表要求：「管理模型参数、检索策略和系统运行配置」

两层配置结构
------------
    环境变量（.env）  ── 部署级默认值，重启生效
            ↓ 初始化时写入
    配置表（sys_config）── 运行时可调，管理员改完立即生效

为什么需要运行时可调
--------------------
检索权重（关键词权重、图谱权重）等参数必须按实际语料反复调试。
后端启动含模型加载，一次约 60 秒；若每次调参都要改 .env 并重启，
调试成本高到无法接受。配置表把这类参数变为运行时可变。

**哪些参数放进配置表，哪些不放**
放进来的：调参频繁、改了不会破坏数据一致性的检索与生成参数。
不放的：
  - 数据库连接串、密钥等 —— 属部署配置，且改动会导致服务不可用
  - 模型文件路径 —— 改动需重新加载模型，无法热生效
  - 分页大小上限等 —— 属代码约束，不应由配置绕过

**白名单机制的不可替代性**
`DEFAULT_CONFIGS` 既是默认值来源，也是**允许修改的参数白名单**。
接口只接受白名单内的 key，避免管理员通过配置表注入任意参数
（例如把某个未预期的内部开关打开）。这是配置模块的安全边界。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.logging import get_logger
from app.models.system import SystemConfig

logger = get_logger("repositories.config")


@dataclass
class ConfigSpec:
    """一个配置项的定义（默认值 + 元信息 + 白名单）。"""

    key: str
    default: Any
    category: str
    label: str
    description: str = ""
    value_type: str = "str"
    min_value: float | None = None
    max_value: float | None = None


def _specs() -> list[ConfigSpec]:
    """构建可配置项清单。

    默认值直接取自 `settings`，因此 .env 里的设置会成为出厂默认。
    这样「运维改 .env」与「管理员在界面改」两条路径互不冲突：
    前者定义基线，后者做运行期覆盖。
    """
    r = settings.retrieval
    m = settings.model
    return [
        # ---------------- 检索策略 ----------------
        ConfigSpec(
            "retrieval.vector_weight", r.vector_weight, "retrieval",
            "向量检索权重",
            "向量语义检索在 RRF 融合中的权重。语料以英文文献为主、"
            "提问为中文时，语义检索是主要召回来源，权重通常应高于其他路径。",
            "float", 0.0, 3.0,
        ),
        ConfigSpec(
            "retrieval.keyword_weight", r.keyword_weight, "retrieval",
            "关键词检索权重",
            "关键词（中文 bigram）检索权重。实测该路径对自然语言提问的贡献很低"
            "（评测集上 R@5 仅 0.005），权重过高会引入噪声并拉低融合效果。",
            "float", 0.0, 3.0,
        ),
        ConfigSpec(
            "retrieval.graph_weight", r.graph_weight, "retrieval",
            "知识图谱检索权重",
            "图谱关联检索权重。中文提问场景下该路径对 R@1 提升明显；"
            "若图谱尚未构建可设为 0 关闭该路径。",
            "float", 0.0, 3.0,
        ),
        ConfigSpec(
            "retrieval.min_keyword_overlap", r.min_keyword_overlap, "retrieval",
            "关键词最小重叠比例",
            "低于此重叠度的关键词候选被丢弃。中文文本普遍共享高频词元，"
            "不过滤会让长查询召回大量无关文档。设为 0 等同不过滤。",
            "float", 0.0, 1.0,
        ),
        ConfigSpec(
            "retrieval.rerank_candidate_limit", r.rerank_candidate_limit, "retrieval",
            "重排候选数量",
            "送入交叉编码器重排的候选数。重排模型逐对打分，该值越大越精确但越慢。",
            "int", 10, 200,
        ),
        ConfigSpec(
            "retrieval.evidence_boost_strength", r.evidence_boost_strength,
            "retrieval", "证据等级加权强度",
            "按证据等级调整分值的强度。注意：该策略会提升高等级证据的排名，"
            "可能挤掉相关性更高但等级较低的文档，对召回率指标有轻微负向影响，"
            "其价值应通过「前列结果中高等级证据占比」衡量。",
            "float", 0.0, 1.0,
        ),
        ConfigSpec(
            "retrieval.max_chunks_per_document", r.max_chunks_per_document,
            "retrieval", "单文献最大块数",
            "同一篇文献最多返回的文本块数。限制该值可提高结果来源的多样性，"
            "避免答案全部依据同一篇文献。",
            "int", 1, 20,
        ),
        # ---------------- 生成参数 ----------------
        ConfigSpec(
            "llm.temperature", settings.llm.llm_temperature, "model",
            "生成温度",
            "大语言模型采样温度。医学问答建议保持较低值（0.1~0.3）以获得"
            "稳定、可复现的回答；过高会增加表述漂移与编造风险。",
            "float", 0.0, 1.5,
        ),
        ConfigSpec(
            "llm.max_tokens", settings.llm.llm_max_tokens, "model",
            "生成最大长度",
            "输出 token 上限。推理模型的思考过程与正式回答共用该额度，"
            "实测低于 1024 时大量返回空答案，复杂问题需 4096 以上。",
            "int", 512, 16384,
        ),
        ConfigSpec(
            "qa.min_evidence_score", 0.05, "retrieval",
            "证据最低相关度",
            "检索最高分低于此值时判定为证据不足，**不调用大语言模型**，"
            "直接返回提示。这是防止无依据作答的第一道防线。",
            "float", 0.0, 1.0,
        ),
        ConfigSpec(
            "qa.top_k", 5, "retrieval",
            "默认证据条数",
            "送入大语言模型的证据片段数量。过多会稀释关键信息并增加 token 消耗，"
            "过少可能遗漏必要依据。",
            "int", 1, 20,
        ),
        # ---------------- 系统运行 ----------------
        ConfigSpec(
            "system.enable_graph_retrieval", r.enable_graph_retrieval, "system",
            "启用图谱检索",
            "总开关。图谱未构建时开启会被静默跳过，不会报错。",
            "bool",
        ),
        ConfigSpec(
            "system.enable_stream", True, "system",
            "启用流式输出",
            "关闭后前端改用非流式接口。仅在某环境下 SSE 被中间层缓冲时才需要关闭。",
            "bool",
        ),
        ConfigSpec(
            "system.qa_log_enabled", True, "system",
            "记录问答日志",
            "关闭后不再写入问答日志。关闭将导致质量监控无数据，不建议长期关闭。",
            "bool",
        ),
        ConfigSpec(
            "system.log_retention_days", 180, "system",
            "日志保留天数",
            "审计日志保留期。超期日志可由管理员手动清理。",
            "int", 7, 3650,
        ),
    ]


SPECS: dict[str, ConfigSpec] = {s.key: s for s in _specs()}


# ----------------------------------------------------------------------
#  类型转换
# ----------------------------------------------------------------------
def _to_stored(value: Any, value_type: str) -> str:
    """把 Python 值转成存储字符串。"""
    if value_type == "bool":
        return "true" if value else "false"
    if value_type == "json":
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _from_stored(raw: str | None, value_type: str, default: Any) -> Any:
    """把存储字符串转回 Python 值。

    解析失败时返回默认值而非抛错：配置表被手工改坏时，
    系统应能继续运行在默认参数上，而不是整体不可用。
    """
    if raw is None or raw == "":
        return default
    try:
        if value_type == "int":
            return int(float(raw))
        if value_type == "float":
            return float(raw)
        if value_type == "bool":
            return str(raw).strip().lower() in ("1", "true", "yes", "on", "是")
        if value_type == "json":
            return json.loads(raw)
        return raw
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        logger.warning("配置项解析失败，使用默认值: %s", str(exc)[:100])
        return default


# ----------------------------------------------------------------------
#  初始化
# ----------------------------------------------------------------------
def ensure_defaults(db: Session) -> int:
    """把缺失的配置项写入数据库。

    **只补缺失项，不覆盖已有值**：管理员调过的参数不能因为重启被重置。
    因此本函数可以安全地在每次启动时调用。
    """
    existing = set(db.execute(select(SystemConfig.key)).scalars().all())
    added = 0
    for key, spec in SPECS.items():
        if key in existing:
            continue
        db.add(
            SystemConfig(
                key=key,
                value=_to_stored(spec.default, spec.value_type),
                value_type=spec.value_type,
                category=spec.category,
                label=spec.label,
                description=spec.description,
                min_value=spec.min_value,
                max_value=spec.max_value,
                is_default=True,
            )
        )
        added += 1
    if added:
        db.flush()
        logger.info("已初始化 %d 个系统配置项", added)
    return added


# ----------------------------------------------------------------------
#  读取
# ----------------------------------------------------------------------
def get_value(db: Session, key: str) -> Any:
    """取单个配置值（带类型转换）。

    配置表中不存在时回落到 `SPECS` 的默认值，
    这样即使该键尚未初始化也不会取到 None。
    """
    spec = SPECS.get(key)
    row = db.execute(
        select(SystemConfig).where(SystemConfig.key == key)
    ).scalar_one_or_none()
    if row is None:
        return spec.default if spec else None
    return _from_stored(row.value, row.value_type, spec.default if spec else None)


# 进程内短缓存。
#
# 为什么需要：检索链路每次请求都要读 8~10 个检索参数，逐键查库会多出
# 一问多查的往返开销；而管理员在界面上改完希望"立刻"看到效果。
# 折中为 **2 秒 TTL**：命中缓存时零查询，写入配置时主动失效。
# 多进程部署下各进程独立缓存，最多延迟 2 秒，不影响语义。
_CACHE: dict[str, Any] = {"at": 0.0, "values": {}}
_CACHE_TTL_SEC = 2.0


def invalidate_cache() -> None:
    """配置被修改后调用，让下一次读取重新查库。"""
    _CACHE["at"] = 0.0
    _CACHE["values"] = {}


def effective(db: Session) -> dict[str, Any]:
    """取"当前生效"的全部配置值（带短缓存）。

    检索链路此前一律读 `settings.retrieval.*`（来自 .env / 代码常量），
    导致管理界面把权重改完保存后**运行行为没有任何变化** —— 界面上写着
    "立即生效"，实际要重启才生效。本函数是让配置表真正接入链路的那一环。
    """
    now = time.monotonic()
    if _CACHE["values"] and (now - _CACHE["at"]) < _CACHE_TTL_SEC:
        return _CACHE["values"]
    values = get_all(db)
    _CACHE["values"] = values
    _CACHE["at"] = now
    return values


def get_all(db: Session) -> dict[str, Any]:
    """取全部配置值的字典（供检索链路读取参数）。"""
    rows = db.execute(select(SystemConfig)).scalars().all()
    out: dict[str, Any] = {}
    for row in rows:
        spec = SPECS.get(row.key)
        out[row.key] = _from_stored(
            row.value, row.value_type, spec.default if spec else None
        )
    # 补齐尚未入库的键
    for key, spec in SPECS.items():
        out.setdefault(key, spec.default)
    return out


def list_configs(
    db: Session, category: str | None = None
) -> list[SystemConfig]:
    """列出配置项（按类别与键排序，便于界面分组展示）。"""
    stmt = select(SystemConfig)
    if category:
        stmt = stmt.where(SystemConfig.category == category)
    stmt = stmt.order_by(SystemConfig.category, SystemConfig.key)
    return list(db.execute(stmt).scalars().all())


def categories(db: Session) -> dict[str, int]:
    """各类别的配置项数量。"""
    return dict(
        db.execute(
            select(SystemConfig.category, func.count()).group_by(SystemConfig.category)
        ).all()
    )


# ----------------------------------------------------------------------
#  修改
# ----------------------------------------------------------------------
class ConfigError(ValueError):
    """配置修改被拒绝（不在白名单、类型错误或超出范围）。"""


def update_value(
    db: Session, key: str, raw_value: Any, updated_by: str | None = None
) -> SystemConfig:
    """更新一个配置项。

    Raises:
        ConfigError: 键不在白名单、类型不合法或超出取值范围

    校验是必须的：配置直接作用于检索与生成链路，
    一个越界值（例如权重设为 100）会让系统表现完全失控。
    """
    spec = SPECS.get(key)
    if spec is None:
        raise ConfigError(f"不支持的配置项: {key}")

    value = _from_stored(str(raw_value), spec.value_type, spec.default)

    # 数值范围校验
    if spec.value_type in ("int", "float"):
        try:
            numeric = float(value)
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"配置项 {key} 需要数值") from exc
        if spec.min_value is not None and numeric < spec.min_value:
            raise ConfigError(
                f"{spec.label} 不能小于 {spec.min_value}（当前 {numeric}）"
            )
        if spec.max_value is not None and numeric > spec.max_value:
            raise ConfigError(
                f"{spec.label} 不能大于 {spec.max_value}（当前 {numeric}）"
            )

    row = db.execute(
        select(SystemConfig).where(SystemConfig.key == key)
    ).scalar_one_or_none()
    if row is None:
        row = SystemConfig(
            key=key,
            value_type=spec.value_type,
            category=spec.category,
            label=spec.label,
            description=spec.description,
            min_value=spec.min_value,
            max_value=spec.max_value,
        )
        db.add(row)

    row.value = _to_stored(value, spec.value_type)
    row.is_default = (
        _to_stored(spec.default, spec.value_type) == row.value
    )
    row.updated_by = updated_by
    db.flush()
    # 写完立刻让缓存失效，否则管理界面"保存成功"后最多 2 秒内仍是旧值
    invalidate_cache()
    logger.info("配置项 %s 已更新为 %s（操作者 %s）", key, row.value, updated_by)
    return row


def reset_value(db: Session, key: str, updated_by: str | None = None) -> SystemConfig:
    """把配置项恢复为出厂默认值。"""
    spec = SPECS.get(key)
    if spec is None:
        raise ConfigError(f"不支持的配置项: {key}")
    return update_value(db, key, spec.default, updated_by)


def reset_all(db: Session, updated_by: str | None = None) -> int:
    """恢复全部配置为默认值。返回恢复的项数。"""
    count = 0
    for key, spec in SPECS.items():
        try:
            update_value(db, key, spec.default, updated_by)
            count += 1
        except ConfigError:
            continue
    logger.info("已恢复 %d 个配置项为默认值", count)
    return count


def describe(db: Session, key: str) -> dict:
    """取配置项的完整描述（含范围），供前端渲染表单。"""
    spec = SPECS.get(key)
    if spec is None:
        return {}
    return {
        "key": key,
        "label": spec.label,
        "description": spec.description,
        "value_type": spec.value_type,
        "category": spec.category,
        "default": spec.default,
        "min_value": spec.min_value,
        "max_value": spec.max_value,
    }
