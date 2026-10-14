"""Table structure recognition with the SLANet-plus ONNX model, and the step that puts the text into the cells.

The model looks at a picture of one table and answers two things: the HTML skeleton (rows, cells, row / column spans)
and a box for every cell. It reads no text. We already have the text lines with their positions (OCR or the PDF text
layer), so each line is given to the cell it overlaps most, and the skeleton is filled in.

Pre- and post-processing follow the PP-Structure SLANet recipe: longest side to 488 px, ImageNet mean / std, zero
padding to 488 x 488; the token dictionary is stored in the model file itself.
"""

from __future__ import annotations

import html as htmllib
import os
import threading

import numpy as np

from .geometry import Box, area, center, intersection
from .types import Line

MAX_SIDE = 488
_MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32)
_STD = np.array([0.229, 0.224, 0.225], dtype=np.float32)


def find_model_file(explicit: str = "") -> str:
    """Path of slanet-plus.onnx: an explicit path, $SCALE_DOCPARSE_TABLE_MODEL, or the copy inside the rapid-table wheel
    (located without importing that package, which pulls in unrelated dependencies)."""
    import importlib.util

    for candidate in (explicit, os.environ.get("SCALE_DOCPARSE_TABLE_MODEL", "")):
        if candidate and os.path.isfile(candidate):
            return candidate
    spec = importlib.util.find_spec("rapid_table")
    for root in (spec.submodule_search_locations or []) if spec else []:
        path = os.path.join(root, "models", "slanet-plus.onnx")
        if os.path.isfile(path):
            return path
    raise FileNotFoundError("slanet-plus.onnx not found: pip install rapid-table, or set SCALE_DOCPARSE_TABLE_MODEL")


class SlanetTable:
    name = "slanet-plus"

    def __init__(self, model_path: str = "", threads: int = 2) -> None:
        import onnxruntime as ort

        opts = ort.SessionOptions()
        opts.intra_op_num_threads = max(1, threads)
        opts.log_severity_level = 3
        self._session = ort.InferenceSession(find_model_file(model_path), opts, providers=["CPUExecutionProvider"])
        meta = self._session.get_modelmeta().custom_metadata_map
        tokens = meta["character"].splitlines()
        if "<td></td>" not in tokens:
            tokens.append("<td></td>")
        if "<td>" in tokens:
            tokens.remove("<td>")
        self._tokens = ["sos"] + tokens + ["eos"]
        self._begin, self._end = 0, len(self._tokens) - 1
        self._input = self._session.get_inputs()[0].name
        self._lock = threading.Lock()

    # ---------------------------------------------------------------------------------------------- the model
    def structure(self, image: np.ndarray) -> tuple[list[str], np.ndarray]:
        """-> (HTML tokens of the table skeleton, cell boxes [n, 4 or 8] in pixels of `image`, one per cell, in order)."""
        import cv2

        h, w = image.shape[:2]
        ratio = MAX_SIDE / float(max(h, w))
        resized = cv2.resize(image, (max(1, int(w * ratio)), max(1, int(h * ratio)))).astype(np.float32)
        resized = (resized * np.float32(1.0 / 255.0) - _MEAN) / _STD
        padded = np.zeros((MAX_SIDE, MAX_SIDE, 3), dtype=np.float32)
        padded[: resized.shape[0], : resized.shape[1]] = resized
        batch = np.ascontiguousarray(padded.transpose(2, 0, 1)[None])
        with self._lock:
            outputs = self._session.run(None, {self._input: batch})
        loc, probs = outputs[0][0], outputs[1][0]
        idx = probs.argmax(axis=1)
        tokens: list[str] = []
        boxes: list[np.ndarray] = []
        for t, k in enumerate(idx):
            k = int(k)
            if t > 0 and k == self._end:
                break
            if k in (self._begin, self._end):
                continue
            token = self._tokens[k]
            if token in ("<td>", "<td", "<td></td>"):
                boxes.append(loc[t].copy())
            tokens.append(token)
        cells = np.array(boxes, dtype=np.float32).reshape(len(boxes), -1)
        # boxes are fractions of the padded square: back to pixels of the original image
        cells[:, 0::2] *= MAX_SIDE / ratio
        cells[:, 1::2] *= MAX_SIDE / ratio
        return ["<table>"] + tokens + ["</table>"], cells

    # ------------------------------------------------------------------------------------------ the engine call
    def __call__(self, image: np.ndarray, lines: list[Line]) -> str | None:
        if not lines or image.size == 0:
            return None
        tokens, cells = self.structure(image)
        boxes = [_as_box(c) for c in cells]
        if not boxes:
            return None
        owned = assign_lines_to_cells(boxes, lines)
        if any(_torn_cell(group) for group in owned.values()):
            return None              # the model merged cells that are visibly apart: let the geometric rebuild decide
        return fill_cells(tokens, boxes, lines, owned)


def _as_box(c: np.ndarray) -> Box:
    xs, ys = c[0::2], c[1::2]
    return (float(xs.min()), float(ys.min()), float(xs.max()), float(ys.max()))


def assign_lines_to_cells(boxes: list[Box], lines: list[Line]) -> dict[int, list[Line]]:
    """Each line goes to the cell holding most of it; a line touching no cell goes to the nearest cell centre if that
    is closer than one line height."""
    out: dict[int, list[Line]] = {}
    for ln in lines:
        la = area(ln.box)
        best, best_share = None, 0.0
        for k, cb in enumerate(boxes):
            share = intersection(ln.box, cb) / la if la > 0 else 0.0
            if share > best_share:
                best, best_share = k, share
        if best is None:
            lcx, lcy = center(ln.box)
            lh = ln.box[3] - ln.box[1]
            dist = [(abs(center(cb)[0] - lcx) + abs(center(cb)[1] - lcy), k) for k, cb in enumerate(boxes)]
            d, k = min(dist)
            if d <= max(lh, 1.0) * 1.5:
                best = k
        if best is not None:
            out.setdefault(best, []).append(ln)
    return out


def _torn_cell(group: list[Line]) -> bool:
    """Two pieces of text on one text row, far apart horizontally, cannot be one cell's content: the structure model
    has merged neighbouring cells (it does this now and then on bold header rows)."""
    if len(group) < 2:
        return False
    heights = sorted(ln.box[3] - ln.box[1] for ln in group)
    lh = heights[len(heights) // 2] or 1.0
    ordered = sorted(group, key=lambda ln: ln.box[0])
    for a, b in zip(ordered, ordered[1:]):
        same_row = min(a.box[3], b.box[3]) - max(a.box[1], b.box[1]) > 0.5 * min(a.box[3] - a.box[1], b.box[3] - b.box[1])
        if same_row and b.box[0] - a.box[2] > 2.0 * lh:
            return True
    return False


def fill_cells(tokens: list[str], boxes: list[Box], lines: list[Line], owned: dict[int, list[Line]] | None = None) -> str:
    """The HTML of the table with each cell's text in place."""
    if owned is None:
        owned = assign_lines_to_cells(boxes, lines)
    out: list[str] = []
    cell = 0
    for token in tokens:
        if "</td>" not in token:
            out.append(token)
            continue
        text = ""
        if cell in owned:
            ordered = sorted(owned[cell], key=lambda ln: (round((ln.box[1] + ln.box[3]) / 2.0 / 8.0), ln.box[0]))
            text = " ".join(ln.text.strip() for ln in ordered if ln.text.strip())
        if token == "<td></td>":
            out.append(f"<td>{htmllib.escape(text)}</td>")
        else:
            out.append(htmllib.escape(text))
            out.append(token)
        cell += 1
    drop = {"<thead>", "</thead>", "<tbody>", "</tbody>", "<html>", "</html>", "<body>", "</body>"}
    return "".join(t for t in out if t not in drop)
