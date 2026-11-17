"""知识图谱构建流程：抽取 → 写入 Neo4j。

执行顺序
--------
  1. 筛选待抽取文本块（高证据等级优先，按文献去重）
  2. 逐块 LLM 抽取实体与关系
  3. 实体先入库（关系写入需要两端节点已存在）
  4. 关系入库
  5. 输出统计

**为什么必须实体先于关系**：`upsert_relations` 用 `MATCH` 定位两端节点，
若节点尚未创建则关系会被静默丢弃（MATCH 无结果 → UNWIND 无输出）。
这是 Cypher 写入的常见陷阱。

增量与重建
----------
`rebuild=False` 为增量模式：已有实体被 MERGE 合并（chunk_ids 累积去重），
不重复创建节点。`rebuild=True` 会先清空图谱再重建。

支持**断点续跑**：抽取进度写入 JSON 文件，中断后可从已完成处继续，
避免重复消耗 LLM 额度（400 块约需 1~2 小时，中断代价高）。
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict
from pathlib import Path

from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.services.extraction import (
    ENTITY_TYPE_ZH,
    ExtractedChunk,
    extract_chunk,
    select_chunks_for_extraction,
)
from app.services.graph_client import GraphClient, GraphEntity, GraphRelation
from app.services.llm_client import LLMClient

logger = get_logger("services.graph_ingest")

DEFAULT_CACHE = r"D:\AI\RAG_project\data\eval\graph_extraction_cache.json"


def load_cache(path: str | Path) -> dict:
    """读取抽取缓存（支持断点续跑）。"""
    p = Path(path)
    if not p.exists():
        return {"done": {}, "failed": []}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return {
            "done": data.get("done", {}),
            "failed": data.get("failed", []),
        }
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("抽取缓存损坏，将重新开始: %s", str(exc)[:120])
        return {"done": {}, "failed": []}


def save_cache(path: str | Path, cache: dict) -> None:
    """写入抽取缓存。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps(cache, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )


def build_graph(
    db: Session,
    *,
    limit: int = 400,
    rebuild: bool = False,
    cache_path: str | Path = DEFAULT_CACHE,
    resume: bool = True,
    progress_every: int = 10,
    excluded_chunk_ids: set[str] | None = None,
    selected_chunks: list[dict] | None = None,
    extraction_max_retries: int = 3,
    evidence_levels: tuple[str, ...] | None = None,
    max_per_document: int = 3,
) -> dict:
    """执行图谱构建。

    Args:
        limit: 抽取的文本块数上限
        evidence_levels: 只抽这些证据等级的块（分阶段扩图用）
        max_per_document: 每篇文献最多抽多少块
        rebuild: 是否清空图谱后重建
        cache_path: 抽取结果缓存路径（支持断点续跑）
        resume: 是否复用缓存中已完成的块

    Returns:
        构建统计
    """
    t_start = time.perf_counter()

    with GraphClient() as graph:
        if not graph.is_available():
            raise RuntimeError(
                "Neo4j 不可用，请确认服务已启动（docker compose up -d neo4j）"
            )
        graph.init_schema()

        if rebuild:
            deleted = graph.clear()
            logger.warning("重建模式：已清空 %d 个节点", deleted)

        excluded = excluded_chunk_ids or set()
        chunks = selected_chunks if selected_chunks is not None else select_chunks_for_extraction(
            db,
            limit=limit + len(excluded),
            evidence_levels=evidence_levels,
            max_per_document=max_per_document,
        )
        chunks = [chunk for chunk in chunks if chunk['chunk_id'] not in excluded][:limit]
        if not chunks:
            raise RuntimeError("没有可用的文本块，请确认知识库已入库")

        cache = load_cache(cache_path) if resume else {"done": {}, "failed": []}
        done: dict = cache["done"]

        # ---------- 抽取 ----------
        pending = [c for c in chunks if c["chunk_id"] not in done]
        logger.info(
            "待抽取 %d 块（已完成 %d 块，复用缓存）",
            len(pending), len(chunks) - len(pending),
        )

        if pending:
            client = LLMClient()
            try:
                for i, chunk in enumerate(pending, start=1):
                    result = extract_chunk(chunk, client, max_retries=extraction_max_retries)
                    done[chunk["chunk_id"]] = {
                        'content_sha256': __import__('hashlib').sha256(chunk['content'].encode()).hexdigest(),
                        'document_id':chunk['document_id'], 'source_content':chunk['content'],
                        "entities": [asdict(e) for e in result.entities],
                        "relations": [asdict(r) for r in result.relations],
                    }
                    if i % progress_every == 0 or i == len(pending):
                        logger.info(
                            "  抽取进度 %d/%d（本批新增实体 %d）",
                            i, len(pending), len(result.entities),
                        )
                        save_cache(cache_path, cache)   # 阶段性落盘，防中断丢失
            finally:
                client.close()
                save_cache(cache_path, cache)

        # ---------- 汇总 ----------
        all_entities: dict[tuple[str, str], GraphEntity] = {}
        all_relations: list[GraphRelation] = []

        for chunk in chunks:
            entry = done.get(chunk["chunk_id"])
            if not entry:
                continue
            cid = chunk["chunk_id"]
            for e in entry.get("entities", []):
                key = (e["name"], e["type"])
                if key in all_entities:
                    # 同一实体在多个块中出现，累积来源与频次
                    existing = all_entities[key]
                    if cid not in existing.chunk_ids:
                        existing.chunk_ids.append(cid)
                    existing.frequency += 1
                    for a in e.get("aliases") or []:
                        if a not in existing.aliases:
                            existing.aliases.append(a)
                else:
                    all_entities[key] = GraphEntity(
                        name=e["name"], type=e["type"], norm=e.get("norm", ""),
                        aliases=list(e.get("aliases") or []),
                        description=e.get("description", ""),
                        chunk_ids=[cid], frequency=1,
                    )

            for r in entry.get("relations", []):
                all_relations.append(
                    GraphRelation(
                        source=r["source"], target=r["target"],
                        rel_type=r["rel_type"],
                        source_type=r.get("source_type", ""),
                        target_type=r.get("target_type", ""),
                        chunk_ids=[cid], weight=1,
                    )
                )

        logger.info(
            "抽取汇总：去重后实体 %d 个，关系 %d 条（原始关系 %d 条）",
            len(all_entities), 0, len(all_relations),
        )

        # ---------- 写入图谱 ----------
        # 必须先写实体：关系写入依赖两端节点已存在（MATCH 语义）
        n_entities = graph.upsert_entities(list(all_entities.values()))
        logger.info("已写入实体 %d 个", n_entities)

        n_relations = graph.upsert_relations(all_relations)
        logger.info("已写入关系 %d 条", n_relations)

        stats = graph.stats()

    elapsed = time.perf_counter() - t_start
    result = {
        "chunks_processed": len(chunks),
        "entities_written": n_entities,
        "relations_written": n_relations,
        "graph": stats,
        "elapsed_sec": round(elapsed, 1),
    }

    logger.info("=" * 76)
    logger.info("图谱构建完成（耗时 %.1f 分钟）", elapsed / 60)
    logger.info("=" * 76)
    logger.info("  处理文本块: %d", result["chunks_processed"])
    logger.info("  图谱节点  : %d", stats.get("nodes", 0))
    logger.info("  图谱关系  : %d", stats.get("relations", 0))
    if stats.get("by_type"):
        logger.info("  节点类型分布:")
        for t, n in stats["by_type"].items():
            logger.info("    %-14s %4d", ENTITY_TYPE_ZH.get(t, t), n)
    if stats.get("by_relation"):
        logger.info("  关系类型分布:")
        for t, n in stats["by_relation"].items():
            logger.info("    %-22s %4d", t, n)

    return result


def graph_report(cache_path: str | Path = DEFAULT_CACHE) -> dict:
    """仅从缓存统计抽取结果（不访问 Neo4j，用于快速查看抽取质量）。"""
    cache = load_cache(cache_path)
    done = cache["done"]

    entity_counter: dict[str, int] = {}      # 实体名 → 出现块数
    relation_counter: dict[str, int] = {}    # 关系类型 → 条数
    names: dict[str, set[str]] = {}          # 实体类型 → 去重后的名称集合

    for entry in done.values():
        for e in entry.get("entities", []):
            entity_counter[e["name"]] = entity_counter.get(e["name"], 0) + 1
            names.setdefault(e["type"], set()).add(e["name"])
        for r in entry.get("relations", []):
            relation_counter[r["rel_type"]] = relation_counter.get(r["rel_type"], 0) + 1

    top_entities = sorted(entity_counter.items(), key=lambda kv: -kv[1])[:25]

    # 按去重实体数降序排列各类型
    by_type = sorted(
        ((ENTITY_TYPE_ZH.get(t, t), len(s)) for t, s in names.items()),
        key=lambda kv: -kv[1],
    )

    return {
        "chunks_extracted": len(done),
        "entity_mentions": sum(entity_counter.values()),
        "unique_entities": len(entity_counter),
        "relation_mentions": sum(relation_counter.values()),
        "by_type": dict(by_type),
        "by_relation": dict(
            sorted(relation_counter.items(), key=lambda kv: -kv[1])
        ),
        "top_entities": top_entities,
    }
