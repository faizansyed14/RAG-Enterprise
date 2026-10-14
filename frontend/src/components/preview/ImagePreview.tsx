"use client";

export function ImagePreview({ url, caption }: { url: string; caption?: string | null }) {
  return (
    <div className="flex h-full flex-col overflow-auto bg-surface p-4 sm:p-6">
      <div className="m-auto overflow-hidden rounded-lg bg-surface-raised p-2 shadow-panel ring-1 ring-border">
        {/* eslint-disable-next-line @next/next/no-img-element */}
        <img src={url} alt={caption ?? "Diagram preview"} className="mx-auto block max-w-full rounded-lg" />
        {caption && <p className="px-2 pb-1 pt-3 text-center text-[10px] font-medium text-muted">{caption}</p>}
      </div>
    </div>
  );
}
