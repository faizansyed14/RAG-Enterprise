"""docparse logic that needs no neural model: reading order, paragraph joining, table rebuilding, running headers and
footers, the page assembly (on fake OCR / layout / table engines) and the word-gap restoration (on drawn pixels).
The same pipeline run on the real models is in test_docparse_models.py."""

import numpy as np
import pytest

from app.scale.docparse import Block, Engines, Line, PageParser, PageResult, ParseOptions, Region, page_text, remove_furniture, table_text
from app.scale.docparse import paragraphs as par
from app.scale.docparse import tables as tbl
from app.scale.docparse import table_model as tm
from app.scale.docparse.order import reading_order
from app.scale.docparse.spaces import restore_spaces


def L(x0, y0, x1, y1, text, **kw):
    return Line(box=(x0, y0, x1, y1), text=text, **kw)


# ------------------------------------------------------------------------------------------------ reading order

def order_of(boxes):
    items = list(range(len(boxes)))
    return reading_order(items, lambda i: boxes[i])


def test_two_columns_are_read_one_after_the_other():
    boxes = [(50, 100, 280, 200), (50, 220, 280, 320), (320, 100, 550, 200), (320, 220, 550, 320)]
    assert order_of(boxes) == [0, 1, 2, 3]
    shuffled = [boxes[2], boxes[0], boxes[3], boxes[1]]
    assert [shuffled[i] for i in order_of(shuffled)] == boxes


def test_a_title_across_both_columns_comes_first_and_a_full_width_table_comes_last():
    boxes = [(50, 40, 550, 80), (50, 100, 280, 300), (320, 100, 550, 300), (50, 400, 550, 600)]
    assert order_of(boxes) == [0, 1, 2, 3]


def test_paragraph_gaps_that_line_up_across_columns_do_not_interleave_the_columns():
    # The regression: both columns end a paragraph at the same height, so a horizontal gap runs clean across the page.
    # Cutting at every gap reads para 1 of each column, then para 2 of each. Cutting at the widest gap does not.
    col1 = [(50, 100, 280, 200), (50, 220, 280, 320)]
    col2 = [(320, 100, 550, 200), (320, 220, 550, 320)]
    table = [(50, 700, 550, 800)]
    boxes = col1 + col2 + table
    assert order_of(boxes) == [0, 1, 2, 3, 4]


def test_a_single_column_is_top_to_bottom_and_ties_go_left_to_right():
    boxes = [(50, 300, 500, 340), (50, 100, 500, 140), (50, 200, 500, 240)]
    assert order_of(boxes) == [1, 2, 0]


def test_overlapping_boxes_do_not_crash_and_stay_deterministic():
    boxes = [(0, 0, 100, 100), (50, 50, 150, 150), (60, 10, 160, 90)]
    assert sorted(order_of(boxes)) == [0, 1, 2]


def test_three_columns():
    boxes = [(30, 100, 180, 400), (210, 100, 360, 400), (390, 100, 540, 400)]
    assert order_of(boxes) == [0, 1, 2]


# ------------------------------------------------------------------------------------------------ paragraphs

def lines_of(texts, x=50, width=400, top=100, step=24, h=18):
    return [L(x, top + i * step, x + width, top + i * step + h, t) for i, t in enumerate(texts)]


def test_consecutive_lines_become_one_paragraph_and_a_blank_line_splits_them():
    lines = lines_of(["The notice period for", "terminating the agreement", "is ninety days."]) + \
            lines_of(["Invoices are payable", "within thirty days."], top=300)
    groups = par.merge_lines(lines)
    assert [par.paragraph_text(g) for g in groups] == [
        "The notice period for terminating the agreement is ninety days.",
        "Invoices are payable within thirty days.",
    ]


def test_end_of_line_hyphenation_is_undone_but_a_real_hyphen_before_a_capital_is_not():
    assert par.join_text(["the termina-", "tion clause"]) == "the termination clause"
    assert par.join_text(["the Anglo-", "Saxon text"]) == "the Anglo- Saxon text"
    assert par.join_text(["at 5 -", "7 pm"]) == "at 5 - 7 pm"


def test_cjk_lines_join_without_a_space():
    assert par.join_text(["合同终止通知期为", "九十天"]) == "合同终止通知期为九十天"


def test_list_items_start_new_paragraphs():
    lines = lines_of(["Duties of the supplier:", "1. Deliver on time", "2. Invoice monthly", "- keep records"])
    assert [par.paragraph_text(g) for g in par.merge_lines(lines)] == [
        "Duties of the supplier:", "1. Deliver on time", "2. Invoice monthly", "- keep records",
    ]


def test_a_short_last_line_followed_by_a_capital_ends_the_paragraph():
    lines = [
        L(50, 100, 450, 118, "A long line that fills the whole measure of the column"),
        L(50, 124, 200, 142, "and ends here."),
        L(50, 148, 450, 166, "Next paragraph starts on the following line"),
    ]
    assert [par.paragraph_text(g) for g in par.merge_lines(lines)] == [
        "A long line that fills the whole measure of the column and ends here.",
        "Next paragraph starts on the following line",
    ]


# ------------------------------------------------------------------------------------------------ tables

def test_html_to_rows_reads_a_plain_table():
    html = "<table><tr><td>Item</td><td>Price</td></tr><tr><td>A</td><td>1.50</td></tr></table>"
    assert tbl.html_to_rows(html) == [["Item", "Price"], ["A", "1.50"]]


def test_colspan_and_rowspan_keep_the_text_once_and_the_grid_rectangular():
    html = ('<table><tr><td colspan="2">Total</td><td>9</td></tr>'
            '<tr><td rowspan="2">x</td><td>a</td><td>1</td></tr><tr><td>b</td><td>2</td></tr></table>')
    assert tbl.html_to_rows(html) == [["Total", "", "9"], ["x", "a", "1"], ["", "b", "2"]]


def test_empty_columns_and_rows_are_dropped_and_broken_html_does_not_raise():
    html = "<table><tr><td>A</td><td></td><td>B</td></tr><tr><td></td><td></td><td></td></tr></table>"
    assert tbl.html_to_rows(html) == [["A", "B"]]
    assert tbl.html_to_rows("<table><tr><td>unclosed") == [["unclosed"]] or tbl.html_to_rows("<table><tr><td>unclosed") == []
    assert tbl.html_to_rows("") == []


def test_recovered_share_counts_how_much_of_the_text_reached_a_cell():
    lines = [L(0, 0, 10, 10, "Widget A"), L(0, 0, 10, 10, "12.50")]
    assert tbl.recovered_share([["Widget A", "12.50"]], lines) == 1.0
    assert tbl.recovered_share([["Widget A", ""]], lines) == pytest.approx(0.5)      # widget a 12 50: two of four words
    assert tbl.recovered_share([], []) == 1.0


def test_rows_from_lines_rebuilds_a_grid_from_positions_only():
    lines = [
        L(10, 10, 60, 30, "Item"), L(200, 10, 250, 30, "Unit"), L(400, 10, 450, 30, "Price"),
        L(10, 50, 90, 70, "Widget A"), L(200, 52, 240, 70, "each"), L(400, 50, 450, 70, "12.50"),
        L(10, 90, 90, 110, "Widget B"), L(200, 90, 240, 110, "box"), L(400, 91, 450, 111, "99.00"),
    ]
    assert tbl.rows_from_lines(lines) == [["Item", "Unit", "Price"], ["Widget A", "each", "12.50"], ["Widget B", "box", "99.00"]]


def test_a_native_line_holding_several_cells_is_cut_at_the_wide_gaps():
    line = L(0, 0, 500, 20, "Widget A each 12.50", native=True,
             words=[(0, 40, "Widget"), (50, 60, "A"), (200, 240, "each"), (400, 450, "12.50")])
    cells = tbl.split_native_cells(line, 60)
    assert [c.text for c in cells] == ["Widget A", "each", "12.50"]
    assert tbl.split_native_cells(L(0, 0, 10, 10, "x"), 60)[0].text == "x"


def test_table_text_labels_every_row_with_its_column_names_or_prints_a_pipe_table():
    rows = [["Item", "Unit", "Price"], ["Widget B", "box", "99.00"], ["Cable", "", "1.75"]]
    assert table_text(rows) == "Item | Unit | Price\nItem: Widget B | Unit: box | Price: 99.00\nItem: Cable | Price: 1.75"
    md = table_text(rows, "markdown").splitlines()
    assert md[0] == "| Item | Unit | Price |" and md[1].startswith("|") and "Widget B" in md[2]
    assert table_text([]) == ""


# -------------------------------------------------------------------------- the table model's cell assignment

def test_each_line_goes_to_the_cell_that_holds_most_of_it():
    boxes = [(0, 0, 100, 40), (100, 0, 200, 40), (0, 40, 100, 80), (100, 40, 200, 80)]
    lines = [L(5, 5, 60, 30, "a"), L(110, 5, 160, 30, "b"), L(5, 45, 60, 70, "c"), L(95, 45, 190, 70, "d")]
    owned = tm.assign_lines_to_cells(boxes, lines)
    assert {k: [l.text for l in v] for k, v in owned.items()} == {0: ["a"], 1: ["b"], 2: ["c"], 3: ["d"]}


def test_fill_cells_puts_text_into_the_skeleton_and_escapes_it():
    tokens = ["<table>", "<thead>", "<tr>", "<td></td>", "<td", ' colspan="1"', ">", "</td>", "</tr>", "</thead>", "</table>"]
    boxes = [(0, 0, 100, 40), (100, 0, 200, 40)]
    html = tm.fill_cells(tokens, boxes, [L(5, 5, 60, 30, "A & B"), L(110, 5, 160, 30, "<c>")])
    assert html == '<table><tr><td>A &amp; B</td><td colspan="1">&lt;c&gt;</td></tr></table>'


def test_text_far_apart_on_one_row_inside_one_cell_means_the_model_merged_cells():
    far = [L(5, 5, 50, 25, "Item"), L(300, 5, 350, 25, "Unit")]
    near = [L(5, 5, 50, 25, "Widget"), L(56, 5, 90, 25, "A")]
    stacked = [L(5, 5, 50, 25, "two"), L(5, 30, 50, 50, "lines")]
    assert tm._torn_cell(far) is True
    assert tm._torn_cell(near) is False
    assert tm._torn_cell(stacked) is False


# ------------------------------------------------------------------------------------------ running headers

def page(n, blocks, h=1000):
    return PageResult(number=n, width=800, height=h, blocks=blocks)


def blk(text, y, type_="paragraph", source="text", x=100):
    return Block(type=type_, text=text, box=(x, y, x + 400, y + 20), source=source)


def test_text_repeated_in_the_margins_and_page_numbers_are_removed_body_text_is_kept():
    pages = [
        page(i, [blk("ACME Corp - Confidential", 20, source="header"), blk(f"Body of page {i} says something", 400),
                 blk(f"Page {i} of 3", 960, source="footer")])
        for i in (1, 2, 3)
    ]
    remove_furniture(pages)
    for p in pages:
        assert [b.text for b in p.blocks] == [f"Body of page {p.number} says something"]
        assert "ACME Corp - Confidential" in p.furniture


def test_a_heading_in_the_middle_of_the_page_is_never_treated_as_furniture():
    pages = [page(i, [blk("Chapter 1 Introduction", 450, "heading", source="header")]) for i in (1, 2, 3)]
    remove_furniture(pages)
    assert all(len(p.blocks) == 1 for p in pages)


def test_a_single_page_keeps_unlabelled_margin_text_but_drops_a_page_number():
    p = page(1, [blk("Draft for review", 20), blk("Real content here", 400), blk("- 1 -", 960)])
    remove_furniture([p])
    assert [b.text for b in p.blocks] == ["Draft for review", "Real content here"]


def test_text_labelled_header_by_the_layout_model_is_dropped_only_in_the_margin():
    p = page(1, [blk("Confidential", 20, source="header"), blk("A long sentence in the middle labelled header by mistake", 500, source="header")])
    remove_furniture([p])
    assert [b.text for b in p.blocks] == ["A long sentence in the middle labelled header by mistake"]


def test_the_first_lines_of_a_paragraph_at_the_top_of_a_page_are_kept_even_when_the_layout_model_calls_them_a_header():
    # measured on a synthetic scan: page 2 has no title, its first paragraph starts in the top margin, and the layout model
    # labelled those two lines "header". Dropping them lost 18 words of body text.
    first = "The notice period for terminating the supplier agreement is ninety days written notice. Either party may"
    pages = [page(1, [blk("Supplier Terms", 120, "heading"), blk("Body of the first page", 400)]),
             page(2, [blk(first, 60, source="header"), blk("Body of the second page", 400)])]
    remove_furniture(pages)
    assert first in [b.text for b in pages[1].blocks]


def test_a_unique_running_header_is_still_removed_by_its_label():
    pages = [page(1, [blk("ACME Corp - Confidential", 20, source="header"), blk("Body", 400)])]
    remove_furniture(pages)
    assert [b.text for b in pages[0].blocks] == ["Body"]


# --------------------------------------------------------------------------------- the page, on fake engines

class FakeOcr:
    name = "fake"

    def __init__(self, lines):
        self.lines, self.calls = lines, 0

    def __call__(self, image):
        self.calls += 1
        return [Line(box=l.box, text=l.text, score=l.score) for l in self.lines]


class FakeLayout:
    name = "fake"

    def __init__(self, regions, fail=False):
        self.regions, self.fail = regions, fail

    def __call__(self, image):
        if self.fail:
            raise RuntimeError("boom")
        return [Region(box=r.box, label=r.label, score=r.score) for r in self.regions]


class FakeTable:
    name = "fake"

    def __init__(self, html=None, fail=False):
        self.html, self.fail = html, fail

    def __call__(self, image, lines):
        if self.fail:
            raise RuntimeError("table model crashed")
        return self.html


IMG = np.full((1200, 900, 3), 255, dtype=np.uint8)

TWO_COLUMN_LINES = [
    L(100, 200, 400, 220, "Left column first paragraph"), L(100, 224, 400, 244, "continues on a second line."),
    L(100, 300, 400, 320, "Left column second paragraph."),
    L(500, 200, 800, 220, "Right column first paragraph"), L(500, 224, 800, 244, "continues here as well."),
]


def parser(lines, regions=(), table=None, opts=None, layout_fail=False):
    return PageParser(Engines(FakeOcr(lines), FakeLayout(list(regions), layout_fail), table or FakeTable()), opts or ParseOptions(ocr="always"))


def test_without_any_layout_regions_lines_still_come_out_as_paragraphs_in_column_order():
    result = parser(TWO_COLUMN_LINES).parse(IMG)
    assert [b.text for b in result.blocks] == [
        "Left column first paragraph continues on a second line.",
        "Left column second paragraph.",
        "Right column first paragraph continues here as well.",
    ]


def test_a_layout_failure_is_recorded_and_the_page_is_still_read():
    result = parser(TWO_COLUMN_LINES, layout_fail=True).parse(IMG, number=3)
    assert len(result.blocks) == 3 and "layout_failed:RuntimeError" in result.notes


def test_titles_become_headings_with_a_level_from_their_size():
    lines = [L(100, 100, 600, 160, "Supplier Contract Review"), L(100, 220, 600, 238, "Body text of the document sits here."),
             L(100, 244, 600, 262, "A second line of body text follows."), L(100, 268, 600, 286, "And a third line, so the body is the typical height.")]
    result = parser(lines, [Region(box=(90, 90, 610, 170), label="title", score=0.9)]).parse(IMG)
    assert result.blocks[0].type == "heading" and result.blocks[0].level == 1
    assert page_text(result).startswith("# Supplier Contract Review")


def test_a_table_region_goes_through_the_structure_model_and_becomes_rows():
    lines = [L(110, 410, 160, 430, "Item"), L(410, 410, 460, 430, "Price"), L(110, 450, 200, 470, "Widget"), L(410, 450, 460, 470, "9.50")]
    html = "<table><tr><td>Item</td><td>Price</td></tr><tr><td>Widget</td><td>9.50</td></tr></table>"
    result = parser(lines, [Region(box=(100, 400, 500, 480), label="table", score=0.9)], FakeTable(html)).parse(IMG)
    (table,) = result.blocks
    assert table.type == "table" and table.rows == [["Item", "Price"], ["Widget", "9.50"]] and table.html == html


def test_when_the_structure_model_loses_text_the_geometric_grid_is_used_instead():
    lines = [L(110, 410, 160, 430, "Item"), L(410, 410, 460, 430, "Price"), L(110, 450, 200, 470, "Widget"), L(410, 450, 460, 470, "9.50")]
    lossy = "<table><tr><td>Item</td><td></td></tr></table>"
    result = parser(lines, [Region(box=(100, 400, 500, 480), label="table", score=0.9)], FakeTable(lossy)).parse(IMG)
    assert result.blocks[0].rows == [["Item", "Price"], ["Widget", "9.50"]] and result.blocks[0].html == ""


def test_a_crashing_table_model_falls_back_and_a_one_column_table_is_read_as_text():
    lines = [L(110, 410, 300, 430, "only one column"), L(110, 434, 300, 454, "of plain text")]
    result = parser(lines, [Region(box=(100, 400, 500, 480), label="table", score=0.9)], FakeTable(fail=True)).parse(IMG)
    assert [b.type for b in result.blocks] == ["paragraph"]


def test_figures_keep_their_text_and_captions_stay_next_to_them_a_picture_without_text_is_dropped():
    lines = [L(120, 520, 300, 536, "Q1 Q2 Q3 Q4"), L(120, 700, 400, 718, "Figure 1: Quarterly volume")]
    regions = [Region(box=(100, 500, 500, 690), label="figure", score=0.9), Region(box=(100, 695, 500, 725), label="figure_caption", score=0.7)]
    result = parser(lines, regions).parse(IMG)
    assert [(b.type, b.text) for b in result.blocks] == [("figure", "Q1 Q2 Q3 Q4"), ("caption", "Figure 1: Quarterly volume")]
    empty = parser([L(100, 800, 400, 818, "just text")], [Region(box=(100, 300, 500, 600), label="figure", score=0.9)]).parse(IMG)
    assert [b.type for b in empty.blocks] == ["paragraph"]


def test_a_caption_is_recognised_by_its_wording_whatever_the_layout_model_called_it():
    result = parser([L(100, 300, 400, 318, "Table 2: Price list")], [Region(box=(90, 290, 410, 325), label="reference", score=0.6)]).parse(IMG)
    assert result.blocks[0].type == "caption"


def test_overlapping_duplicate_regions_keep_the_more_confident_label():
    from app.scale.docparse.page import dedupe_regions

    kept = dedupe_regions([Region((0, 0, 100, 20), "reference", 0.64), Region((2, 0, 100, 20), "figure_caption", 0.58), Region((0, 100, 50, 120), "text", 0.9)])
    assert sorted(r.label for r in kept) == ["reference", "text"]


def test_ocr_runs_only_when_the_pdf_has_no_text_layer_in_auto_mode():
    native = [Line(box=(100, 100, 400, 120), text="This page has a real text layer of enough words to trust.", native=True)]
    ocr = FakeOcr([L(100, 100, 400, 120, "from ocr")])
    p = PageParser(Engines(ocr, FakeLayout([]), FakeTable()), ParseOptions(ocr="auto"))
    result = p.parse(IMG, native=native)
    assert ocr.calls == 0 and not result.used_ocr and result.blocks[0].text.startswith("This page has")
    result = p.parse(IMG, native=[Line(box=(1, 1, 5, 5), text="x", native=True)])      # too little text: treated as a scan
    assert ocr.calls == 1 and result.used_ocr and result.blocks[0].text == "from ocr"
    p_never = PageParser(Engines(ocr, FakeLayout([]), FakeTable()), ParseOptions(ocr="never"))
    assert p_never.parse(IMG, native=[]).blocks == [] and ocr.calls == 1


# ------------------------------------------------------------------------------- word gaps, on drawn pixels

def drawn_line(words, letter=8, gap_in=2, gap_word=9, height=24):
    """A white image with black 'letters' (solid rectangles), returning the image, the text without spaces, and the
    per-character x ranges -- what the recogniser reports for a line whose spaces it failed to emit."""
    img = np.full((height + 20, 600, 3), 255, dtype=np.uint8)
    x, chars = 10, []
    for w, word in enumerate(words):
        for ch in word:
            img[10: 10 + height, x: x + letter] = 0
            chars.append((float(x), float(x + letter), ch))
            x += letter + gap_in
        x += gap_word - gap_in
    text = "".join(c[2] for c in chars)
    box = (10.0, 10.0, float(x), 10.0 + height)
    return img, Line(box=box, text=text, chars=chars)


def test_a_word_gap_the_recogniser_missed_is_put_back():
    img, line = drawn_line(["ACME", "Corp", "Report"])
    assert line.text == "ACMECorpReport"
    assert restore_spaces(img, line) == "ACME Corp Report"


def test_letter_gaps_inside_a_word_and_digits_are_left_alone():
    img, line = drawn_line(["Invoice", "12.50"], gap_in=3)
    assert restore_spaces(img, line) == "Invoice 12.50"
    img, line = drawn_line(["12", "50"], gap_word=14)
    assert restore_spaces(img, line) == "1250"                      # digits are never split by a gap


def test_a_line_that_already_has_its_spaces_or_lacks_positions_is_returned_unchanged():
    img, line = drawn_line(["no", "gaps"])
    with_space = Line(box=line.box, text="no gaps", chars=line.chars[:2] + [(0.0, 0.0, " ")] + line.chars[2:])
    assert restore_spaces(img, with_space) == "no gaps"
    assert restore_spaces(img, Line(box=line.box, text="nogaps")) == "nogaps"
    assert restore_spaces(img, Line(box=(0, 0, 4, 4), text="abc", chars=[(0, 1, "a"), (1, 2, "b"), (2, 3, "c")])) == "abc"
