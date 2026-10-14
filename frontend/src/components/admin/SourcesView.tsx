"use client";

/**
 * Connectors (admin): pick a source from the grid, set it up on its own page, then watch it here: when it was last scanned,
 * how many files are indexed, waiting, skipped or failed; scan now; switch off; add or remove files of an uploaded source;
 * and map logins to access groups so search only returns what each person may read.
 * Talks to /api/scale/... (backend/app/scale/sources_api.py, uploads_api.py). Secrets are never typed here: a source names the
 * server environment variable that holds its password or key.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import clsx from "clsx";
import { AlertTriangle, CheckCircle2, ChevronDown, ChevronUp, Loader2, Play, Plus, PlusCircle, Power, RefreshCw, Search, Share2, Trash2, Upload } from "lucide-react";
import { call, type UploadResult } from "./sources/api";
import { cardsFor, entryFor, type CatalogEntry } from "./sources/catalog";
import { ConnectorIcon } from "./sources/ConnectorIcon";
import { GenericForm } from "./sources/GenericForm";
import { ManagedConnectPanel } from "./sources/ManagedConnectPanel";
import { OnsitePage } from "./sources/OnsitePage";
import { UploadForm } from "./sources/UploadForm";

interface Kind {
  kind: string;
  group: string;
  label: string;
  template: Record<string, unknown>;
}
interface Limits {
  upload_max_files: number;
  upload_max_file_mb: number;
  upload_request_mb: number;
}
interface Connection {
  connection_id: string;
  kind: string;
  name: string;
  enabled: boolean;
  created_at: string;
  config: Record<string, unknown>;
  last_scan_started: string | null;
  last_scan_finished: string | null;
  last_error: string | null;
  items: Record<string, number>;
  scan_queued: boolean;
}
interface Problem {
  path: string;
  name: string;
  size_bytes: number | null;
  reason: string;
}
interface Principal {
  username: string;
  principal: string;
  source: string;
}
interface UploadedFile {
  name: string;
  size: number;
  modified: number;
  state: string;
}

const ago = (iso: string | null) => {
  if (!iso) return "never";
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 90) return "just now";
  if (s < 5400) return `${Math.round(s / 60)} min ago`;
  if (s < 129600) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} d ago`;
};

const STATES: { key: string; label: string; tone: string }[] = [
  { key: "indexed", label: "indexed", tone: "text-success" },
  { key: "pending", label: "waiting", tone: "text-muted" },
  { key: "skipped", label: "skipped", tone: "text-warning" },
  { key: "failed", label: "failed", tone: "text-danger" },
];

type Screen = { at: "home" } | { at: "setup"; entry: CatalogEntry } | { at: "addfiles"; connection: Connection };
const READ_ONLY_KINDS = new Set(["sharepoint", "onedrive", "graph", "teams", "outlook_mail"]);

export function SourcesView({ onUnauthorized }: { onUnauthorized: () => void }) {
  const [kinds, setKinds] = useState<Kind[]>([]);
  const [limits, setLimits] = useState<Limits | null>(null);
  const [connections, setConnections] = useState<Connection[] | null>(null);
  const [principals, setPrincipals] = useState<Principal[]>([]);
  const [message, setMessage] = useState<{ tone: "ok" | "error"; text: string } | null>(null);
  const [open, setOpen] = useState<{ id: string; state: "failed" | "skipped" } | null>(null);
  const [filesOpen, setFilesOpen] = useState<string | null>(null);
  const [screen, setScreen] = useState<Screen>({ at: "home" });
  const [query, setQuery] = useState("");
  const searchRef = useRef<HTMLInputElement>(null);

  const fail = useCallback(
    (err: unknown) => {
      if (err instanceof Error && err.message === "unauthorized") onUnauthorized();
      else setMessage({ tone: "error", text: err instanceof Error ? err.message : "Something went wrong" });
    },
    [onUnauthorized],
  );

  const load = useCallback(async () => {
    try {
      const [c, p] = await Promise.all([
        call<{ connections: Connection[] }>("/connections"),
        call<{ principals: Principal[] }>("/principals"),
      ]);
      setConnections(c.connections);
      setPrincipals(p.principals);
    } catch (err) {
      fail(err);
    }
  }, [fail]);

  useEffect(() => {
    void call<{ kinds: Kind[]; limits?: Limits }>("/kinds").then((r) => { setKinds(r.kinds); setLimits(r.limits ?? null); }).catch(fail);
    void load();
  }, [load, fail]);

  // Status moves while the worker scans, so keep it fresh without making the admin press anything.
  useEffect(() => {
    const t = setInterval(() => void load(), 8000);
    return () => clearInterval(t);
  }, [load]);

  // "/" jumps to the search box, as in the screens this layout follows.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement | null;
      if (e.key === "/" && screen.at === "home" && t && !["INPUT", "TEXTAREA", "SELECT"].includes(t.tagName)) {
        e.preventDefault();
        searchRef.current?.focus();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [screen.at]);

  const act = async (fn: () => Promise<unknown>, ok: string) => {
    try {
      await fn();
      setMessage({ tone: "ok", text: ok });
      await load();
    } catch (err) {
      fail(err);
    }
  };

  const cards = useMemo(() => cardsFor(kinds), [kinds]);
  const shown = useMemo(() => {
    const q = query.trim().toLowerCase();
    return q ? cards.filter((c) => `${c.label} ${c.blurb} ${c.kind}`.toLowerCase().includes(q)) : cards;
  }, [cards, query]);

  const home = () => { setScreen({ at: "home" }); void load(); };
  const created = (name: string) => {
    setMessage({ tone: "ok", text: `${name} added. The first scan is queued; the worker picks it up in a few seconds.` });
    home();
  };
  const uploaded = (r: UploadResult, name: string) => {
    setMessage({ tone: "ok", text: `${r.saved.length} file${r.saved.length === 1 ? "" : "s"} saved to ${name}. Indexing starts in a few seconds.${r.rejected.length ? ` ${r.rejected.length} not saved.` : ""}` });
    if (!r.rejected.length) home();
  };
  const choose = (entry: CatalogEntry) => setScreen({ at: "setup", entry });
  const chooseKind = (kind: string) => choose(entryFor(kind, kinds.find((k) => k.kind === kind)?.label));

  const frame = (child: React.ReactNode) => (
    <div className="h-full min-h-0 overflow-y-auto bg-background text-foreground">{child}</div>
  );

  if (screen.at === "setup") {
    const e = screen.entry;
    if (e.guided === "microsoft") return frame(<ManagedConnectPanel entry={e} onBack={home} onConnected={created} onError={fail} />);
    if (e.guided === "upload") return frame(<UploadForm entry={e} onBack={home} onDone={uploaded} onError={fail} limits={limits} />);
    if (e.guided === "onsite") return frame(<OnsitePage entry={e} onBack={home} onChoose={chooseKind} />);
    return frame(<GenericForm entry={e} template={kinds.find((k) => k.kind === e.kind)?.template ?? {}} onBack={home} onCreated={created} onError={fail} />);
  }
  if (screen.at === "addfiles") {
    return frame(<UploadForm entry={entryFor("upload")} onBack={home} onDone={uploaded} onError={fail} limits={limits} existingName={screen.connection.name} />);
  }

  return frame(
    <div className="mx-auto w-full max-w-6xl px-6 py-8">
      <Share2 className="h-7 w-7" strokeWidth={1.5} aria-hidden />
      <h1 className="mt-3 text-2xl font-semibold tracking-tight">Connectors</h1>

      <div className="relative mt-6">
        <Search className="pointer-events-none absolute left-3.5 top-1/2 h-4 w-4 -translate-y-1/2 text-muted" />
        <input
          ref={searchRef}
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder='Search connectors or press "/" to focus...'
          aria-label="Search connectors"
          className="h-11 w-full rounded-xl border border-border bg-surface pl-10 pr-3 text-sm outline-none focus:border-foreground focus:ring-1 focus:ring-foreground"
        />
      </div>

      {message && (
        <div className={clsx("mt-5 rounded-lg px-3 py-2 text-sm", message.tone === "ok" ? "bg-success-soft text-success" : "bg-warning-soft text-warning")} role="status">
          {message.text}
        </div>
      )}

      <h2 className="mt-8 text-base font-semibold">Add Connector</h2>
      <p className="text-sm text-muted">Connect ALAIN to your data sources.</p>
      <ul className="mt-4 grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
        {shown.map((c) => (
            <li key={c.kind}>
              <button
                type="button"
                onClick={() => choose(c)}
                className="group flex h-full w-full items-start gap-3 rounded-2xl border border-border bg-surface-raised px-4 py-3.5 text-left transition hover:border-foreground hover:shadow-sm"
              >
                <ConnectorIcon entry={c} className="mt-0.5" />
                <span className="min-w-0 flex-1">
                  <span className="block truncate text-sm font-semibold">{c.label}</span>
                  <span className="block text-xs text-muted">{c.blurb}</span>
                </span>
                <PlusCircle className="mt-0.5 h-4 w-4 shrink-0 text-muted group-hover:text-foreground" strokeWidth={1.5} />
              </button>
            </li>
        ))}
        {shown.length === 0 && <li className="col-span-full rounded-xl border border-dashed border-border px-4 py-8 text-center text-sm text-muted">No connector matches &quot;{query}&quot;.</li>}
      </ul>

      <div className="mt-10 flex items-center gap-2">
        <h2 className="text-base font-semibold">Existing Connectors</h2>
        <button type="button" onClick={() => void load()} className="rounded-lg p-1.5 text-muted hover:bg-surface-hover" aria-label="Refresh">
          <RefreshCw className="h-3.5 w-3.5" />
        </button>
      </div>
      {connections === null && (
        <div className="mt-3 flex items-center gap-2 text-sm text-muted"><Loader2 className="h-4 w-4 animate-spin" /> Loading…</div>
      )}
      {connections?.length === 0 && (
        <div className="mt-3 rounded-xl border border-dashed border-border px-4 py-8 text-center text-sm text-muted">
          No connectors yet. Pick one above and its files are indexed in the background.
        </div>
      )}
      <ul className="mt-3 space-y-3">
        {connections?.map((c) => {
          const entry = entryFor(c.kind, kinds.find((k) => k.kind === c.kind)?.label);
          const managed = c.config?.managed_upload === true;
          const fromEnv = c.config?.from_env === true;      // SharePoint / OneDrive: credentials live in the server .env
          return (
            <li key={c.connection_id} className="rounded-2xl border border-border bg-surface-raised px-4 py-3">
              <div className="flex flex-wrap items-center gap-2">
                <ConnectorIcon entry={entry} />
                <span className={clsx("h-2 w-2 rounded-full", c.enabled ? "bg-success" : "bg-border-strong")} title={c.enabled ? "On" : "Off"} />
                <span className="text-sm font-semibold">{c.name}</span>
                <span className="rounded-full bg-accent-soft px-2 py-0.5 text-[10px] uppercase tracking-wide text-muted">{managed ? "uploaded files" : entry.label}</span>
                {READ_ONLY_KINDS.has(c.kind) && <span className="rounded-full border border-border px-2 py-0.5 text-[10px] uppercase tracking-wide text-muted">read only</span>}
                {c.scan_queued && (
                  <span className="inline-flex items-center gap-1 text-[11px] text-muted"><Loader2 className="h-3 w-3 animate-spin" /> scan queued</span>
                )}
                <div className="ml-auto flex flex-wrap items-center gap-1">
                  {managed && (
                    <>
                      <button type="button" onClick={() => setScreen({ at: "addfiles", connection: c })} className="inline-flex items-center gap-1 rounded-lg border border-border px-2.5 py-1.5 text-xs hover:border-foreground">
                        <Upload className="h-3 w-3" /> Add files
                      </button>
                      <button type="button" onClick={() => setFilesOpen(filesOpen === c.connection_id ? null : c.connection_id)} className="inline-flex items-center gap-1 rounded-lg border border-border px-2.5 py-1.5 text-xs hover:border-foreground">
                        Files {filesOpen === c.connection_id ? <ChevronUp className="h-3 w-3" /> : <ChevronDown className="h-3 w-3" />}
                      </button>
                    </>
                  )}
                  <button
                    type="button"
                    disabled={!c.enabled}
                    onClick={() => void act(() => call(`/connections/${c.connection_id}/scan`, { method: "POST" }), "Scan queued.")}
                    className="inline-flex items-center gap-1 rounded-lg border border-border px-2.5 py-1.5 text-xs hover:border-foreground disabled:opacity-40"
                  >
                    <Play className="h-3 w-3" /> Scan now
                  </button>
                  <button
                    type="button"
                    onClick={() =>
                      void act(
                        () => call(`/connections/${c.connection_id}`, { method: "PATCH", body: JSON.stringify({ enabled: !c.enabled }) }),
                        c.enabled ? (fromEnv ? "Disconnected. Its files stay searchable." : "Connector switched off. Its files stay searchable.") : (fromEnv ? "Reconnected." : "Connector switched on."),
                      )
                    }
                    className="inline-flex items-center gap-1 rounded-lg border border-border px-2.5 py-1.5 text-xs hover:border-foreground"
                  >
                    <Power className="h-3 w-3" /> {c.enabled ? (fromEnv ? "Disconnect" : "Switch off") : (fromEnv ? "Reconnect" : "Switch on")}
                  </button>
                  <button
                    type="button"
                    onClick={() => {
                      if (window.confirm(`Remove "${c.name}"? Only possible when it has no indexed files.`))
                        void act(() => call(`/connections/${c.connection_id}`, { method: "DELETE" }), "Connector removed.");
                    }}
                    className="rounded-lg p-1.5 text-muted hover:bg-danger-soft hover:text-danger"
                    aria-label={`Remove ${c.name}`}
                  >
                    <Trash2 className="h-3.5 w-3.5" />
                  </button>
                </div>
              </div>
              <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-muted">
                <span>last scan {ago(c.last_scan_finished)}</span>
                {STATES.map((s) => (
                  <span key={s.key}>
                    <span className={clsx("font-medium tabular-nums", s.tone)}>{(c.items[s.key] ?? 0).toLocaleString()}</span> {s.label}
                  </span>
                ))}
                {(c.items.failed ?? 0) > 0 && (
                  <button type="button" onClick={() => setOpen(open?.id === c.connection_id && open.state === "failed" ? null : { id: c.connection_id, state: "failed" })} className="underline">
                    why failed
                  </button>
                )}
                {(c.items.skipped ?? 0) > 0 && (
                  <button type="button" onClick={() => setOpen(open?.id === c.connection_id && open.state === "skipped" ? null : { id: c.connection_id, state: "skipped" })} className="underline">
                    why skipped
                  </button>
                )}
              </div>
              {c.last_error && (
                <div className="mt-2 flex items-start gap-1.5 rounded-lg bg-warning-soft px-3 py-2 text-xs text-warning">
                  <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" /> <span className="break-words">{c.last_error}</span>
                </div>
              )}
              {open?.id === c.connection_id && <Problems connectionId={c.connection_id} state={open.state} onError={fail} />}
              {managed && filesOpen === c.connection_id && <UploadedFiles connectionId={c.connection_id} onError={fail} onChanged={load} />}
            </li>
          );
        })}
      </ul>

      <div className="mt-10">
        <AccessGroups principals={principals} onChanged={load} onMessage={setMessage} onError={fail} />
      </div>
    </div>,
  );
}

function UploadedFiles({ connectionId, onError, onChanged }: { connectionId: string; onError: (e: unknown) => void; onChanged: () => Promise<void> }) {
  const [files, setFiles] = useState<UploadedFile[] | null>(null);
  const refresh = useCallback(() => {
    call<{ files: UploadedFile[] }>(`/uploads/${connectionId}/files`).then((r) => setFiles(r.files)).catch(onError);
  }, [connectionId, onError]);
  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 8000);
    return () => clearInterval(t);
  }, [refresh]);
  const remove = async (name: string) => {
    if (!window.confirm(`Remove "${name}"? It leaves search at the next scan.`)) return;
    try {
      await call(`/uploads/${connectionId}/files?name=${encodeURIComponent(name)}`, { method: "DELETE" });
      refresh();
      await onChanged();
    } catch (err) {
      onError(err);
    }
  };
  if (!files) return <div className="mt-2 text-xs text-muted">Loading…</div>;
  return (
    <ul className="mt-2 max-h-64 divide-y divide-border overflow-y-auto rounded-xl border border-border bg-surface text-xs">
      {files.length === 0 && <li className="px-3 py-2 text-muted">No files.</li>}
      {files.map((f) => (
        <li key={f.name} className="flex items-center gap-3 px-3 py-2">
          <span className="min-w-0 flex-1 break-words font-medium">{f.name}</span>
          <span className="shrink-0 text-muted">{f.state}</span>
          <button type="button" onClick={() => void remove(f.name)} className="rounded p-1 text-muted hover:bg-danger-soft hover:text-danger" aria-label={`Remove ${f.name}`}>
            <Trash2 className="h-3.5 w-3.5" />
          </button>
        </li>
      ))}
    </ul>
  );
}

function Problems({ connectionId, state, onError }: { connectionId: string; state: "failed" | "skipped"; onError: (e: unknown) => void }) {
  const [items, setItems] = useState<Problem[] | null>(null);
  useEffect(() => {
    setItems(null);
    call<{ items: Problem[] }>(`/connections/${connectionId}/problems?state=${state}`).then((r) => setItems(r.items)).catch(onError);
  }, [connectionId, state, onError]);
  if (!items) return <div className="mt-2 text-xs text-muted">Loading…</div>;
  return (
    <ul className="mt-2 max-h-56 space-y-1 overflow-y-auto rounded-lg bg-background px-3 py-2 text-xs">
      {items.length === 0 && <li className="text-muted">Nothing to show.</li>}
      {items.map((p) => (
        <li key={p.path} className="break-words">
          <span className="font-medium">{p.path}</span> <span className="text-muted">— {p.reason || "no reason recorded"}</span>
        </li>
      ))}
    </ul>
  );
}

function AccessGroups({
  principals,
  onChanged,
  onMessage,
  onError,
}: {
  principals: Principal[];
  onChanged: () => Promise<void>;
  onMessage: (m: { tone: "ok" | "error"; text: string }) => void;
  onError: (e: unknown) => void;
}) {
  const [username, setUsername] = useState("");
  const [principal, setPrincipal] = useState("");

  const add = async () => {
    try {
      const r = await call<{ principal: string }>("/principals", { method: "POST", body: JSON.stringify({ username, principal }) });
      onMessage({ tone: "ok", text: `${username} now carries ${r.principal}.` });
      setPrincipal("");
      await onChanged();
    } catch (err) {
      onError(err);
    }
  };
  const remove = async (p: Principal) => {
    try {
      await call("/principals", { method: "DELETE", body: JSON.stringify({ username: p.username, principal: p.principal }) });
      await onChanged();
    } catch (err) {
      onError(err);
    }
  };

  return (
    <section className="space-y-3 pb-6">
      <div>
        <h2 className="text-sm font-semibold">Access groups</h2>
        <p className="text-[11px] text-muted">
          Search only returns documents a person may read. Give a login the groups that match your source permissions, for example <code>group:finance</code>,{" "}
          <code>sid:S-1-5-21-…</code> or <code>user:alice</code>. A login with no groups sees only documents open to everyone.
        </p>
      </div>
      <div className="flex flex-wrap gap-2">
        <input value={username} onChange={(e) => setUsername(e.target.value)} placeholder="Username" className="w-40 rounded-lg border border-border bg-background px-3 py-2 text-sm" />
        <input value={principal} onChange={(e) => setPrincipal(e.target.value)} placeholder="group:finance" className="w-56 rounded-lg border border-border bg-background px-3 py-2 text-sm" />
        <button
          type="button"
          disabled={!username.trim() || !principal.trim()}
          onClick={() => void add()}
          className="inline-flex items-center gap-1.5 rounded-lg border border-border px-3 py-2 text-xs font-medium hover:border-foreground disabled:opacity-40"
        >
          <Plus className="h-3.5 w-3.5" /> Give access
        </button>
      </div>
      {principals.length > 0 && (
        <ul className="divide-y divide-border rounded-xl border border-border bg-surface text-sm">
          {principals.map((p) => (
            <li key={`${p.username}|${p.principal}`} className="flex items-center gap-3 px-4 py-2">
              <span className="font-medium">{p.username}</span>
              <code className="text-xs text-muted">{p.principal}</code>
              <span className="text-[10px] uppercase tracking-wide text-muted">{p.source}</span>
              <button type="button" onClick={() => void remove(p)} className="ml-auto rounded-lg p-1.5 text-muted hover:bg-danger-soft hover:text-danger" aria-label={`Remove ${p.principal} from ${p.username}`}>
                <Trash2 className="h-3.5 w-3.5" />
              </button>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
