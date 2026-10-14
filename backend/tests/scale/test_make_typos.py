"""The typo generator is deterministic and only slips content words."""

from scripts.scale_make_typos import make_typos, slip


def test_slip_changes_a_word_but_keeps_first_and_last_letter():
    for word in ("kanban", "retention", "termination", "contract"):
        for seed in range(30):
            out = slip(word, seed)
            assert out != word and out[0] == word[0] and out[-1] == word[-1]
            assert abs(len(out) - len(word)) <= 1


def test_same_input_gives_same_output_and_identifiers_are_left_alone():
    gold = [{"id": "a", "q": "What is the termination notice period in this agreement", "style": "nl"},
            {"id": "b", "q": "CTR-2025-2186-TD", "style": "id"},
            {"id": "c", "q": "kanban dragstart drop dragend", "style": "kw"}]
    first, second = make_typos(gold), make_typos(gold)
    assert first == second
    assert [g["id"] for g in first] == ["a", "c"]
    assert all(g["q"] != g["orig_q"] and len(g["q"].split()) == len(g["orig_q"].split()) for g in first)
