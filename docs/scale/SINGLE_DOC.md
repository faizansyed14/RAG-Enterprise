# One document, end to end: read, chunk, embed, store, ask

What was run, what it measured and what it cannot show. Raw output: `docs/scale/bench/single_doc/results.json`. Repeat it on your own
file with `scripts/scale_single_doc.py`.

## Scope

Real models on 2 CPU cores, no GPU; one document and 28 questions. It measures what each stage of this pipeline costs and
whether the right chunk comes back. It does not rank retrieval systems: the 28,230-document study in `BENCHMARKS.md` is the one built for that.
Repeat it on your own file:

```
python -m scripts.scale_single_doc --pdf doc.pdf --questions questions.json --truth truth.json --rerank-onnx-dir <dir> --out result.json
```

A returned chunk counts as correct when it contains every phrase listed in `must` for that question.

## The document and the questions

A real Kubernetes documentation page, *Storage Classes* (about 2,500 words, 2 tables, code samples, notes), printed to a 7-page PDF with a running header
and a "Page n of 7" footer; two scanned copies (image-only, normal and poor quality). 28 questions written by hand, each with the exact phrase(s)
that must appear in the answering chunk: 14 natural-language, 12 keyword / identifier, 2 table lookups. Every phrase was checked to exist in the source text.
The page builder and the answer key are in `docs/scale/bench/single_doc/`.

Real models, all on 2 CPU cores, no GPU: OCR PP-OCRv4, layout PP-Layout (CDLA), table SLANet-plus (all ONNX, via docparse); embedding all-MiniLM-L6-v2 (ONNX);
cross-encoder ms-marco-MiniLM-L-6-v2 (ONNX); a real Qdrant server. MiniLM is a small English model; a production embedding model (bge-m3 or similar) will be
slower and better, so read the embedding times as a floor and the accuracy as a floor too.

## Reading the document (extraction)

True text known, so these are measured. Word recall = share of the true words found; precision = share of the extracted words that are true.

| how it was read | time | word recall | word precision | page header / footer left in | reading order | tables found | table cells in the right row and column |
|---|---|---|---|---|---|---|---|
| digital PDF, plain text-layer extraction (what the pipeline used before) | 0.09 s | 100.0% | 97.4% | 100% | 97% | n/a (cells come out one per line) | n/a |
| digital PDF, docparse (layout + tables) | 1.2 s (2.0 s the first time, models loading) | 99.9% | 96.3% | 0% | 97% | 2 of 2 | 100% |
| scanned copy, docparse OCR | 38 s (5.5 s per page) | 86.2% | 96.3% | 0% | 83% | 2 of 2 | 96% |
| poor scan, docparse OCR | 34 s | 79.9% | 95.5% | 7% | 80% | 1 of 2 | 6% |

Reading it: on a digital PDF both get every word; docparse additionally drops the running header and footer and returns both tables with every cell in place, at about 13 times the
time (still about a second for 7 pages). Its precision is a point lower because it writes a table row as `Column: value | ...`, repeating the column names, on purpose. Scans lose about 14%
of the words (20% on the poor copy) and a table on a poor scan is mostly lost. These are one document's numbers; the 36-page study in `DOCPARSE.md` is the broader one.

## Chunk, embed, store, ask (timings are for this one document)

| stage | digital PDF, docparse text | scanned copy |
|---|---|---|
| read the file | 1.2 s | 38 s |
| split into chunks | 2 ms (14 chunks of about 214 words) | 2 ms (12 chunks) |
| embed the chunks (MiniLM on 2 cores) | 0.6 s (about 40-55 ms per chunk) | 0.6 s |
| store in Qdrant (vectors + keyword vectors + text) | 0.3 s | 0.3 s |
| **ingest total** | **about 2.1 s** | **about 39 s** |
| one question, hybrid search | 11 ms (p50), 16 ms (p95) | 12 ms |
| one question, plus cross-encoder rerank | about 1.5 s (p50) | about 1.4 s |

The cross-encoder is the one slow step here: it scores up to 30 chunks per question on 2 CPU cores.

## Did the right chunk come back? (28 questions)

hit@1 / hit@3 / hit@5 = a chunk holding the full answer phrase is first / in the first 3 / in the first 5. One question is 3.6 points.

| text read by | set-up | chunks | hit@1 | hit@3 | hit@5 |
|---|---|---|---|---|---|
| plain extraction | defaults (320 words, 40 overlap) | 14 | 82% | 100% | 100% |
| plain extraction | + cross-encoder | 14 | 82% | 93% | 96% |
| plain extraction | larger chunks (380 words, no overlap) | 9 | 82% | 100% | 100% |
| docparse | defaults | 14 | 89% | 100% | 100% |
| docparse | + cross-encoder | 14 | 89% | 100% | 100% |
| docparse | larger chunks (380 words, no overlap) | 11 | 89% | 100% | 100% |
| scanned copy (OCR) | defaults | 12 | 71% | 89% | 89% |
| scanned copy (OCR) | + cross-encoder | 12 | 75% | 89% | 89% |
| scanned copy (OCR) | larger chunks (380 words, no overlap) | 7 | 75% | 89% | 89% |
| poor scan (OCR) | defaults | 9 | 71% | 89% | 89% |
| poor scan (OCR) | + cross-encoder | 9 | 75% | 86% | 89% |
| poor scan (OCR) | larger chunks (380 words, no overlap) | 7 | 75% | 89% | 89% |

(A second score tolerates a few OCR slips in the answer phrase: scanned copy 75% / 93% / 93%. The table above is the strict one.)

What this does and does not show:

* Docparse text beat plain extraction by 2 questions at hit@1 (89% vs 82%). Two questions out of 28 is a hint, not a finding.
* The cross-encoder did not help on this document and cost 1.5 s a question; on plain text it dropped hit@3 by two questions. The 28,230-document study (`BENCHMARKS.md` section 16) found about +1 point at first rank for the blended reranker, which a 28-question test cannot see either way; so this is no evidence against it, only no evidence for it on a single page.
* Chunk size and overlap (320/40 vs 380/0) made no difference to hit@3 here. Bigger chunks make "a chunk contains the answer" easier by construction, so do not read the chunk-count column as a quality score.
* OCR costs about 2-4 questions of 28. Several of those misses are the answer phrase itself being misread (for example `efs-ap`, `1..3`), which no retrieval can fix.
* Everything here is a single English technical page with 28 questions. It cannot rank retrieval systems; the 28,230-document study in `BENCHMARKS.md` is the one built for that.
