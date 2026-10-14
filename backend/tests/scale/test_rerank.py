"""The cross-encoder reranker: how its verdict is merged, how much of the pool it reads, what happens when it fails,
and (when a model is on disk) that the real ONNX cross-encoder prefers the passage that answers the question.

The real-model tests need a cross-encoder exported to ONNX: point SCALE_TEST_RERANK_ONNX_DIR at a directory holding
tokenizer.json and onnx/model.onnx (for example Xenova/ms-marco-MiniLM-L-6-v2). Without it they are skipped, and
the logic tests below still run on a fake model.
"""

import os

import pytest

from app.scale import index as ix
from app.scale import search
from app.scale.settings import get_scale_settings


def hit(i, text, score, doc=None):
    doc = doc or f"d{i}"
    return ix.ChunkHit(id=str(i), score=score, document_id=doc, doc_id=doc, name=f"{doc}.txt", path=f"p/{doc}.txt",
                       page=1, chunk=i, start=0, end=len(text), text=text)


class FakeReranker:
    """Scores a passage by how many of the question's words it contains; remembers what it was asked."""

    def __init__(self):
        self.calls = []

    def rerank(self, query, documents, top_n):
        self.calls.append(list(documents))
        words = set(query.lower().split())
        scores = [float(len(words & set(d.lower().split()))) for d in documents]
        ranked = sorted(enumerate(scores), key=lambda p: p[1], reverse=True)
        return ranked[:top_n]


# ---------------------------------------------------------------- merging the two verdicts

def test_blend_zero_is_the_fused_order_and_blend_one_is_the_model_order():
    fused, model = [0.9, 0.5, 0.1], [-5.0, 8.0, 2.0]
    order = lambda s: sorted(range(len(s)), key=lambda i: s[i], reverse=True)        # noqa: E731
    assert order(search.combine_scores(fused, model, 0.0)) == order(fused)
    assert order(search.combine_scores(fused, model, 1.0)) == order(model)


def test_blend_lets_a_confident_model_overrule_a_small_fused_lead_but_not_a_large_one():
    # chunk 0 leads the fused order; the model prefers chunk 1
    near = search.combine_scores([0.52, 0.50, 0.0], [1.0, 9.0, 0.0], 0.5)
    far = search.combine_scores([1.00, 0.30, 0.0], [4.0, 5.0, 0.0], 0.5)
    assert near[1] > near[0]
    assert far[0] > far[1]


def test_a_flat_score_list_does_not_divide_by_zero():
    assert search.combine_scores([0.5, 0.5], [1.0, 1.0], 0.5) == [0.0, 0.0]
    assert search.combine_scores([], [], 0.5) == []


# ---------------------------------------------------------------- search_chunks with a reranker

def seeded(monkeypatch, fake, **settings):
    for key, value in settings.items():
        monkeypatch.setenv(f"SCALE_{key.upper()}", str(value))
    get_scale_settings.cache_clear()
    monkeypatch.setattr(search, "get_reranker", lambda cfg=None: fake)
    search.reset_model_caches()
    base = [
        hit(0, "general policy about holidays", 0.95),
        hit(1, "unrelated cafeteria menu", 0.90),
        hit(2, "the notice period for terminating the supplier contract is ninety days", 0.40),
        hit(3, "parking rules", 0.35),
        hit(4, "travel budget", 0.30),
    ]
    monkeypatch.setattr(search.ix, "search", lambda *a, **k: [h for h in base])
    monkeypatch.setattr(search, "hydrate", lambda hits: hits)
    monkeypatch.setattr(search, "embed_queries", lambda e, q: [[0.0]])
    return base


Q = "notice period for terminating the supplier contract"


def test_the_model_promotes_the_chunk_that_answers_the_question(monkeypatch):
    fake = FakeReranker()
    seeded(monkeypatch, fake, rerank_blend=1.0)
    hits = search.search_chunks(Q, principals={"*"}, limit=3, rerank=True)
    assert hits[0].document_id == "d2"


def test_only_rerank_top_chunks_are_read_by_the_model_and_the_rest_follow_in_fused_order(monkeypatch):
    fake = FakeReranker()
    seeded(monkeypatch, fake, rerank_blend=1.0, rerank_top=3)
    hits = search.search_chunks(Q, principals={"*"}, limit=5, rerank=True)
    assert len(fake.calls[0]) == 3                                           # the model saw three passages, not five
    assert [h.document_id for h in hits][3:] == ["d3", "d4"]                  # the unread ones keep their fused order, after
    assert hits[0].document_id == "d2"


def test_the_model_never_reads_more_than_rerank_top_even_when_many_results_are_asked_for(monkeypatch):
    fake = FakeReranker()
    seeded(monkeypatch, fake, rerank_blend=1.0, rerank_top=2)
    hits = search.search_chunks(Q, principals={"*"}, limit=120, rerank=True)
    assert len(fake.calls[0]) == 2
    assert len(hits) == 5                                # nothing is dropped: the unread chunks follow


def test_a_failing_model_leaves_the_fused_order_untouched(monkeypatch):
    class Broken:
        def rerank(self, *a, **k):
            return None

    seeded(monkeypatch, Broken())
    hits = search.search_chunks(Q, principals={"*"}, limit=5, rerank=True)
    assert [h.document_id for h in hits] == ["d0", "d1", "d2", "d3", "d4"]


def test_rerank_false_never_calls_the_model(monkeypatch):
    fake = FakeReranker()
    seeded(monkeypatch, fake)
    hits = search.search_chunks(Q, principals={"*"}, limit=5, rerank=False)
    assert fake.calls == []
    assert [h.document_id for h in hits][0] == "d0"


def test_blend_mode_keeps_a_strong_fused_hit_the_model_is_unsure_about(monkeypatch):
    class Indifferent:
        def rerank(self, query, documents, top_n):
            return [(i, 0.0) for i in range(len(documents))][:top_n]

    seeded(monkeypatch, Indifferent(), rerank_blend=0.5)
    hits = search.search_chunks(Q, principals={"*"}, limit=5, rerank=True)
    assert [h.document_id for h in hits] == ["d0", "d1", "d2", "d3", "d4"]


def test_a_remote_reranker_that_returns_fewer_results_than_asked_does_not_lose_the_others(monkeypatch):
    class Partial:
        def rerank(self, query, documents, top_n):
            return [(2, 5.0)]                       # only one result comes back

    seeded(monkeypatch, Partial())
    hits = search.search_chunks(Q, principals={"*"}, limit=5, rerank=True)
    assert [h.document_id for h in hits] == ["d2", "d0", "d1", "d3", "d4"]


def test_the_default_leaves_reranking_off():
    assert get_scale_settings().rerank_mode == "none"
    from app.scale.models import get_reranker

    assert get_reranker() is None


def test_onnx_mode_without_model_files_searches_without_a_reranker_and_says_why(monkeypatch, tmp_path, caplog):
    """A missing reranker must never break search: it only improves the order."""
    pytest.importorskip("onnxruntime")
    from app.scale import models
    from app.scale.models import get_reranker

    models._WARNED.clear()
    for directory in ("", str(tmp_path)):                       # not set at all, and an empty folder
        monkeypatch.setenv("SCALE_RERANK_MODE", "onnx")
        monkeypatch.setenv("SCALE_RERANK_ONNX_DIR", directory)
        get_scale_settings.cache_clear()
        models._WARNED.clear()
        caplog.clear()
        assert get_reranker() is None
        assert "reranker is not available" in caplog.text


def test_a_remote_url_wins_over_the_local_model(monkeypatch):
    monkeypatch.setenv("SCALE_RERANK_MODE", "onnx")
    monkeypatch.setenv("SCALE_RERANK_URL", "http://reranker.invalid/rerank")
    get_scale_settings.cache_clear()
    from app.scale.models import HttpReranker, get_reranker

    assert isinstance(get_reranker(), HttpReranker)


# ---------------------------------------------------------------- the real cross-encoder

MODEL_DIR = os.environ.get("SCALE_TEST_RERANK_ONNX_DIR", "")
real = pytest.mark.skipif(not MODEL_DIR, reason="set SCALE_TEST_RERANK_ONNX_DIR to a cross-encoder exported to ONNX")


@pytest.fixture
def onnx_reranker(monkeypatch):
    pytest.importorskip("onnxruntime")
    pytest.importorskip("tokenizers")
    monkeypatch.setenv("SCALE_RERANK_MODE", "onnx")
    monkeypatch.setenv("SCALE_RERANK_ONNX_DIR", MODEL_DIR)
    get_scale_settings.cache_clear()
    from app.scale.models import get_reranker

    return get_reranker()


@real
def test_the_real_cross_encoder_puts_the_answering_passage_first(onnx_reranker):
    docs = [
        "Lunch is served in the cafeteria from noon until two.",
        "Either party may terminate the supplier agreement by giving ninety days written notice.",
        "The parking garage closes at midnight on weekdays.",
        "Invoices are paid monthly in arrears.",
    ]
    ranked = onnx_reranker.rerank("how much notice is needed to terminate the supplier agreement", docs, top_n=4)
    assert ranked[0][0] == 1
    assert ranked[0][1] > 0 > ranked[-1][1]            # relevant above zero, irrelevant below, for this model


@real
def test_the_real_cross_encoder_tells_two_near_duplicate_passages_apart(onnx_reranker):
    docs = [
        "Employees in Dubai accrue 30 calendar days of annual leave per year.",
        "Employees in London accrue 25 working days of annual leave per year.",
    ]
    ranked = onnx_reranker.rerank("how many days of annual leave do London employees get", docs, top_n=2)
    assert ranked[0][0] == 1


@real
def test_scores_do_not_depend_on_batching_or_order(onnx_reranker):
    docs = ["short text about notice periods", "a much longer passage " * 40 + "about notice periods", "unrelated"]
    q = "notice period"
    together = onnx_reranker.score(q, docs)
    alone = [onnx_reranker.score(q, [d])[0] for d in docs]
    for a, b in zip(together, alone):
        assert abs(a - b) < 1e-2                         # padding changes nothing but float noise
    assert onnx_reranker.score(q, list(reversed(docs))) == pytest.approx(list(reversed(together)), abs=1e-2)


@real
def test_empty_input_and_blank_passages_do_not_crash(onnx_reranker):
    assert onnx_reranker.rerank("anything", [], top_n=5) == []
    assert len(onnx_reranker.score("anything", ["", "   "])) == 2


@real
def test_a_very_long_passage_is_cut_not_rejected(onnx_reranker):
    out = onnx_reranker.score("notice", ["word " * 5000])
    assert len(out) == 1
