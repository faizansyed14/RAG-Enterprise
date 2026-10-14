import pytest

from app.scale import index as ix
from app.scale.chunking import chunk_document
from app.scale.models import HashingEmbedder

EMB = HashingEmbedder(128)


def put(doc, name, text, acl, source="s"):
    chunks = chunk_document([text], title=name)
    ix.upsert_chunks([
        ix.IndexedChunk(document_id=doc, doc_id="doc-" + doc, name=name, path=name, source=source, page=c.page,
                        chunk=c.index, start=c.start, end=c.end, acl=acl, dense=EMB.embed([c.embed_text])[0],
                        sparse_text=c.embed_text, text=c.text)
        for c in chunks
    ])


def find(query, principals, **kw):
    return ix.search(EMB.embed([query])[0], query, principals=principals, **kw)


@pytest.fixture(autouse=True)
def corpus():
    put("d-legal", "Contract A.pdf", "Termination for convenience needs 30 days notice. Ref SUB-084 applies.", ["group:legal"])
    put("d-fin", "Contract B.pdf", "Termination for convenience needs 90 days notice. Ref SUB-999 applies.", ["group:finance"])
    put("d-all", "Handbook.pdf", "Vacation policy: two days per month accrue.", ["*"])


def names(hits):
    return {h.name for h in hits}


def test_filter_applies_inside_the_query_for_every_principal_set():
    assert names(find("termination notice", {"group:legal"})) == {"Contract A.pdf"}
    assert names(find("termination notice", {"group:finance"})) == {"Contract B.pdf"}
    assert names(find("termination notice", {"group:legal", "group:finance", "*"})) == {
        "Contract A.pdf", "Contract B.pdf", "Handbook.pdf"}
    assert find("termination notice", {"group:nobody"}) == []
    assert find("termination notice", set()) == []


def test_empty_acl_is_fail_closed():
    put("d-open", "Orphan.pdf", "orphan termination notice text", [])
    assert "Orphan.pdf" not in names(find("orphan termination", {"*", "group:legal", "group:finance"}))


def test_permission_change_without_reembedding():
    ix.set_document_acl("d-legal", ["group:finance"])
    assert find("SUB-084", {"group:legal"}) == []
    assert "Contract A.pdf" in names(find("SUB-084", {"group:finance"}))


def test_exact_identifier_is_found_through_the_sparse_leg():
    top = find("SUB-084", {"group:legal", "group:finance"})[0]
    assert top.name == "Contract A.pdf"


def test_unfiltered_search_is_refused_unless_explicit():
    with pytest.raises(ValueError):
        ix.search(EMB.embed(["x"])[0], "x", principals=None)
    assert find("termination", None, allow_unfiltered=True)


def test_authorized_doc_ids_scope():
    hits = ix.search(EMB.embed(["termination"])[0], "termination", principals=None, authorized_doc_ids=["doc-d-fin"])
    assert names(hits) == {"Contract B.pdf"}
    assert ix.search(EMB.embed(["termination"])[0], "termination", principals=None, authorized_doc_ids=[]) == []


def test_delete_document_chunks():
    ix.delete_document_chunks("d-legal")
    assert find("SUB-084", {"group:legal"}) == []


def test_tokenizer_keeps_ids_and_their_parts_and_arabic():
    toks = ix.tokenize("Ref SUB-084 and الفيلا B27")
    assert "sub-084" in toks and "sub" in toks and "084" in toks
    assert "فيلا" in toks and "b27" in toks            # the definite article is stripped by the default stemmer


def test_identifier_tokens():
    assert "sub-084" in ix.identifier_tokens("what does SUB-084 say")
    assert "b27" in ix.identifier_tokens("see drawing B27 on level 3")
    assert "2023-0412" in ix.identifier_tokens("invoice 2023-0412 was paid")
    assert ix.identifier_tokens("30 days notice, FY and Q") == []
    assert ix.identifier_tokens("termination for convenience") == []


def test_an_identifier_outranks_decoys_that_the_other_list_likes():
    """Fusion by rank alone lets a chunk that is mid-ranked in both lists beat the chunk holding the exact
    identifier. The dense side is noise here (the identifier is a sliver of a long text), as it is for real
    embeddings on a long chunk with one reference number in it."""
    import random

    rng = random.Random(3)
    words = ["pump", "valve", "flange", "gasket", "torque", "boiler", "duct", "relay", "panel", "cable", "sensor", "tank"]

    def body(ref):
        filler = " ".join(rng.choice(words) + str(rng.randint(0, 9)) for _ in range(150))
        return f"{filler} Reference {ref} applies to this record. {filler[:200]}"

    for i in range(150):
        put(f"decoy-{i}", f"decoy{i}.txt", body(f"REF-{i:07d}"), ["*"])
    put("target", "target.txt", body("REF-0004242"), ["*"])
    assert find("what does REF-0004242 say about this record", {"*"})[0].name == "target.txt"


def test_many_workers_creating_the_collection_at_once_all_succeed(monkeypatch):
    """The first batch of a fresh install is indexed by several workers at the same instant."""
    import uuid
    from concurrent.futures import ThreadPoolExecutor

    from app.scale.settings import get_scale_settings

    monkeypatch.setenv("SCALE_EMBED_MODEL", f"race-{uuid.uuid4().hex[:8]}")
    get_scale_settings.cache_clear()
    ix.reset_client_cache()
    with ThreadPoolExecutor(max_workers=8) as pool:
        names = list(pool.map(lambda _: ix.ensure_collection(128), range(16)))
    assert len(set(names)) == 1 and ix.get_client().collection_exists(names[0])


def test_the_index_recovers_when_the_collection_is_removed_underneath_it():
    ix.get_client().delete_collection(ix.collection_name())
    put("d-new", "New.pdf", "fresh text after the collection was dropped", ["*"])
    assert "New.pdf" in names(find("fresh text", {"*"}))


# ---- exact search when the permission filter lets few chunks through (index._dense_search_params) ----

def _filter_for(principals):
    from qdrant_client import models as qm

    return qm.Filter(must=[qm.FieldCondition(key="acl", match=qm.MatchAny(any=sorted(principals)))])


def test_small_visible_sets_are_searched_exactly_and_large_ones_use_the_graph(monkeypatch):
    from app.scale.settings import get_scale_settings

    cfg = get_scale_settings()
    client, name = ix.get_client(cfg), ix.collection_name(cfg)
    monkeypatch.setenv("SCALE_EXACT_BELOW", "2")
    get_scale_settings.cache_clear()
    cfg = get_scale_settings()
    ix._COUNTS.clear()
    assert ix._dense_search_params(client, name, _filter_for({"group:legal"}), cfg).exact is True        # 1 chunk visible
    assert ix._dense_search_params(client, name, _filter_for({"group:legal", "group:finance", "*"}), cfg) is None   # 3 chunks visible
    monkeypatch.setenv("SCALE_EXACT_BELOW", "0")
    get_scale_settings.cache_clear()
    assert ix._dense_search_params(client, name, _filter_for({"group:legal"}), get_scale_settings()) is None  # switched off


def test_exact_search_never_widens_access(monkeypatch):
    monkeypatch.setenv("SCALE_EXACT_BELOW", "1000000")
    from app.scale.settings import get_scale_settings

    get_scale_settings.cache_clear()
    ix._COUNTS.clear()
    assert names(find("termination notice", {"group:legal"})) == {"Contract A.pdf"}
    assert find("termination notice", {"group:nobody"}) == []
    assert names(find("termination notice", {"group:legal", "group:finance"})) == {"Contract A.pdf", "Contract B.pdf"}


def test_a_failing_count_falls_back_to_the_engines_own_plan():
    class Broken:
        def count(self, *a, **k):
            raise RuntimeError("boom")

    ix._COUNTS.clear()
    assert ix._dense_search_params(Broken(), "c", _filter_for({"x"}), __import__("app.scale.settings", fromlist=["x"]).get_scale_settings()) is None


def test_int8_collections_search_with_oversampling_and_rescoring(monkeypatch):
    from app.scale.settings import get_scale_settings

    monkeypatch.setenv("SCALE_QUANTIZE_INT8", "true")
    monkeypatch.setenv("SCALE_QUANTIZE_OVERSAMPLING", "3")
    monkeypatch.setenv("SCALE_EXACT_BELOW", "0")
    get_scale_settings.cache_clear()
    params = ix._dense_search_params(None, "x", ix.qm.Filter(), get_scale_settings())
    assert params.quantization.rescore is True and params.quantization.oversampling == 3.0 and not params.exact
    monkeypatch.setenv("SCALE_EXACT_BELOW", "1000000")
    get_scale_settings.cache_clear()
    ix._COUNTS.clear()
    assert names(find("termination notice", {"group:legal"})) == {"Contract A.pdf"}      # small scope: exact; still correct


def test_plain_collections_send_no_quantization_parameters():
    from app.scale.settings import get_scale_settings

    cfg = get_scale_settings()
    assert cfg.quantize_int8 is False
    cfg.exact_below = 0
    assert ix._dense_search_params(None, "x", ix.qm.Filter(), cfg) is None
