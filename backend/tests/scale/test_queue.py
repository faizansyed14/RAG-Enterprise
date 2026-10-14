import threading

from sqlalchemy import text

from app.scale import queue
from app.scale.db import engine
from app.scale.settings import get_scale_settings


def test_dedupe_blocks_duplicates_until_the_first_finishes():
    first = queue.enqueue("ingest_item", {"item_id": "1"}, dedupe_key="ingest:1")
    assert first is not None
    assert queue.enqueue("ingest_item", {"item_id": "1"}, dedupe_key="ingest:1") is None
    job = queue.claim("w1")[0]
    assert queue.enqueue("ingest_item", {"item_id": "1"}, dedupe_key="ingest:1") is None   # running still blocks
    queue.complete(job.job_id, "w1")
    assert queue.enqueue("ingest_item", {"item_id": "1"}, dedupe_key="ingest:1") is not None


def test_workers_never_claim_the_same_job():
    for i in range(40):
        queue.enqueue("t", {"i": i})
    claimed: list[int] = []
    lock = threading.Lock()

    def grab(worker):
        while True:
            jobs = queue.claim(worker, n=3)
            if not jobs:
                return
            with lock:
                claimed.extend(j.job_id for j in jobs)

    threads = [threading.Thread(target=grab, args=(f"w{i}",)) for i in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(claimed) == 40 and len(set(claimed)) == 40


def test_expired_lease_is_reclaimed_by_another_worker():
    queue.enqueue("t", {})
    job = queue.claim("dead-worker")[0]
    assert queue.claim("w2") == []                        # still leased
    with engine().begin() as conn:
        conn.execute(text("UPDATE scale_jobs SET lease_until = now() - interval '1 second' WHERE job_id = :i"),
                     {"i": job.job_id})
    again = queue.claim("w2")
    assert [j.job_id for j in again] == [job.job_id] and again[0].attempts == 2


def test_heartbeat_extends_lease_and_detects_loss():
    queue.enqueue("t", {})
    job = queue.claim("w1")[0]
    assert queue.heartbeat(job.job_id, "w1")
    assert not queue.heartbeat(job.job_id, "someone-else")


def test_retry_with_backoff_then_dead_letter(monkeypatch):
    monkeypatch.setenv("SCALE_MAX_ATTEMPTS", "2")
    monkeypatch.setenv("SCALE_RETRY_BASE_SECONDS", "60")
    get_scale_settings.cache_clear()
    queue.enqueue("t", {})
    j1 = queue.claim("w")[0]
    assert queue.fail(j1, "w", "boom") == "queued"
    assert queue.claim("w") == []                         # backing off
    with engine().begin() as conn:
        conn.execute(text("UPDATE scale_jobs SET run_after = now()"))
    j2 = queue.claim("w")[0]
    assert queue.fail(j2, "w", "boom again") == "dead"
    assert queue.stats()["t"]["dead"] == 1
    assert queue.requeue_dead() == 1
    assert queue.claim("w")[0].attempts == 1


def test_release_does_not_charge_an_attempt():
    queue.enqueue("t", {})
    job = queue.claim("w")[0]
    queue.release(job, "w", delay_seconds=0)
    assert queue.claim("w")[0].attempts == 1


def test_delayed_job_is_not_claimable_yet():
    queue.enqueue("t", {}, delay_seconds=3600)
    assert queue.claim("w") == []
