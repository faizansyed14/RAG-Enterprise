from openpyxl import Workbook

from app.ingestion.xlsx_ingest import extract_blocks


def _make_xlsx(tmp_path):
    wb = Workbook()
    ws1 = wb.active
    ws1.title = "Drawings"
    ws1.append(["Drawing No", "Revision"])
    ws1.append(["A-108", "A"])
    ws1.append(["A-109", 2])

    ws2 = wb.create_sheet("Empty")  # should be skipped -- no data

    path = str(tmp_path / "test.xlsx")
    wb.save(path)
    return path


def test_extract_blocks_skips_empty_sheets_and_formats_ints(tmp_path):
    path = _make_xlsx(tmp_path)
    blocks = extract_blocks(path)

    assert [b["type"] for b in blocks] == ["heading", "table"]
    assert blocks[0]["text"] == "Drawings"
    assert blocks[1]["rows"] == [["Drawing No", "Revision"], ["A-108", "A"], ["A-109", "2"]]
