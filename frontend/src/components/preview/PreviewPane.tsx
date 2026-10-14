"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { FileText, ImageIcon, Loader2, Network, X, ZoomIn, ZoomOut } from "lucide-react";
import { getDiagramPreview, getPreviewInfo, type PreviewInfo } from "@/lib/api";
import { SourceOnlyView } from "./SourceOnlyView";
import type { Citation, DiagramEvidence } from "@/lib/types";
import { TreeView } from "@/components/tree/TreeView";
import { ImagePreview } from "./ImagePreview";
import { PdfPreview } from "./PdfPreviewLazy";

export type PreviewTarget =
  | { kind: "pdf"; documentId: string; page: number; label: string }
  | { kind: "diagram"; documentId: string; page: number; label: string };

export function citationToTarget(citation: Citation): PreviewTarget | null {
  if (!citation.document_id) return null;
  return { kind: "pdf", documentId: citation.document_id, page: citation.page, label: citation.document };
}

export function diagramToTarget(evidence: DiagramEvidence): PreviewTarget {
  return {
    kind: "diagram",
    documentId: evidence.document_id,
    page: evidence.page_number,
    label: evidence.figure_id ?? `Page ${evidence.page_number}`,
  };
}

const WIDTH_KEY = "rag_preview_width";
const MIN_W = 300;
const MAX_W = 900;
const DEFAULT_W = 440;

function clampWidth(width: number) {
  if (typeof window === "undefined") return Math.min(MAX_W, Math.max(MIN_W, width));
  const max = Math.min(MAX_W, Math.floor(window.innerWidth * 0.7));
  return Math.min(max, Math.max(MIN_W, width));
}

export function PreviewPane({ target, onClose }: { target: PreviewTarget | null; onClose: () => void }) {
  const [url, setUrl] = useState<string | null>(null);
  const [notFound, setNotFound] = useState<string | null>(null);        // the stored copy is gone and nothing else can show it
  const [sourceInfo, setSourceInfo] = useState<PreviewInfo | null>(null);   // set when the original is not kept (STORE_ORIGINALS=false)
  const [loadError, setLoadError] = useState(false);
  const [jumpPage, setJumpPage] = useState(1);
  const [visiblePage, setVisiblePage] = useState(1);
  const [numPages, setNumPages] = useState<number | null>(null);
  const [zoom, setZoom] = useState(1);
  const [showTree, setShowTree] = useState(false);
  const [width, setWidth] = useState(DEFAULT_W);
  const [dragging, setDragging] = useState(false);
  const [isDesktop, setIsDesktop] = useState(false);
  const dragStart = useRef<{ x: number; width: number } | null>(null);
  const loadedKeyRef = useRef<string | null>(null);

  useEffect(() => {
    const saved = window.localStorage.getItem(WIDTH_KEY);
    if (saved) {
      const parsed = Number(saved);
      if (!Number.isNaN(parsed)) setWidth(clampWidth(parsed));
    }
    const media = window.matchMedia("(min-width: 1024px)");
    const sync = () => setIsDesktop(media.matches);
    sync();
    media.addEventListener("change", sync);
    return () => media.removeEventListener("change", sync);
  }, []);

  useEffect(() => {
    if (!target) {
      setUrl(null);
      setSourceInfo(null);
      setNotFound(null);
      setLoadError(false);
      loadedKeyRef.current = null;
      return;
    }

    // A PDF's presigned URL carries a fresh signature/expiry on every
    // fetch, so re-fetching it for every citation click made react-pdf
    // treat each click as a brand-new file and reload the whole document
    // from scratch -- even when it's the same document already open,
    // which is the common case (several citations from one source). That
    // reload raced against the page-jump, and losing the race is exactly
    // what looked like "citations always open on page 1". A diagram's
    // preview is one image per page, so it always reloads for a new page.
    const key = target.kind === "pdf" ? `pdf:${target.documentId}` : `diagram:${target.documentId}:${target.page}`;
    if (loadedKeyRef.current === key) {
      setJumpPage(target.page);
      setVisiblePage(target.page);
      return;
    }
    loadedKeyRef.current = key;

    let cancelled = false;
    setUrl(null);
    setSourceInfo(null);
    setNotFound(null);
    setLoadError(false);
    setNumPages(null);
    setZoom(1);
    setShowTree(false);
    setJumpPage(target.page);
    setVisiblePage(target.page);

    const load: Promise<PreviewInfo> = target.kind === "pdf"
      ? getPreviewInfo(target.documentId)
      : getDiagramPreview(target.documentId, target.page).then((u) => ({ url: u }));
    load
      .then((info) => {
        if (cancelled) return;
        if (info.url) setUrl(info.url);
        else if (info.source?.available) setSourceInfo(info);
        else if (info.missing) setNotFound(info.message ?? "This file was deleted from storage or could not be found.");
        else setSourceInfo(info);
      })
      .catch(() => {
        if (!cancelled) setLoadError(true);
      });
    return () => {
      cancelled = true;
    };
  }, [target]);

  useEffect(() => {
    if (!target) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.document.addEventListener("keydown", onKeyDown);
    return () => window.document.removeEventListener("keydown", onKeyDown);
  }, [target, onClose]);

  const onPointerDown = useCallback(
    (event: React.PointerEvent) => {
      event.preventDefault();
      (event.currentTarget as HTMLElement).setPointerCapture?.(event.pointerId);
      dragStart.current = { x: event.clientX, width };
      setDragging(true);
    },
    [width],
  );

  useEffect(() => {
    if (!dragging) return;

    const onMove = (event: PointerEvent) => {
      if (!dragStart.current) return;
      setWidth(clampWidth(dragStart.current.width + (dragStart.current.x - event.clientX)));
    };

    const onUp = () => {
      setDragging(false);
      dragStart.current = null;
      setWidth((current) => {
        window.localStorage.setItem(WIDTH_KEY, String(current));
        return current;
      });
    };

    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
    window.addEventListener("pointercancel", onUp);
    window.document.body.style.cursor = "col-resize";
    window.document.body.style.userSelect = "none";
    return () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
      window.removeEventListener("pointercancel", onUp);
      window.document.body.style.cursor = "";
      window.document.body.style.userSelect = "";
    };
  }, [dragging]);

  if (!target) return null;

  return (
    <>
      {!isDesktop && (
        <button
          type="button"
          className="fixed inset-0 z-40 bg-black/65 backdrop-blur-sm"
          aria-label="Close preview"
          onClick={onClose}
        />
      )}
      <aside
        className="animate-fade-up fixed inset-y-0 right-0 z-50 flex flex-col border-l border-border bg-surface-raised shadow-2xl lg:static lg:z-auto lg:shrink-0 lg:shadow-none"
        style={{ width: isDesktop ? width : "100%", maxWidth: isDesktop ? undefined : "32rem" }}
        aria-label="Document preview"
      >
        <div className="relative flex h-full min-w-0 flex-1 flex-col">
          <div
            role="separator"
            aria-orientation="vertical"
            aria-valuenow={width}
            aria-valuemin={MIN_W}
            aria-valuemax={MAX_W}
            aria-label="Resize document preview"
            onPointerDown={onPointerDown}
            className={`absolute inset-y-0 -left-1.5 z-20 hidden w-3 cursor-col-resize touch-none items-center justify-center lg:flex ${dragging ? "bg-foreground/10" : "hover:bg-foreground/5"}`}
          >
            <span className={`h-14 w-1 rounded-full transition ${dragging ? "bg-foreground" : "bg-border-strong"}`} />
          </div>

          <button
            type="button"
            aria-label="Resize from top corner"
            onPointerDown={onPointerDown}
            className="absolute left-0 top-0 z-30 hidden h-6 w-6 cursor-col-resize touch-none items-start justify-start p-1 lg:flex"
          >
            <span className="h-2.5 w-2.5 rounded-[2px] border-l-2 border-t-2 border-foreground/60" />
          </button>
          <button
            type="button"
            aria-label="Resize from bottom corner"
            onPointerDown={onPointerDown}
            className="absolute bottom-0 left-0 z-30 hidden h-6 w-6 cursor-col-resize touch-none items-end justify-start p-1 lg:flex"
          >
            <span className="h-2.5 w-2.5 rounded-[2px] border-b-2 border-l-2 border-foreground/60" />
          </button>

          <div className="flex min-h-16 items-center justify-between gap-3 border-b border-border bg-surface-raised px-4 py-3">
            <div className="flex min-w-0 items-center gap-3">
              <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-lg bg-accent text-accent-fg">
                {target.kind === "pdf" ? <FileText className="h-4 w-4" /> : <ImageIcon className="h-4 w-4" />}
              </div>
              <div className="min-w-0">
                <div className="truncate text-xs font-semibold text-foreground" title={target.label}>{target.label}</div>
                <div className="mt-1 flex items-center gap-1.5 text-[9px] font-medium uppercase tracking-[0.12em] text-muted">
                  <span className="h-1 w-1 rounded-full bg-foreground" />
                  {target.kind === "pdf" ? `Document · page ${visiblePage}` : `Diagram · page ${target.page}`}
                </div>
              </div>
            </div>
            <div className="flex shrink-0 items-center gap-1">
              {target.kind === "pdf" && !sourceInfo && (
                <button
                  type="button"
                  onClick={() => setShowTree((current) => !current)}
                  aria-pressed={showTree}
                  className={showTree
                    ? "inline-flex items-center gap-1.5 rounded-lg bg-accent px-2.5 py-1.5 text-[10px] font-semibold text-accent-fg"
                    : "inline-flex items-center gap-1.5 rounded-lg px-2.5 py-1.5 text-[10px] font-medium text-muted transition hover:bg-surface hover:text-foreground"}
                >
                  <Network className="h-3.5 w-3.5" />
                  Structure
                </button>
              )}
              <button
                type="button"
                onClick={onClose}
                className="rounded-lg p-2 text-muted transition hover:bg-surface hover:text-foreground"
                aria-label="Close preview"
              >
                <X className="h-4 w-4" />
              </button>
            </div>
          </div>

          <div className="min-h-0 flex-1 overflow-hidden bg-surface">
            {!url && !sourceInfo && !notFound && !loadError && (
              <div className="flex h-full flex-col items-center justify-center gap-3 p-6 text-xs text-muted">
                <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-surface-raised text-foreground shadow-soft">
                  <Loader2 className="h-4 w-4 animate-spin" />
                </div>
                Preparing preview…
              </div>
            )}
            {notFound && (
              <div className="flex h-full flex-col items-center justify-center px-6 text-center">
                <div className="mb-3 flex h-10 w-10 items-center justify-center rounded-lg bg-warning-soft text-warning"><FileText className="h-4 w-4" /></div>
                <p className="text-xs font-semibold text-foreground">File not found</p>
                <p className="mt-1 text-[10px] leading-4 text-muted">{notFound}</p>
              </div>
            )}
            {loadError && (
              <div className="flex h-full flex-col items-center justify-center px-6 text-center">
                <div className="mb-3 flex h-10 w-10 items-center justify-center rounded-lg bg-danger-soft text-danger"><X className="h-4 w-4" /></div>
                <p className="text-xs font-semibold text-foreground">Preview unavailable</p>
                <p className="mt-1 text-[10px] leading-4 text-muted">The source may still be processing or its preview could not be loaded.</p>
              </div>
            )}
            {sourceInfo && target.kind === "pdf" && (
              <SourceOnlyView documentId={target.documentId} page={jumpPage} onPage={(p) => { setJumpPage(p); setVisiblePage(p); }} info={sourceInfo} />
            )}
            {url && target.kind === "pdf" && showTree && (
              <div className="h-full overflow-y-auto bg-surface-raised">
                <TreeView
                  documentId={target.documentId}
                  onJump={(page) => {
                    setShowTree(false);
                    setJumpPage(page);
                    setVisiblePage(page);
                  }}
                />
              </div>
            )}
            {url && target.kind === "pdf" && !showTree && (
              <PdfPreview url={url} page={jumpPage} zoom={zoom} onLoad={setNumPages} onVisiblePage={setVisiblePage} />
            )}
            {url && target.kind === "diagram" && <ImagePreview url={url} caption={target.label} />}
          </div>

          {url && target.kind === "pdf" && (
            <div className="flex items-center justify-between border-t border-border bg-surface-raised px-4 py-2.5 text-[10px] text-muted">
              <span>
                Page <strong className="font-semibold text-foreground">{visiblePage}</strong>{numPages ? ` of ${numPages}` : ""}
                <span className="ml-2 hidden opacity-70 sm:inline">Scroll to navigate</span>
              </span>
              <div className="flex items-center gap-1 rounded-lg border border-border bg-surface p-1">
                <button
                  type="button"
                  onClick={() => setZoom((current) => Math.max(0.5, current - 0.1))}
                  className="rounded-md p-1 text-muted transition hover:bg-surface-raised hover:text-foreground"
                  aria-label="Zoom out"
                >
                  <ZoomOut className="h-3.5 w-3.5" />
                </button>
                <span className="w-9 text-center text-[9px] font-semibold text-foreground/75">{Math.round(zoom * 100)}%</span>
                <button
                  type="button"
                  onClick={() => setZoom((current) => Math.min(2, current + 0.1))}
                  className="rounded-md p-1 text-muted transition hover:bg-surface-raised hover:text-foreground"
                  aria-label="Zoom in"
                >
                  <ZoomIn className="h-3.5 w-3.5" />
                </button>
              </div>
            </div>
          )}
        </div>
      </aside>
    </>
  );
}
