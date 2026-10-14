"""Straighten a tilted scan before it is read.

Text lines on a page scanned at an angle fall across several pixel rows, so the OCR detector cuts them into slivers, the
layout model sees skewed blocks and rows of a table no longer line up. The skew is found the classic way: rotate a small
binarised copy through a range of angles and keep the angle at which the horizontal ink profile is sharpest (the lines
are then exactly horizontal).
"""

from __future__ import annotations

import numpy as np


def _profile_score(binary: np.ndarray, angle: float) -> float:
    import cv2

    h, w = binary.shape
    m = cv2.getRotationMatrix2D((w / 2.0, h / 2.0), angle, 1.0)
    rotated = cv2.warpAffine(binary, m, (w, h), flags=cv2.INTER_NEAREST, borderValue=0)
    rows = rotated.sum(axis=1).astype(np.float64)
    return float(np.sum(np.diff(rows) ** 2))


def estimate_skew(image: np.ndarray, max_angle: float = 4.0, width: int = 700) -> float:
    """The angle in degrees to rotate the image by (counter-clockwise positive, OpenCV convention) to make text level."""
    import cv2

    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image
    scale = width / max(gray.shape[1], 1)
    if scale < 1.0:
        gray = cv2.resize(gray, (width, max(1, int(gray.shape[0] * scale))), interpolation=cv2.INTER_AREA)
    _, binary = cv2.threshold(gray, 0, 1, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    if binary.sum() < 50:
        return 0.0
    coarse = np.arange(-max_angle, max_angle + 1e-6, 0.5)
    best = max(coarse, key=lambda a: _profile_score(binary, a))
    fine = np.arange(best - 0.5, best + 0.5 + 1e-6, 0.1)
    return float(max(fine, key=lambda a: _profile_score(binary, a)))


def deskew(image: np.ndarray, min_angle: float = 0.3, max_angle: float = 4.0) -> tuple[np.ndarray, float]:
    """-> (straightened image, angle applied). Pages tilted less than `min_angle` degrees are returned as they are."""
    import cv2

    angle = estimate_skew(image, max_angle)
    if abs(angle) < min_angle:
        return image, 0.0
    h, w = image.shape[:2]
    m = cv2.getRotationMatrix2D((w / 2.0, h / 2.0), angle, 1.0)
    out = cv2.warpAffine(image, m, (w, h), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_CONSTANT, borderValue=(255, 255, 255))
    return out, angle
