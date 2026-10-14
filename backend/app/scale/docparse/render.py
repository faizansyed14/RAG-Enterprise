"""ParsedPage -> the text that gets chunked, embedded and cited."""

from __future__ import annotations

from .types import Block, PageResult

def _md_cell(text: str) -> str:
    return text.replace("|", "/").replace("\n", " ").strip()


def table_text(rows: list[list[str]], style: str = "labeled") -> str:
    """'labeled': every data row says what each number is ("Item: Widget B | Unit: box | Price: 99.00"), so any chunk
    of a long table still carries its column names. 'markdown': a pipe table."""
    rows = [r for r in rows if any(c.strip() for c in r)]
    if not rows:
        return ""
    if style == "markdown" or len(rows) == 1:
        lines = ["| " + " | ".join(_md_cell(c) for c in rows[0]) + " |"]
        lines.append("|" + "|".join(" --- " for _ in rows[0]) + "|")
        lines += ["| " + " | ".join(_md_cell(c) for c in r) + " |" for r in rows[1:]]
        return "\n".join(lines)
    header, out = rows[0], []
    out.append(" | ".join(h.strip() for h in header if h.strip()))
    for r in rows[1:]:
        cells = []
        for i, v in enumerate(r):
            v = v.strip()
            if not v:
                continue
            h = header[i].strip() if i < len(header) else ""
            cells.append(f"{h}: {v}" if h else v)
        if cells:
            out.append(" | ".join(cells))
    return "\n".join(out)


def block_text(block: Block, table_style: str = "labeled") -> str:
    if block.type == "heading":
        return "#" * max(1, min(block.level or 2, 4)) + " " + block.text
    if block.type == "table":
        return table_text(block.rows or [], table_style)
    if block.type == "figure":
        return f"[Figure] {block.text}".strip()
    return block.text


def page_text(page: PageResult, table_style: str = "labeled") -> str:
    return "\n\n".join(t for t in (block_text(b, table_style) for b in page.blocks) if t.strip())
