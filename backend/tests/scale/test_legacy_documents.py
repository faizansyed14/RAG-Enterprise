"""Documents that did NOT come through the scale pipeline: the library that existed before the layer was
switched on, and files uploaded through the UI. They keep their section tree; the scale layer must add
them to the hybrid chunk index (or automatic retrieval can never find them) and give them an access row
(or the fail-closed check hides them from everyone)."""

import hashlib
import uuid

import httpx
import pytest
from sqlalchemy import text

from app.main import app
from app.rag_core.postgres_store import PostgresDocStore
from app.scale import index as ix, pipeline, search
from app.scale.db import engine
from tests.scale.helpers import drain, ingest

PREFIX = "legacy-test-"


def make_legacy_doc(name: str, body: str, principals=("*",), status="indexed") -> dict:
    """What the old ingestion leaves behind: a documents row (tier 'tree'), a tree row with pages, and - from the
    migration's backfill - an access row for everyone."""
    document_id, rag_id = str(uuid.uuid4()), "doc-" + uuid.uuid4().hex
    filename = PREFIX + name
    meta = {"id": rag_id, "name": filename, "description": "", "status": "completed", "pageNum": 1, "mode": "standard"}
    PostgresDocStore().save_document(rag_id, meta, [{"title": "Whole", "start_index": 1, "end_index": 1, "nodes": []}],
                                     [{"page_number": 1, "markdown": body}])
    with engine().begin() as conn:
        conn.execute(
            text("INSERT INTO documents (document_id, filename, doc_type, content_hash, storage_key, status, tier, rag_doc_id) "
                 "VALUES (:d, :f, 'txt', :h, :k, :s, 'tree', :r)"),
            {"d": document_id, "f": filename, "h": hashlib.sha256(document_id.encode()).hexdigest(),
             "k": f"originals/{document_id}/{filename}", "s": status, "r": rag_id},
        )
        for p in principals:
            conn.execute(text("INSERT INTO document_access (document_id, principal) VALUES (:d, :p)"), {"d": document_id, "p": p})
    return {"document_id": document_id, "rag_doc_id": rag_id, "filename": filename}


@pytest.fixture(autouse=True)
def clean_legacy():
    def wipe():
        with engine().begin() as conn:
            conn.execute(text("DELETE FROM document_trees WHERE meta ->> 'name' LIKE :p"), {"p": PREFIX + "%"})
            conn.execute(text("DELETE FROM documents WHERE filename LIKE :p"), {"p": PREFIX + "%"})

    wipe()
    yield
    wipe()


def names(principals, query):
    return [c.name for c in search.candidate_documents(query, principals)]


def test_the_existing_library_becomes_searchable_after_the_backfill():
    make_legacy_doc("a.txt", "The FY26 budget for Project Falcon is approved at 4.2 million dirhams.")
    make_legacy_doc("b.txt", "Vacation policy: employees accrue two days of leave per month.")
    make_legacy_doc("c.txt", "Termination for convenience requires 30 days written notice. Reference SUB-084.")
    assert PREFIX + "c.txt" not in names({"*"}, "termination notice SUB-084")     # not in the chunk index yet

    assert pipeline.enqueue_unindexed() == 3
    assert pipeline.enqueue_unindexed() == 0                                       # already queued: no duplicates
    drain()

    assert names({"*"}, "termination notice SUB-084")[0] == PREFIX + "c.txt"
    assert PREFIX + "a.txt" in names({"*"}, "budget Falcon")
    assert pipeline.enqueue_unindexed() == 0                                       # nothing left to do
    with engine().connect() as conn:
        done = conn.execute(text("SELECT count(*) FROM documents WHERE filename LIKE :p AND chunks_indexed_at IS NOT NULL"),
                            {"p": PREFIX + "%"}).scalar_one()
    assert done == 3


def test_a_restricted_legacy_document_stays_restricted_in_the_index():
    make_legacy_doc("hr.txt", "Salary bands for the engineering grade are confidential.", principals=("group:hr",))
    pipeline.enqueue_unindexed()
    drain()
    assert PREFIX + "hr.txt" not in names({"*"}, "salary bands engineering")
    assert PREFIX + "hr.txt" in names({"*", "group:hr"}, "salary bands engineering")


def test_a_manual_upload_with_no_access_row_defaults_to_everyone_but_a_source_file_never_does(tmp_path, object_store):
    manual = make_legacy_doc("old-upload.txt", "Quarterly newsletter about the office move.", principals=())
    assert pipeline.index_document(manual["document_id"])["status"] == "indexed"
    assert PREFIX + "old-upload.txt" in names({"*"}, "newsletter office move")

    # a file that came from a source with no rule for its folder has an EMPTY acl on purpose
    ingest(tmp_path, {"unruled/secret.txt": "Board only: the acquisition target is Project Orion."})
    with engine().connect() as conn:
        document_id = str(conn.execute(text("SELECT document_id FROM documents WHERE filename = 'secret.txt'")).scalar_one())
    pipeline.index_document(document_id)
    with engine().connect() as conn:
        rows = conn.execute(text("SELECT principal FROM document_access WHERE document_id = :d"), {"d": document_id}).all()
    assert rows == []                                                              # still nobody
    assert "secret.txt" not in names({"*", "group:everybody"}, "acquisition target Orion")


def test_unfinished_documents_are_left_alone():
    doc = make_legacy_doc("pending.txt", "Not ready yet.", status="queued")
    assert pipeline.enqueue_unindexed() == 0
    assert pipeline.index_document(doc["document_id"]) == {"status": "not_ready"}


async def test_finishing_a_ui_upload_queues_its_chunk_indexing(monkeypatch):
    from app.ingestion.router import _queue_chunk_indexing

    document_id = uuid.uuid4()

    def jobs():
        with engine().connect() as conn:
            return conn.execute(text("SELECT count(*) FROM scale_jobs WHERE kind = 'index_document' AND payload ->> 'document_id' = :d"),
                                {"d": str(document_id)}).scalar_one()

    await _queue_chunk_indexing(document_id, "lite")
    assert jobs() == 0                                                             # the scale pipeline indexes its own
    await _queue_chunk_indexing(document_id, "tree")
    await _queue_chunk_indexing(document_id, "tree")
    assert jobs() == 1                                                             # once, however often it is called


def client(headers):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", headers=headers)


async def test_a_ui_upload_is_readable_by_everyone_and_deleting_it_removes_its_chunks(monkeypatch, object_store, make_user):
    import app.api.documents as docs_api

    monkeypatch.setattr(docs_api, "get_object_store", lambda: object_store)
    monkeypatch.setattr(docs_api, "schedule_ingest", lambda *a, **k: None)         # the model-driven ingest is not under test
    _, admin_headers, _ = await make_user(role="admin")
    _, user_headers, _ = await make_user()

    async with client(admin_headers) as c:
        resp = await c.post("/api/documents", files={"file": (PREFIX + "upload.txt", b"Minutes of the safety committee meeting.", "text/plain")})
    assert resp.status_code == 200
    document_id = resp.json()["document_id"]
    with engine().connect() as conn:
        assert [r[0] for r in conn.execute(text("SELECT principal FROM document_access WHERE document_id = :d"), {"d": document_id})] == ["*"]
    async with client(user_headers) as c:
        listed = {d["filename"] for d in (await c.get("/api/documents")).json()}
    assert PREFIX + "upload.txt" in listed                                         # a normal user can see it

    # finish the ingest by hand (what the legacy path does), index it, then delete through the API
    rag_id = "doc-" + uuid.uuid4().hex
    PostgresDocStore().save_document(rag_id, {"id": rag_id, "name": PREFIX + "upload.txt", "pageNum": 1},
                                     [], [{"page_number": 1, "markdown": "Minutes of the safety committee meeting."}])
    with engine().begin() as conn:
        conn.execute(text("UPDATE documents SET status = 'indexed', rag_doc_id = :r WHERE document_id = :d"), {"r": rag_id, "d": document_id})
    assert pipeline.index_document(document_id)["chunks"] >= 1
    collection = ix.collection_name()
    from qdrant_client import models as qm

    flt = qm.Filter(must=[qm.FieldCondition(key="document_id", match=qm.MatchValue(value=document_id))])
    assert ix.get_client().count(collection, count_filter=flt, exact=True).count >= 1
    async with client(admin_headers) as c:
        assert (await c.delete(f"/api/documents/{document_id}")).status_code in (200, 204)
    assert ix.get_client().count(collection, count_filter=flt, exact=True).count == 0


async def test_the_document_list_pages_by_keyset_cursor_without_gaps_or_repeats(make_user):
    for i in range(7):
        make_legacy_doc(f"page{i}.txt", f"body {i}")
    _, headers, _ = await make_user(role="admin")
    seen, cursor, pages = [], None, 0
    async with client(headers) as c:
        while pages < 20:
            params = {"limit": 3, "q": PREFIX + "page"}
            if cursor:
                params["cursor"] = cursor
            resp = await c.get("/api/documents", params=params)
            assert resp.status_code == 200
            seen += [d["document_id"] for d in resp.json()]
            pages += 1
            cursor = resp.headers.get("X-Next-Cursor")
            if not cursor:
                break
        assert (await c.get("/api/documents", params={"cursor": "not-a-cursor"})).status_code == 422
    assert len(seen) == 7 and len(set(seen)) == 7 and pages == 3
