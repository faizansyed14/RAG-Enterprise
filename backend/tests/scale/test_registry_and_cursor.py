"""The connector registry, and the sync layer's handling of the cursor a finished scan leaves behind."""

import importlib
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import text

from app.scale import sync
from app.scale.connectors import CONNECTORS, available_kinds, build_connector
from app.scale.connectors.base import Change, ScanBatch, SourceFile
from app.scale.db import engine


@pytest.mark.parametrize("kind", sorted(CONNECTORS))
def test_every_registered_kind_resolves_to_a_connector_that_follows_the_contract(kind):
    module, _, cls = CONNECTORS[kind].partition(":")
    klass = getattr(importlib.import_module(module), cls)
    assert klass.kind == kind
    for method in ("scan", "open", "refresh_acl"):
        assert callable(getattr(klass, method)), f"{kind} lacks {method}"


def test_unknown_kind_is_refused_before_anything_is_stored():
    with pytest.raises(ValueError, match="unknown connector kind"):
        build_connector("nope", {})
    with pytest.raises(ValueError, match="unknown connector kind"):
        sync.create_connection("nope", "x", {})
    assert {"local", "smb", "graph", "s3", "confluence", "gdrive", "sql", "web"} <= set(available_kinds())


def test_database_accepts_every_registered_kind():
    with engine().begin() as conn:
        for kind in sorted(CONNECTORS):
            conn.execute(text("INSERT INTO source_connections (kind, name, config) VALUES (:k, :n, '{}'::jsonb)"),
                         {"k": kind, "n": f"reg-{kind}-{uuid.uuid4().hex[:6]}"})
        with pytest.raises(Exception):
            conn.execute(text("INSERT INTO source_connections (kind, name, config) VALUES ('Bad Kind!', 'x', '{}'::jsonb)"))


class DeltaSource:
    """A source with a native change feed: a token comes back with every scan and only changes follow it."""

    kind = "local"

    def __init__(self):
        self.files = {"a": "one", "b": "two"}
        self.seen_cursors = []
        self.removed_since: dict[str, list[str]] = {}

    def _sf(self, key):
        return SourceFile(external_id=key, path=f"x/{key}.txt", name=f"{key}.txt", size=3, modified_at=datetime.now(timezone.utc),
                          version=self.files[key], acl=["*"])

    def scan(self, cursor):
        self.seen_cursors.append(dict(cursor))
        if not cursor:                                  # first scan: everything, and the token to continue from
            yield ScanBatch([Change("upsert", k, self._sf(k)) for k in self.files], {"token": "T1"}, done=True)
        else:                                           # later scans: only what changed, deletions included
            gone = self.removed_since.get(cursor["token"], [])
            yield ScanBatch([Change("delete", k) for k in gone], {"token": "T2"}, done=True)

    def open(self, file):
        raise NotImplementedError

    def refresh_acl(self, file):
        return ["*"]


def test_the_cursor_a_finished_scan_leaves_is_used_by_the_next_scan_so_deletions_arrive():
    cid = sync.create_connection("local", f"delta-{uuid.uuid4().hex[:6]}", {"root": "/nonexistent"})
    src = DeltaSource()
    first = sync.run_scan(cid, connector=src)
    assert first["status"] == "done" and first["upserts"] == 2
    src.removed_since["T1"] = ["a"]
    second = sync.run_scan(cid, connector=src)
    assert src.seen_cursors == [{}, {"token": "T1"}]            # without the fix the second scan started from {} again
    assert second["deletes"] == 1
    with engine().connect() as conn:
        deleted = conn.execute(text("SELECT external_id, deleted FROM source_items WHERE connection_id = :c ORDER BY external_id"),
                               {"c": cid}).all()
        stored = conn.execute(text("SELECT cursor FROM connector_state WHERE connection_id = :c"), {"c": cid}).scalar_one()
    assert deleted == [("a", True), ("b", False)] and stored == {"token": "T2"}


def test_a_crawl_style_connector_still_starts_from_scratch_every_time(tmp_path):
    (tmp_path / "f.txt").write_text("x")
    cid = sync.create_connection("local", f"crawl-{uuid.uuid4().hex[:6]}", {"root": str(tmp_path), "default_acl": ["*"]})
    sync.run_scan(cid)
    with engine().connect() as conn:
        assert conn.execute(text("SELECT cursor FROM connector_state WHERE connection_id = :c"), {"c": cid}).scalar_one() == {}
