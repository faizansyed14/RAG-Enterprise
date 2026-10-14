"""Text analysis, weighted fusion and document aggregation."""


from app.scale import index as ix
from app.scale import search
from app.scale.chunking import chunk_document
from app.scale.models import HashingEmbedder
from app.scale.settings import get_scale_settings
from app.scale.text import Analyzer, query_words, terms

EMB = HashingEmbedder(128)


# ---------------------------------------------------------------- the analyzer

def test_english_stemming_makes_word_forms_meet_but_never_touches_identifiers():
    an = Analyzer(stem=("en",))
    assert len(set(terms("terminated terminating terminates", an))) == 1          # one stem for the three forms
    assert terms("contracts", an) == terms("contract", an)
    assert "sub-084" in terms("Ref SUB-084", an) and "b27" in terms("drawing B27", an)
    assert terms("Terminated", Analyzer()) == ["terminated"]           # off by default


def test_arabic_is_normalised_and_the_article_is_stripped_when_asked():
    plain = Analyzer()
    stem = Analyzer(stem=("ar",))
    # tashkeel, tatweel, alef variants and ta marbuta are unified even without stemming
    assert terms("الْكِتَابُ", plain) == terms("الكتاب", plain)
    assert terms("أحمد", plain) == terms("احمد", plain)
    assert terms("مدرسة", plain) == terms("مدرسه", plain)
    # the definite article and a plural ending are removed by the stemmer, so singular / plural / defined meet
    assert terms("الكتاب", stem) == terms("كتاب", stem)
    assert terms("المهندسون", stem) == terms("مهندس", stem)


def test_cjk_text_is_searchable_without_a_dictionary():
    t = terms("合同终止通知", Analyzer())
    assert "合同" in t and "终止" in t and "通" in t               # characters and neighbouring pairs
    assert terms("hello 世界", Analyzer())[0] == "hello"


def test_stopwords_leave_only_the_query():
    assert terms("what is the notice period", Analyzer(stopwords_in_queries=True), query=True) == ["notice", "period"]
    assert terms("what is the notice period", Analyzer(stopwords_in_queries=True)) == ["what", "is", "the", "notice", "period"]
    assert query_words("What is the notice period?", Analyzer()) == ["notice", "period"]


def test_whatever_changes_the_indexed_terms_changes_the_collection_name(monkeypatch):
    base = ix.collection_name()
    monkeypatch.setenv("SCALE_STEM_LANGS", "en")
    get_scale_settings.cache_clear()
    stemmed = ix.collection_name()
    assert stemmed != base and "__a" in stemmed and "__a" in base
    monkeypatch.setenv("SCALE_CTX_SPARSE", "false")
    get_scale_settings.cache_clear()
    assert ix.collection_name() not in (base, stemmed)


# ---------------------------------------------------------------- the index modes

def put(doc, name, text, acl=("*",)):
    chunks = chunk_document([text], title=name, path=f"files/{name}")
    cfg = get_scale_settings()
    ix.upsert_chunks([
        ix.IndexedChunk(document_id=doc, doc_id="doc-" + doc, name=name, path=f"files/{name}", source="s", page=c.page,
                        chunk=c.index, start=c.start, end=c.end, acl=list(acl), dense=EMB.embed([c.embed_text])[0],
                        sparse_text=c.embed_text, text=c.text)
        for c in chunks
    ], cfg)


def find(query, **kw):
    return ix.search(EMB.embed([query])[0], query, principals={"*"}, **kw)


def test_weighted_fusion_keeps_the_raw_cosine_and_finds_the_exact_document():
    put("d1", "Falcon Agreement.txt", "The Falcon supplier agreement ends after ninety days written notice.")
    put("d2", "Cafeteria.txt", "Lunch is served from noon.")
    hits = find("falcon supplier notice")
    assert hits[0].name == "Falcon Agreement.txt"
    assert hits[0].dense_score is not None and -1.0 <= hits[0].dense_score <= 1.0


# ---------------------------------------------------------------- coverage re-ranker and aggregation

def hit(i, doc, text, score, name="n.txt", path="p/n.txt"):
    return ix.ChunkHit(id=str(i), score=score, document_id=doc, doc_id=doc, name=name, path=path, page=1, chunk=i,
                       start=0, end=len(text), text=text)


def test_a_phrase_in_the_right_order_beats_the_same_words_scattered():
    cfg = get_scale_settings()
    hits = [
        hit(0, "a", "payment received from the board; terms were reviewed; the late schedule changed", 0.5),
        hit(1, "b", "the late payment terms are thirty days", 0.5),
        hit(2, "c", "filler words about something else entirely", 0.5),
        hit(3, "d", "more filler words about another subject", 0.5),
        hit(4, "e", "even more filler about parking", 0.5),
    ]
    cov = search.coverage_scores("late payment terms", hits, cfg)
    assert cov[1] > cov[0] > cov[2]


def test_documents_are_ordered_by_their_best_chunk_and_listed_once():
    hits = [hit(0, "a", "x", 0.9), hit(1, "b", "x", 0.8), hit(2, "b", "x", 0.79), hit(3, "b", "x", 0.78), hit(4, "a", "x", 0.1)]
    assert search.rank_documents(hits) == ["a", "b"]           # three decent chunks do not outrank one excellent chunk


def test_nothing_relevant_needs_both_meaning_and_keywords_to_be_weak():
    cfg = get_scale_settings().model_copy(update={"min_confidence": 0.5, "min_lexical": 20.0})

    def one(cos, bm25):
        return [ix.ChunkHit("1", 0.9, "d1", "d1", "n", "p", 1, 0, 0, 1, "x", dense_score=cos, sparse_score=bm25)]

    assert search.candidates_from_hits(one(0.2, 5.0), cfg) == []                       # weak on both: nothing relevant
    assert [c.document_id for c in search.candidates_from_hits(one(0.7, 5.0), cfg)] == ["d1"]      # a paraphrase: meaning is strong
    assert [c.document_id for c in search.candidates_from_hits(one(0.1, 60.0), cfg)] == ["d1"]     # an identifier: keywords are strong
    assert search.candidates_from_hits(one(0.2, 5.0), get_scale_settings())            # off by default
    only_meaning = get_scale_settings().model_copy(update={"min_confidence": 0.5})
    assert search.candidates_from_hits(one(0.2, 99.0), only_meaning) == []             # a single threshold behaves as before


def test_coverage_is_a_third_vote_a_chunk_that_covers_the_question_is_never_turned_away():
    """The rule turns a question away only when meaning, keywords AND coverage are all weak. A chunk that contains what was
    asked survives even when cosine and BM25 are low (a short question, an unusual embedding); a question about something
    that is not there leaves the best chunk covering little of it, so all three votes are weak."""
    cfg = get_scale_settings().model_copy(update={"min_confidence": 0.5, "min_lexical": 20.0, "min_coverage": 0.3})

    def chunk(text, cos, bm25):
        return [ix.ChunkHit("1", 0.9, "d1", "d1", "invoice-policy", "finance/invoice-policy.md", 1, 0, 0, len(text), text,
                            dense_score=cos, sparse_score=bm25)]

    text = "Invoices are payable within thirty days of receipt. Late payment bears interest at two percent per month."
    # a question the chunk answers: high coverage, kept even with weak meaning and weak keywords
    assert search.candidates_from_hits(chunk(text, 0.3, 8.0), cfg, "invoices payable within thirty days late payment interest")
    # an invented thing: same topic words are present but the question's own words are not covered -> turned away
    assert search.candidates_from_hits(chunk(text, 0.3, 8.0), cfg, "what is the quantum escrow rebate for orbital invoices") == []
    # the coverage vote is only counted when it is switched on and the question text is known
    no_cov = get_scale_settings().model_copy(update={"min_confidence": 0.5, "min_lexical": 20.0})
    assert search.candidates_from_hits(chunk(text, 0.3, 8.0), no_cov, "what is the quantum escrow rebate for orbital invoices") == []
    assert search.candidates_from_hits(chunk(text, 0.9, 8.0), cfg, "what is the quantum escrow rebate for orbital invoices")      # strong meaning still wins
    assert search.is_off_topic(None, 0.0, 0.0, cfg) is False                          # no raw scores: never judge


def test_a_code_from_the_question_found_in_the_results_is_never_turned_away_but_an_invented_code_is():
    cfg = get_scale_settings().model_copy(update={"min_confidence": 0.9, "min_lexical": 90.0, "min_coverage": 0.9})   # absurdly strict on purpose

    def ticket(code):
        text = f"Ticket {code}: the printer on floor two was replaced and the queue cleared."
        return [ix.ChunkHit("1", 0.9, "d1", "d1", f"{code}.txt", f"tickets/{code}.txt", 1, 0, 0, len(text), text, dense_score=0.2, sparse_score=3.0)]

    hits = ticket("INC2576651")
    assert search.candidates_from_hits(hits, cfg, "What was done to resolve INC2576651?")                    # the code is in the best chunk
    assert search.candidates_from_hits(hits, cfg, "What was done to resolve INC9999999?") == []             # an invented code is not
    assert search.candidates_from_hits(hits, cfg, "What was done in 2023 about version 1.29.3?") == []      # years and versions prove nothing
    assert search.question_identifiers("CTR-2025-2186-TD in 2023 on 1.29.3 and B27") == ["ctr-2025-2186-td", "b27"]
