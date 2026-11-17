"""运行时配置是否真正接入检索链路的回归测试。

覆盖三件事：
  1. `config_repo.effective()` 取到的是配置表里的值，而不是代码里的 settings
  2. 改配置后缓存立刻失效（否则管理界面"保存成功"却还按旧值跑）
  3. 重排候选上限可以被调用方覆盖（配置项 retrieval.rerank_candidate_limit）

为什么要测：历史上出现过"管理界面把权重改完保存，运行行为毫无变化"，
以及"代码默认值改了、配置表没改，导致生效的是旧值"两类问题。
"""
from __future__ import annotations

import sys
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


def test_effective_returns_db_value_and_invalidates_on_update():
    from app.db.session import session_scope
    from app.repositories import config_repo

    key = "retrieval.min_keyword_overlap"
    with session_scope() as db:
        original = config_repo.get_value(db, key)
        try:
            config_repo.update_value(db, key, 0.123456, updated_by="unit-test")
            db.commit()
            # 写入时必须让缓存失效，否则这里仍会读到旧值
            assert abs(float(config_repo.effective(db)[key]) - 0.123456) < 1e-9
        finally:
            config_repo.update_value(db, key, original, updated_by="unit-test")
            db.commit()

    with session_scope() as db:
        assert abs(float(config_repo.effective(db)[key]) - float(original)) < 1e-9


def test_effective_covers_all_retrieval_keys():
    from app.db.session import session_scope
    from app.repositories import config_repo

    with session_scope() as db:
        eff = config_repo.effective(db)
    for key in (
        "retrieval.vector_weight",
        "retrieval.keyword_weight",
        "retrieval.graph_weight",
        "retrieval.min_keyword_overlap",
        "retrieval.rerank_candidate_limit",
        "retrieval.evidence_boost_strength",
    ):
        assert key in eff, f"配置缺少 {key}"


def test_rerank_budget_accepts_override():
    from app.services import hybrid_search

    # 传入 5 时，GPU 上应原样返回 5；CPU 上被压到 min(20, 5) = 5
    assert hybrid_search._rerank_budget(5) == 5
    # 传入 100 时，CPU 上要被压到 20，GPU 上原样 100
    budget = hybrid_search._rerank_budget(100)
    assert budget == 100 or budget == 20
