"""Benchmarks for the scale layer. Two modes, both against a real Postgres and a real Qdrant server.

  python -m scripts.scale_bench ingest --docs 2000 --workers 2
      The REAL pipeline end to end (scan -> queue -> worker -> parse -> chunk -> embed -> index) over a
      synthetic folder tree. Reports documents/second, then runs exact-id queries and a permission-leak probe.

  python -m scripts.scale_bench query --checkpoints 10000,50000,100000 --queries 200
      Loads documents straight into Postgres and Qdrant (no parsing) to reach large corpora cheaply, and
      measures the QUERY path at each size. The point is the shape of the curve: per-question cost must
      stay flat as the corpus grows. Also measures queue claim latency at depth.

Both modes use the offline hashing embedder, so they measure the plumbing (indexes, filters, queues, SQL),
not semantic quality. Semantic quality is measured by scripts/scale_eval.py with the real embedder.

Never run against a production database: it creates and deletes its own rows, identified by a bench tag.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import statistics
import sys
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from sqlalchemy import text

BENCH_TAG = "scalebench"
DEPARTMENTS = ["finance", "legal", "hr", "engineering", "sales", "ops", "projects", "compliance"]


def pct(values: list[float], p: float) -> float:
    if not values:
        return float("nan")
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round(p / 100 * (len(ordered) - 1))))]


def summarize(label: str, values_s: list[float]) -> dict:
    ms = [v * 1000 for v in values_s]
    out = {"p50_ms": round(pct(ms, 50), 2), "p95_ms": round(pct(ms, 95), 2), "p99_ms": round(pct(ms, 99), 2),
           "mean_ms": round(statistics.fmean(ms), 2) if ms else None, "n": len(ms)}
    print(f"  {label:<44} p50 {out['p50_ms']:>8.2f} ms   p95 {out['p95_ms']:>8.2f} ms   p99 {out['p99_ms']:>8.2f} ms   (n={out['n']})")
    return out


def _vocab(rng: random.Random, n: int = 3000) -> list[str]:
    syll = ["ka", "to", "ri", "mu", "sen", "dal", "vor", "pex", "lin", "qua", "zor", "bel", "nid", "fro", "gam", "hul"]
    return ["".join(rng.choice(syll) for _ in range(rng.randint(2, 4))) for _ in range(n)]


def _paragraph(rng: random.Random, vocab: list[str], words: int) -> str:
    return " ".join(rng.choice(vocab) for _ in range(words)).capitalize() + "."


def _env_setup(collection_suffix: str) -> None:
    os.environ["SCALE_EMBED_BACKEND"] = "hashing"
    os.environ.setdefault("SCALE_EMBED_DIM", "128")
    os.environ["SCALE_EMBED_MODEL"] = f"{BENCH_TAG}-{collection_suffix}"
    os.environ["SCALE_RERANK_URL"] = ""


def _cleanup(collection_name: str | None) -> None:
    from app.scale import index as ix
    from app.scale.db import engine

    with engine().begin() as conn:
        conn.execute(text("DELETE FROM scale_jobs"))
        conn.execute(text("DELETE FROM source_connections WHERE name LIKE :t"), {"t": f"{BENCH_TAG}%"})
        conn.execute(text("DELETE FROM document_trees WHERE doc_id IN (SELECT rag_doc_id FROM documents WHERE content_hash LIKE :t OR filename LIKE :t2)"),
                     {"t": f"{BENCH_TAG}%", "t2": f"{BENCH_TAG}%"})
        conn.execute(text("DELETE FROM documents WHERE content_hash LIKE :t OR filename LIKE :t2"),
                     {"t": f"{BENCH_TAG}%", "t2": f"{BENCH_TAG}%"})
    if collection_name:
        client = ix.get_client()
        if client.collection_exists(collection_name):
            client.delete_collection(collection_name)


# ───────────────────────────── mode 1: the real pipeline ─────────────────────────────


def bench_ingest(args) -> dict:
    _env_setup("ingest")
    from app.core.object_store import get_object_store  # noqa: F401  (patched below)
    from app.scale import index as ix
    from app.scale import search, sync, worker
    from app.scale.db import engine
    from app.scale.settings import get_scale_settings

    get_scale_settings.cache_clear()
    ix.reset_client_cache()

    class MemStore:  # the benchmark measures the pipeline, not MinIO
        def __init__(self):
            self.n = 0

        def put_object(self, key, data, content_type="application/octet-stream"):
            self.n += 1
            return key

        def delete_object(self, key):
            pass

        def presigned_url(self, key, expires_in=None):
            return key

    import app.core.object_store as object_store

    store = MemStore()
    object_store.get_object_store = lambda: store  # type: ignore[assignment]

    rng = random.Random(7)
    vocab = _vocab(rng)
    root = Path(tempfile.mkdtemp(prefix=f"{BENCH_TAG}-"))
    rules = [{"prefix": d, "principals": [f"group:{d}"]} for d in DEPARTMENTS]
    refs: dict[int, tuple[str, str]] = {}
    print(f"generating {args.docs} documents under {root}")
    bytes_written = 0
    for i in range(args.docs):
        dept = DEPARTMENTS[i % len(DEPARTMENTS)]
        ref = f"REF-{i:07d}"
        body = "\n\n".join([_paragraph(rng, vocab, 90), f"Reference {ref} applies to this record.", _paragraph(rng, vocab, 90),
                            _paragraph(rng, vocab, 90)])
        folder = root / dept / f"batch{i // 200:04d}"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{BENCH_TAG}-{i:07d}.txt").write_text(body)
        bytes_written += len(body)
        refs[i] = (dept, ref)

    cid = sync.create_connection("local", f"{BENCH_TAG}-ingest", {"root": str(root), "acl_rules": rules})
    result: dict = {"docs": args.docs, "bytes": bytes_written, "workers": args.workers}
    try:
        t0 = time.perf_counter()
        stats = sync.run_scan(cid)
        result["scan_seconds"] = round(time.perf_counter() - t0, 2)
        print(f"scan: {stats} in {result['scan_seconds']}s")

        t0 = time.perf_counter()
        rc = worker.main(["--once", "--no-schedule", "--concurrency", str(args.workers)])
        elapsed = time.perf_counter() - t0
        result["ingest_seconds"] = round(elapsed, 2)
        result["docs_per_second"] = round(args.docs / elapsed, 2)
        result["worker_exit"] = rc
        with engine().connect() as conn:
            result["indexed"] = conn.execute(text("SELECT count(*) FROM source_items WHERE state = 'indexed'")).scalar_one()
            result["failed"] = conn.execute(text("SELECT count(*) FROM source_items WHERE state = 'failed'")).scalar_one()
            result["dead_jobs"] = conn.execute(text("SELECT count(*) FROM scale_jobs WHERE status = 'dead'")).scalar_one()
        result["chunks"] = ix.count_chunks()
        print(f"ingest: {result['indexed']}/{args.docs} indexed, {result['failed']} failed, "
              f"{result['docs_per_second']} docs/s, {result['chunks']} chunks")

        # exact-id queries with the owner's principals; and the same queries as a user of another department
        picks = rng.sample(range(args.docs), min(args.queries, args.docs))
        lat, hit1, hit5, leaks = [], 0, 0, 0
        for i in picks:
            dept, ref = refs[i]
            mine = {"*", f"group:{dept}"}
            other = {"*", f"group:{DEPARTMENTS[(DEPARTMENTS.index(dept) + 1) % len(DEPARTMENTS)]}"}
            t = time.perf_counter()
            cands = search.candidate_documents(f"what does {ref} say", mine)
            lat.append(time.perf_counter() - t)
            names = [c.name for c in cands]
            target = f"{BENCH_TAG}-{i:07d}.txt"
            hit1 += bool(names and names[0] == target)
            hit5 += target in names[:5]
            foreign = search.candidate_documents(f"what does {ref} say", other)
            leaks += any(c.name == target for c in foreign)
        n = len(picks)
        result["exact_id_hit_at_1"] = round(hit1 / n, 4)
        result["exact_id_hit_at_5"] = round(hit5 / n, 4)
        result["permission_leaks"] = leaks
        result["candidate_latency"] = summarize("candidate_documents (hybrid + ACL + recheck)", lat)
        print(f"  exact-id hit@1 {result['exact_id_hit_at_1']}  hit@5 {result['exact_id_hit_at_5']}  leaks {leaks}/{n}")
    finally:
        _cleanup(ix.collection_name())
        import shutil

        shutil.rmtree(root, ignore_errors=True)
    return result


# ───────────────────────────── mode 2: the query path at scale ─────────────────────────────


def _bulk_load(start: int, stop: int, rng: random.Random, vocab: list[str]) -> None:
    """Insert documents [start, stop) directly: Postgres rows for documents/trees/pages/access, Qdrant points."""
    from psycopg2.extras import execute_values

    from app.scale import index as ix
    from app.scale.chunking import chunk_document
    from app.scale.models import get_embedder

    embedder = get_embedder()
    step = 2000
    for lo in range(start, stop, step):
        hi = min(stop, lo + step)
        docs, trees, pages, access, chunks = [], [], [], [], []
        for i in range(lo, hi):
            dept = DEPARTMENTS[i % len(DEPARTMENTS)]
            ref = f"REF-{i:07d}"
            document_id = str(uuid.uuid4())
            rag_id = "pi-" + uuid.uuid4().hex
            name = f"{BENCH_TAG}-{i:07d}.txt"
            body = f"{_paragraph(rng, vocab, 110)}\n\nReference {ref} applies to this record.\n\n{_paragraph(rng, vocab, 110)}"
            docs.append((document_id, name, f"{BENCH_TAG}{uuid.uuid4().hex}", f"originals/{document_id}/{name}", rag_id))
            meta = {"id": rag_id, "name": name, "description": f"Source: {dept}/{name}", "status": "completed", "pageNum": 1, "mode": "lite"}
            trees.append((rag_id, json.dumps(meta), "[]", json.dumps([{"page_number": 1, "markdown": body}])))
            pages.append((rag_id, 1, name, meta["description"], body))
            acl = [f"group:{dept}"] + (["*"] if i % 50 == 0 else [])
            for principal in acl:
                access.append((document_id, principal))
            for c in chunk_document([body], title=name, path=f"{dept}/{name}"):
                chunks.append((document_id, rag_id, name, dept, c, acl))
        from app.scale.db import engine

        conn = engine().raw_connection()
        try:
            cur = conn.cursor()
            execute_values(cur, "INSERT INTO documents (document_id, filename, doc_type, content_hash, storage_key, status, tier, rag_doc_id) VALUES %s",
                           [(d[0], d[1], "txt", d[2], d[3], "indexed", "lite", d[4]) for d in docs], page_size=1000)
            execute_values(cur, "INSERT INTO document_trees (doc_id, meta, tree, pages) VALUES %s", trees, template="(%s, %s::jsonb, %s::jsonb, %s::jsonb)", page_size=500)
            execute_values(cur, "INSERT INTO document_pages (doc_id, page_number, doc_name, doc_description, text) VALUES %s", pages, page_size=500)
            execute_values(cur, "INSERT INTO document_access (document_id, principal) VALUES %s", access, page_size=2000)
            conn.commit()
        finally:
            conn.close()
        vectors: list[list[float]] = []
        for s in range(0, len(chunks), 256):
            vectors.extend(embedder.embed([c[4].embed_text for c in chunks[s: s + 256]]))
        points = [
            ix.IndexedChunk(document_id=c[0], doc_id=c[1], name=c[2], path=f"{c[3]}/{c[2]}", source="bench", page=c[4].page,
                            chunk=c[4].index, start=c[4].start, end=c[4].end, acl=c[5], dense=v, sparse_text=c[4].embed_text, text=c[4].text)
            for c, v in zip(chunks, vectors)
        ]
        ix.upsert_chunks(points, wait=False)
        if (lo - start) % 20000 == 0:
            print(f"    loaded {hi:,} documents")


def bench_query(args) -> dict:
    _env_setup("query")
    from app.rag_core import agent_tools
    from app.rag_core.local_api import LocalAPI
    from app.rag_core.postgres_store import PostgresDocStore
    from app.scale import acl as acl_mod
    from app.scale import index as ix
    from app.scale import queue, search
    from app.scale.db import engine
    from app.scale.settings import get_scale_settings

    get_scale_settings.cache_clear()
    ix.reset_client_cache()
    ix.ensure_collection()
    rng = random.Random(11)
    vocab = _vocab(rng)
    api = object.__new__(LocalAPI)
    api._store = PostgresDocStore()
    checkpoints = [int(x) for x in args.checkpoints.split(",")]
    report: dict = {"checkpoints": []}
    loaded = 0
    try:
        for target_n in checkpoints:
            t0 = time.perf_counter()
            _bulk_load(loaded, target_n, rng, vocab)
            load_s = time.perf_counter() - t0
            loaded = target_n
            time.sleep(1.0)
            with engine().connect() as conn:
                conn.execute(text("ANALYZE documents")); conn.execute(text("ANALYZE document_access")); conn.execute(text("ANALYZE document_trees"))
                conn.commit()
            print(f"\n== {target_n:,} documents ({ix.count_chunks():,} chunks), loaded in {load_s:.0f}s ==")
            sample = rng.sample(range(target_n), min(args.queries, target_n))
            row: dict = {"documents": target_n, "chunks": ix.count_chunks()}

            def principals_for(i: int) -> set[str]:
                return {"*", f"group:{DEPARTMENTS[i % len(DEPARTMENTS)]}", f"group:{DEPARTMENTS[(i + 3) % len(DEPARTMENTS)]}"}

            lat_search, lat_cand, hits1 = [], [], 0
            for i in sample:
                q = f"what does REF-{i:07d} say"
                p = principals_for(i)
                t = time.perf_counter(); search.search_chunks(q, principals=p, limit=60, rerank=False); lat_search.append(time.perf_counter() - t)
                t = time.perf_counter(); cands = search.candidate_documents(q, p); lat_cand.append(time.perf_counter() - t)
                hits1 += bool(cands and cands[0].name == f"{BENCH_TAG}-{i:07d}.txt")
            row["search_chunks"] = summarize("hybrid chunk search, ACL filter in Qdrant", lat_search)
            row["candidate_documents"] = summarize("candidate_documents (+ Postgres re-check)", lat_cand)
            row["exact_id_hit_at_1"] = round(hits1 / len(sample), 4)
            print(f"  exact-id hit@1 over {len(sample)} queries: {row['exact_id_hit_at_1']}")

            lat_name, lat_resolve, lat_uniq, lat_vis, lat_list = [], [], [], [], []
            for i in sample[:100]:
                name = f"{BENCH_TAG}-{i:07d}.txt"
                t = time.perf_counter(); api.list_documents(name=name); lat_name.append(time.perf_counter() - t)
                doc_row = api.list_documents(name=name)["documents"][0]
                t = time.perf_counter(); agent_tools._resolve_document(api, name, None, frozenset({doc_row["id"]})); lat_resolve.append(time.perf_counter() - t)
                t = time.perf_counter(); api._unique_doc_name(name); lat_uniq.append(time.perf_counter() - t)
                ids = [str(uuid.uuid4()) for _ in range(39)]
                with engine().connect() as conn:
                    real = [r[0] for r in conn.execute(text("SELECT document_id::text FROM documents WHERE tier='lite' LIMIT 40")).all()]
                t = time.perf_counter(); acl_mod.visible_document_ids(real, principals_for(i)); lat_vis.append(time.perf_counter() - t)
                p = sorted(principals_for(i))
                t = time.perf_counter()
                with engine().connect() as conn:
                    conn.execute(text(
                        "SELECT d.document_id, d.filename FROM documents d WHERE d.document_id IN "
                        "(SELECT document_id FROM document_access WHERE principal = ANY(:p)) "
                        "ORDER BY d.created_at DESC LIMIT 50"), {"p": p}).all()
                lat_list.append(time.perf_counter() - t)
            row["list_documents_by_name"] = summarize("agent: find a document by exact name", lat_name)
            row["resolve_document"] = summarize("agent: _resolve_document (fast path)", lat_resolve)
            row["unique_doc_name"] = summarize("upload: pick a unique name", lat_uniq)
            row["visible_document_ids_40"] = summarize("ACL re-check of 40 candidate documents", lat_vis)
            row["document_list_page_50"] = summarize("UI: newest 50 documents the user may read", lat_list)

            # concurrent query throughput
            def one(i: int) -> float:
                t = time.perf_counter(); search.candidate_documents(f"what does REF-{i:07d} say", principals_for(i)); return time.perf_counter() - t

            t0 = time.perf_counter()
            with ThreadPoolExecutor(max_workers=args.threads) as pool:
                durations = list(pool.map(one, [rng.randrange(target_n) for _ in range(args.queries)]))
            wall = time.perf_counter() - t0
            row["concurrent"] = {"threads": args.threads, "qps": round(len(durations) / wall, 1),
                                 "p95_ms": round(pct([d * 1000 for d in durations], 95), 1)}
            print(f"  {args.threads} concurrent searchers: {row['concurrent']['qps']} queries/s, p95 {row['concurrent']['p95_ms']} ms")
            report["checkpoints"].append(row)

        # queue behaviour at depth
        depth = args.queue_depth
        t0 = time.perf_counter()
        with engine().begin() as conn:
            conn.execute(text(
                "INSERT INTO scale_jobs (kind, payload, dedupe_key, run_after, max_attempts) "
                "SELECT 'bench', '{}'::jsonb, 'bench:' || g, now(), 5 FROM generate_series(1, :n) g"), {"n": depth})
            conn.execute(text("ANALYZE scale_jobs"))
        print(f"\n== queue ==\n  enqueued {depth:,} jobs in {time.perf_counter() - t0:.1f}s")
        lat_claim = []
        for _ in range(200):
            t = time.perf_counter()
            jobs = queue.claim("bench-worker", n=1, kinds=["bench"])
            lat_claim.append(time.perf_counter() - t)
            for job in jobs:
                queue.complete(job.job_id, "bench-worker")
        report["queue"] = {"depth": depth, "claim": summarize(f"claim one job at depth {depth:,}", lat_claim)}
    finally:
        _cleanup(ix.collection_name())
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="mode", required=True)
    a = sub.add_parser("ingest"); a.add_argument("--docs", type=int, default=2000); a.add_argument("--workers", type=int, default=2)
    a.add_argument("--queries", type=int, default=200)
    b = sub.add_parser("query"); b.add_argument("--checkpoints", default="10000,50000,100000"); b.add_argument("--queries", type=int, default=200)
    b.add_argument("--threads", type=int, default=8); b.add_argument("--queue-depth", type=int, default=100000)
    parser.add_argument("--json", help="write the full report to this file")
    args = parser.parse_args()
    report = bench_ingest(args) if args.mode == "ingest" else bench_query(args)
    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=2))
        print(f"\nreport written to {args.json}")


if __name__ == "__main__":
    sys.exit(main())
