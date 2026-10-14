# Benchmarks: what was measured, how, and what it does not show

Two kinds of measurement are in this file:

* **Sections 1-10: retrieval quality and fidelity** on tens of thousands of real documents with a real (small) embedding
  model: does the search return the right document, among many, and is the text it returns the text of the real file?
* **Sections 11-15: plumbing speed** (queue, filters, SQL) measured earlier with a fake embedder. They say nothing
  about answer quality; they are kept because the shape of the curves still holds.
* **Sections 16-19: the later additions** (local cross-encoder reranker, docparse layout / OCR, knowledge graph, RAPTOR).
  Each says what was measured and what was not. **Sections 18 and 19 describe features that were measured, did not help, and
  were removed from the code.** Features that need a language model (enrichment, query rewriting) have mechanics tests only;
  no accuracy claim is made for them (`LLM_FEATURES.md`).

Raw reports are in `docs/scale/bench/`. How to repeat each measurement on your own files is in `TESTING.md`.

## 1. Corpora and method

| Name | Documents | Questions | Content |
|---|---|---|---|
| **S** | 4,059 | 730 (365 natural-language, 365 keyword) | Kubernetes docs, MDN web docs, SPDX licence texts (many near-duplicates on purpose), Reuters-21578 newswire |
| **Mixed** | 4,259 | 1,330 (S + 600 identifier-style) | S + 200 synthetic enterprise documents (contracts, invoices, purchase orders, tickets, policies) that differ only in identifiers, names and amounts |
| **F** | **28,230** (~98,000 chunks) | 1,330 + 200 held-out + 919 misspelled + 690 selective-permission | 28,030 public files + the same 200 synthetic ones. Each source is its own permission group |
| **Arabic** | 460 | 1,387 | ARCD (Arabic reading-comprehension passages) |
| **Real files** | 1,500 (converted to real PDF / DOCX / HTML / PPTX / RTF) | 299 | A sample of F, through the production pipeline |

* "Right document" = the file the question was written from (a verbatim duplicate counts as right). A sibling page that
  also answers the question counts as **wrong at hit@1**, so hit@1 is pessimistic; hit@10 is closer to "was it found".
* `missed` = questions whose document is not among the top 40 candidates (what Stage B would never get to read).
* Questions were written by a model from sampled passages **before** retrieval was run. Models tend to reuse a
  passage's wording, so *nl* and *kw* scores are **optimistic** compared with real users' phrasing.
* Embedder: all-MiniLM-L6-v2 (ONNX, 384-d, English only). No BGE-M3 could be downloaded here. A better embedder helps the
  meaning side; it was not tested. (A small cross-encoder reranker was obtained later; section 16.)
* **Which numbers are out-of-sample.** The settings (fusion mode, dense share 0.3, stop words, stemming) were chosen
  on the 730 *nl/kw* questions of S, so those are **in-sample**. The 600 identifier questions, the 28,230-document
  scale-up, the 919 misspelled questions and the 690 selective-permission questions came later and were used to
  confirm or to show where it fails. The **200 held-out questions** (100 nl + 100 kw, written afterwards from passages
  never used before, MDN / Kubernetes / news) were used for no choice at all.
* **How much to trust a difference.** With 1,330 questions a gap under about 2 points at hit@1 is a tie; with 200
  questions, under about 5 points. Read "+0.45 point" as "no measurable difference".
* One machine, 2 CPUs, Qdrant server 1.15.4, Postgres 16.

## 2. The finding that moved the numbers: how the two searches are combined

The index originally combined the meaning search and the keyword search with **reciprocal-rank fusion** (RRF; ranks
only), plus an "identifier boost" (extra votes for exact codes such as `SUB-084`) because plain RRF lost them.
The alternative is **weighted score fusion**. Switching to a weighted sum of min-max-normalised
scores (30 % meaning, 70 % keywords) gave the best result on every set, and removed the need for the boost:

| Set | RRF + identifier boost (previous default) | RRF without the boost | **Weighted 0.3 (new default)** |
|---|---|---|---|
| S, 730 nl/kw questions (in-sample) | 74.3 % / MRR 0.847 | 82.2 % | **85.1 %** / MRR 0.913 (85.6 % / 0.915 with stemming) |
| Mixed, 1,330 q (incl. 600 identifier / name questions) | 83.1 % / MRR 0.899 | 81.5 % | **91.7 %** / MRR 0.952 |
| **F, 28,230 docs, 1,330 q** | 72.4 % / MRR 0.821 | not run | **82.7 %** / MRR 0.888 |
| **F held-out, 200 q (out-of-sample)** | 73.0 % / MRR 0.844 | not run | **84.5 %** / MRR 0.906 |

How to read it, without flattering the change:

* The **+10 point** headline is against the previous default, whose identifier boost *hurt* natural questions (S: 74.3 %
  with the boost, 82.2 % without). Against RRF **without** the boost, weighted fusion is **+2.9 points** on S.
* Without the boost, RRF loses the exact-identifier questions (mixed set: bare identifiers 80.5 %, identifier inside a
  question 72.0 %). Weighted fusion finds them **100 %** of the time with no special rule and is also the best on
  keyword (90.7 % vs 86.6 %) and document-name questions (97.0 % vs 90.0 %). That combination, not any single number,
  is why it is the default.
* The gain holds out of sample (held-out +11.5 against the previous default).
* The dense share barely matters: 0.2 / 0.25 / 0.3 / 0.4 gave 85.2 / 85.8 / 85.1 / 85.2 % on S (noise), 0.5 gave 84.0 %,
  0.7 gave 79.9 %.

`SCALE_FUSION=rrf` and `dbsf` (81.6 % on S) remain as options.

## 3. 28,230 documents: does it pick the right document among many?

Default settings, asked as a user who may read everything. Latency is the search itself at ~98,000 chunks and
**excludes** the time to embed the question (the embeddings were cached).

| Mode | hit@1 | hit@3 | hit@10 | hit@40 | MRR | never found (of 1,330) |
|---|---|---|---|---|---|---|
| **Hybrid (default)** | **82.7 %** | 94.4 % | 98.0 % | 99.5 % | 0.888 | **7** |
| Keywords only (BM25-style) | 82.2 % | 92.5 % | 97.5 % | 99.3 % | 0.880 | 10 |
| Meaning only (dense) | 47.0 % | 64.7 % | 79.0 % | 91.8 % | 0.581 | 109 |
| RRF (previous default) | 72.4 % | 91.1 % | 97.4 % | 99.6 % | 0.821 | 6 |
| Hybrid + coverage re-rank | 82.4 % | 94.4 % | 98.0 % | 99.5 % | 0.888 | 7 |

Latency: p50 **10.5 ms**, p95 **12.7 ms** for the hybrid search with the permission filter inside the query. The
coverage re-ranker raised that to p50 52.6 / p95 76.1 ms for no measurable accuracy gain, so it is **off by default**.

By question type (hybrid default):

| Type | What it is | hit@1 | hit@10 | never found |
|---|---|---|---|---|
| `id` | a bare identifier (`SUB-084`) | 100 % | 100 % | 0 |
| `idnl` | an identifier inside a question | 100 % | 100 % | 0 |
| `name` | describe the document by title / parties | 97.0 % | 100 % | 0 |
| `kw` | the keywords a person would type | 77.3 % | 98.1 % | 1 |
| `nl` | a natural question | **61.4 %** | 94.5 % | 6 |

By source: contracts 98.5 %, invoices 100 %, purchase orders 100 %, tickets 98.3 %, policies 97.8 %, news 81.1 %,
licences 75.6 %, Kubernetes 68.1 %, **MDN 61.0 %** (hit@10: 100 / 100 / 100 / 100 / 100 / 97.2 / 95.6 / 96.9 / 95.7 %).
The low rows are sets with many sibling pages about the same topic (MDN, Kubernetes), where a second page that also
answers the question counts as wrong at hit@1; hit@10 is the fair number there.

What this says: with 28,230 documents the right document is in the top 10 in 98 % of questions and in the top 40
(what Stage B reads) in 99.5 %. Exact identifiers are found every time. **Natural-language questions are the weak point
(61 % first, 94.5 % in the top 10)** with this small English embedder; Stage B (the agent) reads the candidates and
picks, which is what the 40-document window is for.

## 4. Held-out questions (out-of-sample), and what the hybrid actually buys

200 questions never used for any setting, F corpus:

| Mode | hit@1 | hit@10 | MRR | never found |
|---|---|---|---|---|
| Hybrid (default) | 84.5 % | 98.5 % | 0.906 | 0 |
| **Keywords only** | **87.5 %** | 98.0 % | **0.923** | 0 |
| Meaning only | 67.0 % | 95.0 % | 0.774 | 6 |
| RRF | 73.0 % | 99.0 % | 0.844 | 0 |

Read this honestly: **on these 200 the keyword-only mode is 3 points ahead at hit@1** (6 questions; inside the noise
band for n = 200, but it points the other way from what a believer in hybrid search would hope). Together with F
(82.7 vs 82.2 %), the conclusion is:

* With this English-only 384-dimension embedder, the hybrid is **about as good as good keyword search at first rank**,
  not clearly better.
* Its measured value is elsewhere: it has **fewer total misses** (F: 7 vs 10; S: 1 vs 1), it does not collapse where the
  keyword side is blind (typos: section 5; paraphrases: `nl` 61.4 vs 60.3 %), and the meaning side is the part that a
  stronger multilingual embedder (BGE-M3, in the deploy overlay, **not run**) would improve.
* Dense-only is clearly the worst (F: 47 %; mixed set with identifiers: 60 %, 42 never found), so a pure vector search,
  which is what many RAG stacks start with, would have been the wrong baseline.

## 5. Misspelled questions, selective permissions, and the Elasticsearch-style design

**Typos.** The 919 *nl / kw / name* questions of F with one or two words misspelled (`scripts/scale_make_typos.py`):

| | original wording | misspelled |
|---|---|---|
| Hybrid (default): hit@1 / hit@10 / never found | 75.4 % / 97.1 % / 7 | **69.6 %** / 94.7 % / 17 |
| Keywords only | 74.8 % / 96.5 % / 10 | 66.2 % / 93.5 % / 22 |
| Meaning only | 58.3 % / 89.1 % / 33 | 48.1 % / 82.2 % / 77 |
| Lucene BM25 (the Elasticsearch / OpenSearch engine) | 74.9 % / 95.5 % / 12 | 66.5 % / 92.0 % / 30 |
| Lucene BM25 **with fuzzy matching** | n/a | 68.7 % / 93.6 % / 23 |

Misspellings cost the hybrid 5.8 points at first rank; the meaning side catches some of them, and the hybrid ends up
slightly ahead of Lucene's fuzzy matching (+0.9 at hit@1, 6 fewer misses; a tie at this size). A spell-corrector is not built.

**Selective permissions.** 690 questions asked as a user who may read only one group's folders (`group:legal`:
the enterprise documents and the licences, 1,026 of 28,230 documents = 3.6 % of the library): hit@1 **96.1 %**, hit@10 99.4 %, 1 never found (Lucene with the same restriction: 95.9 % /
98.8 % / 2). The permission filter runs inside the vector query, so a restricted user is not left with the leftovers of
a global top-k.

**The Elasticsearch-style design.** An Elasticsearch-style path requires a chunk to share terms with the query
(a hard pre-filter) and lets the dense side rank the survivors. Rebuilt as `scripts/scale_ref_strategy.py` and run on
the same chunks, questions and embeddings:

| | hit@1 | hit@10 | MRR | never found |
|---|---|---|---|---|
| F, 1,330 q: that design | 80.9 % | 96.5 % | 0.869 | **27** |
| F, 1,330 q: **this index** | **82.7 %** | **98.0 %** | **0.888** | **7** |
| F held-out, 200 q: that design | 84.0 % | 98.0 % | 0.899 | 2 |
| F held-out, 200 q: this system | 84.5 % | 98.5 % | 0.906 | 0 |
| S, 730 q: that design (3 variants) | 82.2-83.3 % | 98.0-98.2 % | 0.889-0.895 | 10-11 |
| S, 730 q: this system | 85.1-85.6 % | 99.5-99.6 % | 0.913-0.915 | 1 |

It is a reconstruction of the design, not an Elasticsearch deployment. The consistent difference is recall: the hard keyword
pre-filter loses questions the fusion keeps.

## 6. Is Elasticsearch needed?

Short answer: **no, not for accuracy**, at the sizes and languages tested. The measurement and the cases where you
would still add it are in `ELASTICSEARCH_DECISION.md`. The core of it, same chunks and same questions:

| Set | This index (hybrid) | Keywords only | Lucene BM25 |
|---|---|---|---|
| S 730 q, hit@1 / MRR / never found | 85.1 % / 0.913 / 1 | 83.7 % / 0.900 / 1 | 83.0 % / 0.895 / 2 |
| Identifier set 600 q, hit@1 | 99.0 % | 98.8 % | 99.0 % (93.8 % with the standard analyser) |
| **F 1,330 q** | **82.7 % / 0.888 / 7** | 82.2 % / 0.880 / 10 | 82.3 % / 0.879 / 12 |
| F held-out 200 q | 84.5 % / 0.906 / 0 | 87.5 % / 0.923 / 0 | 85.0 % / 0.905 / 1 |
| F misspelled 919 q | 69.6 % / 0.789 / 17 | 66.2 % / 0.762 / 22 | 66.5 % / 0.759 / 30 (68.7 % / 0.781 / 23 fuzzy) |
| F selective permissions 690 q | 96.1 % / 0.974 / 1 | 95.9 % / 0.972 / 2 | 95.9 % / 0.971 / 2 |
| Arabic 460 docs, 1,387 q | 67.6 % / 0.784 / 34 (keywords + stemming; hybrid 67.2 %) | | **69.7 % / 0.800 / 29** (Arabic analyser) |

Lucene is the engine inside Elasticsearch and OpenSearch. An Elasticsearch **server** was not run; Lucene was driven
through Anserini on the same chunks. So: Qdrant's sparse vectors with IDF behave like Lucene's BM25 here, and the one
place Lucene is measurably ahead is Arabic, thanks to a real Arabic analyser (+2 points at first rank).

## 7. "Nothing relevant": answering "I don't know" instead of guessing

Off by default (all three thresholds 0). The search turns a question away only when the best chunk is weak on **all**
of three independent signals: meaning (top cosine), keywords (top BM25) and **coverage** (the share of the question's
wording the chunk holds); a question that contains an identifier found in the corpus (`SUB-084`) is never turned
away. A single signal is not enough: identifiers have a low cosine and a high BM25, paraphrases the opposite.

Calibration (`scripts/scale_calibrate.py`) on F: **1,530 answerable** questions (the golden set) and **112 unanswerable**
ones: 73 *far* (a topic the corpus does not have) and 39 *near* (an invented product, clause or
person inside a real topic, the hard case). Separation (1.0 perfect, 0.5 none): cosine 0.937, BM25 0.982, coverage 0.918.

| Accept losing at most | Thresholds (cosine / BM25 / coverage) | In-sample: kept / turned away | **Out-of-sample** (10 random 50/50 splits): kept / turned away |
|---|---|---|---|
| 1 % | 0.6973 / 30.28 / 0.2117 | 99.2 % / 79 % | **98.8 % / 80 %** |
| 3 % | 0.7274 / 30.28 / 0.4867 | 97.6 % / 88 % | **97.1 % / 85 %** |
| 5 % | 0.6973 / 41.78 / 0.2619 | 95.6 % / 93 % | **94.8 % / 92 %** |

Applied end to end with the 3 % row (in-sample): **2.5 % of answerable questions wrongly turned away** (38 of 1,530; all
*nl* / *kw*: 17/465 and 21/465; **0** of 600 identifier and name questions), **93.2 %** of far unanswerable questions
(68/73) and **76.9 %** of near ones (30/39) turned away.

Limits: the unanswerable questions were written by a model; only 39 are near-topic, so the near figure moves by several
points with a few questions; the thresholds depend on the embedder and corpus statistics and **must be re-measured on your
data**. Quote the out-of-sample column. Refusal here is only a retrieval-level guard: whether it improves the final answers
is not measured.

## 8. Real files end to end: is it getting the real document?

`scripts/scale_e2e_real.py`: 1,500 files from F copied into a work folder, **641 of them converted into real PDF (235),
DOCX (161), HTML (96), PPTX (89) and RTF (60) files** (859 stay Markdown / text), then the production path: connector scan
-> queue -> worker -> parse -> chunk -> embed (real MiniLM) -> Qdrant -> access-checked search -> change detection.

| Check | Result (clean run) |
|---|---|
| Ingest | 1,500 / 1,500 indexed, 0 failed, 0 skipped. **1.54 documents / s** with real embedding on 2 shared CPUs (976 s) |
| Retrieval, 299 questions | right file first **97.3 %**, in top 3 99.7 %, in top 5 **100 %**, MRR 0.984, 0 never found; p50 26.7 ms / p95 39.1 ms **including** query embedding and the Postgres permission re-check |
| Same file | stored content hash equals the SHA-256 of the bytes on disk: **299 / 299**; path matches: 299 / 299 |
| Same text | 897 returned passages (top 3 of each answer) compared with the original text: **840 exact, 57 near** (>= 70 % of 6-word runs; Markdown markup normalised), **0 unmatched** |
| Control | the same passages compared with a random other document: **0 / 300 match** (the check can say no) |
| Same page | the stored page contains the returned passage: 897 / 897 |
| Access | 600 searches as users holding one group, 120 as a user holding none: **0 results outside their folders** |
| Change | edit 5, delete 5, add 1 files, rescan: exactly the **6** changed files re-queued; 5/5 edits found, new file found, 5/5 deleted files gone |

History of this check, so the number is not read as better than it is: the **first run flagged 34 of 812 returned
passages** (25 distinct, 14 of them one generated PPTX) as "not found in the original" because the check required an
exact word match; on inspection they differ from the original in formatting only (Markdown markup, a slide title the
test's converter adds), and they pass the near tier. The check was split into exact / near tiers and given the control above. A **second run reused a
Qdrant collection that still held the first run's points** (a harness bug: the test now deletes its collection first);
the **third run is the clean one reported above**. The first-run and second-run reports are in `docs/scale/bench/`.

Scope: this proves the index holds the text of the real files and returns the right ones, for the formats the
test builds. It does not test scanned PDFs, tables with merged cells, drawings, OCR or password-protected files.

## 9. Arabic

460 ARCD passages, 1,387 questions, **keyword side only** (the embedder is English-only):

| | hit@1 | hit@10 | MRR | never found |
|---|---|---|---|---|
| Normalisation only | 64.2 % | 92.0 % | 0.749 | 64 |
| **+ light stemming (default)** | 67.6 % | 94.9 % | 0.784 | 34 |
| + stemming, no stop words | 65.3 % | 94.7 % | 0.768 | 34 |
| Hybrid with the English-only embedder | 67.2 % | 94.2 % | 0.781 | 37 |
| Hybrid with RRF | 39.1 % | 92.1 % | 0.596 | 40 |
| Lucene, standard analyser | 57.4 % | 87.4 % | 0.685 | 123 |
| Lucene, **Arabic analyser** | **69.7 %** | 95.8 % | 0.800 | 29 |

So the built-in Arabic handling closes most of the gap to a real Arabic analyser (67.6 vs 69.7 %). Meaning-side Arabic
search needs a multilingual embedder; that was **not** tested.

## 10. Settings that were tried and rejected

The code for every rejected option below was removed from the product; this table and the raw reports are the record.

Measured on S (730 q) against the weighted 0.3 default (85.1 %; stemming 85.6 %):

| Tried | hit@1 | Verdict |
|---|---|---|
| Stemming (English) | 85.6 % | kept (within noise, never worse; on by default with Arabic) |
| Coverage re-rank 0.4 | 85.8-85.9 % | **off by default**: +0.3 to +0.7, within noise, 5x the latency |
| Query stop words removed | 85.1 % vs 84.1 % when kept (under RRF the sign was the opposite, +1.1 for keeping them) | removed (weighted default) |
| Word-pair (bigram) terms in the keyword index | 82.9 % | harmful, off |
| Separate title vector, weight 0.3 / 0.6 / 1.0 | 84.9 / 82.3 / 80.3 % | harmful, off (title is already in the chunk prefix) |
| Identifier boost (extra votes for exact codes) | only exists for RRF / DBSF; under RRF it helps identifiers and costs natural questions (S: 74.3 % with, 82.2 % without) | not needed with weighted fusion; off |
| No context prefix on chunks (file name, title, folder, heading) | 85.5 %, never found 7 vs 1 | the prefix is kept: it is what gets the document into the top 40 |
| Longer candidate lists, other normalisations | within 0.5 | no change |

# Plumbing benchmarks (sections 11-15; the later additions are sections 16-19 at the end)

**Read the caveats first.** These runs were made before the real-document tests above. They used the offline
**hashing embedder (128-d, not semantic)** and the **RRF** fusion that was then the default, on synthetic ~2.5 KB text,
with API, workers, Postgres and Qdrant all on the same 2 cores and **Qdrant server 1.15.4** (client 1.19.1; your
compose pins server 1.19.1, so rerun there). They measure indexes, filters, SQL and the queue, **not answer quality
and not real embedding cost**. No LLM was called; stage B (the agent) was not run. Query-path latencies here are
therefore different from the 10.5 ms of section 3 (different data size per chunk, different embedder, different fusion).

## 11. Plumbing: real pipeline, 3,000 documents, 2 workers (hashing embedder)

| | |
|---|---|
| Scan 3,000 files -> 3,000 jobs | 2.6 s |
| Parse -> chunk -> embed -> index, 2 workers | **24.4 documents / s** (123 s for 3,000) |
| Indexed / failed / dead-lettered | 3000 / 0 / 0 |
| **Re-run after the last code changes** (every indexed document now also records `chunks_indexed_at`) | 3000 / 3000 indexed, **30.4 documents / s** (98.7 s), hit@1 200 / 200, 0 leaks in 200, candidate search p50 13.3 ms / p95 17.0 ms. The two runs differ by 25 % on a shared 2-CPU box; treat 24-30 documents / s as the range, and it is not a regression |
| Exact-identifier query ("what does REF-0001234 say"), right document first | **200 / 200** (hit@1), 200 / 200 in top 5 |
| Permission probe: same query asked as a user of a *different* department | **0 leaks in 200** |
| Question latency (hybrid + permission filter + Postgres re-check) | p50 16 ms, p95 22 ms |

## 12. Plumbing: query path as the library grows (hashing embedder)

Median / 95th percentile in milliseconds. The point is the **shape**: nothing here grows with the library
except the vector search itself, and that grows slowly.

| What | 10,000 docs | 50,000 docs | 100,000 docs |
|---|---|---|---|
| Hybrid chunk search, permission filter inside Qdrant | 11.8 / 15.0 | 18.4 / 26.1 | 22.8 / 32.2 |
| Candidate documents (search + Postgres permission re-check) | 17.4 / 21.6 | 24.7 / 33.4 | 28.6 / 43.8 |
| Agent: find a document by exact name | 0.7 / 1.1 | 0.9 / 1.6 | 0.6 / 0.8 |
| Agent: resolve a document (fast path) | 0.6 / 0.8 | 0.8 / 1.0 | 0.5 / 0.8 |
| Upload: pick a unique name | 1.8 / 2.4 | 2.2 / 2.6 | 1.5 / 2.0 |
| Re-check 40 candidate documents' permissions | 1.0 / 1.3 | 1.2 / 1.9 | 0.8 / 1.0 |
| UI: newest 50 documents this user may read | 1.4 / 2.1 | 2.1 / 2.7 | 1.6 / 2.2 |
| Exact-identifier hit@1 (200 queries) | 1.00 | 1.00 | 1.00 |
| 8 concurrent searchers, throughput / p95 | 91 q/s / 119 ms | 79 q/s / 139 ms | 52 q/s / 206 ms |

The concurrency row is CPU-bound on 2 shared cores (client threads, Qdrant and Postgres compete); it says
nothing about a dedicated search node.

**Queue:** claiming one job with **100,000 jobs waiting**: p50 4.0 ms, p95 8.1 ms. Enqueueing 100,000 jobs: 0.7 s.

## 13. Problems the benchmarks found (and the fixes that are in the code)

| Found | Before | After |
|---|---|---|
| Worker slept a fixed second when its slots were busy | ~1 doc/s per slot (40 docs: 40 s) | ~10 docs/s per slot (40 docs: 4 s) |
| Exact identifiers lost to rank-only fusion (dense side is noise on long chunks) | right document first in **61 %** of queries | **100 %** with the identifier boost under RRF (test with 150 decoys fails without it). The weighted default (section 2) gets 100 % with no boost |
| "Pick a unique name" used a non-indexable prefix match | 7.8 -> 33 -> **73 ms** at 10k / 50k / 100k docs | 1.5-2.2 ms, flat |
| Job claim used one OR'd query | **37 ms** at 100k queued | **4 ms** |
| First batch on an empty index: several workers created the collection at once | a job failed and had to retry | race handled; tested with 8 threads |
| A re-indexed file kept a `[xxxxxxxx]` suffix on its name | `budget [2a3f29d1].txt` | plain name restored when the old version is removed |
| Unsupported / scanned files were re-queued on **every** scan | forever | only when the file changes |

## 14. Not measured (so not claimed)

* **5 TB**, or millions of documents. The largest *retrieval* test is 28,230 documents / ~98,000 chunks; the largest
  plumbing test is 100,000 chunks; the largest real-file ingest is 1,500 files.
* **BGE-M3** or any multilingual embedder, a GPU, a larger cross-encoder than MiniLM-L-6. The reranker container and the BGE-M3
  option (`--models`) were **never started**. (The small local cross-encoder was run: section 16.)
* **Answer quality**, against earlier versions or other systems: no LLM was called. `scripts/scale_ab.py` exists for
  this and has only been run against a fake app.
* Real users' questions. The questions were written by a model; wording overlap flatters *nl* and *kw* scores.
* **No external RAG product was run** for comparison. Section 5 compares against a reconstructed Elasticsearch-style strategy.
* Elasticsearch / OpenSearch **servers** (Lucene was used), Qdrant 1.19.1, multi-node, replication, snapshots, restore.
* **Real** scanned paper (section 17 used synthetic scans), handwriting, rotated pages, drawings, spreadsheets with formulas.
* Anything that needs a **language model**: enrichment, model-written RAPTOR summaries, the model graph extractor, query rewriting (`LLM_FEATURES.md`). No model was available.
* The connectors against any **real** service (about 1,000 tests against fakes of each API; `CONNECTORS.md`).
* The chunk-editor page in a browser (type-checked and built only; the Sources page was opened in a browser).
* A real Microsoft tenant, Windows NTFS permissions, Entra groups, SharePoint site groups, a real SMB server.
* Arabic with a multilingual embedder (keyword side only), CJK (unit-tested, not measured), languages other than English / Arabic.
* The Docker images and compose stacks in a running environment (validated statically only).
* Concurrency under real load: the only multi-user number is 8 searchers on 2 shared cores (section 12).
* The abstention thresholds on your documents; the numbers in section 7 are for this corpus and this embedder.

## 15. Back-of-envelope sizing (arithmetic, **not** a test)

Formulas to plan your own measurements; replace the inputs with measured ones.

* Dense vector, 1024 dims: 4,096 B as float32 (on disk), 1,024 B as int8 (in RAM when quantised).
* HNSW links (m = 16): roughly 130 B per chunk. Sparse vector: tens to ~1 KB per chunk depending on text.
* So plan roughly **1.5-2.5 KB of RAM per chunk** with int8 vectors in RAM and float32 on disk:
  10 M chunks ~ 15-25 GB; 100 M chunks ~ 150-250 GB -> several nodes (sharding + replication), or binary
  quantisation with rescoring to cut it. Choose with a measurement, not this paragraph.
* **Ingest time = documents / (documents per second).** Two measured rates: 24-30 documents / s with a fake embedder
  (plumbing ceiling, section 11) and **1.54 documents / s with real embedding (MiniLM on 2 shared CPUs, with PDF /
  DOCX parsing, section 8)**. At 1.5 / s, 5 million files would take about 38 days on that one box. The embedding
  model sets the rate, so the plan at that size is GPU embedding and many workers; multiply by your own measured rate.
* How many chunks 5 TB is depends on what the 5 TB is: video, images, CAD and archives contribute no text.
  Count files by type first.


# Later additions (sections 16-19)

All four use the same 28,230-document collection **F**, the same 1,330 questions and the same asker as section 3 unless
stated, the same embedder, one machine with 2 CPUs. Raw reports are in `docs/scale/bench/`.

## 16. A local cross-encoder reranker

Model: `cross-encoder/ms-marco-MiniLM-L-6-v2` (ONNX, run by onnxruntime on CPU; `SCALE_RERANK_MODE=onnx`). The fused hybrid list's
top 20 to 40 chunks are re-scored and the cross-encoder score is mixed with the fused score.

| Variant (1,330 questions, 28,230 documents) | hit@1 | hit@10 | MRR |
|---|---|---|---|
| hybrid, no reranker | 82.6 % | 98.0 % | 0.888 |
| cross-encoder only (replace the order), top 20 | 79.7 % | 97.7 % | 0.863 |
| blend 0.2, all questions | 83.8 % | 98.0 % | 0.894 |
| **blend 0.5, gated (default)** | **83.6 %** | 98.0 % | **0.893** |

* A cross-encoder used **alone** makes it worse (-2.9 points). It was trained on questions and reads keyword queries and
  identifier lookups badly. Mixing its verdict with the fused score gives about **+1 point** at first rank, and the natural-language
  questions gain most (61.1 % -> 65.2 % in the gated run). Identifier questions stay at 100 %.
* The default **gate** skips the reranker for questions of fewer than 6 words and for questions that contain a code; those are
  the cases it hurt. The gate and the blend weight were chosen on these same 1,330 questions, so the +1 point is in-sample.
* On the 200 held-out questions (not used to pick the variants, but the same collection): 84.5 % -> 86.0 % first, MRR 0.906 -> 0.917
  with blend 0.5 over the top 40. At 200 questions a 1.5-point gap is a tie by the rule in section 1.
* **Cost:** about 0.4 to 1.0 s per question on two CPU cores (top 20 to 40 chunks scored). The p50 of 10-11 ms in the table's raw
  files is the cached score on a repeated run, not the cost of a first question. Plan for a GPU or fewer candidates
  (`SCALE_RERANK_TOP`).
* **Not measured:** a larger or multilingual reranker (BGE-reranker and similar), a remote reranker URL, Arabic.

Reports: `reranker_local_cross_encoder_*.json`.

## 17. docparse: OCR, layout, tables

See `DOCPARSE.md` for the full tables. In short:

* 36 synthetic pages (real text rendered to PDF, then degraded into scans at three levels), word error 5.3 % (clean), 4.7 % (scan),
  13.2 % (poor); reading order 94 %; table cells 94 % (84 % on the poor level). The detector threshold 0.3 beats the engine default
  (0.5 / 1.6 / 736 min: 7.4 / 6.2 / 12.3 %) and a 960 "max" side limit (0.5 / 1.5 / 960 max: 7.4 / 6.0 / 12.3 %) on clean and
  scan pages and is about one point worse on poor ones.
* Retrieval over OCR text (100 documents among 2,000 distractors, 300 questions): hit@1 98.3 % on the original text, 95.3 % / 94.0 %
  / 94.3 % on the clean / scan / poor OCR text; hit@10 99.7-100 %. The loss is concentrated in "find the document by its title"
  questions (95 % -> 82-86 %).
* Not measured: real scanned paper, handwriting, rotated pages, non-Latin scripts, the effect on a corpus that is mostly scans.

Reports: `docparse_*.json`.

## 18. Knowledge graph (rules extractor, no model) - REMOVED from the product

*Result: neutral at the default weight, harmful when turned up, so the feature, its tables and its script were removed. Kept here as the record.*

Entities and relations extracted by rules for all 28,230 documents (stored per document, joined to the access list at query time),
then every question asked with the graph off and on.

| Setting | hit@1 | hit@3 | hit@10 | MRR | questions better / worse |
|---|---|---|---|---|---|
| graph off | 82.6 % | 94.2 % | 98.0 % | 0.888 | |
| weight 0.1, 0 hops | 82.8 % | 94.1 % | 98.0 % | 0.888 | 6 / 7 |
| weight 0.2, 0 hops | 82.9 % | 94.0 % | 98.0 % | 0.889 | 9 / 12 |
| weight 0.4, 0 hops | 81.0 % | 93.8 % | 97.7 % | 0.878 | 14 / 48 |
| weight 0.1, 1 hop | 82.8 % | 94.2 % | 98.0 % | 0.889 | 5 / 3 |
| **weight 0.2, 1 hop (default when on)** | 82.9 % | 94.2 % | 98.0 % | 0.889 | 9 / 7 |
| weight 0.4, 1 hop | 81.4 % | 94.1 % | 98.0 % | 0.881 | 14 / 41 |

**Neutral at the default weight and harmful at 0.4.** Identifier and name questions were already 100 % and 97-98 %, and this set
has no multi-hop questions, which is what a graph is for, so this measurement can neither show nor rule out that benefit. The
multi-hop mechanism is shown only by a constructed test (`tests/scale/test_kg.py`). Not measured: the model extractor, community
reports.

Report: `knowledge_graph_28230docs_1330q.json`.

## 19. RAPTOR summaries (extractive, no model) - REMOVED from the product

*Result: no measurable effect, so the feature and its script were removed. Kept here as the record.*

Trees of extractive summaries (no model) were built for the documents with at least 10 chunks: **1,994 of 28,230 documents**, 10,357
summary points (5.2 per document, up to 3 levels). The same 1,330 questions were asked with the summaries ignored and with them searched.

| | hit@1 | hit@3 | hit@10 | hit@40 | MRR | natural-language hit@1 | keyword hit@1 |
|---|---|---|---|---|---|---|---|
| summaries ignored | 82.8 % | 94.2 % | 98.0 % | 99.5 % | 0.8885 | 61.1 % | 77.8 % |
| summaries searched | 82.6 % | 94.1 % | 98.0 % | 99.4 % | 0.8877 | 61.6 % | 76.7 % |

7 questions ranked better and 24 worse. **No measurable effect, a hair negative** (-0.15 point first-rank, inside the noise
for 1,330 questions). That is the expected result for this question set: every question was written from one passage, and RAPTOR
exists for questions about the **whole** of a long document ("what does this contract say about liability?", "what is this
report about?"), which this set does not contain. So this measurement shows that adding summaries does not damage passage-level search
(a summary is a normal point, so it can only take a rank from a chunk when it matches better), and it does **not** show the
benefit. If the client asks whole-document questions, add some to the golden set and build it again and test it (the earlier script was removed with the feature).
`SCALE_RAPTOR_IN_SEARCH=false` keeps the summaries but ignores them, as a switch to compare. Not measured: summaries written by a
language model.

Report: `raptor_28230docs_1330q.json`.


## 20. A stronger embedding model: BGE-small v1.5 against MiniLM

Same 4,059 documents, 730 questions, same chunking (160 words / 20 overlap), same hybrid search; only the embedding model differs. BGE-small-en-v1.5
(12 layers, 384 dimensions, `SCALE_ONNX_POOLING=cls`) against all-MiniLM-L6-v2 (6 layers, mean pooling). MiniLM figures are from the earlier sweep on the same set (a later re-run of the same setting gave 85.6 %, so expect about half a point of run-to-run drift). Both small English models; bigger ones (bge-m3, bge-large, e5-large)
could not be downloaded in the build sandbox and were not measured.

| search | MiniLM hit@1 / MRR | BGE-small hit@1 / MRR |
|---|---|---|
| hybrid, dense weight 0.3 (default) | 85.1 % / 0.913 | **87.3 % / 0.924** |
| hybrid, 0.5 | 84.0 % / 0.905 | 85.9 % / 0.916 |
| hybrid, 0.7 | 79.9 % / 0.876 | 81.4 % / 0.887 |
| dense only | 73.2 % / 0.824 | 77.4 % / 0.851 |

* The better model helps by about 2 points (hybrid) to 4 points (dense only), about 16 and 31 questions of 730 respectively. One run each.
* **The default weight 0.3 stays the best with the stronger model**, so that setting does not need re-tuning when the model is swapped.
* BGE's recommended query prefix ("Represent this sentence for searching relevant passages: ") did not help the hybrid search (86.9 % vs 87.3 %).
* Cost: 12 layers instead of 6: indexing the 16,691 chunks took about 19 minutes on 2 CPU cores, roughly twice MiniLM's time.
* Raw output: `docs/scale/bench/bge_small_*.json`.


## 21. 500,000 chunks: permission filters, exact search, int8, and what the repair job costs

**Method.** A Qdrant 1.15.4 server (2 CPU cores, about 8 GB RAM, one machine). 500,000 points built from the real BGE-small vectors of the
4,059-document set: each new point is a mix of two real chunks plus a little noise (so the data stays on the real embedding manifold, with no
exact copies), 5 chunks per document, 100,000 documents, permission groups with a spread of sizes (a public set, groups of 25 %, 10 %, 3 %,
1 %, and a rare group of 0.05 %). 300 real questions; for every user the true top 10 among the chunks that user may read is computed by brute
force in numpy, and recall@10 is the share of those found by the index. **The data is synthetic growth of real vectors: it shows how the
engine behaves at this size, not how well answers would be found among 500,000 real, different documents.**

**Finding 1: a permission filter that lets a medium-sized set through breaks the graph search.** recall@10 of the meaning search, float32, graph search:

| User sees | chunks visible | recall@10 | dense p50 |
|---|---|---|---|
| public + two groups (37 %) | 185,495 | 0.988 | 6.5 ms |
| public + one group (3 %) | **13,120** | **0.492** | 3.4 ms |
| three small groups | 9,855 | 1.000 | 3.9 ms |
| one group (10 %) | 50,145 | 0.990 | 4.6 ms |
| one group (25 %) | 125,635 | 0.997 | 5.3 ms |
| one small group (1 %) | 3,100 | 1.000 | 2.8 ms |
| a rare group | 240 | 1.000 | 2.2 ms |

The 0.49 case: the filter keeps a large public set plus one group, so the graph walk passes through many chunks the user may not read and
runs out of search width before it has collected ten it may. Raising the search width (`hnsw_ef` 256, 512, 1024) changed nothing (0.492 each).
Exact search over the same filtered set: recall 1.000 at 4.5 ms (p95 8.7 ms); at 50,000 visible chunks exact costs 15 ms, at 125,000 33 ms,
at 185,000 48 ms. **Change in the code:** the number of chunks a filter lets through is estimated from the payload index (cached 30 s) and
below `SCALE_EXACT_BELOW` (40,000) the meaning search is exact; above it the graph search is used, where it measured 0.99. The filter is
applied the same way in both, so this changes only speed and exactness, never what a user may read (test: `test_exact_search_never_widens_access`).
The keyword search (sparse leg) took 5-6 ms p50 at this size in every case. The 500,000-chunk collection is a probe, not a guarantee for other
distributions of permissions: **the failure depends on how the filter and the data interact**, which is why the switch is by measured size and not by a user type.

**Finding 2: int8 scalar quantization** (vectors on disk, int8 copy in RAM), same 500,000 points, the graph search:

| User sees | float32 | int8, engine defaults | int8, rescored with originals, 1.5x oversampling |
|---|---|---|---|
| 185,495 chunks | 0.988 | 0.931 | 0.979 |
| 125,635 | 0.997 | 0.942 | 0.993 |
| 50,145 | 0.990 | 0.939 | 0.987 |
| 3,100 | 1.000 | 0.939 | 1.000 |
| 240 | 1.000 | 0.941 | 1.000 |

Without rescoring int8 loses 5-6 points; with rescoring and oversampling it is within about 1 point and the latency is unchanged (p50 3-6 ms).
Server resident memory was 2.16 GB against 2.31 GB for float32: **on this machine the saving is small because everything fits in RAM**; the
reason to use it is a collection larger than RAM (50 million chunks at 384 dimensions is about 77 GB of float32 vectors against 19 GB of int8 kept in RAM, arithmetic, not measured).
The product default oversampling is 2.0 (1.5 was measured; 2.0 is more cautious and not separately measured). It is therefore **off by default** (`SCALE_QUANTIZE_INT8`, `SCALE_QUANTIZE_OVERSAMPLING`, `SCALE_ON_DISK_VECTORS`), and when on, every search rescores with the originals. It applies to collections created after it is set. Not measured: binary quantization, float16 vectors, a collection larger than RAM.

**Finding 3: the repair job's cost** (`app/scale/reconcile.py`, on the same 500,000 points / 100,000 documents): the chunk counts of 500 documents in one
facet call took 82-106 ms; the check for chunks whose dense vector is missing 3-4 ms; the list of every document id in the index (for the orphan sweep) 0.33 s.
So comparing a million documents with the index is about 2,000 calls, a few minutes. Test of the behaviour: `tests/scale/test_reconcile.py`.

**Also added (no accuracy effect, speed only):** an LRU cache of recent question embeddings (`SCALE_QUERY_CACHE_SIZE`) and of reranker scores per (question, chunk) pair (`SCALE_RERANK_CACHE_SIZE`).
The reranker's score for a pair is deterministic, so a repeated question returns the same ranking; the cached score is reused instead of running the model again (`tests/scale/test_caches.py`).


## 22. Many near-identical copies of a document

Companies keep drafts, revisions and the same file in several folders. **Method:** the 4,059-document set (section 20) plus 1,800 near-copies:
150 documents that answer questions, each copied 12 times with three words changed and a "Revision k (draft)" line on top (5,859 documents,
34,403 chunks). The same 730 questions; for a question about a copied document, the original or any copy counts as correct. Default search,
BGE-small, no reranker in this run. Synthetic copies: real file families differ in more ways than three words.

| Search over | questions | hit@1 | hit@5 | hit@10 | hit@40 |
|---|---|---|---|---|---|
| the library **without** copies | 470 about documents that have no copies | 88.1 | 98.9 | 99.6 | 99.8 |
| | 260 about documents that were later copied | 85.8 | 98.1 | 99.6 | 100.0 |
| the library **with** copies, no family step | the 470 | 87.4 | 98.1 | 98.5 | 99.6 |
| | the 260 | 84.2 | 96.5 | 96.9 | 100.0 |
| the library **with** copies, family step (default) | the 470 | 87.4 | 98.5 | **98.9** | 99.6 |
| | the 260 | 84.2 | 97.7 | **99.2** | 100.0 |

* **What happens:** a question that matches one family matches its twelve copies just as well, so they take ranks 1-13 and a different, relevant
  document falls to rank 14-19. It was still inside the 40 documents handed to the agent (hit@40 barely moves), so the answer is rarely lost, but the
  first ten were polluted, the agent reads them first, and with three such families the 40 slots would be gone. In the worst of the seven questions
  that fell out of the first ten, the right document went from rank 2 to rank 14.
* **Change in the code (`app/scale/families.py`, `SCALE_FAMILY_CAP=3`, `SCALE_FAMILY_SIMILARITY=0.8`):** documents whose best-matching passages share 80 % of their
  4-word shingles form a family; at most 3 members of a family keep their place, the others move behind the other candidates. **Nothing is dropped**
  (so two contracts that differ only in a name and an amount are still both candidates, and the one the question names ranks first anyway), and it only compares
  documents the user may already read. Cost: about 5-7 ms per question on 120 hits (p50 17.9 -> 24.5 ms in this run).
* Result: hit@10 for the copied families' questions 96.9 -> 99.2, for the others 98.5 -> 98.9, hit@1 unchanged. The library **without** copies gives identical numbers
  with the step on and off (hit@1 87.26 %, hit@10 99.59 %, MRR 0.9244 against 0.9243), so it does no harm where there is nothing to demote.
* **Not fixed:** hit@1 stays about 1 point lower than in the library without copies (87.4 vs 88.1; 84.2 vs 85.8). The cause is probably the keyword scoring: a
  term that appears in thirteen copies is less distinctive (lower IDF), so those documents' keyword matches count for less. Removing duplicates before indexing would fix that but
  would make the copies unfindable; this was not built. Cap 2 and similarity 0.7 scored slightly higher on hit@10 (99.3) but one run on one synthetic set does not justify a more aggressive setting.
