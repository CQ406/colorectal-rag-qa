"""数据库结构同步脚本。

用途：在不重建表的前提下，为已存在的表补充新增的列。
`SQLAlchemy.create_all()` 只创建缺失的**表**，不会为已存在的表**加列**，
因此模型新增字段后需要本脚本同步。

用法:
    python -m scripts.sync_schema
    python -m scripts.sync_schema --dry-run
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import inspect, text

from app.core.logging import get_logger, setup_logging
from app.db.session import engine
from app.models import batch, knowledge, system  # noqa: F401  注册模型元数据
from app.models.base import Base

logger = get_logger("scripts.sync_schema")

# 列类型映射：把 SQLAlchemy 类型编译为 PostgreSQL DDL
def _pg_type(column) -> str:
    """把 SQLAlchemy 列类型编译为 PostgreSQL 类型字符串。"""
    from sqlalchemy.dialects import postgresql

    return column.type.compile(dialect=postgresql.dialect())


def find_missing_columns() -> dict[str, list[str]]:
    """对比模型定义与实际数据库，返回 {表名: [缺失列DDL, ...]}。"""
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    missing: dict[str, list[str]] = {}

    for table_name, table in Base.metadata.tables.items():
        if table_name not in existing_tables:
            continue  # 表不存在由 init_db 负责创建
        db_columns = {c["name"] for c in inspector.get_columns(table_name)}
        for column in table.columns:
            if column.name not in db_columns:
                ddl = f"ALTER TABLE {table_name} ADD COLUMN {column.name} {_pg_type(column)}"
                missing.setdefault(table_name, []).append(ddl)
                if column.comment:
                    safe_comment = column.comment.replace("'", "''")
                    missing[table_name].append(f"COMMENT ON COLUMN {table_name}.{column.name} IS '{safe_comment}'")
    return missing


def find_missing_indexes() -> list[str]:
    """为已有表补齐模型中新增的索引（create_all 不会覆盖已有表）。"""
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    existing_indexes = {
        index["name"]
        for table_name in existing_tables
        for index in inspector.get_indexes(table_name)
        if index.get("name")
    }
    missing: list[str] = []
    for table_name, table in Base.metadata.tables.items():
        if table_name not in existing_tables:
            continue
        for index in table.indexes:
            if index.name in existing_indexes:
                continue
            columns = ", ".join(column.name for column in index.columns)
            if index.dialect_options["postgresql"].get("using") == "hnsw":
                options = index.dialect_options["postgresql"].get("with") or {}
                with_clause = ", ".join(f"{key} = {value}" for key, value in options.items())
                opclass = index.dialect_options["postgresql"].get("ops") or {}
                op = opclass.get(next(iter(index.columns)).name, "")
                columns = f"{columns} {op}" if op else columns
                ddl = f"CREATE INDEX {index.name} ON {table_name} USING hnsw ({columns})"
                if with_clause:
                    ddl += f" WITH ({with_clause})"
            else:
                ddl = f"CREATE INDEX {index.name} ON {table_name} ({columns})"
            missing.append(ddl)
    return missing


def main() -> int:
    parser = argparse.ArgumentParser(description="同步数据库结构（补列与索引）")
    parser.add_argument("--dry-run", action="store_true", help="只显示将执行的 SQL")
    args = parser.parse_args()

    setup_logging()

    missing = find_missing_columns()
    missing_indexes = find_missing_indexes()
    missing_tables = set(Base.metadata.tables) - set(inspect(engine).get_table_names())
    if missing_tables:
        logger.info('缺失表: %s', ', '.join(sorted(missing_tables)))
    if not missing and not missing_indexes and not missing_tables:
        logger.info("数据库结构与模型一致，无需变更 [OK]")
        return 0

    total = sum(ddl.startswith('ALTER TABLE') for values in missing.values() for ddl in values)
    logger.info("发现 %d 个缺失列，分布在 %d 张表:", total, len(missing))
    for table, ddls in missing.items():
        logger.info("  表 %s:", table)
        for ddl in ddls:
            logger.info("    %s", ddl)
    if missing_indexes:
        logger.info("发现 %d 个缺失索引:", len(missing_indexes))
        for ddl in missing_indexes:
            logger.info("    %s", ddl)

    if args.dry_run:
        logger.info("\n[预览模式] 未执行任何变更")
        return 2

    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        for table, ddls in missing.items():
            for ddl in ddls:
                # Statements are separate entries. Never split SQL on ';':
                # comments can contain semicolons and quotes inside literals.
                conn.exec_driver_sql(ddl)
        for ddl in missing_indexes:
            conn.execute(text(ddl))
    logger.info("\n已补充 %d 个列、%d 个索引 [OK]", total, len(missing_indexes))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
