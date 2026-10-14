# What other open-source RAG systems use, and what was taken from them

This is a design review, not a benchmark. The facts about other systems come from their public documentation and repositories as read
during this work; **none of them was installed or run here**, so nothing below says one is better or worse than this system in answer
quality. Where a feature was adopted, the measurement or test that justifies it is named. Where it was not, the reason is given.

## What each one stores, and how it searches

| System | Store | Retrieval | Notes relevant here |
|---|---|---|---|
| Onyx (Danswer) | a search engine with keyword and vector fields plus Postgres (Vespa historically; Onyx's docs now describe moving the document index to OpenSearch) | hybrid, per-chunk permission fields, connector permission sync | closest in purpose: permissioned company search |
| Dify | choice of vector databases (Qdrant, Weaviate, pgvector, Milvus ...) | vector, full-text or hybrid; optional rerank; parent-child chunks; metadata filters | knowledge bases per app, not per-user permissions |
| Haystack | document-store abstraction (Elasticsearch / OpenSearch / Qdrant / pgvector ...) | pipelines; hybrid with rank fusion; rerankers | a toolkit, so the operational behaviour is whatever you wire |
| LlamaIndex | any vector store; a document store with content hashes | many retrievers; ingestion pipeline with upsert / upsert-and-delete by hash | the hash-based "has this changed" idea |
| LangChain indexing API | any vector store plus a "record manager" | cleanup modes that delete vectors of changed or removed sources | the closest thing to comparing the source of truth with the vector store |
| R2R | Postgres with pgvector and full-text | hybrid with rank fusion; optional knowledge graph | everything in one database |
| Open WebUI | Chroma by default, others optional | vector with optional BM25 + rerank | personal / team scale |
| LightRAG, Cognee, Graphiti, GraphRAG | graph plus vector stores | entity / relation extraction with a model, graph traversal | for questions that span many documents |

## What was adopted

| Idea | Seen in | Here | Evidence |
|---|---|---|---|
| Hybrid meaning + keyword search with a reranker | all of the above | Qdrant dense + sparse, weighted min-max fusion, local cross-encoder blended 0.5 | `BENCHMARKS.md` sections 2, 4, 16 |
| Permission fields on every chunk, filter inside the query | Onyx | `acl` payload, filter in the Qdrant query, re-check in Postgres | `test_acl.py`, `test_api_acl.py`, 0 leaks in `scale_eval.py` |
| Compare the source of truth with the vector store, repair the difference | record-manager / hash-based upserts | `reconcile.py`: missing, partial, extra, no-vector, wrong-build, orphan | `tests/scale/test_reconcile.py` (21 tests, deletes vectors and whole collections on purpose) |
| Idempotent writes, deterministic ids | hash-based upserts | point id = hash of (document, chunk), write first and remove leftovers after | `test_a_rebuild_never_leaves_the_document_without_chunks` |
| Exact search when the filter leaves few candidates | vector-database guidance on filtered search | exact search below 40,000 visible chunks | `BENCHMARKS.md` section 21: recall@10 0.49 -> 1.00 |
| Cache embeddings of repeated questions and reranker scores | common practice | two LRU caches | `tests/scale/test_caches.py` |
| Quantized vectors with rescoring for collections larger than RAM | vector-database guidance | optional `SCALE_QUANTIZE_INT8` with oversampling and rescoring | `BENCHMARKS.md` section 21 (accuracy measured, memory saving not demonstrable on this machine) |
| Do not let copies of one file fill the candidate list | (none of the systems reviewed does) | `families.py`: at most 3 members of a near-identical family keep their place, nothing dropped | `BENCHMARKS.md` section 22: hit@10 96.9 -> 99.2 on copied families; no change without copies |
| Validate what the embedder returned | (none of the systems reviewed does) | count, dimension, finite, non-empty | `test_embedder_output_is_checked_before_it_is_stored` |

## What was looked at and not adopted

| Idea | Why not |
|---|---|
| Elasticsearch / OpenSearch for keyword search | Measured with Lucene (the engine inside Elasticsearch; a server was not run) on the same chunks: 82.3 % first-rank against 82.7 % for this index on 1,330 questions. The exception is Arabic, where Lucene's Arabic analyser is about 2 points ahead (`BENCHMARKS.md` section 6, `ELASTICSEARCH_DECISION.md`). One more service to run for no measured gain on English. |
| Knowledge graph (entity extraction) | Tried with a rules extractor and measured: no gain on passage questions; removed (`BENCHMARKS.md` section 18). Graph methods target questions that span documents, which were not in the question set. |
| RAPTOR-style summaries | Tried and measured: no effect on passage questions; removed (section 19). |
| Everything in Postgres with pgvector (R2R) | Would remove a service, but filtered vector search at several million chunks is what Qdrant is built for; not measured against it here. |
| Parent-child chunks (Dify) | Not built and not measured. Pages are stored in Postgres and the agent reads whole pages after the candidate step, which covers the same need (small chunks to find, larger text to read). |
| Metadata filters (folder, type, date) in the question | Not built. The permission filter exists; a user-facing filter is a feature, not a retrieval fix. |
| Multi-vector / late interaction (ColBERT-style) | Not built; the vector size and index cost are several times larger. |
| Binary quantization, float16 vectors | Not measured here. |
| A different, bigger embedding model | The sandbox could not download one. The system supports BGE-M3 through `--models`; its effect is unmeasured. |

## What none of them seems to do, and this system now does

The record-manager and hash-upsert mechanisms handle a source file that **changed**. They do not look at the vector store afterwards. If the
vector volume is restored from an old backup, a collection is dropped, a point or its vector is deleted, or an embedding job dies half way,
the documents stay marked as done and the questions about them go unanswered. Here the worker compares the two stores on a schedule
and rebuilds the difference from the pages already in Postgres (`README.md`, "The index repairs itself").

## Limits of this review

Nothing here compares answer quality with any other system. The 500,000-chunk measurements use synthetic growth of real vectors. The
connectors were tested against fakes of each service. A real corpus of your own, your own questions and `scripts/scale_ab.py` are the only
honest way to compare this against another product.
