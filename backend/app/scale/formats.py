"""Built-in readers for office and web formats that need no external service: PowerPoint (.pptx), OpenDocument text
(.odt), RTF (.rtf) and HTML (.html / .htm), plus the table rule that keeps spreadsheet rows searchable.

Each reader returns the same "blocks" the other ingesters produce ({"type": "heading" | "paragraph" | "table" |
"page_break"}), so everything after this point (rendering to a preview PDF, page text, chunking, indexing, permissions)
is the code every other file type already goes through. (Legacy binary Office files are first converted to their modern
form by LibreOffice, see legacy_office.py; images and scans are read by the OCR models, see docparse/.)

Safety: Office files are zip archives. Every member is read with a size cap (a zip bomb cannot exhaust memory) and XML
that declares entities or a DOCTYPE is refused (no entity-expansion attacks). Nothing here executes anything.
"""

from __future__ import annotations

import html
import re
import zipfile
from html.parser import HTMLParser
from xml.etree import ElementTree as ET

MAX_MEMBER_BYTES = 64 * 1024 * 1024
MAX_MEMBERS = 5000
BUILTIN_TYPES = {"pptx", "odt", "rtf", "html", "htm"}

_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
_P = "{http://schemas.openxmlformats.org/presentationml/2006/main}"
_TEXT = "{urn:oasis:names:tc:opendocument:xmlns:text:1.0}"
_TABLE = "{urn:oasis:names:tc:opendocument:xmlns:table:1.0}"


class UnreadableFile(Exception):
    pass


# ----------------------------------------------------------------------------------------------- safe archive access

def _member(zf: zipfile.ZipFile, name: str) -> bytes:
    info = zf.getinfo(name)
    if info.file_size > MAX_MEMBER_BYTES:
        raise UnreadableFile(f"archive member too large: {name}")
    with zf.open(info) as fh:
        data = fh.read(MAX_MEMBER_BYTES + 1)
    if len(data) > MAX_MEMBER_BYTES:
        raise UnreadableFile(f"archive member too large: {name}")
    return data


def _xml(data: bytes) -> ET.Element:
    head = data[:4096].lower()
    if b"<!entity" in head or b"<!doctype" in head:
        raise UnreadableFile("xml with a DOCTYPE or entity declaration refused")
    try:
        return ET.fromstring(data)
    except ET.ParseError as exc:
        raise UnreadableFile(f"xml parse error: {exc}") from exc


def _open_zip(path: str) -> zipfile.ZipFile:
    try:
        zf = zipfile.ZipFile(path)
    except zipfile.BadZipFile as exc:
        raise UnreadableFile("not a zip container") from exc
    if len(zf.namelist()) > MAX_MEMBERS:
        raise UnreadableFile("too many archive members")
    return zf


# ----------------------------------------------------------------------------------------------- PowerPoint

def _natural(name: str) -> int:
    m = re.search(r"(\d+)\.xml$", name)
    return int(m.group(1)) if m else 0


def _paragraphs(shape: ET.Element) -> list[str]:
    out = []
    for p in shape.iter(f"{_A}p"):
        text = "".join(t.text or "" for t in p.iter(f"{_A}t")).strip()
        if text:
            out.append(text)
    return out


def _slide_blocks(root: ET.Element) -> list[dict]:
    title: list[str] = []
    body: list[dict] = []
    tree = root.find(f"{_P}cSld/{_P}spTree")
    if tree is None:
        return []
    for node in tree.iter():
        if node.tag == f"{_P}sp":
            ph = node.find(f"{_P}nvSpPr/{_P}nvPr/{_P}ph")
            paras = _paragraphs(node)
            if not paras:
                continue
            if ph is not None and ph.get("type") in ("title", "ctrTitle"):
                title.extend(paras)
            else:
                body.extend({"type": "paragraph", "text": t} for t in paras)
        elif node.tag == f"{_A}tbl":
            rows = []
            for tr in node.iter(f"{_A}tr"):
                rows.append([" ".join(_paragraphs(tc)) for tc in tr.findall(f"{_A}tc")])
            if rows:
                body.append({"type": "table", "rows": rows})
    blocks: list[dict] = []
    if title:
        blocks.append({"type": "heading", "text": " ".join(title)})
    return blocks + body


def pptx_blocks(path: str) -> list[dict]:
    """One page per slide (a page break between slides), title as heading, then text, tables and speaker notes."""
    with _open_zip(path) as zf:
        names = zf.namelist()
        slides = sorted((n for n in names if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)), key=_natural)
        notes = {_natural(n): n for n in names if re.fullmatch(r"ppt/notesSlides/notesSlide\d+\.xml", n)}
        blocks: list[dict] = []
        for index, name in enumerate(slides):
            if index:
                blocks.append({"type": "page_break"})
            slide = _slide_blocks(_xml(_member(zf, name)))
            if not slide:
                slide = [{"type": "paragraph", "text": f"Slide {index + 1}"}]
            if not slide[0]["type"] == "heading":
                slide.insert(0, {"type": "heading", "text": f"Slide {index + 1}"})
            blocks.extend(slide)
            note_name = notes.get(_natural(name))
            if note_name:
                note_paras = [t for t in _paragraphs(_xml(_member(zf, note_name))) if not t.isdigit()]
                if note_paras:
                    blocks.append({"type": "paragraph", "text": "Speaker notes: " + " ".join(note_paras)})
    return blocks


# ----------------------------------------------------------------------------------------------- OpenDocument text

def _odf_text(node: ET.Element) -> str:
    parts: list[str] = []

    def walk(n: ET.Element) -> None:
        if n.text:
            parts.append(n.text)
        for child in n:
            if child.tag == f"{_TEXT}s":
                parts.append(" " * int(child.get(f"{_TEXT}c", "1")))
            elif child.tag == f"{_TEXT}tab":
                parts.append("\t")
            elif child.tag == f"{_TEXT}line-break":
                parts.append("\n")
            else:
                walk(child)
            if child.tail:
                parts.append(child.tail)

    walk(node)
    return "".join(parts).strip()


def odt_blocks(path: str) -> list[dict]:
    with _open_zip(path) as zf:
        root = _xml(_member(zf, "content.xml"))
    body = root.find("{urn:oasis:names:tc:opendocument:xmlns:office:1.0}body/{urn:oasis:names:tc:opendocument:xmlns:office:1.0}text")
    blocks: list[dict] = []
    if body is None:
        return blocks
    for node in body:
        if node.tag == f"{_TEXT}h":
            text = _odf_text(node)
            if text:
                blocks.append({"type": "heading", "text": text})
        elif node.tag == f"{_TEXT}p":
            text = _odf_text(node)
            if text:
                blocks.append({"type": "paragraph", "text": text})
        elif node.tag == f"{_TEXT}list":
            for item in node.iter(f"{_TEXT}p"):
                text = _odf_text(item)
                if text:
                    blocks.append({"type": "paragraph", "text": "- " + text})
        elif node.tag == f"{_TABLE}table":
            rows = []
            for tr in node.iter(f"{_TABLE}table-row"):
                rows.append([_odf_text(tc) for tc in tr.findall(f"{_TABLE}table-cell")])
            rows = [r for r in rows if any(c.strip() for c in r)]
            if rows:
                blocks.append({"type": "table", "rows": rows})
    return blocks


# ----------------------------------------------------------------------------------------------- RTF

_RTF_DESTINATIONS = {"fonttbl", "colortbl", "stylesheet", "info", "pict", "header", "footer", "footnote", "themedata",
                     "colorschememapping", "latentstyles", "datastore", "listtable", "listoverridetable", "rsidtbl",
                     "generator", "xmlnstbl", "fldinst", "bkmkstart", "bkmkend", "object", "shpinst", "nonshppict"}
_RTF_TOKEN = re.compile(r"\\([a-zA-Z]+)(-?\d+)? ?|\\'([0-9a-fA-F]{2})|\\([^a-zA-Z])|([{}])|([^\\{}]+)", re.S)


def rtf_text(data: str) -> str:
    out: list[str] = []
    stack: list[bool] = []          # True = this group is skipped
    skip = False
    uc_skip = 0
    for m in _RTF_TOKEN.finditer(data):
        word, num, hexcode, symbol, brace, literal = m.groups()
        if brace == "{":
            stack.append(skip)
        elif brace == "}":
            skip = stack.pop() if stack else False
        elif word is not None:
            if word in _RTF_DESTINATIONS:
                skip = True
            elif word in ("par", "sect", "page") and not skip:
                out.append("\n\n")
            elif word == "line" and not skip:
                out.append("\n")
            elif word == "tab" and not skip:
                out.append("\t")
            elif word == "u" and num is not None and not skip:
                code = int(num)
                out.append(chr(code + 65536 if code < 0 else code))
                uc_skip = 1
            elif word == "cell" and not skip:
                out.append(" | ")
            elif word == "row" and not skip:
                out.append("\n")
        elif hexcode is not None:
            if not skip:
                out.append(bytes([int(hexcode, 16)]).decode("cp1252", errors="replace"))
        elif symbol is not None:
            if symbol == "*":
                skip = True                      # an optional destination: ignore the whole group
            elif symbol in "\\{}" and not skip:
                out.append(symbol)
            elif symbol == "~" and not skip:
                out.append(" ")
        elif literal is not None and not skip:
            text = literal.replace("\r", "").replace("\n", "")
            if uc_skip:
                text = text[uc_skip:]
                uc_skip = 0
            out.append(text)
    return "".join(out)


def rtf_blocks(path: str) -> list[dict]:
    raw = open(path, "rb").read(MAX_MEMBER_BYTES)
    if not raw.lstrip().startswith(b"{\\rtf"):
        raise UnreadableFile("not an RTF file")
    text = rtf_text(raw.decode("latin-1"))
    return [{"type": "paragraph", "text": " ".join(p.split())} for p in re.split(r"\n\s*\n", text) if p.strip()]


# ----------------------------------------------------------------------------------------------- HTML

_SKIP = {"script", "style", "noscript", "template", "svg", "canvas", "iframe", "head"}
_BLOCK = {"p", "div", "section", "article", "main", "li", "ul", "ol", "blockquote", "pre", "br", "tr", "dd", "dt", "dl",
          "figure", "figcaption", "header", "footer", "nav", "aside", "form", "fieldset", "details", "summary", "address"}
_HEADINGS = {"h1", "h2", "h3", "h4", "h5", "h6"}


class _Html(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[dict] = []
        self.title = ""
        self._buf: list[str] = []
        self._skip = 0
        self._in_title = False
        self._heading = False
        self._table: list[list[str]] | None = None
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def _flush(self) -> None:
        text = " ".join("".join(self._buf).split())
        self._buf = []
        if text:
            self.blocks.append({"type": "heading" if self._heading else "paragraph", "text": text})
        self._heading = False

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag == "title":
            self._in_title = True
        if tag in _SKIP and tag != "head":
            self._skip += 1
        if self._skip:
            return
        if tag == "table":
            self._flush()
            self._table = []
        elif tag == "tr" and self._table is not None:
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []
        elif tag in _HEADINGS:
            self._flush()
            self._heading = True
        elif tag in _BLOCK and self._cell is None:
            self._flush()

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        if tag in _SKIP and tag != "head":
            self._skip = max(0, self._skip - 1)
            return
        if self._skip:
            return
        if tag in ("td", "th") and self._cell is not None and self._row is not None:
            self._row.append(" ".join("".join(self._cell).split()))
            self._cell = None
        elif tag == "tr" and self._row is not None and self._table is not None:
            if any(c for c in self._row):
                self._table.append(self._row)
            self._row = None
        elif tag == "table" and self._table is not None:
            if self._table:
                self.blocks.append({"type": "table", "rows": self._table})
            self._table = None
        elif tag in _HEADINGS or (tag in _BLOCK and self._cell is None):
            self._flush()

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
            return
        if self._skip:
            return
        if self._cell is not None:
            self._cell.append(data)
        else:
            self._buf.append(data)

    def close(self) -> None:
        super().close()
        self._flush()


def html_blocks(path: str) -> list[dict]:
    raw = open(path, "rb").read(MAX_MEMBER_BYTES)
    match = re.search(rb"charset=[\"']?([\w-]+)", raw[:4096], re.I)
    encoding = match.group(1).decode("ascii", "ignore") if match else "utf-8"
    try:
        text = raw.decode(encoding, errors="replace")
    except LookupError:
        text = raw.decode("utf-8", errors="replace")
    parser = _Html()
    parser.feed(text)
    parser.close()
    blocks = parser.blocks
    title = html.unescape(" ".join(parser.title.split()))
    if title and not (blocks and blocks[0]["type"] == "heading" and blocks[0]["text"].lower() == title.lower()):
        blocks.insert(0, {"type": "heading", "text": title})
    return blocks


# ----------------------------------------------------------------------------------------------- dispatch + tables

def extract_blocks(path: str, ext: str) -> list[dict]:
    ext = ext.lower()
    if ext == "pptx":
        return pptx_blocks(path)
    if ext == "odt":
        return odt_blocks(path)
    if ext == "rtf":
        return rtf_blocks(path)
    if ext in ("html", "htm"):
        return html_blocks(path)
    raise UnreadableFile(f"no built-in reader for .{ext}")


def labeled_rows(blocks: list[dict], rows_per_section: int = 25, min_rows: int = 0) -> list[dict]:
    """A long table split across chunks loses its column names: the chunk that holds rows 80-90 says '4,500 | 12 | open'
    and nothing says what the numbers are. Rewrite every table with more than `min_rows` data rows as short sections
    whose lines carry the header ('Amount: 4,500 | Qty: 12 | Status: open'), each section under its own heading, so any
    chunk of it still says what it is about. Short tables are rewritten too (min_rows=0): drawn as a table, a four-row
    table comes out of a PDF as loose cells whose labels are no longer next to their values. A table with only a header
    row stays as it is."""
    out: list[dict] = []
    last_heading = ""
    for block in blocks:
        if block.get("type") == "heading":
            last_heading = block["text"]
        if block.get("type") != "table" or len(block.get("rows", [])) <= min_rows + 1:
            out.append(block)
            continue
        rows = block["rows"]
        header, data = rows[0], rows[1:]
        for start in range(0, len(data), rows_per_section):
            part = data[start: start + rows_per_section]
            label = f"{last_heading + ' - ' if last_heading else ''}rows {start + 1}-{start + len(part)}"
            out.append({"type": "heading", "text": label})
            for row in part:
                cells = [f"{h.strip() or f'col{i + 1}'}: {v.strip()}" for i, (h, v) in enumerate(zip(header, row)) if v.strip()]
                if cells:
                    out.append({"type": "paragraph", "text": " | ".join(cells)})
    return out
