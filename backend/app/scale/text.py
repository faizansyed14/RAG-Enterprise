"""Text analysis for the lexical (sparse) side of the index: one function turns a string into the terms that
are indexed, and the SAME function turns a question into the terms that are searched. If the two ever
differ, words stop meeting; so every setting that changes the output is part of `analyzer_signature()`,
which is baked into the collection name (changing it means a new collection and a re-index, never a
silent mismatch).

What it does, by script
  * Latin      lower-case, NFKC, English (Snowball/Porter2) stemming when enabled: "terminated",
               "terminating", "termination" meet. Identifiers (anything with a digit) are never stemmed.
  * Arabic     diacritics and tatweel removed; alef / ya / ta-marbuta variants unified; a light stemmer
               strips the definite article and common prefixes / suffixes. (Same family of rules as
               Lucene's Arabic analyser - written independently.)
  * CJK / Thai every character is a term and adjacent pairs are terms too (no dictionary needed; a
               segmenter such as jieba is better for Chinese and can replace this).
  * Compounds  'sub-084' yields the whole token and its parts so an exact id and a loose mention both match.
"""

from __future__ import annotations

import re
import threading
import unicodedata
from dataclasses import dataclass
from functools import lru_cache

# \w does not include combining marks (Arabic diacritics, Indic vowel signs, accents written as separate marks), which
# would cut a word in pieces; so they are part of a word here.
_W = r"[\w\u0300-\u036f\u0483-\u0489\u0591-\u05c7\u0610-\u061a\u064b-\u065f\u0670\u06d6-\u06ed\u0900-\u0dff]"
_TOKEN = re.compile(rf"{_W}+(?:[-./]{_W}+)*", re.UNICODE)
_PART_SPLIT = re.compile(r"[-./_]")
_ARABIC_MARKS = re.compile("[ؐ-ًؚ-ٰٟۖ-ۭـ]")
_ALEF = re.compile("[آأإٱ]")
_CJK = re.compile(
    "[぀-ヿ㐀-䶿一-鿿가-힯฀-๿豈-﫿]"
)

# Function words plus question words. Removed from the SEARCH side only (the index keeps them; IDF makes them
# nearly free), because a question like "what is the ..." otherwise matches every chunk with a "what" or "is".
STOPWORDS = frozenset(
    """a about above after again against all also am an and any are as at be because been before being below between
    both but by can could did do does doing down during each few for from further had has have having he her here hers
    him his how i if in into is it its itself just me more most my no nor not now of off on once only or other our
    ours out over own same she should so some such than that the their theirs them then there these they this those
    through to too under until up very was we were what when where which while who whom whose why will with would you
    your yours tell please explain describe""".split()
)
_AR_STOP = frozenset(
    """في من علي الي عن مع هذا هذه ذلك تلك هذان هاتان التي الذي الذين اللذين اللتين اللاتي ما ماذا هل كيف لماذا متي اين كم او و ثم
    لكن بل لا لم لن ان انه انها انهم ان اذا اذ حتي قد كان كانت كانوا يكون تكون يكن ليس ليست هو هي هم هن هما انت انا نحن
    كل بعض غير بين عند عندما حيث حين بعد قبل خلال منذ دون ضد نحو الا ايضا فقط جدا اي ايه به بها بهم له لها لهم لهما فيه فيها
    فيهم منه منها منهم عنه عنها عليه عليها عليهم الي اليه اليها كما لقد وقد وان ولا ولم ولن وهو وهي وهل ومن وفي وعلي وما
    يا هناك هنا الان بينما عن""".split()
)


# Bump when a stemmer or the normaliser changes what a word turns into: the collection name carries it, so an old
# index is never searched with terms produced by a newer algorithm (that would silently lose matches).
STEMMER_VERSION = 2


@dataclass(frozen=True)
class Analyzer:
    stem: tuple[str, ...] = ()          # languages to stem: any of ("en", "ar")
    arabic_normalize: bool = True       # harmless for text without Arabic
    cjk_bigrams: bool = True
    stopwords_in_queries: bool = False  # drop function / question words from the SEARCH terms

    def signature(self) -> str:
        stems = f"{','.join(self.stem)}:v{STEMMER_VERSION}" if self.stem else "-"
        return "|".join([stems, f"an{int(self.arabic_normalize)}", f"cj{int(self.cjk_bigrams)}"])


_threads = threading.local()


def _english():
    """One English stemmer PER THREAD. A snowball stemmer keeps its working state on the object, so a single instance shared by
    several threads (the worker runs jobs in threads, the API serves requests in threads) crashes with IndexError or, worse,
    returns a wrong stem silently; the cache below would then keep that wrong stem for every later chunk."""
    stemmer = getattr(_threads, "english", None)
    if stemmer is None:
        import snowballstemmer

        stemmer = _threads.english = snowballstemmer.stemmer("english")
    return stemmer


@lru_cache(maxsize=200_000)
def _stem_en(word: str) -> str:
    return _english().stemWord(word)


# "Light" stemming (the published light10 family): strip ONE prefix, then every matching suffix. A single-letter
# affix needs a word of at least 4 letters, longer affixes leave at least 2 letters.
_AR_PREFIXES = ("وال", "بال", "كال", "فال", "ال", "لل", "و")
_AR_SUFFIXES = ("ها", "ان", "ات", "ون", "ين", "يه", "ية", "ه", "ة", "ي")


def normalize_arabic(word: str) -> str:
    word = _ARABIC_MARKS.sub("", word)
    word = _ALEF.sub("ا", word)
    return word.replace("ى", "ي").replace("ة", "ه")


def _affix_fits(word: str, affix: str) -> bool:
    return len(word) >= 4 if len(affix) == 1 else len(word) >= len(affix) + 2


def stem_arabic(word: str) -> str:
    for prefix in _AR_PREFIXES:
        if word.startswith(prefix) and _affix_fits(word, prefix):
            word = word[len(prefix):]
            break
    for suffix in _AR_SUFFIXES:
        if word.endswith(suffix) and _affix_fits(word, suffix):
            word = word[: -len(suffix)]
    return word


def _is_arabic(word: str) -> bool:
    return any("؀" <= ch <= "ۿ" for ch in word)


def _base_terms(text: str, an: Analyzer) -> list[tuple[str, list[str], str]]:
    """[(indexed term, [part terms], unstemmed form)] in reading order. Parts are the pieces of a compound id; the
    unstemmed form is what the stop-word lists are checked against (a stemmer would turn 'does' into 'doe')."""
    text = unicodedata.normalize("NFKC", text).casefold()
    if an.arabic_normalize:
        text = _ARABIC_MARKS.sub("", text)            # before splitting: diacritics are not word boundaries
    out: list[tuple[str, list[str], str]] = []
    for raw in _TOKEN.findall(text):
        if _CJK.search(raw):
            out.extend((t, parts, t) for t, parts in _cjk_terms(raw, an))
            continue
        parts = [_one(p, an) for p in _PART_SPLIT.split(raw) if p] if _PART_SPLIT.search(raw) else []
        out.append((_one(raw, an), parts, _one(raw, an, stem=False)))
    return out


def _one(word: str, an: Analyzer, stem: bool = True) -> str:
    if any(ch.isdigit() for ch in word):
        return word                                   # identifiers are matched exactly
    if _is_arabic(word):
        if an.arabic_normalize:
            word = normalize_arabic(word)
        if stem and "ar" in an.stem:
            word = stem_arabic(word)
        return word
    if stem and "en" in an.stem and word.isascii() and word.isalpha() and len(word) > 3:
        return _stem_en(word)
    return word


def _cjk_terms(raw: str, an: Analyzer) -> list[tuple[str, list[str]]]:
    chars = [c for c in raw if _CJK.match(c)]
    terms = [(c, []) for c in chars]
    if an.cjk_bigrams:
        terms.extend((a + b, []) for a, b in zip(chars, chars[1:]))
    return terms


def terms(text: str, an: Analyzer, *, query: bool = False) -> list[str]:
    """The terms to index (query=False) or to search (query=True)."""
    base = _base_terms(text, an)
    drop = query and an.stopwords_in_queries
    out: list[str] = []
    for term, parts, plain in base:
        if drop and (plain in STOPWORDS or plain in _AR_STOP):
            continue
        out.append(term)
        out.extend(parts)
    return out


def query_words(text: str, an: Analyzer) -> list[str]:
    """Single-word terms of a question, stopwords removed, for the "nothing relevant" check and for diagnostics."""
    base = _base_terms(text, an)
    return [t for t, _, plain in base if plain not in STOPWORDS and plain not in _AR_STOP and len(t) > 1]
