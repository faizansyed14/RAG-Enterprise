"""Boxes are (x0, y0, x1, y1) in page-image pixels, y growing downwards."""

from __future__ import annotations

Box = tuple[float, float, float, float]


def width(b: Box) -> float:
    return max(b[2] - b[0], 0.0)


def height(b: Box) -> float:
    return max(b[3] - b[1], 0.0)


def area(b: Box) -> float:
    return width(b) * height(b)


def intersection(a: Box, b: Box) -> float:
    return max(min(a[2], b[2]) - max(a[0], b[0]), 0.0) * max(min(a[3], b[3]) - max(a[1], b[1]), 0.0)


def iou(a: Box, b: Box) -> float:
    inter = intersection(a, b)
    union = area(a) + area(b) - inter
    return inter / union if union > 0 else 0.0


def covered(inner: Box, outer: Box) -> float:
    """The share of `inner` that lies inside `outer` (0..1)."""
    a = area(inner)
    return intersection(inner, outer) / a if a > 0 else 0.0


def union_box(boxes: list[Box]) -> Box:
    return (min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes))


def center(b: Box) -> tuple[float, float]:
    return (b[0] + b[2]) / 2.0, (b[1] + b[3]) / 2.0


def x_overlap(a: Box, b: Box) -> float:
    """Horizontal overlap as a share of the narrower box."""
    inter = max(min(a[2], b[2]) - max(a[0], b[0]), 0.0)
    narrow = min(width(a), width(b))
    return inter / narrow if narrow > 0 else 0.0


def y_overlap(a: Box, b: Box) -> float:
    """Vertical overlap as a share of the shorter box: two words on one line overlap ~1, stacked lines ~0."""
    inter = max(min(a[3], b[3]) - max(a[1], b[1]), 0.0)
    short = min(height(a), height(b))
    return inter / short if short > 0 else 0.0


def poly_to_box(poly) -> Box:
    xs = [float(p[0]) for p in poly]
    ys = [float(p[1]) for p in poly]
    return (min(xs), min(ys), max(xs), max(ys))
