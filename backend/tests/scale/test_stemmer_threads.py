"""The English stemmer must give the same answer whichever thread asks (a shared snowball instance crashed with IndexError and,
silently, returned wrong stems when two threads used it at once; the stem cache then kept the wrong ones)."""

import random
import re
import threading

import snowballstemmer

from app.scale import text as T


def _words(n=4000):
    seed = random.Random(3)
    letters = "abcdefghilmnoprstu"
    ends = ["", "s", "ed", "ing", "ion", "ions", "ly", "ness", "ment", "able", "ational", "izer", "ful", "ous", "e", "es", "ies", "ence"]
    return sorted({"".join(seed.choice(letters) for _ in range(seed.randint(2, 8))) + seed.choice(ends) for _ in range(n * 2)})[:n]


def test_threads_get_the_same_stems_as_one_thread():
    words = _words()
    reference = snowballstemmer.stemmer("english")
    truth = {w: reference.stemWord(w) for w in words}
    bad, crashes = [], []

    def work(seed):
        order = words[:]
        random.Random(seed).shuffle(order)
        for w in order:
            try:
                if T._english().stemWord(w) != truth[w]:
                    bad.append(w)
            except Exception as exc:  # noqa: BLE001
                crashes.append((w, type(exc).__name__))

    threads = [threading.Thread(target=work, args=(i,)) for i in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert crashes == [] and bad == []


def test_each_thread_has_its_own_stemmer_and_reuses_it():
    seen = {}

    def grab(name):
        seen[name] = (T._english(), T._english())

    threads = [threading.Thread(target=grab, args=(i,)) for i in range(3)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert all(a is b for a, b in seen.values())                    # reused inside a thread
    assert len({id(a) for a, _ in seen.values()}) == 3              # never shared between threads
