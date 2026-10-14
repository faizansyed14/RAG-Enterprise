"""Near-duplicate families: copies of one document must not crowd other documents out of the candidate list, and nothing may be
dropped or promoted by this step."""

from app.scale import families, index as ix, search
from app.scale.settings import get_scale_settings

BASE = ("The supplier shall deliver the goods within thirty days of the purchase order and the buyer shall pay each invoice "
        "within forty five days of receipt, subject to the warranty terms in schedule two and the limits on liability in clause nine. ") * 2


def variant(i: int) -> str:
    return BASE.replace("thirty", f"thirty{'x' * (i % 3)}", 1) + f" Revision {i}."


OTHER = ("The cafeteria opens at seven and serves breakfast until ten, then lunch from noon with soup, salad and a hot dish "
         "that changes daily, and closes at three in the afternoon on weekdays only. ") * 2


def test_copies_beyond_the_cap_move_behind_other_documents_and_nothing_is_dropped():
    order = [f"copy{i}" for i in range(6)] + ["other1", "other2"]
    texts = {f"copy{i}": variant(i) for i in range(6)} | {"other1": OTHER, "other2": OTHER.replace("seven", "eight") + " Extra words here."}
    out = families.demote_copies(order, texts, cap=3, threshold=0.8)
    assert out == ["copy0", "copy1", "copy2", "other1", "other2", "copy3", "copy4", "copy5"]
    assert sorted(out) == sorted(order)


def test_documents_that_merely_share_a_sentence_are_not_a_family():
    footer = " This agreement is governed by the laws of the emirate and any dispute goes to the courts of Dubai."
    texts = {f"d{i}": " ".join(f"topic{i}word{j}" for j in range(60)) + footer for i in range(6)}
    order = list(texts)
    assert families.demote_copies(order, texts, cap=1, threshold=0.8) == order


def test_a_document_without_text_is_never_grouped_and_cap_zero_is_off():
    order = ["a", "b", "c", "d"]
    assert families.demote_copies(order, {}, cap=1, threshold=0.8) == order
    texts = {d: BASE for d in order}
    assert families.demote_copies(order, texts, cap=0, threshold=0.8) == order


def test_the_order_among_kept_and_among_demoted_documents_is_preserved():
    order = ["c0", "x", "c1", "c2", "y", "c3", "c4", "z"]
    texts = {d: variant(int(d[1:])) for d in order if d.startswith("c")} | {"x": OTHER, "y": OTHER + " second", "z": "short note"}
    out = families.demote_copies(order, texts, cap=2, threshold=0.8)
    assert out == ["c0", "x", "c1", "y", "z", "c2", "c3", "c4"]


def hit(document_id, text, score):
    return ix.ChunkHit(id=document_id, score=score, document_id=document_id, doc_id="r-" + document_id, name=document_id,
                       path=document_id, page=1, chunk=0, start=0, end=len(text), text=text, dense_score=score, sparse_score=score)


def test_candidates_put_a_different_document_ahead_of_the_fourth_copy():
    hits = [hit(f"copy{i}", variant(i), 1.0 - i * 0.01) for i in range(5)] + [hit("other", OTHER, 0.5)]
    names = [c.name for c in search.candidates_from_hits(hits)]
    assert names == ["copy0", "copy1", "copy2", "other", "copy3", "copy4"]
    cfg = get_scale_settings().model_copy(update={"family_cap": 0})
    assert [c.name for c in search.candidates_from_hits(hits, cfg)] == [h.name for h in hits]
