"""The agent addresses documents by name. These paths used to read every document's metadata on every
call; they must now be one indexed query, and must still honour the agent's allowlist."""

import pytest

from app.rag_core import agent_tools
from app.rag_core.local_api import LocalAPI
from app.rag_core.postgres_store import PostgresDocStore
from tests.scale.helpers import doc, ingest

FILES = {
    "finance/budget.txt": "The FY26 budget for Project Falcon is approved at 4.2 million dirhams.",
    "legal/contract.txt": "Termination for convenience requires 30 days written notice.",
}


@pytest.fixture
def api(tmp_path, object_store, monkeypatch):
    ingest(tmp_path, FILES)
    client = object.__new__(LocalAPI)
    client._store = PostgresDocStore()

    def no_sweep(self, *a, **k):
        raise AssertionError("list_metas() loads every document's metadata -- the indexed path must not call it")

    monkeypatch.setattr(PostgresDocStore, "list_metas", no_sweep)
    return client


def test_exact_name_listing_uses_the_index(api):
    page = api.list_documents(name="budget.txt")
    assert [d["name"] for d in page["documents"]] == ["budget.txt"] and page["total"] == 1
    assert api.list_documents(name="nope.txt")["documents"] == []


def test_a_taken_upload_name_is_made_unique_without_a_sweep(api):
    assert api._unique_doc_name("budget.txt") != "budget.txt"
    assert api._unique_doc_name("brand-new.txt") == "brand-new.txt"


def test_resolve_document_finds_an_allowed_document_by_name(api):
    budget = doc("budget.txt")
    entry, error = agent_tools._resolve_document(api, "budget.txt", None, frozenset({budget["rag_doc_id"]}))
    assert error is None and entry["id"] == budget["rag_doc_id"]


def test_resolve_document_honours_the_allowlist(api, monkeypatch):
    contract = doc("contract.txt")
    monkeypatch.undo()                                   # the not-found path may sweep; only the result matters
    api._store = PostgresDocStore()
    entry, error = agent_tools._resolve_document(api, "budget.txt", None, frozenset({contract["rag_doc_id"]}))
    assert entry is None and error is not None            # budget.txt exists, but is outside this caller's scope


def test_names_like_treats_percent_and_underscore_literally(tmp_path, object_store):
    ingest(tmp_path, {"public/100%_done.txt": "A progress note.", "public/100x_done.txt": "Another note.",
                      "public/notes.txt": "Third note."})
    store = PostgresDocStore()
    assert store.names_like("100%") == {"100%_done.txt"}            # '%' is not a wildcard
    assert store.names_like("100_") == set()                         # '_' is not a wildcard either
    assert store.names_like("100") == {"100%_done.txt", "100x_done.txt"}
    assert store.names_like("no-such-prefix") == set()


def test_the_name_index_serves_equality_and_prefix_lookups(tmp_path, object_store):
    from sqlalchemy import text

    from app.scale.db import engine

    ingest(tmp_path, {"public/a.txt": "alpha"})
    with engine().connect() as conn:
        conn.execute(text("SET enable_seqscan = off"))   # a tiny table would seq-scan anyway; ask whether the index CAN serve it
        eq = "\n".join(r[0] for r in conn.execute(text("EXPLAIN SELECT 1 FROM document_trees WHERE meta ->> 'name' = 'a.txt'")))
        like = "\n".join(r[0] for r in conn.execute(text("EXPLAIN SELECT 1 FROM document_trees WHERE meta ->> 'name' LIKE 'a%'")))
    assert "document_trees_name_idx" in eq and "document_trees_name_idx" in like
