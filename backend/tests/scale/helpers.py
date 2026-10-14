"""Shared builders for the scale tests: a local-folder source, the worker drain, and document lookups."""

import uuid

from sqlalchemy import text

from app.scale import sync, worker
from app.scale.db import engine

RULES = [
    {"prefix": "finance", "principals": ["group:finance"]},
    {"prefix": "legal", "principals": ["group:legal"]},
    {"prefix": "public", "principals": ["*"]},
]


def write(root, rel, body):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    return path


def make_source(root, files: dict[str, str], rules=RULES) -> str:
    for rel, body in files.items():
        write(root, rel, body)
    return sync.create_connection("local", f"t-{uuid.uuid4().hex[:6]}", {"root": str(root), "acl_rules": rules})


def drain() -> None:
    assert worker.main(["--once", "--no-schedule", "--concurrency", "1"]) == 0


def ingest(root, files: dict[str, str], rules=RULES) -> str:
    cid = make_source(root, files, rules)
    sync.run_scan(cid)
    drain()
    return cid


def doc(filename: str) -> dict:
    """document_id / rag_doc_id / name of the lite document ingested from `filename`."""
    with engine().connect() as conn:
        row = conn.execute(
            text(
                "SELECT d.document_id::text AS document_id, d.rag_doc_id, t.meta ->> 'name' AS name "
                "FROM documents d JOIN document_trees t ON t.doc_id = d.rag_doc_id "
                "WHERE d.filename = :f AND d.tier = 'lite'"
            ),
            {"f": filename},
        ).mappings().first()
    assert row is not None, f"{filename} was not ingested"
    return dict(row)
