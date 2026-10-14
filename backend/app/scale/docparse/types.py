"""The small data model the parser passes around."""

from __future__ import annotations

from dataclasses import dataclass, field

from .geometry import Box


@dataclass
class Line:
    """One line of text with its position: from OCR, or from the PDF's own text layer."""
    box: Box
    text: str
    score: float = 1.0
    # (x0, x1, char) per character, left to right; only OCR with character positions provides it
    chars: list[tuple[float, float, str]] | None = None
    # (x0, x1, word) per word, left to right; only the PDF text layer provides it (used to split table cells)
    words: list[tuple[float, float, str]] | None = None
    native: bool = False


@dataclass
class Region:
    """A rectangle the layout model recognised, with the lines that fall inside it."""
    box: Box
    label: str                       # text title figure figure_caption table table_caption header footer reference equation
    score: float = 1.0
    lines: list[Line] = field(default_factory=list)


@dataclass
class Block:
    """One unit of the parsed page, in reading order."""
    type: str                        # heading | paragraph | table | figure | caption | equation | reference
    text: str = ""
    box: Box = (0.0, 0.0, 0.0, 0.0)
    page: int = 0
    level: int = 0                   # heading level (1 = biggest)
    rows: list[list[str]] | None = None   # tables
    html: str = ""                   # tables, when a structure model produced it
    source: str = ""                 # layout label the block came from
    score: float = 1.0

    def as_dict(self) -> dict:
        out = {"type": self.type, "text": self.text, "page": self.page, "box": [round(v, 1) for v in self.box]}
        if self.level:
            out["level"] = self.level
        if self.rows is not None:
            out["rows"] = self.rows
        if self.html:
            out["html"] = self.html
        return out


@dataclass
class PageResult:
    number: int                      # 1-based
    width: int
    height: int
    blocks: list[Block] = field(default_factory=list)
    furniture: list[str] = field(default_factory=list)   # text removed as header / footer / page number
    ocr_lines: int = 0
    used_ocr: bool = False
    seconds: float = 0.0
    notes: list[str] = field(default_factory=list)


@dataclass
class ParsedDocument:
    pages: list[PageResult]
    seconds: float = 0.0

    def texts(self, table_style: str = "labeled") -> list[str]:
        from .render import page_text

        return [page_text(p, table_style) for p in self.pages]
