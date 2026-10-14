"""A repeated question is embedded once and its (question, passage) scores are computed once."""

import os
from pathlib import Path

import pytest

from app.scale.models import LRU, OnnxReranker, embed_queries
from app.scale.settings import get_scale_settings


class Counting:
    dim = 2
    name = "count"

    def __init__(self):
        self.calls = []

    def embed(self, texts):
        self.calls.append(list(texts))
        return [[float(len(t)), 1.0] for t in texts]


def test_lru_evicts_the_least_recently_used():
    c = LRU(2)
    c.put("a", 1); c.put("b", 2)
    assert c.get("a") == 1
    c.put("c", 3)                                  # evicts b, not a
    assert c.get("b") is None and c.get("a") == 1 and c.get("c") == 3
    off = LRU(0)
    off.put("x", 1)
    assert off.get("x") is None


def test_the_same_question_is_embedded_once():
    e = Counting()
    first = embed_queries(e, ["notice period", "late fees"])
    again = embed_queries(e, ["late fees", "notice period", "brand new"])
    assert again[0] == first[1] and again[1] == first[0]
    assert e.calls == [["notice period", "late fees"], ["brand new"]]          # only the new one was computed


def test_cache_can_be_switched_off(monkeypatch):
    monkeypatch.setenv("SCALE_QUERY_CACHE_SIZE", "0")
    get_scale_settings.cache_clear()
    e = Counting()
    embed_queries(e, ["q"]); embed_queries(e, ["q"])
    assert len(e.calls) == 2


MODELS = os.environ.get("SCALE_TEST_MODELS_DIR", "")


@pytest.mark.skipif(not (MODELS and (Path(MODELS) / "ms-marco-MiniLM-L-6-v2/onnx/model.onnx").exists()), reason="needs the local reranker")
def test_reranker_scores_are_reused_and_identical(monkeypatch):
    monkeypatch.setenv("SCALE_RERANK_ONNX_DIR", str(Path(MODELS) / "ms-marco-MiniLM-L-6-v2"))
    get_scale_settings.cache_clear()
    r = OnnxReranker()
    docs = ["Termination requires ninety days written notice.", "Lunch is served at noon.", "Invoices are payable in thirty days."]
    first = r.score("how long is the notice period", docs)
    calls = []
    real = r._session.run
    r._session.run = lambda *a, **k: (calls.append(1), real(*a, **k))[1]
    second = r.score("how long is the notice period", docs)
    assert second == first and not calls                       # nothing was run the second time
    mixed = r.score("how long is the notice period", docs[:1] + ["a passage never seen before"])
    assert mixed[0] == first[0] and len(calls) == 1            # only the new passage went through the model
    assert first[0] > first[1]                                  # and the model still ranks sensibly
