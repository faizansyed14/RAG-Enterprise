"use client";

import { MessageSquare, Plus, Trash2 } from "lucide-react";
import type { ChatSession } from "@/lib/chatStore";

interface Props {
  sessions: ChatSession[];
  onStartNew: () => void;
  onOpen: (id: string) => void;
  onDelete: (id: string, event: React.MouseEvent) => void;
}

function formatWhen(ts: number): string {
  const diff = Date.now() - ts;
  const mins = Math.floor(diff / 60_000);
  if (mins < 1) return "Just now";
  if (mins < 60) return `${mins}m ago`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  if (days < 7) return `${days}d ago`;
  return new Date(ts).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

export function ChatsHome({ sessions, onStartNew, onOpen, onDelete }: Props) {
  const history = sessions.filter((s) => s.messages.length > 0);

  return (
    <div className="flex h-full min-h-0 flex-col overflow-hidden bg-background">
      <div className="shrink-0 border-b border-border bg-surface-raised px-4 py-5 sm:px-6">
        <div className="mx-auto flex max-w-3xl items-center justify-between gap-4">
          <div className="min-w-0">
            <div className="text-[10px] font-semibold uppercase tracking-[0.14em] text-accent">Conversations</div>
            <h1 className="font-display mt-1 text-2xl font-medium tracking-[-0.03em] sm:text-3xl">Chats</h1>
            <p className="mt-0.5 truncate text-sm text-muted">Start a new conversation or continue where you left off.</p>
          </div>
          <button
            type="button"
            onClick={onStartNew}
            className="inline-flex shrink-0 items-center gap-2 rounded-lg bg-accent px-4 py-2.5 text-sm font-semibold text-accent-fg transition hover:-translate-y-0.5 hover:shadow-md"
          >
            <Plus className="h-4 w-4" strokeWidth={1.75} />
            Start new chat
          </button>
        </div>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto px-4 py-6 sm:px-6">
        <div className="mx-auto max-w-3xl">
          <div className="mb-3 text-xs font-medium uppercase tracking-wide text-muted">History</div>

          {history.length === 0 ? (
            <div className="rounded-xl border border-dashed border-border-strong bg-surface-raised px-6 py-14 text-center">
              <MessageSquare className="mx-auto mb-3 h-8 w-8 text-muted" />
              <p className="text-sm font-medium">No chats yet</p>
              <p className="mt-1 text-xs text-muted">Start a new chat to begin asking about your documents.</p>
            </div>
          ) : (
            <ul className="divide-y divide-border overflow-hidden rounded-xl border border-border bg-surface-raised shadow-soft">
              {history.map((session) => (
                <li key={session.id} className="group flex items-center">
                  <button
                    type="button"
                    onClick={() => onOpen(session.id)}
                    className="min-w-0 flex-1 px-4 py-3.5 text-left transition hover:bg-surface"
                  >
                    <span className="block truncate text-sm font-medium text-foreground">{session.title}</span>
                    <span className="mt-0.5 block text-xs text-muted">{formatWhen(session.updatedAt)}</span>
                  </button>
                  <button
                    type="button"
                    onClick={(event) => onDelete(session.id, event)}
                    className="mr-3 rounded-lg p-2 text-muted opacity-0 transition hover:bg-danger-soft hover:text-danger group-hover:opacity-100 focus:opacity-100"
                    aria-label="Delete chat"
                  >
                    <Trash2 className="h-3.5 w-3.5" />
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>
      </div>
    </div>
  );
}
