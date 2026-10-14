# Planning for terabytes

This file says what has to be true for a terabyte-class corpus, what is built, what was measured, and what only you can prove.
Nothing here is a claim that the system has been run at a terabyte. It has not: the largest real-document retrieval test is
28,230 documents (about 98,000 chunks) and the largest index probe is 500,000 chunks (`BENCHMARKS.md`, sections 3 and 21).

## 1. "1 TB" is not a number of chunks

Count what the files hold, not their size. Video, images, CAD and archives carry no text; a scanned PDF is mostly picture.

| 1 TB of... | extracted text | chunks (about 1.1 KB each) |
|---|---|---|
| mostly scans and drawings | 5-20 GB | 5-20 million |
| mixed office documents | 20-100 GB | 20-100 million |
| text-heavy (contracts, email, tickets, code) | 100-500 GB | 100-500 million |

Take a sample of a few thousand real files, extract them, and scale up. Then run the planner:

    python -m scripts.scale_capacity --text-gb 60 --dim 1024 --replicas 2 --docs 1000000 --docs-per-second 12

It prints chunks, Qdrant memory and disk, Postgres disk and the days a first ingest takes. It is arithmetic (formulas below), so
replace its defaults with your measured values.

**Per chunk, in the index:** the vector (`dim` x 4 bytes on disk; `dim` bytes in RAM when `SCALE_QUANTIZE_INT8=true`), about 130 bytes of graph
links, about 600 bytes of keyword vector, about 450 bytes of payload (name, path, access list). Page text lives in PostgreSQL, about three times
the extracted text once section trees and indexes are counted.

**Not keeping the originals.** By default every file from a source is also copied into MinIO / S3, so 1 TB of files costs at least 1 TB of object
storage. With `STORE_ORIGINALS=false` that copy (and the rendered preview PDF) is not written: the worker reads the file from the source, extracts
the pages, indexes them, and discards the bytes. What remains is the pages (Postgres) and the index (Qdrant). A citation then shows the stored
text of the cited page and, for SharePoint / OneDrive, a link that opens the file at its source (asked for from Microsoft at the moment of the
click). Trade-offs: the source must stay reachable for people to open the original; a file that is moved or deleted there cannot be opened;
Word / Excel files are no longer converted to a PDF preview; building a tree later, or reading a scan with the vision model, downloads the file
again for that job only. Files uploaded through the screen are always stored, because there is no other copy of them.

**Ways to cut memory** (each is a setting; each costs some speed): `SCALE_QUANTIZE_INT8=true` with `SCALE_ON_DISK_VECTORS=true`,
`SCALE_SPARSE_ON_DISK=true`, `SCALE_QDRANT_ON_DISK_PAYLOAD=true`. Not built and not measured: binary quantisation, float16 vectors.

## 2. What exists for scale

| need | what is there | status |
|---|---|---|
| many workers | jobs claimed with `FOR UPDATE SKIP LOCKED`; `--scale worker=N` | tested (queue tests, 6 competing workers) |
| UI uploads on several API replicas | per-document lease, atomic recovery, progress saved in Postgres | tested |
| model change without an outage | the old index keeps answering until every document is rebuilt (`migration.py`; CLI `migration status` / `migration retire`) | tested with two embedders |
| index repair | `reconcile.py` compares Postgres with Qdrant; the orphan sweep now walks the whole index in bounded runs with a saved cursor | tested |
| finished job rows | deleted after 7 days (dead jobs 60) | tested |
| a cluster | `docker-compose.cluster.yml`: 3 Qdrant nodes + load balancer; `SCALE_QDRANT_SHARDS`, `..._REPLICATION_FACTOR`, `..._WRITE_CONSISTENCY` for new collections | **written, not run** (no container runtime where it was built) |
| a big scope | a selection longer than one index filter is searched in parts and merged | tested |
| paging a big library | keyset cursor (`X-Next-Cursor`) and a trigram index on file names | tested |

## 3. What is NOT built (and what to do about it)

* **PostgreSQL high availability.** One node. Use a managed Postgres, or Patroni / CloudNativePG, with PgBouncer and WAL archiving (point-in-time recovery). `scripts/backup.sh` is a logical dump and is not enough at this size.
* **Kubernetes / Helm, autoscaling, separate pools for OCR, embedding and reranking.** Compose only. The roles already exist as separate processes (API, worker with `--kinds`, optional embedding and reranking servers), so a chart is packaging work, not redesign.
* **A separate lexical engine.** Keyword search is a sparse vector inside Qdrant. Terms are hashed to 32 bits, so with millions of distinct terms a few pairs share an id (about a hundred pairs among a million terms). Measured against Lucene at 98,000 chunks (`ELASTICSEARCH_DECISION.md`); not re-measured at your size. If exact-identifier recall matters more than operating one more system, add OpenSearch for that leg and re-measure.
* **Duplicate-heavy corpora.** Near-copies are demoted after retrieval (`families.py`). If hundreds of copies of a document are common, widen `SCALE_DENSE_K` / `SCALE_SPARSE_K` and measure.
* **Query routing, metadata filters, multilingual models, SSO/SCIM, online relevance feedback.** Not built.

## 4. What only you can prove, and how

Retrieval accuracy at your size depends on your documents, your questions and your embedding model. Run, on production-like hardware and a copy of the data:

1. `python -m scripts.scale_bench ingest --docs 2000 --workers N`: the real pipeline's rate on your hardware. Multiply to a first-ingest time. (Measured here: about 1.5 documents/s per 2-CPU box with real embeddings.)
2. `python -m scripts.scale_bench query --checkpoints 10000,100000,1000000`: query latency should stay flat as the corpus grows; use the access-list sizes your users really have. The 500,000-chunk probe found filtered graph search dropping to 0.49 recall for a user who sees about 13,000 chunks; the fix is exact search below `SCALE_EXACT_BELOW` visible chunks (default 40,000). What happens for users who see 40,000 to several million chunks of a much larger index was not measured: make that a benchmark dimension.
3. A golden set of real questions with known source documents (`scripts/scale_realeval.py`, `scripts/eval_retrieval.py`): hit@1, hit@10, hit@40 per question type, per language and per access-list size.
4. Kill a node while searching (cluster) and restore a backup to a clean host: time both.
5. Change the embedding model on a copy and watch `migration status` reach 100 %.

Suggested release gates: access-list leaks exactly 0; exact identifiers 100 % document recall; hit@40 >= 99 %; no per-question-type regression larger than 0.5 point; a node loss with no failed searches; a restore inside your agreed recovery time.

## 5. Changing the embedding model on a live system

1. Set the new `SCALE_EMBED_*` values (and mount the new model) on the API and every worker, then restart them. Searches keep working: the previous collection is read with the model that built it.
2. Documents re-embed as reconcile finds them (`bash scripts/cli.sh local reconcile --force` to start at once; `SCALE_RECONCILE_REQUEUE_CAP` sets how many per run). Watch `bash scripts/cli.sh local migration status` or Health.
3. When it reports 100 %: `bash scripts/cli.sh local migration retire` deletes the old collection. Before that, rolling back is putting the old settings back.

While it runs, a question searches both indexes (slower). New documents are searchable through the new index only until it has caught up with the rest. Permission changes and deletions are applied to both. The previous model must stay available until retirement; if it is gone, the old index is skipped with a warning and only rebuilt documents are found. An index built before this feature has no recorded settings, so it cannot be read after the first switch: switch models once on a copy first, or plan a rebuild window.
