"""PostgresDocStore -- real Postgres, no mocking (matches this repo's test discipline).
Sync code under test (it runs off the event loop in production, via asyncio.to_thread), so
these tests are plain sync functions, not async -- pytest-asyncio's asyncio_mode=auto only
affects `async def` tests.
"""

import threading
import time
import uuid

import pytest

from app.rag_core.postgres_store import PostgresDocStore, _strip_nul


@pytest.fixture
def store():
    s = PostgresDocStore()
    created: list[str] = []
    original_save = s.save_document

    def tracking_save(doc_id, meta, tree, pages):
        created.append(doc_id)
        return original_save(doc_id, meta, tree, pages)

    s.save_document = tracking_save
    yield s
    for doc_id in created:
        s.delete_document(doc_id)


def _doc_id() -> str:
    return f"doc-test-{uuid.uuid4().hex}"


def test_save_and_get_round_trip(store):
    doc_id = _doc_id()
    meta = {"id": doc_id, "name": "Sample.pdf", "pageNum": 2}
    tree = [{"title": "Section 1", "node_id": "0000", "start_index": 1, "end_index": 2, "summary": "s"}]
    pages = [{"page_number": 1, "markdown": "page one"}, {"page_number": 2, "markdown": "page two"}]

    store.save_document(doc_id, meta, tree, pages)

    assert store.get_meta(doc_id) == meta
    assert store.get_tree(doc_id) == tree
    assert store.get_pages(doc_id) == pages


def test_get_missing_document_returns_none(store):
    assert store.get_meta(_doc_id()) is None
    assert store.get_tree(_doc_id()) is None
    assert store.get_pages(_doc_id()) is None


def test_save_is_an_upsert(store):
    doc_id = _doc_id()
    store.save_document(doc_id, {"name": "v1"}, [], [])
    store.save_document(doc_id, {"name": "v2"}, [{"title": "new"}], [{"page_number": 1, "markdown": "x"}])
    assert store.get_meta(doc_id) == {"name": "v2"}
    assert store.get_tree(doc_id) == [{"title": "new"}]


def test_list_metas_includes_saved_documents(store):
    doc_id = _doc_id()
    store.save_document(doc_id, {"name": "listed-doc", "marker": doc_id}, [], [])
    names = [m.get("marker") for m in store.list_metas()]
    assert doc_id in names


def test_delete_returns_whether_a_row_existed(store):
    doc_id = _doc_id()
    store.save_document(doc_id, {"name": "x"}, [], [])
    assert store.delete_document(doc_id) is True
    assert store.get_meta(doc_id) is None
    assert store.delete_document(doc_id) is False  # already gone


def test_nul_bytes_in_extracted_text_are_stripped_not_rejected(store):
    """Live-verified failure this test guards against: a real invoice PDF extracted a stray
    \\u0000 in place of a hyphen, and Postgres's jsonb type raised UntranslatableCharacter on
    insert before _strip_nul() existed."""
    doc_id = _doc_id()
    pages = [{"page_number": 1, "markdown": "KPFUIKY8\x000005"}]

    store.save_document(doc_id, {"name": "x\x00y"}, [], pages)

    assert store.get_meta(doc_id) == {"name": "xy"}
    assert store.get_pages(doc_id) == [{"page_number": 1, "markdown": "KPFUIKY80005"}]


def test_strip_nul_recurses_through_nested_structures():
    assert _strip_nul({"a": ["b\x00c", {"d": "e\x00"}], "n": 3}) == {"a": ["bc", {"d": "e"}], "n": 3}


def test_advisory_lock_serializes_concurrent_callers(store):
    """The lock DocStore.lock() replaced was a single global mutex (confirmed: one call site,
    local_api.py's submit_document) -- prove two callers can never be inside it at once."""
    order: list[str] = []
    lock_errors: list[Exception] = []

    def worker(name: str, hold_seconds: float):
        try:
            with store.lock():
                order.append(f"{name}-enter")
                time.sleep(hold_seconds)
                order.append(f"{name}-exit")
        except Exception as exc:  # noqa: BLE001
            lock_errors.append(exc)

    t1 = threading.Thread(target=worker, args=("first", 0.2))
    t2 = threading.Thread(target=worker, args=("second", 0.0))
    t1.start()
    time.sleep(0.05)  # ensure t1 acquires first
    t2.start()
    t1.join(timeout=5)
    t2.join(timeout=5)

    assert not lock_errors
    # first must fully enter and exit before second enters -- never interleaved.
    assert order == ["first-enter", "first-exit", "second-enter", "second-exit"]


def test_search_metas_ranks_name_match_above_description_only_match(store):
    name_hit = _doc_id()
    description_hit = _doc_id()
    store.save_document(name_hit, {"name": "Quarterly Budget Report.pdf",
                                    "description": "Financial summary"}, [], [])
    store.save_document(description_hit, {"name": "Other.pdf",
                                          "description": "Mentions budget planning"}, [], [])

    results, total = store.search_metas("budget", limit=10, offset=0)

    names = [m["name"] for m in results]
    assert names.index("Quarterly Budget Report.pdf") < names.index("Other.pdf")
    assert total == 2


def test_search_metas_finds_top_level_tree_titles(store):
    doc_id = _doc_id()
    store.save_document(doc_id, {"name": "Report.pdf", "description": "no keyword here"},
                        [{"title": "Environmental Impact Assessment", "node_id": "0000",
                          "start_index": 1, "end_index": 5}], [])

    results, total = store.search_metas("environmental", limit=10, offset=0)

    assert total == 1
    assert results[0]["name"] == "Report.pdf"


def test_search_metas_respects_doc_ids_scope(store):
    in_scope = _doc_id()
    out_of_scope = _doc_id()
    store.save_document(in_scope, {"name": "Scoped Invoice.pdf"}, [], [])
    store.save_document(out_of_scope, {"name": "Unscoped Invoice.pdf"}, [], [])

    results, total = store.search_metas("invoice", limit=10, offset=0, doc_ids=[in_scope])

    assert total == 1
    assert results[0]["name"] == "Scoped Invoice.pdf"


def test_search_metas_no_match_returns_empty(store):
    doc_id = _doc_id()
    store.save_document(doc_id, {"name": "Unrelated.pdf"}, [], [])

    results, total = store.search_metas("nonexistentkeywordxyz", limit=10, offset=0)

    assert results == []
    assert total == 0


def test_unique_doc_name_against_a_real_postgres_store(store):
    """local_api.py's _unique_doc_name() only calls list_metas() -- exercise it against the
    real store instead of the lightweight FakeStore test_hardening.py uses, so the swap from
    DocStore to PostgresDocStore is covered end to end, not just in isolation."""
    from app.rag_core.local_api import LocalAPI

    api = object.__new__(LocalAPI)
    api._store = store

    taken_id = _doc_id()
    store.save_document(taken_id, {"name": "Invoice.pdf"}, [], [])

    assert api._unique_doc_name("Report.pdf") == "Report.pdf"  # untaken name, unchanged
    assert api._unique_doc_name("Invoice.pdf") == "Invoice_1.pdf"  # taken, suffixed
