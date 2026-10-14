"use client";

import { useMemo, useState } from "react";
import { AnimatePresence } from "framer-motion";
import clsx from "clsx";
import {
  ChevronLeft,
  CheckSquare,
  FileText,
  Folder as FolderIcon,
  Grid2X2,
  LayoutList,
  Loader2,
  MessageSquare,
  Plus,
  Search,
  Square,
  Trash2,
  Upload,
  X,
} from "lucide-react";
import { createFolder, deleteDocument, setDocumentFolders } from "@/lib/api";
import { DocIcon } from "@/lib/docIcon";
import { isUnfiledFolder, UNFILED_FOLDER_ID } from "@/lib/folders";
import type { DocumentOut, Folder } from "@/lib/types";
import { DocumentInspector } from "@/components/documents/DocumentInspector";
import { FolderPickerMenu } from "@/components/documents/FolderPickerMenu";
import { UploadOverlay } from "@/components/upload/UploadOverlay";

interface Props {
  documents: DocumentOut[];
  folders: Folder[];
  activeFolderId: string | null;
  onIndexed: () => void;
  onFoldersChanged: () => void;
  onOpen: (documentId: string) => void;
  onSelectFolder: (folderId: string) => void;
  onSelectAll: () => void;
  onDeleteFolder: (folderId: string) => Promise<void>;
  onChatWithFolders: (folderIds: string[]) => void;
  /** Regular users browse only — no upload / rename / delete / move. */
  readOnly?: boolean;
}

function formatDate(iso: string | null): string {
  if (!iso) return "";
  return new Date(iso).toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
}

const STATUS_LABEL: Record<string, string> = {
  queued: "Queued",
  extracting: "Extracting",
  ocr: "OCR",
  indexing: "Indexing",
  indexed: "Ready",
  failed: "Failed",
};

function StatusBadge({ status }: { status: DocumentOut["status"] }) {
  return (
    <span
      className={clsx(
        "inline-flex items-center gap-1.5 text-xs",
        status === "indexed" && "text-success",
        status === "failed" && "text-danger",
        !["indexed", "failed"].includes(status) && "text-muted",
      )}
    >
      <span
        className={clsx(
          "h-1.5 w-1.5 rounded-full",
          status === "indexed" && "bg-success",
          status === "failed" && "bg-danger",
          !["indexed", "failed"].includes(status) && "animate-pulse bg-muted",
        )}
      />
      {STATUS_LABEL[status] ?? status}
    </span>
  );
}

function FolderChips({ folderIds, folders }: { folderIds: string[]; folders: Folder[] }) {
  if (folderIds.length === 0) return null;
  const names = folderIds.map((id) => folders.find((f) => f.folder_id === id)?.name).filter(Boolean) as string[];
  return (
    <div className="mt-1 flex flex-wrap gap-1">
      {names.map((name) => (
        <span key={name} className="rounded-md bg-surface px-1.5 py-0.5 text-[10px] text-muted">
          {name}
        </span>
      ))}
    </div>
  );
}

export function DocumentsView({
  documents,
  folders,
  activeFolderId,
  onIndexed,
  onFoldersChanged,
  onOpen,
  onSelectFolder,
  onSelectAll,
  onDeleteFolder,
  onChatWithFolders,
  readOnly = false,
}: Props) {
  const [query, setQuery] = useState("");
  const [view, setView] = useState<"list" | "grid">("list");
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [uploadOpen, setUploadOpen] = useState(false);
  const [inspectDoc, setInspectDoc] = useState<DocumentOut | null>(null);
  // Read-only users get the same window with just the PDF preview: the index/storage tabs
  // are admin-only on the server and are neither shown nor requested for them.
  const openDocument = (doc: DocumentOut) => setInspectDoc(doc);
  const [deleting, setDeleting] = useState(false);
  const [deletingDocId, setDeletingDocId] = useState<string | null>(null);
  const [applyingFolders, setApplyingFolders] = useState(false);
  const [selectedFolderIds, setSelectedFolderIds] = useState<Set<string>>(new Set());
  const [deletingFolders, setDeletingFolders] = useState(false);
  const [creatingFolder, setCreatingFolder] = useState(false);
  const [newFolderName, setNewFolderName] = useState("");

  const viewingUnfiled = isUnfiledFolder(activeFolderId);
  const activeFolder = viewingUnfiled
    ? null
    : activeFolderId
      ? folders.find((f) => f.folder_id === activeFolderId) ?? null
      : null;
  const unfiledCount = useMemo(() => documents.filter((d) => d.folder_ids.length === 0).length, [documents]);

  const scoped = useMemo(() => {
    if (viewingUnfiled) return documents.filter((d) => d.folder_ids.length === 0);
    if (activeFolderId) return documents.filter((d) => d.folder_ids.includes(activeFolderId));
    return documents;
  }, [documents, activeFolderId, viewingUnfiled]);

  const filtered = useMemo(
    () => scoped.filter((d) => d.filename.toLowerCase().includes(query.trim().toLowerCase())),
    [scoped, query],
  );

  const allVisibleSelected = filtered.length > 0 && filtered.every((d) => selected.has(d.document_id));

  const displayFolders = useMemo(
    () => [
      { folder_id: UNFILED_FOLDER_ID, name: "Unfiled", document_count: unfiledCount } as Folder,
      ...folders,
    ],
    [folders, unfiledCount],
  );

  const toggleAll = () => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (allVisibleSelected) filtered.forEach((d) => next.delete(d.document_id));
      else filtered.forEach((d) => next.add(d.document_id));
      return next;
    });
  };

  const toggleOne = (id: string) => {
    setSelected((prev) => {
      const next = new Set(prev);
      next.has(id) ? next.delete(id) : next.add(id);
      return next;
    });
  };

  const deleteSelected = async () => {
    if (selected.size === 0 || deleting) return;
    if (!window.confirm(`Delete ${selected.size} document${selected.size === 1 ? "" : "s"}?`)) return;
    setDeleting(true);
    try {
      await Promise.all([...selected].map((id) => deleteDocument(id)));
      setSelected(new Set());
      onIndexed();
    } finally {
      setDeleting(false);
    }
  };

  const handleDeleteDocument = async (doc: DocumentOut) => {
    if (deletingDocId) return;
    if (!window.confirm(`Delete "${doc.filename}"?`)) return;
    setDeletingDocId(doc.document_id);
    try {
      await deleteDocument(doc.document_id);
      setSelected((prev) => {
        if (!prev.has(doc.document_id)) return prev;
        const next = new Set(prev);
        next.delete(doc.document_id);
        return next;
      });
      onIndexed();
    } finally {
      setDeletingDocId(null);
    }
  };

  const selectedDocs = () => documents.filter((d) => selected.has(d.document_id));

  const moveSelectedTo = async (folderId: string) => {
    setApplyingFolders(true);
    try {
      await Promise.all(selectedDocs().map((d) => setDocumentFolders(d.document_id, [folderId])));
      setSelected(new Set());
      onIndexed();
      onFoldersChanged();
    } finally {
      setApplyingFolders(false);
    }
  };

  const copySelectedTo = async (folderId: string) => {
    setApplyingFolders(true);
    try {
      await Promise.all(
        selectedDocs().map((d) =>
          d.folder_ids.includes(folderId) ? Promise.resolve() : setDocumentFolders(d.document_id, [...d.folder_ids, folderId]),
        ),
      );
      setSelected(new Set());
      onIndexed();
      onFoldersChanged();
    } finally {
      setApplyingFolders(false);
    }
  };

  const handleCreateFolder = async (name: string): Promise<Folder> => {
    const folder = await createFolder(name);
    onFoldersChanged();
    return folder;
  };

  const submitNewFolder = async () => {
    const name = newFolderName.trim();
    if (!name) {
      setCreatingFolder(false);
      setNewFolderName("");
      return;
    }
    await handleCreateFolder(name);
    setNewFolderName("");
    setCreatingFolder(false);
  };

  const toggleFolderSelection = (id: string) => {
    setSelectedFolderIds((prev) => {
      const next = new Set(prev);
      next.has(id) ? next.delete(id) : next.add(id);
      return next;
    });
  };

  const handleDeleteFolderCard = async (folder: Folder) => {
    if (isUnfiledFolder(folder.folder_id)) return;
    if (!window.confirm(`Delete "${folder.name}"? Documents inside stay, they just leave this folder.`)) return;
    await onDeleteFolder(folder.folder_id);
    setSelectedFolderIds((prev) => {
      if (!prev.has(folder.folder_id)) return prev;
      const next = new Set(prev);
      next.delete(folder.folder_id);
      return next;
    });
  };

  const handleBulkDeleteFolders = async () => {
    const ids = [...selectedFolderIds].filter((id) => !isUnfiledFolder(id));
    if (ids.length === 0 || deletingFolders) return;
    if (
      !window.confirm(
        `Delete ${ids.length} folder${ids.length === 1 ? "" : "s"}? Documents inside stay, they just leave these folders.`,
      )
    )
      return;
    setDeletingFolders(true);
    try {
      await Promise.all(ids.map((id) => onDeleteFolder(id)));
      setSelectedFolderIds(new Set());
    } finally {
      setDeletingFolders(false);
    }
  };

  const handleOpenChatWithFolders = () => {
    if (selectedFolderIds.size === 0) return;
    onChatWithFolders([...selectedFolderIds]);
    setSelectedFolderIds(new Set());
  };

  const atRoot = !activeFolderId;
  const showFileList = !atRoot || Boolean(query);

  return (
    <div className="flex h-full min-h-0 flex-col overflow-hidden bg-background">
      <div className="min-h-0 flex-1 overflow-y-auto px-4 py-5 sm:px-6 sm:py-6">
        <div className="mx-auto max-w-6xl">
          {!atRoot && (
            <button
              type="button"
              onClick={onSelectAll}
              className="mb-3 inline-flex items-center gap-1 text-xs font-medium text-muted transition hover:text-foreground"
            >
              <ChevronLeft className="h-3.5 w-3.5" />
              All documents
            </button>
          )}

          <div className="mb-5 flex flex-col gap-2 sm:flex-row sm:items-center">
            <div className="flex min-w-0 flex-1 items-center gap-2 rounded-lg border border-border bg-surface-raised px-3 py-2.5 shadow-soft transition focus-within:border-accent focus-within:ring-4 focus-within:ring-accent-soft">
              <Search className="h-4 w-4 shrink-0 text-muted" />
              <input
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder="Search…"
                className="w-full bg-transparent text-sm outline-none placeholder:text-muted"
              />
            </div>
            <div className="flex flex-wrap items-center gap-2">
              {!readOnly && !atRoot && (
                <button
                  type="button"
                  onClick={() => setUploadOpen(true)}
                  className="inline-flex items-center gap-1.5 rounded-lg bg-accent px-2.5 py-1.5 text-xs font-semibold text-accent-fg transition hover:opacity-90"
                >
                  <Upload className="h-3.5 w-3.5" strokeWidth={1.75} />
                  Upload
                </button>
              )}
              {!readOnly && selected.size > 0 && (
                <>
                  <FolderPickerMenu
                    folders={folders}
                    label={applyingFolders ? "Moving…" : "Move to…"}
                    onSelect={(id) => void moveSelectedTo(id)}
                    onCreateFolder={handleCreateFolder}
                  />
                  <FolderPickerMenu
                    folders={folders}
                    label={applyingFolders ? "Copying…" : "Copy to…"}
                    onSelect={(id) => void copySelectedTo(id)}
                    onCreateFolder={handleCreateFolder}
                  />
                  <button
                    type="button"
                    onClick={deleteSelected}
                    disabled={deleting}
                    className="inline-flex items-center gap-1.5 rounded-lg border border-border px-3 py-2 text-xs font-semibold text-danger transition hover:bg-danger-soft disabled:opacity-60"
                  >
                    {deleting ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Trash2 className="h-3.5 w-3.5" />}
                    Delete {selected.size}
                  </button>
                </>
              )}
              <div className="flex rounded-lg border border-border bg-surface-raised p-0.5 shadow-soft">
                <button
                  type="button"
                  onClick={() => setView("list")}
                  className={clsx("rounded-md p-1.5", view === "list" ? "bg-surface text-foreground" : "text-muted")}
                  aria-label="List"
                >
                  <LayoutList className="h-4 w-4" />
                </button>
                <button
                  type="button"
                  onClick={() => setView("grid")}
                  className={clsx("rounded-md p-1.5", view === "grid" ? "bg-surface text-foreground" : "text-muted")}
                  aria-label="Grid"
                >
                  <Grid2X2 className="h-4 w-4" />
                </button>
              </div>
            </div>
          </div>

          {atRoot && !query && (
            <div className="mb-5">
              <div className="mb-2 flex items-center justify-between gap-2">
                <div className="text-xs font-medium uppercase tracking-wide text-muted">
                  {!readOnly && selectedFolderIds.size > 0 ? `${selectedFolderIds.size} selected` : "Folders"}
                </div>
                {!readOnly && (
                  <div className="flex items-center gap-2">
                    {selectedFolderIds.size > 0 ? (
                      <>
                        <button
                          type="button"
                          onClick={handleOpenChatWithFolders}
                          className="inline-flex items-center gap-1.5 rounded-lg bg-accent px-2.5 py-1.5 text-xs font-semibold text-accent-fg transition hover:opacity-90"
                        >
                          <MessageSquare className="h-3.5 w-3.5" />
                          Open chat
                        </button>
                        {[...selectedFolderIds].some((id) => !isUnfiledFolder(id)) && (
                          <button
                            type="button"
                            onClick={handleBulkDeleteFolders}
                            disabled={deletingFolders}
                            className="inline-flex items-center gap-1.5 rounded-lg border border-border px-2.5 py-1.5 text-xs font-semibold text-danger transition hover:bg-danger-soft disabled:opacity-60"
                          >
                            {deletingFolders ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Trash2 className="h-3.5 w-3.5" />}
                            Delete {[...selectedFolderIds].filter((id) => !isUnfiledFolder(id)).length}
                          </button>
                        )}
                        <button
                          type="button"
                          onClick={() => setSelectedFolderIds(new Set())}
                          className="rounded-lg p-1.5 text-muted transition hover:bg-surface hover:text-foreground"
                          aria-label="Clear selection"
                        >
                          <X className="h-3.5 w-3.5" />
                        </button>
                      </>
                    ) : (
                      <>
                        <button
                          type="button"
                          onClick={() => setCreatingFolder(true)}
                          className="inline-flex items-center gap-1.5 rounded-lg border border-border bg-surface-raised px-2.5 py-1.5 text-xs font-semibold text-foreground transition hover:border-border-strong hover:bg-surface"
                        >
                          <Plus className="h-3.5 w-3.5" />
                          New folder
                        </button>
                        <button
                          type="button"
                          onClick={() => setUploadOpen(true)}
                          className="inline-flex items-center gap-1.5 rounded-lg bg-accent px-2.5 py-1.5 text-xs font-semibold text-accent-fg transition hover:opacity-90"
                        >
                          <Upload className="h-3.5 w-3.5" strokeWidth={1.75} />
                          Upload
                        </button>
                      </>
                    )}
                  </div>
                )}
              </div>

              {!readOnly && creatingFolder && (
                <form
                  onSubmit={(event) => {
                    event.preventDefault();
                    void submitNewFolder();
                  }}
                  className="mb-3 flex items-center gap-2"
                >
                  <input
                    autoFocus
                    value={newFolderName}
                    onChange={(event) => setNewFolderName(event.target.value)}
                    onKeyDown={(event) => {
                      if (event.key === "Escape") {
                        setCreatingFolder(false);
                        setNewFolderName("");
                      }
                    }}
                    placeholder="Folder name"
                    className="h-10 min-w-0 flex-1 rounded-lg border border-border-strong bg-surface-raised px-3 text-sm outline-none focus:border-accent focus:ring-4 focus:ring-accent-soft"
                  />
                  <button type="submit" className="h-10 rounded-lg bg-accent px-3 text-xs font-semibold text-accent-fg">
                    Create
                  </button>
                  <button
                    type="button"
                    onClick={() => {
                      setCreatingFolder(false);
                      setNewFolderName("");
                    }}
                    className="h-10 rounded-lg border border-border px-3 text-xs font-medium text-muted hover:text-foreground"
                  >
                    Cancel
                  </button>
                </form>
              )}

              <div className="grid grid-cols-1 gap-2.5 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
                {displayFolders.map((folder) => {
                  const virtual = isUnfiledFolder(folder.folder_id);
                  return (
                    <div
                      key={folder.folder_id}
                      role="button"
                      tabIndex={0}
                      onClick={() => onSelectFolder(folder.folder_id)}
                      onKeyDown={(e) => {
                        if (e.key === "Enter" || e.key === " ") onSelectFolder(folder.folder_id);
                      }}
                      className="group flex cursor-pointer items-center gap-3 rounded-xl border border-border bg-surface-raised p-4 text-left shadow-soft transition hover:-translate-y-0.5 hover:border-border-strong hover:shadow-md"
                    >
                      {!readOnly && (
                        <button
                          type="button"
                          className="shrink-0 text-muted hover:text-foreground"
                          onClick={(e) => {
                            e.stopPropagation();
                            toggleFolderSelection(folder.folder_id);
                          }}
                          aria-label={selectedFolderIds.has(folder.folder_id) ? "Deselect folder" : "Select folder"}
                        >
                          {selectedFolderIds.has(folder.folder_id) ? <CheckSquare className="h-4 w-4" /> : <Square className="h-4 w-4" />}
                        </button>
                      )}
                      <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-surface text-muted group-hover:text-foreground">
                        <FolderIcon className="h-4 w-4" strokeWidth={1.75} />
                      </span>
                      <span className="min-w-0 flex-1">
                        <span className="block truncate text-sm font-medium">{folder.name}</span>
                        <span className="text-xs text-muted">
                          {folder.document_count} file{folder.document_count === 1 ? "" : "s"}
                        </span>
                      </span>
                      {!readOnly && !virtual && (
                        <button
                          type="button"
                          className="shrink-0 rounded-lg p-1 text-muted opacity-0 transition hover:bg-danger-soft hover:text-danger group-hover:opacity-100"
                          onClick={(e) => {
                            e.stopPropagation();
                            void handleDeleteFolderCard(folder);
                          }}
                          aria-label={`Delete ${folder.name}`}
                        >
                          <Trash2 className="h-3.5 w-3.5" />
                        </button>
                      )}
                    </div>
                  );
                })}
              </div>
            </div>
          )}

          {showFileList && (
            <>
              {!readOnly && filtered.length > 0 && (
                <button type="button" onClick={toggleAll} className="mb-3 inline-flex items-center gap-2 text-xs text-muted hover:text-foreground">
                  {allVisibleSelected ? <CheckSquare className="h-4 w-4" /> : <Square className="h-4 w-4" />}
                  {allVisibleSelected ? "Deselect all" : "Select all"}
                </button>
              )}

              {filtered.length === 0 && (
                <div className="rounded-xl border border-dashed border-border-strong bg-surface-raised px-6 py-16 text-center">
                  <FileText className="mx-auto mb-3 h-8 w-8 text-muted" />
                  <p className="text-sm font-medium">{query ? "No matches" : "No documents yet"}</p>
                  <p className="mt-1 text-xs text-muted">
                    {query
                      ? "Try another search."
                      : readOnly
                        ? viewingUnfiled || activeFolder
                          ? "Nothing in here yet."
                          : "No documents available."
                        : viewingUnfiled || activeFolder
                          ? "Upload a file or move one in."
                          : "Upload a file to get started."}
                  </p>
                  {!readOnly && !query && (
                    <button
                      type="button"
                      onClick={() => setUploadOpen(true)}
                      className="mt-4 inline-flex items-center gap-2 rounded-lg bg-accent px-3 py-2 text-xs font-semibold text-accent-fg"
                    >
                      <Upload className="h-3.5 w-3.5" />
                      Upload
                    </button>
                  )}
                </div>
              )}

              {view === "list" && filtered.length > 0 && (
                <div className="divide-y divide-border overflow-hidden rounded-xl border border-border bg-surface-raised shadow-soft">
                  {filtered.map((doc) => (
                    <div
                      key={doc.document_id}
                      role="button"
                      tabIndex={0}
                      className="group flex cursor-pointer items-center gap-3 px-3 py-3.5 transition hover:bg-surface sm:px-4"
                      onClick={() => openDocument(doc)}
                      onKeyDown={(e) => {
                        if (e.key === "Enter" || e.key === " ") openDocument(doc);
                      }}
                    >
                      {!readOnly && (
                        <button
                          type="button"
                          className="text-muted hover:text-foreground"
                          onClick={(e) => {
                            e.stopPropagation();
                            toggleOne(doc.document_id);
                          }}
                        >
                          {selected.has(doc.document_id) ? <CheckSquare className="h-4 w-4" /> : <Square className="h-4 w-4" />}
                        </button>
                      )}
                      <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-surface">
                        <DocIcon docType={doc.doc_type} className="h-4 w-4 text-muted" />
                      </div>
                      <div className="min-w-0 flex-1">
                        <div className="truncate text-sm font-medium">{doc.filename}</div>
                        <div className="mt-0.5 flex items-center gap-2 text-xs text-muted">
                          {doc.status !== "indexed" && <StatusBadge status={doc.status} />}
                          {doc.page_count ? (
                            <span>
                              {doc.status !== "indexed" ? "· " : ""}
                              {doc.page_count} pages
                            </span>
                          ) : null}
                        </div>
                        {!activeFolder && <FolderChips folderIds={doc.folder_ids} folders={folders} />}
                      </div>
                      <span className="hidden shrink-0 text-xs text-muted sm:inline">{formatDate(doc.created_at)}</span>
                      {!readOnly && (
                        <button
                          type="button"
                          className="shrink-0 rounded-lg p-1.5 text-muted opacity-0 transition hover:bg-danger-soft hover:text-danger group-hover:opacity-100 disabled:opacity-60"
                          disabled={deletingDocId === doc.document_id}
                          onClick={(e) => {
                            e.stopPropagation();
                            void handleDeleteDocument(doc);
                          }}
                          aria-label={`Delete ${doc.filename}`}
                        >
                          {deletingDocId === doc.document_id ? (
                            <Loader2 className="h-3.5 w-3.5 animate-spin" />
                          ) : (
                            <Trash2 className="h-3.5 w-3.5" />
                          )}
                        </button>
                      )}
                    </div>
                  ))}
                </div>
              )}

              {view === "grid" && filtered.length > 0 && (
                <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 md:grid-cols-4">
                  {filtered.map((doc) => (
                    <div
                      key={doc.document_id}
                      role="button"
                      tabIndex={0}
                      onClick={() => openDocument(doc)}
                      onKeyDown={(e) => {
                        if (e.key === "Enter" || e.key === " ") openDocument(doc);
                      }}
                      className="group relative flex min-h-[150px] cursor-pointer flex-col items-start gap-3 rounded-xl border border-border bg-surface-raised p-4 text-left shadow-soft transition hover:-translate-y-0.5 hover:border-border-strong hover:shadow-md"
                    >
                      {!readOnly && (
                        <button
                          type="button"
                          className="absolute right-2 top-2 shrink-0 rounded-lg bg-surface-raised p-1.5 text-muted opacity-0 transition hover:bg-danger-soft hover:text-danger group-hover:opacity-100 disabled:opacity-60"
                          disabled={deletingDocId === doc.document_id}
                          onClick={(e) => {
                            e.stopPropagation();
                            void handleDeleteDocument(doc);
                          }}
                          aria-label={`Delete ${doc.filename}`}
                        >
                          {deletingDocId === doc.document_id ? (
                            <Loader2 className="h-3.5 w-3.5 animate-spin" />
                          ) : (
                            <Trash2 className="h-3.5 w-3.5" />
                          )}
                        </button>
                      )}
                      <DocIcon docType={doc.doc_type} className="h-6 w-6 text-muted" />
                      <span className="line-clamp-2 text-xs font-medium">{doc.filename}</span>
                      {doc.status !== "indexed" && <StatusBadge status={doc.status} />}
                      {!activeFolder && <FolderChips folderIds={doc.folder_ids} folders={folders} />}
                    </div>
                  ))}
                </div>
              )}
            </>
          )}
        </div>
      </div>

      <AnimatePresence>
        {!readOnly && uploadOpen && (
          <UploadOverlay
            key="upload-overlay"
            onClose={() => setUploadOpen(false)}
            onIndexed={onIndexed}
            folders={folders}
            defaultFolderId={activeFolderId}
            onFoldersChanged={onFoldersChanged}
          />
        )}
      </AnimatePresence>

      <AnimatePresence>
        {inspectDoc && (
          <DocumentInspector
            key="document-inspector"
            document={inspectDoc}
            previewOnly={readOnly}
            onClose={() => setInspectDoc(null)}
            onOpenPreview={(id) => {
              setInspectDoc(null);
              onOpen(id);
            }}
          />
        )}
      </AnimatePresence>
    </div>
  );
}
