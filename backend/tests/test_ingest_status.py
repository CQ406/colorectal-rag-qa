"""入库状态机的回归测试。

背景（2026-11-14 审查）：向量化失败曾被静默吞掉，导致三层"假成功"——
文本块入库但无向量、`doc.status` 提前写成 embedded、批次统计恒为 success。
修复后要求：

  1. 向量化抛错时，`ingest_articles` 必须把异常上抛，并把相关文档标为 failed
  2. 向量化成功时，状态才推进到 embedded
  3. 批次状态在出错时不再是 success

测试会用一篇合成文献，跑完立即删除，避免污染 120 篇语料（评测集指纹依赖篇数）。
"""
from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _db_available() -> bool:
    try:
        from app.db.session import session_scope
        from sqlalchemy import text

        with session_scope() as db:
            db.execute(text("SELECT 1"))
        return True
    except Exception:  # noqa: BLE001
        return False


pytestmark = pytest.mark.skipif(not _db_available(), reason="需要数据库")


def _make_article(title: str):
    from app.etl.pubmed_client import AbstractSection, ParsedArticle

    return ParsedArticle(
        pmid=None,
        title=title,
        abstract_sections=[AbstractSection(label="正文", text="测试用正文。" * 40)],
        language="chi",
        source="manual",
        phase_hint="postop",
        evidence_hint="review",
    )


def _cleanup(title: str) -> None:
    from app.db.session import session_scope
    from app.models.knowledge import Chunk, Document
    from sqlalchemy import delete, select

    with session_scope() as db:
        ids = [r for r in db.execute(
            select(Document.id).where(Document.title == title)
        ).scalars().all()]
        for doc_id in ids:
            db.execute(delete(Chunk).where(Chunk.document_id == doc_id))
            db.execute(delete(Document).where(Document.id == doc_id))
        db.commit()


def test_embedding_failure_marks_document_failed(monkeypatch):
    """向量化失败 → 抛错 + 状态 failed + 有错误信息。"""
    from app.db.session import session_scope
    from app.etl import ingest as ingest_mod
    from app.models.knowledge import Document
    from app.services import search_service
    from sqlalchemy import select

    title = f"[测试] 向量化失败用例 {uuid.uuid4().hex[:8]}"

    def boom(*args, **kwargs):
        raise RuntimeError("模拟向量化失败")

    monkeypatch.setattr(search_service, "embed_texts", boom)

    try:
        with session_scope() as db:
            with pytest.raises(RuntimeError):
                ingest_mod.ingest_articles(db, [_make_article(title)], embed=True)
            db.commit()

        with session_scope() as db:
            doc = db.execute(
                select(Document).where(Document.title == title)
            ).scalar_one_or_none()
            assert doc is not None, "文档应已入库（块无向量）"
            assert doc.status == "failed", f"状态应为 failed，实际 {doc.status}"
            assert doc.error_message and "向量化失败" in doc.error_message
    finally:
        _cleanup(title)


def test_successful_embedding_sets_embedded(monkeypatch):
    """向量化成功 → 状态 embedded，且错误信息被清空。"""
    from app.db.session import session_scope
    from app.etl import ingest as ingest_mod
    from app.models.knowledge import Document
    from app.services import search_service
    from sqlalchemy import select

    title = f"[测试] 向量化成功用例 {uuid.uuid4().hex[:8]}"

    def fake_embed(texts, batch_size=None, **kwargs):
        return [[0.001] * 1024 for _ in texts], 0.01

    monkeypatch.setattr(search_service, "embed_texts", fake_embed)

    try:
        with session_scope() as db:
            stats = ingest_mod.ingest_articles(db, [_make_article(title)], embed=True)
            db.commit()
            assert stats.chunks_embedded == stats.chunks_created

        with session_scope() as db:
            doc = db.execute(
                select(Document).where(Document.title == title)
            ).scalar_one_or_none()
            assert doc is not None
            assert doc.status == "embedded", f"状态应为 embedded，实际 {doc.status}"
            assert doc.error_message in (None, "")
    finally:
        _cleanup(title)
