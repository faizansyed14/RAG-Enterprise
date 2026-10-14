"use client";

/**
 * Chunk editor (admin): look at what the index holds for a document, fix a chunk's text, add keywords / questions, switch a
 * chunk off, write a chunk by hand, and try a search to see which chunks win and with what scores.
 * Talks to /api/scale/... (backend/app/scale/api.py). Edits change what search FINDS and the text of the hit; the stored page
 * (the preview and the agent's full page read) is not changed. docs/scale/CHUNK_EDITOR.md has the details.
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import clsx from "clsx";
import { EyeOff, Loader2, Plus, RotateCcw, Save, Search, Trash2 } from "lucide-react";
import { getToken } from "@/lib/api";
import type { DocumentOut } from "@/lib/types";

const API_BASE = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";
const PAGE = 25;

interface Chunk {
  chunk: number;
  page: number;
  kind: string;
  text: string;
  original_text: string | null;
  available: boolean;
  keywords: string[];
  questions: string[];
  generated_keywords: string[];
  generated_questions: string[];
}
interface ChunkList {
  total: number;
  chunks: Chunk[];
  orphaned_edits: { chunk: number; text: string | null; original_text: string }[];
}
interface TestHit {
  document_id: string;
  name: string;
  page: number;
  chunk: number;
  kind: string;
  score: number;
  dense: number | null;
  keyword: number | null;
  text: string;
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

const splitList = (s: string) =>
  s
    .split(/[\n,]/)
    .map((x) => x.trim())
    .filter(Boolean);
const splitLines = (s: string) =>
  s
    .split("\n")
    .map((x) => x.trim())
    .filter(Boolean);

export function ChunkEditorView({ documents, onUnauthorized }: { documents: DocumentOut[]; onUnauthorized: () => void }) {
  const indexed = useMemo(() => documents.filter((d) => d.status === "indexed"), [documents]);
  const [docId, setDocId] = useState<string>("");
  const [query, setQuery] = useState("");
  const [offset, setOffset] = useState(0);
  const [list, setList] = useState<ChunkList | null>(null);
  const [loading, setLoading] = useState(false);
  const [message, setMessage] = useState<{ tone: "ok" | "error"; text: string } | null>(null);
  const [adding, setAdding] = useState(false);

  const fail = useCallback(
    (err: unknown) => {
      if (err instanceof Error && err.message === "unauthorized") onUnauthorized();
      else setMessage({ tone: "error", text: err instanceof Error ? err.message : "Something went wrong" });
    },
    [onUnauthorized],
  );

  const load = useCallback(async () => {
    if (!docId) return;
    setLoading(true);
    try {
      const qs = new URLSearchParams({ offset: String(offset), limit: String(PAGE), q: query });
      setList(await call<ChunkList>(`/documents/${docId}/chunks?${qs}`));
    } catch (err) {
      fail(err);
    } finally {
      setLoading(false);
    }
  }, [docId, offset, query, fail]);

  useEffect(() => {
    void load();
  }, [load]);

  return (
    <div className="flex h-full min-h-0 flex-col overflow-y-auto px-4 py-4 md:px-8">
      <div className="mx-auto w-full max-w-4xl space-y-4">
        <div className="flex flex-wrap items-center gap-2">
          <select
            value={docId}
            onChange={(e) => {
              setDocId(e.target.value);
              setOffset(0);
              setList(null);
              setMessage(null);
            }}
            className="min-w-0 flex-1 rounded-lg border border-border bg-background px-3 py-2 text-sm"
            aria-label="Document"
          >
            <option value="">Choose a document…</option>
            {indexed.map((d) => (
              <option key={d.document_id} value={d.document_id}>
                {d.filename}
              </option>
            ))}
          </select>
          {docId && (
            <>
              <input
                value={query}
                onChange={(e) => {
                  setQuery(e.target.value);
                  setOffset(0);
                }}
                placeholder="Filter chunks…"
                className="w-48 rounded-lg border border-border bg-background px-3 py-2 text-sm"
              />
              <button type="button" onClick={() => setAdding((v) => !v)} className="inline-flex items-center gap-1.5 rounded-lg border border-border px-3 py-2 text-xs font-medium hover:border-foreground">
                <Plus className="h-3.5 w-3.5" /> Add chunk
              </button>
            </>
          )}
        </div>

        {message && (
          <div className={clsx("rounded-lg px-3 py-2 text-xs", message.tone === "ok" ? "bg-surface text-success" : "bg-warning-soft text-warning")} role="status">
            {message.text}
          </div>
        )}

        {adding && docId && (
          <NewChunk
            documentId={docId}
            onDone={(text) => {
              setAdding(false);
              if (text) setMessage({ tone: "ok", text });
              void load();
            }}
            onError={fail}
          />
        )}

        {docId && loading && !list && (
          <div className="flex items-center gap-2 text-sm text-muted">
            <Loader2 className="h-4 w-4 animate-spin" /> Loading chunks…
          </div>
        )}

        {list && list.orphaned_edits.length > 0 && (
          <div className="rounded-lg border border-border bg-warning-soft px-3 py-2 text-xs text-warning">
            {list.orphaned_edits.length} saved edit(s) no longer match any chunk because the file changed. They are kept but not applied.
          </div>
        )}

        {list?.chunks.map((c) => (
          <ChunkCard key={`${c.chunk}:${c.text.length}:${c.available}`} documentId={docId} chunk={c} onChanged={load} onSaved={(t) => setMessage({ tone: "ok", text: t })} onError={fail} />
        ))}

        {list && list.total === 0 && <div className="text-sm text-muted">No chunks match.</div>}

        {list && list.total > PAGE && (
          <div className="flex items-center justify-between text-xs text-muted">
            <button type="button" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE))} className="rounded-lg border border-border px-3 py-1.5 disabled:opacity-40">
              Previous
            </button>
            <span>
              {offset + 1}–{Math.min(offset + PAGE, list.total)} of {list.total}
            </span>
            <button type="button" disabled={offset + PAGE >= list.total} onClick={() => setOffset(offset + PAGE)} className="rounded-lg border border-border px-3 py-1.5 disabled:opacity-40">
              Next
            </button>
          </div>
        )}

        <RetrievalTest onError={fail} />
      </div>
    </div>
  );
}

function ChunkCard({
  documentId,
  chunk,
  onChanged,
  onSaved,
  onError,
}: {
  documentId: string;
  chunk: Chunk;
  onChanged: () => Promise<void> | void;
  onSaved: (text: string) => void;
  onError: (err: unknown) => void;
}) {
  const [text, setText] = useState(chunk.text);
  const [keywords, setKeywords] = useState(chunk.keywords.join(", "));
  const [questions, setQuestions] = useState(chunk.questions.join("\n"));
  const [busy, setBusy] = useState(false);
  const dirty = text !== chunk.text || keywords !== chunk.keywords.join(", ") || questions !== chunk.questions.join("\n");

  const patch = async (body: Record<string, unknown>, done: string) => {
    setBusy(true);
    try {
      await call(`/documents/${documentId}/chunks/${chunk.chunk}`, { method: "PATCH", body: JSON.stringify(body) });
      onSaved(done);
      await onChanged();
    } catch (err) {
      onError(err);
    } finally {
      setBusy(false);
    }
  };

  const remove = async () => {
    if (!window.confirm("Delete this hand-written chunk?")) return;
    setBusy(true);
    try {
      await call(`/documents/${documentId}/chunks/${chunk.chunk}`, { method: "DELETE" });
      onSaved("Chunk deleted.");
      await onChanged();
    } catch (err) {
      onError(err);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className={clsx("rounded-xl border border-border bg-background p-3", !chunk.available && "opacity-60")}>
      <div className="mb-2 flex flex-wrap items-center gap-2 text-[11px] text-muted">
        <span className="font-medium text-foreground">#{chunk.chunk}</span>
        <span>page {chunk.page}</span>
        <span className="rounded-full bg-surface px-2 py-0.5">{chunk.kind}</span>
        {chunk.original_text !== null && <span className="rounded-full bg-surface px-2 py-0.5">edited</span>}
        {!chunk.available && <span className="rounded-full bg-surface px-2 py-0.5">switched off</span>}
      </div>
      <textarea
        value={text}
        onChange={(e) => setText(e.target.value)}
        rows={Math.min(10, Math.max(3, Math.ceil(text.length / 110)))}
        className="w-full resize-y rounded-lg border border-border bg-surface px-3 py-2 text-sm leading-relaxed"
        aria-label={`Text of chunk ${chunk.chunk}`}
      />
      <div className="mt-2 grid gap-2 md:grid-cols-2">
        <label className="text-[11px] text-muted">
          Keywords (comma separated)
          <input value={keywords} onChange={(e) => setKeywords(e.target.value)} className="mt-1 w-full rounded-lg border border-border bg-surface px-3 py-1.5 text-sm text-foreground" />
        </label>
        <label className="text-[11px] text-muted">
          Questions this chunk answers (one per line)
          <textarea value={questions} onChange={(e) => setQuestions(e.target.value)} rows={2} className="mt-1 w-full rounded-lg border border-border bg-surface px-3 py-1.5 text-sm text-foreground" />
        </label>
      </div>
      {(chunk.generated_keywords.length > 0 || chunk.generated_questions.length > 0) && (
        <div className="mt-2 text-[11px] text-muted">
          Written by the model: {[...chunk.generated_keywords, ...chunk.generated_questions].join(" · ")}
        </div>
      )}
      <div className="mt-3 flex flex-wrap items-center gap-2">
        <button
          type="button"
          disabled={!dirty || busy}
          onClick={() => patch({ text, keywords: splitList(keywords), questions: splitLines(questions) }, "Saved.")}
          className="inline-flex items-center gap-1.5 rounded-lg bg-foreground px-3 py-1.5 text-xs font-medium text-background disabled:opacity-40"
        >
          <Save className="h-3.5 w-3.5" /> Save
        </button>
        {chunk.original_text !== null && (
          <button type="button" disabled={busy} onClick={() => patch({ text: null }, "Back to the extracted text.")} className="inline-flex items-center gap-1.5 rounded-lg border border-border px-3 py-1.5 text-xs font-medium hover:border-foreground">
            <RotateCcw className="h-3.5 w-3.5" /> Restore original
          </button>
        )}
        <button type="button" disabled={busy} onClick={() => patch({ available: !chunk.available }, chunk.available ? "Switched off: search will skip this chunk." : "Switched on.")} className="inline-flex items-center gap-1.5 rounded-lg border border-border px-3 py-1.5 text-xs font-medium hover:border-foreground">
          <EyeOff className="h-3.5 w-3.5" /> {chunk.available ? "Switch off" : "Switch on"}
        </button>
        {chunk.kind === "manual" && (
          <button type="button" disabled={busy} onClick={remove} className="inline-flex items-center gap-1.5 rounded-lg border border-border px-3 py-1.5 text-xs font-medium text-warning hover:border-warning">
            <Trash2 className="h-3.5 w-3.5" /> Delete
          </button>
        )}
        {busy && <Loader2 className="h-4 w-4 animate-spin text-muted" />}
      </div>
    </div>
  );
}

function NewChunk({ documentId, onDone, onError }: { documentId: string; onDone: (message?: string) => void; onError: (err: unknown) => void }) {
  const [text, setText] = useState("");
  const [page, setPage] = useState(1);
  const [keywords, setKeywords] = useState("");
  const [busy, setBusy] = useState(false);

  const save = async () => {
    setBusy(true);
    try {
      await call(`/documents/${documentId}/chunks`, { method: "POST", body: JSON.stringify({ text, page, keywords: splitList(keywords) }) });
      onDone("Chunk added.");
    } catch (err) {
      onError(err);
      setBusy(false);
    }
  };

  return (
    <div className="rounded-xl border border-dashed border-border p-3">
      <textarea value={text} onChange={(e) => setText(e.target.value)} rows={4} placeholder="Text of the new chunk…" className="w-full rounded-lg border border-border bg-surface px-3 py-2 text-sm" />
      <div className="mt-2 flex flex-wrap items-center gap-2">
        <label className="text-[11px] text-muted">
          Page{" "}
          <input type="number" min={1} value={page} onChange={(e) => setPage(Math.max(1, Number(e.target.value) || 1))} className="w-16 rounded-lg border border-border bg-surface px-2 py-1 text-sm text-foreground" />
        </label>
        <input value={keywords} onChange={(e) => setKeywords(e.target.value)} placeholder="Keywords, comma separated" className="min-w-0 flex-1 rounded-lg border border-border bg-surface px-3 py-1.5 text-sm" />
        <button type="button" disabled={busy || !text.trim()} onClick={save} className="inline-flex items-center gap-1.5 rounded-lg bg-foreground px-3 py-1.5 text-xs font-medium text-background disabled:opacity-40">
          <Plus className="h-3.5 w-3.5" /> Add
        </button>
        <button type="button" onClick={() => onDone()} className="rounded-lg border border-border px-3 py-1.5 text-xs font-medium">
          Cancel
        </button>
      </div>
    </div>
  );
}

function RetrievalTest({ onError }: { onError: (err: unknown) => void }) {
  const [q, setQ] = useState("");
  const [rerank, setRerank] = useState(true);
  const [hits, setHits] = useState<TestHit[] | null>(null);
  const [busy, setBusy] = useState(false);

  const run = async () => {
    if (q.trim().length < 2) return;
    setBusy(true);
    try {
      const qs = new URLSearchParams({ q, k: "10", rerank: String(rerank) });
      const out = await call<{ results: TestHit[] }>(`/retrieval-test?${qs}`);
      setHits(out.results);
    } catch (err) {
      onError(err);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="rounded-xl border border-border p-3">
      <div className="mb-2 text-sm font-semibold">Try a search</div>
      <div className="flex flex-wrap items-center gap-2">
        <input
          value={q}
          onChange={(e) => setQ(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && void run()}
          placeholder="Ask what a user would ask…"
          className="min-w-0 flex-1 rounded-lg border border-border bg-background px-3 py-2 text-sm"
        />
        <label className="flex items-center gap-1.5 text-xs text-muted">
          <input type="checkbox" checked={rerank} onChange={(e) => setRerank(e.target.checked)} /> rerank
        </label>
        <button type="button" onClick={run} disabled={busy} className="inline-flex items-center gap-1.5 rounded-lg bg-foreground px-3 py-2 text-xs font-medium text-background disabled:opacity-40">
          {busy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Search className="h-3.5 w-3.5" />} Search
        </button>
      </div>
      {hits && hits.length === 0 && <div className="mt-3 text-sm text-muted">Nothing you can read matches.</div>}
      <ol className="mt-3 space-y-2">
        {hits?.map((h, i) => (
          <li key={`${h.document_id}:${h.chunk}`} className="rounded-lg bg-surface px-3 py-2 text-xs">
            <div className="mb-1 flex flex-wrap gap-x-3 text-[11px] text-muted">
              <span className="font-medium text-foreground">
                {i + 1}. {h.name}
              </span>
              <span>page {h.page}</span>
              <span>chunk {h.chunk}</span>
              <span>{h.kind}</span>
              <span>score {h.score}</span>
              {h.dense !== null && <span>meaning {h.dense}</span>}
              {h.keyword !== null && <span>words {h.keyword}</span>}
            </div>
            <div className="line-clamp-3 leading-relaxed">{h.text}</div>
          </li>
        ))}
      </ol>
    </div>
  );
}
