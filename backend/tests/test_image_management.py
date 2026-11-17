"""Image publication and access regressions; no database, GPU, or cloud calls."""
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.dialects import postgresql

from app.api import deps
from app.api.v1 import images
from app.api.v1.cases import _validate_image_links
from app.core.config import settings
from app.core.permissions import is_public_path
from app.core.security import create_access_token
from app.db.session import get_db
from app.main import app
from app.models.knowledge import ImageAsset
from app.repositories import image_repo
from app.schemas.common import ImageAnalyzeRequest, ImageSimilarRequest


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setattr(settings.auth, "enabled", True)
    user = SimpleNamespace(id=uuid4(), role="physician", username="test")
    db = SimpleNamespace(commit=lambda: None, flush=lambda: None)
    asset = ImageAsset(
        id=uuid4(), filename="draft.png", phase="intraop",
        storage_path="test-only.png", mime_type="image/png",
        is_public=False, title="Draft", tags=[],
    )
    monkeypatch.setattr(image_repo, "get", lambda _db, _id: asset)
    monkeypatch.setattr(images, "write_audit", lambda *args, **kwargs: None)
    previous = app.dependency_overrides.copy()
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[deps.get_current_user] = lambda: user
    app.dependency_overrides[deps.get_optional_user] = lambda: None
    token, _ = create_access_token(str(user.id), user.username, user.role)
    client = TestClient(app)  # Deliberately skip startup and its real database initialization.
    yield SimpleNamespace(
        client=client, user=user, db=db, asset=asset,
        headers={"Authorization": "Bearer " + token},
    )
    client.close()
    app.dependency_overrides.clear()
    app.dependency_overrides.update(previous)


@pytest.mark.parametrize("path", [
    "/api/images/manage", "/api/images/manage/stats",
    "/api/images/manage/00000000-0000-0000-0000-000000000001/raw",
    "/api/cases/manage",
])
def test_draft_management_requires_authentication(env, path):
    response = env.client.get(path)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_auth_failure_has_cors_headers(env):
    origin = settings.app.cors_origin_list[0]
    response = env.client.get("/api/images/manage", headers={"Origin": origin})
    assert response.status_code == 401
    assert response.headers["access-control-allow-origin"] == origin


@pytest.mark.parametrize("suffix", ["", "/raw"])
def test_public_read_cannot_reveal_draft_even_when_logged_in(env, suffix):
    response = env.client.get(f"/api/images/{env.asset.id}{suffix}", headers=env.headers)
    assert response.status_code == 404


def test_maintainer_can_open_draft_detail(env):
    response = env.client.get(f"/api/images/manage/{env.asset.id}", headers=env.headers)
    assert response.status_code == 200
    assert response.json()["is_public"] is False


def test_maintainer_preview_is_private_and_not_cached(env, monkeypatch):
    monkeypatch.setattr(image_repo, "read_bytes", lambda _asset: b"private-image")
    response = env.client.get(f"/api/images/manage/{env.asset.id}/raw", headers=env.headers)
    assert response.status_code == 200
    assert response.content == b"private-image"
    assert response.headers["cache-control"] == "private, no-store"
    assert "authorization" in response.headers["vary"].lower()


def test_management_list_includes_private_only_through_protected_route(env, monkeypatch):
    calls = []
    def listing(_db, **kwargs):
        calls.append(kwargs)
        return [], 0
    monkeypatch.setattr(image_repo, "list_images", listing)
    assert env.client.get("/api/images").status_code == 200
    assert calls[-1].get("include_private", False) is False
    assert env.client.get("/api/images/manage", headers=env.headers).status_code == 200
    assert calls[-1]["include_private"] is True


def test_management_stats_route_does_not_match_uuid_detail(env, monkeypatch):
    calls = []
    def counts(_db, **kwargs):
        calls.append(kwargs)
        return dict(total=0, analyzed=0, by_phase={}, total_bytes=0)
    monkeypatch.setattr(image_repo, "stats", counts)
    assert env.client.get("/api/images/stats").status_code == 200
    assert calls[-1].get("include_private", False) is False
    assert env.client.get("/api/images/manage/stats", headers=env.headers).status_code == 200
    assert calls[-1]["include_private"] is True


def test_physician_cannot_publish_or_modify_published_image(env):
    url = f"/api/images/{env.asset.id}"
    assert env.client.patch(url, json={"is_public": True}, headers=env.headers).status_code == 403
    env.asset.is_public = True
    assert env.client.patch(url, json={"is_public": False}, headers=env.headers).status_code == 403
    assert env.client.patch(url, json={"title": "edited"}, headers=env.headers).status_code == 403


def test_admin_can_publish_then_unpublish(env):
    env.user.role = "admin"
    url = f"/api/images/{env.asset.id}"
    response = env.client.patch(url, json={"is_public": True}, headers=env.headers)
    assert response.status_code == 200
    assert response.json()["is_public"] is True
    response = env.client.patch(url, json={"is_public": False}, headers=env.headers)
    assert response.status_code == 200
    assert response.json()["is_public"] is False


def test_similarity_rejects_private_query_for_visitors(env):
    response = env.client.post("/api/images/similar", json={"image_id": str(env.asset.id)})
    assert response.status_code == 404


def test_maintainer_can_query_with_draft_without_exposing_private_results(env, monkeypatch):
    app.dependency_overrides[deps.get_optional_user] = lambda: env.user
    monkeypatch.setattr(image_repo, "ensure_feature", lambda *args: [1.0] + [0.0] * 511)
    monkeypatch.setattr(image_repo, "similar_images", lambda *args, **kwargs: [])
    response = env.client.post("/api/images/similar", json={"image_id": str(env.asset.id)}, headers=env.headers)
    assert response.status_code == 200
    assert response.json()["items"] == []


@pytest.mark.parametrize("path", ["/api/images/not-a-uuid", "/api/images/not-a-uuid/raw", "/api/cases/not-a-uuid"])
def test_invalid_resource_ids_return_validation_error(env, path):
    assert env.client.get(path).status_code == 422


@pytest.mark.parametrize("schema", [ImageAnalyzeRequest, ImageSimilarRequest])
def test_invalid_image_query_id_is_validated(schema):
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        schema(image_id="not-a-uuid")


def test_upload_enforces_size_limit_before_repository_call(env, monkeypatch):
    monkeypatch.setattr(images.vision_client, "MAX_IMAGE_BYTES", 8)
    response = env.client.post(
        "/api/images", files={"file": ("large.png", b"x" * 9, "image/png")}, headers=env.headers,
    )
    assert response.status_code == 413


def test_similarity_enforces_base64_size_limit(env, monkeypatch):
    monkeypatch.setattr(images.vision_client, "MAX_IMAGE_BYTES", 8)
    response = env.client.post("/api/images/similar", json={"image_base64": "A" * 20})
    assert response.status_code == 413


@pytest.mark.parametrize("method,path,expected", [
    ("GET", "/api/images", True),
    ("GET", "/api/images/manage/stats", False),
    ("GET", "/api/cases/manage", False),
    ("POST", "/api/images/similar", True),
    ("POST", "/api/cases/search", True),
    ("POST", "/api/images", False),
    ("POST", "/api/images/analyze", False),
    # 尾斜杠必须与无斜杠等价：此前 `POST /api/cases/` 会被判为公开，
    # 只靠 Starlette 的 307 重定向兜住。
    ("POST", "/api/cases/", False),
    ("POST", "/api/images/", False),
    # 未来新增的写操作子路径默认仍需认证（旧规则会整段豁免）
    ("POST", "/api/cases/abc/publish", False),
    ("POST", "/api/images/abc/reanalyze", False),
])
def test_public_path_policy(method, path, expected):
    assert is_public_path(method, path) is expected


@pytest.mark.parametrize("include_private", [False, True])
def test_repository_stats_visibility_is_applied_to_all_aggregates(include_private):
    statements = []
    class Result:
        def scalar_one(self): return 0
        def all(self): return []
    class Db:
        def execute(self, stmt):
            statements.append(str(stmt.compile(dialect=postgresql.dialect())))
            return Result()
    image_repo.stats(Db(), include_private=include_private)
    assert len(statements) == 4
    assert all(("kb_image.is_public IS true" in sql) is not include_private for sql in statements)


def test_loaded_pgvector_feature_does_not_use_array_truth_value():
    np = pytest.importorskip("numpy")
    from app.services.image_features import FEATURE_VERSION
    asset = SimpleNamespace(feature_vector=np.array([1.0] + [0.0] * 511), feature_model=FEATURE_VERSION)
    vector = image_repo.ensure_feature(None, asset)
    assert len(vector) == 512
    assert vector[0] == 1.0


@pytest.mark.parametrize("require_public,is_public,valid", [
    (False, True, True), (False, False, True),
    (True, True, True), (True, False, False),
])
def test_case_image_links_follow_publish_visibility(require_public, is_public, valid):
    from fastapi import HTTPException
    image_id = uuid4()
    class Result:
        def scalars(self): return self
        def all(self): return [image_id] if (not require_public or is_public) else []
    class Db:
        def execute(self, _statement): return Result()
    if valid:
        _validate_image_links(Db(), [str(image_id)], require_public=require_public)
    else:
        with pytest.raises(HTTPException) as error:
            _validate_image_links(Db(), [str(image_id)], require_public=require_public)
        assert error.value.status_code == 422


def test_case_image_links_reject_unknown_and_deduplicate_ids():
    from fastapi import HTTPException
    existing_id, missing_id = uuid4(), uuid4()
    class Result:
        def scalars(self): return self
        def all(self): return [existing_id]
    class Db:
        def execute(self, _statement): return Result()
    _validate_image_links(Db(), [str(existing_id), str(existing_id)], require_public=False)
    with pytest.raises(HTTPException) as error:
        _validate_image_links(Db(), [str(existing_id), str(missing_id)], require_public=False)
    assert error.value.status_code == 422
