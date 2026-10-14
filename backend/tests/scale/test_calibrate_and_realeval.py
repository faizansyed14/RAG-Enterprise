"""Pure parts of the calibration and real-document evaluation tools."""

from scripts.scale_calibrate import auc, out_of_sample, pick_thresholds
from scripts.scale_realeval import rank_of, score_ranks, split_by, split_settings


def test_thresholds_turn_away_questions_weak_on_both_signals_but_keep_identifiers_and_paraphrases():
    # (top cosine, top BM25)
    answerable = [(0.70, 40.0), (0.65, 35.0), (0.60, 30.0), (0.15, 90.0),     # the last: an identifier lookup (low meaning, strong keywords)
                  (0.72, 12.0), (0.68, 45.0), (0.66, 33.0), (0.62, 28.0), (0.64, 31.0), (0.69, 38.0)]
    unanswerable = [(0.30, 10.0), (0.35, 12.0), (0.40, 9.0), (0.33, 15.0), (0.38, 8.0)]
    t = pick_thresholds(answerable, unanswerable, keep=0.9)
    assert t["kept_answerable"] >= 0.9 and t["turned_away"] == 1.0
    # the identifier lookup and the paraphrase both survive the rule
    survives = lambda c, b: c >= t["min_confidence"] or b >= t["min_lexical"]
    assert survives(0.15, 90.0) and survives(0.72, 12.0)
    assert not survives(0.30, 10.0)


def test_separation_is_one_for_perfectly_separated_scores_and_half_for_identical_ones():
    assert auc([0.9, 0.8], [0.1, 0.2]) == 1.0
    assert auc([0.5, 0.5], [0.5, 0.5]) == 0.5


def test_scoring_helpers():
    assert rank_of(["a", "b", "c"], ["c", "x"]) == 3 and rank_of(["a"], ["z"]) is None
    s = score_ranks([1, 2, None, 11])
    assert s["hit@1"] == 0.25 and s["hit@10"] == 0.5 and s["missed"] == 1
    assert split_by([{"rank": 1, "style": "nl"}, {"rank": None, "style": "kw"}], "style")["kw"]["hit@1"] == 0.0
    index_part, query_part = split_settings({"stem_langs": "en", "fusion_dense_weight": 0.3, "ctx_dense": False})
    assert index_part == {"stem_langs": "en", "ctx_dense": False} and query_part == {"fusion_dense_weight": 0.3}


def test_a_third_signal_is_used_when_the_first_two_cannot_separate_the_hard_questions():
    # (top cosine, top BM25, coverage). The hard unanswerable questions are weak in meaning but their BM25 overlaps the
    # answerable ones; only coverage separates them.
    answerable = [(0.7, 40.0, 0.8), (0.65, 35.0, 0.7), (0.3, 30.0, 0.6), (0.2, 80.0, 0.9), (0.68, 12.0, 0.5), (0.66, 33.0, 0.7),
                  (0.62, 28.0, 0.6), (0.64, 31.0, 0.55), (0.69, 38.0, 0.75), (0.25, 25.0, 0.5)]
    unanswerable = [(0.3, 28.0, 0.1), (0.35, 32.0, 0.12), (0.2, 26.0, 0.08), (0.4, 30.0, 0.15)]
    two = pick_thresholds([a[:2] for a in answerable], [u[:2] for u in unanswerable], keep=0.9)
    three = pick_thresholds(answerable, unanswerable, keep=0.9)
    assert three["kept_answerable"] >= 0.9 and three["turned_away"] == 1.0
    assert two["turned_away"] < three["turned_away"]
    assert "min_coverage" in three and "min_coverage" not in two


def test_out_of_sample_numbers_never_exceed_what_the_data_allows():
    answerable = [(0.7 + i / 100, 40.0 + i, 0.7) for i in range(20)]
    unanswerable = [(0.3 + i / 100, 10.0 + i, 0.1) for i in range(20)]
    o = out_of_sample(answerable, unanswerable, keep=0.95, repeats=4)
    assert o["splits"] == 4 and 0.95 <= o["kept_answerable"] <= 1.0 and o["turned_away"] == 1.0
