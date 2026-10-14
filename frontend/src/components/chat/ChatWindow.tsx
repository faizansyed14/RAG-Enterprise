"use client";

import { useEffect, useRef, useState } from "react";
import { ArrowUp, Mic, Square } from "lucide-react";
import { streamChat } from "@/lib/api";
import { isMicSupported, speak, stopSpeaking, useMicInput } from "@/lib/speech";
import type { ChatEvent, ChatMessage, Citation, DiagramEvidence, DocumentOut, Folder, Quota } from "@/lib/types";
import { CreditsMeter, UsageExplainer, formatCountdown, useQuotaCountdown, type QuotaState } from "./CreditsMeter";
import { DocumentPicker } from "./DocumentPicker";
import { MessageBubble } from "./MessageBubble";
import { VoiceRecorderBar } from "./VoiceRecorderBar";

interface Props {
  sessionId: string;
  initialMessages: ChatMessage[];
  initialSelectedIds: string[] | null;
  documentIds: string[] | null;
  documents: DocumentOut[];
  folders: Folder[];
  onDocumentsChanged: () => void;
  onCitationClick: (citation: Citation) => void;
  onDiagramClick: (evidence: DiagramEvidence) => void;
  onSessionUpdate: (patch: { messages: ChatMessage[]; selectedDocIds: string[] | null }) => void;
  /** Present only for metered (non-admin) users. */
  quotaState: QuotaState | null;
  onQuota: (quota: Quota) => void;
  onQuotaExpired: () => void;
  onUnauthorized: () => void;
  canUpload: boolean;
}

function summarizeArgs(args: unknown): string {
  if (args == null) return "";
  if (typeof args === "string") return args.slice(0, 100);
  if (typeof args === "object" && !Array.isArray(args)) {
    const o = args as Record<string, unknown>;
    const parts: string[] = [];
    if (typeof o.doc_name === "string") parts.push(o.doc_name);
    if (o.pages != null) parts.push(`p.${String(o.pages)}`);
    if (parts.length) return parts.join(" | ").slice(0, 100);
  }
  try {
    return JSON.stringify(args).slice(0, 80);
  } catch {
    return "";
  }
}

export function ChatWindow({
  sessionId,
  initialMessages,
  initialSelectedIds,
  documentIds,
  documents,
  folders,
  onDocumentsChanged,
  onCitationClick,
  onDiagramClick,
  onSessionUpdate,
  quotaState,
  onQuota,
  onQuotaExpired,
  onUnauthorized,
  canUpload,
}: Props) {
  const [messages, setMessages] = useState<ChatMessage[]>(initialMessages);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  // null = no selection made (every ready document); [] = "Deselect all", an explicit choice of
  // nothing that must never be quietly turned back into "all" (here or on the server).
  const [selectedIds, setSelectedIds] = useState<string[] | null>(initialSelectedIds ?? null);
  const nothingSelected = selectedIds !== null && selectedIds.length === 0;
  const [speakingId, setSpeakingId] = useState<string | null>(null);
  // Client-only, after mount -- see MessageBubble's speechSupported for why.
  const [micSupported, setMicSupported] = useState(false);
  const bottomRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  useEffect(() => setMicSupported(isMicSupported()), []);

  const { blocked, secondsLeft, resetsAt } = useQuotaCountdown(quotaState, onQuotaExpired);

  const { listening, interimText, seconds, start: startMicRaw, stop: stopMic } = useMicInput();
  const startMic = () =>
    startMicRaw((text) => {
      setInput((prev) => (prev ? `${prev.trim()} ${text}` : text).trim());
      resizeTextarea();
    });

  const toggleSpeak = (id: string, text: string) => {
    if (speakingId === id) {
      stopSpeaking();
      setSpeakingId(null);
      return;
    }
    setSpeakingId(id);
    speak(text, () => setSpeakingId((current) => (current === id ? null : current)));
  };

  useEffect(() => {
    setMessages(initialMessages);
    setSelectedIds(initialSelectedIds ?? null);
    setInput("");
    setBusy(false);
  }, [sessionId]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  useEffect(() => {
    onSessionUpdate({ messages, selectedDocIds: selectedIds });
  }, [messages, selectedIds]); // eslint-disable-line react-hooks/exhaustive-deps

  const resizeTextarea = () => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 160)}px`;
  };

  const send = async (override?: string) => {
    const query = (override ?? input).trim();
    if (!query || busy || blocked || nothingSelected) return;
    setNotice(null);
    if (listening) stopMic();

    const history = messages
      .filter((m) => m.content || m.resolvedContent)
      .map((m) => ({ role: m.role, content: m.resolvedContent ?? m.content }));

    const userMessage: ChatMessage = { id: crypto.randomUUID(), role: "user", content: query };
    const assistantId = crypto.randomUUID();
    setMessages((prev) => [
      ...prev,
      userMessage,
      { id: assistantId, role: "assistant", content: "", streaming: true, toolActivity: [] },
    ]);
    setInput("");
    if (textareaRef.current) textareaRef.current.style.height = "auto";
    setBusy(true);

    const patchAssistant = (update: (m: ChatMessage) => ChatMessage) => {
      setMessages((prev) => prev.map((m) => (m.id === assistantId ? update(m) : m)));
    };

    const onEvent = (event: ChatEvent) => {
      switch (event.type) {
        case "answer":
          if (event.delta) patchAssistant((m) => ({ ...m, content: m.content + event.delta }));
          break;
        case "tool_call":
          patchAssistant((m) => ({
            ...m,
            toolActivity: [
              ...(m.toolActivity ?? []),
              { callId: event.call_id, name: event.name ?? "tool", detail: summarizeArgs(event.arguments), done: false },
            ],
          }));
          break;
        case "tool_result":
          patchAssistant((m) => ({
            ...m,
            toolActivity: (m.toolActivity ?? []).map((t) =>
              t.callId && t.callId === event.call_id ? { ...t, done: true } : t
            ),
          }));
          break;
        case "citations":
          patchAssistant((m) => ({
            ...m,
            citations: event.citations ?? [],
            diagrams: event.diagrams ?? [],
            resolvedContent: event.resolved_answer,
          }));
          break;
        case "done":
          patchAssistant((m) => ({ ...m, streaming: false }));
          setBusy(false);
          break;
        case "usage":
          if (event.quota) onQuota(event.quota);
          break;
        case "limit":
          // Rejected before anything ran: drop the placeholder bubbles and give the
          // text back to the composer so nothing the user typed is lost.
          setMessages((prev) => prev.filter((m) => m.id !== userMessage.id && m.id !== assistantId));
          setInput(query);
          if (event.quota) onQuota(event.quota);
          setBusy(false);
          break;
        case "throttled": {
          // Too many messages too quickly: nothing was spent. Give the text back.
          setMessages((prev) => prev.filter((m) => m.id !== userMessage.id && m.id !== assistantId));
          setInput(query);
          const wait = event.retry_after_seconds ?? 5;
          setNotice(`You're sending messages too quickly. Try again in ${wait} second${wait === 1 ? "" : "s"}.`);
          setBusy(false);
          break;
        }
        case "error":
          patchAssistant((m) => ({
            ...m,
            content: m.content || `Error: ${event.message ?? "Please try again."}`,
            streaming: false,
          }));
          setBusy(false);
          break;
      }
    };

    try {
      await streamChat(query, selectedIds ?? documentIds, onEvent, undefined, history);
    } catch (error) {
      if (error instanceof Error && error.message === "unauthorized") {
        onUnauthorized();
        return;
      }
      patchAssistant((m) => ({
        ...m,
        content: m.content || "Could not reach the service. Please try again.",
        streaming: false,
      }));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="relative flex h-full min-h-0 flex-col bg-background">
      <div className="flex-1 overflow-y-auto">
        {messages.length === 0 ? (
          <div className="mx-auto flex min-h-full max-w-3xl flex-col items-center justify-center px-4 py-14 sm:px-6">
            <div className="mb-5 inline-flex items-center gap-2 rounded-full border border-border bg-surface-raised px-3 py-1.5 text-[10px] font-semibold uppercase tracking-[0.12em] text-muted shadow-soft">
              <span className="h-1.5 w-1.5 rounded-full bg-success" /> Knowledge workspace
            </div>
            <h1 className="font-display text-balance text-center text-4xl font-medium tracking-[-0.035em] text-foreground sm:text-5xl">
              What do you need to know?
            </h1>
            {quotaState && <UsageExplainer quota={quotaState.quota} />}
          </div>
        ) : (
          <div className="mx-auto w-full max-w-3xl space-y-7 px-4 py-8 sm:px-6 sm:py-10">
            {messages.map((m) => (
              <MessageBubble
                key={m.id}
                message={m}
                onCitationClick={onCitationClick}
                onDiagramClick={onDiagramClick}
                isSpeaking={speakingId === m.id}
                onToggleSpeak={() => toggleSpeak(m.id, m.resolvedContent ?? m.content)}
              />
            ))}
            <div ref={bottomRef} />
          </div>
        )}
      </div>

      <div className="shrink-0 bg-gradient-to-t from-background via-background to-transparent px-3 pb-3 pt-3 sm:px-4 sm:pb-5">
        <div className="mx-auto max-w-3xl">
          {notice && (
            <p role="status" className="mb-2 rounded-lg border border-border bg-surface px-3 py-2 text-xs text-muted">
              {notice}
            </p>
          )}
          {quotaState && (
            <CreditsMeter state={quotaState} blocked={blocked} secondsLeft={secondsLeft} resetsAt={resetsAt} />
          )}
          {nothingSelected && (
            <p role="status" className="mb-2 rounded-lg border border-border bg-surface px-3 py-2 text-xs text-muted">
              No documents are selected. Choose at least one source below to ask a question.
            </p>
          )}
          <div className="mb-2">
            <DocumentPicker
              documents={documents}
              folders={folders}
              selectedIds={selectedIds}
              onChange={setSelectedIds}
              onUploaded={onDocumentsChanged}
              canUpload={canUpload}
            />
          </div>
          <div className="flex items-end gap-2 rounded-2xl border border-border-strong bg-composer px-3 py-2 shadow-soft transition focus-within:border-accent focus-within:ring-4 focus-within:ring-accent-soft">
            {listening ? (
              <VoiceRecorderBar liveText={interimText} seconds={seconds} />
            ) : (
              <textarea
                ref={textareaRef}
                rows={1}
                value={input}
                onChange={(e) => {
                  setInput(e.target.value);
                  resizeTextarea();
                }}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && !e.shiftKey) {
                    e.preventDefault();
                    send();
                  }
                }}
                disabled={blocked}
                placeholder={
                  blocked
                    ? `Limit reached - resets in ${formatCountdown(secondsLeft)}`
                    : nothingSelected
                      ? "Select at least one document to ask a question"
                      : "Ask a question about your documents"
                }
                aria-label="Message"
                className="max-h-40 min-h-[44px] flex-1 resize-none bg-transparent px-2 py-2.5 text-sm leading-5 text-foreground outline-none placeholder:text-muted"
              />
            )}
            {micSupported && !blocked && (
              <button
                type="button"
                onClick={() => (listening ? stopMic() : startMic())}
                aria-label={listening ? "Stop voice input" : "Voice input"}
                title={listening ? "Stop voice input" : "Voice input"}
                className={`mb-1 flex h-9 w-9 shrink-0 items-center justify-center rounded-full transition ${
                  listening
                    ? "bg-danger text-white"
                    : "text-muted hover:bg-surface hover:text-foreground"
                }`}
              >
                {listening ? <Square className="h-3.5 w-3.5" fill="currentColor" /> : <Mic className="h-4 w-4" strokeWidth={2} />}
              </button>
            )}
            <button
              type="button"
              onClick={() => send()}
              disabled={busy || blocked || nothingSelected || !input.trim()}
              title={nothingSelected ? "Select at least one document first" : undefined}
              aria-label="Send"
              className="mb-1 flex h-9 w-9 shrink-0 items-center justify-center rounded-lg bg-accent text-accent-fg transition hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-30"
            >
              <ArrowUp className="h-4 w-4" strokeWidth={2.25} />
            </button>
          </div>
          <p className="mt-2 text-center text-[11px] text-muted">
            Answers may need review. Open citations to verify source evidence.
          </p>
        </div>
      </div>
    </div>
  );
}
