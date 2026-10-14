"use client";

import { useEffect, useState } from "react";
import { AnimatePresence, motion, useReducedMotion } from "framer-motion";
import { Check, Volume2, VolumeX } from "lucide-react";
import { isSpeechSupported } from "@/lib/speech";
import type { ChatMessage, Citation, DiagramEvidence } from "@/lib/types";
import { AnswerText } from "./AnswerText";
import { CitationSources } from "./CitationSources";
import { DiagramChip } from "./DiagramChip";

interface Props {
  message: ChatMessage;
  onCitationClick: (citation: Citation) => void;
  onDiagramClick: (evidence: DiagramEvidence) => void;
  isSpeaking: boolean;
  onToggleSpeak: () => void;
}

const TOOL_LABELS: Record<string, string> = {
  browse_documents: "Browsing documents",
  search_content: "Searching page text",
  get_document: "Opening a document",
  get_document_structure: "Reading structure",
  get_page_content: "Reading pages",
  search_diagrams: "Scanning diagrams",
  search: "Searching documents",
};

function toolLabel(name: string): string {
  return TOOL_LABELS[name] ?? name.replace(/_/g, " ");
}

export function MessageBubble({ message, onCitationClick, onDiagramClick, isSpeaking, onToggleSpeak }: Props) {
  const isUser = message.role === "user";
  const tools = (message.toolActivity ?? []).filter((t) => !t.name.endsWith("→"));
  const hasAnswer = Boolean(message.resolvedContent || message.content);
  const answerText = message.resolvedContent || message.content;
  const [speechSupported, setSpeechSupported] = useState(false);
  useEffect(() => setSpeechSupported(isSpeechSupported()), []);
  const reduced = useReducedMotion();
  const fadeUp = {
    initial: reduced ? false : { opacity: 0, y: 6 },
    animate: { opacity: 1, y: 0 },
    transition: { duration: 0.28, ease: "easeOut" as const },
  };

  if (isUser) {
    return (
      <motion.div {...fadeUp} className="flex justify-end">
        <div className="max-w-[min(85%,36rem)] rounded-2xl rounded-br-md bg-bubble-user px-4 py-3 text-sm leading-relaxed text-bubble-user-fg shadow-soft">
          <p className="whitespace-pre-wrap">{message.content}</p>
        </div>
      </motion.div>
    );
  }

  return (
    <motion.div {...fadeUp} className="relative pl-9">
      <div className="absolute left-0 top-0 flex h-6 w-6 items-center justify-center rounded-md bg-accent text-[9px] font-bold text-accent-fg">R</div>
      <div className="mb-3 text-[10px] font-semibold uppercase tracking-[0.12em] text-muted">RAG response</div>

      <div className="text-sm leading-7 text-foreground">
        {message.streaming && <ResponseStatus message={message} tools={tools} />}
        {answerText ? (
          <AnswerText
            text={answerText}
            citations={message.citations ?? []}
            onCitationClick={onCitationClick}
          />
        ) : null}

        {message.diagrams && message.diagrams.length > 0 && (
          <div className="mt-3 flex flex-wrap gap-1.5">
            {message.diagrams.map((d, i) => (
              <DiagramChip key={`d-${i}`} evidence={d} onClick={() => onDiagramClick(d)} />
            ))}
          </div>
        )}
        {message.citations && message.citations.length > 0 && (
          <CitationSources citations={message.citations} onOpen={onCitationClick} />
        )}

        {hasAnswer && !message.streaming && speechSupported && (
          <button
            type="button"
            onClick={onToggleSpeak}
            aria-label={isSpeaking ? "Stop reading aloud" : "Read aloud"}
            title={isSpeaking ? "Stop reading aloud" : "Read aloud"}
            className={`mt-3 inline-flex items-center gap-1.5 rounded-lg px-2 py-1.5 text-[11px] font-medium transition ${
              isSpeaking ? "bg-accent-soft text-accent" : "text-muted hover:bg-surface hover:text-foreground"
            }`}
          >
            {isSpeaking ? <VolumeX className="h-3.5 w-3.5" /> : <Volume2 className="h-3.5 w-3.5" />}
            {isSpeaking ? "Stop" : "Listen"}
          </button>
        )}
      </div>
    </motion.div>
  );
}

function ResponseStatus({
  message,
  tools,
}: {
  message: ChatMessage;
  tools: { callId?: string; name: string; detail: string; done?: boolean }[];
}) {
  const reduced = useReducedMotion();
  const hasDraft = Boolean(message.content || message.resolvedContent);

  const status = message.resolvedContent
    ? "Linking sources"
    : message.content
      ? "Writing"
      : tools.length > 0
        ? toolLabel(tools[tools.length - 1].name)
        : "Thinking";

  // Skeleton only while waiting for the first tokens — keeps the stream itself clean.
  if (!hasDraft) {
    return (
      <div className="mb-1" role="status" aria-live="polite" aria-label={status}>
        <div className="mb-3 flex items-center gap-2.5">
          <span className="relative flex h-2 w-2 shrink-0">
            {!reduced && (
              <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-foreground/25" />
            )}
            <span className="relative h-2 w-2 rounded-full bg-foreground/70" />
          </span>
          <span className="text-[13px] font-medium tracking-[-0.01em] text-foreground/80">
            {status}
            <span className="ml-0.5 inline-flex w-4 justify-start overflow-hidden" aria-hidden="true">
              <span className={reduced ? "" : "status-ellipsis"}>...</span>
            </span>
          </span>
        </div>
        {tools.length > 0 ? (
          <ul className="space-y-1.5 pr-8">
            <AnimatePresence initial={false}>
              {tools.map((tool, i) => (
                <motion.li
                  key={tool.callId ?? i}
                  initial={reduced ? false : { opacity: 0, y: 4 }}
                  animate={{ opacity: 1, y: 0 }}
                  transition={{ duration: 0.22, ease: "easeOut" }}
                  className="flex items-center gap-2 text-[12px]"
                >
                  <span className="relative flex h-3 w-3 shrink-0 items-center justify-center">
                    {tool.done ? (
                      <Check className="h-3 w-3 text-foreground/50" strokeWidth={2.5} />
                    ) : (
                      <>
                        {!reduced && (
                          <span className="absolute inline-flex h-2 w-2 animate-ping rounded-full bg-foreground/25" />
                        )}
                        <span className="relative h-1.5 w-1.5 rounded-full bg-foreground/60" />
                      </>
                    )}
                  </span>
                  <span className={tool.done ? "text-muted" : "font-medium text-foreground/80"}>
                    {toolLabel(tool.name)}
                  </span>
                  {tool.detail && <span className="truncate text-muted/70">{tool.detail}</span>}
                </motion.li>
              ))}
            </AnimatePresence>
          </ul>
        ) : (
          <div className="space-y-2.5 pr-8">
            <div className={`h-3 max-w-[22rem] rounded-md ${reduced ? "bg-surface" : "status-shimmer"}`} />
            <div className={`h-3 max-w-[18rem] rounded-md ${reduced ? "bg-surface" : "status-shimmer"}`} />
            <div className={`h-3 max-w-[12rem] rounded-md ${reduced ? "bg-surface" : "status-shimmer"}`} />
          </div>
        )}
      </div>
    );
  }

  return (
    <div className="mb-3 flex items-center gap-2" role="status" aria-live="polite" aria-label={status}>
      <span className="relative flex h-1.5 w-1.5 shrink-0">
        {!reduced && <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-foreground/20" />}
        <span className="relative h-1.5 w-1.5 rounded-full bg-foreground/50" />
      </span>
      <span className="text-[11px] font-medium text-muted">{status}</span>
    </div>
  );
}
