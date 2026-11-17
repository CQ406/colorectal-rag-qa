"""Run five explicit retrieval configurations against the frozen live corpus.

Uses the running API to share already loaded GPU models; no external LLM calls.
Records every returned chunk, path timings, rank and request configuration.
"""
import argparse
import json
import time
from datetime import datetime, timezone
from urllib.request import Request, urlopen
from app.core.config import PROJECT_ROOT, settings
from pathlib import Path
from app.db.session import session_scope
from scripts.prepare_review_set import corpus_snapshot

CONFIGS = [
    ('vector', dict(recall_mode='vector', use_graph=False, use_rerank=False)),
    ('hybrid', dict(recall_mode='hybrid', use_graph=False, use_rerank=False)),
    ('hybrid_graph', dict(recall_mode='hybrid', use_graph=True, use_rerank=False)),
    ('hybrid_rerank', dict(recall_mode='hybrid', use_graph=False, use_rerank=True)),
    ('hybrid_graph_rerank', dict(recall_mode='hybrid', use_graph=True, use_rerank=True)),
]


def live_graph_audit() -> dict:
    """实时核对图谱来源块与当前语料的一致性。

    为什么不再直接读固定文件：此前这里抄的是 `graph_source_audit_20261105.json`，
    扩图之后新生成的报告仍然显示「有效来源块 23 / 1556 节点」，
    与实际（898 / 4156）不符 —— 报告会自己骗自己。
    """
    from app.core.config import settings as _s
    from app.models.knowledge import Chunk
    from sqlalchemy import select
    try:
        from neo4j import GraphDatabase
        drv = GraphDatabase.driver(_s.neo4j.neo4j_uri, auth=(_s.neo4j.neo4j_user, _s.neo4j.neo4j_password))
        try:
            with drv.session() as sess:
                rows = sess.run(
                    'MATCH (e:Entity) WHERE size(e.chunk_ids) > 0 RETURN e.chunk_ids AS ids'
                ).data()
                nodes = sess.run('MATCH (n:Entity) RETURN count(n) AS c').single()['c']
                rels = sess.run('MATCH ()-[r]->() RETURN count(r) AS c').single()['c']
        finally:
            drv.close()
    except Exception as exc:  # noqa: BLE001
        return {'error': f'graph unavailable: {exc}'}

    ids = sorted({i for r in rows for i in (r['ids'] or [])})
    with session_scope() as db:
        exist = {str(x) for x in db.execute(
            select(Chunk.id).where(Chunk.id.in_(ids))
        ).scalars().all()} if ids else set()
    valid_ents = sum(1 for r in rows if any(i in exist for i in (r['ids'] or [])))
    return {
        'graph_nodes': nodes,
        'graph_relations': rels,
        'graph_unique_source_ids': len(ids),
        'source_ids_in_active_corpus': len(exist),
        'unresolved_legacy_source_ids': len(ids) - len(exist),
        'nodes_with_active_source': valid_ents,
        'review_status': 'machine_extracted_pending_independent_review',
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--base-url', default='http://127.0.0.1:8000')
    parser.add_argument('--label',choices=[label for label,_ in CONFIGS],help='Run or replace one configuration in a compatible existing report')
    args = parser.parse_args()
    path = PROJECT_ROOT / 'data/eval/review_set_20261104.json'
    data = json.loads(path.read_text(encoding='utf-8'))
    with session_scope() as db:
        current = corpus_snapshot(db)
        # 检索链路以配置表为准，报告需同时记录生效值，
        # 否则只写 settings 会与真实运行参数不符（曾因此误判调参效果）。
        from app.repositories import config_repo

        effective_cfg = config_repo.effective(db)
    if current != data['corpus']:
        raise ValueError('Corpus changed: freeze and review a new dataset before evaluation')
    report = {'dataset': path.name, 'dataset_version': data['version'], 'corpus': current,
        'retrieval_settings_from_environment': settings.retrieval.model_dump(),
        'retrieval_settings_effective': {k: v for k, v in effective_cfg.items()
                                          if k.startswith(('retrieval.', 'llm.', 'qa.'))},
        'models': {'embedding':Path(settings.model.embedding_model_path).name,
            'reranker':Path(settings.model.reranker_model_path).name, 'embedding_dim':settings.model.embedding_dim},
        'started_at': datetime.now(timezone.utc).isoformat(), 'warmup': 'First request per config recorded separately',
        'limitations': data['limitations'], 'results': []}
    out = PROJECT_ROOT / 'data/eval/review_ablation_20261105.json'
    if args.label and out.exists():
        report=json.loads(out.read_text(encoding='utf-8'))
        if report['corpus']!=current or report['dataset_version']!=data['version']:
            raise ValueError('Cannot extend report from a different dataset/corpus')
    # 实时核对，避免把旧审计结果抄进新报告
    report['graph_source_audit'] = live_graph_audit()
    for label, switches in CONFIGS:
        if args.label and label!=args.label:
            continue
        config = dict(top_k=10, recall_limit=50, rrf_limit=80, max_per_document=0, use_evidence_boost=False, **switches)
        records = []
        for item in data['items']:
            req = Request(args.base_url+'/api/search', data=json.dumps({'query':item['question'], **config}).encode(), headers={'Content-Type':'application/json'})
            started = time.perf_counter()
            with urlopen(req, timeout=240) as resp:
                result = json.load(resp)
            latency = round((time.perf_counter()-started)*1000, 1)
            ids = [r['chunk_id'] for r in result['results']]
            rank = ids.index(item['gold_chunk_id'])+1 if item['gold_chunk_id'] in ids else None
            records.append({'qid':item['qid'], 'rank':rank, 'elapsed_ms':latency,
                'chunk_ids':ids, 'recall_paths': result.get('recall_paths'), 'timings':result.get('timings')})
            print(f'{label} {item["qid"]}: rank={rank} {latency}ms', flush=True)
        ranks = [r['rank'] for r in records]
        entry = {'label':label, 'config':config, 'total':len(records),
            'recall_at_5':sum(r is not None and r<=5 for r in ranks)/len(ranks),
            'recall_at_10':sum(r is not None and r<=10 for r in ranks)/len(ranks),
            'mrr_at_10':sum(1/r if r else 0 for r in ranks)/len(ranks),
            'first_request_ms':records[0]['elapsed_ms'],
            'remaining_mean_ms':sum(r['elapsed_ms'] for r in records[1:])/(len(records)-1), 'records':records}
        report['results']=[result for result in report['results'] if result['label']!=label]
        report['results'].append(entry)
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    with session_scope() as db:
        report['corpus_unchanged_at_finish'] = corpus_snapshot(db) == current
    report['finished_at'] = datetime.now(timezone.utc).isoformat()
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'Report: {out}')


if __name__ == '__main__':
    main()
