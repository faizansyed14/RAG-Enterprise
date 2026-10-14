"use client";

/**
 * SharePoint and OneDrive. Nothing is typed on this page: the tenant, app ID, secret and scope are all read from the server's .env
 * (see backend app/scale/managed.py). The page lists which variables the server found (names only, never values) and offers
 * Connect and Disconnect. Everything the app does in Microsoft 365 is read only.
 */

import { useCallback, useEffect, useState } from "react";
import clsx from "clsx";
import { AlertTriangle, CheckCircle2, Loader2, Plug, Unplug } from "lucide-react";
import { call } from "./api";
import type { CatalogEntry } from "./catalog";
import { Section, SetupFrame } from "./Frame";

interface EnvVar { name: string; set: boolean; required: boolean }
interface Managed {
  kind: string;
  label: string;
  env: EnvVar[];
  problems: string[];
  ready: boolean;
  connection: { connection_id: string; name: string; enabled: boolean } | null;
}
interface Report { ok: boolean; notes?: string[]; drive_count?: number; permissions_readable?: boolean | null }

export function ManagedConnectPanel({ entry, onBack, onConnected, onError }: {
  entry: CatalogEntry; onBack: () => void; onConnected: (name: string) => void; onError: (e: unknown) => void;
}) {
  const [info, setInfo] = useState<Managed | null>(null);
  const [busy, setBusy] = useState<"connect" | "disconnect" | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [report, setReport] = useState<Report | null>(null);

  const load = useCallback(async () => {
    try {
      const r = await call<{ connectors: Managed[] }>("/managed");
      setInfo(r.connectors.find((c) => c.kind === entry.kind) ?? null);
    } catch (err) {
      if (err instanceof Error && err.message === "unauthorized") onError(err);
      else setProblem(err instanceof Error ? err.message : "Something went wrong");
    }
  }, [entry.kind, onError]);

  useEffect(() => { void load(); }, [load]);

  const run = async (action: "connect" | "disconnect") => {
    setBusy(action);
    setProblem(null);
    setReport(null);
    try {
      const r = await call<{ report?: Report }>(`/managed/${entry.kind}/${action}`, { method: "POST" });
      if (action === "connect") {
        setReport(r.report ?? null);
        onConnected(entry.label);
      } else {
        await load();
      }
    } catch (err) {
      if (err instanceof Error && err.message === "unauthorized") onError(err);
      else setProblem(err instanceof Error ? err.message : "Something went wrong");
    } finally {
      setBusy(null);
    }
  };

  const connected = !!info?.connection?.enabled;

  return (
    <SetupFrame entry={entry} onBack={onBack} intro={`${entry.label} is configured by the server administrator in the .env file. Connect starts indexing; Disconnect stops it.`}>
      <Section title="Server settings" subtitle="Read from the server's .env when you press Connect. Only the names are shown here, never the values.">
        {!info && !problem && <div className="flex items-center gap-2 text-sm text-muted"><Loader2 className="h-4 w-4 animate-spin" /> Checking the server…</div>}
        {info && (
          <ul className="space-y-1.5">
            {info.env.map((v) => (
              <li key={v.name} className="flex items-center gap-2 text-sm">
                {v.set
                  ? <CheckCircle2 className="h-4 w-4 shrink-0 text-success" aria-label="set" />
                  : v.required
                    ? <AlertTriangle className="h-4 w-4 shrink-0 text-warning" aria-label="missing" />
                    : <span className="h-4 w-4 shrink-0 text-center text-muted" aria-label="not set">–</span>}
                <code className="font-mono text-xs">{v.name}</code>
                <span className="text-xs text-muted">{v.set ? "set" : v.required ? "not set" : "optional, not set"}</span>
              </li>
            ))}
          </ul>
        )}
        {info && !info.ready && (
          <div className="flex items-start gap-2 rounded-xl border border-warning bg-warning-soft px-4 py-3 text-sm text-warning" role="alert">
            <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
            <span>{info.problems.join("; ")}. Add {info.problems.length === 1 ? "it" : "them"} to <code>.env.dev</code> (or <code>.env.prod</code>), then restart the backend and worker.</span>
          </div>
        )}
      </Section>

      {report && report.permissions_readable === false && (
        <div className="flex items-start gap-2 rounded-xl border border-warning bg-warning-soft px-4 py-3 text-sm text-warning" role="status">
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
          The app cannot read file permissions, so with the default access mode nobody could read these files. Set the access variable to <code>everyone</code> or give the app permission to read sharing.
        </div>
      )}
      {problem && (
        <div className="flex items-start gap-2 rounded-xl border border-warning bg-warning-soft px-4 py-3 text-sm text-warning" role="alert">
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" /> <span className="break-words">{problem}</span>
        </div>
      )}

      <div className="flex flex-wrap items-center gap-3 pb-6">
        {!connected ? (
          <button type="button" disabled={busy !== null || !info?.ready} onClick={() => void run("connect")}
            className="inline-flex items-center gap-2 rounded-lg bg-accent px-4 py-2.5 text-sm font-medium text-accent-fg hover:opacity-90 disabled:opacity-40">
            {busy === "connect" ? <Loader2 className="h-4 w-4 animate-spin" /> : <Plug className="h-4 w-4" />}
            {busy === "connect" ? "Checking the credentials…" : info?.connection ? "Reconnect" : "Connect"}
          </button>
        ) : (
          <>
            <span className={clsx("inline-flex items-center gap-1.5 text-sm text-success")}><CheckCircle2 className="h-4 w-4" /> Connected</span>
            <button type="button" disabled={busy !== null} onClick={() => void run("disconnect")}
              className="inline-flex items-center gap-2 rounded-lg border border-border-strong bg-surface px-4 py-2.5 text-sm font-medium hover:border-foreground disabled:opacity-40">
              {busy === "disconnect" ? <Loader2 className="h-4 w-4 animate-spin" /> : <Unplug className="h-4 w-4" />} Disconnect
            </button>
            <span className="text-xs text-muted">Files already indexed stay searchable.</span>
          </>
        )}
      </div>
    </SetupFrame>
  );
}
