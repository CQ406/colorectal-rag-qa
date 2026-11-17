from types import SimpleNamespace
from sqlalchemy import Column, MetaData, String, Table
from app.eval.dataset import EvalDataset, EvalItem
from app.eval.runner import evaluate_retrieval
from app.services import hybrid_search
from scripts import sync_schema
from app.repositories import search_repo
from sqlalchemy.dialects import postgresql
from app.services import graph_search


def test_retrieval_ablation_explicitly_disables_graph(monkeypatch):
    requests=[]
    def search(db, req):
        requests.append(req)
        return SimpleNamespace(results=[])
    monkeypatch.setattr(hybrid_search,'search',search)
    dataset=EvalDataset('test','1','fixture',items=[EvalItem('q','query','c','d','title')])
    metrics=evaluate_retrieval(None,dataset,'vector',recall_mode='vector',use_rerank=False)
    assert requests[0].use_graph is False
    assert metrics.config['use_graph'] is False
    metrics=evaluate_retrieval(None,dataset,'graph',use_graph=True)
    assert requests[1].use_graph is True
    assert metrics.config['use_graph'] is True


def test_schema_preserves_semicolons_and_quotes_in_comments(monkeypatch):
    metadata=MetaData()
    Table('fixture',metadata,Column('version',String(100),comment="Algorithm; don't split: value"))
    monkeypatch.setattr(sync_schema,'Base',SimpleNamespace(metadata=metadata))
    inspector=SimpleNamespace(get_table_names=lambda:['fixture'],get_columns=lambda _:[])
    monkeypatch.setattr(sync_schema,'inspect',lambda _:inspector)
    statements=sync_schema.find_missing_columns()['fixture']
    assert len(statements)==2
    assert statements[1]=="COMMENT ON COLUMN fixture.version IS 'Algorithm; don''t split: value'"


def test_keyword_candidates_rank_before_limit_and_respect_doc_type():
    class Empty:
        def unique(self): return self
        def scalars(self): return self
        def all(self): return []
    class DB:
        def __init__(self): self.calls=[]
        def execute(self,statement):
            self.calls.append(str(statement.compile(dialect=postgresql.dialect())))
            return Empty()
    db=DB()
    search_repo.search_by_keyword(db,'吻合口漏',doc_type='guideline')
    assert 'count(DISTINCT term)' in db.calls[0]
    assert 'ORDER BY' in db.calls[0] and 'kb_chunk.id' in db.calls[0]
    assert all('kb_chunk.doc_type =' in sql for sql in db.calls)


def test_graph_cache_fingerprint_tracks_all_sources_and_types():
    names=[f'entity-{n}' for n in range(60)]
    fingerprint=graph_search.EntityIndex._fingerprint(names,['disease']*60,[['old']]*60)
    changed=names.copy(); changed[-1]='changed'
    assert fingerprint!=graph_search.EntityIndex._fingerprint(changed,['disease']*60,[['old']]*60)
    assert fingerprint!=graph_search.EntityIndex._fingerprint(names,['disease']*60,[['new']]*60)


def test_graph_with_only_deleted_sources_does_not_load_encoder(monkeypatch,tmp_path):
    class Graph:
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def is_available(self): return True
        def run(self,*args,**kwargs): return [{'name':'entity','type':'disease','chunk_ids':['deleted']}]
    monkeypatch.setattr(graph_search,'GraphClient',Graph)
    index=graph_search.EntityIndex()
    index.CACHE_PATH=str(tmp_path/'unused.json')
    assert index.ensure_built({'current'}) is False
    assert '上架来源' in index.build_error
    assert not (tmp_path/'unused.json').exists()


def test_graph_expansion_requires_current_evidence_on_every_edge_before_limit(monkeypatch):
    calls = []
    class Graph:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def is_available(self): return True
        def run(self, query, **params):
            calls.append((query, params))
            return [dict(name='target', type='disease', chunk_ids=['active', 'deleted'], hops=1, rel='TREATS')]
    monkeypatch.setattr(graph_search, 'GraphClient', Graph)
    seeds = [graph_search.GraphHit('seed', 'drug', 0.8, 0, ['active'])]
    hits = graph_search.expand_by_relations(seeds, active_ids={'active'})
    query, params = calls[0]
    assert query.index('any(cid IN b.chunk_ids') < query.index('LIMIT')
    assert query.index('all(r IN relationships(path)') < query.index('LIMIT')
    assert 'coalesce(r.chunk_ids, [])' in query
    assert params['active_ids'] == ['active']
    assert hits[0].chunk_ids == ['active']
    assert graph_search.expand_by_relations(seeds, active_ids=set()) == []
    assert len(calls) == 1


def test_graph_revision_refreshes_running_index_without_corpus_changes(monkeypatch, tmp_path):
    from app.services import graph_revision, search_service
    monkeypatch.setattr(graph_revision, 'REVISION_PATH', tmp_path / 'revision.txt')
    monkeypatch.setattr(search_service, 'embed_texts', lambda names, **kwargs: ([[1.0]] * len(names), 0.0))
    rows = [dict(name='first', type='disease', chunk_ids=['active'])]
    calls = []
    class Graph:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def is_available(self): return True
        def run(self, query, **params):
            calls.append(query)
            assert query.index('any(cid IN e.chunk_ids') < query.index('LIMIT')
            assert params['active_ids'] == ['active']
            return rows
    monkeypatch.setattr(graph_search, 'GraphClient', Graph)
    index = graph_search.EntityIndex()
    index.CACHE_PATH = str(tmp_path / 'index.json')
    assert index.ensure_built({'active'}) and index.size == 1
    assert index.ensure_built({'active'}) and len(calls) == 1
    rows.append(dict(name='second', type='disease', chunk_ids=['active']))
    graph_revision.mark_graph_changed()
    assert index.ensure_built({'active'}) and index.size == 2 and len(calls) == 2
    rows.clear()
    graph_revision.mark_graph_changed()
    assert not index.ensure_built({'active'})
    assert not index.ensure_built({'active'}) and index.size == 0
    assert not list(tmp_path.glob('.graph_revision_*.tmp'))
