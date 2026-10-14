# Do you need Elasticsearch? A measured answer

**Short answer: no, not for accuracy, at the sizes and languages tested.** The Qdrant index here (a meaning
vector plus a keyword vector with IDF weighting, fused by score) matched the engine that Elasticsearch and OpenSearch
are built on (Lucene BM25) on every English test, and was slightly ahead on the one that has typos in it.
There are real reasons to add Elasticsearch or OpenSearch later; they are listed below with what each would cost.

## What was compared (and what was not)

Same chunks (exported from its own chunker), same questions, same "right document" rule. The comparison engine
was **Lucene BM25 driven through Anserini 2.3.0** (`scripts/lucene_reference/`), with the English analyser, with the
standard analyser, with the Arabic analyser, and with fuzzy matching. Lucene is the engine inside Elasticsearch and
OpenSearch and its BM25 scoring is the same; so this is a fair stand-in for the **keyword half** of Elasticsearch.

**Not run:** an Elasticsearch or OpenSearch *server*; their vector (kNN) search; their hybrid / RRF retrievers; cluster
behaviour and operations. Those were not available here. What is claimed below is about retrieval quality of the
keyword engine, not about running Elasticsearch.

## Results (hit@1 = right document first; MRR; "never found" = not in the top 40)

| Set | This index (hybrid) | Keywords only | Lucene BM25 | Lucene + fuzzy |
|---|---|---|---|---|
| S: 4,059 docs, 730 q | **85.1 %** / 0.913 / 1 | 83.7 % / 0.900 / 1 | 83.0 % / 0.895 / 2 | |
| Identifier set: 600 q (codes, names) | 99.0 % | 98.8 % | 99.0 % (93.8 % with the standard analyser) | |
| **F: 28,230 docs, 1,330 q** | **82.7 %** / 0.888 / 7 | 82.2 % / 0.880 / 10 | 82.3 % / 0.879 / 12 | |
| F held-out: 200 q (not used for any setting) | 84.5 % / 0.906 / 0 | **87.5 %** / 0.923 / 0 | 85.0 % / 0.905 / 1 | |
| F misspelled: 919 q | **69.6 %** / 0.789 / 17 | 66.2 % / 0.762 / 22 | 66.5 % / 0.759 / 30 | 68.7 % / 0.781 / 23 |
| F, user who may read 3.6 % of the library: 690 q | 96.1 % / 0.974 / 1 | 95.9 % / 0.972 / 2 | 95.9 % / 0.971 / 2 | |
| Arabic: 460 docs, 1,387 q (English-only embedder) | 67.2 % / 0.781 / 37 | 67.6 % / 0.784 / 34 | **69.7 %** / 0.800 / 29 (Arabic analyser; 57.4 % / 123 with the standard one) | |

How to read it:

* **English, at 28,230 documents: a tie.** The 0.45-point lead at hit@1 on F is 6 questions out of 1,330 and is not
  evidence. The part that is consistent is the number of questions never found in the top 40 (7 vs 12 on F) and the
  top-10 rate (98.0 vs 96.9 %), which is what the hybrid is for. On the 200 held-out questions keywords alone are ahead
  of the hybrid; the sets are small and the honest reading is "no difference".
* **Typos:** the hybrid beats Lucene by 3 points and beats Lucene's fuzzy matching by about 1 (a tie). The meaning side
  rescues some misspelled words.
* **Selective permissions:** a user who sees 3.6 % of the library is served as well as one who sees everything,
  because the permission filter is part of the vector query. The design that does not do this, shown in
  `BENCHMARKS.md` section 5 (keyword pre-filter, dense ranks the survivors), loses recall: 27 questions never found
  against 7.
* **Arabic** is where Lucene wins: its Arabic analyser is 2 points ahead of the light Arabic stemmer. The
  this index closes most of the gap (the standard analyser is 12 points behind), but if Arabic is a main language this is
  the strongest argument for OpenSearch / Elasticsearch.
* **Everything is English-tuned and uses a small English embedder.** A multilingual embedder is the planned fix for
  the meaning side; it was not run.

## What Elasticsearch / OpenSearch would add (and this lacks)

| Capability | Today | With Elasticsearch / OpenSearch |
|---|---|---|
| Language analysers (about 30 languages, Japanese / Korean / Chinese plugins) | English, Arabic (light), CJK characters | Real analysers per language |
| Phrase, proximity, wildcard, regex queries | Not available | Available |
| Highlighted snippets, facets / aggregations (counts per author, year, type) | Not available (facets could be computed in Postgres) | Built in |
| Fuzzy matching for typos | The meaning vector copes with some | Available (+2 points on the typo set, section above) |
| One search platform for logs and documents | No | Yes, if you already run one |

## What it would cost

* A second datastore to run, size (JVM heap), secure, back up and upgrade.
* A second copy of every permission. The permission check is the security boundary of this system; today it lives in
  one place (the filter inside the Qdrant query plus the Postgres re-check). A second index means a second filter
  that must stay correct when permissions change, and a second place a deleted file could linger. Everything that makes a
  forbidden document "look exactly like a missing one" would have to be re-tested.
* Consistency between two indexes (a file indexed in one and not yet in the other).
* Licensing: Elasticsearch's own licence terms have changed over the years (Elastic License 2.0 / SSPL, AGPL added as
  an option in recent versions) and one third-party source reported that its RRF / linear-combination retrievers need a
  paid tier; **verify that against Elastic's current subscription page before relying on it** (it was not
  verifiable from here). OpenSearch is Apache 2.0 and has hybrid search with score normalisation / RRF in recent releases
  (also: verify the version you would run).

## Recommendation

1. **Start without Elasticsearch.** Measure on your own questions with `scripts/scale_realeval.py` (it also runs the
   Lucene comparison: `scripts/lucene_reference/README.md`).
2. **Add OpenSearch / Elasticsearch only if** one of these is true: your main language is one the system does not analyse
   (the Arabic result is the size of the effect to expect); users need phrase / wildcard / regex search or highlighted
   snippets; you want facets; or the client already runs and operates a cluster.
3. If you add it, keep **Qdrant for meaning + permissions** and put OpenSearch beside it as a second candidate generator,
   merging the two ranked lists before Postgres re-checks access. Inside the scale layer Qdrant is used only by `app/scale/index.py` (plus the filter builder in `app/scale/acl.py`), so a
   second index should be an addition behind the same `search()` call rather than a rewrite (not built, not tested).
4. At much larger sizes the first thing to measure is Qdrant itself (sharding, int8 / binary quantisation, on-disk
   vectors), not a second engine; nothing in these tests reached that point.
