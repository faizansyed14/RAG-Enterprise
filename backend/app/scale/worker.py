"""The worker process:  python -m app.scale.worker

Runs the heavy, retryable work outside the API process: scanning sources, parsing, embedding,
indexing, permission updates, deletions, tree promotion. Start as many as the hardware allows -- jobs
are claimed with SKIP LOCKED, so workers never collide.
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import socket
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

from sqlalchemy import text

from app.scale import queue
from app.scale.db import engine
from app.scale.settings import get_scale_settings

log = logging.getLogger("scale.worker")


def _h_scan(p: dict) -> dict:
    from app.scale.sync import run_scan

    return run_scan(p["connection_id"])


def _h_ingest(p: dict) -> dict:
    from app.scale import pipeline

    try:
        return pipeline.ingest_item(p["item_id"])
    except pipeline.RetryLater:
        raise
    except Exception as exc:
        pipeline._mark(p["item_id"], "failed", str(exc)[:1000])
        raise


def _h_delete(p: dict) -> dict:
    from app.scale import pipeline

    return pipeline.delete_item(p["item_id"])


def _h_index_acl(p: dict) -> dict:
    from app.scale import pipeline

    return {"principals": len(pipeline._refresh_access(p["document_id"]))}


def _h_acl_sweep(p: dict) -> dict:
    from app.scale.sync import run_acl_sweep

    return run_acl_sweep(p["connection_id"], older_than_hours=get_scale_settings().acl_sweep_hours)


def _h_index_document(p: dict) -> dict:
    from app.scale import pipeline

    return pipeline.index_document(p["document_id"])


def _h_reconcile(p: dict) -> dict:
    from app.scale import reconcile

    return reconcile.run(force=bool(p.get("force")), orphans=p.get("orphans"))


def _h_promote(p: dict) -> dict:
    from app.scale import pipeline

    return pipeline.promote_to_tree(p["document_id"])


HANDLERS = {
    "scan": _h_scan, "ingest_item": _h_ingest, "delete_item": _h_delete,
    "index_acl": _h_index_acl, "index_document": _h_index_document, "acl_sweep": _h_acl_sweep, "promote_tree": _h_promote,
    "reconcile": _h_reconcile,
}


def run_job(job: queue.Job, worker_id: str) -> None:
    from app.scale.pipeline import RetryLater

    stop = threading.Event()
    lease = get_scale_settings().lease_seconds

    def beat() -> None:
        while not stop.wait(max(lease / 3, 1)):
            if not queue.heartbeat(job.job_id, worker_id):
                log.warning("job %s lost its lease", job.job_id)
                return

    threading.Thread(target=beat, daemon=True).start()
    try:
        handler = HANDLERS.get(job.kind)
        if handler is None:
            queue.fail(job, worker_id, f"unknown job kind {job.kind}", retry=False)
            return
        result = handler(job.payload)
        queue.complete(job.job_id, worker_id)
        log.info("job %s %s -> %s", job.job_id, job.kind, result)
    except RetryLater:
        queue.release(job, worker_id, delay_seconds=10.0)
    except Exception as exc:  # noqa: BLE001
        status = queue.fail(job, worker_id, f"{type(exc).__name__}: {exc}")
        log.exception("job %s %s failed (%s)", job.job_id, job.kind, status)
    finally:
        stop.set()


_PRUNED_AT = 0.0


def _prune_if_due() -> None:
    """Retire old finished jobs at most every 10 minutes, per worker process. Never raises: housekeeping must not stop scheduling."""
    global _PRUNED_AT
    if time.monotonic() - _PRUNED_AT < 600:
        return
    _PRUNED_AT = time.monotonic()
    try:
        gone = queue.prune()
        if gone["done"] or gone["dead"]:
            log.info("retired finished jobs: %s", gone)
    except Exception:  # noqa: BLE001
        log.warning("job retention failed", exc_info=True)


def tick_schedule() -> None:
    """Enqueue periodic scans and permission sweeps for every enabled connection, and chunk-indexing for any
    document (UI upload, pre-existing library) that is not in the chunk index yet, and the reconcile job that repairs
    chunks that went missing or were built by another model."""
    cfg = get_scale_settings()
    from app.scale import pipeline

    pipeline.enqueue_unindexed()
    _prune_if_due()
    from app.scale import reconcile

    if reconcile.has_work(cfg):
        queue.enqueue("reconcile", {}, dedupe_key="reconcile")
    with engine().connect() as conn:
        rows = conn.execute(
            text(
                "SELECT c.connection_id, s.last_scan_started FROM source_connections c "
                "LEFT JOIN connector_state s USING (connection_id) WHERE c.enabled AND "
                "(s.last_scan_started IS NULL OR s.last_scan_started < now() - make_interval(secs => :i))"
            ),
            {"i": float(cfg.scan_interval_seconds)},
        ).all()
    for connection_id, _ in rows:
        queue.enqueue("scan", {"connection_id": str(connection_id)}, dedupe_key=f"scan:{connection_id}")
        queue.enqueue("acl_sweep", {"connection_id": str(connection_id)}, dedupe_key=f"sweep:{connection_id}")


ALIVE_FILE = os.environ.get("SCALE_WORKER_ALIVE_FILE", "/tmp/scale-worker.alive")


def beat(worker_id: str) -> None:
    """Say "this worker is alive": touch a file (the container health check reads it) and write a row the admin
    status page reads. Never raises: a missed beat must not stop a worker that is otherwise fine."""
    try:
        with open(ALIVE_FILE, "w") as fh:
            fh.write(str(time.time()))
    except OSError:
        pass
    try:
        with engine().begin() as conn:
            conn.execute(
                text("INSERT INTO scale_state (key, value) VALUES (:k, :v) "
                     "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()"),
                {"k": f"worker:{worker_id}", "v": "alive"},
            )
            conn.execute(text("DELETE FROM scale_state WHERE key LIKE 'worker:%' AND updated_at < now() - interval '1 day'"))
    except Exception as exc:  # noqa: BLE001
        log.warning("worker heartbeat not written: %s", str(exc).splitlines()[0][:120])


def wait_for_schema(timeout_s: float = 300.0, stopping: threading.Event | None = None) -> bool:
    """Block until the migrations that create the job queue have run (a worker may start before the API has
    migrated). Returns False if the schema never appeared or the worker was told to stop."""
    deadline = time.time() + timeout_s
    announced = False
    while time.time() < deadline and not (stopping and stopping.is_set()):
        try:
            with engine().connect() as conn:
                if conn.execute(text("SELECT to_regclass('scale_jobs')")).scalar():
                    return True
        except Exception as exc:  # noqa: BLE001 -- the database itself may still be starting
            log.info("waiting for the database: %s", str(exc).splitlines()[0][:120])
        else:
            if not announced:
                log.info("waiting for database migrations (alembic upgrade head) ...")
                announced = True
        time.sleep(2)
    return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="scale ingest worker")
    parser.add_argument("--once", action="store_true", help="drain the queue then exit (tests, batch runs)")
    parser.add_argument("--no-schedule", action="store_true", help="do not enqueue periodic scans")
    parser.add_argument("--concurrency", type=int, default=None)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    cfg = get_scale_settings()
    concurrency = args.concurrency or cfg.worker_concurrency
    worker_id = f"{socket.gethostname()}:{os.getpid()}"
    stopping = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stopping.set())

    if not wait_for_schema(stopping=stopping):
        log.error("the database schema is not ready; run `alembic upgrade head` and start the worker again")
        return 1

    last_tick = 0.0
    last_beat = 0.0
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        running: set = set()
        while not stopping.is_set():
            running = {f for f in running if not f.done()}
            if not args.once and time.time() - last_beat > 20:
                beat(worker_id)
                last_beat = time.time()
            if not args.once and not args.no_schedule and time.time() - last_tick > 30:
                tick_schedule()
                last_tick = time.time()
            free = concurrency - len(running)
            jobs = queue.claim(worker_id, n=free) if free > 0 else []
            for job in jobs:
                running.add(pool.submit(run_job, job, worker_id))
            if args.once and not jobs and not running:
                break
            if jobs:
                continue                      # refill any free slot straight away
            if running:
                # Every slot is busy: wake the moment one finishes. (A fixed sleep here capped each slot
                # at one job per poll interval, whatever the job's real duration.)
                wait(running, timeout=cfg.poll_seconds, return_when=FIRST_COMPLETED)
            else:
                time.sleep(cfg.poll_seconds)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
