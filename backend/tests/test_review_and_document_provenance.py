import copy
import hashlib
import uuid
from types import SimpleNamespace

import pytest

from app.etl.manual_import import parse_pdf, to_parsed_article, ManualParseError
from app.etl.ingest import build_chunks
from app.models.knowledge import Chunk, Document
from app.services.document_versions import citation_snapshot, archived_citation
from scripts.prepare_clinical_review import case_dossier
from scripts.prepare_public_cases import parse_article
from scripts.prepare_independent_annotation import source_groups


def test_pdf_real_page_numbers_survive_empty_page_and_chunking(tmp_path):
    import fitz
    path = tmp_path / 'pages.pdf'
    with fitz.open() as pdf:
        pdf.new_page().insert_text((72, 72), 'Surgical evidence 2025 first page.')
        pdf.new_page()
        pdf.new_page().insert_text((72, 72), 'Outcome evidence on the third page.')
        pdf.save(path)
    entry = parse_pdf(path)
    assert entry.page_count == 3 and entry.section_pages == [1, 3]
    assert entry.source_sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
    assert any('第2页' in warning for warning in entry.parse_warnings)
    article = to_parsed_article(entry)
    assert article.evidence_hint == 'other' and 'Guideline' not in article.publication_types
    chunks = build_chunks(article, None)
    assert [(c.page_start, c.page_end) for c in chunks] == [(None, None), (1, 1), (3, 3)]


def test_image_only_pdf_requires_ocr(tmp_path):
    import fitz
    path = tmp_path / 'scan.pdf'
    with fitz.open() as pdf:
        pdf.new_page()
        pdf.save(path)
    with pytest.raises(ManualParseError, match='OCR'):
        parse_pdf(path)


def test_review_packet_preserves_sibling_evidence_and_refuses_changed_source():
    raw = b'''<article><front><article-meta><title-group><article-title>Case</article-title></title-group>
    <permissions><license href="https://creativecommons.org/licenses/by/4.0/">CC BY</license></permissions>
    </article-meta></front><body><sec><title>Case presentation</title><p>A 61-year-old man underwent surgery.</p></sec>
    <sec><title>Diagnostic assessment</title><p>Follow-up revealed a fistula.</p></sec>
    <sec><title>Discussion</title><p>This is not patient-specific evidence.</p></sec></body></article>'''
    case = parse_article(raw, 'PMC1', 'postop', 'Label')
    result = case_dossier(case, raw)
    assert result['candidate_fields']['demographics']['age'] == 61
    assert result['candidate_fields']['demographics']['sex'] == 'M'
    assert result['additional_evidence_not_in_frozen_excerpt'] == ['s2-p1']
    assert len(result['paragraphs']) == 2
    assert result['review']['decision'] is None
    changed = copy.deepcopy(case)
    changed['history'] += 'Modified.'
    with pytest.raises(ValueError, match='no longer matches'):
        case_dossier(changed, raw)


def test_old_citation_keeps_metadata_and_is_explicitly_archived():
    document = Document(id=uuid.uuid4(), title='Original title', journal='Original journal',
                        year=2024, is_active=True)
    chunk = Chunk(id=uuid.uuid4(), document_id=document.id, content='Original evidence.',
                  chunk_index=2, page_start=3, page_end=3, phase='postop')
    snapshot = citation_snapshot(chunk, document)
    document.title = 'New title'
    assert snapshot['doc_title'] == 'Original title' and snapshot['page_start'] == 3
    saved = SimpleNamespace(snapshot=snapshot, revision_id=uuid.uuid4(), content_sha256='sha')
    db = SimpleNamespace(get=lambda model, identity: saved if identity == chunk.id else None)
    result = archived_citation(db, str(chunk.id))
    assert result['archived'] and not result['doc_is_active']
    assert result['content'] == 'Original evidence.'
    assert archived_citation(db, 'invalid') is None


def test_source_split_unions_transitive_duplicate_identifiers():
    documents = [dict(id='1', doi='https://doi.org/10.1/ABC', title='First title'),
                 dict(id='2', doi='10.1/abc', title='Same title'),
                 dict(id='3', title='Same-title!'), dict(id='4', title='Other')]
    groups = source_groups(documents)
    assert sorted(sorted(d['id'] for d in g) for g in groups) == [['1', '2', '3'], ['4']]
    assert source_groups(list(reversed(documents))) == groups


def test_source_original_is_preserved_and_changed_input_is_rejected(monkeypatch, tmp_path):
    from app.etl import ingest
    from app.etl.pubmed_client import ParsedArticle, AbstractSection
    monkeypatch.setattr(ingest, 'PROJECT_ROOT', tmp_path)
    source = tmp_path / 'original.md'
    source.write_text('# Title\nSource text.', encoding='utf-8')
    original = source.read_bytes()
    article = ParsedArticle(pmid=None, title='Title', source='manual',
                            abstract_sections=[AbstractSection('Body', 'Source text.')],
                            source_path=str(source), source_sha256=hashlib.sha256(original).hexdigest())
    doc, _ = ingest._build_document(None, article, Document(title='Title'), source_db='manual')
    preserved = tmp_path / doc.file_path
    assert preserved.read_bytes() == original
    source.write_text('Modified.', encoding='utf-8')
    assert preserved.read_bytes() == original
    with pytest.raises(ValueError, match='changed after parsing'):
        ingest._build_document(None, article, Document(title='Title'), source_db='manual')


def test_e2e_cleanup_rejects_production_or_unrelated_processes():
    import sys
    from scripts.cleanup_e2e import verified_listener
    from app.core.config import PROJECT_ROOT
    python = SimpleNamespace(exe=lambda: sys.executable,
                             cmdline=lambda: [sys.executable, '-m', 'tests.e2e_server'])
    assert verified_listener(python, 8011, 'node')
    assert not verified_listener(python, 8000, 'node')
    unrelated = SimpleNamespace(exe=lambda: sys.executable,
                                cmdline=lambda: [sys.executable, '-m', 'uvicorn', 'app.main:app'])
    assert not verified_listener(unrelated, 8011, 'node')
    node = str(PROJECT_ROOT / 'node.exe')
    vite = SimpleNamespace(exe=lambda: node,
                           cmdline=lambda: ['node', str(PROJECT_ROOT / 'frontend/node_modules/vite/bin/vite.js'), '--port', '5174'])
    assert verified_listener(vite, 5174, node)
    assert not verified_listener(vite, 5173, node)
