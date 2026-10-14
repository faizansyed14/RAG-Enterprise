"""A dead worker is the quietest way for a deployment to fail: uploads sit in the queue and nothing complains. The worker
reports in on a schedule; the container health check and the admin page both read that report."""

import os
import time

from sqlalchemy import text

from app.scale import worker
from app.scale.api import _status
from app.scale.db import engine


def test_beat_touches_the_file_and_the_status_page_lists_the_worker(tmp_path, monkeypatch):
    alive = tmp_path / "alive"
    monkeypatch.setattr(worker, "ALIVE_FILE", str(alive))
    worker.beat("host-a:1")
    assert alive.exists() and time.time() - os.path.getmtime(alive) < 5
    seen = {w["id"]: w["seen_seconds"] for w in _status()["workers"]}
    assert 0 <= seen["host-a:1"] < 30


def test_beat_survives_an_unwritable_file(monkeypatch):
    monkeypatch.setattr(worker, "ALIVE_FILE", "/nonexistent-dir/alive")
    worker.beat("host-b:2")                                  # must not raise
    assert any(w["id"] == "host-b:2" for w in _status()["workers"])


def test_old_reports_are_dropped():
    with engine().begin() as conn:
        conn.execute(text("INSERT INTO scale_state (key, value, updated_at) VALUES ('worker:gone:9', 'alive', now() - interval '3 days') "
                          "ON CONFLICT (key) DO UPDATE SET updated_at = EXCLUDED.updated_at"))
    worker.beat("host-c:3")
    assert all(w["id"] != "gone:9" for w in _status()["workers"])
    with engine().connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM scale_state WHERE key = 'worker:gone:9'")).scalar_one() == 0
