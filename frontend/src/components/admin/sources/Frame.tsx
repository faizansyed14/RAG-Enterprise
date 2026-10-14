"use client";

import type { ReactNode } from "react";
import { ArrowLeft, ArrowLeftRight } from "lucide-react";
import clsx from "clsx";
import type { CatalogEntry } from "./catalog";
import { ConnectorIcon } from "./ConnectorIcon";

/** The look of a setup page: back link, source icon next to the product mark, title, one-line description, then sections. */
export function SetupFrame({ entry, onBack, children, intro }: { entry: CatalogEntry; onBack: () => void; children: ReactNode; intro?: string }) {
  return (
    <div className="mx-auto w-full max-w-3xl px-5 py-8">
      <button type="button" onClick={onBack} className="mb-5 inline-flex items-center gap-1.5 text-xs text-muted hover:text-foreground">
        <ArrowLeft className="h-3.5 w-3.5" /> All connectors
      </button>
      <div className="flex items-center gap-3">
        <ConnectorIcon entry={entry} size="lg" />
        <ArrowLeftRight className="h-4 w-4 text-muted" />
        <span className="flex h-11 w-11 items-center justify-center rounded-xl bg-accent text-lg font-semibold text-accent-fg" aria-hidden>
          A
        </span>
      </div>
      <h1 className="mt-4 text-2xl font-semibold tracking-tight">{entry.label}</h1>
      <p className="mt-1 text-sm text-muted">{intro ?? `Connect to ${entry.label} and set up the connector for ALAIN.`}</p>
      <div className="mt-6 space-y-8 border-t border-border pt-8">{children}</div>
    </div>
  );
}

export function Section({ title, subtitle, children }: { title: string; subtitle?: string; children: ReactNode }) {
  return (
    <section>
      <h2 className="text-base font-semibold">{title}</h2>
      {subtitle && <p className="mt-0.5 text-sm text-muted">{subtitle}</p>}
      <div className="mt-3 space-y-5 rounded-2xl border border-border bg-surface-raised p-5">{children}</div>
    </section>
  );
}

export function Field({ label, hint, children, htmlFor }: { label: string; hint?: ReactNode; children: ReactNode; htmlFor?: string }) {
  return (
    <div>
      <label htmlFor={htmlFor} className="text-sm font-medium">
        {label}
      </label>
      {hint && <div className="mt-0.5 text-xs text-muted">{hint}</div>}
      <div className="mt-2">{children}</div>
    </div>
  );
}

export const inputClass =
  "w-full rounded-lg border border-border bg-surface px-3 py-2.5 text-sm outline-none placeholder:text-muted focus:border-foreground focus:ring-1 focus:ring-foreground";

export function Radio({
  checked, onChange, title, children, name,
}: { checked: boolean; onChange: () => void; title: string; children?: ReactNode; name: string }) {
  return (
    <label className={clsx("flex cursor-pointer items-start gap-3 rounded-xl border px-4 py-3 text-sm", checked ? "border-foreground bg-surface" : "border-border hover:border-border-strong")}>
      <input type="radio" name={name} checked={checked} onChange={onChange} className="mt-1 accent-foreground" />
      <span className="min-w-0">
        <span className="block font-medium">{title}</span>
        {children && <span className="mt-0.5 block text-xs text-muted">{children}</span>}
      </span>
    </label>
  );
}
