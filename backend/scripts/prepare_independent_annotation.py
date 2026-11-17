"""Reserve source groups for independent questions and prepare unlabelled image pairs.

Does not manufacture questions, relevance labels, clinical decisions, or models.
"""
import hashlib
import itertools
import json
import re
from datetime import datetime, timezone

from sqlalchemy import select

from app.core.config import PROJECT_ROOT
from app.db.session import session_scope
from app.models.knowledge import Document, Chunk, ImageAsset
from scripts.prepare_review_set import corpus_snapshot


def source_groups(documents: list[dict]) -> list[list[dict]]:
    """Union duplicate DOI, PMID or normalized title before splitting sources."""
    parent = list(range(len(documents)))
    seen = {}
    def root(index):
        while index != parent[index]:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index
    for index, document in enumerate(documents):
        doi = re.sub(r'^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)', '',
                     (document.get('doi') or '').lower().strip())
        title = re.sub(r'[\W_]+', '', (document.get('title') or '').casefold())
        identifiers = [('doi', doi), ('pmid', (document.get('pmid') or '').strip()), ('title', title)]
        for key, value in identifiers:
            if not value:
                continue
            if (key, value) in seen:
                parent[root(index)] = root(seen[(key, value)])
            else:
                seen[(key, value)] = index
    groups = {}
    for index, document in enumerate(documents):
        groups.setdefault(root(index), []).append(document)
    return sorted((sorted(group, key=lambda d: d['id']) for group in groups.values()),
                  key=lambda group: hashlib.sha256('|'.join(d['id'] for d in group).encode()).hexdigest())


def main():
    dataset = json.loads((PROJECT_ROOT / 'data/eval/review_set_20261104.json').read_text(encoding='utf-8'))
    development_ids = {item['gold_document_id'] for item in dataset['items']}
    with session_scope() as db:
        corpus = corpus_snapshot(db)
        documents = [dict(id=str(d.id), title=d.title, doi=d.doi, pmid=d.pmid,
                          phase=d.phase, evidence_level=d.evidence_level)
                     for d in db.scalars(select(Document).where(Document.is_active.is_(True)).order_by(Document.id))]
        groups = source_groups(documents)
        excluded = [group for group in groups if any(d['id'] in development_ids for d in group)]
        candidates = [group for group in groups if not any(d['id'] in development_ids for d in group)]
        reserved = candidates[:20]
        tasks = []
        for number, group in enumerate(reserved, 1):
            ids = [d['id'] for d in group]
            chunks = db.scalars(select(Chunk).where(Chunk.document_id.in_(ids),
                                                     Chunk.section != 'Title')
                                .order_by(Chunk.document_id, Chunk.chunk_index)).all()
            # Full source evidence is provided; no automatic positive label.
            tasks.append({'task_id': f'HOLDOUT-{number:02}', 'documents': group,
                          'evidence': [{'chunk_id': str(c.id), 'document_id': str(c.document_id),
                                        'section': c.section, 'page_start': c.page_start,
                                        'page_end': c.page_end, 'content': c.content,
                                        'content_sha256': hashlib.sha256(c.content.encode()).hexdigest()}
                                       for c in chunks],
                          'reviewer': None, 'reviewed_at': None, 'question': None,
                          'relevant_chunk_ids': [], 'reference_claims_and_quotes': [],
                          'insufficient_evidence_expected': None, 'status': 'pending_independent_annotation'})
        images = [dict(image_id=str(i.id), title=i.title, source=i.source,
                       file_sha256=i.file_hash, is_public=i.is_public,
                       feature_model=i.feature_model, source_group=None,
                       license_confirmed=None, deidentification_confirmed=None)
                  for i in db.scalars(select(ImageAsset).order_by(ImageAsset.id))]
    report = {'created_at': datetime.now(timezone.utc).isoformat(), 'corpus': corpus,
              'status': 'unlabelled_reservation_not_a_validated_holdout',
              'external_calls': 0, 'database_writes': 0,
              'split_method': 'deterministic SHA256 ordering; union DOI/PMID/normalized title; exclude development source groups',
              'limitations': ['This corpus and its developer have already been inspected; independent question writers and frozen labels are still required.',
                             'Question writers must define relevance without seeing system rankings or development questions.',
                             'Source identity beyond DOI/PMID/title needs manual duplicate review.',
                             'Three images are insufficient for encoder selection; source groups and licences are unconfirmed.'],
              'excluded_development_document_ids': sorted(d['id'] for group in excluded for d in group),
              'reserved_document_ids': sorted(d['id'] for group in reserved for d in group),
              'question_tasks': tasks, 'images': images,
              'image_pairs': [{'image_a': a['image_id'], 'image_b': b['image_id'],
                               'anatomy_similarity': None, 'procedure_similarity': None,
                               'visual_similarity': None, 'reviewer_a': None, 'reviewer_b': None,
                               'adjudication': None, 'split': None, 'status': 'pending_annotation'}
                              for a, b in itertools.combinations(images, 2)]}
    assert not (set(report['reserved_document_ids']) & set(report['excluded_development_document_ids']))
    destination = PROJECT_ROOT / 'data/reviews' / datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    destination.mkdir(parents=True, exist_ok=False)
    (destination / 'independent_annotation.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'directory': str(destination), 'reserved_source_groups': len(tasks),
                      'images': len(images), 'unlabelled_pairs': len(report['image_pairs']),
                      'clinical_labels_created': 0}, ensure_ascii=False))


if __name__ == '__main__':
    main()
