"""A/B harness: does a change (another embedding model, reranker, chunk size, a new release) make your real answers better or worse?

`scale_eval.py` measures stage A (the candidate documents) only. It cannot say whether a *final answer*
got better or worse. This script compares what
the user actually gets: it asks the running app the same questions and records the answer, the documents
cited, refusals, errors and time.

Run it twice against the SAME documents, once per configuration (change one thing between the runs), then compare:

  # 1. start the app as it is today
  python -m scripts.scale_ab run golden.json --base-url http://localhost:8000 --users users.json \\
         --label before --out ab_off.json
  # 2. change the one setting (a new embedding model: let `migration status` reach 100% first), restart, and run again
  python -m scripts.scale_ab run golden.json --base-url http://localhost:8000 --users users.json \\
         --label after --out ab_on.json
  # 3.
  python -m scripts.scale_ab compare ab_off.json ab_on.json [--max-regressions 0]

golden.json (write 50-200 real questions WITH the client; same shape as scale_eval.py plus a few keys):

  [{"q": "What is the notice period in the Falcon supplier contract?",
    "user": "alice",                              # optional; --default-user otherwise
    "expect": ["Falcon Supplier Agreement"],      # substrings of the CITED document names
    "answer_contains": ["90 days"],               # optional: substrings the answer must contain
    "forbid": ["salary"],                         # substrings that must never be cited or appear in the answer
    "expect_refusal": false},                     # true: the right behaviour is to cite nothing
   ...]

users.json maps a username to the NAME OF AN ENVIRONMENT VARIABLE holding its password (never the password):
  {"alice": "AB_PASSWORD_ALICE", "bob": "AB_PASSWORD_BOB"}

What this does NOT do: judge whether an answer is *correct* beyond the substrings you list. Read the cases
it marks as differing; for a larger set add an LLM judge (UPGRADE_PROMPT.md, phase 4). It spends model credits
(one chat per question). Admin accounts are not metered; ordinary users are.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from pathlib import Path
from typing import Any, Iterable, Iterator


# ------------------------------------------------------------------ pure helpers (unit tested)

def parse_sse(lines: Iterable[str]) -> Iterator[dict[str, Any]]:
    """Turn the chat endpoint's `event: x` / `data: {json}` stream into event dicts. Bad frames are skipped."""
    event_type, data = None, []
    for raw in lines:
        line = raw.rstrip("\r\n")
        if line == "":
            if data:
                try:
                    payload = json.loads("\n".join(data))
                except ValueError:
                    payload = None
                if isinstance(payload, dict):
                    payload.setdefault("type", event_type)
                    yield payload
            event_type, data = None, []
        elif line.startswith("event:"):
            event_type = line[6:].strip()
        elif line.startswith("data:"):
            data.append(line[5:].lstrip())
    if data:
        try:
            payload = json.loads("\n".join(data))
        except ValueError:
            payload = None
        if isinstance(payload, dict):
            payload.setdefault("type", event_type)
            yield payload


def fold_events(events: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Collapse a stream of chat events into one result record."""
    answer, cited, refused, error = [], [], False, None
    for ev in events:
        kind = ev.get("type")
        if kind == "answer" and ev.get("delta"):
            answer.append(str(ev["delta"]))
        elif kind == "citations":
            refused = bool(ev.get("refused"))
            for c in ev.get("citations", []):
                cited.append({"document": c.get("document") or "", "page": c.get("page"),
                              "document_id": c.get("document_id")})
        elif kind == "error":
            error = str(ev.get("message") or ev.get("detail") or ev)[:300]
    return {"answer": "".join(answer), "cited": cited, "refused": refused, "error": error}


def score_result(case: dict[str, Any], res: dict[str, Any]) -> dict[str, Any]:
    cited = [c["document"].lower() for c in res.get("cited", [])]
    text = (res.get("answer") or "").lower()
    expect = case.get("expect", [])
    found = [e for e in expect if any(e.lower() in c for c in cited)]
    cited_recall = (len(found) / len(expect)) if expect else 1.0
    wanted = case.get("answer_contains", [])
    contains = (sum(1 for w in wanted if w.lower() in text) / len(wanted)) if wanted else 1.0
    forbidden = sum(1 for f in case.get("forbid", []) if any(f.lower() in c for c in cited) or f.lower() in text)
    ok = bool(res.get("ok", True)) and not res.get("error")
    answered = ok and not res.get("refused") and bool((res.get("answer") or "").strip())
    if case.get("expect_refusal"):
        passed = ok and not cited and forbidden == 0
    else:
        passed = answered and cited_recall == 1.0 and contains == 1.0 and forbidden == 0
    return {"cited_recall": cited_recall, "contains": contains, "forbidden": forbidden,
            "answered": answered, "passed": passed}


def _pct(values: list[float], p: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, int(len(ordered) * p))], 2)


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(rows)
    times = [r["seconds"] for r in rows if r.get("seconds") is not None]
    return {
        "questions": n,
        "pass_rate": round(sum(1 for r in rows if r["score"]["passed"]) / n, 4) if n else None,
        "cited_recall": round(statistics.fmean(r["score"]["cited_recall"] for r in rows), 4) if n else None,
        "answered_rate": round(sum(1 for r in rows if r["score"]["answered"]) / n, 4) if n else None,
        "forbidden_total": sum(r["score"]["forbidden"] for r in rows),
        "errors": sum(1 for r in rows if r.get("error") or not r.get("ok", True)),
        "p50_seconds": _pct(times, 0.5),
        "p95_seconds": _pct(times, 0.95),
    }


def compare_runs(base: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    """Per question: who passed. `regressions` = passed before, fails now (the list that matters most)."""
    by_q = {r["q"]: r for r in base["cases"]}
    regressions, improvements, both_fail, missing = [], [], [], []
    for r in new["cases"]:
        b = by_q.get(r["q"])
        if b is None:
            missing.append(r["q"])
            continue
        bp, np_ = b["score"]["passed"], r["score"]["passed"]
        if bp and not np_:
            regressions.append(r["q"])
        elif np_ and not bp:
            improvements.append(r["q"])
        elif not bp and not np_:
            both_fail.append(r["q"])
    return {"base": base["summary"], "new": new["summary"], "regressions": regressions,
            "improvements": improvements, "both_fail": both_fail, "not_in_base": missing}


# ------------------------------------------------------------------ talking to the app

def _login(client, username: str, password: str) -> str:
    r = client.post("/api/auth/login", json={"username": username, "password": password})
    r.raise_for_status()
    return r.json()["token"]


def ask(client, token: str, q: str, timeout: float = 240.0) -> dict[str, Any]:
    """One chat turn. Never raises: a failure is recorded as a result with ok=False."""
    t0 = time.perf_counter()
    first = None
    events: list[dict[str, Any]] = []
    try:
        with client.stream("POST", "/api/chat", json={"query": q, "document_ids": None, "history": []},
                           headers={"Authorization": f"Bearer {token}"}, timeout=timeout) as resp:
            if resp.status_code != 200:
                body = resp.read().decode("utf-8", "replace")[:300]
                return {"ok": False, "status": resp.status_code, "answer": "", "cited": [], "refused": False,
                        "error": f"HTTP {resp.status_code}: {body}", "seconds": round(time.perf_counter() - t0, 3),
                        "first_token_seconds": None}
            for ev in parse_sse(resp.iter_lines()):
                if first is None and ev.get("type") == "answer" and ev.get("delta"):
                    first = time.perf_counter() - t0
                events.append(ev)
    except Exception as exc:  # noqa: BLE001 - a harness must record failures, not die on them
        return {"ok": False, "status": None, "answer": "", "cited": [], "refused": False,
                "error": f"{type(exc).__name__}: {exc}"[:300], "seconds": round(time.perf_counter() - t0, 3),
                "first_token_seconds": None}
    res = fold_events(events)
    res.update(ok=True, status=200, seconds=round(time.perf_counter() - t0, 3),
               first_token_seconds=round(first, 3) if first is not None else None)
    return res


def run(golden: list[dict[str, Any]], client, users: dict[str, str], default_user: str, label: str,
        pause: float = 0.0) -> dict[str, Any]:
    tokens: dict[str, str] = {}
    rows = []
    for case in golden:
        user = case.get("user") or default_user
        if user not in tokens:
            env_name = users.get(user)
            password = os.environ.get(env_name or "")
            if not password:
                raise SystemExit(f"no password for {user!r}: set the environment variable named in users.json ({env_name!r})")
            tokens[user] = _login(client, user, password)
        res = ask(client, tokens[user], case["q"])
        score = score_result(case, res)
        rows.append({"q": case["q"], "user": user, **res, "score": score})
        print(f"{'PASS' if score['passed'] else 'FAIL'}  {res.get('seconds', 0):6.1f}s  {case['q'][:80]}", flush=True)
        if pause:
            time.sleep(pause)
    return {"label": label, "summary": summarize(rows), "cases": rows}


# ------------------------------------------------------------------ CLI

def _print_compare(c: dict[str, Any], base_label: str, new_label: str) -> None:
    keys = ["questions", "pass_rate", "cited_recall", "answered_rate", "forbidden_total", "errors", "p50_seconds", "p95_seconds"]
    print(f"{'':16}{base_label:>14}{new_label:>14}")
    for k in keys:
        print(f"{k:16}{str(c['base'].get(k)):>14}{str(c['new'].get(k)):>14}")
    for title, items in (("REGRESSIONS (passed before, fails now)", c["regressions"]),
                         ("IMPROVEMENTS (failed before, passes now)", c["improvements"]),
                         ("FAIL IN BOTH", c["both_fail"]), ("NOT IN BASELINE RUN", c["not_in_base"])):
        print(f"\n{title}: {len(items)}")
        for q in items[:25]:
            print(f"  - {q[:110]}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("golden")
    r.add_argument("--base-url", default="http://localhost:8000")
    r.add_argument("--users", required=True, help="JSON: username -> NAME of the env var holding the password")
    r.add_argument("--default-user")
    r.add_argument("--label", required=True)
    r.add_argument("--out", required=True)
    r.add_argument("--pause", type=float, default=0.0, help="seconds between questions (chat is rate-limited per user)")
    c = sub.add_parser("compare")
    c.add_argument("base")
    c.add_argument("new")
    c.add_argument("--max-regressions", type=int, default=0)
    args = parser.parse_args(argv)

    if args.cmd == "run":
        import httpx

        golden = json.loads(Path(args.golden).read_text())
        users = json.loads(Path(args.users).read_text())
        default_user = args.default_user or next(iter(users))
        with httpx.Client(base_url=args.base_url) as client:
            out = run(golden, client, users, default_user, args.label, args.pause)
        Path(args.out).write_text(json.dumps(out, indent=2))
        print(json.dumps(out["summary"], indent=2))
        return 0

    base = json.loads(Path(args.base).read_text())
    new = json.loads(Path(args.new).read_text())
    result = compare_runs(base, new)
    _print_compare(result, base.get("label", "base"), new.get("label", "new"))
    bad = result["new"]["forbidden_total"] > 0 or len(result["regressions"]) > args.max_regressions
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
