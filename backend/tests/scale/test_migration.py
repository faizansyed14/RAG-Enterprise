"""Changing the embedding model must not turn into "nothing found" while the new index is being built."""

import uuid

import pytest

from app.scale import index as ix, migration, models, reconcile, search
from app.scale.settings import get_scale_settings
from tests.scale.helpers import doc, drain, ingest

FILES = {
    "public/budget.txt": "The FY26 budget for Project Falcon is approved at 4.2 million dirhams.",
    "public/contract.txt": "Termination for convenience requires 30 days written notice. Reference SUB-084.",
    "finance/salaries.txt": "Salary bands for the engineering grade are confidential. Reference SAL-777.",
}
RULES = [{"prefix": "public", "principals": ["*"]}, {"prefix": "finance", "principals": ["group:finance"]}]


def _switch_model(monkeypatch, dim: int):
    monkeypatch.setenv("SCALE_EMBED_MODEL", f"hash-{uuid.uuid4().hex[:8]}")
    monkeypatch.setenv("SCALE_EMBED_DIM", str(dim))
    get_scale_settings.cache_clear()
    models.clear_embedder_cache()
    search._embedder.cache_clear()
    migration._CACHE.clear()


def _names(hits):
    return {h.name for h in hits}


@pytest.fixture
def built(tmp_path, object_store, monkeypatch):
    monkeypatch.setenv("SCALE_RECONCILE_GRACE_MINUTES", "0")
    get_scale_settings.cache_clear()
    ingest(tmp_path, FILES, rules=RULES)
    return get_scale_settings()


def test_search_keeps_working_while_the_new_model_is_still_empty(built, monkeypatch):
    old = ix.collection_name(built)
    assert "contract.txt" in _names(search.search_chunks("termination notice SUB-084", principals={"*"}, limit=5))
    _switch_model(monkeypatch, 96)
    # a worker opens the new collection (nothing is rebuilt yet) ...
    ix.ensure_collection(96)
    assert ix.collection_name(get_scale_settings()) != old
    # ... and the old index still answers, with the model that built it
    st = migration.status()
    assert st["legacy"] == [old] and st["migrating"] and st["documents_remaining"] == 3 and st["percent"] == 0
    assert "contract.txt" in _names(search.search_chunks("termination notice SUB-084", principals={"*"}, limit=5))


def test_a_rebuilt_document_is_answered_from_the_new_index_only(built, monkeypatch):
    _switch_model(monkeypatch, 96)
    ix.ensure_collection(96)
    reconcile.run(force=True, budget_s=60)          # sends every document back to be embedded with the new model
    drain()
    st = migration.status()
    assert st["documents_remaining"] == 0 and st["percent"] == 100.0
    new = ix.collection_name(get_scale_settings())
    hits = search.search_chunks("termination notice SUB-084", principals={"*"}, limit=10)
    assert "contract.txt" in _names(hits)
    ids = [h.id for h in hits]
    assert len(ids) == len(set(ids))                # no chunk appears twice (legacy copies of rebuilt documents are dropped)
    assert ix.get_client().collection_exists(new)


def test_a_permission_change_reaches_the_old_index_too(built, monkeypatch):
    _switch_model(monkeypatch, 96)
    ix.ensure_collection(96)
    contract = doc("contract.txt")["document_id"]
    ix.set_document_acl(contract, ["group:legal"])
    assert "contract.txt" not in _names(search.search_chunks("termination notice SUB-084", principals={"*"}, limit=5))
    assert "contract.txt" in _names(search.search_chunks("termination notice SUB-084", principals={"group:legal"}, limit=5))


def test_a_restricted_document_stays_restricted_through_the_switch(built, monkeypatch):
    _switch_model(monkeypatch, 96)
    ix.ensure_collection(96)
    assert "salaries.txt" not in _names(search.search_chunks("salary bands SAL-777", principals={"*"}, limit=5))
    assert "salaries.txt" in _names(search.search_chunks("salary bands SAL-777", principals={"group:finance"}, limit=5))


def test_retire_refuses_until_every_document_is_rebuilt_then_removes_the_old_index(built, monkeypatch):
    old = ix.collection_name(built)
    _switch_model(monkeypatch, 96)
    ix.ensure_collection(96)
    with pytest.raises(RuntimeError, match="not rebuilt"):
        migration.retire()
    assert ix.get_client().collection_exists(old)
    reconcile.run(force=True, budget_s=60)
    drain()
    assert migration.retire() == [old]
    assert not ix.get_client().collection_exists(old)
    assert migration.status()["legacy"] == [] and not migration.status()["migrating"]
    assert "contract.txt" in _names(search.search_chunks("termination notice SUB-084", principals={"*"}, limit=5))


def test_an_old_index_that_cannot_be_read_is_skipped_not_fatal(built, monkeypatch):
    _switch_model(monkeypatch, 96)
    ix.ensure_collection(96)
    real = models.get_embedder

    def flaky(cfg=None):
        if cfg is not None:
            raise RuntimeError("old model files are gone")       # only the old build's embedder is unavailable
        return real()

    monkeypatch.setattr(search, "get_embedder", flaky)
    search._embedder.cache_clear()
    assert search.search_chunks("termination notice SUB-084", principals={"*"}, limit=5) == []   # new index is empty: nothing, no error
