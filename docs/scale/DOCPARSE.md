# docparse: layout analysis, OCR and tables

`app/scale/docparse/` is this project's own document parser for the files where plain text extraction fails: scans, photos of
pages, multi-column layouts, tables. It is written in this repository (the code is ours); it drives three published neural
models through `onnxruntime` on CPU. No GPU, no language model, no network after `pip install`.

It is **on by default** for scanned PDFs and image files (`SCALE_DOCPARSE_MODE=scanned`) and is built into the Docker images
(`--slim` leaves it out). `SCALE_DOCPARSE_MODE=off` brings back the earlier behaviour: scanned PDFs are read by the vision model, one
call per page.

## What it does to a page

1. **Text** from the PDF's text layer where the page has one, otherwise from OCR (`SCALE_DOCPARSE_OCR=auto|always|never`).
   OCR output has its missing word gaps restored (the recogniser tends to glue words together).
2. **Layout** regions: title, text, table, figure, caption, header, footer, ... (PP-Layout CDLA, 10 classes).
3. **Tables** rebuilt into rows and columns (SLANet-plus structure model) and written as self-describing rows
   (`Item: Widget B | Price: 99.00`), which retrieves better than a pipe table; `SCALE_DOCPARSE_TABLE_STYLE=markdown` if you prefer.
4. **Header / footer / page-number removal** across the pages of a document.
5. **Paragraphs** joined across line breaks; **reading order** that follows columns instead of reading straight across.
6. **Deskew** of slightly rotated scans.

Models (all inside the pip wheels, no separate download): PP-OCRv4 detection + recognition + 180° classifier
(`rapidocr-onnxruntime`), PP-Layout CDLA (`rapid-layout`), SLANet-plus (`rapid-table`).

## Install it (already done in the Docker images)

```bash
pip install -r backend/requirements-docparse.txt
pip install --no-deps rapidocr-onnxruntime==1.4.4 rapid-layout==1.2.1 rapid-table==1.0.3   # two steps: their numpy pins conflict
SCALE_DOCPARSE_MODE=scanned     # in .env.dev / .env.prod: scanned (default) | all | off
```

Modes:

| `SCALE_DOCPARSE_MODE` | what goes through docparse |
|---|---|
| `off` | nothing local: pages the ordinary reader cannot read go to the vision model when `SCALE_OCR_ENABLED=true` (see "Pages sent to the vision model" below); with it off, a scan is skipped and image files are refused |
| `scanned` (default) | PDFs with no text layer, and image files (`.png .jpg .jpeg .tif .tiff .bmp .webp`) |
| `all` | every PDF as well: layout analysis (columns, tables as rows, running headers removed) using the PDF's own text where it has any |

Without the packages the setting logs one warning and ingest carries on with the ordinary readers. `all` changes how every PDF is
read, so compare retrieval before and after on your questions before using it; the measurements below are for scanned pages.
Settings are in `.env.dev.example` / `.env.prod.example` (`SCALE_DOCPARSE_*`).

## Pages sent to the vision model (`SCALE_DOCPARSE_MODE=off` + `SCALE_OCR_ENABLED=true`)

The ordinary local reader (the PDF's own text layer) always runs first. Only the pages it cannot read go to the vision model
(`MODEL_VISION`, one call per page):

| page | sent? |
|---|---|
| has its text layer (at least `SCALE_VISION_MIN_CHARS`, default 60 characters) | no, even with a picture on it: the page's own text is indexed |
| no usable text, with a picture over it (a scan, a photo) or many drawn lines (a CAD sheet exported as lines) | yes |
| little text (under `SCALE_VISION_MAX_TEXT_CHARS`, default 1500) and images covering at least `SCALE_VISION_IMAGE_RATIO` (default 35%) of the page (a brochure floor plan, a map) | yes |
| blank | no |

So a normal PDF that contains images keeps all of its text: nothing is removed. What stays unread is a picture inside a page that
also has text (a chart in a paper), because only the page's own text is indexed. Image files (png, jpg, tif, bmp, webp) are wrapped as
a one-page PDF and read the same way. At most `SCALE_VISION_MAX_PAGES` (default 40) pages of one document are sent; the rest keep what
text they have, and the log says how many were left. The vision model's text, plus a short description of what the page shows, is what is
indexed; no page image or separate image index is stored (`STORE_DIAGRAMS=false`). If `SCALE_OCR_ENABLED` is off or there is no
`OPENROUTER_API_KEY`, nothing is sent: a PDF with no text is skipped (`needs_ocr`) and a mixed PDF is indexed from its text pages.

## Local OCR or the vision model for scans?

| | `scanned` (local OCR, default) | `off` (vision model per page) |
|---|---|---|
| Cost | none; CPU only (1.4-7 s per page on 2 cores) | one model call per page: at 5 TB of scans, a real bill |
| Privacy | nothing leaves the machine | every scanned page is sent to the gateway |
| Tables | rebuilt as labelled rows | whatever the model writes |
| Diagrams and drawings | text only, **no diagram captions and no visual (diagram) search** | captioned and embedded, searchable as images |
| Handwriting, very poor scans | weak (not tested) | likely better (not measured) |
| Accuracy | measured only on synthetic scans (below) | **not measured here** |

The two have not been compared on the same real pages. Use `scanned` for volume and for sources that must stay inside the
building; use `off` for a library of engineering drawings or handwriting, or run both on a sample of your own pages and look.

## What was measured

All of these are in `docs/scale/bench/`. The pages are **synthetic**: real documents from the test corpus rendered to PDF, then turned
into image-only "scans" with blur, noise, skew and JPEG damage at three levels (clean / scan / poor), so the true text is known.
They are not photographs of real office paper.

Error rates on 36 pages (text recovered vs the true text; lower is better), default settings
(`SCALE_DOCPARSE_DET_BOX_THRESH=0.3`):

| level | word error | character error | reading order | table cells right | s/page (2 CPU cores) |
|---|---|---|---|---|---|
| clean | 5.3% | 3.9% | 94% | 94% | 4.0 |
| scan | 4.7% | 3.0% | 94% | 94% | 6.9 |
| poor | 13.2% | 11.5% | 94% | 84% | 6.5 |

The detector setting matters, and the result is mixed. Two other settings were run on the same 36 pages (word error, clean / scan / poor):

| detector box threshold / unclip / side limit | clean | scan | poor | page furniture left in (poor) |
|---|---|---|---|---|
| **0.3 / 1.8 / 736 min (this default)** | **5.3%** | **4.7%** | 13.2% | **1.9%** |
| 0.5 / 1.6 / 736 min (the OCR engine's own defaults) | 7.4% | 6.2% | 12.3% | 4.6% |
| 0.5 / 1.5 / 960 max (larger side limit, scales large images down) | 7.4% | 6.0% | **12.3%** | 5.1% |

So 0.3 is better on clean and scan pages (the usual case) and about one point worse on the poor ones; the 960 "max" values behave
like the engine's (side limit 960 "max" instead of 736 "min" changed nothing measurable). Values tuned for a different detector build
do not carry over to this model export. The default therefore stays 0.3,
and `SCALE_DOCPARSE_DET_BOX_THRESH=0.5` (or `SCALE_DOCPARSE_DET_LIMIT_SIDE=960` + `SCALE_DOCPARSE_DET_LIMIT_TYPE=max`) is there if the client's scans are bad.
The value was chosen on these 36 pages and confirmed only on the same pages: a starting point, to re-check on your documents
(`scripts/scale_docparse_eval.py ocr`). Seconds per page varied between runs because other jobs shared the two cores.

**Does OCR still find the right document?** 100 generated business documents (contracts, invoices, policies, purchase orders,
tickets) among 2,000 distractors, 300 questions, each document turned into a scan and read by docparse, then searched. This run was made
before the detector setting above was lowered to 0.3 (it used the engine's own detector values, 0.5 / 1.6 / 736 min), and was not repeated:

| text source | hit@1 | hit@3 | hit@10 |
|---|---|---|---|
| original text | 98.3% | 100% | 100% |
| OCR, clean scan | 95.3% | 98.0% | 99.7% |
| OCR, scan | 94.0% | 97.0% | 99.7% |
| OCR, poor scan | 94.3% | 97.0% | 100% |

OCR costs about 4 points of top-1, almost all of it on questions that name a document by its title (95% to 82-86%): OCR reads a
heading less reliably than it reads body text. Identifier questions were unaffected. Speed in that run was about 1.4 s/page (short, simple pages; the 4-7 s/page above are denser pages measured while other jobs shared the two cores).

## What it does not do

* **Real scans are harder than these.** Synthetic degradation is a proxy. Crumpled paper, shadows, stamps over text, faint
  photocopies and phone photos will be worse than the "poor" row, and I could not measure them here. Test on 50 of the client's
  worst pages before promising anything.
* Text rotated by 90° is not handled (only slight skew). **Handwriting is not supported** by these models.
* The layout model was trained on Chinese documents (CDLA). It works on English pages in the tests above, but it is not the best
  model for every page style. The engine is replaceable (`engines.py`: three small classes).
* Speed: roughly 1.4 to 7 s per page on two CPU cores. A 5 TB corpus that is mostly scans is a multi-week job on one machine;
  scale out with more workers (`SCALE_DOCPARSE_THREADS`, more worker processes) or a GPU engine behind the same protocol.
* A one-page document cannot prove that a repeating line is a running header, so such a line is kept.
* Arabic and other scripts need a different recognition model (the bundled one is Chinese/English); not tested.
* This is not a copy of any other project's parser; it uses the same published open models any such parser would use, with our own
  pipeline around them, so its behaviour will differ from theirs and was not compared against theirs.

## Tests

`tests/scale/test_docparse.py` runs the whole pipeline on fake engines (ordering, furniture removal, paragraphs, tables, options).
`tests/scale/test_docparse_models.py` runs the real models and is skipped when the packages are missing.
