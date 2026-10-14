"""Whole path with real Postgres and embedded Qdrant: connector -> scan -> queue -> worker -> pipeline ->
index -> access-filtered search -> the existing agent tools reading the result."""

import json
import time
import uuid

import pytest
from sqlalchemy import text

from app.scale import acl, search, sync, worker
from app.scale.db import engine
from app.scale.models import HashingEmbedder

RULES = [
    {"prefix": "finance", "principals": ["group:finance"]},
    {"prefix": "legal", "principals": ["group:legal"]},
    {"prefix": "public", "principals": ["*"]},
]


def write(root, rel, body):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(body, bytes):
        path.write_bytes(body)
    else:
        path.write_text(body)
    return path


def text_pdf(path, heading, paragraph):
    from app.ingestion.text_to_pdf import render_blocks_as_pdf

    render_blocks_as_pdf([{"type": "heading", "text": heading}, {"type": "paragraph", "text": paragraph}], str(path))


def blank_pdf(path):
    from reportlab.pdfgen import canvas

    c = canvas.Canvas(str(path))
    c.showPage()
    c.save()


def drain():
    assert worker.main(["--once", "--no-schedule", "--concurrency", "1"]) == 0


def docs():
    with engine().connect() as conn:
        return conn.execute(
            text("SELECT document_id, filename, status, tier, rag_doc_id, doc_type FROM documents WHERE tier = 'lite' ORDER BY filename")
        ).mappings().all()


def visible(query, *principals):
    hits = search.search_chunks(query, principals=set(principals), limit=10)
    allowed = acl.visible_document_ids(list({h.document_id for h in hits}), set(principals))
    return {h.name for h in hits if h.document_id in allowed}


@pytest.fixture
def source(tmp_path, object_store):
    write(tmp_path, "finance/budget.txt", "The FY26 budget for Project Falcon is approved at 4.2 million dirhams.")
    write(tmp_path, "legal/contract.md", "# Termination\n\nTermination for convenience requires 30 days written notice. Reference SUB-084.")
    write(tmp_path, "public/handbook.txt", "Vacation policy: employees accrue two days of leave per month.")
    (tmp_path / "public" / "notes.xyz").write_bytes(bytes(range(256)) * 8)       # a binary nothing can read (readable text of any extension is indexed)
    text_pdf(tmp_path / "legal" / "report.pdf", "Annual Report", "The retention period for signed contracts is seven years.")
    blank_pdf(tmp_path / "public" / "scan.pdf")
    cid = sync.create_connection("local", f"t-{uuid.uuid4().hex[:6]}", {"root": str(tmp_path), "acl_rules": RULES})
    return tmp_path, cid


def test_full_lifecycle(source, monkeypatch):
    root, cid = source

    # ── first scan + ingest ──
    stats = sync.run_scan(cid)
    assert stats["status"] == "done" and stats["queued"] == 6
    drain()
    names = {d["filename"]: d for d in docs()}
    assert set(names) == {"budget.txt", "contract.md", "handbook.txt", "report.pdf"}
    assert all(d["status"] == "indexed" and d["rag_doc_id"] for d in names.values())

    with engine().connect() as conn:
        states = dict(conn.execute(text("SELECT name, state FROM source_items")).all())
        reasons = dict(conn.execute(text("SELECT name, error FROM source_items WHERE state = 'skipped'")).all())
    assert states["notes.xyz"] == "skipped" and reasons["notes.xyz"].startswith("unsupported_type")
    assert states["scan.pdf"] == "skipped" and reasons["scan.pdf"].startswith("needs_ocr")   # recorded, not lost

    # ── access control inside the search ──
    assert visible("budget Falcon", "group:finance") == {"budget.txt"}
    assert "budget.txt" not in visible("budget Falcon", "group:legal")   # legal sees its own files, never finance's
    assert "contract.md" in visible("termination notice SUB-084", "group:legal")
    assert "contract.md" not in visible("termination notice SUB-084", "group:finance", "*")
    assert visible("vacation leave", "*") == {"handbook.txt"}
    assert visible("budget termination vacation retention", "group:nobody") == set()

    # ── the existing agent tools can read a lite document: pages, structure, keyword search ──
    from app.rag_core import agent_tools
    from app.rag_core.local_api import LocalAPI
    from app.rag_core.postgres_store import PostgresDocStore

    api = object.__new__(LocalAPI)
    api._store = PostgresDocStore()
    name = names["contract.md"]
    meta_name = PostgresDocStore().get_meta(name["rag_doc_id"])["name"]
    allowed = frozenset({name["rag_doc_id"]})
    payload, is_error = agent_tools._get_page_content(api, meta_name, "1", _allowed_ids=allowed)
    assert not is_error and "30 days written notice" in json.dumps(payload)
    payload, is_error = agent_tools._get_document_structure(api, meta_name, _allowed_ids=allowed)
    assert not is_error
    payload, is_error = agent_tools._search_content(api, "SUB-084", _allowed_ids=allowed)
    assert not is_error and payload["results"] and payload["results"][0]["document"] == meta_name

    # ── rescan with no changes queues nothing ──
    assert sync.run_scan(cid)["queued"] == 0

    # ── content change: new text searchable, old text gone, no duplicate documents ──
    before = len(docs())
    time.sleep(0.01)
    write(root, "finance/budget.txt", "The FY26 budget for Project Falcon was revised to 5.9 million dirhams.")
    assert sync.run_scan(cid)["queued"] == 1
    drain()
    assert len(docs()) == before
    assert visible("revised 5.9 million", "group:finance") == {"budget.txt"}
    hits = search.search_chunks("approved 4.2 million", principals={"group:finance"}, limit=5)
    assert not any("4.2 million" in h.text for h in hits)

    # ── permission change only: access flips immediately, nothing is re-embedded ──
    calls = []
    original = HashingEmbedder.embed
    monkeypatch.setattr(HashingEmbedder, "embed", lambda self, texts: calls.append(len(texts)) or original(self, texts))
    new_rules = [r for r in RULES if r["prefix"] != "finance"] + [{"prefix": "finance", "principals": ["group:legal"]}]
    with engine().begin() as conn:
        conn.execute(text("UPDATE source_connections SET config = jsonb_set(config, '{acl_rules}', CAST(:r AS jsonb)) WHERE connection_id = :c"),
                     {"r": json.dumps(new_rules), "c": cid})
    stats = sync.run_scan(cid)
    assert stats["acl_changes"] >= 1 and stats["queued"] == 0
    budget_id = str(next(d["document_id"] for d in docs() if d["filename"] == "budget.txt"))
    assert not acl.can_read(budget_id, {"group:finance"})          # effective in Postgres before any job ran
    assert acl.can_read(budget_id, {"group:legal"})
    drain()                                                         # the index payload follows
    assert calls == []                                              # no document was re-embedded (queries come after this line)
    assert visible("revised 5.9 million", "group:finance") == set()
    assert "budget.txt" in visible("revised 5.9 million", "group:legal")   # legal now holds it (and its own files)

    # ── delete at the source: unreadable at once, physically removed by the job ──
    (root / "public" / "handbook.txt").unlink()
    stats = sync.run_scan(cid)
    assert stats["deletes"] == 1
    handbook_id = None
    with engine().connect() as conn:
        handbook_id = conn.execute(text("SELECT document_id FROM source_items WHERE name = 'handbook.txt'")).scalar_one()
    assert not acl.can_read(handbook_id, {"*"})
    drain()
    assert "handbook.txt" not in {d["filename"] for d in docs()}
    assert visible("vacation leave", "*") == set()
    with engine().connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM document_trees WHERE meta ->> 'name' = 'handbook.txt'")).scalar_one() == 0


def test_identical_files_in_two_places_become_one_document_readable_by_both(source):
    root, cid = source
    same = "Shared playbook: escalate incidents within one hour."
    write(root, "finance/playbook.txt", same)
    write(root, "legal/playbook.txt", same)
    sync.run_scan(cid)
    drain()
    playbooks = [d for d in docs() if d["filename"] == "playbook.txt"]
    assert len(playbooks) == 1
    assert "playbook.txt" in visible("escalate incidents", "group:finance")
    assert "playbook.txt" in visible("escalate incidents", "group:legal")
    assert visible("escalate incidents", "group:hr") == set()
    # remove one copy: the other still works, access narrows to the survivor
    (root / "finance" / "playbook.txt").unlink()
    sync.run_scan(cid)
    drain()
    assert "playbook.txt" in visible("escalate incidents", "group:legal")
    assert "playbook.txt" not in visible("escalate incidents", "group:finance")


def test_same_filename_in_different_folders_gets_distinct_document_names(source):
    root, cid = source
    write(root, "finance/summary.txt", "Finance summary: quarterly totals.")
    write(root, "legal/summary.txt", "Legal summary: open matters list.")
    sync.run_scan(cid)
    drain()
    with engine().connect() as conn:
        rows = conn.execute(text("SELECT meta ->> 'name', meta ->> 'description' FROM document_trees "
                                 "WHERE meta ->> 'name' LIKE 'summary%'")).all()
    assert len(rows) == 2 and len({r[0] for r in rows}) == 2          # the agent addresses documents by name
    assert {r[1] for r in rows} == {"Source: finance/summary.txt", "Source: legal/summary.txt"}


def test_a_failing_file_is_retried_then_dead_lettered_and_visible(source, monkeypatch):
    root, cid = source
    monkeypatch.setenv("SCALE_MAX_ATTEMPTS", "2")
    monkeypatch.setenv("SCALE_RETRY_BASE_SECONDS", "0")
    from app.scale.settings import get_scale_settings
    get_scale_settings.cache_clear()
    from app.scale import pipeline

    real = pipeline.extract_pages

    def boom(path, filename, *a, **k):
        if filename == "budget.txt":
            raise RuntimeError("parser exploded")
        return real(path, filename, *a, **k)

    monkeypatch.setattr(pipeline, "extract_pages", boom)
    sync.run_scan(cid)
    drain()
    drain()
    with engine().connect() as conn:
        state, err = conn.execute(text("SELECT state, error FROM source_items WHERE name = 'budget.txt'")).one()
        dead = conn.execute(text("SELECT count(*) FROM scale_jobs WHERE status = 'dead'")).scalar_one()
    assert state == "failed" and "parser exploded" in err and dead == 1
    assert "budget.txt" not in {d["filename"] for d in docs()}     # no half-indexed document is left readable
    assert "contract.md" in {d["filename"] for d in docs()}        # one bad file never blocks the others
