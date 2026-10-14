"use client";

import { useEffect, useRef, useState } from "react";
import { FileWarning, Loader2 } from "lucide-react";
import { Document, Page, pdfjs } from "react-pdf";
import "react-pdf/dist/Page/AnnotationLayer.css";
import "react-pdf/dist/Page/TextLayer.css";

// Bundled with the app (served from our own origin) instead of a third-party CDN.
pdfjs.GlobalWorkerOptions.workerSrc = new URL("pdfjs-dist/build/pdf.worker.min.mjs", import.meta.url).toString();

interface Props {
  url: string;
  page: number;
  zoom: number;
  onLoad: (numPages: number) => void;
  onVisiblePage?: (page: number) => void;
}

export function PdfPreview({ url, page, zoom, onLoad, onVisiblePage }: Props) {
  const [numPages, setNumPages] = useState(0);
  const [containerWidth, setContainerWidth] = useState(420);
  const containerRef = useRef<HTMLDivElement>(null);
  const pageRefs = useRef<Map<number, HTMLDivElement>>(new Map());
  const skipScrollReport = useRef(false);

  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;
    const observer = new ResizeObserver((entries) => {
      const nextWidth = entries[0]?.contentRect.width;
      if (nextWidth) setContainerWidth(nextWidth);
    });
    observer.observe(container);
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    if (!page || !numPages) return;
    const element = pageRefs.current.get(page);
    if (!element) return;
    skipScrollReport.current = true;
    element.scrollIntoView({ behavior: "smooth", block: "start" });
    const timeout = window.setTimeout(() => {
      skipScrollReport.current = false;
    }, 600);
    return () => window.clearTimeout(timeout);
  }, [page, numPages, url]);

  useEffect(() => {
    const root = containerRef.current;
    if (!root || !onVisiblePage || numPages === 0) return;

    const observer = new IntersectionObserver(
      (entries) => {
        if (skipScrollReport.current) return;
        let best: { page: number; ratio: number } | null = null;
        for (const entry of entries) {
          const pageNumber = Number((entry.target as HTMLElement).dataset.page);
          if (!pageNumber || !entry.isIntersecting) continue;
          if (!best || entry.intersectionRatio > best.ratio) best = { page: pageNumber, ratio: entry.intersectionRatio };
        }
        if (best) onVisiblePage(best.page);
      },
      { root, threshold: [0.2, 0.45, 0.7] },
    );

    pageRefs.current.forEach((element) => observer.observe(element));
    return () => observer.disconnect();
  }, [numPages, onVisiblePage, url]);

  const fittedPageWidth = Math.max(220, Math.min(720, containerWidth - 32));

  return (
    <div ref={containerRef} className="h-full overflow-auto bg-surface p-3 sm:p-4">
      <Document
        file={url}
        loading={
          <div className="flex min-h-64 flex-col items-center justify-center gap-3 text-xs text-muted">
            <Loader2 className="h-5 w-5 animate-spin text-foreground" />
            Rendering document…
          </div>
        }
        onLoadError={() => { /* shown by `error` below; handling it here keeps it out of the console and the dev crash overlay */ }}
        onSourceError={() => { /* same: the file could not be fetched (deleted, moved, link expired) */ }}
        error={
          <div className="flex min-h-64 flex-col items-center justify-center gap-2 px-6 text-center text-xs text-muted">
            <FileWarning className="h-6 w-6 text-danger" />
            <span className="font-semibold text-foreground">File not found</span>
            The file could not be loaded. It may have been deleted or moved.
          </div>
        }
        onLoadSuccess={(pdf) => {
          setNumPages(pdf.numPages);
          onLoad(pdf.numPages);
        }}
      >
        <div className="mx-auto flex w-fit flex-col gap-4">
          {Array.from({ length: numPages }, (_, index) => {
            const pageNumber = index + 1;
            return (
              <div
                key={pageNumber}
                data-page={pageNumber}
                ref={(element) => {
                  if (element) pageRefs.current.set(pageNumber, element);
                  else pageRefs.current.delete(pageNumber);
                }}
                className="overflow-hidden rounded-lg bg-white shadow-[0_8px_24px_rgba(0,0,0,0.14)] ring-1 ring-border"
              >
                <div className="flex items-center justify-between border-b border-border bg-surface-raised px-3 py-1.5 text-[9px] font-medium text-muted">
                  <span>Page {pageNumber}</span>
                  <span>{numPages ? `${pageNumber} / ${numPages}` : ""}</span>
                </div>
                <Page pageNumber={pageNumber} width={Math.round(fittedPageWidth * zoom)} />
              </div>
            );
          })}
        </div>
      </Document>
    </div>
  );
}
