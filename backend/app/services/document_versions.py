"""Preserve citation text and metadata before replacing a document's chunks."""
import hashlib
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.knowledge import Chunk, ChunkRevision, Document


def citation_snapshot(chunk: Chunk, doc: Document) -> dict:
    return {
        'chunk_id': str(chunk.id), 'document_id': str(doc.id),
        'doc_title': doc.title, 'doc_journal': doc.journal, 'doc_year': doc.year,
        'section': chunk.section, 'page_start': chunk.page_start, 'page_end': chunk.page_end,
        'chunk_index': chunk.chunk_index, 'evidence_level': chunk.evidence_level,
        'phase': chunk.phase, 'content': chunk.content or '',
        'char_count': len(chunk.content or ''), 'doc_is_active': bool(doc.is_active),
        'source_sha256': doc.file_hash, 'source_file_path': doc.file_path,
        'doi': doc.doi, 'pmid': doc.pmid,
    }


def archive_chunks(db: Session, doc: Document) -> int:
    """Call before changing metadata; the caller owns the transaction."""
    revision_id = uuid.uuid4()
    chunks = db.scalars(select(Chunk).where(Chunk.document_id == doc.id)
                        .order_by(Chunk.chunk_index)).all()
    for chunk in chunks:
        snapshot = citation_snapshot(chunk, doc)
        db.add(ChunkRevision(id=chunk.id, document_id=doc.id, revision_id=revision_id,
                             snapshot=snapshot,
                             content_sha256=hashlib.sha256(snapshot['content'].encode()).hexdigest()))
    db.flush()
    return len(chunks)


def archived_citation(db: Session, chunk_id: str) -> dict | None:
    try:
        identity = uuid.UUID(chunk_id)
    except ValueError:
        return None
    saved = db.get(ChunkRevision, identity)
    if saved is None:
        return None
    return {**saved.snapshot, 'archived': True, 'doc_is_active': False,
            'revision_id': str(saved.revision_id), 'content_sha256': saved.content_sha256}
