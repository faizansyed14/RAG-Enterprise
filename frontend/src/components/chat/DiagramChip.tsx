"use client";

import { ImageIcon } from "lucide-react";
import type { DiagramEvidence } from "@/lib/types";

export function DiagramChip({ evidence, onClick }: { evidence: DiagramEvidence; onClick: () => void }) {
  return (
    <button
      type="button"
      onClick={onClick}
      title={evidence.vision_verdict}
      className="inline-flex items-center gap-1.5 rounded-lg border border-border bg-surface-raised px-2.5 py-2 text-[10px] font-medium text-foreground shadow-soft transition hover:border-border-strong hover:bg-surface"
    >
      <ImageIcon className="h-3 w-3" />
      {evidence.figure_id ?? `page ${evidence.page_number}`}
    </button>
  );
}
