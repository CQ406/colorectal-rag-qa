"""SQLAlchemy 引擎与会话管理。

设计说明：
  - 同步引擎 + psycopg3。本系统的瓶颈在 GPU 推理与外部 LLM 调用（均为 IO 等待），
    数据库查询量小（单次检索 2~3 条 SQL），同步驱动足够且调试简单。
    GPU 推理与 LLM 调用通过线程池/异步客户端隔离，不阻塞会话。
  - Alembic 迁移复用同一 DSN（见 alembic/env.py）。
"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger("db.session")

engine = create_engine(
    settings.postgres.dsn,
    pool_size=10,
    max_overflow=20,
    pool_pre_ping=True,   # 自动剔除失效连接，避免长时间空闲后报错
    pool_recycle=3600,
    echo=False,
    future=True,
)

SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    autocommit=False,
    expire_on_commit=False,
    class_=Session,
)


def get_db() -> Generator[Session, None, None]:
    """FastAPI 依赖注入：每个请求一个会话，自动提交/回滚/关闭。"""
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@contextmanager
def session_scope() -> Generator[Session, None, None]:
    """脚本/后台任务用的会话上下文管理器。"""
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def check_connection() -> tuple[bool, str]:
    """健康检查：验证数据库连通性并返回版本信息。"""
    try:
        with engine.connect() as conn:
            version = conn.execute(text("SHOW server_version")).scalar_one()
            pgvector = conn.execute(
                text(
                    "SELECT extversion FROM pg_extension WHERE extname = 'vector'"
                )
            ).scalar_one_or_none()
        if pgvector is None:
            return False, f"PostgreSQL {version} 已连接，但 pgvector 扩展未安装"
        return True, f"PostgreSQL {version} / pgvector {pgvector}"
    except Exception as exc:  # noqa: BLE001 - 健康检查需吞掉所有异常并回报
        logger.warning("数据库连接检查失败: %s", exc)
        return False, f"连接失败: {type(exc).__name__}: {exc}"
