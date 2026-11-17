"""Bounded incremental graph repair, independent of the review questions.

Maximum 24 model calls. Retains old graph; current-source filtering quarantines
unresolvable references. Does not claim expert review of extracted relations.
"""
import json
import argparse
import hashlib
from pathlib import Path
from app.core.config import PROJECT_ROOT, settings
from app.db.session import session_scope
from app.services.graph_ingest import build_graph
from app.services.extraction import select_chunks_for_extraction
from sqlalchemy import text


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--apply',action='store_true',help='Send selected source text to the configured model and update graph; requires explicit authorization')
    args=parser.parse_args()
    marker=PROJECT_ROOT/'data/eval/graph_current_build_20261105.json'
    if marker.exists():
        print('Completed batch exists; refusing duplicate re-import:',marker)
        return
    review=json.loads((PROJECT_ROOT/'data/eval/review_set_20261104.json').read_text(encoding='utf-8'))
    excluded={item['gold_chunk_id'] for item in review['items']}
    plan_path=PROJECT_ROOT/'data/eval/graph_refresh_transfer_plan_20261105.json'
    if plan_path.exists():
        plan=json.loads(plan_path.read_text(encoding='utf-8'))
        selected=plan['chunks']
    else:
        with session_scope() as db:
            selected=[c for c in select_chunks_for_extraction(db,limit=24+len(excluded)) if c['chunk_id'] not in excluded][:24]
        plan={'destination':settings.llm.llm_base_url,'model':settings.llm.llm_model,'maximum_calls':24,
            'maximum_characters':sum(len(c['content']) for c in selected),'review_status':'pending_explicit_data_transfer_authorization',
            'chunks':[dict(c,content_sha256=hashlib.sha256(c['content'].encode()).hexdigest()) for c in selected]}
        plan_path.write_text(json.dumps(plan,ensure_ascii=False,indent=2),encoding='utf-8')
    if not args.apply:
        print(json.dumps({'status':'preview_only_no_external_calls','destination':plan['destination'],
            'model':plan['model'],'chunks':len(selected),'characters':plan['maximum_characters'],'plan':str(plan_path)},ensure_ascii=False))
        return
    if plan['destination']!=settings.llm.llm_base_url or plan['model']!=settings.llm.llm_model:
        raise ValueError('Configured destination differs from the authorized transfer plan')
    if len(selected)!=24 or sum(len(c['content']) for c in selected)!=13226:
        raise ValueError('Source batch differs from authorized limits')
    with session_scope() as db:
        for chunk in selected:
            current=db.execute(text('SELECT c.content FROM kb_chunk c JOIN kb_document d ON d.id=c.document_id WHERE c.id=:id AND d.is_active'),{'id':chunk['chunk_id']}).scalar_one()
            if current!=chunk['content'] or chunk['chunk_id'] in excluded:
                raise ValueError('Source content changed since authorization')
    with session_scope() as db:
        report=build_graph(db,limit=24,cache_path=PROJECT_ROOT/'data/eval/graph_current_cache_20261105.json',
            progress_every=1,excluded_chunk_ids=excluded,selected_chunks=selected,extraction_max_retries=1)
    report['excluded_review_chunk_ids']=sorted(excluded)
    report['review_status']='machine_extracted_pending_independent_review'
    marker.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False),flush=True)


if __name__=='__main__':
    main()
