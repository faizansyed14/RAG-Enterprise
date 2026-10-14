"use client";

import type { JSX, ReactNode } from "react";
import type { Citation } from "@/lib/types";

function fileTag(name: string): string {
  return name.slice(0, 5);
}

function renderInline(
  text: string,
  citations: Map<number, Citation>,
  onCitationClick: (citation: Citation) => void,
  keyPrefix: string,
): ReactNode[] {
  const tokenPattern = /(\[\[(\d+)\]\]\(#rag-citation-\d+\))|(\*\*[^*\n]+\*\*|__[^_\n]+__)|(`[^`\n]+`)|(\[[^\]\n]+\]\(https?:\/\/[^\s)]+\))|(\*[^*\n]+\*|_[^_\n]+_)/g;
  const parts: ReactNode[] = [];
  let lastEnd = 0;
  let match: RegExpExecArray | null;
  let key = 0;

  while ((match = tokenPattern.exec(text)) !== null) {
    if (match.index > lastEnd) parts.push(text.slice(lastEnd, match.index));

    const token = match[0];
    const citationMatch = /^\[\[(\d+)\]\]/.exec(token);
    if (citationMatch) {
      const index = Number(citationMatch[1]);
      const citation = citations.get(index);
      parts.push(
        citation ? (
          <button
            key={`${keyPrefix}-citation-${key++}`}
            type="button"
            onClick={() => onCitationClick(citation)}
            title={`${citation.document} · page ${citation.page}`}
            className="mx-0.5 inline-flex h-[17px] items-center rounded bg-accent px-1.5 align-super text-[9px] font-semibold uppercase leading-none tracking-wide text-accent-fg transition hover:opacity-80"
          >
            {fileTag(citation.document)}
          </button>
        ) : (
          <span
            key={`${keyPrefix}-citation-missing-${key++}`}
            className="align-super mx-px inline-flex h-[15px] min-w-[15px] items-center justify-center rounded-full text-[9px] font-medium leading-none text-muted/60"
          >
            {index}
          </span>
        ),
      );
    } else if (/^(\*\*|__).+\1$/.test(token)) {
      parts.push(<strong key={`${keyPrefix}-strong-${key++}`}>{token.slice(2, -2)}</strong>);
    } else if (token.startsWith("`") && token.endsWith("`")) {
      parts.push(
        <code key={`${keyPrefix}-code-${key++}`} className="rounded bg-surface px-1.5 py-0.5 font-mono text-[0.9em] text-foreground">
          {token.slice(1, -1)}
        </code>,
      );
    } else if (token.startsWith("[") && token.includes("](")) {
      const linkMatch = /^\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)$/.exec(token);
      if (linkMatch) {
        parts.push(
          <a
            key={`${keyPrefix}-link-${key++}`}
            href={linkMatch[2]}
            target="_blank"
            rel="noopener noreferrer nofollow"
            className="font-medium text-accent underline decoration-accent/40 underline-offset-2 hover:decoration-accent"
          >
            {linkMatch[1]}
          </a>,
        );
      } else {
        parts.push(token);
      }
    } else if (/^(\*|_).+\1$/.test(token)) {
      parts.push(<em key={`${keyPrefix}-em-${key++}`}>{token.slice(1, -1)}</em>);
    } else {
      parts.push(token);
    }

    lastEnd = match.index + token.length;
  }

  if (lastEnd < text.length) parts.push(text.slice(lastEnd));
  return parts;
}

function renderMarkdown(
  text: string,
  citations: Map<number, Citation>,
  onCitationClick: (citation: Citation) => void,
): ReactNode[] {
  const lines = text.replace(/\r\n?/g, "\n").split("\n");
  const blocks: ReactNode[] = [];
  let paragraph: string[] = [];
  let listItems: string[] = [];
  let listType: "ul" | "ol" | null = null;
  let quoteLines: string[] = [];
  let codeLines: string[] = [];
  let inCode = false;
  let blockKey = 0;

  const flushParagraph = () => {
    if (paragraph.length === 0) return;
    blocks.push(
      <p key={`paragraph-${blockKey++}`} className="text-left leading-7">
        {renderInline(paragraph.join(" ").trim(), citations, onCitationClick, `paragraph-${blockKey}`)}
      </p>,
    );
    paragraph = [];
  };

  const flushList = () => {
    if (!listType || listItems.length === 0) return;
    const List = listType;
    blocks.push(
      <List key={`list-${blockKey++}`} className={`space-y-1.5 pl-6 text-left leading-7 marker:text-muted ${listType === "ol" ? "list-decimal" : "list-disc"}`}>
        {listItems.map((item, index) => (
          <li key={`${listType}-${index}`}>{renderInline(item, citations, onCitationClick, `list-${blockKey}-${index}`)}</li>
        ))}
      </List>,
    );
    listItems = [];
    listType = null;
  };

  const flushQuote = () => {
    if (quoteLines.length === 0) return;
    blocks.push(
      <blockquote key={`quote-${blockKey++}`} className="border-l-2 border-accent/40 pl-4 text-left italic leading-7 text-muted">
        {renderInline(quoteLines.join(" ").trim(), citations, onCitationClick, `quote-${blockKey}`)}
      </blockquote>,
    );
    quoteLines = [];
  };

  const flushCode = () => {
    if (codeLines.length === 0) return;
    blocks.push(
      <pre key={`code-block-${blockKey++}`} className="overflow-x-auto rounded-lg bg-surface p-3 text-left font-mono text-xs leading-6 text-foreground">
        <code>{codeLines.join("\n")}</code>
      </pre>,
    );
    codeLines = [];
  };

  const flushOpenBlocks = () => {
    flushParagraph();
    flushList();
    flushQuote();
  };

  for (const line of lines) {
    if (/^\s*```/.test(line)) {
      if (inCode) flushCode();
      else flushOpenBlocks();
      inCode = !inCode;
      continue;
    }
    if (inCode) {
      codeLines.push(line);
      continue;
    }

    if (/^\s*$/.test(line)) {
      flushOpenBlocks();
      continue;
    }

    const heading = /^(#{1,6})\s+(.+)$/.exec(line);
    if (heading) {
      flushOpenBlocks();
      const level = heading[1].length;
      const Heading = (`h${level}` as keyof JSX.IntrinsicElements);
      blocks.push(
        <Heading key={`heading-${blockKey++}`} className="text-left font-semibold tracking-[-0.01em] text-foreground">
          {renderInline(heading[2], citations, onCitationClick, `heading-${blockKey}`)}
        </Heading>,
      );
      continue;
    }

    if (/^\s*([-*_])(?:\s*\1){2,}\s*$/.test(line)) {
      flushOpenBlocks();
      blocks.push(<hr key={`rule-${blockKey++}`} className="border-border" />);
      continue;
    }

    const unordered = /^\s*[-*+]\s+(.+)$/.exec(line);
    const ordered = /^\s*\d+[.)]\s+(.+)$/.exec(line);
    if (unordered || ordered) {
      flushParagraph();
      flushQuote();
      const nextType = unordered ? "ul" : "ol";
      if (listType && listType !== nextType) flushList();
      listType = nextType;
      listItems.push((unordered ?? ordered)![1].trim());
      continue;
    }

    const quote = /^\s*>\s?(.*)$/.exec(line);
    if (quote) {
      flushParagraph();
      flushList();
      quoteLines.push(quote[1]);
      continue;
    }

    flushList();
    flushQuote();
    paragraph.push(line.trim());
  }

  if (inCode) flushCode();
  flushOpenBlocks();
  return blocks;
}

/** Renders assistant Markdown while keeping citation markers interactive. */
export function AnswerText({
  text,
  citations,
  onCitationClick,
}: {
  text: string;
  citations: Citation[];
  onCitationClick: (citation: Citation) => void;
}) {
  const byIndex = new Map<number, Citation>(
    citations
      .filter((citation): citation is Citation & { index: number } => typeof citation.index === "number")
      .map((citation) => [citation.index, citation]),
  );
  return <div className="space-y-4 text-left">{renderMarkdown(text, byIndex, onCitationClick)}</div>;
}
