"use client";

import { useEffect, useState, type ReactNode } from "react";
import { motion, useReducedMotion } from "framer-motion";
import clsx from "clsx";
import { ExternalLink, FileText, Loader2, X } from "lucide-react";
import { getDocumentRaw, getPreviewUrl, PreviewUnavailableError } from "@/lib/api";
import type { DocumentOut, DocumentRawDump, TreeNode } from "@/lib/types";
import { PdfPreview } from "@/components/preview/PdfPreviewLazy";

type Tab = "preview" | "raw" | "pages" | "nodes" | "tree" | "diagrams" | "json";

interface Props {
  document: DocumentOut;
  onClose: () => void;
  onOpenPreview: (documentId: string) => void;
  /** Read-only users: just the PDF preview -- no tabs, and none of the admin-only index data is requested. */
  previewOnly?: boolean;
}

function TreeBlock({ nodes, depth = 0 }: { nodes: TreeNode[]; depth?: number }) {
  if (!nodes?.length) return null;
  return (
    <ul className={clsx("space-y-3", depth > 0 && "ml-3 border-l border-foreground pl-4")}>
      {nodes.map((n, i) => (
        <li key={`${n.node_id ?? n.title}-${i}`} className="rounded-xl border border-border bg-surface-raised p-3 text-sm shadow-soft">
          <div className="font-semibold text-foreground">
            {n.title || "(untitled)"}
            {n.page_number != null && (
              <span className="ml-2 text-xs font-normal text-muted">p.{n.page_number}</span>
            )}
            {n.node_id && <span className="ml-2 font-mono text-[10px] text-muted">{n.node_id}</span>}
          </div>
          {(n.summary || n.prefix_summary) && (
            <p className="mt-1 text-xs text-muted">{n.summary || n.prefix_summary}</p>
          )}
          {n.text && (
            <pre className="mt-2 max-h-64 overflow-auto whitespace-pre-wrap rounded-lg border border-border bg-surface p-3 font-mono text-[11px] leading-relaxed text-foreground/80">
              {n.text}
            </pre>
          )}
          {n.nodes?.length > 0 && <TreeBlock nodes={n.nodes} depth={depth + 1} />}
        </li>
      ))}
    </ul>
  );
}

function MetaRow({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div className="grid gap-1 border-b border-border py-3 text-sm last:border-0 sm:grid-cols-[150px_1fr] sm:gap-3">
      <dt className="text-[11px] font-medium text-muted">{label}</dt>
      <dd className="break-all font-mono text-[11px] text-foreground/80">{value ?? "-"}</dd>
    </div>
  );
}

export function DocumentInspector({ document, onClose, onOpenPreview, previewOnly = false }: Props) {
  const reduced = useReducedMotion();
  const [tab, setTab] = useState<Tab>(previewOnly ? "preview" : "pages");
  const [dump, setDump] = useState<DocumentRawDump | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(!previewOnly);
  const [previewUrl, setPreviewUrl] = useState<string | null>(null);
  const [previewError, setPreviewError] = useState<string | null>(null);
  const [numPages, setNumPages] = useState(0);

  useEffect(() => {
    if (previewOnly) return;
    let cancelled = false;
    setLoading(true);
    setError(null);
    getDocumentRaw(document.document_id)
      .then((data) => {
        if (!cancelled) setDump(data);
      })
      .catch((e: Error) => {
        if (!cancelled) setError(e.message || "Failed to load raw data");
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [document.document_id, previewOnly]);

  useEffect(() => {
    if (tab !== "preview") return;
    let cancelled = false;
    setPreviewUrl(null);
    setPreviewError(null);
    getPreviewUrl(document.document_id)
      .then((url) => {
        if (!cancelled) setPreviewUrl(url);
      })
      .catch((err) => {
        // Most commonly a 404 -- ingestion failed before a rendered preview
        // ever existed (see api/documents.py's preview_document). Not a
        // valid PDF to hand the viewer either way, so show a clear message
        // instead of leaving "Loading preview..." up forever. A deleted or
        // source-only file says so in its own words.
        if (!cancelled) setPreviewError(err instanceof PreviewUnavailableError ? err.message : "No preview available for this document.");
      });
    return () => {
      cancelled = true;
    };
  }, [tab, document.document_id]);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.document.addEventListener("keydown", onKeyDown);
    return () => window.document.removeEventListener("keydown", onKeyDown);
  }, [onClose]);

  const tabs: { id: Tab; label: string; count?: number }[] = [
    { id: "pages", label: "Pages / chunks", count: dump?.index.pages?.length },
    { id: "nodes", label: "Node chunks", count: dump?.index.node_chunks?.length },
    { id: "tree", label: "Tree" },
    { id: "diagrams", label: "Scanned Pages", count: dump?.diagrams?.length },
    { id: "raw", label: "Storage" },
    { id: "preview", label: "PDF preview" },
    { id: "json", label: "Full JSON" },
  ];

  return (
    <motion.div
      initial={reduced ? false : { opacity: 0, scale: 0.97 }}
      animate={{ opacity: 1, scale: 1 }}
      exit={{ opacity: 0, scale: 0.97 }}
      transition={{ duration: 0.2, ease: "easeOut" }}
      className="fixed inset-0 z-[70] flex flex-col overflow-hidden bg-background shadow-[0_0_0_100vmax_rgba(0,0,0,0.65)] sm:inset-4 sm:rounded-2xl sm:border sm:border-border-strong lg:inset-6"
    >
      <header className="flex shrink-0 items-center gap-3 border-b border-border bg-surface-raised px-4 py-4 sm:px-6">
        <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl bg-surface text-foreground">
          <FileText className="h-[18px] w-[18px]" />
        </div>
        <div className="min-w-0 flex-1">
          <h2 className="truncate text-base font-semibold tracking-tight text-foreground sm:text-lg">{document.filename}</h2>
          <p className="mt-0.5 flex items-center gap-1.5 text-[10px] text-muted">
            {document.doc_type.toUpperCase()}
            {document.page_count ? ` · ${document.page_count} pages` : ""}
            {" · "}
            <span className={document.status === "indexed" ? "font-semibold text-success" : document.status === "failed" ? "font-semibold text-danger" : "font-semibold text-warning"}>
              {document.status}
            </span>
          </p>
        </div>
        {!previewOnly && (
          <button
            type="button"
            onClick={() => onOpenPreview(document.document_id)}
            className="hidden items-center gap-1.5 rounded-lg border border-border bg-surface-raised px-3 py-2 text-xs font-semibold text-foreground shadow-soft transition hover:border-foreground sm:inline-flex"
          >
            <ExternalLink className="h-3.5 w-3.5" />
            Side preview
          </button>
        )}
        <button
          type="button"
          onClick={onClose}
          className="rounded-lg p-2 text-muted transition hover:bg-surface hover:text-foreground"
          aria-label="Close"
        >
          <X className="h-5 w-5" strokeWidth={1.75} />
        </button>
      </header>

      {!previewOnly && (
      <div className="flex shrink-0 gap-1 overflow-x-auto border-b border-border bg-surface px-3 py-2 sm:px-6">
        {tabs.map((t) => (
          <button
            key={t.id}
            type="button"
            onClick={() => setTab(t.id)}
            className={clsx(
              "shrink-0 rounded-lg px-3 py-2 text-[11px] font-semibold transition",
              tab === t.id
                ? "bg-surface-raised text-foreground shadow-soft ring-1 ring-border"
                : "text-muted hover:bg-surface-raised/70 hover:text-foreground",
            )}
          >
            {t.label}
            {t.count != null && t.count > 0 ? (
              <span className={clsx("ml-1.5 rounded-full px-1.5 py-0.5 text-[9px]", tab === t.id ? "bg-surface text-foreground" : "bg-surface text-muted")}>{t.count}</span>
            ) : null}
          </button>
        ))}
      </div>
      )}

      <div className="flex-1 overflow-y-auto bg-background px-4 py-5 sm:px-6 sm:py-6">
        {loading && (
          <div className="flex flex-col items-center justify-center gap-3 py-24 text-sm text-muted">
            <div className="flex h-10 w-10 items-center justify-center rounded-xl bg-surface text-foreground"><Loader2 className="h-4 w-4 animate-spin" /></div>
            Loading stored index…
          </div>
        )}
        {error && !loading && <p className="py-8 text-center text-sm text-danger">{error}</p>}

        {!loading && !error && dump && tab === "raw" && (
          <div className="mx-auto max-w-3xl">
            <h3 className="mb-3 text-sm font-semibold">Storage &amp; metadata</h3>
            <dl className="rounded-xl border border-border bg-surface-raised px-4 shadow-soft sm:px-5">
              <MetaRow label="document_id" value={dump.document.document_id} />
              <MetaRow label="rag_doc_id" value={dump.document.rag_doc_id} />
              <MetaRow label="content_hash" value={dump.document.content_hash} />
              <MetaRow label="storage_key" value={dump.document.storage_key} />
              <MetaRow label="preview_storage_key" value={dump.document.preview_storage_key} />
              <MetaRow label="status_detail" value={dump.document.status_detail} />
              <MetaRow label="error" value={dump.document.error} />
              <MetaRow label="is_scanned" value={String(dump.document.is_scanned)} />
              <MetaRow label="created_at" value={dump.document.created_at} />
            </dl>
            {dump.index.meta && (
              <>
                <h3 className="mb-3 mt-6 text-sm font-semibold">Tree-engine meta</h3>
                <pre className="overflow-auto rounded-2xl border border-border bg-slate-900 p-4 font-mono text-[11px] text-slate-200 shadow-soft">
                  {JSON.stringify(dump.index.meta, null, 2)}
                </pre>
              </>
            )}
            {dump.index.error && (
              <p className="mt-4 text-sm text-danger">Index load error: {dump.index.error}</p>
            )}
            <p className="mt-4 text-xs text-muted">
              Pages tab = full markdown chunks from pages.json. Node chunks = tree-node text. Tree =
              hierarchy with embedded text. Scanned Pages = OCR/VLM rows in Postgres + Qdrant (any
              document without a real text layer -- not necessarily a technical drawing).
            </p>
          </div>
        )}

        {!loading && dump && tab === "pages" && (
          <div className="mx-auto max-w-3xl space-y-4">
            {(!dump.index.pages || dump.index.pages.length === 0) && (
              <p className="text-sm text-muted">No page chunks stored yet (still indexing or failed).</p>
            )}
            {dump.index.pages?.map((page, i) => {
              const idx = page.page_number ?? i + 1;
              return (
                <section key={idx} className="overflow-hidden rounded-xl border border-border bg-surface-raised shadow-soft">
                  <div className="flex items-center justify-between border-b border-border bg-surface px-4 py-2.5">
                    <span className="text-xs font-semibold">Page {idx}</span>
                    <span className="text-[10px] text-muted">
                      {typeof page.markdown === "string" ? `${page.markdown.length} chars` : ""}
                    </span>
                  </div>
                  <pre className="max-h-[70vh] overflow-auto whitespace-pre-wrap p-4 font-mono text-[11px] leading-relaxed text-foreground/80">
                    {typeof page.markdown === "string" ? page.markdown : JSON.stringify(page, null, 2)}
                  </pre>
                </section>
              );
            })}
          </div>
        )}

        {!loading && dump && tab === "nodes" && (
          <div className="mx-auto max-w-3xl space-y-3">
            {(!dump.index.node_chunks || dump.index.node_chunks.length === 0) && (
              <p className="text-sm text-muted">No node-level chunks.</p>
            )}
            {dump.index.node_chunks?.map((node, i) => (
              <section key={i} className="overflow-hidden rounded-xl border border-border bg-surface-raised shadow-soft">
                <div className="border-b border-border bg-surface px-4 py-2.5 text-xs font-semibold text-foreground">
                  {node.title || `Node ${i + 1}`}
                  {node.page_number != null && (
                    <span className="ml-2 font-normal text-muted">p.{node.page_number}</span>
                  )}
                  {node.level != null && (
                    <span className="ml-2 font-normal text-muted">L{node.level}</span>
                  )}
                </div>
                <pre className="max-h-80 overflow-auto whitespace-pre-wrap p-4 font-mono text-[11px] leading-relaxed text-foreground/80">
                  {node.text || "(empty)"}
                </pre>
              </section>
            ))}
          </div>
        )}

        {!loading && dump && tab === "tree" && (
          <div className="mx-auto max-w-3xl">
            {(!dump.index.tree || dump.index.tree.length === 0) && (
              <p className="text-sm text-muted">No tree stored.</p>
            )}
            <TreeBlock nodes={dump.index.tree || []} />
          </div>
        )}

        {!loading && dump && tab === "diagrams" && (
          <div className="mx-auto max-w-3xl space-y-4">
            {(!dump.diagrams || dump.diagrams.length === 0) && (
              <p className="text-sm text-muted">No scanned pages for this document.</p>
            )}
            {dump.diagrams?.map((d) => (
              <section key={d.qdrant_point_id} className="rounded-xl border border-border bg-surface-raised p-4 text-sm shadow-soft sm:p-5">
                <div className="mb-2 font-semibold">
                  Page {d.page_number}
                  {d.figure_id ? ` · ${d.figure_id}` : ""}
                </div>
                <MetaRow label="image_key" value={d.image_key} />
                <MetaRow label="qdrant_point_id" value={d.qdrant_point_id} />
                <MetaRow label="embedding_model" value={d.embedding_model} />
                {d.caption && (
                  <div className="mt-2">
                    <div className="text-xs text-muted">caption</div>
                    <p>{d.caption}</p>
                  </div>
                )}
                {d.description && (
                  <div className="mt-2">
                    <div className="text-xs text-muted">description</div>
                    <p className="whitespace-pre-wrap">{d.description}</p>
                  </div>
                )}
                {d.ocr_text && (
                  <div className="mt-2">
                    <div className="text-xs text-muted">ocr_text</div>
                    <pre className="mt-1 max-h-48 overflow-auto whitespace-pre-wrap rounded-lg border border-border bg-surface p-3 font-mono text-[11px]">
                      {d.ocr_text}
                    </pre>
                  </div>
                )}
                {d.callouts != null && (
                  <pre className="mt-2 overflow-auto rounded-lg border border-border bg-surface p-3 font-mono text-[11px]">
                    {JSON.stringify(d.callouts, null, 2)}
                  </pre>
                )}
              </section>
            ))}
          </div>
        )}

        {!loading && tab === "preview" && (
          <div className="mx-auto flex h-[calc(100vh-8rem)] max-w-4xl flex-col">
            {!previewUrl && !previewError && <p className="py-12 text-center text-sm text-muted">Loading preview…</p>}
            {previewError && <p className="py-12 text-center text-sm text-muted">{previewError}</p>}
            {previewUrl && (
              <div className="min-h-0 flex-1 overflow-hidden rounded-xl border border-border bg-surface-raised shadow-panel">
                <PdfPreview
                  url={previewUrl}
                  page={1}
                  zoom={1.1}
                  onLoad={setNumPages}
                />
              </div>
            )}
            {numPages > 0 && (
              <div className="py-1.5 text-center text-[10px] text-muted">{numPages} pages</div>
            )}
          </div>
        )}

        {!loading && dump && tab === "json" && (
          <pre className="mx-auto max-w-4xl overflow-auto rounded-2xl border border-slate-800 bg-slate-950 p-4 font-mono text-[11px] leading-relaxed text-slate-200 shadow-panel">
            {JSON.stringify(dump, null, 2)}
          </pre>
        )}
      </div>
    </motion.div>
  );
}
