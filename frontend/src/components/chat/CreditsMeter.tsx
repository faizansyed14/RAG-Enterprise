"use client";

import { useEffect, useState } from "react";
import { Clock, Gauge, MessageSquare, Zap } from "lucide-react";
import clsx from "clsx";
import type { Quota } from "@/lib/types";

/** A quota plus the local time it arrived. The server sends `retry_after_seconds`
 *  (not a wall-clock time), so the countdown is anchored to arrival and stays correct
 *  even if this device's clock is wrong. */
export interface QuotaState {
  quota: Quota;
  receivedAt: number;
}

export function useQuotaCountdown(state: QuotaState | null, onExpired: () => void) {
  const endsAt = state && state.quota.retry_after_seconds > 0 ? state.receivedAt + state.quota.retry_after_seconds * 1000 : null;
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    if (endsAt === null) return;
    setNow(Date.now());
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [endsAt]);

  const secondsLeft = endsAt === null ? 0 : Math.max(0, Math.ceil((endsAt - now) / 1000));
  const blocked = endsAt !== null && secondsLeft > 0;

  useEffect(() => {
    if (endsAt !== null && secondsLeft === 0) onExpired();
  }, [endsAt, secondsLeft]); // eslint-disable-line react-hooks/exhaustive-deps

  return { blocked, secondsLeft, resetsAt: endsAt === null ? null : new Date(endsAt) };
}

export function formatCountdown(totalSeconds: number): string {
  const h = Math.floor(totalSeconds / 3600);
  const m = Math.floor((totalSeconds % 3600) / 60);
  const s = totalSeconds % 60;
  const mm = String(m).padStart(2, "0");
  const ss = String(s).padStart(2, "0");
  return h > 0 ? `${h}:${mm}:${ss}` : `${mm}:${ss}`;
}

/** 3600 -> "1 hour", 1800 -> "30 minutes" -- the wording a person would use. */
export function formatDuration(totalSeconds: number): string {
  if (totalSeconds % 3600 === 0) {
    const h = totalSeconds / 3600;
    return `${h} hour${h === 1 ? "" : "s"}`;
  }
  if (totalSeconds % 60 === 0) {
    const m = totalSeconds / 60;
    return `${m} minute${m === 1 ? "" : "s"}`;
  }
  return `${totalSeconds} seconds`;
}

const plural = (n: number, word: string) => `${n} ${word}${n === 1 ? "" : "s"}`;

/** One segment per message the user gets -- makes "how many do I have" readable at a glance. */
function MessagePips({ total, left, low }: { total: number; left: number; low: boolean }) {
  return (
    <div className="flex gap-1" aria-hidden>
      {Array.from({ length: total }, (_, i) => (
        <span
          key={i}
          className={clsx(
            "h-1.5 flex-1 rounded-full transition-colors duration-500",
            i < left ? (low ? "bg-warning" : "bg-accent") : "bg-border",
          )}
        />
      ))}
    </div>
  );
}

interface Props {
  state: QuotaState;
  blocked: boolean;
  secondsLeft: number;
  resetsAt: Date | null;
}

export function CreditsMeter({ state, blocked, secondsLeft, resetsAt }: Props) {
  const { quota } = state;
  const totalMessages = Math.max(1, Math.floor(quota.limit / quota.cost));
  const pause = formatDuration(quota.block_seconds);

  if (blocked) {
    const elapsed = quota.block_seconds > 0 ? 100 - Math.min(100, (secondsLeft / quota.block_seconds) * 100) : 100;
    return (
      <div role="status" aria-live="polite" className="mb-2 overflow-hidden rounded-xl border border-border-strong bg-surface-raised shadow-soft">
        <div className="flex items-center gap-2.5 px-3 py-2">
          <Clock className="h-4 w-4 shrink-0 text-muted" strokeWidth={1.75} />
          <p className="min-w-0 flex-1 truncate text-xs text-muted">
            <span className="font-semibold text-foreground">Limit reached.</span> Chat unlocks in {pause}
            {resetsAt ? ` (${resetsAt.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })})` : ""} and refills automatically.
          </p>
          <span className="shrink-0 text-sm font-semibold tabular-nums text-foreground">{formatCountdown(secondsLeft)}</span>
        </div>
        <div className="h-0.5 bg-border" aria-hidden>
          <div className="h-full bg-foreground transition-all duration-1000 ease-linear" style={{ width: `${elapsed}%` }} />
        </div>
      </div>
    );
  }

  const low = quota.messages_left <= 2;
  return (
    <div
      role="status"
      aria-live="polite"
      title={`Each message uses ${quota.cost} credits. When they run out, chat pauses for ${pause} and then refills.`}
      className={clsx(
        "mb-2 flex items-center gap-2.5 rounded-xl border bg-surface-raised px-3 py-2 shadow-soft transition-colors",
        low ? "border-warning" : "border-border-strong",
      )}
    >
      <Zap className={clsx("h-4 w-4 shrink-0", low ? "text-warning" : "text-foreground")} strokeWidth={2} />
      <p className="shrink-0 text-sm font-semibold text-foreground">
        <span className="tabular-nums">{quota.messages_left}</span> {quota.messages_left === 1 ? "message" : "messages"} left
      </p>
      {totalMessages <= 24 && (
        <div className="hidden min-w-0 flex-1 sm:block">
          <MessagePips total={totalMessages} left={quota.messages_left} low={low} />
        </div>
      )}
      <p className={clsx("ml-auto min-w-0 truncate text-[11px]", low ? "text-warning" : "text-muted")}>
        {low ? `then a ${pause} pause` : `${quota.remaining} / ${quota.limit} credits`}
      </p>
    </div>
  );
}

/** Shown on an empty chat so someone who has never used the app understands the limit before they hit it. */
export function UsageExplainer({ quota }: { quota: Quota }) {
  const total = Math.max(1, Math.floor(quota.limit / quota.cost));
  const steps = [
    { icon: MessageSquare, title: "Ask a question" },
    { icon: Gauge, title: `You have ${plural(total, "message")}` },
    { icon: Clock, title: "Then a short break" },
  ];
  return (
    <div className="mt-5 w-full max-w-md rounded-xl border border-border bg-surface-raised px-3 py-2.5 shadow-soft">
      <div className="flex flex-col gap-1.5 sm:flex-row sm:items-center sm:justify-between sm:gap-2">
        {steps.map((step, i) => (
          <div key={step.title} className="flex min-w-0 items-center gap-1.5">
            <span className="flex h-5 w-5 shrink-0 items-center justify-center rounded-md bg-surface text-muted">
              <step.icon className="h-3 w-3" strokeWidth={1.75} />
            </span>
            <p className="truncate text-[11px] font-medium text-foreground">
              <span className="text-muted">{i + 1}.</span> {step.title}
            </p>
          </div>
        ))}
      </div>
    </div>
  );
}
