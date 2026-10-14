"""The three neural models docparse drives: text detection + recognition (OCR), page layout, table structure.

Each is a tiny adapter around an ONNX model package, loaded on first use and shared by every thread in the process.
Everything else in docparse sees only the protocols below, so a different engine (a GPU service, PaddleOCR, a
newer layout model) is one class, and the tests run the whole pipeline on fakes.

Models used by default (all run by onnxruntime on CPU, no GPU, no network after `pip install`):
  OCR     PP-OCRv4 detection + recognition + 180-degree classifier   (rapidocr-onnxruntime wheel)
  layout  PP-Layout CDLA, 10 classes                                  (rapid-layout wheel)
  tables  SLANet-plus table structure recogniser                      (model file from the rapid-table wheel)
"""

from __future__ import annotations

import logging
import threading
from typing import Protocol

import numpy as np

from .geometry import poly_to_box
from .types import Line, Region

log = logging.getLogger(__name__)


class DocparseUnavailable(RuntimeError):
    """The optional packages are not installed (pip install -r requirements-docparse.txt)."""


class OcrEngine(Protocol):
    name: str

    def __call__(self, image: np.ndarray) -> list[Line]: ...


class LayoutEngine(Protocol):
    name: str

    def __call__(self, image: np.ndarray) -> list[Region]: ...


class TableEngine(Protocol):
    name: str

    def __call__(self, image: np.ndarray, lines: list[Line]) -> str | None:
        """HTML of the table in `image`, cells filled from `lines` (positions relative to the image), or None."""


# ---------------------------------------------------------------------------------------------------------- OCR

class RapidOcrEngine:
    name = "ppocr-v4"

    # The text detector's default box threshold (0.5) silently drops faint or thin lines -- measured on synthetic scans: whole
    # lines of a paragraph went missing on a CLEAN scan. 0.3 / unclip 1.8 finds them (see docs/scale/DOCPARSE.md for the numbers).
    def __init__(self, *, min_score: float = 0.5, threads: int = 2, det_box_thresh: float = 0.3, det_unclip: float = 1.8,
                 det_limit_side: int = 0, det_limit_type: str = "min") -> None:
        self._det = {"box_thresh": det_box_thresh, "unclip_ratio": det_unclip}
        try:
            from rapidocr_onnxruntime import RapidOCR
        except ImportError as exc:                                           # pragma: no cover - environment
            raise DocparseUnavailable("rapidocr-onnxruntime is not installed (see requirements-docparse.txt)") from exc
        extra = {"det_limit_side_len": det_limit_side, "det_limit_type": det_limit_type} if det_limit_side > 0 else {}
        self._ocr = RapidOCR(intra_op_num_threads=max(1, threads), inter_op_num_threads=1, **extra)
        self._min_score = min_score
        self._lock = threading.Lock()          # the det post-processor keeps per-call state

    def __call__(self, image: np.ndarray) -> list[Line]:
        with self._lock:
            result, _ = self._ocr(image, return_word_box=True, **self._det)
        lines: list[Line] = []
        for item in result or []:
            poly, text, score = item[0], item[1], float(item[2])
            if not text or not text.strip() or score < self._min_score:
                continue
            chars = None
            if len(item) >= 5 and item[3] and item[4] and len(item[3]) == len(item[4]):
                chars = [(float(poly_to_box(p)[0]), float(poly_to_box(p)[2]), c) for p, c in zip(item[3], item[4])]
            lines.append(Line(box=poly_to_box(poly), text=text, score=score, chars=chars))
        return lines


# -------------------------------------------------------------------------------------------------------- layout

# CDLA label -> our label
_LABELS = {
    "text": "text", "title": "title", "figure": "figure", "figure_caption": "figure_caption", "table": "table",
    "table_caption": "table_caption", "header": "header", "footer": "footer", "reference": "reference",
    "equation": "equation",
}


class RapidLayoutEngine:
    name = "pp-layout-cdla"

    def __init__(self, *, min_score: float = 0.4) -> None:
        try:
            from rapid_layout import RapidLayout
        except ImportError as exc:                                           # pragma: no cover - environment
            raise DocparseUnavailable("rapid-layout is not installed (see requirements-docparse.txt)") from exc
        self._layout = RapidLayout(conf_thresh=min_score, iou_thresh=0.5)
        self._lock = threading.Lock()

    def __call__(self, image: np.ndarray) -> list[Region]:
        with self._lock:
            out = self._layout(image)
        regions = []
        for box, label, score in zip(out.boxes or [], out.class_names or [], out.scores or []):
            regions.append(Region(
                box=(float(box[0]), float(box[1]), float(box[2]), float(box[3])),
                label=_LABELS.get(str(label).lower(), "text"), score=float(score),
            ))
        return regions


# -------------------------------------------------------------------------------------------------------- tables

def make_table_engine(threads: int = 2) -> TableEngine:
    """SLANet-plus through onnxruntime (the model file ships inside the rapid-table wheel)."""
    try:
        from .table_model import SlanetTable

        return SlanetTable(threads=threads)
    except (ImportError, FileNotFoundError) as exc:                          # pragma: no cover - environment
        raise DocparseUnavailable(f"table model unavailable: {exc}") from exc


# ------------------------------------------------------------------------------------------------------ the set

class Engines:
    """OCR, layout and table models, created lazily and once. `Engines.default()` is the shared process-wide set."""

    _default: "Engines | None" = None
    _guard = threading.Lock()

    def __init__(self, ocr: OcrEngine | None = None, layout: LayoutEngine | None = None, table: TableEngine | None = None,
                 *, ocr_factory=None, layout_factory=None, table_factory=None) -> None:
        self._ocr, self._layout, self._table = ocr, layout, table
        self._factories = {"ocr": ocr_factory, "layout": layout_factory, "table": table_factory}
        self._mine = threading.Lock()

    def _get(self, kind: str):
        attr = f"_{kind}"
        if getattr(self, attr) is None:
            with self._mine:
                if getattr(self, attr) is None:
                    factory = self._factories[kind]
                    if factory is None:
                        raise DocparseUnavailable(f"no {kind} engine configured")
                    setattr(self, attr, factory())
        return getattr(self, attr)

    @property
    def ocr(self) -> OcrEngine:
        return self._get("ocr")

    @property
    def layout(self) -> LayoutEngine:
        return self._get("layout")

    @property
    def table(self) -> TableEngine:
        return self._get("table")

    @classmethod
    def default(cls, *, min_ocr_score: float = 0.5, threads: int = 2) -> "Engines":
        with cls._guard:
            if cls._default is None:
                from app.scale.settings import get_scale_settings

                cfg = get_scale_settings()
                cls._default = cls(
                    ocr_factory=lambda: RapidOcrEngine(min_score=min_ocr_score, threads=threads, det_box_thresh=cfg.docparse_det_box_thresh,
                                                       det_unclip=cfg.docparse_det_unclip, det_limit_side=cfg.docparse_det_limit_side,
                                                       det_limit_type=cfg.docparse_det_limit_type),
                    layout_factory=RapidLayoutEngine, table_factory=lambda: make_table_engine(threads),
                )
            return cls._default

    @classmethod
    def forget(cls) -> None:
        with cls._guard:
            cls._default = None


def available() -> bool:
    """Are the model packages installed? (Importing them is cheap; loading the models is not done here.)"""
    import importlib.util

    return all(importlib.util.find_spec(m) is not None for m in ("rapidocr_onnxruntime", "rapid_layout", "rapid_table", "cv2")) and (
        importlib.util.find_spec("pymupdf") is not None or importlib.util.find_spec("fitz") is not None
    )
