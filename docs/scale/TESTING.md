# Testing: what each test proves, and how to repeat it on your own documents

Nothing here needs an LLM key (the language-model features are tested with a scripted fake model). Everything runs on one machine against a real Postgres and a real Qdrant. Numbers from
these tests are in `BENCHMARKS.md`; what was **not** tested is listed at the bottom of this file.

## 1. The automated suite (`backend/tests`; the scale layer is in `backend/tests/scale`). Last run: 1,727 passed, 7 skipped

```bash
cd backend
pytest tests/scale -q                                                  # embedded Qdrant (no server needed)
SCALE_TEST_QDRANT_URL=http://localhost:6333 pytest tests/scale -q      # against a real Qdrant server
pip install python-pptx odfpy                                          # optional: lets the format tests build real .pptx / .odt files
pip install -r requirements-docparse.txt && pip install --no-deps rapidocr-onnxruntime rapid-layout rapid-table   # optional: runs test_docparse_models.py (skipped otherwise)
```

| File | What it proves |
|---|---|
| `test_acl.py`, `test_api_acl.py` | Permissions: `*`, `user:`, `group:`, `sid:`; empty ACL = nobody; union over duplicate files; a forbidden document looks exactly like a missing one; preview / diagram routes refuse it |
| `test_index.py` | The hybrid index (also: exact search for small permission scopes never widens access, a failing count falls back, int8 search parameters): sparse + dense vectors, the ACL filter on **every** search leg, a search with no filter is refused, identifiers (`SUB-084`, `B27`) are found, collection name changes with the model and the text analysis |
| `test_text_and_modes.py` | The text analyser (English stemming, Arabic, CJK), query stop words, weighted score fusion, the three-signal "nothing relevant" rule and the identifier rescue |
| `test_search_scope.py` | Stage A: candidate documents are re-checked in Postgres; a user's selection is a boundary; short follow-up questions borrow the previous turn |
| `test_chunking.py` | Chunks never cross a page, carry their heading path and the document's own title, and map back to offsets in the stored page |
| `test_formats.py`, `test_formats_pipeline.py` | Built-in `.pptx` / `.odt` / `.rtf` / `.html` readers (files made by real libraries), labelled table rows, entity-bomb and oversized-archive refusal, skipped (not failed) unreadable files |
| `test_every_file.py` | The connector path reads text and code of any kind (`.log`, `.yaml`, `.sql`, no extension, unregistered extensions), refuses binaries with a reason, converts a real legacy `.doc` through LibreOffice, and reports a missing converter instead of crashing |
| `test_fidelity_real_models.py` | **Real ONNX embedder and reranker, every file type.** 19 files (txt, md, log, yaml, rtf, html, docx, pdf, pptx, odt, eml, json, xlsx, csv, and legacy doc / xls / ppt built by LibreOffice, a scanned PDF and a PNG read by OCR) among 48 look-alike distractors: right file first, planted passage returned word for word, every returned chunk a literal slice of the stored page. Skipped unless `SCALE_TEST_MODELS_DIR` points at `./models` |
| `test_queue.py` | Leases, heartbeats, backoff, dead letters, no double-claim under concurrency |
| `test_connectors.py` | Local folder, SMB (fake client), SharePoint / OneDrive via Graph (recorded-shape responses): deletes, permission changes, delta tokens |
| `test_connector_*.py` (27 files, about 1,000 tests), `test_registry_and_cursor.py` | Each of the other connectors against a **fake of that service's API**: paging, resuming a half-finished scan, deleted items, rate-limit headers, auth failures, malformed answers, read-only guarantees, and that a failed scan never deletes anything. This proves the connector follows the documented contract, **not** that the real service does |
| `test_rerank.py` | The cross-encoder blend / gate / cache logic (fake scorer) |
| `test_docparse.py` | Layout, reading order, furniture removal, paragraphs, tables, deskew, options, on fake OCR / layout / table engines |
| `test_docparse_models.py` | The **real** OCR, layout and table models on generated pages (skipped when the packages are not installed) |
| `test_llm_enrich.py` | The model client (JSON repair, retries, timeouts), enrichment, query keywords and translation, all with a scripted fake model: what is sent, what is stored, that a failing model never fails ingest or search |
| `test_reconcile.py` | Self-healing index: deleted chunks, a deleted vector alone, a dropped collection (every kind of document), leftovers of an interrupted rebuild, a changed embedding model or chunk size, orphan chunks, hand-written chunks not mistaken for damage, a failed rebuild keeps the old chunks, the whole-index size check, the scheduler, the queue cap; on a healthy index it changes nothing |
| `test_families.py` | Near-duplicate families: copies beyond the cap move behind other documents, nothing is dropped or reordered otherwise, shared boilerplate is not a family, off at cap 0 |
| `test_caches.py` | Question-embedding cache and reranker-score cache: eviction, one embedding per repeated question, same ranking with and without the cache |
| `test_chunk_edit.py` | Chunk editor: edit / restore / switch off / add / delete, carry-over on a changed file, orphaning, history, access rules over HTTP |
| `test_end_to_end.py`, `test_legacy_documents.py`, `test_name_index.py` | Scan -> queue -> worker -> index -> search for real files; back-filling documents that existed before the system; unique-name lookups |
| `test_ab_harness.py`, `test_eval_scoring.py`, `test_calibrate_and_realeval.py`, `test_e2e_checker.py`, `test_make_typos.py` | The measuring tools themselves (a scoring tool that cannot fail proves nothing): the A/B comparer, hit@k scoring, threshold calibration, the real-file fidelity check **including that it rejects text from another document**, the typo generator |

One test in the original suite fails and always did (`test_text_to_pdf.py::...arabic_text_survives_round_trip`, a font-rendering test); it is not caused by the enterprise layer.

### One real document, end to end

`scripts/scale_single_doc.py` times read / chunk / embed / store / query and scores 28 hand-written questions on one PDF with real models; results and method in `SINGLE_DOC.md`.

## 2. Real files end to end: "is it getting the real document?"

```bash
python -m scripts.scale_e2e_real --source SHARE --manifest manifest.jsonl --golden golden.json --sample 1500 --questions 300 --out report.json
```

Uses the production path (connector -> scan -> queue -> worker -> parse -> chunk -> embed -> index -> access-checked search).
Only the object store is a temp folder. The test copies a sample of your files into a work folder and **converts about
half of them into real PDF, DOCX, HTML, PPTX and RTF files**, so the real parsers run, then:

| Check | Pass means |
|---|---|
| Scan + ingest | every file indexed, none failed / pending; documents per second printed |
| Retrieval | where the right **file** ranks among the ingested documents, asked as a user who may read all |
| Same file | the stored content hash equals the SHA-256 of the bytes on disk at that path (the returned document **is** that file) |
| Same text | each of the top 3 returned chunks is compared with the original text the file was made from: *exact* (same words, same order) or *near* (>= 70 % of its 6-word sequences: Markdown / code-fence markup was normalised). A **control** compares the same chunks with random other documents and must match **none** |
| Same page | the stored page text contains the returned passage (the agent reads pages) |
| Access | 600 searches as users holding one group, and as a user holding none: **0** results outside their folders |
| Change | edit 5 files, delete 5, add 1, rescan: only the changed files are re-queued, the edits are found, the deleted files are gone |

It refuses to run unless the database name contains `test`, `e2e` or `bench`: it creates and deletes rows.

## 3. Retrieval quality: "does it pick the correct document among many?"

```bash
python -m scripts.scale_realeval run --share SHARE --manifest manifest.jsonl --golden golden.json --docs-file docs.txt \
       --asker group:a,group:b --matrix matrix.json --out result.json
```

* `golden.json`: `[{"id", "q", "expect_paths": [...], "style", "source"}]`. Ground truth is the file the question was written
  from; a verbatim duplicate of that file counts as correct (listed in `expect_paths`).
* `matrix.json`: named setting overrides (`{"lexical only": {"fusion_dense_weight": 0.0}, ...}`). Each distinct index setting builds its
  own collection; query-time settings re-use it. Embeddings are cached in SQLite so a re-run costs seconds.
* Reports hit@1/3/5/10/20/40, MRR, number of questions never found, latency p50/p95, split by question type and by source.
* Run it with `--reuse-index` to ask new questions (typos, other users) of an index you already built.

Questions come in five kinds: *nl* (a natural question), *kw* (the keywords a person would type), *name* (describe the
document by its title / parties), *id* (a bare identifier) and *idnl* (an identifier inside a question). Report them
separately: an average hides that identifiers and paraphrases fail in opposite ways.

Companions:

| Tool | Use |
|---|---|
| `scripts/scale_make_typos.py` | A misspelled copy of the golden set (one or two words per question) |
| `scripts/lucene_reference/` | The same chunks and questions through **Lucene BM25** (what Elasticsearch / OpenSearch use), plain and with fuzzy matching: the Elasticsearch question as a measurement (`ELASTICSEARCH_DECISION.md`) |
| `scripts/scale_ref_strategy.py` | The "lexical pre-filter + dense + coverage re-rank" strategy of Elasticsearch-based RAG stacks, in numpy, on the same data |
| `scripts/scale_calibrate.py` | Chooses `SCALE_MIN_CONFIDENCE` / `SCALE_MIN_LEXICAL` / `SCALE_MIN_COVERAGE` from your own answerable and unanswerable questions (include 20-40 near-topic unanswerable ones) and prints the in-sample and the out-of-sample figure; quote the second |
| `scripts/scale_eval.py`, `scale_ab.py` | Golden-set retrieval check with permission probes; answer-level A/B (scale off vs on) through the running app |
| `scripts/scale_bench.py` | Ingest speed and query latency as the library grows |

## 4. How the test corpus was built (so you can judge it)

Public documents, fetched from GitHub: the Kubernetes website docs, MDN web docs (`mdn/content`), the SPDX licence texts
(826 licences, many near-duplicates **on purpose**) and the Reuters-21578 newswire (via NLTK data), 28,030 files; plus 200
synthetic enterprise documents (contracts, invoices, purchase orders, tickets, policies) that look alike and differ only in
identifiers, names and amounts, which is the hard case for "find the one document". Each source is a separate permission
group. Questions were written from sampled passages **before** any retrieval was run. The settings were afterwards chosen on the same
730 natural-language / keyword questions, so those choices are in-sample: the dense-weight sweep (0.2 to 0.4) moved results by less
than a point, the fusion change (RRF -> weighted) by 11, so over-fitting is not what explains the headline, but you should re-run
the sweep on your own questions before trusting the exact values. The 600 identifier-style questions, the 28 230-document
scale-up, 919 misspelled questions and 690 questions asked as a user with narrow permissions came later and were used to confirm the choice
(and to show where it fails), not to pick new values. **200 held-out questions** (100 natural-language, 100 keyword, written afterwards) were used for no choice at all.

Limits you must keep in mind: the embedding model was a small English one (all-MiniLM-L6-v2), BGE-M3 could not be
downloaded (a small cross-encoder was; `BENCHMARKS.md` section 16), so those numbers are for a weak embedder (a better one helps the meaning side; it does not
change the conclusions about fusion); one machine with 2 CPUs; the questions were written by a model, which tends to reuse
the passage's wording, so *nl* and *kw* scores are optimistic compared with real users' phrasing; "right document" is the
source document, so a sibling that also answers the question counts as wrong at hit@1 (this makes hit@1 pessimistic and
hit@10 more meaningful for "was it found").

## 5. What has NOT been tested

* 5 TB, or more than ~100 000 chunks / 28 230 documents of **retrieval** testing (ingest was run on 3 000 and 1 500 documents).
* BGE-M3, a larger or multilingual reranker, a GPU, or any LLM answer. Nothing measures whether the final **answer** is better. Enrichment and query rewriting have **mechanics tests only** (no language model was available).
* OCR and layout on **real** scanned paper (the measurements used synthetic scans), handwriting, rotated pages, drawings, spreadsheets with formulas.
* Every connector against its **real** service (fakes only).
* The chunk-editor page in a browser (type-checked and built only; the Sources page was opened in a browser).
* A real Microsoft tenant, a Windows file server with NTFS ACLs, Entra group membership, SharePoint site groups.
* Qdrant 1.19 (the server tested is 1.15.4), multi-node, backups, restore.
* Arabic with a multilingual embedder (only the keyword side was measured; the embedder was English-only).
* An Elasticsearch / OpenSearch server (Lucene BM25 was used in its place).
* The Docker images and compose stacks in a running environment (validated statically only).

## 6. Repeating the measurements of the later additions

(The knowledge-graph and RAPTOR measurements, `BENCHMARKS.md` sections 18 and 19, were made with scripts that were removed together with those features.)

```bash
python -m scripts.scale_realeval run ... --rerank-cache rr.sqlite --matrix matrix_rr.json   # cross-encoder variants (BENCHMARKS 16)
python -m scripts.scale_docparse_eval ocr --share SHARE --manifest manifest.jsonl --docs 36 --restore-only --interleave   # BENCHMARKS 17
python -m scripts.scale_docparse_eval retrieval --share SHARE --manifest manifest.jsonl --golden golden.json              # retrieval over OCR text
```

