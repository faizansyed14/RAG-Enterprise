from app.ingestion.xer_ingest import extract_blocks

_SAMPLE = (
    "ERMHDR\t21.12\n"
    "%T\tPROJWBS\n"
    "%F\twbs_id\twbs_name\n"
    "%R\t1\tFoundation\n"
    "%R\t2\tRoof\n"
    "%T\tTASK\n"
    "%F\ttask_id\ttask_name\tdur\n"
    "%R\t100\tPour footings\t5\n"
    "%E\n"
)


def test_extract_blocks_one_table_per_section(tmp_path):
    path = str(tmp_path / "schedule.xer")
    with open(path, "w", encoding="utf-8") as f:
        f.write(_SAMPLE)

    blocks = extract_blocks(path)

    assert [b["type"] for b in blocks] == ["heading", "table", "heading", "table"]
    assert blocks[0]["text"] == "PROJWBS"
    assert blocks[1]["rows"] == [["wbs_id", "wbs_name"], ["1", "Foundation"], ["2", "Roof"]]
    assert blocks[2]["text"] == "TASK"
    assert blocks[3]["rows"] == [["task_id", "task_name", "dur"], ["100", "Pour footings", "5"]]


def test_extract_blocks_tolerates_missing_trailing_marker(tmp_path):
    path = str(tmp_path / "schedule.xer")
    with open(path, "w", encoding="utf-8") as f:
        f.write("%T\tTASK\n%F\ttask_id\n%R\t1\n")  # no trailing %E

    blocks = extract_blocks(path)
    assert blocks[1]["rows"] == [["task_id"], ["1"]]


def test_extract_blocks_pads_ragged_rows(tmp_path):
    path = str(tmp_path / "schedule.xer")
    with open(path, "w", encoding="utf-8") as f:
        f.write("%T\tTASK\n%F\ta\tb\tc\n%R\t1\t2\n%E\n")  # row shorter than fields

    blocks = extract_blocks(path)
    assert blocks[1]["rows"] == [["a", "b", "c"], ["1", "2", ""]]
