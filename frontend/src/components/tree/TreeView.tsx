"use client";

import { useEffect, useState } from "react";
import { ChevronDown, ChevronRight, ListTree, Loader2 } from "lucide-react";
import { getDocumentTree } from "@/lib/api";
import type { TreeNode } from "@/lib/types";

function Node({ node, depth, onJump }: { node: TreeNode; depth: number; onJump: (page: number) => void }) {
  const [open, setOpen] = useState(depth < 1);
  const hasChildren = Boolean(node.nodes?.length);
  const summary = node.summary || node.prefix_summary;

  return (
    <div className="relative" style={{ paddingLeft: depth * 14 }}>
      {depth > 0 && <span className="absolute bottom-0 left-[5px] top-0 w-px bg-border" style={{ marginLeft: (depth - 1) * 14 }} />}
      <div className="group flex items-start gap-1.5 rounded-lg px-1.5 py-2 text-left hover:bg-surface">
        {hasChildren ? (
          <button
            type="button"
            onClick={() => setOpen((current) => !current)}
            className="mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center rounded-md text-muted transition hover:bg-surface-raised hover:text-foreground"
            aria-label={open ? "Collapse section" : "Expand section"}
          >
            {open ? <ChevronDown className="h-3 w-3" /> : <ChevronRight className="h-3 w-3" />}
          </button>
        ) : (
          <span className="h-5 w-5 shrink-0" />
        )}
        <div className="min-w-0 flex-1">
          <button
            type="button"
            onClick={() => node.page_number != null && onJump(node.page_number)}
            className="max-w-full truncate text-left text-xs font-medium text-foreground/80 transition hover:text-foreground"
            title={summary || node.title}
          >
            {node.title || "Untitled section"}
          </button>
          {node.page_number != null && (
            <button
              type="button"
              onClick={() => onJump(node.page_number!)}
              className="ml-1.5 rounded bg-accent px-1.5 py-0.5 text-[9px] font-semibold text-accent-fg hover:opacity-80"
            >
              p.{node.page_number}
            </button>
          )}
          {summary && <p className="mt-1 line-clamp-2 text-[10px] leading-4 text-muted">{summary}</p>}
        </div>
      </div>
      {open && hasChildren && (
        <div>
          {node.nodes.map((child, index) => (
            <Node key={child.node_id ?? index} node={child} depth={depth + 1} onJump={onJump} />
          ))}
        </div>
      )}
    </div>
  );
}

export function TreeView({ documentId, onJump }: { documentId: string; onJump: (page: number) => void }) {
  const [tree, setTree] = useState<TreeNode[] | null>(null);
  const [error, setError] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setTree(null);
    setError(false);
    getDocumentTree(documentId)
      .then((data) => {
        if (!cancelled) setTree(data);
      })
      .catch(() => {
        if (!cancelled) setError(true);
      });
    return () => {
      cancelled = true;
    };
  }, [documentId]);

  if (error) return <p className="p-5 text-center text-xs text-muted">The document structure is not available yet.</p>;
  if (!tree) {
    return (
      <div className="flex items-center justify-center gap-2 p-8 text-xs text-muted">
        <Loader2 className="h-3.5 w-3.5 animate-spin text-foreground" />
        Loading structure…
      </div>
    );
  }
  if (tree.length === 0) return <p className="p-5 text-center text-xs text-muted">No document structure was found.</p>;

  return (
    <div>
      <div className="sticky top-0 z-10 flex items-center gap-2 border-b border-border bg-surface-raised/95 px-4 py-3 backdrop-blur">
        <div className="flex h-7 w-7 items-center justify-center rounded-lg bg-accent text-accent-fg"><ListTree className="h-3.5 w-3.5" /></div>
        <div>
          <div className="text-[10px] font-semibold uppercase tracking-[0.12em] text-foreground/80">Document structure</div>
          <div className="mt-0.5 text-[9px] text-muted">Choose a section to jump to its page</div>
        </div>
      </div>
      <div className="p-2.5">
        {tree.map((node, index) => (
          <Node key={node.node_id ?? index} node={node} depth={0} onJump={onJump} />
        ))}
      </div>
    </div>
  );
}
