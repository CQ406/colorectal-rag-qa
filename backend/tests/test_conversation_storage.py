"""Persistence and isolation at the storage and HTTP boundaries."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from app.models.system import ChatSession
from app.services.conversation import ConversationStore, ConversationTurn, ConversationNotFound
from app.api.v1 import chat as chat_api
from app.db.session import get_db


@pytest.fixture
def factory():
    engine = create_engine('sqlite://', connect_args={'check_same_thread':False}, poolclass=StaticPool)
    ChatSession.__table__.create(engine)
    yield sessionmaker(engine, expire_on_commit=False)
    engine.dispose()


def test_persists_across_store_restarts_and_scopes(factory):
    store = ConversationStore(factory)
    conv = store.get_or_create(None, 'user:a')
    conv.add_turn(ConversationTurn('question', 'answer', citations=[{'index':1}]))
    restarted = ConversationStore(factory)
    restored = restarted.get(conv.session_id, 'user:a')
    assert restored.turns[0].answer == 'answer'
    assert restored.turns[0].citations == [{'index':1}]
    assert restarted.get(conv.session_id, 'user:b') is None
    assert restarted.get(conv.session_id, 'guest:x') is None
    assert restarted.clear(conv.session_id, 'user:b') is False
    with pytest.raises(ConversationNotFound):
        restarted.get_or_create(conv.session_id, 'user:b')
    assert restarted.stats('user:b')['total_turns'] == 0


def test_stale_snapshots_append_without_overwrite_and_delete_is_final(factory):
    store = ConversationStore(factory)
    first = store.get_or_create(None, 'user:a')
    second = store.get(first.session_id, 'user:a')
    first.add_turn(ConversationTurn('first', 'one'))
    second.add_turn(ConversationTurn('second', 'two'))
    assert [t.question for t in store.get(first.session_id, 'user:a').turns] == ['first','second']
    for n in range(12):
        first.add_turn(ConversationTurn(str(n), 'answer'))
    assert len(store.get(first.session_id, 'user:a').turns) == 10
    assert store.clear(first.session_id, 'user:a')
    with pytest.raises(ConversationNotFound):
        second.add_turn(ConversationTurn('late', 'ignored'))
    assert store.get(first.session_id, 'user:a') is None


def test_anonymous_cookie_and_stream_owner_boundary(factory, monkeypatch):
    app = FastAPI()
    app.include_router(chat_api.router, prefix='/api')
    app.dependency_overrides[get_db] = lambda: None
    store = ConversationStore(factory)
    monkeypatch.setattr(chat_api.conversation, 'get_store', lambda:store)
    def stream(db, question, session_id, **kwargs):
        conv = store.get_or_create(session_id, kwargs['owner_key'])
        conv.add_turn(ConversationTurn(question, 'test answer'))
        yield {'type':'done','data':conv.to_dict()}
    monkeypatch.setattr(chat_api.conversation, 'chat_stream', stream)
    with TestClient(app) as owner, TestClient(app) as stranger:
        response = owner.post('/api/chat/stream', json={'question':'hello'})
        assert response.status_code == 200
        assert 'HttpOnly' in response.headers['set-cookie']
        import json
        sid = json.loads(response.text.removeprefix('data: ').strip())['data']['session_id']
        assert owner.get('/api/chat/'+sid).status_code == 200
        assert stranger.get('/api/chat/'+sid).status_code == 404
        assert stranger.post('/api/chat/stream',json={'question':'attack','session_id':sid}).status_code == 404
        assert stranger.delete('/api/chat/'+sid).json()['cleared'] is False
        assert owner.get('/api/chat/stats').json()['total_turns'] == 1
        assert stranger.get('/api/chat/stats').json()['total_turns'] == 0
        assert owner.get('/api/chat/'+sid, headers={'Authorization':'Bearer invalid'}).status_code == 401
