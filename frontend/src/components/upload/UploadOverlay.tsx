"use client";

import { useState } from "react";
import { motion, useReducedMotion } from "framer-motion";
import { Upload, X } from "lucide-react";
import { createFolder } from "@/lib/api";
import type { Folder } from "@/lib/types";
import { FolderPickerMenu } from "@/components/documents/FolderPickerMenu";
import { FileDropzone } from "@/components/upload/FileDropzone";

interface Props {
  onClose: () => void;
  onIndexed: () => void;
  folders: Folder[];
  defaultFolderId: string | null;
  onFoldersChanged: () => void;
}

export function UploadOverlay({ onClose, onIndexed, folders, defaultFolderId, onFoldersChanged }: Props) {
  const [targetFolderId, setTargetFolderId] = useState<string | null>(defaultFolderId);
  const targetFolder = targetFolderId ? folders.find((f) => f.folder_id === targetFolderId) : null;
  const reduced = useReducedMotion();

  const handleCreateFolder = async (name: string): Promise<Folder> => {
    const folder = await createFolder(name);
    onFoldersChanged();
    return folder;
  };

  return (
    <motion.div
      initial={reduced ? false : { opacity: 0 }}
      animate={{ opacity: 1 }}
      exit={{ opacity: 0 }}
      transition={{ duration: 0.18, ease: "easeOut" }}
      className="fixed inset-0 z-[70] flex items-center justify-center bg-black/50 p-4"
      role="dialog"
      aria-modal="true"
      aria-labelledby="upload-title"
    >
      <motion.div
        initial={reduced ? false : { opacity: 0, y: 12, scale: 0.98 }}
        animate={{ opacity: 1, y: 0, scale: 1 }}
        exit={{ opacity: 0, y: 12, scale: 0.98 }}
        transition={{ duration: 0.22, ease: "easeOut" }}
        className="flex max-h-[90vh] w-full max-w-lg flex-col overflow-hidden rounded-xl border border-border-strong bg-background shadow-panel"
      >
        <header className="flex shrink-0 items-center justify-between border-b border-border px-4 py-3">
          <div>
            <h2 id="upload-title" className="text-base font-semibold tracking-tight">
              Upload
            </h2>
            <p className="text-xs text-muted">Indexed automatically once uploaded</p>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="rounded-lg p-2 text-muted transition hover:bg-surface hover:text-foreground"
            aria-label="Close"
          >
            <X className="h-4 w-4" strokeWidth={1.75} />
          </button>
        </header>
        <div className="flex items-center justify-between gap-3 border-b border-border px-4 py-2.5">
          <span className="text-xs text-muted">Upload into</span>
          <FolderPickerMenu
            folders={folders}
            label={targetFolder ? targetFolder.name : "No folder"}
            selectedFolderId={targetFolderId}
            onSelect={setTargetFolderId}
            onCreateFolder={handleCreateFolder}
            allowNone
            noneLabel="No folder"
            onSelectNone={() => setTargetFolderId(null)}
          />
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto p-4">
          <FileDropzone onIndexed={onIndexed} folderId={targetFolderId} />
        </div>
      </motion.div>
    </motion.div>
  );
}
