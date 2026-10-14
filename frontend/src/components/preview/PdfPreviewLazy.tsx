"use client";

import dynamic from "next/dynamic";
import { Loader2 } from "lucide-react";

// pdf.js touches `window` at import time, so it must never run during server rendering.
export const PdfPreview = dynamic(() => import("./PdfPreview").then((m) => m.PdfPreview), {
  ssr: false,
  loading: () => (
    <div className="flex h-full min-h-[8rem] items-center justify-center text-muted">
      <Loader2 className="h-4 w-4 animate-spin" />
    </div>
  ),
});
