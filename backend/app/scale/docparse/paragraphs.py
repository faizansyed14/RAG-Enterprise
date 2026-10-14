"""Lines -> paragraphs: join the lines a person reads as one block of prose, keep list items and short lines apart."""

from __future__ import annotations

import re
import statistics

from .geometry import height, union_box
from .order import reading_order
from .types import Line

_BULLET = re.compile(r"^\s*(?:[-•●▪–*·]|\(?\d{1,3}[.)]|\(?[a-zA-Z][.)]|[ivxIVX]{1,4}[.)])\s+\S")
_SENTENCE_END = re.compile(r"[.!?:;。！？][\"'”’)\]]*$")
_CJK = re.compile(r"[぀-ヿ㐀-鿿豈-﫿가-힯]")


def join_text(parts: list[str]) -> str:
    """Join the text of consecutive lines: undo end-of-line hyphenation, no space between CJK characters."""
    out = ""
    for part in (p.strip() for p in parts):
        if not part:
            continue
        if not out:
            out = part
        elif out.endswith("-") and len(out) >= 3 and out[-2].isalpha() and part[:1].islower():
            out = out[:-1] + part                       # "termina-" + "tion" -> "termination"
        elif _CJK.search(out[-1]) and _CJK.search(part[0]):
            out += part
        else:
            out += " " + part
    return out


def order_lines(lines: list[Line]) -> list[Line]:
    return reading_order(lines, lambda ln: ln.box, min_gutter=max(10.0, 1.2 * (statistics.median([height(ln.box) for ln in lines]) if lines else 10.0)))


def merge_lines(lines: list[Line]) -> list[list[Line]]:
    """Group ordered lines into paragraphs."""
    if not lines:
        return []
    heights = [height(ln.box) for ln in lines if height(ln.box) > 0]
    lh = statistics.median(heights) if heights else 10.0
    left = min(ln.box[0] for ln in lines)
    right = max(ln.box[2] for ln in lines)
    span = max(right - left, 1.0)
    paragraphs: list[list[Line]] = [[lines[0]]]
    for prev, line in zip(lines, lines[1:]):
        gap = line.box[1] - prev.box[3]
        column_break = line.box[0] > prev.box[2] or line.box[1] < prev.box[1] - lh        # moved to another column
        starts_item = bool(_BULLET.match(line.text))
        indented = line.box[0] - left > 1.2 * lh and prev.box[0] - left <= 1.2 * lh
        short_last = (prev.box[2] - left) < 0.8 * span and bool(_SENTENCE_END.search(prev.text.strip()))
        capital = line.text.strip()[:1].isupper() or line.text.strip()[:1].isdigit()
        if column_break or gap > 0.75 * lh or starts_item or indented or (short_last and capital):
            paragraphs.append([line])
        else:
            paragraphs[-1].append(line)
    return paragraphs


def paragraph_text(group: list[Line]) -> str:
    return join_text([ln.text for ln in group])


def paragraph_box(group: list[Line]):
    return union_box([ln.box for ln in group])
