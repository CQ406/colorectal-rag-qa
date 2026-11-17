"""知识库更新任务执行器（阶段九「知识库维护模块」的后台执行部分）。

要解决的问题
------------
采集 + 向量化是**数十分钟级**的操作（现有 444 篇入库约 1 小时）。
若放在 HTTP 请求里同步执行：
  - 浏览器/网关会在 30~60 秒后超时断开
  - 用户看不到任何进度，只能干等
  - 中途刷新页面就会丢失任务状态

因此改为**后台执行 + 轮询进度**：

    接口 POST /api/admin/kb/collect
      → 立即返回 batch_id（此时 state=pending）
      → 后台线程开始执行
      → 前端每 2 秒轮询 GET /api/admin/kb/tasks/{batch_id}
      → 完成后前端刷新文献列表

状态存储为什么用内存字典 + 数据库两层
------------------------------------
  - **数据库**（KbUpdateBatch）存**最终结果**：持久、可追溯、重启不丢
  - **内存字典**存**实时进度**：进度每秒都在变，写数据库代价过高且无必要

代价是后端重启会丢失进行中任务的进度显示，但批次记录仍在数据库里
（状态停在 running），管理员可以据此判断需要重跑。

并发控制
--------
向量化会占用 GPU。若管理员连点两次「补齐向量」，
两个任务会争抢显存并互相拖慢，甚至 OOM。
因此用一个全局锁保证**同一时刻只有一个重任务在跑**，
第二个请求直接返回「已有任务正在执行」。
"""

from __future__ import annotations

import threading
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from app.core.logging import get_logger

logger = get_logger("services.kb_jobs")


# 任务类型
JOB_COLLECT = "collect"
JOB_SEARCH = "search"
JOB_VECTORIZE = "vectorize"


@dataclass
class JobProgress:
    """一个后台任务的实时进度。"""

    job_id: str
    operation: str
    status: str = "running"          # running / success / failed
    stage: str = "准备中"             # 当前阶段的中文描述
    current: int = 0                 # 当前进度
    total: int = 0                   # 总数（0 表示未知）
    message: str = ""
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    result: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    @property
    def percent(self) -> float:
        """完成百分比。总数未知时返回 0，避免显示误导性的进度。"""
        if self.total <= 0:
            return 0.0
        return round(min(self.current / self.total, 1.0) * 100, 1)

    @property
    def elapsed_seconds(self) -> float:
        end = self.finished_at or time.time()
        return round(end - self.started_at, 1)

    def to_dict(self) -> dict:
        return {
            "job_id": self.job_id,
            "operation": self.operation,
            "status": self.status,
            "stage": self.stage,
            "current": self.current,
            "total": self.total,
            "percent": self.percent,
            "message": self.message,
            "elapsed_seconds": self.elapsed_seconds,
            "result": self.result,
            "error": self.error,
        }


class JobRegistry:
    """任务注册表（进程内单例）。

    只保留最近 N 个任务的进度，避免长期运行后内存无限增长。
    """

    MAX_KEEP = 50

    def __init__(self) -> None:
        self._jobs: dict[str, JobProgress] = {}
        self._order: list[str] = []
        self._lock = threading.Lock()
        # 重任务互斥锁：保证同一时刻只有一个占用 GPU 的任务
        self._heavy_lock = threading.Lock()
        self._active_job: str | None = None

    # ------------------------------------------------------------------
    #  查询
    # ------------------------------------------------------------------
    def get(self, job_id: str) -> JobProgress | None:
        return self._jobs.get(job_id)

    def list_recent(self, limit: int = 20) -> list[JobProgress]:
        return [self._jobs[j] for j in reversed(self._order[-limit:]) if j in self._jobs]

    @property
    def active_job(self) -> str | None:
        """当前正在执行的重任务 id（无则 None）。"""
        return self._active_job

    def is_busy(self) -> bool:
        return self._active_job is not None

    # ------------------------------------------------------------------
    #  执行
    # ------------------------------------------------------------------
    def submit(
        self,
        job_id: str,
        operation: str,
        target: Callable[[JobProgress], dict],
        *,
        exclusive: bool = True,
    ) -> JobProgress:
        """提交一个后台任务并立即返回进度对象。

        Args:
            job_id: 任务标识（使用批次 id，便于与数据库记录对应）
            target: 实际执行体，接收 JobProgress 以便上报进度，
                    返回结果字典
            exclusive: 是否独占执行（占用 GPU 的任务应设为 True）
        """
        progress = JobProgress(job_id=job_id, operation=operation)
        with self._lock:
            self._jobs[job_id] = progress
            self._order.append(job_id)
            # 清理过旧的任务
            while len(self._order) > self.MAX_KEEP:
                old = self._order.pop(0)
                self._jobs.pop(old, None)

        def runner() -> None:
            acquired = False
            try:
                if exclusive:
                    # 非阻塞获取：拿不到说明有任务在跑，直接失败而不是排队，
                    # 因为排队会让管理员以为任务已开始，实际还在等
                    acquired = self._heavy_lock.acquire(blocking=False)
                    if not acquired:
                        progress.status = "failed"
                        progress.error = "已有知识库更新任务正在执行，请等待其完成"
                        progress.finished_at = time.time()
                        logger.warning("任务 %s 被拒绝：已有任务在执行", job_id[:8])
                        return
                    self._active_job = job_id

                progress.stage = "执行中"
                result = target(progress)
                progress.result = result or {}
                progress.status = "success"
                progress.stage = "已完成"
                progress.message = "任务完成"
            except Exception as exc:  # noqa: BLE001
                progress.status = "failed"
                progress.stage = "失败"
                progress.error = f"{type(exc).__name__}: {exc}"
                logger.error(
                    "知识库任务 %s 失败: %s\n%s",
                    job_id[:8], str(exc)[:200], traceback.format_exc()[-1200:],
                )
            finally:
                progress.finished_at = time.time()
                if acquired:
                    self._active_job = None
                    self._heavy_lock.release()

        threading.Thread(target=runner, name=f"kb-job-{job_id[:8]}", daemon=True).start()
        return progress


# 进程内单例
registry = JobRegistry()


# ======================================================================
#  具体任务
# ======================================================================
def _select_queries(mode: str) -> tuple[list, str]:
    """按采集模式选择检索式集合。

    Args:
        mode: default（主检索式 9 个）/ supplement（补充检索式 17 个）/
              all（两者合并）

    Returns:
        (检索式列表, 模式说明)

    补充检索式的存在原因见 `collect_queries_supplement` 模块文档：
    主检索式无时间与语言过滤，导致中文文献仅占 3.8%、近三年仅占 11.0%，
    而这两类文献在 PubMed 上实际可得数百至上千篇。
    """
    from app.etl import collect_queries
    from app.etl import collect_queries_supplement as supp

    main = list(getattr(collect_queries, "COLLECT_QUERIES", []) or [])
    extra = list(getattr(supp, "SUPPLEMENT_QUERIES", []) or [])

    if mode == "supplement":
        return extra, "补充检索式（中文文献 + 近三年高证据）"
    if mode == "all":
        return main + extra, "主检索式 + 补充检索式"
    return main, "主检索式"


def run_collect(
    *,
    batch_id: str,
    mode: str = "default",
    max_total: int | None = None,
    mindate: str | None = None,
    embed: bool = True,
    batch_size: int = 32,
) -> dict:
    """按预定义检索式批量采集 PubMed 文献。

    实际执行逻辑与 CLI 的 `collect` 子命令一致，
    复用同一套 ETL 函数，避免两处实现产生行为差异。

    Args:
        mode: default / supplement / all，决定使用哪一组检索式
    """
    from app.db.session import session_scope
    from app.etl.ingest import ingest_articles
    from app.etl.pubmed_client import PubMedClient
    from app.repositories import kb_repo

    def target(progress: JobProgress) -> dict:
        with session_scope() as db:
            batch = kb_repo.get_batch(db, batch_id)
            if batch is not None:
                kb_repo.update_batch(
                    db, batch,
                    status="running", started_at=datetime.now(timezone.utc),
                )

        queries, mode_desc = _select_queries(mode)
        # max_total 未指定时按所选检索式的上限之和；指定时按比例缩减每个检索式的额度，
        # 保证覆盖全部主题而不是只采前几个
        if max_total and queries:
            total_lim = sum(getattr(q, "limit", 50) for q in queries) or 1
            scale = min(1.0, max_total / total_lim)
        else:
            scale = 1.0

        progress.total = len(queries)
        progress.stage = f"{mode_desc}：共 {len(queries)} 个检索式"

        stats_total = {
            "documents_created": 0, "documents_updated": 0, "documents_skipped": 0,
            "chunks_created": 0, "chunks_embedded": 0,
        }
        errors: list[str] = []
        seen_pmids: set[str] = set()

        with PubMedClient() as client:
            for idx, cq in enumerate(queries, 1):
                if progress.status != "running":
                    break
                q = getattr(cq, "query", None) or (cq.get("query") if isinstance(cq, dict) else str(cq))
                limit = getattr(cq, "limit", 50) if not isinstance(cq, dict) else cq.get("limit", 50)
                phase = getattr(cq, "phase", None) if not isinstance(cq, dict) else cq.get("phase")
                # 应用总量缩放：保证每个主题都按比例采到，而不是只采前几个主题
                limit = max(1, int(limit * scale))
                name = getattr(cq, "name", "") if not isinstance(cq, dict) else cq.get("name", "")

                progress.current = idx - 1
                progress.stage = f"检索第 {idx}/{len(queries)} 个检索式：{name}"
                progress.message = f"{phase or ''} 上限 {limit} 篇"

                try:
                    pmids = client.search(str(q), max_results=limit, mindate=mindate)
                    pmids = [p for p in pmids if p not in seen_pmids]
                    seen_pmids.update(pmids)
                    if not pmids:
                        continue

                    articles = client.fetch(pmids)
                    with session_scope() as db:
                        st = ingest_articles(
                            db, articles, embed=embed, skip_no_abstract=True,
                            batch_size=batch_size,
                        )
                        # 记录批次归属，支持后续按批次追溯与回滚
                        attached = _attach_batch(db, articles, batch_id)
                        for k in stats_total:
                            stats_total[k] += getattr(st, k, 0) or 0
                        errors.extend(getattr(st, "errors", []) or [])
                    progress.message = (
                        f"{str(q)[:60]} → 新增 {st.documents_created} 篇"
                        + (f"，归属 {attached} 篇" if attached else "")
                    )
                except Exception as exc:  # noqa: BLE001
                    # 单个检索式失败不应中断整个批次
                    errors.append(f"检索式失败: {str(exc)[:150]}")
                    logger.warning("检索式失败，继续下一个: %s", str(exc)[:150])

        progress.current = len(queries)
        progress.stage = "写入批次记录"

        with session_scope() as db:
            batch = kb_repo.get_batch(db, batch_id)
            if batch is not None:
                kb_repo.finish_batch(
                    db, batch,
                    # 此前是 "success" if not errors else "success"（两侧同值），
                    # 无论失败与否都记 success，界面上看不出问题。
                    status="success" if not errors else "partial",
                    stats=stats_total,
                    errors=errors,
                )
                # 新入库文献不在图谱里，标记需重建
                batch.graph_rebuilt = False

        return {**stats_total, "errors": errors[:20], "queries": len(queries)}

    return registry.submit(batch_id, JOB_COLLECT, target).to_dict()


def run_search(
    *,
    batch_id: str,
    query: str,
    limit: int = 50,
    mindate: str | None = None,
    embed: bool = True,
    batch_size: int = 32,
) -> dict:
    """按自定义检索式采集。"""
    from app.db.session import session_scope
    from app.etl.ingest import ingest_articles
    from app.etl.pubmed_client import PubMedClient
    from app.repositories import kb_repo

    def target(progress: JobProgress) -> dict:
        with session_scope() as db:
            batch = kb_repo.get_batch(db, batch_id)
            if batch is not None:
                kb_repo.update_batch(
                    db, batch, status="running", started_at=datetime.now(timezone.utc)
                )

        progress.stage = "检索 PubMed"
        progress.message = query[:80]
        errors: list[str] = []

        with PubMedClient() as client:
            pmids = client.search(query, max_results=limit, mindate=mindate)
            progress.total = len(pmids)
            if not pmids:
                with session_scope() as db:
                    batch = kb_repo.get_batch(db, batch_id)
                    if batch is not None:
                        kb_repo.finish_batch(db, batch, status="success", stats={})
                return {"documents_created": 0, "message": "未检索到文献"}

            progress.stage = f"获取 {len(pmids)} 篇文献详情"
            articles = client.fetch(pmids)
            progress.current = len(pmids) // 2
            progress.stage = "入库并向量化"

            with session_scope() as db:
                st = ingest_articles(
                    db, articles, embed=embed, skip_no_abstract=True,
                    batch_size=batch_size,
                )
                attached = _attach_batch(db, articles, batch_id)
                stats = {
                    "documents_created": st.documents_created,
                    "documents_updated": st.documents_updated,
                    "documents_skipped": st.documents_skipped,
                    "chunks_created": st.chunks_created,
                    "chunks_embedded": st.chunks_embedded,
                }
                errors.extend(getattr(st, "errors", []) or [])

            progress.current = len(pmids)
            progress.stage = "写入批次记录"

        with session_scope() as db:
            batch = kb_repo.get_batch(db, batch_id)
            if batch is not None:
                kb_repo.finish_batch(db, batch, status="success", stats=stats, errors=errors)
                batch.graph_rebuilt = False

        return {**stats, "attached": attached, "errors": errors[:20]}

    return registry.submit(batch_id, JOB_SEARCH, target).to_dict()


def run_vectorize(*, batch_id: str, batch_size: int = 32) -> dict:
    """为未向量化的文本块补齐向量。

    这是**断点续跑**能力：入库时若中断，未完成向量化的块会残留，
    本任务将它们补齐。没有这一步，那些块虽然存在于数据库，
    但在向量检索路径下永远检索不到。
    """
    from app.db.session import session_scope
    from app.etl.ingest import vectorize_missing
    from app.repositories import kb_repo

    def target(progress: JobProgress) -> dict:
        with session_scope() as db:
            batch = kb_repo.get_batch(db, batch_id)
            if batch is not None:
                kb_repo.update_batch(
                    db, batch, status="running", started_at=datetime.now(timezone.utc)
                )

        progress.stage = "统计待补向量的文本块"
        from app.db.session import session_scope as ss
        from app.models.knowledge import Chunk
        from sqlalchemy import func, select
        with ss() as db:
            pending = db.execute(
                select(func.count()).select_from(Chunk).where(Chunk.embedding.is_(None))
            ).scalar_one()

        progress.total = pending
        if pending == 0:
            progress.message = "所有文本块均已有向量，无需处理"
            with session_scope() as db:
                batch = kb_repo.get_batch(db, batch_id)
                if batch is not None:
                    kb_repo.finish_batch(
                        db, batch, status="success",
                        stats={"chunks_vectorized": 0},
                    )
            return {"chunks_vectorized": 0, "message": "无需补向量"}

        progress.stage = f"向量化 {pending} 个文本块（首次会加载模型，约 60 秒）"
        # 分批执行以便上报进度
        done_total = 0
        while done_total < pending and progress.status == "running":
            with session_scope() as db:
                n, secs = vectorize_missing(db, limit=200, batch_size=batch_size)
            if n == 0:
                break
            done_total += n
            progress.current = min(done_total, pending)
            progress.message = f"已完成 {done_total}/{pending} 块（{secs:.1f} 秒/批）"

        progress.stage = "写入批次记录"
        with session_scope() as db:
            batch = kb_repo.get_batch(db, batch_id)
            if batch is not None:
                # 向量化一块都没产出时不再写 success：
                # 否则"全部已向量化"的结论在失败时同样成立，掩盖了问题。
                failed = done_total == 0 and pending > 0
                kb_repo.finish_batch(
                    db, batch,
                    status="failed" if failed else "success",
                    stats={"chunks_vectorized": done_total, "pending_before": pending},
                    errors=(["向量化未产出任何向量"] if failed else None),
                )

        return {"chunks_vectorized": done_total, "pending_before": pending}

    return registry.submit(batch_id, JOB_VECTORIZE, target).to_dict()


def _attach_batch(db, articles, batch_id: str) -> int:
    """把本次入库的文献归属到批次。

    ingest_articles 不接收 batch_id 参数（ETL 层不应感知维护功能），
    因此入库后按 PMID/标题匹配一次，回填 batch_id。

    这样做的代价是多一次查询，好处是 ETL 层保持独立，
    命令行入库与界面入库共用同一实现。
    """
    from sqlalchemy import or_, select
    from app.models.knowledge import Document

    pmids = [a.pmid for a in articles if getattr(a, "pmid", None)]
    if not pmids:
        return 0
    docs = db.execute(
        select(Document).where(
            Document.pmid.in_(pmids),
            Document.batch_id.is_(None),
        )
    ).scalars().all()
    for d in docs:
        d.batch_id = batch_id
    db.flush()
    return len(docs)
