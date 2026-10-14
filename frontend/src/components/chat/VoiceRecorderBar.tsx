"use client";

const BAR_COUNT = 5;

function formatSeconds(total: number): string {
  const m = Math.floor(total / 60);
  const s = total % 60;
  return `${m}:${String(s).padStart(2, "0")}`;
}

/** Replaces the composer's textarea while the mic is listening: an
 * animated waveform, elapsed-time counter under it, and a live caption of
 * the in-progress (not-yet-finalized) speech -- so the user sees words
 * appear as they talk, before anything is committed to the actual input.
 * The stop control is the mic button itself (turns into a stop icon while
 * listening), rendered by the composer alongside this. */
export function VoiceRecorderBar({ liveText, seconds }: { liveText: string; seconds: number }) {
  return (
    <div className="flex min-h-[44px] flex-1 flex-col items-center justify-center gap-1.5 py-1">
      <div className="flex h-6 items-center gap-[3px]">
        {Array.from({ length: BAR_COUNT }, (_, i) => (
          <span
            key={i}
            className="voice-wave-bar w-1 rounded-full bg-danger"
            style={{ height: "100%", animationDelay: `${i * 0.12}s` }}
          />
        ))}
      </div>
      <span className="text-[11px] font-medium tabular-nums text-muted">{formatSeconds(seconds)}</span>
      <p className="max-w-full truncate px-2 text-xs text-foreground/80">
        {liveText || <span className="text-muted">Listening…</span>}
      </p>
    </div>
  );
}
