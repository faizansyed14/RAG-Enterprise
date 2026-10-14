"use client";

/**
 * Upload files from this computer into a source. The files are read locally by the worker (no language model, no API cost) and
 * become searchable for the people chosen under Document Access. Nothing is preselected there: opening files up is a choice.
 */

import { useCallback, useRef, useState } from "react";
import clsx from "clsx";
import { AlertTriangle, CheckCircle2, Loader2, Upload, X } from "lucide-react";
import { uploadFiles, type UploadResult } from "./api";
import type { CatalogEntry } from "./catalog";
import { Field, Radio, Section, SetupFrame, inputClass } from "./Frame";

const size = (n: number) => (n < 1024 * 1024 ? `${Math.max(1, Math.round(n / 1024))} KB` : `${(n / 1024 / 1024).toFixed(1)} MB`);

export function UploadForm({
  entry, onBack, onDone, onError, limits, existingName,
}: {
  entry: CatalogEntry; onBack: () => void; onDone: (r: UploadResult, name: string) => void; onError: (e: unknown) => void;
  limits: { upload_max_files: number; upload_max_file_mb: number } | null; existingName?: string;
}) {
  const [files, setFiles] = useState<File[]>([]);
  const [name, setName] = useState(existingName ?? "");
  const [access, setAccess] = useState<"" | "everyone" | "principals">("");
  const [principals, setPrincipals] = useState("");
  const [drag, setDrag] = useState(false);
  const [progress, setProgress] = useState<number | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [result, setResult] = useState<UploadResult | null>(null);
  const input = useRef<HTMLInputElement>(null);

  const add = useCallback((list: FileList | File[]) => {
    setFiles((cur) => {
      const seen = new Set(cur.map((f) => `${f.name}|${f.size}`));
      return [...cur, ...Array.from(list).filter((f) => !seen.has(`${f.name}|${f.size}`))];
    });
  }, []);

  const needAccess = !existingName;
  const missing: string[] = [];
  if (!files.length) missing.push("at least one file");
  if (!name.trim()) missing.push("a display name");
  if (needAccess && !access) missing.push("who may read the files");
  if (needAccess && access === "principals" && !principals.trim()) missing.push("the groups that may read them");
  const tooMany = limits && files.length > limits.upload_max_files;

  const submit = async () => {
    setProblem(null);
    setProgress(0);
    const form = new FormData();
    form.append("name", name.trim());
    if (needAccess) {
      form.append("access", access);
      form.append("principals", principals);
    }
    files.forEach((f) => form.append("files", f, f.name));
    try {
      const r = await uploadFiles(form, setProgress);
      setResult(r);
      onDone(r, name.trim());
    } catch (err) {
      if (err instanceof Error && err.message === "unauthorized") onError(err);
      else setProblem(err instanceof Error ? err.message : "The upload failed");
    } finally {
      setProgress(null);
    }
  };

  return (
    <SetupFrame entry={entry} onBack={onBack} intro={existingName ? `Add more files to ${existingName}.` : undefined}>
      <Section title="Connector Content" subtitle="Select content to index for this connector.">
        <Field label="Files">
          <div
            onDragOver={(e) => { e.preventDefault(); setDrag(true); }}
            onDragLeave={() => setDrag(false)}
            onDrop={(e) => { e.preventDefault(); setDrag(false); add(e.dataTransfer.files); }}
            className={clsx("flex flex-wrap items-center gap-3 rounded-xl border border-dashed px-4 py-5", drag ? "border-foreground bg-surface-hover" : "border-border-strong")}
          >
            <button type="button" onClick={() => input.current?.click()} className="inline-flex items-center gap-2 rounded-xl border border-border bg-surface-hover px-4 py-2.5 text-sm font-medium hover:border-foreground">
              <Upload className="h-4 w-4" /> Choose files
            </button>
            <span className="text-sm text-muted">Or drag and drop files here.</span>
            <input ref={input} type="file" multiple className="hidden" onChange={(e) => { if (e.target.files) add(e.target.files); e.target.value = ""; }} aria-label="Choose files" />
          </div>
          {limits && <p className="mt-2 text-xs text-muted">Up to {limits.upload_max_files} files per upload, {limits.upload_max_file_mb} MB each. PDF, Word, Excel, PowerPoint, text, e-mail and scanned images are read; other types are listed as skipped with a reason.</p>}
        </Field>
        {files.length > 0 && (
          <ul className="max-h-60 divide-y divide-border overflow-y-auto rounded-xl border border-border bg-surface text-sm">
            {files.map((f, i) => (
              <li key={`${f.name}-${i}`} className="flex items-center gap-3 px-3 py-2">
                <span className="min-w-0 flex-1 break-words">{f.name}</span>
                <span className="shrink-0 text-xs tabular-nums text-muted">{size(f.size)}</span>
                <button type="button" onClick={() => setFiles((cur) => cur.filter((_, j) => j !== i))} className="rounded p-1 text-muted hover:bg-surface-hover" aria-label={`Remove ${f.name}`}>
                  <X className="h-3.5 w-3.5" />
                </button>
              </li>
            ))}
          </ul>
        )}
        {tooMany && <p className="text-xs text-warning">That is more than {limits?.upload_max_files} files. Upload them in batches.</p>}
      </Section>

      <Section title="Connector Settings" subtitle="Connector information and access.">
        <Field label="Connector Display Name" htmlFor="up-name">
          <input id="up-name" value={name} onChange={(e) => setName(e.target.value)} disabled={!!existingName} placeholder="File" className={inputClass} />
        </Field>
        {needAccess ? (
          <Field label="Document Access" hint="Control who can access documents indexed by this connector. This is set once, when the connector is created.">
            <div className="space-y-2">
              <Radio name="up-access" checked={access === "everyone"} onChange={() => setAccess("everyone")} title="Everyone in this workspace">Every signed-in user can search these files.</Radio>
              <Radio name="up-access" checked={access === "principals"} onChange={() => setAccess("principals")} title="Only specific groups">Give groups or people, for example <code>group:finance</code>, <code>user:ana</code>.</Radio>
            </div>
            {access === "principals" && <input value={principals} onChange={(e) => setPrincipals(e.target.value)} placeholder="group:finance, user:ana" className={clsx(inputClass, "mt-3")} aria-label="Who may read" />}
          </Field>
        ) : (
          <p className="text-xs text-muted">These files get the same readers as the rest of {existingName}.</p>
        )}
      </Section>

      {progress !== null && (
        <div className="space-y-1.5" role="status" aria-label="Upload progress">
          <div className="h-2 overflow-hidden rounded-full bg-accent-soft"><div className="h-full bg-accent transition-all" style={{ width: `${Math.round(progress * 100)}%` }} /></div>
          <div className="text-xs text-muted">{progress < 1 ? `Uploading… ${Math.round(progress * 100)}%` : "Saving and queueing for indexing…"}</div>
        </div>
      )}
      {problem && (
        <div className="flex items-start gap-2 rounded-xl border border-warning bg-warning-soft px-4 py-3 text-sm text-warning" role="alert">
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" /> <span className="break-words">{problem}</span>
        </div>
      )}
      {result && result.rejected.length > 0 && (
        <div className="rounded-xl border border-warning bg-warning-soft px-4 py-3 text-xs text-warning">
          <div className="mb-1 flex items-center gap-1.5 font-medium"><CheckCircle2 className="h-3.5 w-3.5" /> {result.saved.length} saved. Not saved:</div>
          <ul className="space-y-0.5">{result.rejected.map((r) => <li key={r.name}>{r.name}: {r.reason}</li>)}</ul>
        </div>
      )}

      <div className="flex flex-wrap items-center gap-3 pb-6">
        <button type="button" disabled={progress !== null || !!missing.length || !!tooMany} onClick={() => void submit()} className="inline-flex items-center gap-2 rounded-lg bg-accent px-4 py-2.5 text-sm font-medium text-accent-fg hover:opacity-90 disabled:opacity-40">
          {progress !== null && <Loader2 className="h-4 w-4 animate-spin" />} {existingName ? "Upload and index" : "Create connector and index"}
        </button>
        {missing.length > 0 && <span className="text-xs text-muted">Still needed: {missing.join(", ")}.</span>}
      </div>
    </SetupFrame>
  );
}
