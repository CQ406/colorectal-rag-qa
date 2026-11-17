"""Exercise the real LCEL graph with local retrieval/model fixtures only."""
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from langchain_core.runnables import RunnableSequence
from langsmith import get_tracing_context

from app.schemas.common import QaRequest
from app.services import rag_chain, rag_service
from app.services.llm_client import ChatResult, ChatUsage, LLMError


@pytest.fixture
def adapters(monkeypatch):
    calls = []
    clients = []

    def search(db, req):
        calls.append(('retrieve', req.query, req.phase))
        return SimpleNamespace(elapsed_ms=12.5, results=[SimpleNamespace(
            chunk_id='fixture-chunk', document_id='fixture-doc',
            content='Synthetic fixture {literal}: 3%.', score=0.9, rerank_score=0.9,
            doc_title='Synthetic source', doc_journal='Fixture', doc_year=2025,
            page_start=3, page_end=5, section='Fixture section', evidence_level='guideline', phase='preop',
        )])

    class Client:
        model = 'local-fixture'
        closed = False

        def __init__(self):
            clients.append(self)

        def chat(self, messages, temperature=None):
            calls.append(('generate', messages, temperature))
            assert get_tracing_context()['enabled'] is False
            return ChatResult(content='Synthetic 3%[1], unsupported 99%[9].',
                              usage=ChatUsage(10, 5, 15), model=self.model)

        def chat_stream(self, messages, temperature=None):
            calls.append(('generate', messages, temperature))
            assert get_tracing_context()['enabled'] is False
            yield 'Synthetic 3%[1], '
            assert get_tracing_context()['enabled'] is False
            yield 'unsupported 99%[9].'

        def close(self):
            self.closed = True

    monkeypatch.setattr(rag_chain.hybrid_search, 'search', search)
    monkeypatch.setattr(rag_service, 'LLMClient', Client)
    monkeypatch.setattr(rag_chain.search_repo, 'count_active_documents', lambda _: 109)
    monkeypatch.setattr(rag_chain.search_repo, 'count_active_chunks', lambda _: 3772)
    return SimpleNamespace(calls=calls, clients=clients, Client=Client)


def test_sync_graph_preserves_prompt_citations_numeric_validation_usage(adapters):
    assert isinstance(rag_service.build_qa_chain().bound, RunnableSequence)
    result = rag_service.answer(None, QaRequest(question='Synthetic perioperative question', phase='preop'))
    assert [x[0] for x in adapters.calls] == ['retrieve', 'generate']
    messages = adapters.calls[1][1]
    assert [m['role'] for m in messages] == ['system', 'user']
    assert '{literal}' in messages[1]['content']
    assert result.citations[0].chunk_id == 'fixture-chunk'
    assert (result.citations[0].page_start, result.citations[0].page_end) == (3, 5)
    assert result.invalid_citations == [9]
    assert '99%' in result.unsupported_numbers and result.numeric_warning
    assert result.usage['total_tokens'] == 15
    assert result.model == 'local-fixture'
    assert result.timings['retrieval_ms'] == 12.5
    assert adapters.clients[0].closed


def test_stream_is_incremental_and_matches_sync_validation(adapters):
    events = rag_service.answer_stream(None, QaRequest(question='Synthetic question'))
    first = next(events)
    assert first['type'] == 'retrieval'
    assert first['data']['evidences'][0]['page_end'] == 5
    assert not adapters.clients  # evidence arrives before a model connection opens
    assert next(events)['data']['text'] == 'Synthetic 3%[1], '
    assert not adapters.clients[0].closed
    tail = list(events)
    assert [e['type'] for e in tail] == ['delta', 'done']
    done = tail[-1]['data']
    assert done['answer'] == 'Synthetic 3%[1], unsupported 99%[9].'
    assert done['invalid_citations'] == [9]
    assert done['unsupported_numbers'] == ['99%']
    assert done['citations'][0]['page_end'] == 5
    assert done['model'] == 'local-fixture'
    assert adapters.clients[0].closed


@pytest.mark.parametrize('stream', [False, True])
@pytest.mark.parametrize('score, reason', [(None, 'no_evidence'), (0.001, 'low_evidence')])
def test_evidence_gate_never_opens_model(adapters, monkeypatch, stream, score, reason):
    results = [] if score is None else [SimpleNamespace(score=score, rerank_score=score)]
    monkeypatch.setattr(rag_chain.hybrid_search, 'search', lambda *_: SimpleNamespace(elapsed_ms=1, results=results))
    req = QaRequest(question='Synthetic question', min_evidence_score=0.05)
    if stream:
        events = list(rag_service.answer_stream(None, req))
        assert len(events) == 1 and events[0]['type'] == 'error'
        assert events[0]['data']['reason'] == reason
    else:
        result = rag_service.answer(None, req)
        assert not result.has_evidence and not result.citations
    assert not adapters.clients


@pytest.mark.parametrize('stream', [False, True])
def test_identity_short_circuits_retrieval_and_model(adapters, stream):
    req = QaRequest(question='你是谁')
    if stream:
        events = list(rag_service.answer_stream(None, req))
        assert [e['type'] for e in events] == ['retrieval', 'delta', 'done']
        assert '109' in events[-1]['data']['answer']
    else:
        assert '109' in rag_service.answer(None, req).answer
    assert not adapters.calls and not adapters.clients


def test_model_error_closes_client_and_preserves_sse_error(adapters, monkeypatch):
    def fail(*_args, **_kwargs):
        raise LLMError('local synthetic error')
        yield  # make this a generator so the error occurs while streaming
    monkeypatch.setattr(adapters.Client, 'chat_stream', fail)
    events = list(rag_service.answer_stream(None, QaRequest(question='Synthetic question')))
    assert [e['type'] for e in events] == ['retrieval', 'error']
    assert events[-1]['data']['reason'] == 'llm_error'
    assert adapters.clients[0].closed


def test_cancel_closes_model_and_does_not_emit_done(adapters):
    events = rag_service.answer_stream(None, QaRequest(question='Synthetic question'))
    next(events)
    next(events)
    events.close()
    assert adapters.clients[0].closed


def test_stream_can_resume_in_distinct_worker_contexts(adapters):
    events = rag_service.answer_stream(None, QaRequest(question='Synthetic question'))
    with ThreadPoolExecutor(1) as a, ThreadPoolExecutor(1) as b:
        assert a.submit(next, events).result()['type'] == 'retrieval'
        assert b.submit(next, events).result()['type'] == 'delta'
        assert a.submit(next, events).result()['type'] == 'delta'
        assert b.submit(next, events).result()['type'] == 'done'
        a.submit(events.close).result()
    assert adapters.clients[0].closed


def test_retrieval_failure_is_not_a_model_error(adapters, monkeypatch):
    def fail(*_):
        raise RuntimeError('local retrieval error')
    monkeypatch.setattr(rag_chain.hybrid_search, 'search', fail)
    events = list(rag_service.answer_stream(None, QaRequest(question='Synthetic question')))
    assert events[0]['type'] == 'error'
    assert '检索失败' in events[0]['data']['message']
    assert not adapters.clients


def test_empty_stream_is_an_error_instead_of_success(adapters, monkeypatch):
    monkeypatch.setattr(adapters.Client, 'chat_stream', lambda *_args, **_kwargs: iter([]))
    events = list(rag_service.answer_stream(None, QaRequest(question='Synthetic question')))
    assert [e['type'] for e in events] == ['retrieval', 'error']
    assert adapters.clients[0].closed


def test_http_stream_uses_lcel_and_preserves_sse_contract(adapters, monkeypatch):
    import json
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api.v1 import qa
    from app.api.deps import get_db, get_optional_user

    class Session:
        def commit(self):
            pass

    app = FastAPI()
    app.include_router(qa.router, prefix='/api')
    app.dependency_overrides[get_db] = lambda: Session()
    app.dependency_overrides[get_optional_user] = lambda: None
    monkeypatch.setattr(qa.qa_logging, 'log_qa', lambda *_args, **_kwargs: None)
    with TestClient(app) as client:
        response = client.post('/api/qa/stream', json={'question': 'Synthetic question'})
    assert response.status_code == 200
    events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: ')]
    assert [e['type'] for e in events] == ['retrieval', 'delta', 'delta', 'done']
    assert events[-1]['data']['citations'][0]['page_end'] == 5


def test_multi_turn_answer_reuses_lcel_and_persists_citations(adapters, monkeypatch):
    from app.services import conversation
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from app.models.base import Base
    from app.models.system import ChatSession
    engine = create_engine('sqlite://')
    Base.metadata.create_all(engine, tables=[ChatSession.__table__])
    store = conversation.ConversationStore(sessionmaker(engine, expire_on_commit=False))
    monkeypatch.setattr(conversation, 'get_store', lambda: store)
    monkeypatch.setattr(conversation, 'rewrite_query', lambda question, history: (question, False, 0))
    events = list(conversation.chat_stream(None, 'Synthetic question', use_intent_routing=False, owner_key='user:fixture'))
    assert events[0]['type'] == 'meta' and events[-1]['type'] == 'done'
    session = events[0]['data']['session_id']
    restored = store.get(session, 'user:fixture')
    assert restored.turns[0].citations[0]['chunk_id'] == 'fixture-chunk'
    assert store.get(session, 'user:other') is None
    assert [x[0] for x in adapters.calls] == ['retrieve', 'generate']
    engine.dispose()
