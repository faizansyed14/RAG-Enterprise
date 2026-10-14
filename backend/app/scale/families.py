"""Near-duplicate families among the candidate documents.

Companies keep many copies of the same file: drafts and revisions, the same contract in several folders, a template filled in
for twenty customers. When a question matches one of them, it matches all of them equally, and a dozen near-identical copies of
one document can fill the candidate list ahead of a different, relevant document (measured in BENCHMARKS.md, section 22).

Nothing is removed. Documents whose best-matching passages are near-identical form a family; the first `family_cap` members of
a family (the best ranked) keep their place, the rest move behind the other documents, in their own order. They are still
candidates if there is room, and a copy that is the better answer (its own passage ranks above the others') is a family's head and
stays where it is. Only documents the caller may already read are compared, so this can never show or hide anything by permission.
"""

from __future__ import annotations

import heapq
import re
from typing import Sequence

_WORD = re.compile(r"\w+", re.UNICODE)


def shingles(text: str, size: int = 4) -> frozenset[int]:
    """Hashes of every run of `size` words. (Python's own hash is enough: signatures are compared inside one process only.)"""
    words = _WORD.findall(text.lower())
    if len(words) < size:
        return frozenset({hash(" ".join(words))}) if words else frozenset()
    return frozenset(hash(" ".join(words[i: i + size])) for i in range(len(words) - size + 1))


def similarity(a: frozenset[int], b: frozenset[int]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


_KEYS = 4        # a document is looked up under its smallest few shingle hashes; two near-identical texts share one of them
_WINDOW = 80     # only the best-ranked documents are grouped (the candidate list is shorter than this)


def demote_copies(order: Sequence[str], best_text: dict[str, str], *, cap: int, threshold: float) -> list[str]:
    """`order`: document ids best first. `best_text`: each document's best-matching passage (a missing one is never grouped).
    Returns the same ids with every family member beyond the first `cap` moved behind the rest."""
    if cap <= 0 or len(order) <= cap:
        return list(order)
    window, tail = list(order[:_WINDOW]), list(order[_WINDOW:])
    by_key: dict[int, list[int]] = {}                     # shingle hash -> families that have a member with it
    families: list[tuple[frozenset[int], list[str]]] = []  # (the first member's shingles, members so far)
    kept: list[str] = []
    demoted: list[str] = []
    for doc in window:
        sig = shingles(best_text.get(doc, ""))
        keys = heapq.nsmallest(_KEYS, sig)
        found = None
        for key in keys:
            for index in by_key.get(key, ()):
                if similarity(sig, families[index][0]) >= threshold:
                    found = index
                    break
            if found is not None:
                break
        if found is None:
            families.append((sig, [doc]))
            for key in keys:
                by_key.setdefault(key, []).append(len(families) - 1)
            kept.append(doc)
            continue
        members = families[found][1]
        members.append(doc)
        (kept if len(members) <= cap else demoted).append(doc)
    return kept + tail + demoted
