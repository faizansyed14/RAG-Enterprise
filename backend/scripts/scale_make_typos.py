"""Make a misspelled copy of a golden set, to measure typo robustness.

  python -m scripts.scale_make_typos golden.json golden_typo.json [queries_typo.tsv]

Natural-language and keyword questions get one slip (two letters swapped, one dropped, one doubled) in one or two of
their content words, chosen deterministically from the question id, so the same file always comes out the same.
Identifier questions are left alone (a wrong digit in a contract number is not a typo anyone expects to survive).
The optional .tsv ("id<TAB>question") feeds scripts/lucene_reference.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys

from app.scale.text import STOPWORDS


def _h(s: str) -> int:
    return int(hashlib.blake2b(s.encode(), digest_size=4).hexdigest(), 16)


def slip(word: str, seed: int) -> str:
    inner = len(word) - 2
    kind = seed % 3
    i = 1 + (seed // 3) % max(inner - 1, 1)
    if kind == 0:                                     # swap two neighbouring letters
        return word[:i] + word[i + 1] + word[i] + word[i + 2:]
    if kind == 1:                                     # drop a letter
        return word[:i] + word[i + 1:]
    return word[:i] + word[i] + word[i:]              # double a letter


def make_typos(gold: list[dict]) -> list[dict]:
    out = []
    for g in gold:
        if g.get("style") not in ("nl", "kw", "name"):
            continue
        words = re.findall(r"\S+", g["q"])
        cand = [i for i, w in enumerate(words) if re.fullmatch(r"[A-Za-z]{5,}", w) and w.lower() not in STOPWORDS]
        if not cand:
            continue
        pick = sorted(cand, key=lambda i: _h(g["id"] + words[i]))[: 1 + _h(g["id"]) % 2]
        new = list(words)
        for i in pick:
            new[i] = slip(words[i], _h(g["id"] + "x" + words[i]))
        q = " ".join(new)
        if q != g["q"]:
            out.append({**g, "q": q, "orig_q": g["q"]})
    return out


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args and args[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    if len(args) < 2:
        print(__doc__)
        return 2
    out = make_typos(json.load(open(args[0])))
    json.dump(out, open(args[1], "w"), indent=0)
    if len(args) > 2:
        open(args[2], "w").write("".join(f"{g['id']}\t{g['q']}\n" for g in out))
    print(f"{len(out)} misspelled questions written to {args[1]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
