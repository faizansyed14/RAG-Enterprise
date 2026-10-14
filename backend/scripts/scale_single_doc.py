"""Time and score the whole pipeline on ONE document: read it, chunk it, embed it, store it, ask it questions.

  python -m scripts.scale_single_doc --pdf doc.pdf --questions questions.json [--truth truth.json] [--rerank-onnx-dir DIR]
         [--ocr auto|always] [--out report.json]

questions.json   [{"q": "...", "must": ["phrase that has to be in the answering chunk", "another (all must be in ONE chunk)"], "style": "nl|kw|table"}]
truth.json       optional {"all_text": "...", "tables": [[[cell, ...], ...]], "furniture": ["running header", ...]} -> extraction metrics

Three set-ups run on the same text: the defaults, the defaults plus the local cross-encoder (needs --rerank-onnx-dir),
and larger chunks without overlap (about 380 words) with no reranker.
Everything is real: the embedding model named by SCALE_ONNX_MODEL_DIR (or your endpoint), a real Qdrant (QDRANT_URL), and the
docparse models for scans. Index timings are for one small document; on a big corpus they are dominated by the embedding model.
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
import time
import uuid
from collections import Counter
from pathlib import Path


def norm_alnum(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _grams(s: str, n: int = 4) -> set[str]:
    s = norm_alnum(s)
    return {s[i: i + n] for i in range(max(len(s) - n + 1, 1))}


def hit_strict(text: str, must: list[str]) -> bool:
    t = norm_alnum(text)
    return all(norm_alnum(m) in t for m in must)


def hit_lenient(text: str, must: list[str]) -> bool:
    """Tolerates a few OCR errors: >= 85% of the phrase's character 4-grams are in the chunk (short phrases must match exactly)."""
    tg, tn = _grams(text), norm_alnum(text)
    return all((norm_alnum(m) in tn) if len(norm_alnum(m)) < 4 else (len(_grams(m) & tg) / max(len(_grams(m)), 1) >= 0.85) for m in must)


def words(s: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", s.lower())


def extraction_metrics(pages: list[str], truth: dict, doc=None) -> dict:
    from app.scale.docparse import synth as S

    text = "\n".join(pages)
    t, g = Counter(words(truth["all_text"])), Counter(words(text))
    common = sum((t & g).values())
    out = {"word_recall": round(common / max(sum(t.values()), 1), 4), "word_precision": round(common / max(sum(g.values()), 1), 4)}
    fur = truth.get("furniture") or []
    if fur:
        out["furniture_left"] = round(sum(1 for f in fur for pg in pages if f.lower() in pg.lower()) / max(len(fur) * len(pages), 1), 3)
    cells = [c for tb in truth.get("tables", []) for r in tb for c in r if c.strip()]
    if cells:
        nt = " ".join(words(text))
        out["table_cells_present_anywhere"] = round(sum(1 for c in cells if " ".join(words(c)) in nt) / len(cells), 4)
    if doc is not None and truth.get("tables"):
        found = [b.rows for p in doc.pages for b in p.blocks if b.type == "table"]
        out["tables_found"] = len(found)
        out["table_cells_at_right_row_col"] = round(statistics.mean(
            [max([S.table_cell_accuracy(tb, f) for f in found] or [0.0]) for tb in truth["tables"]]), 4)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pdf", required=True)
    ap.add_argument("--questions", required=True)
    ap.add_argument("--truth")
    ap.add_argument("--rerank-onnx-dir", default="")
    ap.add_argument("--ocr", default="auto", choices=["auto", "always", "never"])
    ap.add_argument("--plain", action="store_true", help="also run the plain text-layer extraction (what the pipeline used before docparse)")
    ap.add_argument("--out")
    args = ap.parse_args(argv)

    from app.scale import index as ix
    from app.scale import search
    from app.scale.chunking import chunk_document
    from app.scale.docparse import ParseOptions, parse_pdf
    from app.scale.models import OnnxReranker, get_embedder
    from app.scale.settings import get_scale_settings

    questions = json.loads(Path(args.questions).read_text())
    truth = json.loads(Path(args.truth).read_text()) if args.truth else None
    base = get_scale_settings().model_copy(update={"rerank_onnx_dir": args.rerank_onnx_dir})
    load: dict = {}
    t = time.perf_counter()
    embedder = get_embedder(base)
    load["embedder_load_s"] = round(time.perf_counter() - t, 2)
    reranker = None
    if args.rerank_onnx_dir:
        t = time.perf_counter()
        reranker = OnnxReranker(base)
        load["reranker_load_s"] = round(time.perf_counter() - t, 2)
    embedder.embed(["warm up"])
    embedder.embed_queries(["warm up"])
    print("model load:", load, flush=True)

    inputs: dict[str, dict] = {}
    if args.plain:
        import PyPDF2

        t = time.perf_counter()
        pages = [p.extract_text() or "" for p in PyPDF2.PdfReader(args.pdf).pages]
        inputs["plain text extraction"] = {"pages": pages, "parse_s": round(time.perf_counter() - t, 2),
                                           "metrics": extraction_metrics(pages, truth) if truth else {}}
    t = time.perf_counter()
    doc = parse_pdf(args.pdf, ParseOptions(ocr=args.ocr))
    cold = round(time.perf_counter() - t, 2)
    t = time.perf_counter()
    doc = parse_pdf(args.pdf, ParseOptions(ocr=args.ocr))
    inputs["docparse"] = {"pages": doc.texts(), "parse_s": round(time.perf_counter() - t, 2), "parse_first_call_s": cold,
                          "metrics": extraction_metrics(doc.texts(), truth, doc) if truth else {}}
    for k, v in inputs.items():
        print(f"[read] {k}: {v['parse_s']}s  {v['metrics']}", flush=True)

    setups = {
        "defaults (320 words / 40 overlap, hybrid)": {"chunk_words": 320, "chunk_overlap_words": 40, "rerank_mode": "none"},
        "larger chunks (380 words / 0 overlap, no rerank)": {"chunk_words": 380, "chunk_overlap_words": 0, "rerank_mode": "none"},
    }
    if reranker is not None:
        setups["defaults + cross-encoder"] = {"chunk_words": 320, "chunk_overlap_words": 40, "rerank_mode": "onnx"}
    docid = str(uuid.uuid5(uuid.NAMESPACE_URL, "single-doc:" + args.pdf))
    results = []
    for iname, inp in inputs.items():
        for sname, upd in setups.items():
            cfg = base.model_copy(update={**upd, "collection": "singledoc_" + re.sub(r"\W+", "", sname)[:12].lower()})
            ix.reset_client_cache()
            client = ix.get_client(cfg)
            name = ix.collection_name(cfg)
            if client.collection_exists(name):
                client.delete_collection(name)
                ix.reset_client_cache()
            t = time.perf_counter()
            chunks = chunk_document(inp["pages"], title=Path(args.pdf).name, path=f"docs/{Path(args.pdf).name}", cfg=cfg)
            t_chunk = time.perf_counter() - t
            t = time.perf_counter()
            vecs = embedder.embed([(c.embed_text if cfg.ctx_dense else c.text) for c in chunks])
            t_embed = time.perf_counter() - t
            items = [ix.IndexedChunk(document_id=docid, doc_id=docid, name=Path(args.pdf).name, path=f"docs/{Path(args.pdf).name}",
                                     source="eval", page=c.page, chunk=c.index, start=c.start, end=c.end, acl=["group:eval"], dense=vecs[i],
                                     sparse_text=c.embed_text if cfg.ctx_sparse else c.text, text=c.text)
                     for i, c in enumerate(chunks)]
            t = time.perf_counter()
            ix.upsert_chunks(items, cfg)
            t_store = time.perf_counter() - t
            search._embedder = lambda: embedder                                               # type: ignore[assignment]
            search._reranker = (lambda: reranker) if cfg.rerank_mode == "onnx" else (lambda: None)   # type: ignore[assignment]
            rows, lat = [], []
            for q in questions:
                t = time.perf_counter()
                hits = search.search_chunks(q["q"], principals={"group:eval", "*"}, limit=10, rerank=True, cfg=cfg)
                lat.append((time.perf_counter() - t) * 1000)
                rows.append({"id": q.get("id"), "style": q.get("style"),
                             "rank_strict": next((i + 1 for i, h in enumerate(hits) if hit_strict(h.text, q["must"])), None),
                             "rank_lenient": next((i + 1 for i, h in enumerate(hits) if hit_lenient(h.text, q["must"])), None)})
            lat.sort()

            def hk(key: str, k: int) -> float:
                return round(sum(1 for r in rows if r[key] and r[key] <= k) / len(rows), 4)

            res = {"input": iname, "setup": sname, "chunks": len(chunks), "avg_chunk_words": round(statistics.mean(len(c.text.split()) for c in chunks), 1),
                   "chunk_s": round(t_chunk, 3), "embed_s": round(t_embed, 3), "embed_ms_per_chunk": round(1000 * t_embed / max(len(chunks), 1), 1),
                   "store_s": round(t_store, 3), "query_p50_ms": round(lat[len(lat) // 2], 1), "query_p95_ms": round(lat[int(len(lat) * 0.95)], 1),
                   "strict": {f"hit@{k}": hk("rank_strict", k) for k in (1, 3, 5)}, "lenient": {f"hit@{k}": hk("rank_lenient", k) for k in (1, 3, 5)}, "rows": rows}
            results.append(res)
            print(f"[{iname[:26]:26}] [{sname[:30]:30}] chunks={res['chunks']:3} embed={res['embed_s']}s store={res['store_s']}s "
                  f"query p50={res['query_p50_ms']}ms strict={res['strict']}", flush=True)
    report = {"load": load, "reading": {k: {kk: vv for kk, vv in v.items() if kk != "pages"} for k, v in inputs.items()},
              "retrieval": results, "n_questions": len(questions)}
    if args.out:
        Path(args.out).write_text(json.dumps(report, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
