import { FileText, type LucideIcon } from "lucide-react";

/**
 * What the Connectors screen shows. `kind` is the server's connector kind, except two cards that are not kinds of their own:
 *   "upload"  files dragged in from this computer (stored as a managed folder source; see backend uploads_api.py)
 *   "onsite"  an explanation page: Onsite has no public API documentation, so the page shows the two routes that work today
 * Only SharePoint, OneDrive, Onsite, and Files are offered in the UI.
 * `logo` points at a brand mark under /public/connectors when available.
 */
export interface CatalogEntry {
  kind: string;
  label: string;
  blurb: string;
  icon: LucideIcon;
  logo?: string;
  tint: string; // tailwind classes for the icon tile (fallback when no logo)
  guided?: "microsoft" | "upload" | "onsite";
}

const SLATE = "bg-neutral-200 text-neutral-700";

export const CATALOG: CatalogEntry[] = [
  {
    kind: "sharepoint",
    label: "SharePoint",
    blurb: "Microsoft intranet",
    icon: FileText,
    logo: "/connectors/sharepoint.svg",
    tint: "bg-teal-100 text-teal-700",
    guided: "microsoft",
  },
  {
    kind: "onedrive",
    label: "OneDrive",
    blurb: "Personal files in Microsoft 365",
    icon: FileText,
    logo: "/connectors/onedrive.svg",
    tint: "bg-sky-100 text-sky-700",
    guided: "microsoft",
  },
  {
    kind: "onsite",
    label: "Onsite",
    blurb: "Construction cloud",
    icon: FileText,
    logo: "/connectors/onsite.svg",
    tint: "bg-amber-100 text-amber-700",
    guided: "onsite",
  },
  {
    kind: "upload",
    label: "Files",
    blurb: "Upload from local files",
    icon: FileText,
    logo: "/connectors/files.svg",
    tint: SLATE,
    guided: "upload",
  },
];

const VIRTUAL = new Set(["upload", "onsite"]);
const ALLOWED = new Set(CATALOG.map((c) => c.kind));

export function entryFor(kind: string, serverLabel?: string): CatalogEntry {
  return (
    CATALOG.find((c) => c.kind === kind) ?? {
      kind,
      label: serverLabel ?? kind.replace(/_/g, " "),
      blurb: "Connector",
      icon: FileText,
      tint: SLATE,
    }
  );
}

/** The cards to show: only the allowlisted catalogue entries this server supports (virtual cards always). */
export function cardsFor(serverKinds: { kind: string; label: string }[]): CatalogEntry[] {
  const have = new Set(serverKinds.map((k) => k.kind));
  return CATALOG.filter((c) => ALLOWED.has(c.kind) && (VIRTUAL.has(c.kind) || have.has(c.kind)));
}
