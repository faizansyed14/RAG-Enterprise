"""Choose the "nothing relevant" thresholds (SCALE_MIN_CONFIDENCE, SCALE_MIN_LEXICAL, SCALE_MIN_COVERAGE) from YOUR questions.

  python -m scripts.scale_calibrate run --answerable golden.json --unanswerable none.json [--keep 0.97] [--asker group:a,group:b]

answerable    [{"q": "..."}]  questions your corpus can answer (the golden set works as is)
unanswerable  [{"q": "..."}]  questions whose topic is NOT in the corpus - write 30-100 that a user might plausibly ask
              (HR, finance, a product you do not sell...), AND 20-40 that sound like your documents but ask about things
              that do not exist (an invented product, clause, person). The second kind is the hard one: say how many of
              each you used when you quote a number.

The rule being calibrated (needs SCALE_FUSION=weighted, which keeps the raw scores): the search returns no documents when
the best chunk is weak in ALL of: meaning (top cosine < min_confidence), keywords (top BM25 < min_lexical) and coverage
(share of the question's wording the chunk contains < min_coverage). An identifier lookup has a low cosine and a high
BM25; a paraphrase has the opposite; only a question the corpus knows nothing about is low on all three. The thresholds
depend on the embedding model and the corpus statistics, so they must be measured here, not copied from another deployment.

Output: for each target share of answerable questions you accept losing (1%, 3%, 5%), the thresholds and the share of
unanswerable questions that would be correctly turned away. The tool reports the numbers on the same questions it chose
the thresholds on (in-sample), and a second figure from a random 50/50 split repeated 10 times (choose on one half, count
on the other), which is the number to quote. Re-check after a month of real traffic.
"""

from __future__ import annotations

import argparse
import itertools
import json
import random
import sys
from pathlib import Path

import numpy as np

SIGNALS = ("min_confidence", "min_lexical", "min_coverage")        # cosine, BM25, coverage


def pick_thresholds(answerable: list[tuple[float, ...]], unanswerable: list[tuple[float, ...]], keep: float,
                    steps: int = 16) -> dict:
    """answerable / unanswerable: [(top cosine, top BM25, coverage)] per question (any number of signals >= 1).
    Finds the thresholds (one per signal) that keep at least `keep` of the answerable questions - a question is kept when
    ANY signal reaches its threshold - and turn away as many unanswerable ones as possible."""
    if not answerable or not unanswerable:
        raise ValueError("need both answerable and unanswerable questions")
    a, u = np.asarray(answerable, dtype=float), np.asarray(unanswerable, dtype=float)
    n = a.shape[1]
    grids = []
    for j in range(n):
        values = np.unique(np.concatenate([a[:, j], u[:, j]]))
        values = values[np.isfinite(values)]              # a question whose code was found carries infinite signals: always kept
        grids.append(values if len(values) <= steps else values[np.linspace(0, len(values) - 1, steps).astype(int)])
    best = (-1.0, 0.0, tuple(float(g[0]) for g in grids))       # (turned away, kept, thresholds); lowest thresholds turn nothing away
    for th in itertools.product(*grids):
        t = np.asarray(th)
        kept = float(np.mean(np.any(a >= t, axis=1)))
        if kept < keep:
            continue
        away = float(np.mean(~np.any(u >= t, axis=1)))
        if away > best[0] or (away == best[0] and kept > best[1]):
            best = (away, kept, th)
    out = {name: round(float(best[2][j]), 4) for j, name in enumerate(SIGNALS[:n])}
    out.update({"kept_answerable": round(best[1], 4) if best[0] >= 0 else 1.0, "turned_away": round(max(best[0], 0.0), 4)})
    return out


def out_of_sample(answerable: list[tuple[float, ...]], unanswerable: list[tuple[float, ...]], keep: float,
                  repeats: int = 10, seed: int = 5) -> dict:
    """Choose the thresholds on a random half, count on the other half; averaged over `repeats` splits."""
    rng = random.Random(seed)
    kept, away = [], []
    for _ in range(repeats):
        ia, iu = list(range(len(answerable))), list(range(len(unanswerable)))
        rng.shuffle(ia)
        rng.shuffle(iu)
        ha, hu = len(ia) // 2, len(iu) // 2
        if ha == 0 or hu == 0:
            break
        t = pick_thresholds([answerable[i] for i in ia[:ha]], [unanswerable[i] for i in iu[:hu]], keep)
        th = np.asarray([t[name] for name in SIGNALS[: len(answerable[0])]])
        te_a = np.asarray([answerable[i] for i in ia[ha:]])
        te_u = np.asarray([unanswerable[i] for i in iu[hu:]])
        kept.append(float(np.mean(np.any(te_a >= th, axis=1))))
        away.append(float(np.mean(~np.any(te_u >= th, axis=1))))
    return {"kept_answerable": round(float(np.mean(kept)), 4), "turned_away": round(float(np.mean(away)), 4), "splits": len(kept)}


def auc(pos: list[float], neg: list[float]) -> float:
    """Chance that a random answerable question scores higher than a random unanswerable one."""
    wins = sum((p > n) + 0.5 * (p == n) for p in pos for n in neg)
    return wins / (len(pos) * len(neg))


def top_scores(question: str, principals: set[str] | None) -> tuple[float, float, float]:
    """(top cosine, top BM25, coverage of the best chunk) - computed by the same code the search uses."""
    from app.scale import search
    from app.scale.settings import get_scale_settings

    cfg = get_scale_settings()
    hits = search.search_chunks(question, principals=principals, limit=cfg.candidate_chunks, rerank=False, allow_unfiltered=principals is None, cfg=cfg)
    if not hits:
        return 0.0, 0.0, 0.0
    cos, lex, cov = search.relevance_signals(question, hits, cfg)
    return float(cos or 0.0), float(lex), float(cov)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--answerable", required=True)
    r.add_argument("--unanswerable", required=True)
    r.add_argument("--asker", default="", help="comma-separated principals of the asking user; empty = no permission filter (admin view)")
    r.add_argument("--keep", type=float, nargs="*", default=[0.99, 0.97, 0.95])
    args = ap.parse_args(argv)

    principals = {p.strip() for p in args.asker.split(",") if p.strip()} | {"*"} if args.asker else None
    ans = [top_scores(q["q"], principals) for q in json.loads(Path(args.answerable).read_text())]
    una = [top_scores(q["q"], principals) for q in json.loads(Path(args.unanswerable).read_text())]
    print(f"{len(ans)} answerable, {len(una)} unanswerable questions")
    print("separation (1.0 = perfect, 0.5 = none): " + ", ".join(
        f"{name} {auc([a[j] for a in ans], [u[j] for u in una]):.3f}" for j, name in enumerate(("cosine", "BM25", "coverage"))))
    for keep in args.keep:
        t = pick_thresholds(ans, una, keep)
        o = out_of_sample(ans, una, keep)
        print(f"keep >= {keep:.0%} of answerable: SCALE_MIN_CONFIDENCE={t['min_confidence']} SCALE_MIN_LEXICAL={t['min_lexical']} "
              f"SCALE_MIN_COVERAGE={t['min_coverage']}")
        print(f"      in-sample: keeps {t['kept_answerable']:.1%}, turns away {t['turned_away']:.0%} of unanswerable;   "
              f"out-of-sample ({o['splits']} random splits): keeps {o['kept_answerable']:.1%}, turns away {o['turned_away']:.0%}")
    if len(una) < 30:
        print("note: fewer than 30 unanswerable questions - treat these numbers as a rough guide")
    return 0


if __name__ == "__main__":
    sys.exit(main())
