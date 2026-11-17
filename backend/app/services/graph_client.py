"""Neo4j 图谱客户端。

职责：连接管理、schema 初始化、批量写入、查询执行。

为什么图谱要用 Neo4j 而不是 PostgreSQL 表
------------------------------------------
本系统的图谱查询包含**可变深度的多跳遍历**（"吻合口漏的治疗药物中，
哪些有禁忌证"需要 并发症 → 药物 → 禁忌证 三跳）。
在关系型数据库中这类查询需要递归 CTE，且性能随深度急剧下降；
Neo4j 的原生图遍历对多跳查询是常数级指针跳转。

这是选择 Neo4j 的唯一理由 —— 如果只需要存实体列表，
PostgreSQL 的表足够，不必引入额外组件。

图模型设计
----------
节点标签（`Entity`）：所有实体统一用一个标签，具体类型放在 `type` 属性上。

**为什么不用多个标签（:Disease / :Drug / …）**：本系统的实体类型由 LLM
抽取得到，类型集合可能随模型与提示词调整而变化。单一标签 + 属性更灵活，
且查询时用 `WHERE e.type = $type` 即可，无需维护标签索引。

关系类型固定，取固定枚举（见 ingest 中的 RELATION_TYPES）。
每条关系带 `evidence_chunk_ids`（来源文本块）与 `weight`（出现次数），
用于**把图谱命中映射回可引用的文献证据** —— 这是本系统可溯源能力
在图谱侧的延续。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.core.config import settings
from app.core.logging import get_logger
from app.services.graph_revision import mark_graph_changed

logger = get_logger("services.graph")

# 图谱 schema 约束与索引
#
# 说明：Entity 的唯一键是 (name, type) 组合而非仅 name ——
# 同名实体可能分属不同类型（如"贫血"可能是疾病也可能是并发症），
# 仅用 name 会导致实体错误合并。
SCHEMA_STATEMENTS = [
    "CREATE CONSTRAINT entity_unique IF NOT EXISTS "
    "FOR (e:Entity) REQUIRE (e.name, e.type) IS UNIQUE",
    "CREATE INDEX entity_name IF NOT EXISTS FOR (e:Entity) ON (e.name)",
    "CREATE INDEX entity_type IF NOT EXISTS FOR (e:Entity) ON (e.type)",
    "CREATE INDEX entity_norm IF NOT EXISTS FOR (e:Entity) ON (e.norm)",
]


@dataclass
class GraphEntity:
    """图谱实体。"""

    name: str
    type: str
    norm: str = ""                      # 归一化名称（小写去空格），用于匹配
    aliases: list[str] = field(default_factory=list)
    description: str = ""
    chunk_ids: list[str] = field(default_factory=list)   # 来源文本块
    frequency: int = 1

    def to_props(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "type": self.type,
            "norm": self.norm or self.name.strip().lower(),
            "aliases": self.aliases,
            "description": self.description,
            "chunk_ids": self.chunk_ids,
            "frequency": self.frequency,
        }


@dataclass
class GraphRelation:
    """图谱关系。"""

    source: str
    target: str
    rel_type: str
    source_type: str = ""
    target_type: str = ""
    chunk_ids: list[str] = field(default_factory=list)
    weight: int = 1

    def to_props(self) -> dict[str, Any]:
        return {
            "chunk_ids": self.chunk_ids,
            "weight": self.weight,
        }


class GraphClient:
    """Neo4j 客户端封装。"""

    def __init__(
        self,
        uri: str | None = None,
        user: str | None = None,
        password: str | None = None,
    ) -> None:
        from neo4j import GraphDatabase

        cfg = settings.neo4j
        self.uri = uri or cfg.neo4j_uri
        self.user = user or cfg.neo4j_user
        self.password = password or cfg.neo4j_password
        self._driver = GraphDatabase.driver(
            self.uri, auth=(self.user, self.password),
            max_connection_lifetime=300,
        )
        self._available: bool | None = None

    def close(self) -> None:
        self._driver.close()

    def __enter__(self) -> "GraphClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------
    #  连通性
    # ------------------------------------------------------------------
    def is_available(self) -> bool:
        """检查 Neo4j 是否可连接（结果缓存，避免每次查询都探测）。"""
        if self._available is not None:
            return self._available
        try:
            self._driver.verify_connectivity()
            self._available = True
        except Exception as exc:  # noqa: BLE001
            logger.warning("Neo4j 不可用: %s", str(exc)[:160])
            self._available = False
        return self._available

    def run(self, cypher: str, **params: Any) -> list[dict]:
        """执行 Cypher 并返回记录列表。"""
        with self._driver.session() as session:
            result = session.run(cypher, **params)
            return [dict(record) for record in result]

    # ------------------------------------------------------------------
    #  Schema
    # ------------------------------------------------------------------
    def init_schema(self) -> list[str]:
        """创建约束与索引，返回执行结果说明。"""
        done: list[str] = []
        for stmt in SCHEMA_STATEMENTS:
            try:
                self.run(stmt)
                done.append(f"OK: {stmt}")
            except Exception as exc:  # noqa: BLE001
                # 约束已存在或权限不足时不应中断整个初始化流程
                msg = f"跳过: {stmt} ({str(exc)[:100]})"
                logger.warning(msg)
                done.append(msg)
        logger.info("图谱 schema 初始化完成: %d 条语句", len(done))
        return done

    # ------------------------------------------------------------------
    #  写入
    # ------------------------------------------------------------------
    def upsert_entities(self, entities: list[GraphEntity]) -> int:
        """批量写入实体。

        用 MERGE 保证幂等：重复导入同一实体时合并属性而非新建节点。

        ⚠️ 不用 APOC：实测本环境的 Neo4j 5.26.31 社区版**未安装 APOC 扩展**，
        `apoc.coll.toSet` 不可用。因此列表去重改用纯 Cypher 的 reduce 实现。

        去重的必要性：图谱构建可能分多批执行，同一实体的 chunk_ids
        会被反复追加，不去重会导致列表随导入次数线性膨胀。
        """
        if not entities:
            return 0

        rows = [e.to_props() for e in entities]
        batch_size = 500
        written = 0
        for i in range(0, len(rows), batch_size):
            batch = rows[i : i + batch_size]
            self.run(
                """
                UNWIND $rows AS row
                MERGE (e:Entity {name: row.name, type: row.type})
                SET e.norm = row.norm,
                    e.description = CASE
                        WHEN row.description IS NULL OR row.description = ''
                        THEN e.description
                        ELSE row.description END,
                    e.aliases = reduce(
                        acc = coalesce(e.aliases, []), x IN coalesce(row.aliases, []) |
                        CASE WHEN x IN acc THEN acc ELSE acc + x END
                    ),
                    e.chunk_ids = reduce(
                        acc = coalesce(e.chunk_ids, []), x IN coalesce(row.chunk_ids, []) |
                        CASE WHEN x IN acc THEN acc ELSE acc + x END
                    ),
                    e.frequency = coalesce(e.frequency, 0) + row.frequency
                """,
                rows=batch,
            )
            written += len(batch)
            mark_graph_changed()
        return written

    def upsert_relations(self, relations: list[GraphRelation]) -> int:
        """批量写入关系。

        关系按类型分组（Cypher 不支持动态关系类型参数），
        每组用 UNWIND 批量 MERGE。chunk_ids 同样用 reduce 去重。
        """
        if not relations:
            return 0

        by_type: dict[str, list[dict]] = {}
        for rel in relations:
            by_type.setdefault(rel.rel_type, []).append(
                {
                    "source": rel.source,
                    "target": rel.target,
                    "source_type": rel.source_type,
                    "target_type": rel.target_type,
                    "chunk_ids": rel.chunk_ids,
                    "weight": rel.weight,
                }
            )

        written = 0
        for rel_type, rows in by_type.items():
            # rel_type 来自固定枚举（见 ingest 的 RELATION_TYPES），非用户输入，
            # 因此在 Cypher 中拼接是安全的。仍做标识符校验以防误用。
            if not rel_type.isidentifier():
                logger.warning("跳过非法关系类型: %r", rel_type)
                continue

            batch_size = 500
            for i in range(0, len(rows), batch_size):
                batch = rows[i : i + batch_size]
                self.run(
                    f"""
                    UNWIND $rows AS row
                    MATCH (a:Entity {{name: row.source, type: row.source_type}})
                    MATCH (b:Entity {{name: row.target, type: row.target_type}})
                    MERGE (a)-[r:{rel_type}]->(b)
                    SET r.chunk_ids = reduce(
                            acc = coalesce(r.chunk_ids, []),
                            x IN coalesce(row.chunk_ids, []) |
                            CASE WHEN x IN acc THEN acc ELSE acc + x END
                        ),
                        r.weight = coalesce(r.weight, 0) + row.weight
                    """,
                    rows=batch,
                )
                written += len(batch)
                mark_graph_changed()
        return written

    # ------------------------------------------------------------------
    #  统计
    # ------------------------------------------------------------------
    def stats(self) -> dict:
        """图谱规模统计（论文中需报告）。"""
        try:
            node_rows = self.run(
                "MATCH (e:Entity) RETURN e.type AS type, count(*) AS n "
                "ORDER BY n DESC"
            )
            rel_rows = self.run(
                "MATCH ()-[r]->() RETURN type(r) AS type, count(*) AS n "
                "ORDER BY n DESC"
            )
            total_nodes = sum(r["n"] for r in node_rows)
            total_rels = sum(r["n"] for r in rel_rows)
            return {
                "available": True,
                "nodes": total_nodes,
                "relations": total_rels,
                "by_type": {r["type"]: r["n"] for r in node_rows},
                "by_relation": {r["type"]: r["n"] for r in rel_rows},
            }
        except Exception as exc:  # noqa: BLE001
            return {"available": False, "error": str(exc)[:200]}

    def clear(self) -> int:
        """清空图谱（重建前调用）。"""
        rows = self.run("MATCH (e:Entity) DETACH DELETE e RETURN count(*) AS n")
        deleted = rows[0]["n"] if rows else 0
        mark_graph_changed()
        logger.warning("已清空图谱: %d 个节点", deleted)
        return deleted
