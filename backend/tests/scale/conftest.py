"""Fixtures for the scale layer: real Postgres, embedded Qdrant, an in-memory object store, and the
offline hashing embedder. No network, no model server, no GPU."""

import os
import uuid

import pytest
from sqlalchemy import text


class FakeObjectStore:
    def __init__(self):
        self.objects: dict[str, bytes] = {}

    def put_object(self, key, data, content_type="application/octet-stream"):
        self.objects[key] = data
        return key

    def get_object(self, key):
        return self.objects[key]

    def delete_object(self, key):
        self.objects.pop(key, None)

    def exists(self, key):
        return key in self.objects

    def presigned_url(self, key, expires_in=None):
        return f"http://fake/{key}"

    def ensure_bucket(self):
        pass


@pytest.fixture(autouse=True)
def scale_env(monkeypatch):
    from app.scale import index as ix
    from app.scale import pipeline, search
    from app.scale.settings import get_scale_settings

    # Embedded Qdrant by default; SCALE_TEST_QDRANT_URL=http://localhost:6333 runs the same tests against a real server
    # (payload indexes, server-side IDF and filtered prefetch behave differently there).
    server = os.environ.get("SCALE_TEST_QDRANT_URL")
    if server:
        monkeypatch.setenv("SCALE_QDRANT_LOCATION", "")
        monkeypatch.setenv("QDRANT_URL", server)
    else:
        monkeypatch.setenv("SCALE_QDRANT_LOCATION", ":memory:")
    monkeypatch.setenv("SCALE_EMBED_BACKEND", "hashing")
    monkeypatch.setenv("SCALE_EMBED_DIM", "128")
    monkeypatch.setenv("SCALE_EMBED_MODEL", f"hash-{uuid.uuid4().hex[:8]}")  # fresh collection per test
    monkeypatch.setenv("SCALE_RERANK_URL", "")
    # The shipped defaults use local models (reranker, OCR); tests opt in to those individually.
    monkeypatch.setenv("SCALE_RERANK_MODE", "none")
    monkeypatch.setenv("SCALE_DOCPARSE_MODE", "off")
    get_scale_settings.cache_clear()
    ix.reset_client_cache()
    search.reset_model_caches()
    pipeline.forget_connectors()
    yield
    get_scale_settings.cache_clear()
    ix.reset_client_cache()
    search.reset_model_caches()
    pipeline.forget_connectors()


@pytest.fixture
def object_store(monkeypatch):
    store = FakeObjectStore()
    monkeypatch.setattr("app.core.object_store.get_object_store", lambda: store)
    monkeypatch.setattr("app.api.documents.get_object_store", lambda: store)       # imported by name there: patch that reference too
    return store


@pytest.fixture(autouse=True)
def clean_scale_tables():
    from app.scale.db import engine

    def wipe():
        with engine().begin() as conn:
            conn.execute(text("DELETE FROM scale_jobs"))
            conn.execute(text("DELETE FROM source_connections"))  # cascades items + state
            conn.execute(text("DELETE FROM user_principals"))
            conn.execute(text(
                "DELETE FROM document_trees WHERE doc_id IN (SELECT rag_doc_id FROM documents WHERE tier = 'lite')"
            ))
            conn.execute(text("DELETE FROM documents WHERE tier = 'lite'"))

    wipe()
    yield
    wipe()


@pytest.fixture
def make_pg_user():
    """A real users row (needed for user_principals' foreign key). Cleaned up with the table wipes."""
    from app.core.security import hash_password
    from app.scale.db import engine

    created = []

    def _make(username=None):
        uid = uuid.uuid4()
        name = username or f"u_{uuid.uuid4().hex[:8]}"
        with engine().begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO users (user_id, username, password_hash, role, credit_limit) "
                    "VALUES (:i, :n, :p, 'user', 100)"
                ),
                {"i": uid, "n": name, "p": hash_password("x-pass-123!")},
            )
        created.append(uid)
        return uid, name

    yield _make
    with engine().begin() as conn:
        for uid in created:
            conn.execute(text("DELETE FROM users WHERE user_id = :i"), {"i": uid})
