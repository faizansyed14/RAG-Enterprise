"use client";

import { useEffect, useRef, useState } from "react";
import clsx from "clsx";
import { Check, ChevronDown, Folder as FolderIcon, Plus } from "lucide-react";
import type { Folder } from "@/lib/types";

interface Props {
  folders: Folder[];
  label: string;
  selectedFolderId?: string | null;
  onSelect: (folderId: string) => void;
  onCreateFolder: (name: string) => Promise<Folder>;
  allowNone?: boolean;
  noneLabel?: string;
  onSelectNone?: () => void;
  className?: string;
}

/** Reusable folder-target dropdown: pick an existing folder, create a new
 * one inline, or (when allowNone) pick "no folder". Shared by the bulk
 * move/copy action in DocumentsView and the upload-target selector in
 * UploadOverlay -- same open/close/click-outside pattern already
 * established in DocumentPicker.tsx. */
export function FolderPickerMenu({
  folders,
  label,
  selectedFolderId,
  onSelect,
  onCreateFolder,
  allowNone,
  noneLabel = "No folder",
  onSelectNone,
  className,
}: Props) {
  const [open, setOpen] = useState(false);
  const [creating, setCreating] = useState(false);
  const [newName, setNewName] = useState("");
  const rootRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onClickOutside = (event: MouseEvent) => {
      if (rootRef.current && !rootRef.current.contains(event.target as Node)) {
        setOpen(false);
        setCreating(false);
      }
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setOpen(false);
        setCreating(false);
      }
    };
    document.addEventListener("mousedown", onClickOutside);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("mousedown", onClickOutside);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [open]);

  const submitCreate = async () => {
    const name = newName.trim();
    if (!name) return;
    const folder = await onCreateFolder(name);
    setNewName("");
    setCreating(false);
    setOpen(false);
    onSelect(folder.folder_id);
  };

  return (
    <div ref={rootRef} className={clsx("relative", className)}>
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
        className="inline-flex items-center gap-1.5 rounded-lg border border-border px-2.5 py-1.5 text-xs font-medium text-foreground transition hover:bg-surface"
      >
        {label}
        <ChevronDown className={clsx("h-3 w-3 transition", open && "rotate-180")} />
      </button>

      {open && (
        <div className="animate-fade-up absolute right-0 top-full z-30 mt-1.5 w-56 overflow-hidden rounded-xl border border-border bg-background shadow-panel">
          <div className="max-h-52 overflow-y-auto p-1">
            {allowNone && (
              <button
                type="button"
                onClick={() => {
                  onSelectNone?.();
                  setOpen(false);
                }}
                className="flex w-full items-center gap-2 rounded-lg px-2.5 py-1.5 text-left text-xs text-muted transition hover:bg-surface"
              >
                {noneLabel}
              </button>
            )}
            {folders.length === 0 && (
              <p className="px-2.5 py-3 text-center text-[11px] text-muted">No folders yet</p>
            )}
            {folders.map((folder) => (
              <button
                key={folder.folder_id}
                type="button"
                onClick={() => {
                  onSelect(folder.folder_id);
                  setOpen(false);
                }}
                className="flex w-full items-center gap-2 rounded-lg px-2.5 py-1.5 text-left text-xs text-foreground transition hover:bg-surface"
              >
                <FolderIcon className="h-3.5 w-3.5 shrink-0 text-muted" />
                <span className="min-w-0 flex-1 truncate">{folder.name}</span>
                {selectedFolderId === folder.folder_id && <Check className="h-3 w-3 shrink-0" />}
              </button>
            ))}
          </div>
          <div className="border-t border-border p-1.5">
            {creating ? (
              <form
                onSubmit={(event) => {
                  event.preventDefault();
                  void submitCreate();
                }}
                className="flex items-center gap-1"
              >
                <input
                  autoFocus
                  value={newName}
                  onChange={(event) => setNewName(event.target.value)}
                  placeholder="Folder name"
                  className="w-full rounded-lg border border-border bg-background px-2 py-1 text-xs outline-none focus:border-foreground"
                />
                <button
                  type="submit"
                  className="shrink-0 rounded-lg bg-foreground px-2 py-1 text-[10px] font-medium text-accent-fg"
                >
                  Add
                </button>
              </form>
            ) : (
              <button
                type="button"
                onClick={() => setCreating(true)}
                className="flex w-full items-center gap-1.5 rounded-lg px-2.5 py-1.5 text-left text-xs font-medium text-foreground transition hover:bg-surface"
              >
                <Plus className="h-3.5 w-3.5" />
                New folder
              </button>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
