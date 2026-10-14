"""A reference retrieval strategy to compare the scale layer against: "lexical pre-filter + dense ranking +
token-coverage re-rank", the approach used by Elasticsearch-based RAG stacks.

This is NOT any product's code. It is a small re-implementation, in numpy, of that documented strategy so the
two approaches can be measured on the same documents, the same questions and the same embeddings:

  1. lexical pre-filter   a chunk is a candidate only if it shares at least `min_match` of the question's terms
                          (the lexical score itself does not rank - it only filters, as an Elasticsearch
                          bool/query_string filter with a zero boost does)
  2. dense ranking        candidates ranked by cosine similarity, the best `window` (64) are kept
  3. coverage re-rank     sim = (1 - vw) * term_similarity + vw * cosine   (vw = 0.3)
                          term_similarity = share of the question's term weight (unigrams 0.4, adjacent pairs 0.6)
                          that appears in the chunk (title counted twice); weights are normalised IDFs
  4. threshold            keep sim >= threshold (0.2)
  5. documents            ranked by their best chunk

  python -m scripts.scale_ref_strategy --share DIR --manifest m.jsonl --golden golden.json --docs-file docs.txt \\
         --cache emb.sqlite [--embed raw|context] [--out result.json]

--embed context  embeds the same text the scale layer embeds (title/path/heading prefix + text): isolates the
                 retrieval logic. --embed raw embeds the bare chunk text, with the file name blended in at
                 weight 0.1 (the usual "no context prefix" design).
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter
from pathlib import Path

import numpy as np

from app.scale.text import STOPWORDS, Analyzer, terms

AN = Analyzer(stem=("en",))


def tokens(text: str) -> list[str]:
    return terms(text, AN)


def query_tokens(text: str) -> list[str]:
    out = [t for t in terms(text, AN) if t not in STOPWORDS and len(t) > 1]
    return out or terms(text, AN)


def idf_table(docs: list[list[str]]) -> dict[str, float]:
    df: Counter[str] = Counter()
    for toks in docs:
        df.update(set(toks))
    n = len(docs)
    return {t: math.log10(10 + (n - c + 0.5) / (c + 0.5)) for t, c in df.items()}


def query_dict(q: list[str], idf: dict[str, float], default: float) -> dict[str, float]:
    w = [idf.get(t, default) for t in q]
    total = sum(w) or 1.0
    w = [x / total for x in w]
    d: dict[str, float] = {}
    for t, x in zip(q, w):
        d[t] = d.get(t, 0.0) + x * 0.4
    for i in range(len(q) - 1):
        k = q[i] + q[i + 1]
        d[k] = d.get(k, 0.0) + max(w[i], w[i + 1]) * 0.6
    return d


def term_similarity(qd: dict[str, float], chunk_toks: list[str]) -> float:
    have = set(chunk_toks) | {a + b for a, b in zip(chunk_toks, chunk_toks[1:])}
    num = sum(v for k, v in qd.items() if k in have)
    return (1e-9 + num) / (1e-9 + sum(qd.values()))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--share", required=True)
    p.add_argument("--manifest", required=True)
    p.add_argument("--golden", required=True)
    p.add_argument("--docs-file", required=True)
    p.add_argument("--cache", required=True)
    p.add_argument("--embed", choices=["context", "raw"], default="context")
    p.add_argument("--title", choices=["stem", "context"], default="stem",
                   help="stem: the file name without extension (strict; 'index.md' tells nothing). "
                        "context: the scale layer's own title/path/heading context (generous)")
    p.add_argument("--window", type=int, default=64)
    p.add_argument("--vector-weight", type=float, default=0.3)
    p.add_argument("--min-match", type=float, default=0.3, help="share of the question's terms a chunk must contain")
    p.add_argument("--out")
    a = p.parse_args(argv)

    from app.scale.chunking import chunk_document
    from app.scale.models import get_embedder
    from app.scale.settings import get_scale_settings
    from scripts.scale_realeval import CachedEmbedder, rank_of, score_ranks, split_by

    cfg = get_scale_settings()
    share = Path(a.share)
    keep = [x.strip() for x in Path(a.docs_file).read_text().splitlines() if x.strip()]
    groups = {}
    for line in Path(a.manifest).read_text().splitlines():
        if line.strip():
            m = json.loads(line)
            groups[m["path"]] = m["group"]
    golden = json.loads(Path(a.golden).read_text())

    paths, texts, titles, embed_texts = [], [], [], []
    for rel in keep:
        body = (share / rel).read_text("utf-8", errors="replace")
        for c in chunk_document([body], title=Path(rel).name, path=rel, cfg=cfg):
            paths.append(rel)
            texts.append(c.text)
            titles.append(Path(rel).stem.replace("_", " ").replace("-", " ") if a.title == "stem" else c.context)
            embed_texts.append(c.embed_text if a.embed == "context" else c.text)
    print(f"{len(keep)} documents, {len(paths)} chunks, {len(golden)} questions, embeddings={a.embed}", flush=True)

    emb = CachedEmbedder(get_embedder(cfg), a.cache, cfg.embed_model)
    vecs = np.asarray(emb.embed(embed_texts), dtype=np.float32)
    if a.embed == "raw":                                    # file name blended in at weight 0.1
        uniq = sorted(set(titles))
        tv = dict(zip(uniq, np.asarray(emb.embed(uniq), dtype=np.float32)))
        vecs = 0.1 * np.stack([tv[t] for t in titles]) + 0.9 * vecs
    vecs /= np.linalg.norm(vecs, axis=1, keepdims=True) + 1e-9
    qv = np.asarray(emb.embed_queries([g["q"] for g in golden]), dtype=np.float32)
    qv /= np.linalg.norm(qv, axis=1, keepdims=True) + 1e-9

    chunk_toks = [tokens(t) for t in texts]
    title_toks = [tokens(t) for t in titles]
    idf = idf_table([ct + tt for ct, tt in zip(chunk_toks, title_toks)])
    default_idf = math.log10(10 + len(chunk_toks))
    vocab_sets = [set(ct) | set(tt) for ct, tt in zip(chunk_toks, title_toks)]
    inverted: dict[str, list[int]] = {}
    for i, s in enumerate(vocab_sets):
        for t in s:
            inverted.setdefault(t, []).append(i)

    thresholds = (0.0, 0.1, 0.2)
    results = {f"threshold={t}": [] for t in thresholds}
    for gi, g in enumerate(golden):
        q = query_tokens(g["q"])
        counts: Counter[int] = Counter()
        for t in set(q):
            counts.update(inverted.get(t, ()))
        need = max(1, math.floor(a.min_match * len(set(q))))
        cand = np.fromiter((i for i, c in counts.items() if c >= need), dtype=np.int64)
        if cand.size == 0:
            for t in thresholds:
                results[f"threshold={t}"].append({"id": g["id"], "rank": None, "style": g["style"], "source": g["source"]})
            continue
        cos = vecs[cand] @ qv[gi]
        order = np.argsort(-cos)[: a.window]
        qd = query_dict(q, idf, default_idf)
        scored = []
        for j in order:
            i = int(cand[j])
            tsim = term_similarity(qd, chunk_toks[i] + title_toks[i] * 2)
            scored.append((i, (1 - a.vector_weight) * tsim + a.vector_weight * float(cos[j])))
        scored.sort(key=lambda x: -x[1])
        for t in thresholds:
            docs: list[str] = []
            for i, s in scored:
                if s >= t and paths[i] not in docs:
                    docs.append(paths[i])
            results[f"threshold={t}"].append({"id": g["id"], "rank": rank_of(docs, g["expect_paths"]), "style": g["style"], "source": g["source"]})

    report = {}
    for label, rows in results.items():
        s = score_ranks([r["rank"] for r in rows])
        report[label] = {"summary": s, "by_style": split_by(rows, "style"), "by_source": split_by(rows, "source")}
        print(f"[{a.embed}/{a.title}:{label:15}] " + "  ".join(f"{k}={s[k]}" for k in ("hit@1", "hit@3", "hit@5", "hit@10", "mrr", "missed")), flush=True)
    if a.out:
        Path(a.out).write_text(json.dumps(report, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
