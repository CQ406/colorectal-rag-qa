"""Isolated browser-test server using a newly created PostgreSQL database.

Never modifies the normal project database or source image directory.
"""
from __future__ import annotations

import io
import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import create_engine, text
from app.core.config import PROJECT_ROOT, settings


def main():
    name = "rag_e2e_" + uuid.uuid4().hex[:12]
    admin = create_engine(settings.postgres.dsn, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    settings.postgres.postgres_db = name
    settings.auth.admin_username = "e2e-admin"
    settings.auth.admin_password = "E2eOnly!234"
    settings.auth.enabled = True
    from app.db.session import engine, session_scope
    from app.models import knowledge, system, batch
    from app.models.base import Base
    from app.models.system import User
    from app.core.security import hash_password
    from app.services import storage, image_analysis
    from app.repositories import image_repo, case_repo
    from app.models.knowledge import CaseRecord
    from PIL import Image
    from app.main import app
    import uvicorn

    root = PROJECT_ROOT / "data/e2e" / name
    root.mkdir(parents=True, exist_ok=True)
    (PROJECT_ROOT / "data/e2e/runtime.json").write_text(json.dumps({"database": name}), encoding="utf-8")
    storage._storage = storage.LocalStorage(root / "storage")
    # Avoid loading GPU models for browser workflow tests.
    case_repo._embedding = lambda _text: None
    with engine.begin() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
    Base.metadata.create_all(engine)
    with session_scope() as db:
        for role in ("admin", "physician"):
            db.add(User(username=f"e2e-{role}", password_hash=hash_password("E2eOnly!234"),
                        role=role, is_active=True, display_name=f"E2E {role}"))
        ids = []
        for index in range(26):
            stream = io.BytesIO()
            Image.new("RGB", (16, 16), (index * 7, 20, 100)).save(stream, format="PNG")
            image, _ = image_repo.create(db, filename=f"fixture-{index:02}.png", data=stream.getvalue(),
                mime_type="image/png", title=f"E2E image {index:02}", source="Synthetic browser-test fixture",
                is_public=index != 25, phase="intraop")
            ids.append(str(image.id))
            image.created_at = datetime.now(timezone.utc) + timedelta(seconds=index)
        db.add(CaseRecord(case_code="E2E-DRAFT", title="E2E synthetic draft", phase="preop", is_public=False,
            image_ids=[ids[0]], source_title="Synthetic browser-test fixture (not a real case)",
            source_url="https://example.org/e2e-fixture", source_license="Test fixture only",
            source_note="Synthetic non-clinical fields for workflow tests only.", tags=["E2E"], complications=[]))
    from app.etl.ingest import ingest_articles
    from app.etl.pubmed_client import ParsedArticle, AbstractSection
    with session_scope() as db:
        article = ParsedArticle(pmid='e2e-revision', title='E2E original document',
                                abstract_sections=[AbstractSection('Evidence', 'Original evidence fixture. ' * 8, 3, 3)])
        first = ingest_articles(db, [article], embed=False)
        assert not first.errors
        article.title = 'E2E revised document'
        article.abstract_sections = [AbstractSection('Evidence', 'Revised evidence fixture. ' * 8, 5, 5)]
        second = ingest_articles(db, [article], embed=False)
        assert not second.errors
    print(f"E2E database ready: {name}", flush=True)
    try:
        uvicorn.run(app, host="127.0.0.1", port=8011, log_level="warning")
    finally:
        engine.dispose()
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


if __name__ == "__main__":
    main()
