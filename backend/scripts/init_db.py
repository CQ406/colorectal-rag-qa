"""数据库表初始化脚本。

用法:
    python -m scripts.init_db

说明:
  - 开发阶段用 SQLAlchemy 直接建表，便于快速迭代
  - 表结构稳定后应改用 Alembic 迁移管理（见 README）
"""

from __future__ import annotations

import sys
from pathlib import Path

# 允许以脚本方式直接运行
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import inspect, text

from app.core.logging import get_logger, setup_logging
from app.db.session import engine
from app.models import knowledge  # noqa: F401  导入以注册模型元数据
from app.models.base import Base

logger = get_logger("scripts.init_db")


def main() -> int:
    setup_logging()

    logger.info("准备建表，共 %d 张表", len(Base.metadata.tables))
    for table in Base.metadata.sorted_tables:
        logger.info("  - %s", table.name)

    # vector 扩展必须先于含 vector 列的表创建
    with engine.begin() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
        conn.execute(text('CREATE EXTENSION IF NOT EXISTS "uuid-ossp"'))
        logger.info("扩展已就绪: vector / pg_trgm / uuid-ossp")

    Base.metadata.create_all(engine)
    logger.info("建表完成")

    # 校验结果
    inspector = inspect(engine)
    tables = sorted(inspector.get_table_names())
    logger.info("当前数据库中的表 (%d): %s", len(tables), ", ".join(tables))

    # 确认 HNSW 索引与 GIN 索引已创建
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT indexname, indexdef FROM pg_indexes "
                "WHERE tablename IN ('kb_chunk','kb_case','kb_document') "
                "ORDER BY tablename, indexname"
            )
        ).all()
    logger.info("已创建索引 (%d 个):", len(rows))
    for name, definition in rows:
        method = "HNSW" if "hnsw" in definition else ("GIN" if "gin" in definition else "BTREE")
        logger.info("  [%s] %s", method, name)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
