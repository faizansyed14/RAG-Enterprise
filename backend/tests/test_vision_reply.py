from app.ingestion.diagram_pipeline import parse_vision_reply


def test_plain_json():
    assert parse_vision_reply('{"ocr_text": "A", "caption": "c"}')["ocr_text"] == "A"


def test_fenced_json():
    out = parse_vision_reply('```json\n{"ocr_text": "FOUNDATION PLAN", "caption": "plan", "callouts": ["5/A-512"]}\n```')
    assert out["ocr_text"] == "FOUNDATION PLAN" and out["callouts"] == ["5/A-512"]


def test_sentence_before_json():
    assert parse_vision_reply('Here is the result: {"ocr_text": "X"} Hope it helps.')["ocr_text"] == "X"


def test_cut_off_reply_keeps_text_not_json_syntax():
    out = parse_vision_reply('```json\n{"ocr_text": "ROOF PLAN 4/12 SLOPE", "caption": "A roof plan", "description": "This sh')
    assert out["ocr_text"] == "ROOF PLAN 4/12 SLOPE" and out["caption"] == "A roof plan"
    assert "{" not in out["ocr_text"]


def test_not_json_at_all_is_kept_as_text():
    assert parse_vision_reply("just some words")["ocr_text"] == "just some words"


def test_render_page_png_steps_down_until_it_fits(monkeypatch):
    from app.ingestion import diagram_pipeline as dp

    monkeypatch.setattr(dp, "_MAX_IMAGE_BYTES", 1000)
    sizes = {200: 5000, 150: 3000, 120: 900, 96: 500, 72: 300}
    asked = []

    class Pixmap:
        def __init__(self, dpi):
            self.dpi = dpi

        def tobytes(self, fmt):
            asked.append(self.dpi)
            return b"x" * sizes[self.dpi]

    class Page:
        def get_pixmap(self, dpi):
            return Pixmap(dpi)

    assert len(dp._render_page_png(Page())) == 900 and asked == [200, 150, 120]


def test_page_text_includes_what_the_page_shows():
    from app.ingestion.diagram_pipeline import page_text_with_description

    out = page_text_with_description("MASTER PLAN; CANAL VIEWS", "Aerial master plan", "Villas around a central lagoon.")
    assert out.startswith("MASTER PLAN; CANAL VIEWS") and "What this page shows: Aerial master plan Villas around a central lagoon." in out
    assert page_text_with_description("only text", None, None) == "only text"


def test_diagram_images_are_off_by_default():
    from app.core.config import Settings

    assert Settings.model_fields["store_diagrams"].default is False
