"""图谱检索（阶段五）。

设计思路
--------
图谱检索要解决的问题与向量检索不同：

  向量检索：找**语义相似**的文本块（"这段话在讲什么"）
  图谱检索：找**概念关联**的证据（"与这些概念有关的块"）

具体流程：

  查询 → 匹配图谱中的实体 → 沿关系遍历关联实体 → 收集这些实体
      所来源的文本块（chunk_ids）→ 按匹配度排序返回

**为什么实体匹配用向量而不是字符串**
  用户提问是口语化的（"荧光造影准不准"），而图谱实体名是规范术语
  （"吲哚菁绿荧光血管造影"）。字符串匹配会大量漏召回。
  复用已有的 BGE-M3 嵌入模型（已在本机 GPU 上加载）做语义匹配，
  无需额外模型，也不额外占用显存。

**诚实的预期管理**
  阶段十的评测集是**口语化临床问题**（"这个术式有什么优势"），
  这类问题主要依赖语义相似度，图谱的多跳关联难以带来提升 ——
  与关键词检索的情况类似。

  图谱真正的价值场景是**多跳关系查询**：
    - "用 X 药的患者有哪些禁忌证"（药物 → 疾病 → 并发症，三跳）
    - "哪些并发症既与 A 术式有关也与 B 疾病有关"（汇聚查询）
  这类问题在本评测集中占比很低。

  因此图谱检索的定位是**补充召回源**，权重不高（默认 0.6），
  且需在论文中如实报告其在口语化问题上的边际贡献。
"""

from __future__ import annotations

import json
import hashlib
import math
import threading
from pathlib import Path
import time
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.core.config import settings, PROJECT_ROOT
from app.core.logging import get_logger
from app.services.graph_client import GraphClient
from app.services.graph_revision import graph_revision

logger = get_logger("services.graph_search")

# 实体匹配的相似度下限：低于此值认为查询与图谱无关，返回空结果。
# 依据：BGE-M3 对不相关中文本的余弦相似度通常在 0.3~0.5，
# 相关文本在 0.6 以上。取 0.55 作为保守门槛。
MIN_ENTITY_SIMILARITY = 0.55

# 单次查询最多匹配的实体数（过多会引入噪声并增加遍历开销）
MAX_MATCHED_ENTITIES = 8

# 图谱遍历的最大跳数。2 跳足以覆盖"术式 → 并发症 → 预防手段"这类常见链路；
# 3 跳以上会迅速扩散到无关概念（图爆炸）。
MAX_HOPS = 2


@dataclass
class GraphHit:
    """图谱检索命中的实体。"""

    name: str
    type: str
    similarity: float
    hop: int              # 0 = 直接匹配，1/2 = 关联实体
    chunk_ids: list[str]
    relation: str = ""    # 关联关系类型（hop > 0 时有值）


# ----------------------------------------------------------------------
#  实体向量缓存
# ----------------------------------------------------------------------
class EntityIndex:
    """图谱实体的向量索引。

    懒加载 + 全局缓存：实体集合在两次图谱构建之间是稳定的，
    每次查询都重新编码全部实体名会造成大量无谓计算。

    **为什么要加锁**：FastAPI 的同步端点跑在线程池中，
    多请求并发时可能同时触发首次构建，导致重复编码。
    """

    def __init__(self) -> None:
        self._names: list[str] = []
        self._types: list[str] = []
        self._chunk_ids: list[list[str]] = []
        self._vectors: list[list[float]] = []
        self._built = False
        self._lock = threading.Lock()
        self._build_error: str = ""
        self._source_version = ''

    @property
    def size(self) -> int:
        return len(self._names)

    @property
    def built(self) -> bool:
        return self._built

    @property
    def build_error(self) -> str:
        return self._build_error

    # ------------------------------------------------------------------
    #  磁盘缓存
    # ------------------------------------------------------------------
    # 编码 1423 个实体名需约 70 秒（模型冷启动时更久），而实体集合在
    # 两次图谱构建之间是稳定的。缓存到磁盘后，后续进程启动直接读取
    # （约 0.1 秒），避免每次重启后端都要等待。
    CACHE_PATH = str(PROJECT_ROOT / 'data/eval/graph_entity_index.json')

    @staticmethod
    def _fingerprint(names: list[str], types=None, chunk_ids=None) -> str:
        """Invalidate on any entity, type, source mapping or encoder change."""
        import hashlib

        payload = {'names':names,'types':types,'chunk_ids':chunk_ids,
            'model':settings.model.embedding_model_path, 'dimension':settings.model.embedding_dim}
        return hashlib.sha256(json.dumps(payload,sort_keys=True,ensure_ascii=False).encode()).hexdigest()

    def _load_cache(self, fingerprint: str) -> bool:
        """尝试从磁盘缓存加载索引。"""
        path = Path(self.CACHE_PATH)
        if not path.exists():
            return False
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("实体索引缓存读取失败: %s", str(exc)[:100])
            return False

        if data.get("fingerprint") != fingerprint:
            logger.info("实体索引缓存已过期（图谱已变化），将重新编码")
            return False

        try:
            self._names = data["names"]
            self._types = data["types"]
            self._chunk_ids = data["chunk_ids"]
            self._vectors = data["vectors"]
        except KeyError as exc:
            logger.warning("实体索引缓存字段缺失: %s", exc)
            return False

        logger.info(
            "实体索引从磁盘缓存加载: %d 个实体（免去约 70 秒编码等待）",
            len(self._names),
        )
        return True

    def _save_cache(self, fingerprint: str) -> None:
        """写入磁盘缓存。失败不影响功能（仅损失下次启动的加速）。"""
        try:
            path = Path(self.CACHE_PATH)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(
                    {
                        "fingerprint": fingerprint,
                        "names": self._names,
                        "types": self._types,
                        "chunk_ids": self._chunk_ids,
                        "vectors": self._vectors,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            logger.info("实体索引已缓存到 %s", path)
        except OSError as exc:
            logger.warning("实体索引缓存写入失败: %s", str(exc)[:100])

    def invalidate(self) -> None:
        """图谱重建后调用，强制下次查询重新编码（并清除磁盘缓存）。"""
        with self._lock:
            self._built = False
            self._names, self._types, self._chunk_ids, self._vectors = [], [], [], []
            self._build_error = ""
        try:
            Path(self.CACHE_PATH).unlink(missing_ok=True)
        except OSError:
            pass

    def ensure_built(self, active_ids: set[str] | None = None) -> bool:
        """确保索引已构建。返回是否可用。"""
        version = hashlib.sha256(json.dumps({
            'active_ids': sorted(active_ids) if active_ids is not None else None,
            'graph_revision': graph_revision(),
        }, sort_keys=True).encode()).hexdigest()
        with self._lock:
            if version != self._source_version:
                self._built = False
                self._source_version = version
                self._names, self._types, self._chunk_ids, self._vectors = [], [], [], []
                self._build_error = ""
            if self._built:
                return bool(self._names)

            t0 = time.perf_counter()
            try:
                with GraphClient() as graph:
                    if not graph.is_available():
                        self._build_error = "Neo4j 不可用"
                        self._built = True
                        return False
                    rows = graph.run(
                        "MATCH (e:Entity) "
                        "WHERE e.chunk_ids IS NOT NULL AND size(e.chunk_ids) > 0 "
                        "AND ($active_ids IS NULL OR any(cid IN e.chunk_ids WHERE cid IN $active_ids)) "
                        "RETURN e.name AS name, e.type AS type, "
                        "       e.chunk_ids AS chunk_ids "
                        "ORDER BY e.name, e.type "
                        "LIMIT 3000",
                        active_ids=sorted(active_ids) if active_ids is not None else None,
                    )
            except Exception as exc:  # noqa: BLE001
                self._build_error = f"读取图谱失败: {str(exc)[:120]}"
                logger.warning(self._build_error)
                self._built = True
                return False

            if active_ids is not None:
                rows = [dict(row, chunk_ids=[cid for cid in row['chunk_ids'] if cid in active_ids]) for row in rows]
                rows = [row for row in rows if row['chunk_ids']]
            if not rows:
                self._build_error = "图谱没有可引用的上架来源，请基于当前文本块重建"
                self._built = True
                return False

            names = [r["name"] for r in rows]
            types = [r["type"] for r in rows]
            chunk_ids = [list(r["chunk_ids"] or []) for r in rows]
            fingerprint = self._fingerprint(names, types, chunk_ids)

            # 优先尝试磁盘缓存（图谱未变时可免去约 70 秒编码）
            if self._load_cache(fingerprint):
                self._built = True
                return True

            try:
                from app.services.search_service import embed_texts

                # embed_texts 返回 (向量列表, 耗时秒)
                vectors, _elapsed = embed_texts(names, batch_size=64)
            except Exception as exc:  # noqa: BLE001
                self._build_error = f"实体编码失败: {str(exc)[:120]}"
                logger.warning(self._build_error)
                self._built = True
                return False

            self._names, self._types = names, types
            self._chunk_ids, self._vectors = chunk_ids, vectors
            self._built = True
            elapsed = time.perf_counter() - t0
            logger.info(
                "图谱实体索引构建完成: %d 个实体（耗时 %.1f 秒）",
                len(names), elapsed,
            )
            self._save_cache(fingerprint)
            return True

    def search(self, query_vector: list[float], top_k: int) -> list[GraphHit]:
        """按余弦相似度匹配实体。"""
        if not self._built or not self._vectors:
            return []

        qnorm = math.sqrt(sum(x * x for x in query_vector)) or 1.0
        scored: list[tuple[float, int]] = []
        for i, vec in enumerate(self._vectors):
            dot = 0.0
            vnorm = 0.0
            for a, b in zip(query_vector, vec):
                dot += a * b
                vnorm += b * b
            sim = dot / (qnorm * (math.sqrt(vnorm) or 1.0))
            if sim >= MIN_ENTITY_SIMILARITY:
                scored.append((sim, i))

        scored.sort(key=lambda x: -x[0])
        return [
            GraphHit(
                name=self._names[i],
                type=self._types[i],
                similarity=round(sim, 4),
                hop=0,
                chunk_ids=self._chunk_ids[i],
            )
            for sim, i in scored[:top_k]
        ]


# 全局索引单例
_index = EntityIndex()


def get_index() -> EntityIndex:
    return _index


def invalidate_index() -> None:
    """图谱重建后调用。"""
    _index.invalidate()


# ----------------------------------------------------------------------
#  多跳遍历
# ----------------------------------------------------------------------
def expand_by_relations(
    seeds: list[GraphHit],
    *,
    max_hops: int = MAX_HOPS,
    limit_per_entity: int = 5,
    active_ids: set[str] | None = None,
) -> list[GraphHit]:
    """沿关系扩展实体（多跳）。

    返回的关联实体保留 chunk_ids —— 这些块就是"与查询概念相关联"的证据，
    是图谱检索区别于向量检索的核心产出。
    """
    if not seeds or active_ids == set():
        return []

    seed_keys = [(h.name, h.type) for h in seeds]
    expanded: list[GraphHit] = []

    try:
        with GraphClient() as graph:
            if not graph.is_available():
                return []
            rows = graph.run(
                f"""
                UNWIND $seeds AS seed
                MATCH (a:Entity {{name: seed.name, type: seed.type}})
                MATCH path = (a)-[*1..{max_hops}]-(b:Entity)
                WHERE b.chunk_ids IS NOT NULL AND size(b.chunk_ids) > 0
                  AND NOT (b.name = seed.name AND b.type = seed.type)
                  AND ($active_ids IS NULL OR (
                    any(cid IN b.chunk_ids WHERE cid IN $active_ids)
                    AND all(r IN relationships(path) WHERE
                        any(cid IN coalesce(r.chunk_ids, []) WHERE cid IN $active_ids))
                  ))
                WITH b, seed, length(path) AS hops,
                     [r IN relationships(path) | type(r)] AS rels
                RETURN b.name AS name, b.type AS type,
                       b.chunk_ids AS chunk_ids, hops, rels[0] AS rel
                ORDER BY hops ASC, name ASC, type ASC, rel ASC
                LIMIT $limit
                """,
                seeds=[{"name": n, "type": t} for n, t in seed_keys],
                limit=limit_per_entity * len(seed_keys) * 4,
                active_ids=sorted(active_ids) if active_ids is not None else None,
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("图谱遍历失败: %s", str(exc)[:140])
        return []

    # 去重：同一实体可能通过多条路径到达，保留最短跳数
    seen: dict[tuple[str, str], GraphHit] = {}
    for r in rows:
        sources = [cid for cid in (r['chunk_ids'] or []) if active_ids is None or cid in active_ids]
        if not sources:
            continue
        key = (r["name"], r["type"])
        if key in seen:
            continue
        seen[key] = GraphHit(
            name=r["name"],
            type=r["type"],
            similarity=0.0,
            hop=int(r["hops"]),
            chunk_ids=sources,
            relation=r.get("rel") or "",
        )
        if len(seen) >= limit_per_entity * len(seed_keys):
            break

    expanded = list(seen.values())
    logger.debug(
        "图谱扩展: 种子 %d 个 → 关联实体 %d 个", len(seeds), len(expanded)
    )
    return expanded


# ----------------------------------------------------------------------
#  对外入口
# ----------------------------------------------------------------------
def graph_recall(
    db: Session,
    query: str,
    limit: int = 30,
    phase: str | None = None,
    doc_type: str | None = None,
) -> list:
    """图谱路径召回，返回 ScoredChunk 列表（供 RRF 融合）。

    流程：
      查询向量 → 匹配图谱实体 → 多跳扩展 → 收集 chunk_ids → 构造 ScoredChunk

    评分设计：
      直接匹配的实体给高分（按相似度），关联实体按跳数衰减。
      最终块的得分取**来源实体的最高分** —— 因为一个块可能包含多个
      相关实体，取最高分比累加更稳健（避免长文本因包含实体多而占优）。
    """
    from app.models.knowledge import Chunk, Document
    from app.repositories.search_repo import _to_scored
    from app.services.search_service import embed_query
    from sqlalchemy import select

    index = get_index()
    active_ids = {str(cid) for cid in db.scalars(select(Chunk.id).join(Document,Chunk.document_id==Document.id).where(Document.is_active.is_(True)))}
    if not index.ensure_built(active_ids):
        logger.debug("图谱索引不可用: %s", index.build_error)
        return []
    eligible = select(Chunk.id).join(Document,Chunk.document_id==Document.id).where(Document.is_active.is_(True))
    if phase:
        eligible = eligible.where(Chunk.phase==phase)
    if doc_type:
        eligible = eligible.where(Chunk.doc_type==doc_type)
    eligible_ids = {str(cid) for cid in db.scalars(eligible)} if phase or doc_type else active_ids

    # ---------- 查询向量 ----------
    try:
        # embed_query 返回 (向量, 耗时秒)
        qvec, _elapsed = embed_query(query)
    except Exception as exc:  # noqa: BLE001
        logger.warning("图谱检索：查询编码失败 %s", str(exc)[:120])
        return []

    # ---------- 实体匹配与扩展 ----------
    seeds = index.search(qvec, top_k=MAX_MATCHED_ENTITIES)
    if not seeds:
        logger.debug("图谱检索：未匹配到实体（查询=%s）", query[:40])
        return []

    expanded = expand_by_relations(seeds, active_ids=active_ids)

    # ---------- 汇总块得分 ----------
    # 跳数衰减：直接匹配权重 1.0，1 跳 0.5，2 跳 0.25
    hop_decay = {0: 1.0, 1: 0.5, 2: 0.25}
    chunk_scores: dict[str, float] = {}

    for hit in seeds:
        score = hit.similarity * hop_decay.get(hit.hop, 0.25)
        for cid in hit.chunk_ids:
            chunk_scores[cid] = max(chunk_scores.get(cid, 0.0), score)

    for hit in expanded:
        # 关联实体本身与查询的相似度未知，给一个基于跳数的基准分；
        # 它代表「通过概念关联被召回」，弱于直接语义匹配
        score = 0.45 * hop_decay.get(hit.hop, 0.25)
        for cid in hit.chunk_ids:
            chunk_scores[cid] = max(chunk_scores.get(cid, 0.0), score)

    if not chunk_scores:
        return []

    # ---------- 取块内容 ----------
    top_ids = sorted(((cid,score) for cid,score in chunk_scores.items() if cid in eligible_ids),
        key=lambda kv: (-kv[1], kv[0]))[:limit]
    ids = [cid for cid, _ in top_ids]
    try:
        rows = db.execute(select(Chunk).where(Chunk.id.in_(ids))).scalars().all()
    except Exception as exc:  # noqa: BLE001 - id 格式异常（如非 UUID）不中断流程
        logger.warning("图谱检索：读取文本块失败 %s", str(exc)[:140])
        return []

    by_id = {str(c.id): c for c in rows}

    # 复用 search_repo._to_scored 构造 ScoredChunk，
    # 避免手工拼装字段时漏掉必填项（ScoredChunk 含 13 个必填字段）
    results: list = []
    for cid, score in top_ids:
        chunk = by_id.get(cid)
        if chunk is None:
            continue
        results.append(_to_scored(chunk, round(score, 4)))

    # 用 debug 级别：评测时每题都会命中此路径，
    # info 级别会产生数万行日志（实测单次评测刷出 95KB）
    logger.debug(
        "图谱检索: 种子实体 %s → 关联 %d 个 → 召回 %d 块",
        [h.name for h in seeds[:3]], len(expanded), len(results),
    )
    return results
