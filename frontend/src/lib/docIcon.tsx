import { FileCode, FileJson, FileSpreadsheet, FileText, FileType, Mail, type LucideIcon } from "lucide-react";
import type { DocumentOut } from "./types";

const ICONS: Record<DocumentOut["doc_type"], LucideIcon> = {
  pdf: FileText,
  docx: FileType,
  csv: FileSpreadsheet,
  xlsx: FileSpreadsheet,
  eml: Mail,
  txt: FileText,
  json: FileJson,
  xer: FileCode,
  py: FileCode,
  md: FileText,
  other: FileText,
};

export function DocIcon({ docType, className }: { docType: DocumentOut["doc_type"]; className?: string }) {
  const Icon = ICONS[docType] ?? FileText;
  return <Icon className={className} strokeWidth={1.75} />;
}
