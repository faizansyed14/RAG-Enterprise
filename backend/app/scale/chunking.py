"""Structure-aware chunking that keeps the one thing the product promises: a page-level citation.

Rules
  * A chunk never crosses a page boundary, so every hit maps to exactly one cited page.
  * Windows are ~SCALE_CHUNK_WORDS words with SCALE_CHUNK_OVERLAP_WORDS of overlap, and prefer to
    end on a paragraph / table boundary rather than mid-sentence.
  * A chunk records character offsets into its page, so its text can be re-read from Postgres
    (document_pages) instead of being stored a second time in the vector index.
  * The text that is *embedded* carries the document name, folder path and nearest heading
    ("context"), because a chunk that says "the retention period is 7 years" is unanswerable
    without knowing which policy it belongs to. The stored/cited text stays exactly as extracted.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.scale.settings import ScaleSettings, get_scale_settings

_WORD = re.compile(r"\S+")
_MD_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$")
_NUM_HEADING = re.compile(r"^\s*(?:\d+(?:\.\d+){0,4}[.)]?|[A-Z]\.|Article\s+\d+|Section\s+\d+)\s+[A-Z][^.!?]{2,90}$")


_DUP_SUFFIX = re.compile(r"\s*\[[0-9a-f]{8}\]")      # '[2a3f29d1]' added when two documents share a name
_EXT = re.compile(r"\.[A-Za-z0-9]{1,5}$")
_GENERIC_NAMES = {"index", "readme", "_index", "document", "untitled", "new document", "page", "main", "home", "default"}
_FRONT_MATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.S)
_FM_TITLE = re.compile(r"^title:\s*(.+?)\s*$", re.M)


def clean_name(name: str) -> str:
    """A name as a person would say it: no '[2a3f29d1]' de-duplication suffix, no extension."""
    return _EXT.sub("", _DUP_SUFFIX.sub("", name or "")).strip()


def extract_title(first_page: str, name: str = "") -> str:
    """The document's own title: front-matter `title:`, else the first Markdown heading, else a short first
    line (news and licence texts start with one). Returns '' when the first line looks like body text."""
    text = (first_page or "")[:6000]
    match = _FRONT_MATTER.match(text)
    if match:
        found = _FM_TITLE.search(match.group(1))
        if found:
            return found.group(1).strip().strip("\"'")[:160]
        text = text[match.end():]
    for line in text.splitlines()[:40]:
        line = line.strip()
        if not line:
            continue
        heading = _MD_HEADING.match(line)
        if heading:
            return heading.group(1).strip()[:160]
        words = line.split()
        if 1 <= len(words) <= 16 and not line.endswith((".", ",", ";")):
            return line[:160]
        break
    return ""


@dataclass(frozen=True)
class Chunk:
    page: int          # 1-based page number in the stored document
    index: int         # position of the chunk within the document (0-based)
    start: int         # character offset into the page text
    end: int
    text: str          # exactly page_text[start:end]
    heading: str       # nearest heading above the chunk ('' if none)
    embed_text: str    # context + text: what gets embedded / sparse-encoded
    context: str = ""  # the context prefix alone: document title | path | heading


def _is_heading(line: str) -> str | None:
    line = line.rstrip()
    if not line or len(line.split()) > 14:
        return None
    match = _MD_HEADING.match(line)
    if match:
        return match.group(1).strip()
    if _NUM_HEADING.match(line) and not line.endswith((".", ",", ";", ":")):
        return line.strip()
    return None


def _boundaries(text: str, spans: list[tuple[int, int]]) -> set[int]:
    """Word indices that start a paragraph, a table row block or a heading."""
    starts: set[int] = set()
    for j in range(1, len(spans)):
        gap = text[spans[j - 1][1]: spans[j][0]]
        if "\n\n" in gap:
            starts.add(j)
        elif "\n" in gap and (text[spans[j][0]] in "#|" or _is_heading(_line_at(text, spans[j][0])) is not None):
            starts.add(j)
    return starts


def _line_at(text: str, pos: int) -> str:
    end = text.find("\n", pos)
    return text[pos: end if end != -1 else len(text)]


def _heading_before(text: str, pos: int, carried: str) -> str:
    """The last heading that starts before `pos` on this page, else the one carried from earlier pages."""
    heading = carried
    line_end = text.find("\n", pos)
    # Include the line the chunk starts on: a chunk that opens with its own heading belongs to it.
    for line in text[: line_end if line_end != -1 else len(text)].split("\n"):
        found = _is_heading(line)
        if found:
            heading = found
    return heading


def context_prefix(title: str, path: str, heading: str, doc_title: str = "") -> str:
    """name | own title (if different) | folder tail | heading: what a person would use to say which document
    this is. A name such as 'index.md' or 'Document1.docx' says nothing, so the folder and the own title carry it."""
    name = clean_name(title)
    own = doc_title.strip()
    parts: list[str] = []
    if name and name.casefold() not in _GENERIC_NAMES:
        parts.append(name)
    if own and own.casefold() != name.casefold():
        parts.append(own)
    tail = _path_tail(path)
    if tail:
        parts.append(tail)
    if heading and heading.casefold() not in {p.casefold() for p in parts}:
        parts.append(heading)
    return " | ".join(parts)


def _path_tail(path: str, keep: int = 4) -> str:
    segments = [s for s in re.split(r"[\\/]+", path or "") if s]
    return "/".join(segments[-keep - 1: -1]) if len(segments) > 1 else ""


def chunk_page(
    text: str,
    page: int,
    *,
    first_index: int = 0,
    title: str = "",
    path: str = "",
    carried_heading: str = "",
    doc_title: str = "",
    cfg: ScaleSettings | None = None,
) -> list[Chunk]:
    cfg = cfg or get_scale_settings()
    window, overlap, minimum = cfg.chunk_words, cfg.chunk_overlap_words, cfg.min_chunk_words
    spans = [(m.start(), m.end()) for m in _WORD.finditer(text)]
    if not spans:
        return []
    n = len(spans)
    boundaries = _boundaries(text, spans)
    pieces: list[tuple[int, int]] = []  # word index ranges [i, end)
    i = 0
    while i < n:
        hard_end = min(i + window, n)
        end = hard_end
        if hard_end < n:
            floor = i + int(window * 0.6)
            for j in range(hard_end, floor, -1):
                if j in boundaries:
                    end = j
                    break
        pieces.append((i, end))
        if end >= n:
            break
        i = max(end - overlap, i + 1)
    # A short tail is folded into the previous chunk rather than becoming a fragment of its own.
    if len(pieces) > 1 and (pieces[-1][1] - pieces[-1][0]) < minimum:
        prev_start, _ = pieces[-2]
        pieces[-2:] = [(prev_start, pieces[-1][1])]

    chunks: list[Chunk] = []
    for offset, (a, b) in enumerate(pieces):
        start, end = spans[a][0], spans[b - 1][1]
        body = text[start:end]
        heading = _heading_before(text, start, carried_heading)
        prefix = context_prefix(title, path, heading, doc_title)
        chunks.append(
            Chunk(
                page=page,
                index=first_index + offset,
                start=start,
                end=end,
                text=body,
                heading=heading,
                embed_text=f"{prefix}\n{body}" if prefix else body,
                context=prefix,
            )
        )
    return chunks


def chunk_document(
    pages: list[str],
    *,
    title: str = "",
    path: str = "",
    cfg: ScaleSettings | None = None,
) -> list[Chunk]:
    """`pages[0]` is page 1. Heading state is carried across pages."""
    cfg = cfg or get_scale_settings()
    chunks: list[Chunk] = []
    carried = ""
    doc_title = extract_title(pages[0] if pages else "", title)
    for number, page_text in enumerate(pages, start=1):
        page_chunks = chunk_page(
            page_text or "", number, first_index=len(chunks), title=title, path=path,
            carried_heading=carried, doc_title=doc_title, cfg=cfg,
        )
        if page_chunks:
            carried = page_chunks[-1].heading or carried
        chunks.extend(page_chunks)
    return chunks
