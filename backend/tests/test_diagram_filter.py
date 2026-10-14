from app.retrieval.chat_service import diagrams_for_cited_documents

CREST = {"document_id": "crest", "page_number": 3, "score": 0.65}
ALTIUS = {"document_id": "altius", "page_number": 4, "score": 0.66}


def test_only_images_of_cited_documents_are_kept():
    out = diagrams_for_cited_documents([ALTIUS, CREST], [{"document_id": "crest", "page": 3}])
    assert out == [CREST]


def test_no_citations_leaves_the_list_alone():
    assert diagrams_for_cited_documents([ALTIUS, CREST], []) == [ALTIUS, CREST]
    assert diagrams_for_cited_documents([ALTIUS], [{"document_id": None}]) == [ALTIUS]


def test_cited_text_document_drops_unrelated_images():
    assert diagrams_for_cited_documents([ALTIUS], [{"document_id": "irs-pub"}]) == []
