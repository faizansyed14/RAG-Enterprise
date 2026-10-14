"""Reading order by recursive XY-cut.

Sort boxes by their top edge and a two-column page is read across both columns, line by line. The cut works the way a
reader does: at every step look for the widest empty vertical gutter and the widest empty horizontal band that run
clean through everything left, split at the wider one (a gutter counts a little extra: columns are read before rows),
and recurse into both halves until nothing can be cut. Splitting at the WIDEST gap first matters: a page with two
columns and a full-width table has small paragraph gaps between the text blocks and one large gap above the table; cutting
at every gap would read the first paragraph of each column, then the second of each, and so on.
"""

from __future__ import annotations

from typing import Callable, TypeVar

from .geometry import Box

T = TypeVar("T")


def _gaps(spans: list[tuple[float, float, int]]) -> list[tuple[float, int]]:
    """spans sorted by start -> [(gap width, number of spans before the gap)] for every empty gap."""
    out = []
    reach = None
    for k, (start, end, _) in enumerate(spans):
        if reach is not None and start > reach:
            out.append((start - reach, k))
        reach = end if reach is None else max(reach, end)
    return out


def reading_order(
    items: list[T], box_of: Callable[[T], Box], *, min_gutter: float = 12.0, min_column_share: float = 0.12,
    min_band_gap: float = 1.0, gutter_bias: float = 1.5,
) -> list[T]:
    """`items` in the order a person reads them. Pure function; stable for ties."""
    boxes = [box_of(i) for i in items]

    def cut(idx: list[int]) -> list[int]:
        if len(idx) <= 1:
            return idx
        x0 = min(boxes[i][0] for i in idx)
        x1 = max(boxes[i][2] for i in idx)
        total_w = max(x1 - x0, 1.0)

        vspans = sorted((boxes[i][0], boxes[i][2], i) for i in idx)
        best_v = None                                           # (width, position)
        for width_, k in _gaps(vspans):
            if width_ < min_gutter:
                continue
            left, right = vspans[:k], vspans[k:]
            lw = max(s[1] for s in left) - min(s[0] for s in left)
            rw = max(s[1] for s in right) - min(s[0] for s in right)
            if lw / total_w < min_column_share or rw / total_w < min_column_share:
                continue
            if best_v is None or width_ > best_v[0]:
                best_v = (width_, k)

        hspans = sorted((boxes[i][1], boxes[i][3], i) for i in idx)
        best_h = None
        for width_, k in _gaps(hspans):
            if width_ >= min_band_gap and (best_h is None or width_ > best_h[0]):
                best_h = (width_, k)

        if best_v and (best_h is None or best_v[0] * gutter_bias >= best_h[0]):
            k = best_v[1]
            return cut([s[2] for s in vspans[:k]]) + cut([s[2] for s in vspans[k:]])
        if best_h:
            k = best_h[1]
            return cut([s[2] for s in hspans[:k]]) + cut([s[2] for s in hspans[k:]])
        return sorted(idx, key=lambda i: (round(boxes[i][1], 1), boxes[i][0]))

    return [items[i] for i in cut(list(range(len(items))))]
