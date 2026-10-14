"""Compare retrieval settings on YOUR documents, with YOUR questions, through the scale layer's real code.

  python -m scripts.scale_realeval run --share /data/share --golden golden.json --matrix matrix.json \\
         [--docs-file paths.txt] [--cache emb.sqlite] [--out report.json] [--json-only]

share        a folder of text-like files (txt / md / html-as-text). Each file is one document with one page.
             (The full pipeline - PDF, Office, scans - is exercised by the end-to-end tests and by `scale_bench`;
             this tool exists to compare SETTINGS quickly, so it indexes text directly.)
golden.json  [{"q": "...", "expect_paths": ["rel/path/a.md", ...], "source": "any label", "style": "nl|kw"}, ...]
             expect_paths: every document that really answers the question (relative to --share).
matrix.json  {"baseline": {}, "stemming": {"stem_langs": "en"}, "dense": {"fusion_dense_weight": 0.4}, ...}
             each entry overrides SCALE_* settings (names without the prefix). Entries that change what is INDEXED
             (stem_langs, ctx_dense, ctx_sparse, chunk_words, ...) build their own
             index automatically; the rest are query-time and reuse one.
Embeddings are cached in --cache (sqlite), so a second run, or a variant that embeds the same text, costs nothing.

Reported per configuration: hit@1/3/5/10/20/40 (the right document is among the first k handed to the agent), MRR,
latency, and the same split by question style and by source. The permission filter is exercised (every document
belongs to a group; the asker holds the groups in --asker).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Iterable

KS = (1, 3, 5, 10, 20, 40)
_NS = uuid.UUID("0f0e0d0c-0b0a-4908-8706-050403020100")

# index-time settings: a configuration that changes any of these needs its own index
INDEX_KEYS = {"stem_langs", "sparse_avgdl", "ctx_dense", "ctx_sparse",
              "chunk_words", "chunk_overlap_words", "min_chunk_words", "embed_model", "quantize_int8"}


# ------------------------------------------------------------------ pure helpers (unit tested)

def rank_of(ranked_paths: list[str], expect: Iterable[str]) -> int | None:
    """1-based rank of the first acceptable path, or None."""
    want = set(expect)
    for i, p in enumerate(ranked_paths, start=1):
        if p in want:
            return i
    return None


def score_ranks(ranks: list[int | None], ks: tuple[int, ...] = KS) -> dict[str, float]:
    n = len(ranks) or 1
    out = {f"hit@{k}": round(sum(1 for r in ranks if r is not None and r <= k) / n, 4) for k in ks}
    out["mrr"] = round(sum((1.0 / r) for r in ranks if r) / n, 4)
    out["missed"] = sum(1 for r in ranks if r is None)
    return out


def split_by(rows: list[dict[str, Any]], key: str) -> dict[str, dict[str, float]]:
    groups: dict[str, list[int | None]] = {}
    for r in rows:
        groups.setdefault(str(r.get(key)), []).append(r["rank"])
    return {k: score_ranks(v) for k, v in sorted(groups.items())}


def split_settings(entry: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    index_part = {k: v for k, v in entry.items() if k in INDEX_KEYS}
    return index_part, {k: v for k, v in entry.items() if k not in INDEX_KEYS}


# ------------------------------------------------------------------ embedding cache

class CachedEmbedder:
    """Wraps any embedder with a sqlite cache keyed by (model, text). Also memoises query embeddings."""

    def __init__(self, inner, cache_path: str, model: str) -> None:
        self.inner, self.model = inner, model
        self.dim, self.name = inner.dim, inner.name
        self.db = sqlite3.connect(cache_path)
        self.db.execute("CREATE TABLE IF NOT EXISTS emb (k TEXT PRIMARY KEY, v BLOB)")
        self.hits = self.misses = 0

    def _key(self, kind: str, text: str) -> str:
        return hashlib.sha1(f"{self.model}\x00{kind}\x00{text}".encode()).hexdigest()

    def _embed(self, texts, kind, fn):
        import numpy as np

        keys = [self._key(kind, t) for t in texts]
        found: dict[str, Any] = {}
        for i in range(0, len(keys), 500):
            part = keys[i: i + 500]
            q = ",".join("?" * len(part))
            for k, v in self.db.execute(f"SELECT k, v FROM emb WHERE k IN ({q})", part):
                found[k] = np.frombuffer(v, dtype=np.float32)
        todo = [i for i, k in enumerate(keys) if k not in found]
        self.hits += len(texts) - len(todo)
        self.misses += len(todo)
        if todo:
            vecs = fn([texts[i] for i in todo])
            rows = []
            for i, v in zip(todo, vecs):
                arr = np.asarray(v, dtype=np.float32)
                found[keys[i]] = arr
                rows.append((keys[i], arr.tobytes()))
            self.db.executemany("INSERT OR REPLACE INTO emb VALUES (?, ?)", rows)
            self.db.commit()
        return [found[k].tolist() for k in keys]

    def embed(self, texts):
        return self._embed(list(texts), "d", self.inner.embed)

    def embed_queries(self, texts):
        fn = getattr(self.inner, "embed_queries", self.inner.embed)
        return self._embed(list(texts), "q", fn)


class CachedReranker:
    """Wraps the local cross-encoder with a sqlite cache keyed by (model, question, passage), so a matrix of merge
    modes / pool sizes pays for each (question, passage) pair once. Latency figures from a cached run are NOT real."""

    def __init__(self, inner, cache_path: str, model: str) -> None:
        self.inner, self.model = inner, model
        self.db = sqlite3.connect(cache_path)
        self.db.execute("CREATE TABLE IF NOT EXISTS ce (k TEXT PRIMARY KEY, v REAL)")
        self.hits = self.misses = 0

    def _key(self, query: str, text: str) -> str:
        return hashlib.sha1(f"{self.model}\x00{query}\x00{text}".encode()).hexdigest()

    def rerank(self, query, documents, top_n):
        keys = [self._key(query, d) for d in documents]
        q = ",".join("?" * len(keys))
        found = dict(self.db.execute(f"SELECT k, v FROM ce WHERE k IN ({q})", keys)) if keys else {}
        todo = [i for i, k in enumerate(keys) if k not in found]
        self.hits += len(keys) - len(todo)
        self.misses += len(todo)
        if todo:
            scores = self.inner.score(query, [documents[i] for i in todo])
            for i, v in zip(todo, scores):
                found[keys[i]] = v
            self.db.executemany("INSERT OR REPLACE INTO ce VALUES (?, ?)", [(keys[i], v) for i, v in zip(todo, scores)])
            self.db.commit()
        ranked = sorted(((i, found[k]) for i, k in enumerate(keys)), key=lambda p: p[1], reverse=True)
        return ranked[:top_n]


# ------------------------------------------------------------------ indexing and running

def doc_uuid(path: str) -> str:
    return str(uuid.uuid5(_NS, path))


def read_doc(share: Path, rel: str) -> str:
    return (share / rel).read_text("utf-8", errors="replace")


def index_corpus(cfg, share: Path, docs: list[dict[str, str]], embedder, *, batch_docs: int = 64, log=print) -> int:
    from app.scale import index as ix
    from app.scale.chunking import chunk_document

    ix.reset_client_cache()
    name = ix.collection_name(cfg)
    client = ix.get_client(cfg)
    if client.collection_exists(name):
        client.delete_collection(name)
        ix.reset_client_cache()
    total, t0 = 0, time.time()
    for start in range(0, len(docs), batch_docs):
        part = docs[start: start + batch_docs]
        per_doc = []
        texts: list[str] = []
        for d in part:
            body = read_doc(share, d["path"])
            chunks = chunk_document([body], title=Path(d["path"]).name, path=d["path"], cfg=cfg)
            per_doc.append((d, chunks))
            texts.extend((c.embed_text if cfg.ctx_dense else c.text) for c in chunks)
        vectors = embedder.embed(texts) if texts else []
        pos, indexed = 0, []
        for d, chunks in per_doc:
            did = doc_uuid(d["path"])
            for c in chunks:
                indexed.append(ix.IndexedChunk(
                    document_id=did, doc_id=did, name=Path(d["path"]).name, path=d["path"], source="eval",
                    page=c.page, chunk=c.index, start=c.start, end=c.end, acl=[d["group"]], dense=vectors[pos],
                    sparse_text=c.embed_text if cfg.ctx_sparse else c.text, text=c.text,
                ))
                pos += 1
        total += ix.upsert_chunks(indexed, cfg)
        if (start // batch_docs) % 10 == 0:
            log(f"  indexed {start + len(part)}/{len(docs)} documents, {total} chunks, {time.time() - t0:.0f}s")
    return total


def run_config(cfg, golden: list[dict[str, Any]], principals: set[str], embedder, reranker=None) -> dict[str, Any]:
    from app.scale import search

    if hasattr(search._embedder, "cache_clear"):
        search._embedder.cache_clear()
    search._embedder = lambda: embedder            # type: ignore[assignment]  # share the cached embedder
    search._reranker = lambda: (reranker if cfg.rerank_mode == "onnx" else None)   # type: ignore[assignment]
    rows, lat = [], []
    for g in golden:
        t = time.perf_counter()
        hits = search.search_chunks(g["q"], principals=principals, limit=cfg.candidate_chunks, rerank=True, cfg=cfg)
        cands = search.candidates_from_hits(hits, cfg, g["q"])[: cfg.candidate_docs]
        lat.append((time.perf_counter() - t) * 1000)
        rank = rank_of([c.path for c in cands], g["expect_paths"])
        rows.append({"id": g.get("id"), "q": g["q"], "rank": rank, "style": g.get("style"), "source": g.get("source"),
                     "top": [c.path for c in cands[:3]]})
    lat.sort()
    summary = score_ranks([r["rank"] for r in rows])
    summary["p50_ms"] = round(lat[len(lat) // 2], 1)
    summary["p95_ms"] = round(lat[min(len(lat) - 1, int(len(lat) * 0.95))], 1)
    return {"summary": summary, "by_style": split_by(rows, "style"), "by_source": split_by(rows, "source"), "rows": rows}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--share", required=True)
    r.add_argument("--golden", required=True)
    r.add_argument("--matrix", required=True)
    r.add_argument("--manifest", help="jsonl {path, group}; default: every file under --share in group 'group:eval'")
    r.add_argument("--docs-file", help="only index these relative paths (one per line)")
    r.add_argument("--asker", default="group:eval", help="comma-separated principals of the asking user")
    r.add_argument("--cache", default="scale_eval_embeddings.sqlite")
    r.add_argument("--collection", help="index name prefix; default derives from the document list, so two corpora never share (or delete) an index")
    r.add_argument("--out")
    r.add_argument("--rerank-cache", help="sqlite cache for the local cross-encoder's scores (used by configurations with rerank_mode=onnx)")
    r.add_argument("--reuse-index", action="store_true", help="do not rebuild an index that already exists")
    args = parser.parse_args(argv)

    from app.scale import index as ix
    from app.scale.models import get_embedder
    from app.scale.settings import get_scale_settings

    share = Path(args.share)
    golden = json.loads(Path(args.golden).read_text())
    matrix = json.loads(Path(args.matrix).read_text())
    base = get_scale_settings()
    if args.manifest:
        manifest = [json.loads(line) for line in Path(args.manifest).read_text().splitlines() if line.strip()]
    else:
        manifest = [{"path": str(p.relative_to(share)), "group": "group:eval"} for p in sorted(share.rglob("*")) if p.is_file()]
    if args.docs_file:
        keep = {line.strip() for line in Path(args.docs_file).read_text().splitlines() if line.strip()}
        manifest = [m for m in manifest if m["path"] in keep]
    principals = {p.strip() for p in args.asker.split(",") if p.strip()} | {"*"}
    fingerprint = hashlib.sha1("\n".join(sorted(m["path"] for m in manifest)).encode()).hexdigest()[:8]
    base = base.model_copy(update={"collection": args.collection or f"eval-{fingerprint}"})
    embedder = CachedEmbedder(get_embedder(base), args.cache, base.embed_model)
    reranker = None
    if any(e.get("rerank_mode") == "onnx" for e in matrix.values()):
        from app.scale.models import OnnxReranker

        reranker = OnnxReranker(base)
        if args.rerank_cache:
            reranker = CachedReranker(reranker, args.rerank_cache, base.rerank_onnx_dir)
    print(f"{len(manifest)} documents, {len(golden)} questions, {len(matrix)} configurations", flush=True)

    report: dict[str, Any] = {}
    built: set[str] = set()
    for label, entry in matrix.items():
        cfg = base.model_copy(update=entry)
        # chunking settings are not part of the index's own name, so give each distinct chunking its own collection here
        # (otherwise two configurations that differ only in chunk size would silently share one index)
        chunking = f"{cfg.chunk_words}-{cfg.chunk_overlap_words}-{cfg.min_chunk_words}-{int(cfg.quantize_int8)}"
        if chunking != f"{base.chunk_words}-{base.chunk_overlap_words}-{base.min_chunk_words}-{int(base.quantize_int8)}":
            cfg = cfg.model_copy(update={"collection": f"{base.collection}-c{chunking}"})
        name = ix.collection_name(cfg)
        client = ix.get_client(cfg)
        if name not in built:
            if args.reuse_index and client.collection_exists(name):
                pass
            else:
                print(f"[{label}] building index {name}", flush=True)
                chunks = index_corpus(cfg, share, manifest, embedder)
                print(f"[{label}] {chunks} chunks (embedding cache hits={embedder.hits} misses={embedder.misses})", flush=True)
            built.add(name)
        res = run_config(cfg, golden, principals, embedder, reranker)
        report[label] = res
        s = res["summary"]
        print(f"[{label:28}] " + "  ".join(f"{k}={s[k]}" for k in ("hit@1", "hit@5", "hit@10", "hit@40", "mrr", "p50_ms")), flush=True)
    if args.out:
        Path(args.out).write_text(json.dumps(report, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
