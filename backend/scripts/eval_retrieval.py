"""Retrieval recall check -- run it so a loss of accuracy as the library grows is *visible*.

For every question in retrieval_golden.json it asks the two searches the agent has (the document
search behind browse_documents(query) and the page search behind search_content) and reports how
often the page that actually holds the fact is in the top results. A question's "right page" is any
page (outside excluded documents) whose text contains every string in its `must_contain`. Questions
with no such page in this library are listed and left out of the numbers, so the same golden file
works on any library.

Retrieval-only: SQL against Postgres, no LLM calls, no embeddings -- free, run it as often as you
like. Exit status is 1 when identifier recall@5 falls below --min-identifier-recall, so it can gate
a deploy.

    docker compose --env-file .env.dev -f docker-compose.dev.yml exec backend python -m scripts.eval_retrieval
    ... python -m scripts.eval_retrieval --include-prefix scale-     # also search the fake scale-* documents

Scope is every indexed document in the `documents` table (what "All documents" sends the agent),
minus any document whose name matches --exclude-name (default: the planted-problems "Answer key",
which names every fact and would trivially satisfy every question).
"""

import argparse
import json
import re
import statistics
import sys
import time
from pathlib import Path

from sqlalchemy import text

from app.rag_core.postgres_store import PostgresDocStore, _engine

GOLDEN = Path(__file__).with_name("retrieval_golden.json")


def _load_scope(include_prefix: str | None, exclude_name: re.Pattern) -> tuple[list[str], dict[str, str]]:
    with _engine().connect() as conn:
        rows = conn.execute(text(
            "SELECT t.doc_id, t.meta->>'name' FROM document_trees t WHERE t.doc_id IN "
            "(SELECT rag_doc_id FROM documents WHERE status = 'indexed' AND rag_doc_id IS NOT NULL)"
        )).all()
        if include_prefix:
            rows += conn.execute(
                text("SELECT doc_id, meta->>'name' FROM document_trees WHERE doc_id LIKE :p"),
                {"p": include_prefix.replace("%", "") + "%"},
            ).all()
    names = {doc_id: name or "" for doc_id, name in rows}
    return [d for d, n in names.items() if not exclude_name.search(n)], names


def _health(scope: list[str]) -> list[str]:
    """Documents whose pages are not all in the page-text index (a failed index refresh, or a document
    saved before the migration): those pages are invisible to search_content."""
    with _engine().connect() as conn:
        rows = conn.execute(text(
            "SELECT t.doc_id, jsonb_array_length(t.pages) AS have, "
            "(SELECT count(*) FROM document_pages p WHERE p.doc_id = t.doc_id) AS indexed "
            "FROM document_trees t WHERE t.doc_id = ANY(:ids)"), {"ids": scope}).all()
    return [f"{d}: {i}/{h} pages indexed" for d, h, i in rows if i != h]


def _relevant_pages(scope: list[str], tokens: list[str]) -> set[tuple[str, int]]:
    needles = [t.lower() for t in tokens]
    with _engine().connect() as conn:
        rows = conn.execute(text("SELECT doc_id, page_number, lower(text) FROM document_pages WHERE doc_id = ANY(:ids)"),
                            {"ids": scope}).all()
    return {(d, p) for d, p, body in rows if all(n in body for n in needles)}


def _rank_of(hits: list, wanted: set) -> int | None:
    return next((i + 1 for i, h in enumerate(hits) if h in wanted), None)


def _pct(values: list[bool]) -> str:
    return f"{100 * sum(values) / len(values):4.0f}%" if values else "  n/a"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--golden", type=Path, default=GOLDEN)
    parser.add_argument("--include-prefix", help="also search documents whose id starts with this (e.g. scale-)")
    parser.add_argument("--exclude-name", default=r"answer key", help="regex of document names to leave out")
    parser.add_argument("--min-identifier-recall", type=float, default=0.9,
                        help="exit 1 if identifier recall@5 (page search) is below this")
    args = parser.parse_args()

    scope, names = _load_scope(args.include_prefix, re.compile(args.exclude_name, re.IGNORECASE))
    if not scope:
        print("No indexed documents in scope.")
        return 1
    print(f"Scope: {len(scope)} documents" + (f" (incl. '{args.include_prefix}*')" if args.include_prefix else ""))
    unindexed = _health(scope)
    print("Index health: " + ("OK, every page of every document is in the page-text index" if not unindexed
                              else f"{len(unindexed)} document(s) NOT fully indexed -> " + "; ".join(unindexed[:5])))

    store = PostgresDocStore()
    results: dict[str, dict[str, list]] = {k: {"doc@1": [], "doc@5": [], "doc@10": [], "page@1": [], "page@3": [],
                                               "page@5": [], "page@10": [], "mrr": []} for k in ("identifier", "project")}
    skipped, latencies = [], []
    for item in json.loads(args.golden.read_text(encoding="utf-8")):
        wanted = _relevant_pages(scope, item["must_contain"])
        if not wanted:
            skipped.append(item["question"])
            continue
        wanted_docs = {d for d, _ in wanted}
        bucket = results[item["kind"]]

        metas, _ = store.search_metas(item["question"], 10, 0, doc_ids=scope)
        doc_rank = _rank_of([m["id"] for m in metas], wanted_docs)
        for k in (1, 5, 10):
            bucket[f"doc@{k}"].append(bool(doc_rank and doc_rank <= k))

        started = time.perf_counter()
        hits, _mode = store.search_pages(item["question"], limit=10, doc_ids=scope)
        latencies.append(time.perf_counter() - started)
        page_rank = _rank_of([(h["doc_id"], h["page"]) for h in hits], wanted)
        for k in (1, 3, 5, 10):
            bucket[f"page@{k}"].append(bool(page_rank and page_rank <= k))
        bucket["mrr"].append(1 / page_rank if page_rank else 0.0)

    print("\nHow often the right page/document is in the top results (higher is better)")
    print(f"{'':10s} {'n':>3s} | document search: @1    @5   @10 | page search:   @1    @3    @5   @10   MRR")
    for kind, b in results.items():
        n = len(b["doc@1"])
        if not n:
            continue
        print(f"{kind:10s} {n:3d} |                {_pct(b['doc@1'])} {_pct(b['doc@5'])} {_pct(b['doc@10'])} |"
              f"              {_pct(b['page@1'])} {_pct(b['page@3'])} {_pct(b['page@5'])} {_pct(b['page@10'])}  "
              f"{statistics.mean(b['mrr']):.2f}")
    if latencies:
        latencies.sort()
        p95 = latencies[min(len(latencies) - 1, int(0.95 * len(latencies)))]
        print(f"\nPage-search latency over {len(latencies)} queries: p50 {1000 * statistics.median(latencies):.0f} ms, "
              f"p95 {1000 * p95:.0f} ms (first query includes connection warm-up)")
    if skipped:
        print(f"\n{len(skipped)} question(s) have no matching page in this library and are excluded:")
        for q in skipped:
            print(f"  - {q}")

    ident = results["identifier"]["page@5"]
    if ident and sum(ident) / len(ident) < args.min_identifier_recall:
        print(f"\nFAIL: identifier recall@5 {100 * sum(ident) / len(ident):.0f}% is below "
              f"{100 * args.min_identifier_recall:.0f}%")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
