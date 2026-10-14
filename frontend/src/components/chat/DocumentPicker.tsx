"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import clsx from "clsx";
import { Check, ChevronDown, FileText, Folder as FolderIcon, Loader2, Search, Upload } from "lucide-react";
import { streamProgress, uploadDocument } from "@/lib/api";
import { DocIcon } from "@/lib/docIcon";
import { UNFILED_FOLDER_ID } from "@/lib/folders";
import type { DocumentOut, Folder } from "@/lib/types";
import { ACCEPT_ATTRIBUTE } from "@/lib/fileTypes";

interface Props {
  documents: DocumentOut[];
  folders: Folder[];
  selectedIds: string[] | null;
  onChange: (ids: string[] | null) => void;
  onUploaded: () => void;
  /** Regular users get a read-only picker: no upload button. */
  canUpload?: boolean;
}

const STATUS_LABEL: Record<string, string> = {
  queued: "Queued",
  extracting: "Extracting",
  ocr: "Running OCR",
  embedding: "Embedding",
  indexing: "Indexing",
  indexed: "Ready",
  failed: "Failed",
};

export function DocumentPicker({ documents, folders, selectedIds, onChange, onUploaded, canUpload = true }: Props) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [uploading, setUploading] = useState<{ name: string; phase: string }[]>([]);
  const inputRef = useRef<HTMLInputElement>(null);
  const rootRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onClickOutside = (event: MouseEvent) => {
      if (rootRef.current && !rootRef.current.contains(event.target as Node)) setOpen(false);
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", onClickOutside);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("mousedown", onClickOutside);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [open]);

  const readyIds = useMemo(
    () => documents.filter((document) => document.status === "indexed").map((document) => document.document_id),
    [documents],
  );

  // null = every ready doc (default/"all"). An array -- including [] -- is an
  // explicit choice: [] means "nothing picked yet" (e.g. mid-way through
  // Deselect all -> pick one), distinct from null/"all". Only null falls back
  // to readyIds; an explicit empty selection stays empty so "Deselect all"
  // actually shows 0 checked instead of silently reverting to "all".
  const effectiveIds = useMemo(() => (selectedIds == null ? readyIds : selectedIds), [selectedIds, readyIds]);

  const allSelected =
    selectedIds == null ||
    (readyIds.length > 0 && readyIds.every((id) => effectiveIds.includes(id)) && effectiveIds.length === readyIds.length);

  const selected = useMemo(
    () => documents.filter((document) => effectiveIds.includes(document.document_id)),
    [documents, effectiveIds],
  );

  const filtered = useMemo(
    () => documents.filter((document) => document.filename.toLowerCase().includes(query.trim().toLowerCase())),
    [documents, query],
  );

  const readyCount = readyIds.length;

  const commit = (ids: string[]) => {
    // Collapse "every ready doc individually picked" down to the canonical
    // "all" (null) -- but NOT an empty selection, which is its own explicit
    // state (see effectiveIds above), not a synonym for "all".
    if (readyIds.length > 0 && ids.length === readyIds.length && readyIds.every((id) => ids.includes(id))) {
      onChange(null);
      return;
    }
    onChange(ids);
  };

  const selectFolder = (folderId: string) => {
    const ids =
      folderId === UNFILED_FOLDER_ID
        ? documents
            .filter((document) => document.status === "indexed" && document.folder_ids.length === 0)
            .map((document) => document.document_id)
        : documents
            .filter((document) => document.status === "indexed" && document.folder_ids.includes(folderId))
            .map((document) => document.document_id);
    commit(ids);
  };

  const toggle = (document: DocumentOut) => {
    if (document.status !== "indexed") return;
    const next = effectiveIds.includes(document.document_id)
      ? effectiveIds.filter((id) => id !== document.document_id)
      : [...effectiveIds, document.document_id];
    commit(next);
  };

  const handleUpload = async (files: FileList) => {
    const uploadSelection = new Set(allSelected ? readyIds : (selectedIds ?? []));
    for (const file of Array.from(files)) {
      setUploading((previous) => [...previous, { name: file.name, phase: "uploading" }]);
      try {
        const { document_id } = await uploadDocument(file);
        await streamProgress(document_id, (event) => {
          setUploading((previous) =>
            previous.map((item) => (item.name === file.name ? { ...item, phase: event.phase } : item)),
          );
          if (event.phase === "indexed") {
            uploadSelection.add(document_id);
            onUploaded();
            onChange(allSelected ? null : [...uploadSelection]);
            setUploading((previous) => previous.filter((item) => item.name !== file.name));
          }
        });
      } catch {
        setUploading((previous) =>
          previous.map((item) => (item.name === file.name ? { ...item, phase: "failed" } : item)),
        );
      }
    }
  };

  return (
    <div className="flex flex-col gap-2">
      <div ref={rootRef} className="relative w-fit">
        <button
          type="button"
          onClick={() => setOpen((current) => !current)}
          aria-expanded={open}
          className={clsx(
            "inline-flex items-center gap-1.5 rounded-lg border px-2.5 py-1.5 text-[11px] font-semibold transition",
            open ? "border-border-strong bg-surface text-foreground" : "border-border bg-surface-raised text-muted hover:border-border-strong hover:text-foreground",
          )}
        >
          <FileText className="h-3.5 w-3.5" strokeWidth={1.8} />
          {allSelected ? `All documents (${readyCount})` : `${selected.length} selected`}
          <ChevronDown className={clsx("h-3 w-3 transition", open && "rotate-180")} />
        </button>

        {open && (
          <div className="animate-fade-up absolute bottom-full left-0 z-30 mb-2 w-[min(calc(100vw-2rem),22rem)] overflow-hidden rounded-xl border border-border-strong bg-surface-raised shadow-panel">
            <div className="border-b border-border px-3 pb-2.5 pt-3">
              <div className="mb-2.5 flex items-start justify-between gap-3">
                <div>
                  <div className="text-xs font-semibold text-foreground">Sources</div>
                  <div className="mt-0.5 text-[10px] text-muted">Pick files, or select/deselect all.</div>
                </div>
                <div className="flex shrink-0 items-center gap-2">
                  {!allSelected && (
                    <button
                      type="button"
                      onClick={() => onChange(null)}
                      className="text-[10px] font-semibold text-foreground hover:underline"
                    >
                      Select all
                    </button>
                  )}
                  {selected.length > 0 && (
                    <button
                      type="button"
                      onClick={() => onChange([])}
                      className="text-[10px] font-semibold text-muted hover:text-foreground hover:underline"
                    >
                      Deselect all
                    </button>
                  )}
                </div>
              </div>
              <div className="flex items-center gap-2 rounded-lg border border-border bg-background px-3 py-2">
                <Search className="h-3.5 w-3.5 shrink-0 text-muted" />
                <input
                  autoFocus
                  value={query}
                  onChange={(event) => setQuery(event.target.value)}
                  placeholder="Search documents"
                  className="w-full bg-transparent text-xs text-foreground outline-none placeholder:text-muted"
                />
              </div>
              <div className="mt-2 flex flex-wrap gap-1.5">
                  <button
                    type="button"
                    onClick={() => selectFolder(UNFILED_FOLDER_ID)}
                    title='Search only "Unfiled"'
                    className="inline-flex items-center gap-1 rounded-md border border-border bg-background px-2 py-1 text-[10px] font-medium text-foreground transition hover:border-border-strong hover:bg-surface"
                  >
                    <FolderIcon className="h-3 w-3 text-muted" />
                    Unfiled
                  </button>
                  {folders.map((folder) => (
                    <button
                      key={folder.folder_id}
                      type="button"
                      onClick={() => selectFolder(folder.folder_id)}
                      title={`Search only "${folder.name}"`}
                      className="inline-flex items-center gap-1 rounded-md border border-border bg-background px-2 py-1 text-[10px] font-medium text-foreground transition hover:border-border-strong hover:bg-surface"
                    >
                      <FolderIcon className="h-3 w-3 text-muted" />
                      {folder.name}
                    </button>
                  ))}
                </div>
            </div>

            <div className="max-h-60 overflow-y-auto p-1.5">
              {filtered.length === 0 && (
                <div className="px-3 py-8 text-center">
                  <Search className="mx-auto mb-2 h-5 w-5 text-muted" />
                  <p className="text-xs font-medium text-muted">No matching documents</p>
                  <p className="mt-1 text-[10px] text-muted">{canUpload ? "Try another filename or upload a new source." : "Try another filename."}</p>
                </div>
              )}
              {filtered.map((document) => {
                const checked = document.status === "indexed" && effectiveIds.includes(document.document_id);
                const selectable = document.status === "indexed";
                return (
                  <button
                    key={document.document_id}
                    type="button"
                    onClick={() => toggle(document)}
                    disabled={!selectable}
                    className={clsx(
                      "flex w-full items-center gap-2.5 rounded-lg px-2.5 py-2 text-left transition",
                      checked ? "bg-surface" : "hover:bg-surface",
                      !selectable && "cursor-not-allowed opacity-65",
                    )}
                  >
                    <span
                      className={clsx(
                        "flex h-4 w-4 shrink-0 items-center justify-center rounded-[5px] border transition",
                        checked ? "border-foreground bg-foreground text-accent-fg" : "border-border-strong bg-background",
                      )}
                    >
                      {checked && <Check className="h-3 w-3" strokeWidth={3} />}
                    </span>
                    <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-surface text-muted">
                      <DocIcon docType={document.doc_type} className="h-3.5 w-3.5" />
                    </span>
                    <span className="min-w-0 flex-1">
                      <span className="block truncate text-xs font-medium text-foreground">{document.filename}</span>
                      <span
                        className={clsx(
                          "mt-0.5 flex items-center gap-1 text-[9px]",
                          document.status === "failed"
                            ? "text-danger"
                            : document.status === "indexed"
                              ? "text-success"
                              : "text-muted",
                        )}
                      >
                        <span className="h-1 w-1 rounded-full bg-current" />
                        {STATUS_LABEL[document.status] ?? document.status}
                      </span>
                    </span>
                  </button>
                );
              })}
            </div>

            {canUpload && uploading.length > 0 && (
              <div className="space-y-1.5 border-t border-border bg-surface/50 p-2.5">
                {uploading.map((item) => (
                  <div key={item.name} className="flex items-center gap-2 rounded-lg bg-background px-2.5 py-2 text-[10px] text-muted">
                    {item.phase !== "failed" && <Loader2 className="h-3 w-3 shrink-0 animate-spin text-foreground" />}
                    <span className="min-w-0 flex-1 truncate text-muted">{item.name}</span>
                    <span className={item.phase === "failed" ? "text-danger" : ""}>{STATUS_LABEL[item.phase] ?? item.phase}</span>
                  </div>
                ))}
              </div>
            )}

            <div className="flex items-center justify-between border-t border-border bg-surface/50 p-2">
              <span className="px-1 text-[10px] text-muted">{readyCount} ready to search</span>
              {canUpload && (
                <button
                  type="button"
                  onClick={() => inputRef.current?.click()}
                  className="inline-flex items-center gap-1.5 rounded-lg bg-accent px-2.5 py-1.5 text-[10px] font-semibold text-accent-fg shadow-soft transition hover:opacity-90"
                >
                  <Upload className="h-3 w-3" />
                  Upload source
                </button>
              )}
            </div>
            {canUpload && (
              <input
                ref={inputRef}
                type="file"
                accept={ACCEPT_ATTRIBUTE}
                multiple
                hidden
                onChange={(event) => {
                  const files = event.target.files;
                  if (files) void handleUpload(files);
                  event.currentTarget.value = "";
                }}
              />
            )}
          </div>
        )}
      </div>
    </div>
  );
}
