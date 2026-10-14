"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import clsx from "clsx";
import { CheckCircle2, FileUp, Loader2, TriangleAlert } from "lucide-react";
import { fetchDocuments, streamProgress, uploadDocument } from "@/lib/api";
import type { ProgressEvent } from "@/lib/types";
import { ACCEPT_ATTRIBUTE, SUPPORTED_EXTENSIONS, extensionOf } from "@/lib/fileTypes";

interface UploadItem {
  id: string;
  name: string;
  documentId?: string;
  phase: ProgressEvent["phase"] | "uploading" | "pending" | "queued";
  page?: number;
  total?: number;
  caption?: string | null;
  error?: string;
}

interface Props {
  onIndexed: () => void;
  folderId?: string | null;
}

// Batches up to this size upload one at a time with live per-file progress (the original
// behaviour). Larger batches switch to bulk mode: several uploads in flight, and progress
// from one shared status poll instead of one open stream per file.
const BULK_THRESHOLD = 6;
const BULK_UPLOAD_PARALLELISM = 3;
const BULK_POLL_MS = 3000;
// Keep the list light for very large batches: finished files collapse into the counter.
const HIDE_FINISHED_ABOVE = 30;

const isFinished = (phase: UploadItem["phase"]) => phase === "indexed" || phase === "failed";

const PHASE_LABEL: Record<string, string> = {
  pending: "Waiting to upload",
  queued: "Waiting to be indexed",
  uploading: "Uploading",
  extracting: "Extracting content",
  ocr: "Reading visual pages",
  embedding: "Embedding diagrams",
  indexing: "Building search index",
  indexed: "Ready to search",
  failed: "Upload failed",
};

function progressFor(item: UploadItem): number {
  if (item.phase === "pending") return 4;
  if (item.phase === "uploading") return 12;
  if (item.phase === "queued") return 18;
  if (item.phase === "extracting") return 28;
  if (item.phase === "ocr") {
    if (item.page && item.total) return 32 + Math.round((item.page / item.total) * 34);
    return 48;
  }
  if (item.phase === "embedding") return 74;
  if (item.phase === "indexing") return 88;
  if (item.phase === "indexed") return 100;
  return 0;
}

export function FileDropzone({ onIndexed, folderId }: Props) {
  const [items, setItems] = useState<UploadItem[]>([]);
  const [dragging, setDragging] = useState(false);
  const [skipped, setSkipped] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);
  const itemsRef = useRef<UploadItem[]>([]);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  useEffect(() => {
    itemsRef.current = items;
  }, [items]);

  useEffect(
    () => () => {
      if (pollRef.current) clearInterval(pollRef.current);
    },
    [],
  );

  const updateItem = (id: string, update: Partial<UploadItem>) => {
    setItems((previous) => previous.map((item) => (item.id === id ? { ...item, ...update } : item)));
  };

  // One poll for the whole batch: maps each document's server-side status onto its row.
  const startStatusPoll = useCallback(() => {
    if (pollRef.current) return;
    pollRef.current = setInterval(async () => {
      const waiting = itemsRef.current.filter((item) => item.documentId && !isFinished(item.phase));
      const stillUploading = itemsRef.current.some((item) => item.phase === "pending" || item.phase === "uploading");
      if (waiting.length === 0 && !stillUploading) {
        if (pollRef.current) clearInterval(pollRef.current);
        pollRef.current = null;
        return;
      }
      try {
        const documents = await fetchDocuments();
        const byId = new Map(documents.map((doc) => [doc.document_id, doc]));
        let newlyIndexed = false;
        setItems((previous) =>
          previous.map((item) => {
            const doc = item.documentId ? byId.get(item.documentId) : undefined;
            if (!doc || isFinished(item.phase)) return item;
            const phase = doc.status as UploadItem["phase"];
            if (phase === item.phase) return item;
            if (phase === "indexed") newlyIndexed = true;
            return { ...item, phase, error: phase === "failed" ? doc.error ?? "Indexing failed" : undefined };
          }),
        );
        if (newlyIndexed) onIndexed();
      } catch {
        // transient network/auth error: try again on the next tick
      }
    }, BULK_POLL_MS);
  }, [onIndexed]);

  const runBulk = useCallback(
    async (all: File[]) => {
      const files = all.filter((file) => SUPPORTED_EXTENSIONS.has(extensionOf(file.name)));
      setSkipped((count) => count + (all.length - files.length));
      if (files.length === 0) return;
      const entries: { id: string; file: File }[] = files.map((file) => ({ id: crypto.randomUUID(), file }));
      setItems((previous) => [
        ...previous,
        ...entries.map(({ id, file }): UploadItem => ({ id, name: file.name, phase: "pending" })),
      ]);
      startStatusPoll();

      let next = 0;
      const worker = async () => {
        while (next < entries.length) {
          const { id, file } = entries[next++];
          updateItem(id, { phase: "uploading" });
          try {
            const { document_id, status } = await uploadDocument(file, folderId);
            const phase = (status || "queued") as UploadItem["phase"];
            updateItem(id, { documentId: document_id, phase, error: undefined });
            if (phase === "indexed") onIndexed();
          } catch (error) {
            updateItem(id, {
              phase: "failed",
              error: error instanceof Error && error.message ? error.message : "The file could not be uploaded.",
            });
          }
        }
      };
      await Promise.all(Array.from({ length: BULK_UPLOAD_PARALLELISM }, worker));
    },
    [folderId, onIndexed, startStatusPoll],
  );

  const handleFiles = useCallback(
    async (files: FileList) => {
      if (files.length >= BULK_THRESHOLD) {
        await runBulk(Array.from(files));
        return;
      }
      for (const file of Array.from(files)) {
        const id = crypto.randomUUID();
        setItems((previous) => [...previous, { id, name: file.name, phase: "uploading" }]);
        try {
          const { document_id } = await uploadDocument(file, folderId);
          updateItem(id, { documentId: document_id, phase: "extracting" });

          await streamProgress(document_id, (event) => {
            updateItem(id, {
              phase: event.phase,
              page: event.page,
              total: event.total,
              caption: event.caption,
              error: event.error,
            });
            if (event.phase === "indexed") onIndexed();
          });
        } catch {
          updateItem(id, { phase: "failed", error: "The file could not be uploaded. Please try again." });
        }
      }
    },
    [onIndexed, folderId, runBulk],
  );

  const finishedCount = items.filter((item) => item.phase === "indexed").length;
  const failedCount = items.filter((item) => item.phase === "failed").length;
  const bulk = items.length >= BULK_THRESHOLD;
  const visibleItems = items.length > HIDE_FINISHED_ABOVE ? items.filter((item) => item.phase !== "indexed") : items;

  return (
    <div>
      <div
        role="button"
        tabIndex={0}
        onDragOver={(event) => {
          event.preventDefault();
          setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={(event) => {
          event.preventDefault();
          setDragging(false);
          if (event.dataTransfer.files.length) void handleFiles(event.dataTransfer.files);
        }}
        onClick={() => inputRef.current?.click()}
        onKeyDown={(event) => {
          if (event.key === "Enter" || event.key === " ") inputRef.current?.click();
        }}
        className={clsx(
          "group cursor-pointer rounded-xl border border-dashed px-5 py-10 text-center transition sm:px-10 sm:py-12",
          dragging
            ? "border-foreground bg-surface ring-4 ring-border"
            : "border-border-strong bg-surface hover:border-foreground",
        )}
      >
        <div className={clsx("mx-auto mb-4 flex h-14 w-14 items-center justify-center rounded-2xl transition", dragging ? "scale-105 bg-foreground text-accent-fg" : "bg-background text-foreground shadow-soft ring-1 ring-border")}>
          <FileUp className="h-6 w-6" strokeWidth={1.7} />
        </div>
        <p className="text-sm font-semibold text-foreground">{dragging ? "Drop files to begin" : "Drop documents here"}</p>
        <p className="mt-1.5 text-xs leading-5 text-muted">or click to choose files from your computer</p>
        <div className="mx-auto mt-4 flex max-w-md flex-wrap justify-center gap-1.5">
          {["PDF", "DOCX", "CSV", "XLSX", "EML", "TXT", "JSON", "XER"].map((type) => (
            <span key={type} className="rounded-md border border-border bg-surface-raised px-1.5 py-0.5 text-[9px] font-semibold text-muted">{type}</span>
          ))}
        </div>
        <input
          ref={inputRef}
          type="file"
          accept={ACCEPT_ATTRIBUTE}
          multiple
          hidden
          onChange={(event) => {
            const files = event.target.files;
            if (files) void handleFiles(files);
            event.currentTarget.value = "";
          }}
        />
      </div>

      {items.length > 0 && (
        <div className="mt-5">
          <div className="mb-2 flex items-center justify-between px-1">
            <span className="text-[10px] font-semibold uppercase tracking-[0.14em] text-muted">Upload queue</span>
            <span className="text-[10px] text-muted">
              {bulk
                ? `${finishedCount} of ${items.length} ready${failedCount ? ` · ${failedCount} failed` : ""}`
                : `${items.length} file${items.length === 1 ? "" : "s"}`}
            </span>
          </div>
          {skipped > 0 && (
            <p className="mb-2 px-1 text-[10px] text-muted">
              Skipped {skipped} file{skipped === 1 ? "" : "s"} that aren&apos;t a supported type.
            </p>
          )}
          {items.length > HIDE_FINISHED_ABOVE && finishedCount > 0 && (
            <p className="mb-2 px-1 text-[10px] text-muted">{finishedCount} finished file{finishedCount === 1 ? "" : "s"} hidden from this list.</p>
          )}
          <ul className="space-y-2">
            {visibleItems.map((item) => {
              const progress = progressFor(item);
              return (
                <li key={item.id} className="rounded-xl border border-border bg-surface-raised p-3.5 shadow-soft">
                  <div className="flex items-start gap-3">
                    <div
                      className={clsx(
                        "mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-lg",
                        item.phase === "indexed" && "bg-success-soft text-success",
                        item.phase === "failed" && "bg-danger-soft text-danger",
                        item.phase !== "indexed" && item.phase !== "failed" && "bg-surface text-foreground",
                      )}
                    >
                      {item.phase === "indexed" ? (
                        <CheckCircle2 className="h-4 w-4" />
                      ) : item.phase === "failed" ? (
                        <TriangleAlert className="h-4 w-4" />
                      ) : (
                        <Loader2 className="h-4 w-4 animate-spin" />
                      )}
                    </div>
                    <div className="min-w-0 flex-1">
                      <div className="flex items-center justify-between gap-3">
                        <span className="truncate text-xs font-semibold text-foreground/80">{item.name}</span>
                        <span
                          className={clsx(
                            "shrink-0 text-[10px] font-medium",
                            item.phase === "failed" && "text-danger",
                            item.phase === "indexed" && "text-success",
                            item.phase !== "failed" && item.phase !== "indexed" && "text-foreground",
                          )}
                        >
                          {PHASE_LABEL[item.phase] ?? item.phase}
                        </span>
                      </div>
                      {item.phase !== "failed" && (
                        <div className="mt-2.5 h-1.5 overflow-hidden rounded-full bg-surface">
                          <div
                            className={clsx("h-full rounded-full transition-all duration-500", item.phase === "indexed" ? "bg-success" : "bg-accent")}
                            style={{ width: `${progress}%` }}
                          />
                        </div>
                      )}
                      {item.phase === "ocr" && item.total ? (
                        <div className="mt-2 truncate text-[10px] text-muted">
                          Page {item.page ?? 0} of {item.total}{item.caption ? ` · ${item.caption}` : ""}
                        </div>
                      ) : null}
                      {item.error && <div className="mt-2 text-[10px] text-danger">{item.error}</div>}
                    </div>
                  </div>
                </li>
              );
            })}
          </ul>
        </div>
      )}
    </div>
  );
}
