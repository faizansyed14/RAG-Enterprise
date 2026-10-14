from app.scale.chunking import chunk_document, chunk_page
from app.scale.settings import ScaleSettings


def cfg(**kw):
    return ScaleSettings(chunk_words=40, chunk_overlap_words=8, min_chunk_words=6, **kw)


def words(n, prefix="w"):
    return " ".join(f"{prefix}{i}" for i in range(n))


def test_chunk_text_is_exact_slice_of_page():
    page = "Intro paragraph here.\n\n" + words(120) + "\n\nClosing words."
    chunks = chunk_page(page, 3, cfg=cfg())
    assert len(chunks) > 1
    for c in chunks:
        assert page[c.start:c.end] == c.text
        assert c.page == 3


def test_chunks_never_cross_pages_and_indexes_run_on():
    chunks = chunk_document([words(100, "a"), words(100, "b")], title="Doc", cfg=cfg())
    assert [c.index for c in chunks] == list(range(len(chunks)))
    for c in chunks:
        prefix = "a" if c.page == 1 else "b"
        assert all(w.startswith(prefix) for w in c.text.split())


def test_overlap_repeats_words_between_neighbours():
    chunks = chunk_page(words(100), 1, cfg=cfg())
    first, second = chunks[0].text.split(), chunks[1].text.split()
    assert set(first[-8:]) & set(second[:10])


def test_prefers_paragraph_boundary():
    page = words(30, "p") + "\n\n" + words(30, "q")
    chunks = chunk_page(page, 1, cfg=cfg())
    assert chunks[0].text.split()[-1] == "p29"          # cut at the blank line, not mid-paragraph


def test_short_tail_is_folded_into_previous_chunk():
    chunks = chunk_page(words(43), 1, cfg=cfg())
    assert all(len(c.text.split()) >= 6 for c in chunks)


def test_heading_trail_is_carried_across_pages_and_into_embed_text():
    pages = ["# Payment terms\n\n" + words(20), words(20, "x")]
    chunks = chunk_document(pages, title="Master Agreement.pdf", path="Legal/2023/Master Agreement.pdf", cfg=cfg())
    assert chunks[0].heading == "Payment terms"
    assert chunks[-1].heading == "Payment terms"          # carried to page 2
    assert "Master Agreement |" in chunks[-1].embed_text          # the name without extension or de-dup suffix
    assert ".pdf" not in chunks[-1].context
    assert "Legal/2023" in chunks[-1].embed_text
    assert chunks[-1].text == chunks[-1].text.strip()      # stored text carries no context prefix


def test_empty_and_whitespace_pages_yield_nothing():
    assert chunk_document(["", "   \n\n  "], cfg=cfg()) == []


def test_context_uses_the_folder_and_own_title_when_the_file_name_says_nothing():
    pages = ["---\ntitle: Fetch API\nslug: Web/API/Fetch_API\n---\n\nThe Fetch API provides an interface for fetching resources. " + "word " * 30]
    chunks = chunk_document(pages, title="index [2a3f29d1].md", path="mdn/web/api/fetch_api/index.md", cfg=cfg())
    assert chunks[0].context.startswith("Fetch API | ")           # 'index' and the de-dup suffix are not a name
    assert "web/api/fetch_api" in chunks[0].context
    assert "2a3f29d1" not in chunks[0].embed_text


def test_the_documents_own_title_is_found_in_front_matter_a_heading_or_the_first_line():
    from app.scale.chunking import extract_title

    assert extract_title("---\ntitle: \"Pods\"\n---\nBody text here.") == "Pods"
    assert extract_title("# Quarterly report\n\nText") == "Quarterly report"
    assert extract_title("VAN OMMEREN ACQUIRES DUTCH GAS CONTAINER UNIT\n  Dutch shipping group said") == "VAN OMMEREN ACQUIRES DUTCH GAS CONTAINER UNIT"
    assert extract_title("This is a long sentence of prose that ends with a full stop and is clearly body text.") == ""
