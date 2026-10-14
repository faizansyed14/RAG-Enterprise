"""Stage-A retrieval: the index narrows, Postgres decides. A wrong index payload must never widen access."""

import types
import uuid

import pytest

from app.scale import index as ix, search
from app.scale.scope import resolve_scope, retrieval_text
from app.scale.settings import get_scale_settings
from tests.scale.helpers import doc, ingest

FILES = {
    "finance/budget.txt": "The FY26 budget for Project Falcon is approved at 4.2 million dirhams.",
    "legal/contract.txt": "Termination for convenience requires 30 days written notice. Reference SUB-084.",
    "public/handbook.txt": "Vacation policy: employees accrue two days of leave per month.",
}


def test_candidates_are_authorised_and_exact_ids_are_found(tmp_path, object_store):
    ingest(tmp_path, FILES)
    legal = search.candidate_documents("termination notice SUB-084", {"group:legal"})
    assert [c.name for c in legal][:1] == ["contract.txt"]
    assert all(c.name != "budget.txt" for c in search.candidate_documents("budget Falcon", {"group:legal"}))
    assert search.candidate_documents("budget Falcon", {"group:nobody"}) == []
    assert search.candidate_documents("budget Falcon", set()) == []


def test_a_stale_index_payload_cannot_widen_access(tmp_path, object_store):
    """Simulate the index missing a permission change: the chunks claim '*' but Postgres says finance only."""
    ingest(tmp_path, FILES)
    budget = doc("budget.txt")
    ix.set_document_acl(budget["document_id"], ["*"])                       # index is now wrong (too wide)
    legal = {"*", "group:legal"}                                              # what a signed-in legal user matches
    leaked = search.search_chunks("budget Falcon", principals=legal, limit=5)
    assert any(h.name == "budget.txt" for h in leaked)                       # stage 1 alone would leak ...
    names = [c.name for c in search.candidate_documents("budget Falcon", legal)]
    assert "budget.txt" not in names                                         # ... the Postgres re-check drops it


def test_a_stale_index_payload_that_is_too_narrow_only_hides(tmp_path, object_store):
    ingest(tmp_path, FILES)
    budget = doc("budget.txt")
    ix.set_document_acl(budget["document_id"], ["group:somebody-else"])
    assert "budget.txt" not in [c.name for c in search.candidate_documents("budget Falcon", {"group:finance"})]


def test_an_unfiltered_search_is_refused_unless_explicitly_requested(tmp_path, object_store):
    ingest(tmp_path, FILES)
    with pytest.raises(ValueError):
        search.search_chunks("budget", principals=None)
    assert search.search_chunks("budget Falcon", principals=None, allow_unfiltered=True, limit=3)


def test_page_search_hook_needs_an_allowlist_and_stays_inside_it(tmp_path, object_store):
    ingest(tmp_path, FILES)
    budget, contract = doc("budget.txt"), doc("contract.txt")
    assert search.page_search_hook("budget Falcon", 5, None) == []           # no allowlist -> nothing, never the corpus
    assert search.page_search_hook("budget Falcon", 5, []) == []
    rows = search.page_search_hook("budget Falcon termination", 10, [contract["rag_doc_id"]])
    assert rows and {r["doc_id"] for r in rows} == {contract["rag_doc_id"]}
    assert all(r["text"] for r in rows)                                       # page text is hydrated
    both = search.page_search_hook("budget Falcon termination", 10, [contract["rag_doc_id"], budget["rag_doc_id"]])
    assert {r["doc_id"] for r in both} == {contract["rag_doc_id"], budget["rag_doc_id"]}


def test_merge_union_keeps_keyword_hits_in_place_and_appends_new_ones():
    cfg = get_scale_settings()
    kw = [{"doc_id": "a", "page": 1}, {"doc_id": "b", "page": 2}]
    hy = [{"doc_id": "b", "page": 2}, {"doc_id": "c", "page": 1}, {"doc_id": "d", "page": 5}]
    merged, mode = search.merge_page_results(kw, "keyword", hy, 3, cfg)
    assert [(r["doc_id"], r["page"]) for r in merged] == [("a", 1), ("b", 2), ("c", 1)] and mode == "keyword"
    merged, mode = search.merge_page_results([], "none", hy, 10, cfg)
    assert [r["doc_id"] for r in merged] == ["b", "c", "d"] and mode == "semantic"
    assert search.merge_page_results(kw, "keyword", [], 5, cfg) == (kw, "keyword")   # index down -> unchanged


def test_merge_hybrid_mode_uses_hybrid_order(monkeypatch):
    monkeypatch.setenv("SCALE_MERGE_MODE", "hybrid")
    get_scale_settings.cache_clear()
    kw = [{"doc_id": "a", "page": 1}]
    hy = [{"doc_id": "c", "page": 1}, {"doc_id": "a", "page": 1}]
    merged, mode = search.merge_page_results(kw, "keyword", hy, 5)
    assert [r["doc_id"] for r in merged] == ["c", "a"] and mode == "hybrid"


def test_short_follow_ups_borrow_the_previous_question():
    history = [{"role": "user", "content": "What is the termination notice period in the contract?"},
               {"role": "assistant", "content": "30 days."}]
    assert retrieval_text("and for the lease?", history).startswith("What is the termination notice")
    long_q = "Please summarise every obligation the supplier has under clause twelve of the agreement"
    assert retrieval_text(long_q, history) == long_q
    assert retrieval_text("and for the lease?", None) == "and for the lease?"


def _user(user_id, username, role="user"):
    return types.SimpleNamespace(user_id=user_id, username=username, role=role)


@pytest.fixture
def corpus(tmp_path, object_store):
    ingest(tmp_path, FILES)


async def test_resolve_scope_filters_a_selection_and_retrieves_otherwise(corpus, make_pg_user):
    from sqlalchemy import text

    from app.scale.db import engine

    uid, name = make_pg_user()
    with engine().begin() as conn:
        conn.execute(text("INSERT INTO user_principals (user_id, principal) VALUES (:u, 'group:legal')"), {"u": uid})
    budget, contract = doc("budget.txt"), doc("contract.txt")
    user = _user(uid, name)

    # explicit selection: the forbidden id is silently dropped, never returned
    rows = await resolve_scope("anything", [uuid.UUID(budget["document_id"]), uuid.UUID(contract["document_id"])], user)
    assert [r.filename for r in rows] == ["contract.txt"]
    # no selection: retrieval picks candidates, and only authorised ones
    rows = await resolve_scope("termination notice SUB-084", None, user)
    assert "contract.txt" in [r.filename for r in rows] and "budget.txt" not in [r.filename for r in rows]
    # a selection of nothing readable gives an empty scope (the chat layer answers with a refusal)
    assert await resolve_scope("x", [uuid.UUID(budget["document_id"])], user) == []


async def test_admin_is_filtered_like_everyone_unless_bypass_is_enabled(corpus, monkeypatch, make_pg_user):
    uid, name = make_pg_user()
    admin = _user(uid, name, role="admin")
    selected = [uuid.UUID(doc("budget.txt")["document_id"])]

    assert await resolve_scope("x", selected, admin) == []
    monkeypatch.setenv("SCALE_ADMIN_BYPASS_ACL", "true")
    get_scale_settings.cache_clear()
    assert [r.filename for r in await resolve_scope("x", selected, admin)] == ["budget.txt"]


@pytest.mark.parametrize("filter_max", ["5000", "1"])    # applied inside the index query / applied afterwards
async def test_a_long_selection_is_a_scope_to_retrieve_in_not_a_reading_list(
    corpus, monkeypatch, make_pg_user, filter_max
):
    """The current UI sends every document id it knows when nothing is narrowed. That must not hand the whole
    library to the agent: retrieval runs, restricted to the selection and to what the user may read."""
    from sqlalchemy import text

    from app.scale.db import engine

    monkeypatch.setenv("SCALE_CANDIDATE_DOCS", "1")                 # anything longer than one document is a scope
    monkeypatch.setenv("SCALE_SCOPE_FILTER_MAX", filter_max)
    get_scale_settings.cache_clear()
    uid, name = make_pg_user()
    with engine().begin() as conn:
        conn.execute(text("INSERT INTO user_principals (user_id, principal) VALUES (:u, 'group:legal')"), {"u": uid})
    budget, contract, handbook = doc("budget.txt"), doc("contract.txt"), doc("handbook.txt")
    user = _user(uid, name)
    ids = lambda *docs: [uuid.UUID(d["document_id"]) for d in docs]  # noqa: E731

    # legal may read contract + handbook; budget (finance) is in the list but must never come back
    rows = await resolve_scope("termination notice SUB-084", ids(budget, contract, handbook), user)
    assert [r.filename for r in rows] == ["contract.txt"]            # retrieval ran (2 readable > 1) and picked the best

    # a different question picks a different document from the same list: it is retrieval, not list order
    rows = await resolve_scope("vacation policy leave accrue", ids(budget, contract, handbook), user)
    assert [r.filename for r in rows] == ["handbook.txt"]

    # an ordinary hand-picked single document is returned as chosen, with no retrieval in between
    assert [r.filename for r in await resolve_scope("anything at all", ids(handbook), user)] == ["handbook.txt"]


async def test_the_selection_is_a_boundary(corpus, monkeypatch, make_pg_user):
    from sqlalchemy import text

    from app.scale.db import engine

    monkeypatch.setenv("SCALE_CANDIDATE_DOCS", "1")
    get_scale_settings.cache_clear()
    uid, name = make_pg_user()
    with engine().begin() as conn:
        for g in ("group:legal", "group:finance"):
            conn.execute(text("INSERT INTO user_principals (user_id, principal) VALUES (:u, :g)"), {"u": uid, "g": g})
    budget, contract, handbook = doc("budget.txt"), doc("contract.txt"), doc("handbook.txt")
    inside = [uuid.UUID(budget["document_id"]), uuid.UUID(handbook["document_id"]), uuid.uuid4()]   # contract is NOT in it
    rows = await resolve_scope("termination notice SUB-084", inside, _user(uid, name))
    assert "contract.txt" not in [r.filename for r in rows]          # it matches best and is readable, but was not selected
    assert len(rows) == 1


async def test_a_selection_longer_than_one_index_filter_still_finds_its_documents(tmp_path, object_store, monkeypatch, make_pg_user):
    """The selection is searched in parts. Searching the whole corpus and dropping what is outside the selection afterwards
    would never reach a selected document that ranks below the unselected ones (a false 'nothing found')."""
    files = {f"public/decoy{i}.txt": f"Falcon budget approval memo number {i}. The Falcon budget is approved." for i in range(20)}
    files.update({f"public/filler{i}.txt": f"Cafeteria menu for week {i}: soup and bread." for i in range(8)})
    files["public/target.txt"] = "Notes mention the Falcon programme once, in passing."
    ingest(tmp_path, files, rules=[{"prefix": "public", "principals": ["*"]}])
    monkeypatch.setenv("SCALE_CANDIDATE_DOCS", "3")
    monkeypatch.setenv("SCALE_CANDIDATE_CHUNKS", "6")
    monkeypatch.setenv("SCALE_SCOPE_FILTER_MAX", "4")
    monkeypatch.setenv("SCALE_SCOPE_PARTITION_MAX", "10")
    get_scale_settings.cache_clear()
    uid, name = make_pg_user()
    selected = [uuid.UUID(doc(f"filler{i}.txt")["document_id"]) for i in range(8)] + [uuid.UUID(doc("target.txt")["document_id"])]
    assert len(selected) > 4 > 3
    rows = await resolve_scope("Falcon budget approval", selected, _user(uid, name))
    names = [r.filename for r in rows]
    assert "target.txt" in names
    assert not any(n.startswith("decoy") for n in names)           # nothing outside the selection comes back


async def test_a_selection_beyond_the_partition_limit_is_still_confined_to_the_selection(tmp_path, object_store, monkeypatch, make_pg_user):
    files = {f"public/d{i}.txt": f"Falcon budget memo {i}." for i in range(12)}
    ingest(tmp_path, files, rules=[{"prefix": "public", "principals": ["*"]}])
    monkeypatch.setenv("SCALE_CANDIDATE_DOCS", "2")
    monkeypatch.setenv("SCALE_SCOPE_FILTER_MAX", "3")
    monkeypatch.setenv("SCALE_SCOPE_PARTITION_MAX", "1")           # 6 selected docs need 2 parts: over the limit
    get_scale_settings.cache_clear()
    uid, name = make_pg_user()
    chosen = {f"d{i}.txt" for i in range(6)}
    selected = [uuid.UUID(doc(n)["document_id"]) for n in sorted(chosen)]
    rows = await resolve_scope("Falcon budget memo", selected, _user(uid, name))
    assert {r.filename for r in rows} <= chosen
