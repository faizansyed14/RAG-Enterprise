"""Durable job queue on Postgres (FOR UPDATE SKIP LOCKED).

Replaces "asyncio.create_task inside the API process" for heavy work. What that buys:
  * survives restarts: a job is a row, not a coroutine
  * many workers on many machines, no double-processing (SKIP LOCKED)
  * a worker that dies mid-job loses its lease and the job is picked up again
  * retries with exponential backoff, then a dead-letter state you can inspect and re-queue
  * dedupe: scanning the same source twice cannot pile up duplicate work

Postgres is the right queue at this scale (thousands of jobs per second is far beyond what parsing
and embedding can consume). If the client already runs Kafka / SQS / NATS, only this module changes.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text

from app.scale.db import engine
from app.scale.settings import get_scale_settings

log = logging.getLogger(__name__)


@dataclass
class Job:
    job_id: int
    kind: str
    payload: dict[str, Any]
    attempts: int
    max_attempts: int
    dedupe_key: str | None


def enqueue(
    kind: str,
    payload: dict[str, Any],
    *,
    dedupe_key: str | None = None,
    delay_seconds: float = 0.0,
    max_attempts: int | None = None,
    conn=None,
) -> int | None:
    """Returns the job id, or None when an identical job (same dedupe_key) is already queued/running."""
    max_attempts = max_attempts or get_scale_settings().max_attempts
    sql = text(
        "INSERT INTO scale_jobs (kind, payload, dedupe_key, run_after, max_attempts) "
        "VALUES (:kind, CAST(:payload AS jsonb), :dedupe, now() + make_interval(secs => :delay), :max) "
        "ON CONFLICT (dedupe_key) WHERE dedupe_key IS NOT NULL AND status IN ('queued', 'running') "
        "DO NOTHING RETURNING job_id"
    )
    params = {
        "kind": kind, "payload": json.dumps(payload), "dedupe": dedupe_key,
        "delay": float(delay_seconds), "max": max_attempts,
    }
    if conn is not None:
        row = conn.execute(sql, params).first()
    else:
        with engine().begin() as c:
            row = c.execute(sql, params).first()
    return row[0] if row else None


def claim(worker_id: str, n: int = 1, kinds: list[str] | None = None) -> list[Job]:
    """Take up to `n` jobs. Ready jobs first (an index scan in queue order); only if the slots are not
    full are jobs whose worker died (expired lease) taken over. Two cheap queries, each served by its own
    partial index -- one OR'd query cannot use either index for the ordering and slows with queue depth."""
    cfg = get_scale_settings()
    kind_filter = "AND kind = ANY(:kinds)" if kinds else ""
    ready = f"status = 'queued' AND run_after <= now() {kind_filter}"
    expired = f"status = 'running' AND lease_until < now() {kind_filter}"
    order = "ORDER BY run_after, job_id"

    def sql(where: str, order_by: str):
        return text(
            f"""
            WITH picked AS (
                SELECT job_id FROM scale_jobs
                WHERE {where}
                {order_by}
                FOR UPDATE SKIP LOCKED
                LIMIT :n
            )
            UPDATE scale_jobs j
               SET status = 'running', locked_by = :worker, attempts = j.attempts + 1,
                   lease_until = now() + make_interval(secs => :lease), updated_at = now()
              FROM picked
             WHERE j.job_id = picked.job_id
            RETURNING j.job_id, j.kind, j.payload, j.attempts, j.max_attempts, j.dedupe_key
            """
        )

    params: dict[str, Any] = {"worker": worker_id, "lease": float(cfg.lease_seconds)}
    if kinds:
        params["kinds"] = kinds
    out: list[Job] = []
    with engine().begin() as conn:
        rows = conn.execute(sql(ready, order), {**params, "n": n}).all()
        out.extend(Job(r[0], r[1], r[2], r[3], r[4], r[5]) for r in rows)
        if len(out) < n:
            rows = conn.execute(sql(expired, "ORDER BY lease_until"), {**params, "n": n - len(out)}).all()
            out.extend(Job(r[0], r[1], r[2], r[3], r[4], r[5]) for r in rows)
    return out


def heartbeat(job_id: int, worker_id: str) -> bool:
    """Extend the lease. False means the job was taken away (lease expired and re-claimed)."""
    lease = float(get_scale_settings().lease_seconds)
    with engine().begin() as conn:
        row = conn.execute(
            text(
                "UPDATE scale_jobs SET lease_until = now() + make_interval(secs => :lease), updated_at = now() "
                "WHERE job_id = :id AND locked_by = :w AND status = 'running' RETURNING 1"
            ),
            {"lease": lease, "id": job_id, "w": worker_id},
        ).first()
    return row is not None


def complete(job_id: int, worker_id: str) -> None:
    with engine().begin() as conn:
        conn.execute(
            text(
                "UPDATE scale_jobs SET status = 'done', lease_until = NULL, last_error = NULL, updated_at = now() "
                "WHERE job_id = :id AND locked_by = :w"
            ),
            {"id": job_id, "w": worker_id},
        )


def fail(job: Job, worker_id: str, error: str, *, retry: bool = True) -> str:
    """Record a failure. Returns the new status: 'queued' (will retry) or 'dead'."""
    cfg = get_scale_settings()
    dead = (not retry) or job.attempts >= job.max_attempts
    delay = min(cfg.retry_base_seconds * (2 ** max(job.attempts - 1, 0)), 3600)
    status = "dead" if dead else "queued"
    with engine().begin() as conn:
        conn.execute(
            text(
                "UPDATE scale_jobs SET status = :status, last_error = :err, lease_until = NULL, "
                "run_after = now() + make_interval(secs => :delay), updated_at = now() "
                "WHERE job_id = :id AND locked_by = :w"
            ),
            {"status": status, "err": error[:4000], "delay": float(0 if dead else delay), "id": job.job_id, "w": worker_id},
        )
    return status


def release(job: Job, worker_id: str, delay_seconds: float = 5.0) -> None:
    """Put a job back without charging it an attempt (e.g. another worker holds the same content)."""
    with engine().begin() as conn:
        conn.execute(
            text(
                "UPDATE scale_jobs SET status = 'queued', attempts = GREATEST(attempts - 1, 0), lease_until = NULL, "
                "run_after = now() + make_interval(secs => :d), updated_at = now() WHERE job_id = :id AND locked_by = :w"
            ),
            {"d": float(delay_seconds), "id": job.job_id, "w": worker_id},
        )


def requeue_dead(kind: str | None = None) -> int:
    with engine().begin() as conn:
        result = conn.execute(
            text(
                "UPDATE scale_jobs SET status = 'queued', attempts = 0, run_after = now(), last_error = NULL "
                "WHERE status = 'dead' AND (:kind IS NULL OR kind = :kind)"
            ),
            {"kind": kind},
        )
    return result.rowcount


def stats() -> dict[str, dict[str, int]]:
    with engine().connect() as conn:
        rows = conn.execute(text("SELECT kind, status, count(*) FROM scale_jobs GROUP BY kind, status")).all()
    out: dict[str, dict[str, int]] = {}
    for kind, status, count in rows:
        out.setdefault(kind, {})[status] = count
    return out


def oldest_queued_age_seconds() -> float | None:
    with engine().connect() as conn:
        row = conn.execute(
            text("SELECT extract(epoch FROM (now() - min(created_at))) FROM scale_jobs WHERE status = 'queued'")
        ).first()
    return float(row[0]) if row and row[0] is not None else None



def prune(*, done_days: float | None = None, dead_days: float | None = None, batch: int = 5000) -> dict[str, int]:
    """Delete finished jobs older than the retention windows, a bounded batch at a time so the table never takes a long lock.
    Finished ("done") jobs are only history; dead jobs are kept longer because an admin may still want to retry them.
    Queued and running jobs are never touched."""
    cfg = get_scale_settings()
    done_days = cfg.job_retention_days if done_days is None else done_days
    dead_days = cfg.dead_job_retention_days if dead_days is None else dead_days
    out = {"done": 0, "dead": 0}
    for status, days in (("done", done_days), ("dead", dead_days)):
        if days is None or days <= 0:
            continue
        with engine().begin() as conn:
            res = conn.execute(
                text(
                    "DELETE FROM scale_jobs WHERE job_id IN ("
                    "  SELECT job_id FROM scale_jobs WHERE status = :s AND updated_at < now() - make_interval(secs => :age) "
                    "  ORDER BY updated_at LIMIT :n)"
                ),
                {"s": status, "age": float(days) * 86400.0, "n": int(batch)},
            )
            out[status] = res.rowcount or 0
    return out
