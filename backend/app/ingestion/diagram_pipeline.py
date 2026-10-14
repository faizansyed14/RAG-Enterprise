"""
Diagram/drawing pipeline -- the vision-model piece added on top of the
tree engine, per the brief ("add vision model extra for diagram"). Runs
for scanned or drawing-heavy PDFs. For every page, in parallel (bounded by
_MAX_CONCURRENT_VISION_CALLS): rasterize it, call the pinned vision model
for OCR text, a short caption, a longer structured description, a figure
id, and any callout strings; store the crop in object storage; embed the
caption+description+OCR text with the pinned embedding model and upsert it
into Qdrant; write one diagram_pages row. Each page's completion is
published to ingestion/progress.py as it happens, for the frontend's
real-time ingestion view.

Also returns the per-page OCR text so ingestion/router.py can feed it into
text_to_pdf.render_ocr_pages_as_pdf -- our own vision OCR standing in for
building a tree from a document that never had a text layer.
"""

import asyncio
import base64
import json
import re
import uuid

import fitz  # PyMuPDF
from openai import AsyncOpenAI
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.object_store import get_object_store
from app.ingestion.progress import publish
from app.models.db import DiagramPage
from app.retrieval.embeddings import embed_text
from app.retrieval.vector_store import diagram_collection_name, upsert as qdrant_upsert

_CALLOUT_RE = re.compile(r"\b\d{1,3}/[A-Z]{1,4}-\d{2,4}\b")
_MAX_CONCURRENT_VISION_CALLS = 4

_VISION_PROMPT = (
    "You are looking at one page of a document, possibly a construction "
    "drawing or diagram. Return strict JSON with keys: ocr_text (all "
    "legible text on the page), caption (a one sentence description of "
    "what the page shows), description (a fuller paragraph: key elements, "
    "layout, dimensions if visible), figure_id (a drawing/sheet number if "
    "visible, else null), callouts (a list of any reference strings like "
    "'5/A-512' found on the page)."
)


def _client() -> AsyncOpenAI:
    settings = get_settings()
    return AsyncOpenAI(base_url=settings.openrouter_base_url, api_key=settings.openrouter_api_key)


_MAX_IMAGE_BYTES = 4_000_000   # vision providers refuse an image over 5 MB; a rendered photo page is far larger than a line drawing


def _render_page_png(page) -> bytes:
    """The page as a PNG, at 200 dpi if that fits the provider's image-size limit, else at the highest dpi that does."""
    png = b""
    for dpi in (200, 150, 120, 96, 72):
        png = page.get_pixmap(dpi=dpi).tobytes("png")
        if len(png) <= _MAX_IMAGE_BYTES:
            break
    return png


async def _analyze_page(image_bytes: bytes, semaphore: asyncio.Semaphore) -> dict:
    settings = get_settings()
    b64 = base64.b64encode(image_bytes).decode()
    async with semaphore:
        resp = await _client().chat.completions.create(
            model=settings.model_vision,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": _VISION_PROMPT},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
                ],
            }],
        )
    return parse_vision_reply(resp.choices[0].message.content or "{}")


def parse_vision_reply(raw: str) -> dict:
    """The model's JSON reply as a dict. Models often wrap it in a ```json fence or add a sentence before it; a reply that
    still is not JSON (cut off at the token limit, say) is kept as plain page text rather than stored as raw JSON."""
    text = raw.strip()
    fenced = re.match(r"^```[A-Za-z]*\s*(.*?)\s*(?:```)?$", text, re.S)
    candidates = [text] + ([fenced.group(1)] if fenced else [])
    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
        candidates.append(text[start:end + 1])
    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
    # Not parseable: pull the fields out one by one so a cut-off reply still yields its text, never the JSON syntax.
    ocr_text = _json_string_field(text, "ocr_text")
    return {"ocr_text": ocr_text if ocr_text is not None else raw, "caption": _json_string_field(text, "caption"),
            "description": _json_string_field(text, "description"), "figure_id": None, "callouts": []}


def _json_string_field(text: str, key: str) -> str | None:
    match = re.search(r'"' + key + r'"\s*:\s*"((?:[^"\\]|\\.)*)"', text, re.S)
    if match is None:
        return None
    try:
        return json.loads('"' + match.group(1) + '"')
    except json.JSONDecodeError:
        return match.group(1)


def page_text_with_description(ocr_text: str, caption: str | None, description: str | None) -> str:
    """What is searchable for a page that was read by the vision model: the text on it, then what it shows. The description is what
    lets a question like "master plan" or "L-shaped layout" find a drawing whose own text is only a few labels."""
    shown = " ".join(part.strip() for part in (caption, description) if part and part.strip())
    if not shown:
        return ocr_text
    return f"{ocr_text.strip()}\n\nWhat this page shows: {shown}".strip()


async def read_pages(file_path: str, page_numbers: list[int]) -> dict[int, str]:
    """Read the given pages (0-based) of a PDF with the vision model: page index -> searchable text. Nothing is stored; the text is
    what the page's own text layer would have been. One model call per page, a few at a time."""
    doc = fitz.open(file_path)
    semaphore = asyncio.Semaphore(_MAX_CONCURRENT_VISION_CALLS)
    images = {n: _render_page_png(doc[n]) for n in page_numbers}
    analyses = await asyncio.gather(*(_analyze_page(image, semaphore) for image in images.values()))
    return {n: page_text_with_description(a.get("ocr_text") or "", a.get("caption"), a.get("description"))
            for n, a in zip(images, analyses)}


async def process_pdf_pages(document_id: uuid.UUID, file_path: str, session: AsyncSession) -> list[dict]:
    """Returns [{"page_number", "text"}, ...] for every page, for the
    OCR-then-synthesize tree-build step in router.py."""
    store = get_object_store()
    doc = fitz.open(file_path)
    semaphore = asyncio.Semaphore(_MAX_CONCURRENT_VISION_CALLS)
    total_pages = len(doc)

    publish(document_id, {"phase": "ocr", "page": 0, "total": total_pages})

    pixmaps = [(i + 1, _render_page_png(doc[i])) for i in range(total_pages)]

    async def analyze_and_report(page_number: int, image_bytes: bytes) -> dict:
        analysis = await _analyze_page(image_bytes, semaphore)
        publish(document_id, {"phase": "ocr", "page": page_number, "total": total_pages,
                               "caption": analysis.get("caption")})
        return analysis

    analyses = await asyncio.gather(*(analyze_and_report(n, img) for n, img in pixmaps))

    if not get_settings().store_diagrams:
        # Text only: nothing is kept of the page image, no image vector is made (no embedding call per page).
        return [{"page_number": n, "text": page_text_with_description(a.get("ocr_text") or "", a.get("caption"), a.get("description"))}
                for (n, _image), a in zip(pixmaps, analyses)]

    ocr_pages: list[dict] = []
    embed_inputs: list[str] = []

    for (page_number, image_bytes), analysis in zip(pixmaps, analyses):
        ocr_text = analysis.get("ocr_text") or ""
        caption = analysis.get("caption")
        description = analysis.get("description")
        figure_id = analysis.get("figure_id")
        callouts = analysis.get("callouts") or _CALLOUT_RE.findall(ocr_text)

        storage_key = f"diagrams/{document_id}/page-{page_number}.png"
        store.put_object(storage_key, image_bytes, content_type="image/png")

        point_id = uuid.uuid4()
        session.add(DiagramPage(
            document_id=document_id,
            page_number=page_number,
            ocr_text=ocr_text,
            caption=caption,
            description=description,
            figure_id=figure_id,
            callouts=callouts,
            image_key=storage_key,
            qdrant_point_id=point_id,
            embedding_model=get_settings().model_embedding,
        ))

        embed_inputs.append(" ".join(filter(None, [caption, description, ocr_text])) or "(no content)")
        ocr_pages.append({
            "page_number": page_number, "text": ocr_text,
            "_point_id": point_id, "_storage_key": storage_key,
            "_figure_id": figure_id, "_caption": caption, "_description": description,
        })

    vectors = await asyncio.gather(*(embed_text(t) for t in embed_inputs))

    publish(document_id, {"phase": "embedding", "total": total_pages})
    collection = diagram_collection_name()
    for page, vector in zip(ocr_pages, vectors):
        await qdrant_upsert(collection, page["_point_id"], vector, {
            "document_id": str(document_id),
            "page_number": page["page_number"],
            "figure_id": page["_figure_id"],
            "caption": page["_caption"],
            "description": page["_description"],
            "storage_key": page["_storage_key"],
        })

    await session.commit()
    return [{"page_number": p["page_number"], "text": p["text"]} for p in ocr_pages]
