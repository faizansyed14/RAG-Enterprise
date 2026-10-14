"""Put back the spaces the recogniser leaves out.

PP-OCR often returns 'WidgetB' or 'Table1:Pricelist' for text that has visible gaps: it was trained on Chinese,
where words are not separated, and it drops Latin word gaps whenever they are narrow. A keyword index then sees one
long nonsense word. The page image still shows the gaps, so this module measures them: it looks for runs of blank
pixel columns inside the line that are wider than a letter gap can be, and inserts a space in the text at the
character the run falls next to (character positions come from the recogniser's own per-character boxes).
"""

from __future__ import annotations

import numpy as np

from .types import Line


def _ink(gray: np.ndarray) -> tuple[np.ndarray, int]:
    """(boolean per pixel column: does it hold ink?, height in pixels of the band that holds ink). Otsu threshold;
    dark text on light paper. The ink height is the size of the writing itself -- the detector's box is a loose
    1.3-1.6 times taller and varies with the model's padding, so word gaps are measured against the ink."""
    import cv2

    if gray.ndim == 3:
        gray = cv2.cvtColor(gray, cv2.COLOR_BGR2GRAY)
    if gray.size == 0:
        return np.zeros(0, dtype=bool), 0
    _, binary = cv2.threshold(gray, 0, 1, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    # a column counts as inked when it has a few ink pixels: single speckles of scan noise do not
    need = max(1, int(round(gray.shape[0] * 0.06)))
    columns = binary.sum(axis=0) >= need
    rows = np.flatnonzero(binary.sum(axis=1) >= max(2, int(round(gray.shape[1] * 0.004))))
    return columns, (int(rows[-1] - rows[0] + 1) if len(rows) else gray.shape[0])


def _gaps(ink: np.ndarray) -> list[tuple[int, int]]:
    """Interior runs of blank columns as (start, end_exclusive); the blank margins at both ends are not gaps."""
    n = len(ink)
    if n == 0 or not ink.any():
        return []
    first, last = int(np.argmax(ink)), n - 1 - int(np.argmax(ink[::-1]))
    out, start = [], None
    for x in range(first, last + 1):
        if not ink[x]:
            if start is None:
                start = x
        elif start is not None:
            out.append((start, x))
            start = None
    return out


def restore_spaces(image: np.ndarray, line: Line, *, min_gap_ratio: float = 0.2, min_vs_median: float = 2.3) -> str:
    """The line's text with spaces inserted where the image shows a word gap and the text has none."""
    text, chars = line.text, line.chars
    if not chars or len(chars) != len(text) or len(text) < 3:
        return text
    x0, y0, x1, y1 = (int(round(v)) for v in line.box)
    h, w = image.shape[:2]
    x0, x1, y0, y1 = max(x0, 0), min(x1, w), max(y0, 0), min(y1, h)
    if x1 - x0 < 8 or y1 - y0 < 6:
        return text
    crop = image[y0:y1, x0:x1]
    columns, ink_height = _ink(crop)
    threshold = max(3.0, ink_height * min_gap_ratio)
    centres = [(c[0] + c[1]) / 2.0 for c in chars]
    gaps = _gaps(columns)
    # A word gap is much wider than this line's own letter gaps, whatever the font size.
    widths = sorted(e - s for s, e in gaps)
    if widths:
        threshold = max(threshold, min_vs_median * widths[len(widths) // 2])
    insert_after: set[int] = set()
    for start, end in gaps:
        if end - start < threshold:
            continue
        gx = x0 + (start + end) / 2.0
        # the pair of characters the gap lies between
        i = None
        for k in range(len(centres) - 1):
            if centres[k] <= gx < centres[k + 1]:
                i = k
                break
        if i is None:
            continue
        a, b = text[i], text[i + 1]
        if a.isspace() or b.isspace():
            continue
        # numbers are never split ("12.50", "1,750"); the stroke of a '1' or a '.' leaves a gap that is not a space
        if (a.isdigit() and (b.isdigit() or b in ".,")) or (a in ".," and b.isdigit()):
            continue
        insert_after.add(i)
    if not insert_after:
        return text
    out = []
    for i, ch in enumerate(text):
        out.append(ch)
        if i in insert_after:
            out.append(" ")
    return "".join(out)
