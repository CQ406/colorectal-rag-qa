"""ORM 模型包。

在此集中导入全部模型模块，作用是**注册元数据**：
`Base.metadata` 只有在模型模块被导入后才知道有哪些表，
而 `create_all` / `sync_schema` 都依赖该元数据。

若不在此导入，直接调用 `Base.metadata.create_all()` 会得到
「没有可创建的表」——这是一个安静且难以定位的问题，
因此统一在此处注册，任何入口导入 `app.models` 即可获得完整元数据。
"""

from app.models.base import Base, TimestampMixin, UUIDMixin
from app.models.batch import KbUpdateBatch
from app.models.knowledge import CaseRecord, Chunk, Document, ImageAsset
from app.models.system import (
    AuditLog,
    QaFeedback,
    QaLog,
    SystemConfig,
    User,
)

__all__ = [
    "Base",
    "TimestampMixin",
    "UUIDMixin",
    "Document",
    "KbUpdateBatch",
    "Chunk",
    "CaseRecord",
    "ImageAsset",
    "User",
    "AuditLog",
    "QaLog",
    "QaFeedback",
    "SystemConfig",
]
