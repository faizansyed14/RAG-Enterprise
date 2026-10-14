"use client";

/**
 * System health (admin): is the search index complete and current, what is the worker doing, and what failed.
 * Talks to GET /api/scale/status and POST /api/scale/reconcile (backend/app/scale/api.py).
 * "Check the index" asks the worker to compare every document with the search index and repair any difference; it never
 * deletes documents or files, and an index that is already correct is left alone.
 */

import { useCallback, useEffect, useState } from "react";
import clsx from "clsx";
import { AlertTriangle, CheckCircle2, Loader2, RefreshCw, ShieldCheck } from "lucide-react";
import { getToken } from "@/lib/api";

const API_BASE = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";

interface LastRun {
  examined: number;
  verdicts: Record<string, number>;
  requeued: number;
  dry_run: boolean;
  collection_missing?: boolean;
  seconds: number;
  orphans_removed?: number;
  at?: string;
}
interface Migration {
  current: string;
  legacy: string[];
  migrating: boolean;
  documents_rebuilt: number;
  documents_remaining: number;
  percent: number;
}
interface Status {
  migration?: Migration | null;
  items: Record<string, number>;
  reasons: { state: string; reason: string; count: number }[];
  documents_by_tier: Record<string, number>;
  jobs: Record<string, Record<string, number>>;
  workers: { id: string; seen_seconds: number }[];
  oldest_queued_seconds: number | null;
  index_health: { indexed: number; pending: number; other_build: number; never_compared: number; last_run: LastRun | null };
}

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  const token = getToken();
  const res = await fetch(`${API_BASE}/api/scale${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...(token ? { Authorization: `Bearer ${token}` } : {}), ...(init?.headers ?? {}) },
  });
  if (res.status === 401) throw new Error("unauthorized");
  if (!res.ok) {
    let detail = `Request failed (${res.status})`;
    try {
      const body = await res.json();
      if (typeof body?.detail === "string") detail = body.detail;
    } catch {
      // keep the generic message
    }
    throw new Error(detail);
  }
  return res.json() as Promise<T>;
}

const VERDICT_TEXT: Record<string, string> = {
  ok: "complete",
  missing: "missing from the index",
  partial: "some chunks missing",
  extra: "stale chunks removed",
  no_vector: "no search vector",
  signature: "built with another model or setting",
  adopt: "recorded as already correct",
};

const wait = (s: number | null) => {
  if (s == null) return "nothing waiting";
  if (s < 90) return `${Math.round(s)} s`;
  if (s < 5400) return `${Math.round(s / 60)} min`;
  return `${(s / 3600).toFixed(1)} h`;
};

function Stat({ label, value, tone, hint }: { label: string; value: number | string; tone?: string; hint?: string }) {
  return (
    <div className="rounded-xl border border-border bg-surface px-4 py-3" title={hint}>
      <div className={clsx("text-2xl font-semibold tabular-nums", tone)}>{typeof value === "number" ? value.toLocaleString() : value}</div>
      <div className="text-[11px] uppercase tracking-wide text-muted">{label}</div>
    </div>
  );
}

export function HealthView({ onUnauthorized }: { onUnauthorized: () => void }) {
  const [status, setStatus] = useState<Status | null>(null);
  const [message, setMessage] = useState<{ tone: "ok" | "error"; text: string } | null>(null);
  const [busy, setBusy] = useState(false);

  const fail = useCallback(
    (err: unknown) => {
      if (err instanceof Error && err.message === "unauthorized") onUnauthorized();
      else setMessage({ tone: "error", text: err instanceof Error ? err.message : "Something went wrong" });
    },
    [onUnauthorized],
  );

  const load = useCallback(async () => {
    try {
      setStatus(await call<Status>("/status"));
    } catch (err) {
      fail(err);
    }
  }, [fail]);

  useEffect(() => {
    void load();
    const t = setInterval(() => void load(), 8000);
    return () => clearInterval(t);
  }, [load]);

  const check = async (force: boolean) => {
    setBusy(true);
    setMessage(null);
    try {
      await call(`/reconcile?force=${force}`, { method: "POST" });
      setMessage({ tone: "ok", text: force ? "Full check queued. The worker compares every document with the index." : "Check queued. The worker looks at the documents that are due." });
      await load();
    } catch (err) {
      if (err instanceof Error && err.message.startsWith("A comparison is already")) setMessage({ tone: "ok", text: err.message });
      else fail(err);
    } finally {
      setBusy(false);
    }
  };

  if (!status) {
    return (
      <div className="flex items-center gap-2 px-8 py-6 text-sm text-muted">
        <Loader2 className="h-4 w-4 animate-spin" /> Loading…
      </div>
    );
  }

  const h = status.index_health;
  const last = h.last_run;
  const failed = status.items.failed ?? 0;
  const jobKinds = Object.entries(status.jobs);
  const running = jobKinds.reduce((n, [, s]) => n + (s.running ?? 0), 0);
  const queued = jobKinds.reduce((n, [, s]) => n + (s.queued ?? 0), 0);
  const jobsFailed = jobKinds.reduce((n, [, s]) => n + (s.dead ?? 0), 0);
  const liveWorkers = status.workers.filter((w) => w.seen_seconds < 90).length;
  const noWorker = liveWorkers === 0;
  const needsAttention = noWorker || h.pending > 0 || h.other_build > 0 || failed > 0 || last?.collection_missing || jobsFailed > 0;

  return (
    <div className="flex h-full min-h-0 flex-col overflow-y-auto px-4 py-4 md:px-8">
      <div className="mx-auto w-full max-w-4xl space-y-5">
        {status.migration?.migrating && (
          <div className="rounded-xl border border-border bg-surface px-4 py-3 text-sm" role="status">
            <div className="flex items-center gap-2 font-medium">
              <Loader2 className="h-4 w-4 animate-spin" /> Search is moving to a new embedding model: {status.migration.percent}% done
            </div>
            <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-border">
              <div className="h-full rounded-full bg-foreground" style={{ width: `${Math.min(100, status.migration.percent)}%` }} />
            </div>
            <div className="mt-2 text-xs text-muted">
              {status.migration.documents_remaining.toLocaleString()} documents are still answered from the previous index, which stays in use
              until they are rebuilt. When this reaches 100%, an administrator can delete the previous index (migration retire).
            </div>
          </div>
        )}
        <div
          className={clsx(
            "flex items-start gap-2.5 rounded-xl border px-4 py-3 text-sm",
            needsAttention ? "border-warning bg-warning-soft text-warning" : "border-border bg-surface text-success",
          )}
          role="status"
        >
          {needsAttention ? <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" /> : <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0" />}
          <div className="min-w-0">
            <div className="font-medium">{needsAttention ? "Something needs a look" : "The index is complete and current"}</div>
            <div className="mt-0.5 text-xs opacity-90">
              {noWorker
                ? "No worker has reported in during the last 90 seconds. Uploads and source scans wait in the queue until one is running."
                : last?.collection_missing
                ? "The search index was not found. A full check rebuilds it from the stored pages."
                : needsAttention
                  ? "See the numbers below. A check repairs missing or stale search data by itself."
                  : `${h.indexed.toLocaleString()} documents are searchable. The worker compares them with the index on a schedule.`}
            </div>
          </div>
        </div>

        <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
          <Stat label="Searchable" value={h.indexed} tone="text-success" hint="Documents whose text is in the search index" />
          <Stat label="Waiting to index" value={h.pending} tone={h.pending ? "text-warning" : "text-muted"} hint="Read and stored, search data not written yet" />
          <Stat label="Older build" value={h.other_build} tone={h.other_build ? "text-warning" : "text-muted"} hint="Indexed with a different model or chunk setting; re-indexed by a check" />
          <Stat label="Failed files" value={failed} tone={failed ? "text-danger" : "text-muted"} hint="Files from connected sources that could not be read" />
        </div>

        <div className="flex flex-wrap items-center gap-2">
          <h2 className="text-sm font-semibold">Index check</h2>
          <button type="button" onClick={() => void load()} className="rounded-lg p-1.5 text-muted hover:bg-surface-hover" aria-label="Refresh">
            <RefreshCw className="h-3.5 w-3.5" />
          </button>
          <div className="ml-auto flex gap-2">
            <button
              type="button"
              disabled={busy}
              onClick={() => void check(false)}
              className="inline-flex items-center gap-1.5 rounded-lg border border-border px-3 py-2 text-xs font-medium hover:border-foreground disabled:opacity-40"
            >
              <ShieldCheck className="h-3.5 w-3.5" /> Check what is due
            </button>
            <button
              type="button"
              disabled={busy}
              onClick={() => void check(true)}
              className="inline-flex items-center gap-1.5 rounded-lg bg-accent px-3 py-2 text-xs font-medium text-accent-fg hover:opacity-90 disabled:opacity-40"
            >
              <ShieldCheck className="h-3.5 w-3.5" /> Check everything now
            </button>
          </div>
        </div>
        {message && (
          <div className={clsx("rounded-lg px-3 py-2 text-xs", message.tone === "ok" ? "bg-surface text-success" : "bg-warning-soft text-warning")} role="status">
            {message.text}
          </div>
        )}

        <div className="rounded-xl border border-border bg-surface px-4 py-3 text-xs">
          <div className="font-medium">Last check</div>
          {!last ? (
            <div className="mt-1 text-muted">No check has run yet. The worker runs one automatically; you can start one above.</div>
          ) : (
            <div className="mt-1 space-y-1 text-muted">
              <div>
                Looked at {last.examined.toLocaleString()} documents in {last.seconds}s; {last.requeued.toLocaleString()} sent back for re-indexing
                {typeof last.orphans_removed === "number" ? `; ${last.orphans_removed.toLocaleString()} leftover chunks of deleted documents removed` : ""}.
              </div>
              <div className="flex flex-wrap gap-x-3 gap-y-0.5">
                {Object.entries(last.verdicts).map(([k, v]) => (
                  <span key={k}>
                    <span className="font-medium text-foreground tabular-nums">{v.toLocaleString()}</span> {VERDICT_TEXT[k] ?? k}
                  </span>
                ))}
              </div>
            </div>
          )}
        </div>

        <div>
          <h2 className="mb-2 text-sm font-semibold">Worker queue</h2>
          <div className="mb-2 flex flex-wrap gap-x-5 gap-y-1 text-xs text-muted">
            <span>
              <span className={clsx("font-medium tabular-nums", noWorker ? "text-danger" : "text-foreground")}>{liveWorkers}</span>{" "}
              {liveWorkers === 1 ? "worker" : "workers"} online
            </span>
            <span><span className="font-medium text-foreground tabular-nums">{running}</span> running</span>
            <span><span className="font-medium text-foreground tabular-nums">{queued.toLocaleString()}</span> waiting</span>
            <span>
              <span className={clsx("font-medium tabular-nums", jobsFailed ? "text-danger" : "text-foreground")}>{jobsFailed}</span> failed
            </span>
            <span>oldest waiting: <span className="font-medium text-foreground">{wait(status.oldest_queued_seconds)}</span></span>
          </div>
          {jobKinds.length === 0 ? (
            <div className="rounded-xl border border-dashed border-border px-4 py-5 text-center text-xs text-muted">No jobs yet.</div>
          ) : (
            <div className="overflow-x-auto rounded-xl border border-border bg-surface">
              <table className="w-full min-w-[420px] text-left text-xs">
                <thead className="text-[11px] uppercase tracking-wide text-muted">
                  <tr>
                    <th className="px-4 py-2 font-medium">Job</th>
                    {["queued", "running", "done", "dead"].map((c) => (
                      <th key={c} className="px-3 py-2 text-right font-medium">{c === "dead" ? "failed" : c}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {jobKinds.map(([kind, s]) => (
                    <tr key={kind} className="border-t border-border">
                      <td className="px-4 py-2 font-medium">{kind.replace(/_/g, " ")}</td>
                      {["queued", "running", "done", "dead"].map((c) => (
                        <td key={c} className={clsx("px-3 py-2 text-right tabular-nums", c === "dead" && (s[c] ?? 0) > 0 ? "text-danger" : "text-muted")}>
                          {(s[c] ?? 0).toLocaleString()}
                        </td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>

        <div className="grid gap-4 sm:grid-cols-2">
          <div>
            <h2 className="mb-2 text-sm font-semibold">Files from sources</h2>
            <ul className="space-y-1 rounded-xl border border-border bg-surface px-4 py-3 text-xs">
              {Object.keys(status.items).length === 0 && <li className="text-muted">No connected-source files yet.</li>}
              {Object.entries(status.items).map(([k, v]) => (
                <li key={k} className="flex justify-between">
                  <span className="capitalize">{k}</span>
                  <span className="tabular-nums text-muted">{v.toLocaleString()}</span>
                </li>
              ))}
            </ul>
          </div>
          <div>
            <h2 className="mb-2 text-sm font-semibold">Documents by depth</h2>
            <ul className="space-y-1 rounded-xl border border-border bg-surface px-4 py-3 text-xs">
              {Object.keys(status.documents_by_tier).length === 0 && <li className="text-muted">No documents yet.</li>}
              {Object.entries(status.documents_by_tier).map(([k, v]) => (
                <li key={k} className="flex justify-between">
                  <span>{k === "tree" ? "Full section tree" : k === "lite" ? "Search-only (lite)" : k}</span>
                  <span className="tabular-nums text-muted">{v.toLocaleString()}</span>
                </li>
              ))}
            </ul>
          </div>
        </div>

        {status.reasons.length > 0 && (
          <div>
            <h2 className="mb-2 text-sm font-semibold">Why files were skipped or failed</h2>
            <ul className="space-y-1 rounded-xl border border-border bg-surface px-4 py-3 text-xs">
              {status.reasons.map((r, i) => (
                <li key={i} className="flex items-start gap-2 break-words">
                  <span className={clsx("w-14 shrink-0 font-medium", r.state === "failed" ? "text-danger" : "text-warning")}>{r.state}</span>
                  <span className="min-w-0 flex-1 text-muted">{r.reason || "no reason recorded"}</span>
                  <span className="tabular-nums text-muted">{r.count.toLocaleString()}</span>
                </li>
              ))}
            </ul>
          </div>
        )}
      </div>
    </div>
  );
}
