"""Tables: the structure model's HTML -> rows, and a geometric fallback that builds rows from text positions alone."""

from __future__ import annotations

import re
import statistics
from html.parser import HTMLParser

from .geometry import height
from .types import Line


class _TableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[tuple[str, int, int]]] = []       # (text, colspan, rowspan)
        self._row: list[tuple[str, int, int]] | None = None
        self._cell: list[str] | None = None
        self._span = (1, 1)
        self._depth = 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "table":
            self._depth += 1
        elif tag == "tr" and self._depth == 1:
            self._row = []
        elif tag in ("td", "th") and self._row is not None and self._depth == 1:
            def num(name):
                try:
                    return max(1, min(int(a.get(name) or 1), 50))
                except ValueError:
                    return 1
            self._cell, self._span = [], (num("colspan"), num("rowspan"))
        elif tag == "br" and self._cell is not None:
            self._cell.append(" ")

    def handle_endtag(self, tag):
        if tag == "table":
            self._depth -= 1
        elif tag in ("td", "th") and self._cell is not None and self._row is not None:
            self._row.append((re.sub(r"\s+", " ", "".join(self._cell)).strip(), *self._span))
            self._cell = None
        elif tag == "tr" and self._row is not None and self._depth == 1:
            self.rows.append(self._row)
            self._row = None

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)


def html_to_rows(html: str) -> list[list[str]]:
    """A rectangular grid of cell texts. A cell spanning several columns keeps its text in the first of them; a cell
    spanning several rows keeps it in the first row (the rest are blank), so no text is repeated."""
    parser = _TableParser()
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # noqa: BLE001 - malformed model output
        return []
    grid: list[list[str]] = []
    pending: dict[tuple[int, int], bool] = {}                  # (row, col) occupied by a rowspan from above
    for r, row in enumerate(parser.rows):
        out: list[str] = []
        col = 0
        for text, colspan, rowspan in row:
            while pending.get((r, col)):
                out.append("")
                col += 1
            out.append(text)
            for k in range(1, colspan):
                out.append("")
            for dr in range(1, rowspan):
                for dc in range(colspan):
                    pending[(r + dr, col + dc)] = True
            col += colspan
        while pending.get((r, col)):
            out.append("")
            col += 1
        grid.append(out)
    width = max((len(r) for r in grid), default=0)
    grid = [r + [""] * (width - len(r)) for r in grid]
    grid = [r for r in grid if any(c.strip() for c in r)]
    if grid:                                                   # drop columns that are empty in every row
        keep = [c for c in range(width) if any(r[c].strip() for r in grid)]
        grid = [[r[c] for c in keep] for r in grid]
    return grid


def tokens(text: str) -> list[str]:
    return re.findall(r"\w+", text.lower())


def recovered_share(rows: list[list[str]], lines: list[Line]) -> float:
    """How much of the text that sits inside the table region the structure model put into cells (0..1)."""
    want = [t for ln in lines for t in tokens(ln.text)]
    if not want:
        return 1.0
    have: dict[str, int] = {}
    for row in rows:
        for cell in row:
            for t in tokens(cell):
                have[t] = have.get(t, 0) + 1
    hit = 0
    for t in want:
        if have.get(t, 0) > 0:
            have[t] -= 1
            hit += 1
    return hit / len(want)


def rows_from_lines(lines: list[Line]) -> list[list[str]]:
    """Rebuild a table from text positions only: lines whose vertical centres agree are one row; column bands are the
    horizontal stretches no text crosses. Used when the structure model fails or loses text."""
    if not lines:
        return []
    lh = statistics.median([height(ln.box) for ln in lines]) or 10.0
    ordered = sorted(lines, key=lambda ln: (ln.box[1] + ln.box[3]) / 2.0)
    rows: list[list[Line]] = []
    for ln in ordered:
        cy = (ln.box[1] + ln.box[3]) / 2.0
        if rows:
            last = rows[-1]
            ry = statistics.mean((m.box[1] + m.box[3]) / 2.0 for m in last)
            if abs(cy - ry) <= 0.6 * lh:
                last.append(ln)
                continue
        rows.append([ln])
    # column bands: x-ranges of all lines merged where they overlap
    spans = sorted((ln.box[0], ln.box[2]) for ln in lines)
    bands: list[list[float]] = []
    for x0, x1 in spans:
        if bands and x0 <= bands[-1][1] + 0.4 * lh:
            bands[-1][1] = max(bands[-1][1], x1)
        else:
            bands.append([x0, x1])
    grid: list[list[str]] = []
    for row in rows:
        cells = [""] * len(bands)
        for ln in sorted(row, key=lambda m: m.box[0]):
            cx = (ln.box[0] + ln.box[2]) / 2.0
            k = next((i for i, (a, b) in enumerate(bands) if a - 1 <= cx <= b + 1), 0)
            cells[k] = (cells[k] + " " + ln.text).strip()
        grid.append(cells)
    return grid


def split_native_cells(line: Line, min_gap: float) -> list[Line]:
    """A line of the PDF text layer that holds several cells ("Widget A   each   12.50") -> one Line per cell, cut
    wherever two words are further apart than `min_gap` pixels."""
    if not line.words or len(line.words) < 2:
        return [line]
    parts: list[list[tuple[float, float, str]]] = [[line.words[0]]]
    for prev, word in zip(line.words, line.words[1:]):
        if word[0] - prev[1] >= min_gap:
            parts.append([word])
        else:
            parts[-1].append(word)
    if len(parts) == 1:
        return [line]
    top, bottom = line.box[1], line.box[3]
    return [
        Line(box=(p[0][0], top, p[-1][1], bottom), text=" ".join(w[2] for w in p), score=line.score, native=True, words=p)
        for p in parts
    ]


def looks_tabular(lines: list[Line], min_rows: int = 3, min_cols: int = 2, min_fill: float = 0.6) -> bool:
    """Do these text lines sit in a grid? (A table the layout model took for a figure: its words are in rows and the
    rows line up in columns.) Used only for regions labelled figure, where prose is rare."""
    if len(lines) < min_rows * min_cols:
        return False
    grid = rows_from_lines(lines)
    cols = max((len(r) for r in grid), default=0)
    if len(grid) < min_rows or cols < min_cols:
        return False
    filled = sum(1 for r in grid for c in r if c.strip())
    return filled / (len(grid) * cols) >= min_fill
