"""TREE_GENERATION: one switch for section trees, for UI uploads and for files that come from a source."""

import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import text

from app.core.config import get_settings
from app.scale import sync
from app.scale.db import engine
from tests.scale.helpers import drain, make_source


def _pdf(path, pages=3):
    from reportlab.pdfgen import canvas

    c = canvas.Canvas(str(path))
    for i in range(pages):
        c.drawString(72, 720, f"Page {i + 1}: the supplier maintains indemnity cover of five million dirhams.")
        c.showPage()
    c.save()


def _no_model(*_a, **_k):
    raise AssertionError("a model was called although TREE_GENERATION is false")


def _engine():
    from app.rag_core.local_api import LocalAPI

    return LocalAPI(storage_path="", model="m", summary_model="m")


def _stored(doc_id):
    with engine().connect() as conn:
        return conn.execute(text("SELECT tree, pages, meta ->> 'description' FROM document_trees WHERE doc_id = :d"), {"d": doc_id}).one()


def test_off_stores_the_pages_and_no_tree_and_never_calls_a_model(tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "tree_generation", False)
    monkeypatch.setattr("app.rag_core.local_api.LocalAPI._index_flash", _no_model)
    monkeypatch.setattr("app.rag_core.utils.generate_doc_description", _no_model)
    path = tmp_path / "contract.pdf"
    _pdf(path)
    api = _engine()
    out = api.submit_document(str(path), mode="flash")
    try:
        tree, pages, description = _stored(out["doc_id"])
        assert tree == [] and len(pages) == 3 and "indemnity cover" in pages[0]["markdown"]
        assert "contract" in description
        assert api.get_ocr(out["doc_id"], format="page")["result"][1]["page_number"] == 2     # readable page by page
    finally:
        api.delete_document(out["doc_id"])


def test_on_builds_the_tree_through_the_engine(tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "tree_generation", True)
    calls = []
    structure = [{"title": "Cover", "start_index": 1, "end_index": 3, "node_id": "0000", "summary": "s"}]
    monkeypatch.setattr("app.rag_core.local_api.LocalAPI._index_flash", lambda self, f: (calls.append(f) or structure, "a described contract"))
    path = tmp_path / "contract.pdf"
    _pdf(path)
    api = _engine()
    out = api.submit_document(str(path), mode="flash")
    try:
        assert len(calls) == 1 and _stored(out["doc_id"])[0][0]["title"] == "Cover"
    finally:
        api.delete_document(out["doc_id"])


def _scan_one(tmp_path):
    cid = make_source(tmp_path, {"public/a.txt": "The supplier maintains indemnity cover of five million dirhams. " * 6})
    sync.run_scan(cid)
    return cid


def _promote_jobs():
    with engine().connect() as conn:
        return conn.execute(text("SELECT count(*) FROM scale_jobs WHERE kind = 'promote_tree'")).scalar_one()


def test_source_files_are_queued_for_a_tree_only_when_it_is_on(tmp_path, monkeypatch, object_store):
    monkeypatch.setattr(get_settings(), "tree_generation", False)
    _scan_one(tmp_path)
    drain()
    assert _promote_jobs() == 0                                            # off: nothing queued

    monkeypatch.setattr(get_settings(), "tree_generation", True)
    monkeypatch.setattr("app.scale.pipeline.promote_to_tree", lambda document_id: {"status": "stubbed"})   # no model in the test
    other = tmp_path / "second"
    other.mkdir()
    cid = make_source(other, {"public/b.txt": "Falcon programme budget approved. " * 8})
    sync.run_scan(cid)
    drain()
    with engine().connect() as conn:
        done = conn.execute(text("SELECT count(*) FROM scale_jobs WHERE kind = 'promote_tree' AND status = 'done'")).scalar_one()
    assert done >= 1                                                      # on: every new file is promoted (the stub ran)


def test_promoting_does_nothing_while_it_is_off(monkeypatch):
    from app.scale import pipeline

    monkeypatch.setattr(get_settings(), "tree_generation", False)
    assert pipeline.promote_to_tree(str(uuid.uuid4())) == {"status": "skipped", "reason": "TREE_GENERATION is off"}


async def test_the_promote_endpoint_says_why_it_refuses(monkeypatch):
    from app.scale import api

    monkeypatch.setattr(get_settings(), "tree_generation", False)
    with pytest.raises(HTTPException) as err:
        await api.promote(uuid.uuid4(), _admin=None)
    assert err.value.status_code == 409 and "TREE_GENERATION" in err.value.detail
