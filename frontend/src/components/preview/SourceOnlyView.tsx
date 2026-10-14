"use client";

/**
 * Shown instead of the PDF viewer when the original file is not kept (STORE_ORIGINALS=false). The file lives at its source;
 * this panel says where, offers a link that opens it there when the source gave one, and shows the stored text of the cited page.
 */

import { useEffect, useState } from "react";
import { ChevronLeft, ChevronRight, ExternalLink, Loader2 } from "lucide-react";
import { getPageText, type PreviewInfo } from "@/lib/api";

export function SourceOnlyView({ documentId, page, onPage, info }: {
  documentId: string; page: number; onPage: (page: number) => void; info: PreviewInfo;
}) {
  const [text, setText] = useState<string | null>(null);
  const [total, setTotal] = useState<number | null>(info.page_count ?? null);
  const [missing, setMissing] = useState(false);
  const source = info.source;

  useEffect(() => {
    let cancelled = false;
    setText(null);
    setMissing(false);
    getPageText(documentId, page)
      .then((r) => { if (!cancelled) { setText(r.text); setTotal((t) => r.page_count ?? t); } })
      .catch(() => { if (!cancelled) setMissing(true); });
    return () => { cancelled = true; };
  }, [documentId, page]);

  const product = source?.kind === "onedrive" ? "OneDrive" : source?.kind === "sharepoint" ? "SharePoint" : "the source";

  return (
    <div className="flex h-full flex-col bg-surface-raised">
      <div className="space-y-2 border-b border-border px-4 py-3 text-xs">
        <div className="text-[10px] font-medium uppercase tracking-[0.12em] text-muted">Stored at the source</div>
        {info.missing && <div className="text-[11px] text-warning">The stored copy was deleted. The file can still be opened at its source.</div>}
        <div className="break-words text-foreground">
          {source?.available
            ? <>{source.source}{source.path ? <span className="text-muted"> · {source.path}</span> : null}</>
            : "The file is no longer at its source."}
        </div>
        {source?.url && (
          <a href={source.url} target="_blank" rel="noopener noreferrer"
             className="inline-flex items-center gap-1.5 rounded-lg bg-accent px-3 py-1.5 text-[11px] font-semibold text-accent-fg hover:opacity-90">
            <ExternalLink className="h-3.5 w-3.5" /> Open at {product}
          </a>
        )}
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto px-4 py-3">
        {text === null && !missing && (
          <div className="flex items-center gap-2 text-xs text-muted"><Loader2 className="h-3.5 w-3.5 animate-spin" /> Loading page text…</div>
        )}
        {missing && <p className="text-xs text-muted">The text of this page is not available.</p>}
        {text !== null && <p className="whitespace-pre-wrap break-words text-xs leading-5 text-foreground">{text}</p>}
      </div>
      <div className="flex items-center justify-between border-t border-border px-4 py-2.5 text-[10px] text-muted">
        <span>Page <strong className="font-semibold text-foreground">{page}</strong>{total ? ` of ${total}` : ""}</span>
        <div className="flex items-center gap-1 rounded-lg border border-border bg-surface p-1">
          <button type="button" disabled={page <= 1} onClick={() => onPage(page - 1)} aria-label="Previous page"
            className="rounded-md p-1 text-muted hover:bg-surface-raised hover:text-foreground disabled:opacity-30">
            <ChevronLeft className="h-3.5 w-3.5" />
          </button>
          <button type="button" disabled={!!total && page >= total} onClick={() => onPage(page + 1)} aria-label="Next page"
            className="rounded-md p-1 text-muted hover:bg-surface-raised hover:text-foreground disabled:opacity-30">
            <ChevronRight className="h-3.5 w-3.5" />
          </button>
        </div>
      </div>
    </div>
  );
}
