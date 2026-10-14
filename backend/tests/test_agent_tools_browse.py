"""_browse_documents' new relevance-search path -- real Postgres, no mocking (matches this
repo's test discipline; see test_postgres_store.py). Exercises the tool function directly
against a LocalAPI backed by a real PostgresDocStore, the same pattern
test_postgres_store.py's test_unique_doc_name_against_a_real_postgres_store uses.
"""

import uuid

import pytest

from app.rag_core.agent_tools import _browse_documents
from app.rag_core.local_api import LocalAPI
from app.rag_core.postgres_store import PostgresDocStore


@pytest.fixture
def client():
    store = PostgresDocStore()
    api = object.__new__(LocalAPI)
    api._store = store
    created: list[str] = []
    original_save = store.save_document

    def tracking_save(doc_id, meta, tree, pages):
        created.append(doc_id)
        return original_save(doc_id, meta, tree, pages)

    store.save_document = tracking_save
    yield api
    for doc_id in created:
        store.delete_document(doc_id)


def _doc_id() -> str:
    return f"doc-test-{uuid.uuid4().hex}"


def test_query_returns_ranked_results_instead_of_the_old_rejection(client):
    doc_id = _doc_id()
    client._store.save_document(doc_id, {"name": "Budget Forecast 2026.pdf",
                                         "description": "Annual budget planning"}, [], [])

    data, is_error = _browse_documents(client, sort="relevance", query="budget")

    assert is_error is False
    assert data["success"] is True
    assert data["sort"] == "relevance"
    assert any(d["name"] == "Budget Forecast 2026.pdf" for d in data["documents"])


def test_query_works_even_without_explicit_relevance_sort(client):
    doc_id = _doc_id()
    client._store.save_document(doc_id, {"name": "Site Safety Plan.pdf"}, [], [])

    data, is_error = _browse_documents(client, query="safety")

    assert is_error is False
    assert any(d["name"] == "Site Safety Plan.pdf" for d in data["documents"])


def test_relevance_sort_without_query_fails_cleanly(client):
    data, is_error = _browse_documents(client, sort="relevance")

    assert is_error is True
    assert data["errorCode"] == "INVALID_INPUT"


def test_allowed_ids_scopes_the_search_path(client):
    in_scope = _doc_id()
    out_of_scope = _doc_id()
    client._store.save_document(in_scope, {"name": "Scoped Drawing.pdf"}, [], [])
    client._store.save_document(out_of_scope, {"name": "Unscoped Drawing.pdf"}, [], [])

    data, is_error = _browse_documents(client, query="drawing",
                                       _allowed_ids=frozenset({in_scope}))

    assert is_error is False
    names = [d["name"] for d in data["documents"]]
    assert names == ["Scoped Drawing.pdf"]


def test_no_match_gives_fallback_guidance_not_empty_library_message(client):
    doc_id = _doc_id()
    client._store.save_document(doc_id, {"name": "Unrelated.pdf"}, [], [])

    data, is_error = _browse_documents(client, query="nonexistentkeywordxyz")

    assert is_error is False
    assert data["documents"] == []
    options = " ".join(data["next_steps"]["options"])
    assert "without `query`" in options          # `sort` is hidden from the model; don't point at it
    assert "search_content()" in options          # in-page facts are found there, not here


def test_plain_time_sort_is_unaffected(client):
    doc_id = _doc_id()
    # A far-future createdAt keeps this first in the real dev library's newest-first
    # ordering -- a doc saved with no createdAt sorts last (empty string), past the
    # default page size, and this test isn't exercising pagination.
    client._store.save_document(doc_id, {"name": "Plain Listing.pdf",
                                         "createdAt": "9999-12-31T23:59:59.000000"}, [], [])

    data, is_error = _browse_documents(client)

    assert is_error is False
    assert data["sort"] == "time"
    assert any(d["name"] == "Plain Listing.pdf" for d in data["documents"])
