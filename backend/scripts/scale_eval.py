"""Retrieval quality + permission-safety evaluation for the scale layer. Run it on every model change.

  python -m scripts.scale_eval golden.json [--k 5,20,40] [--no-rerank] [--json out.json]

golden.json is a list of cases written WITH the client (50-200 real questions is a good start):

  [{"q": "What is the notice period in the Falcon supplier contract?",
    "as": ["group:legal"],                       # who is asking (the '*' principal is always added)
    "expect": ["Falcon Supplier Agreement"],    # substrings of the document name or path that must be found
    "forbid": ["salary"]},                      # substrings that must NEVER appear for this asker
   ...]

Reported: recall@k over candidate documents (what the agent gets to read), MRR, p50/p95 latency, and the
number of FORBIDDEN documents returned (must be 0). Uses whatever embedder / reranker the environment
configures, so run it with the production models, not the offline hashing embedder.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path


def score_case(names_and_paths: list[str], expect: list[str], forbid: list[str], ks: list[int]) -> dict:
    """names_and_paths: for each ranked candidate, 'name | path' lowercased. Pure function (unit tested)."""
    ranked = [s.lower() for s in names_and_paths]

    def first_rank(needle: str) -> int | None:
        for i, s in enumerate(ranked, start=1):
            if needle.lower() in s:
                return i
        return None

    ranks = [first_rank(e) for e in expect]
    out = {f"recall@{k}": (sum(1 for r in ranks if r is not None and r <= k) / len(ranks)) if ranks else 1.0 for k in ks}
    found = [r for r in ranks if r is not None]
    out["rr"] = (1.0 / min(found)) if found else 0.0
    out["forbidden"] = sum(1 for s in ranked for f in forbid if f.lower() in s)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("golden")
    parser.add_argument("--k", default="5,20,40")
    parser.add_argument("--no-rerank", action="store_true")
    parser.add_argument("--json")
    args = parser.parse_args()
    ks = [int(x) for x in args.k.split(",")]

    from app.scale import search
    from app.scale.settings import get_scale_settings

    cfg = get_scale_settings()
    cases = json.loads(Path(args.golden).read_text())
    rows, latencies = [], []
    for case in cases:
        principals = {"*", *case.get("as", [])}
        t = time.perf_counter()
        if args.no_rerank:
            hits = search.search_chunks(case["q"], principals=principals, limit=cfg.candidate_chunks, rerank=False)
            seen, docs = set(), []
            for h in hits:
                if h.document_id not in seen:
                    seen.add(h.document_id)
                    docs.append(f"{h.name} | {h.path}")
        else:
            docs = [f"{c.name} | {c.path}" for c in search.candidate_documents(case["q"], principals)]
        latencies.append(time.perf_counter() - t)
        scored = score_case(docs, case.get("expect", []), case.get("forbid", []), ks)
        scored["q"] = case["q"]
        rows.append(scored)

    summary = {f"recall@{k}": round(statistics.fmean(r[f"recall@{k}"] for r in rows), 4) for k in ks}
    summary["mrr"] = round(statistics.fmean(r["rr"] for r in rows), 4)
    summary["forbidden_total"] = sum(r["forbidden"] for r in rows)
    ordered = sorted(latencies)
    summary["p50_ms"] = round(ordered[len(ordered) // 2] * 1000, 1)
    summary["p95_ms"] = round(ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))] * 1000, 1)
    print(json.dumps(summary, indent=2))
    for r in sorted(rows, key=lambda r: r["rr"])[:10]:
        if r["rr"] < 1.0 or r["forbidden"]:
            print(f"  weak: rr={r['rr']:.2f} forbidden={r['forbidden']}  {r['q'][:90]}")
    if args.json:
        Path(args.json).write_text(json.dumps({"summary": summary, "cases": rows}, indent=2))
    return 1 if summary["forbidden_total"] else 0


if __name__ == "__main__":
    sys.exit(main())
