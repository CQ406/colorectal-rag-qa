"""Exercise revision replacement and rollback in a fresh disposable database."""
import json
import uuid

from sqlalchemy import create_engine, select, text, func
from sqlalchemy.orm import Session

from app.core.config import PROJECT_ROOT, settings
from app.models.base import Base
from app.models.knowledge import Chunk, ChunkRevision, Document
from app.etl.ingest import ingest_articles
from app.etl.pubmed_client import ParsedArticle, AbstractSection
from app.api.v1.search import chunk_detail
from app.api.v1.knowledge import document_revisions
from app.repositories.search_repo import count_active_chunks
from app.repositories.kb_repo import delete_document
from app.services import document_versions


def main():
    name = 'rag_revision_' + uuid.uuid4().hex[:12]
    admin = create_engine(settings.postgres.dsn, isolation_level='AUTOCOMMIT')
    target = None
    created = False
    report = {'database': name, 'production_data_written': False, 'external_calls': 0}
    try:
        with admin.connect() as connection:
            connection.execute(text(f'CREATE DATABASE "{name}"'))
            created = True
        target = create_engine(admin.url.set(database=name))
        with target.begin() as connection:
            connection.execute(text('CREATE EXTENSION IF NOT EXISTS vector'))
            connection.execute(text('CREATE EXTENSION IF NOT EXISTS pg_trgm'))
        Base.metadata.create_all(target)
        article = ParsedArticle(pmid='revision-fixture', title='Original title',
                                abstract_sections=[AbstractSection('Results', 'Old evidence. ' * 15, 3, 3)])
        with Session(target, expire_on_commit=False) as db:
            first = ingest_articles(db, [article], embed=False)
            assert first.documents_created == 1 and not first.errors
            db.commit()
            doc = db.scalar(select(Document))
            original = db.scalar(select(Chunk).where(Chunk.section == 'Results'))
            original_id = str(original.id)
            initial_count = count_active_chunks(db)
            identical = ingest_articles(db, [article], embed=False)
            assert identical.documents_skipped == 1 and not identical.errors
            assert db.scalar(select(func.count()).select_from(ChunkRevision)) == 0
            assert db.get(Chunk, uuid.UUID(original_id)) is not None
            db.commit()
            article.title = 'Revised title'
            article.abstract_sections = [AbstractSection('Results', 'Revised evidence. ' * 15, 5, 5)]
            revised = ingest_articles(db, [article], embed=False)
            assert revised.documents_updated == 1 and not revised.errors
            db.commit()
            historical = chunk_detail(original_id, db)
            assert historical['content'] == 'Old evidence. ' * 14 + 'Old evidence.'
            assert historical['doc_title'] == 'Original title' and historical['page_start'] == 3
            assert historical['archived'] and not historical['doc_is_active']
            assert count_active_chunks(db) == initial_count
            current = db.scalar(select(Chunk).where(Chunk.section == 'Results'))
            current_id = current.id
            assert current.page_start == 5
            versions = document_revisions(str(doc.id), None, db)
            assert len(versions['revisions']) == 1
            db.commit()
            # A failed replacement must not leave snapshots or erase current IDs.
            original_archive = document_versions.archive_chunks
            import app.etl.ingest as ingest_module
            def fail_after_archive(db, document):
                original_archive(db, document)
                raise RuntimeError('deliberate failure after archive')
            ingest_module.archive_chunks = fail_after_archive
            try:
                article.title = 'Must roll back'
                failed = ingest_articles(db, [article], embed=False)
                assert failed.errors and failed.documents_updated == 0
                db.commit()
                assert db.get(Chunk, current_id) is not None
                assert len(document_revisions(str(doc.id), None, db)['revisions']) == 1
            finally:
                ingest_module.archive_chunks = original_archive
            # One malformed article cannot roll back a preceding valid article.
            valid = ParsedArticle(pmid='other-fixture', title='Another source',
                                  abstract_sections=[AbstractSection('Result', 'Source evidence. ' * 15)])
            invalid = ParsedArticle(pmid='bad-fixture', title='Missing local source',
                                    abstract_sections=valid.abstract_sections,
                                    source_path=str(PROJECT_ROOT / 'data/nonexistent-revision-fixture.txt'))
            mixed = ingest_articles(db, [valid, invalid], embed=False)
            assert mixed.documents_created == 1 and len(mixed.errors) == 1
            db.commit()
            assert db.scalar(select(Document).where(Document.pmid == 'other-fixture')) is not None
            # Hard deletion intentionally purges archives as well.
            delete_document(db, doc)
            db.commit()
            assert db.scalar(select(func.count()).select_from(ChunkRevision)) == 0
        report.update(status='passed', checks=['identical_import_preserves_ids',
                      'revised_text_and_page_retained', 'historical_citation_resolves',
                      'archives_excluded_from_retrieval_counts', 'failed_replacement_rolls_back',
                      'batch_failure_preserves_success', 'hard_delete_purges_archives'])
    finally:
        if target is not None:
            target.dispose()
        if created:
            # Exact name generated here; never enumerate databases for deletion.
            assert name.startswith('rag_revision_') and len(name) == 25
            with admin.connect() as connection:
                connection.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
            report['isolated_database_removed'] = True
        admin.dispose()
    (PROJECT_ROOT / 'data/eval/document_versions_verification_20261105.json').write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False))


if __name__ == '__main__':
    main()
