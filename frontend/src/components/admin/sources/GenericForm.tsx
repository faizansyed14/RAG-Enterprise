"use client";

import { useEffect, useState } from "react";
import { AlertTriangle, CheckCircle2, Loader2 } from "lucide-react";
import { call } from "./api";
import type { CatalogEntry } from "./catalog";
import { Field, Section, SetupFrame, inputClass } from "./Frame";

/** Every other source type: a name and a JSON configuration, started from the server's template for that type. */
export function GenericForm({ entry, template, onBack, onCreated, onError }: {
  entry: CatalogEntry; template: Record<string, unknown>; onBack: () => void; onCreated: (name: string) => void; onError: (e: unknown) => void;
}) {
  const [name, setName] = useState("");
  const [config, setConfig] = useState(JSON.stringify(template, null, 2));
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  useEffect(() => setConfig(JSON.stringify(template, null, 2)), [template]);

  const submit = async () => {
    let parsed: unknown;
    try {
      parsed = JSON.parse(config || "{}");
    } catch {
      setProblem("The configuration is not valid JSON.");
      return;
    }
    setBusy(true);
    setProblem(null);
    try {
      await call("/connections", { method: "POST", body: JSON.stringify({ kind: entry.kind, name: name.trim(), config: parsed }) });
      onCreated(name.trim());
    } catch (err) {
      if (err instanceof Error && err.message === "unauthorized") onError(err);
      else setProblem(err instanceof Error ? err.message : "Something went wrong");
    } finally {
      setBusy(false);
    }
  };

  return (
    <SetupFrame entry={entry} onBack={onBack}>
      <Section title="Connector Settings" subtitle="Connector information and configuration.">
        <Field label="Connector Display Name" htmlFor="gen-name">
          <input id="gen-name" value={name} onChange={(e) => setName(e.target.value)} placeholder={entry.label} className={inputClass} />
        </Field>
        <Field label="Configuration" hint={<>Passwords and keys never go here. Put the secret in the server&apos;s environment and name it, for example <code>&quot;password_env&quot;: &quot;SMB_PASSWORD&quot;</code>. Folder sources must be inside the mounted sources directory.</>} htmlFor="gen-config">
          <textarea id="gen-config" value={config} onChange={(e) => setConfig(e.target.value)} rows={12} spellCheck={false} className={`${inputClass} font-mono text-xs`} />
        </Field>
      </Section>
      {problem && (
        <div className="flex items-start gap-2 rounded-xl border border-warning bg-warning-soft px-4 py-3 text-sm text-warning" role="alert">
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" /> <span className="break-words">{problem}</span>
        </div>
      )}
      <div className="flex items-center gap-3 pb-6">
        <button type="button" disabled={busy || !name.trim()} onClick={() => void submit()} className="inline-flex items-center gap-2 rounded-lg bg-accent px-4 py-2.5 text-sm font-medium text-accent-fg hover:opacity-90 disabled:opacity-40">
          {busy ? <Loader2 className="h-4 w-4 animate-spin" /> : <CheckCircle2 className="h-4 w-4" />} Connect and scan
        </button>
      </div>
    </SetupFrame>
  );
}
