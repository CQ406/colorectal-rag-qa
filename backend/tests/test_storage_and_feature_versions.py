from pathlib import Path
import pytest
from sqlalchemy.dialects import postgresql
from app.services.storage import LocalStorage
from app.services.image_features import FEATURE_VERSION
from app.repositories import image_repo


def test_storage_rejects_sibling_prefix_traversal(tmp_path):
    store = LocalStorage(tmp_path/'store')
    with pytest.raises(ValueError):
        store._path('../store-escape/secret')
    assert store._path('nested/image.png').is_relative_to(tmp_path/'store')


def test_similarity_filters_feature_version_and_publication():
    class DB:
        def execute(self, statement):
            sql = str(statement.compile(dialect=postgresql.dialect(),compile_kwargs={'literal_binds':True}))
            assert f"kb_image.feature_model = '{FEATURE_VERSION}'" in sql
            assert 'kb_image.is_public IS true' in sql
            class Rows:
                def all(self): return []
            return Rows()
    assert image_repo.similar_images(DB(),[1.0]+[0.0]*511)==[]
