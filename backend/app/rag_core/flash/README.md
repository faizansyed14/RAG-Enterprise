# Flash tree builder

Builds the section tree of a PDF from layout statistics, without a language model. Summaries and the optional
refinement pass for retrieval do use one.

## Usage

```python
from app.rag_core.flash import tree_flash

tree = tree_flash("paper.pdf")                                 # optimized tree + summaries
tree = tree_flash("paper.pdf", summary=False, optimize=False)  # raw tree only, no LLM
tree = tree_flash("paper.pdf", optimize="merge")               # deterministic merge, no LLM expand
```

Takes a file path or an `io.BytesIO` stream and returns the tree as a dict. Summaries are on by default and need a
configured model (see `docs/MODELS.md`).

## Output

```python
{
    "doc_name": str,
    "doc_title": str,
    "structure": [
        {
            "title": str,
            "node_id": str,       # 4-digit, zero-padded
            "start_index": int,   # 1-based, inclusive
            "end_index": int,
            "summary": str,       # summary=True only
            "key_items": [str],   # optimize only: titles of subsections merged away
            "nodes": [...],       # entries with children only
        }
    ],
    "toc_source": str,  # "detected" | "bookmarks" | "hybrid" | "pages" | "unreadable"
}
```

`toc_source` says where the structure came from: `"detected"` from the layout, `"bookmarks"` from the embedded
outline, `"hybrid"` when bookmarks frame the detected sections. `"pages"` means the layout yielded no hierarchy, so
every page became one node titled `Page N`; past `FLAT_TREE_MAX_NODES` (10) pages that flat tree comes back without
summaries or optimization, and the local client refuses it. `"unreadable"` means no page carries text and
`structure` is empty.

Every page is in some node: a hierarchy that starts after page 1 is preceded by a `Preface` node covering the pages
before it, as in standard mode.
