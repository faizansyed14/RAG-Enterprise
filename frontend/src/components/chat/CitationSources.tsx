"use client";

import { useId, useState } from "react";
import { ChevronDown, ExternalLink } from "lucide-react";
import type { Citation } from "@/lib/types";

function shortDocName(name: string, max = 25): string {
  if (name.length <= max) return name;
  const keep = Math.floor((max - 3) / 2);
  return `${name.slice(0, keep)}...${name.slice(-keep)}`;
}

function uniqueCitations(citations: Citation[]): Citation[] {
  const seen = new Set<string>();
  const unique: Citation[] = [];
  for (const citation of citations) {
    const key = `${citation.document_id ?? citation.doc_id ?? citation.document}|${citation.page}|${citation.index ?? ""}`;
    if (seen.has(key)) continue;
    seen.add(key);
    unique.push(citation);
  }
  return unique;
}

export function CitationSources({ citations, onOpen }: { citations: Citation[]; onOpen: (citation: Citation) => void }) {
  const items = uniqueCitations(citations);
  const [open, setOpen] = useState(false);
  const listId = useId();
  if (items.length === 0) return null;

  return (
    <div className="mt-5 border-t border-border pt-3">
      <button
        type="button"
        aria-expanded={open}
        aria-controls={listId}
        onClick={() => setOpen((current) => !current)}
        className="group inline-flex items-center gap-2 rounded-md py-1 text-[10px] font-semibold uppercase tracking-[0.12em] text-muted transition hover:text-foreground"
      >
        <span className="text-foreground">{items.length} source{items.length === 1 ? "" : "s"}</span>
        <ChevronDown className={`h-3.5 w-3.5 transition-transform ${open ? "rotate-180" : ""}`} />
        <span className="sr-only">{open ? "Hide sources" : "Show sources"}</span>
      </button>

      {open && (
        <div id={listId} className="mt-2 grid gap-1.5 sm:grid-cols-2">
          {items.map((citation, index) => {
            const label = citation.index != null ? citation.index : index + 1;
            return (
              <button
                key={`${citation.document_id ?? citation.document}-${citation.page}-${citation.index ?? index}`}
                type="button"
                title={`Open ${citation.document} · p.${citation.page}`}
                onClick={() => onOpen(citation)}
                className="group/source flex min-w-0 items-center gap-2 rounded-md border border-border bg-surface-raised px-2 py-1.5 text-left text-[10px] text-foreground transition hover:border-border-strong hover:bg-surface"
              >
                <span className="flex h-4 min-w-4 items-center justify-center rounded bg-accent px-1 text-[8px] font-bold leading-none text-accent-fg">{label}</span>
                <span className="min-w-0 flex-1 truncate font-medium">{shortDocName(citation.document)}</span>
                <span className="shrink-0 text-[9px] text-muted">p.{citation.page}</span>
                <ExternalLink className="h-3 w-3 shrink-0 text-muted opacity-0 transition group-hover/source:opacity-100" />
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}
