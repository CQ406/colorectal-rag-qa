"""Preview a local import, including OCR warnings and actual chunk pages."""
import argparse
import json
from pathlib import Path

from app.etl.manual_import import parse_pdf, parse_docx, parse_markdown, to_parsed_article
from app.etl.ingest import build_chunks


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('path', type=Path)
    args = parser.parse_args()
    source = args.path
    if source.suffix.lower() == '.pdf':
        entry = parse_pdf(source)
    elif source.suffix.lower() == '.docx':
        entry = parse_docx(source)
    elif source.suffix.lower() == '.md':
        entry = parse_markdown(source.read_text(encoding='utf-8-sig'), source_path=str(source))
    else:
        parser.error('Supported inputs: .pdf, .docx, .md')
    chunks = build_chunks(to_parsed_article(entry), None)
    print(json.dumps({'title': entry.title, 'page_count': entry.page_count,
                      'source_sha256': entry.source_sha256,
                      'evidence_hint': entry.evidence_level,
                      'warnings': entry.parse_warnings,
                      'chunks': [{'index': c.index, 'section': c.section,
                                  'page_start': c.page_start, 'page_end': c.page_end,
                                  'characters': c.char_count, 'preview': c.content[:400]}
                                 for c in chunks]}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
