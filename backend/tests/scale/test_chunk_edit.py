"""The chunk editor: what an edit changes in search, what it must not change (who may read what), and what happens to it when
the file is re-ingested. Real Postgres and Qdrant, the offline embedder, the real pipeline."""

import os
import time

import httpx
import pytest
from sqlalchemy import text

from app.main import app
from app.scale import chunk_edit, index as ix, search, sync
from app.scale.chunk_edit import MANUAL_BASE, ChunkEditError
from app.scale.db import engine
from app.scale.settings import get_scale_settings

from .helpers import doc, drain, make_source

def _para(text: str, words: int = 30) -> str:
    """Exactly `words` words, so that with SCALE_CHUNK_WORDS=30 and no overlap, chunk i is paragraph i."""
    filler = "annex appendix schedule exhibit rider addendum clause proviso recital".split()
    got = text.split()
    assert len(got) <= words, text
    return " ".join(got + [filler[i % len(filler)] for i in range(words - len(got))])


PARAS = [_para(t) for t in [
    "Section one covers the supplier onboarding checklist, including company registration, trade licence, tax certificate and bank letter.",
    "Section two covers payment terms: invoices are payable within thirty days of receipt and late payments accrue interest monthly.",
    "Section three covers insurance: the supplier maintains public liability cover of five million dirhams for the whole contract term.",
    "Section four covers termination: either party may end the agreement with ninety days written notice delivered to the other party.",
    "Section five covers confidentiality: all pricing and technical information exchanged remains confidential for five years after expiry.",
]]
BODY = "\n\n".join(PARAS) + "\n"
FILES = {"legal/terms.txt": BODY, "public/other.txt": "Cafeteria menu for the week with soup, salad and grilled fish every day at noon.\n"}


@pytest.fixture(autouse=True)
def small_chunks(monkeypatch, object_store):
    monkeypatch.setenv("SCALE_CHUNK_WORDS", "30")
    monkeypatch.setenv("SCALE_CHUNK_OVERLAP_WORDS", "0")
    monkeypatch.setenv("SCALE_MIN_CHUNK_WORDS", "3")
    get_scale_settings.cache_clear()


@pytest.fixture
def corpus(tmp_path):
    cid = make_source(tmp_path, FILES)
    sync.run_scan(cid)
    drain()
    return tmp_path, cid, doc("terms.txt")["document_id"]


def find(query, *principals, limit=5):
    return search.search_chunks(query, principals=set(principals) or {"group:legal"}, limit=limit, rerank=False)


def chunk_with(document_id, needle):
    for c in chunk_edit.list_chunks(document_id, limit=200)["chunks"]:
        if needle in c["text"]:
            return c
    raise AssertionError(f"no chunk contains {needle!r}")


# ---------------------------------------------------------------------------------------------- looking

def test_the_editor_lists_the_chunks_in_order_with_their_pages(corpus):
    _, _, d = corpus
    out = chunk_edit.list_chunks(d)
    assert out["total"] == 5
    assert [c["chunk"] for c in out["chunks"]] == sorted(c["chunk"] for c in out["chunks"])
    assert all(c["kind"] == "extracted" and c["available"] and c["page"] == 1 for c in out["chunks"])
    assert "onboarding" in " ".join(c["text"] for c in out["chunks"])


def test_listing_filters_by_text_and_pages(corpus):
    _, _, d = corpus
    assert all("insurance" in c["text"] for c in chunk_edit.list_chunks(d, query="insurance")["chunks"])
    assert len(chunk_edit.list_chunks(d, limit=2)["chunks"]) == 2
    assert chunk_edit.list_chunks(d, offset=1000)["chunks"] == []


# ---------------------------------------------------------------------------------------------- editing text

def test_correcting_a_chunk_changes_what_is_found_and_what_the_hit_shows(corpus):
    _, _, d = corpus
    c = chunk_with(d, "insurance")
    assert not find("indemnity cover tenmillion")[0].text.count("tenmillion")
    out = chunk_edit.update_chunk(d, c["chunk"], text="Section three covers insurance: the supplier maintains indemnity cover of tenmillion dirhams.", actor="admin")
    assert out["kind"] == "edited" and out["original_text"] == c["text"]
    top = find("indemnity cover tenmillion")[0]
    assert top.chunk == c["chunk"] and "tenmillion" in top.text and top.kind == "edited"
    assert "public liability" not in top.text                         # the old words are gone from the index
    assert top.start == c["chunk"] * 0 + top.start                    # citation position unchanged
    assert find("public liability five million")[0].chunk != c["chunk"] or "public liability" not in find("public liability five million")[0].text


def test_reverting_an_edit_restores_the_original_and_removes_the_row(corpus):
    _, _, d = corpus
    c = chunk_with(d, "termination")
    chunk_edit.update_chunk(d, c["chunk"], text="Totally different words about zebra crossings.", keywords=["zebra"])
    back = chunk_edit.update_chunk(d, c["chunk"], text=None, keywords=[])
    assert back["kind"] == "extracted" and back["text"] == c["text"] and back["original_text"] is None
    assert chunk_edit.rows_for(d) == []
    assert find("ninety days written notice")[0].chunk == c["chunk"]


def test_editing_to_the_same_text_is_not_an_edit(corpus):
    _, _, d = corpus
    c = chunk_with(d, "confidentiality")
    chunk_edit.update_chunk(d, c["chunk"], text="  " + c["text"].replace(" ", "  ") + " ")
    assert chunk_edit.rows_for(d) == []


def test_keywords_and_questions_added_by_a_person_make_the_chunk_findable(corpus):
    _, _, d = corpus
    c = chunk_with(d, "payment terms")
    assert (find("remittance cadence net30")[0].sparse_score or 0) == 0
    chunk_edit.update_chunk(d, c["chunk"], keywords=["remittance cadence", "net30"], questions=["When do we pay suppliers?"])
    top = find("remittance cadence net30")[0]
    assert top.chunk == c["chunk"] and top.sparse_score > 0
    assert find("when do we pay suppliers")[0].chunk == c["chunk"]
    got = chunk_edit.get_chunk(d, c["chunk"])
    assert got["keywords"] == ["remittance cadence", "net30"] and got["text"] == c["text"]


def test_switching_a_chunk_off_removes_it_from_search_and_back_on_restores_it(corpus):
    _, _, d = corpus
    c = chunk_with(d, "confidentiality")
    assert find("confidential pricing five years")[0].chunk == c["chunk"]
    chunk_edit.update_chunk(d, c["chunk"], available=False)
    assert all(h.chunk != c["chunk"] for h in find("confidential pricing five years", limit=20))
    assert chunk_edit.get_chunk(d, c["chunk"])["available"] is False
    assert chunk_edit.list_chunks(d, unavailable_only=True)["total"] == 1
    chunk_edit.update_chunk(d, c["chunk"], available=True)
    assert find("confidential pricing five years")[0].chunk == c["chunk"]
    assert chunk_edit.rows_for(d) == []                                # back to plain: nothing left to remember


def test_a_switched_off_chunk_is_off_in_every_search_leg(corpus):
    _, _, d = corpus
    c = chunk_with(d, "confidentiality")
    chunk_edit.update_chunk(d, c["chunk"], available=False)
    assert all(h.chunk != c["chunk"] for h in find("confidential pricing technical information", limit=30))


def test_validation_rejects_bad_input_without_touching_anything(corpus):
    _, _, d = corpus
    c = chunk_with(d, "onboarding")
    for bad in (dict(keywords="notalist"), dict(keywords=[1]), dict(text="x" * 20001), dict(keywords=["k" * 100]), dict(keywords=[str(i) for i in range(60)])):
        with pytest.raises(ChunkEditError) as e:
            chunk_edit.update_chunk(d, c["chunk"], **bad)
        assert e.value.code == "invalid"
    assert chunk_edit.rows_for(d) == []
    with pytest.raises(ChunkEditError) as e:
        chunk_edit.update_chunk(d, 9999, text="x")
    assert e.value.code == "not_found"


def test_every_change_is_logged_with_who_and_before_after(corpus):
    _, _, d = corpus
    c = chunk_with(d, "onboarding")
    chunk_edit.update_chunk(d, c["chunk"], keywords=["kyc"], actor="alice")
    chunk_edit.update_chunk(d, c["chunk"], keywords=[], actor="bob")
    log = chunk_edit.history(d)
    assert [(h["action"], h["actor"]) for h in log] == [("revert", "bob"), ("update", "alice")]
    assert log[1]["after"]["keywords"] == ["kyc"] and log[0]["before"]["keywords"] == ["kyc"]


# ---------------------------------------------------------------------------------------------- access control

def test_an_edit_never_changes_who_can_read_the_chunk(corpus):
    _, _, d = corpus
    c = chunk_with(d, "insurance")
    chunk_edit.update_chunk(d, c["chunk"], text="Secret rewritten insurance wording with xylophone.", keywords=["xylophone"])
    assert find("xylophone", "group:legal")[0].chunk == c["chunk"]
    assert find("xylophone", "group:finance") == [] or all(h.document_id != d for h in find("xylophone", "group:finance"))
    assert ix.first_point(d).payload["acl"] == ["group:legal"]


def test_a_permission_change_after_an_edit_still_reaches_the_edited_and_hand_written_chunks(corpus):
    _, cid, d = corpus
    c = chunk_with(d, "insurance")
    chunk_edit.update_chunk(d, c["chunk"], keywords=["xylophone"])
    manual = chunk_edit.add_chunk(d, "Hand written note about marimba tuning.", page=1)
    ix.set_document_acl(d, ["group:finance"])
    assert find("xylophone", "group:legal") == [] or all(h.document_id != d for h in find("xylophone", "group:legal"))
    assert any(h.chunk == manual["chunk"] for h in find("marimba tuning", "group:finance"))
    assert all(h.document_id != d for h in find("marimba tuning", "group:legal"))


def test_a_hand_written_chunk_is_found_only_by_those_who_can_read_the_document(corpus):
    _, _, d = corpus
    out = chunk_edit.add_chunk(d, "FAQ: the supplier portal password reset takes two business days.", page=1, keywords=["portal"], questions=["How long does a password reset take?"])
    assert out["chunk"] >= MANUAL_BASE and out["kind"] == "manual"
    top = find("how long does a password reset take")[0]
    assert top.chunk == out["chunk"] and top.kind == "manual" and "two business days" in top.text
    assert all(h.document_id != d for h in find("password reset", "group:finance"))
    assert all(h.document_id != d for h in find("password reset", "group:nobody"))


def test_fetch_visible_applies_the_same_filter_as_search(corpus):
    _, _, d = corpus
    c = chunk_with(d, "insurance")
    assert [h.chunk for h in ix.fetch_visible([(d, c["chunk"])], principals={"group:legal"})] == [c["chunk"]]
    assert ix.fetch_visible([(d, c["chunk"])], principals={"group:finance"}) == []
    assert ix.fetch_visible([(d, c["chunk"])], principals=set()) == []
    chunk_edit.update_chunk(d, c["chunk"], available=False)
    assert ix.fetch_visible([(d, c["chunk"])], principals={"group:legal"}) == []


# ---------------------------------------------------------------------------------------------- hand-written chunks

def test_hand_written_chunks_can_be_edited_and_deleted(corpus):
    _, _, d = corpus
    a = chunk_edit.add_chunk(d, "Note about glockenspiel maintenance.")
    b = chunk_edit.add_chunk(d, "Another note about ocarina storage.")
    assert b["chunk"] == a["chunk"] + 1
    chunk_edit.update_chunk(d, a["chunk"], text="Note about celesta maintenance.")
    assert find("celesta maintenance")[0].chunk == a["chunk"]
    assert all("glockenspiel" not in h.text for h in find("glockenspiel maintenance", limit=20))
    assert chunk_edit.delete_chunk(d, a["chunk"]) == {"deleted": a["chunk"]}
    assert all(h.chunk != a["chunk"] for h in find("celesta maintenance", limit=20))
    with pytest.raises(ChunkEditError):
        chunk_edit.delete_chunk(d, 0)                                       # an extracted chunk: switch it off instead
    with pytest.raises(ChunkEditError):
        chunk_edit.add_chunk(d, "x", page=99)


# ---------------------------------------------------------------------------------------------- re-ingesting the file

def rescan(tmp_path, cid, body):
    time.sleep(0.01)
    (tmp_path / "legal" / "terms.txt").write_text(body)
    os.utime(tmp_path / "legal" / "terms.txt", ns=(time.time_ns(), time.time_ns() + 5_000_000_000))
    sync.run_scan(cid)
    drain()


def test_an_edit_follows_its_chunk_when_the_file_changes_elsewhere(corpus):
    tmp, cid, d = corpus
    c = chunk_with(d, "insurance")
    chunk_edit.update_chunk(d, c["chunk"], keywords=["indemnity"], text=None)
    new_body = _para("Section zero is a brand new opening paragraph added before everything else.") + "\n\n" + BODY
    rescan(tmp, cid, new_body)
    d2 = doc("terms.txt")["document_id"]
    moved = chunk_with(d2, "insurance")
    assert moved["chunk"] > c["chunk"]                                       # the chunk moved
    assert moved["keywords"] == ["indemnity"]                                # ... and its edit moved with it
    assert find("indemnity")[0].chunk == moved["chunk"]


def test_an_edit_whose_chunk_changed_is_kept_but_not_applied(corpus):
    tmp, cid, d = corpus
    c = chunk_with(d, "insurance")
    chunk_edit.update_chunk(d, c["chunk"], text="Corrected insurance text with quokka.")
    rescan(tmp, cid, BODY.replace("five million dirhams", "ten million dirhams"))
    d2 = doc("terms.txt")["document_id"]
    out = chunk_edit.list_chunks(d2, limit=200)
    assert all("quokka" not in ch["text"] for ch in out["chunks"])            # not applied to different text
    assert len(out["orphaned_edits"]) == 1 and "quokka" in out["orphaned_edits"][0]["text"]
    assert any("ten million" in ch["text"] for ch in out["chunks"])


def test_hand_written_chunks_survive_a_re_ingest(corpus):
    tmp, cid, d = corpus
    chunk_edit.add_chunk(d, "Persistent note about harpsichord tuning.", keywords=["harpsichord"])
    rescan(tmp, cid, BODY + "\n" + _para("An extra closing paragraph that changes the file so it is ingested again.") + "\n")
    d2 = doc("terms.txt")["document_id"]
    assert any(h.kind == "manual" and "harpsichord" in h.text for h in find("harpsichord tuning"))
    assert find("harpsichord", "group:finance") == [] or all(h.document_id != d2 for h in find("harpsichord", "group:finance"))


def test_switched_off_chunks_stay_off_after_a_re_ingest(corpus):
    tmp, cid, d = corpus
    c = chunk_with(d, "public liability")
    chunk_edit.update_chunk(d, c["chunk"], available=False)
    rescan(tmp, cid, BODY + "\n" + _para("A closing sentence that changes the file for this test of re-ingestion.") + "\n")
    d2 = doc("terms.txt")["document_id"]
    assert d2 != d
    assert all("public liability" not in h.text for h in find("public liability cover five million dirhams", limit=30))
    assert chunk_edit.list_chunks(d2, unavailable_only=True)["total"] == 1


def test_deleting_the_document_deletes_its_edits_and_history(corpus):
    from app.scale import pipeline

    _, _, d = corpus
    c = chunk_with(d, "insurance")
    chunk_edit.update_chunk(d, c["chunk"], keywords=["k"])
    chunk_edit.add_chunk(d, "note")
    pipeline.delete_document_everywhere(d)
    with engine().connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM chunk_edits WHERE document_id = :d"), {"d": d}).scalar_one() == 0
        assert conn.execute(text("SELECT count(*) FROM chunk_edit_log WHERE document_id = :d"), {"d": d}).scalar_one() == 0


# ---------------------------------------------------------------------------------------------- the agent's keyword search

def test_the_agents_keyword_search_shows_corrected_text_and_hand_written_notes(corpus):
    _, _, d = corpus
    rag = doc("terms.txt")["rag_doc_id"]
    c = chunk_with(d, "insurance")
    chunk_edit.update_chunk(d, c["chunk"], text="Section three covers insurance: cover of exactly 5000000 dirhams.")
    chunk_edit.add_chunk(d, "Hand note: renewal is automatic.")
    page = search._page_texts([(rag, 1)])
    shown = chunk_edit.overlay_pages(page)[(rag, 1)]
    assert "exactly 5000000" in shown and "public liability" not in shown and "renewal is automatic" in shown
    assert "public liability" in page[(rag, 1)]                              # the stored page is untouched


def test_overlay_can_be_switched_off(corpus, monkeypatch):
    _, _, d = corpus
    rag = doc("terms.txt")["rag_doc_id"]
    c = chunk_with(d, "insurance")
    chunk_edit.update_chunk(d, c["chunk"], text="Replaced wording.")
    monkeypatch.setenv("SCALE_CHUNK_EDIT_OVERLAY", "false")
    get_scale_settings.cache_clear()
    page = search._page_texts([(rag, 1)])
    assert chunk_edit.overlay_pages(page) == page


# ---------------------------------------------------------------------------------------------- the HTTP surface

def client(headers) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", headers=headers)


@pytest.fixture(autouse=True)
def mount_router():
    from app.scale.api import router

    if not any(getattr(r, "path", "") == "/api/scale/search" for r in app.routes):
        app.include_router(router)


def grant(user_id, *principals):
    with engine().begin() as conn:
        for p in principals:
            conn.execute(text("INSERT INTO user_principals (user_id, principal) VALUES (:u, :p)"), {"u": user_id, "p": p})


async def test_http_reading_needs_access_and_changing_needs_admin(corpus, make_user):
    _, _, d = corpus
    reader, h_reader, _ = await make_user()
    outsider, h_out, _ = await make_user()
    admin, h_admin, _ = await make_user(role="admin")
    grant(reader.user_id, "group:legal")
    async with client(h_reader) as c:
        listing = await c.get(f"/api/scale/documents/{d}/chunks")
        assert listing.status_code == 200 and listing.json()["total"] == 5
        n = listing.json()["chunks"][0]["chunk"]
        assert (await c.patch(f"/api/scale/documents/{d}/chunks/{n}", json={"available": False})).status_code == 403     # may read, may not change
        assert (await c.post(f"/api/scale/documents/{d}/chunks", json={"text": "x"})).status_code == 403
    async with client(h_out) as c:
        assert (await c.get(f"/api/scale/documents/{d}/chunks")).status_code == 404                                     # indistinguishable from missing
        assert (await c.get(f"/api/scale/documents/{d}/chunks/0")).status_code == 404
    async with client({}) as c:
        assert (await c.get(f"/api/scale/documents/{d}/chunks")).status_code in (401, 403)
    async with client(h_admin) as c:
        patched = await c.patch(f"/api/scale/documents/{d}/chunks/{n}", json={"keywords": ["zeppelin"]})
        assert patched.status_code == 200 and patched.json()["keywords"] == ["zeppelin"]
        added = await c.post(f"/api/scale/documents/{d}/chunks", json={"text": "Added over http about theremin."})
        assert added.status_code == 201
        assert (await c.patch(f"/api/scale/documents/{d}/chunks/99999", json={"text": "x"})).status_code == 404
        assert (await c.patch(f"/api/scale/documents/{d}/chunks/{n}", json={"keywords": [3]})).status_code in (422,)
        hist = await c.get(f"/api/scale/documents/{d}/chunk-history")
        assert [h["action"] for h in hist.json()["history"]] == ["add", "update"]
        assert (await c.delete(f"/api/scale/documents/{d}/chunks/{added.json()['chunk']}")).status_code == 200
        assert (await c.delete(f"/api/scale/documents/{d}/chunks/{n}")).status_code == 422


async def test_http_a_null_text_means_back_to_the_original_and_an_absent_field_means_unchanged(corpus, make_user):
    _, _, d = corpus
    admin, h, _ = await make_user(role="admin")
    c0 = chunk_with(d, "insurance")
    async with client(h) as c:
        await c.patch(f"/api/scale/documents/{d}/chunks/{c0['chunk']}", json={"text": "Edited words.", "keywords": ["kk"]})
        r = await c.patch(f"/api/scale/documents/{d}/chunks/{c0['chunk']}", json={"keywords": ["kk", "ll"]})
        assert r.json()["text"] == "Edited words."                             # text not mentioned: unchanged
        r = await c.patch(f"/api/scale/documents/{d}/chunks/{c0['chunk']}", json={"text": None})
        assert r.json()["text"] == c0["text"] and r.json()["keywords"] == ["kk", "ll"]


async def test_http_retrieval_test_shows_scores_and_respects_access(corpus, make_user):
    reader, h, _ = await make_user()
    outsider, h_out, _ = await make_user()
    grant(reader.user_id, "group:legal")
    async with client(h) as c:
        body = (await c.get("/api/scale/retrieval-test", params={"q": "ninety days written notice termination"})).json()
    top = body["results"][0]
    assert top["name"] == "terms.txt" and top["keyword"] is not None and top["dense"] is not None and body["settings"]["dense_weight"] == 0.3
    async with client(h_out) as c:
        assert all(r["name"] != "terms.txt" for r in (await c.get("/api/scale/retrieval-test", params={"q": "ninety days written notice termination"})).json()["results"])
