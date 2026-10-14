"use client";

import { ArrowRight, Info } from "lucide-react";
import type { CatalogEntry } from "./catalog";
import { Section, SetupFrame } from "./Frame";

/**
 * Onsite (onsite.us) says it has a REST API but publishes no endpoints, no base URL and no sign-in method, and asks customers to
 * request API access from its sales team. Nothing is guessed here: this page shows the two routes that work today.
 */
export function OnsitePage({ entry, onBack, onChoose }: { entry: CatalogEntry; onBack: () => void; onChoose: (kind: string) => void }) {
  return (
    <SetupFrame entry={entry} onBack={onBack} intro="Onsite is a construction cloud for documents, drawings, inspections and handover.">
      <Section title="Direct connection" subtitle="Not available yet.">
        <div className="flex items-start gap-2.5 text-sm">
          <Info className="mt-0.5 h-4 w-4 shrink-0 text-muted" />
          <p>
            Onsite offers an API to customers, but its endpoints and sign-in method are not published, so this app cannot connect to it directly without guessing.
            Onsite says it has an open API, so ask your Onsite contact for access and documentation (the site lists sales@onsite.us for enquiries). When you have the API documentation, a direct connector can be added.
          </p>
        </div>
      </Section>
      <Section title="What works today" subtitle="Both routes keep your files searchable and respect who may read them.">
        <button type="button" onClick={() => onChoose("sharepoint")} className="group flex w-full items-center gap-3 rounded-xl border border-border bg-surface px-4 py-3 text-left hover:border-foreground">
          <span className="min-w-0 flex-1">
            <span className="block text-sm font-medium">Connect the SharePoint library Onsite syncs to</span>
            <span className="block text-xs text-muted">Onsite lists SharePoint, Dropbox and Google Drive among its integrations. If your project syncs documents to one of them, connect that location here. Read only.</span>
          </span>
          <ArrowRight className="h-4 w-4 text-muted group-hover:text-foreground" />
        </button>
        <button type="button" onClick={() => onChoose("upload")} className="group flex w-full items-center gap-3 rounded-xl border border-border bg-surface px-4 py-3 text-left hover:border-foreground">
          <span className="min-w-0 flex-1">
            <span className="block text-sm font-medium">Upload documents exported from Onsite</span>
            <span className="block text-xs text-muted">Export drawings, manuals and records from Onsite, then drag them into an uploaded-files connector.</span>
          </span>
          <ArrowRight className="h-4 w-4 text-muted group-hover:text-foreground" />
        </button>
      </Section>
    </SetupFrame>
  );
}
