"""统一日志配置。

要点：
  - 控制台输出用 UTF-8，避免中文日志在 Windows 终端乱码
  - 降低 SQLAlchemy / httpx 等第三方库的日志级别，防止调试信息淹没业务日志
  - 支持通过环境变量 LOG_LEVEL 调整级别
"""

from __future__ import annotations

import logging
import sys
from logging.config import dictConfig

LOG_LEVEL = "INFO"
LOG_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)-28s | %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def setup_logging(level: str = LOG_LEVEL) -> None:
    """初始化日志系统（应用启动时调用一次）。"""
    dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {
                "standard": {"format": LOG_FORMAT, "datefmt": DATE_FORMAT},
            },
            "handlers": {
                "console": {
                    "class": "logging.StreamHandler",
                    "formatter": "standard",
                    "stream": "ext://sys.stdout",
                },
            },
            "root": {"handlers": ["console"], "level": level},
            "loggers": {
                # 应用自身
                "app": {"level": level, "propagate": True},
                # 第三方库降噪
                "sqlalchemy.engine": {"level": "WARNING", "propagate": True},
                "sqlalchemy.pool": {"level": "WARNING", "propagate": True},
                "httpx": {"level": "WARNING", "propagate": True},
                "httpcore": {"level": "WARNING", "propagate": True},
                "neo4j": {"level": "WARNING", "propagate": True},
                "urllib3": {"level": "WARNING", "propagate": True},
                "minio": {"level": "WARNING", "propagate": True},
                # 模型加载信息较多，保留 INFO 但独立命名便于过滤
                "FlagEmbedding": {"level": "WARNING", "propagate": True},
            },
        }
    )

    # Windows 控制台默认可能是 GBK，强制 UTF-8 以正确输出中文
    for handler in logging.getLogger().handlers:
        reconfigure = getattr(handler, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8")
            except (ValueError, OSError):
                pass


def get_logger(name: str) -> logging.Logger:
    """获取带命名空间的 logger。"""
    return logging.getLogger(f"app.{name}" if not name.startswith("app.") else name)
