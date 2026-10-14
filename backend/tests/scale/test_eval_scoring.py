from scripts.scale_eval import score_case


def test_recall_mrr_and_forbidden_are_computed_per_case():
    ranked = ["Contract A.pdf | legal/a.pdf", "Budget.xlsx | finance/b.xlsx", "Handbook.pdf | hr/h.pdf"]
    got = score_case(ranked, ["handbook", "contract a", "missing doc"], ["budget"], [1, 3])
    assert got["recall@1"] == 1 / 3 and got["recall@3"] == 2 / 3
    assert got["rr"] == 1.0
    assert got["forbidden"] == 1


def test_a_case_with_nothing_found_scores_zero_not_an_error():
    got = score_case(["x | y"], ["absent"], [], [5])
    assert got["recall@5"] == 0.0 and got["rr"] == 0.0 and got["forbidden"] == 0
