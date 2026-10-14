"""Measure docparse on pages whose contents are known.

  python -m scripts.scale_docparse_eval layout  --share /data/share --manifest m.jsonl [--docs 12] [--out r.json]
  python -m scripts.scale_docparse_eval ocr     --share ... --manifest ... [--docs 12] [--levels clean,scan,poor]
  python -m scripts.scale_docparse_eval retrieval --share ... --manifest ... --golden golden.json [--docs 120]

layout     PDFs WITH a text layer (two columns, running header / footer, table, figure). Compares docparse with the
           plain text extraction the pipeline used before (PyPDF2): reading order, header / footer leakage, table cells.
ocr        the same pages turned into image-only "scans" at three quality levels: word / character error rate of the
           recognised text, reading order, table cells, seconds per page; with and without word-gap restoration.
retrieval  real documents of your corpus drawn as pages, scanned, parsed; then the same questions are asked of an
           index holding the original text and of an index where those documents hold their OCR text.

Every number is from running the models; nothing is estimated. Pages come from synth.py, so the answer key is exact.
"""

from __future__ import annotations

import argparse
import json
import random
import re
import statistics
import sys
import time
from pathlib import Path

VOCAB_ITEMS = ["Widget A", "Widget B", "Cable", "Bracket", "Gasket", "Sensor", "Valve", "Filter", "Adapter", "Bearing",
               "Coupler", "Relay", "Switch", "Fuse", "Hinge", "Washer", "Bolt", "Nozzle"]
UNITS = ["each", "box", "metre", "kg", "pack", "roll"]


# ------------------------------------------------------------------------------------------------ source text

def prose(text: str) -> list[str]:
    """Paragraphs of plain prose from markdown / text: no code, tables, front matter, links or lists."""
    text = re.sub(r"^---\n.*?\n---\n", "", text, flags=re.S)
    text = re.sub(r"```.*?```", "", text, flags=re.S)
    out = []
    for block in re.split(r"\n\s*\n", text):
        block = " ".join(block.split())
        block = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", block)
        block = re.sub(r"[`*_#>|{}\[\]<]", "", block)
        words = block.split()
        if 25 <= len(words) <= 120 and re.fullmatch(r"[A-Za-z0-9 ,.;:'\"()%/\-?!]+", block) and block[0].isupper():
            out.append(block)
    return out


def load_docs(share: Path, manifest: Path, prefixes: tuple[str, ...], limit: int, seed: int) -> list[tuple[str, list[str]]]:
    rng = random.Random(seed)
    paths = [json.loads(line)["path"] for line in manifest.read_text().splitlines() if line.strip()]
    paths = [p for p in paths if p.startswith(prefixes)]
    rng.shuffle(paths)
    out = []
    for p in paths:
        try:
            pars = prose((share / p).read_text("utf-8", errors="replace"))
        except OSError:
            continue
        if len(pars) >= 6:
            out.append((p, pars[:10]))
        if len(out) >= limit:
            break
    return out


def make_specs(name: str, pars: list[str], rng: random.Random, interleave: bool = False):
    from app.scale.docparse.synth import PageSpec

    title = Path(name).stem.replace("_", " ").replace("-", " ").title()[:60] or "Report"
    rows = [["Item", "Unit", "Qty", "Price"]]
    for _ in range(rng.randint(3, 5)):
        rows.append([rng.choice(VOCAB_ITEMS), rng.choice(UNITS), str(rng.randint(2, 480)), f"{rng.randint(1, 900)}.{rng.randint(0, 99):02d}"])
    half = len(pars) // 2
    p1 = PageSpec(
        title=title, columns=[pars[:3], pars[3:half + 1][:3]], table=rows, table_caption="Table 1: Price list",
        header_left="ACME Corp - Confidential", header_right="Annual Report 2025", footer="Page {n} of {total}",
        interleave=interleave,
    )
    p2 = PageSpec(
        columns=[pars[half + 1: half + 4]], figure_caption="Figure 1: Quarterly volume",
        header_left="ACME Corp - Confidential", header_right="Annual Report 2025", footer="Page {n} of {total}",
        interleave=interleave,
    )
    return [p1, p2]


# ------------------------------------------------------------------------------------------------ metrics

def naive_pages(pdf_bytes: bytes) -> list[str]:
    import io

    import PyPDF2

    return [p.extract_text() or "" for p in PyPDF2.PdfReader(io.BytesIO(pdf_bytes)).pages]


def evaluate_pages(texts: list[str], parsed_tables: list[list[list[str]] | None], truth, body_texts: list[str] | None = None) -> dict:
    """texts: the whole page text (furniture is looked for here); body_texts: the same without tables, for error rates
    (a table is judged by its cells, not by words, and the labelled-row style repeats the column names on purpose)."""
    from app.scale import docparse  # noqa: F401
    from app.scale.docparse import synth as S

    wer, cer, order, leak, cells = [], [], [], [], []
    for i, (text, table, t) in enumerate(zip(texts, parsed_tables, truth)):
        body = " ".join(([t.title] if t.title else []) + t.paragraphs + t.captions)
        got = body_texts[i] if body_texts is not None else text
        if body_texts is None and t.table:                  # raw extraction: its table text is part of `text`; compare to it too
            body += " " + " ".join(c for row in t.table for c in row)
        wer.append(S.word_error_rate(body, got))
        cer.append(S.char_error_rate(body, got))
        order.append(S.order_accuracy(([t.title] if t.title else []) + t.paragraphs, text))
        leak.append(S.furniture_leak(t.furniture, text))
        if t.table:
            cells.append(S.table_cell_accuracy(t.table, table))
    mean = lambda xs: round(statistics.mean(xs), 4) if xs else None      # noqa: E731
    return {"wer": mean(wer), "cer": mean(cer), "reading_order": mean(order), "furniture_leak": mean(leak), "table_cells": mean(cells)}


def body_text(page) -> str:
    return "\n".join(b.text for b in page.blocks if b.type != "table" and b.text)


def parsed_table(page) -> list[list[str]] | None:
    for b in page.blocks:
        if b.type == "table":
            return b.rows
    return None


# ------------------------------------------------------------------------------------------------ commands

def cmd_layout(args) -> dict:
    from app.scale.docparse import ParseOptions, parse_pdf
    from app.scale.docparse.synth import build_pdf
    import tempfile

    share, manifest = Path(args.share), Path(args.manifest)
    docs = load_docs(share, manifest, tuple(args.prefix.split(",")), args.docs, args.seed)
    rng = random.Random(args.seed)
    agg = {"naive": [], "docparse": []}
    for name, pars in docs:
        data, truth = build_pdf(make_specs(name, pars, rng, interleave=args.interleave))
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as fh:
            fh.write(data)
        naive = naive_pages(data)
        agg["naive"].append(evaluate_pages(naive, [None] * len(naive), truth))
        doc = parse_pdf(fh.name, ParseOptions(ocr="auto"))
        texts = doc.texts()
        agg["docparse"].append(evaluate_pages(texts, [parsed_table(p) for p in doc.pages], truth, [body_text(p) for p in doc.pages]))
        print(f"  {name}: naive order={agg['naive'][-1]['reading_order']} leak={agg['naive'][-1]['furniture_leak']} | "
              f"docparse order={agg['docparse'][-1]['reading_order']} leak={agg['docparse'][-1]['furniture_leak']} cells={agg['docparse'][-1]['table_cells']}", flush=True)
    return {k: {m: round(statistics.mean([r[m] for r in v if r[m] is not None]), 4) if any(r[m] is not None for r in v) else None for m in v[0]} for k, v in agg.items()}


def cmd_ocr(args) -> dict:
    import tempfile

    from app.scale.docparse import ParseOptions, parse_pdf
    from app.scale.docparse.synth import build_pdf, scan_pdf

    share, manifest = Path(args.share), Path(args.manifest)
    docs = load_docs(share, manifest, tuple(args.prefix.split(",")), args.docs, args.seed)
    rng = random.Random(args.seed)
    built = []
    for name, pars in docs:
        built.append(build_pdf(make_specs(name, pars, rng, interleave=args.interleave)))
    report: dict = {}
    for level in args.levels.split(","):
        for restore in ((True,) if args.restore_only else (False, True)):
            rows, secs, pages_n = [], 0.0, 0
            for k, (data, truth) in enumerate(built):
                pdf = data if level == "native" else scan_pdf(data, level, seed=args.seed + k)
                with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as fh:
                    fh.write(pdf)
                t0 = time.perf_counter()
                doc = parse_pdf(fh.name, ParseOptions(ocr="always" if level != "native" else "auto", restore_spaces=restore, dpi=args.dpi, max_side=args.max_side))
                secs += time.perf_counter() - t0
                pages_n += len(doc.pages)
                rows.append(evaluate_pages(doc.texts(), [parsed_table(p) for p in doc.pages], truth, [body_text(p) for p in doc.pages]))
            summary = {m: round(statistics.mean([r[m] for r in rows if r[m] is not None]), 4) for m in rows[0] if any(r[m] is not None for r in rows)}
            summary["seconds_per_page"] = round(secs / max(pages_n, 1), 2)
            report[f"{level} / {'spaces restored' if restore else 'raw OCR'}"] = summary
            print(f"[{level:6} {'restored' if restore else 'raw     '}] " + "  ".join(f"{k}={v}" for k, v in summary.items()), flush=True)
    return report


def text_page_pdf(text: str) -> bytes:
    """A document's text typeset as an ordinary page (title line bold), the way a printed invoice or letter looks."""
    import io

    from reportlab.lib.pagesizes import A4
    from reportlab.lib.utils import simpleSplit
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    W, H = A4
    y = H - 70
    first = True
    for raw in text.splitlines():
        font, size = ("Helvetica-Bold", 15) if first else ("Helvetica", 11)
        first = False
        if not raw.strip():
            y -= 10
            continue
        for ln in simpleSplit(raw, font, size, W - 120):
            if y < 60:
                c.showPage()
                y = H - 70
            c.setFont(font, size)
            c.drawString(60, y, ln)
            y -= size * 1.45
    c.save()
    return buf.getvalue()


def cmd_retrieval(args) -> dict:
    """Same questions, same index, but N documents hold their OCR text (at each scan quality) instead of the original."""
    import tempfile

    from app.scale import index as ix
    from app.scale.docparse import ParseOptions, parse_pdf
    from app.scale.docparse.synth import scan_pdf
    from app.scale.models import get_embedder
    from app.scale.settings import get_scale_settings
    from scripts import scale_realeval as RE

    share = Path(args.share)
    entries = [json.loads(line) for line in Path(args.manifest).read_text().splitlines() if line.strip()]
    rng = random.Random(args.seed)
    corp = [e for e in entries if e["path"].startswith(args.prefix)]
    others = [e for e in entries if not e["path"].startswith("corp/")]
    sample = rng.sample(corp, min(args.docs, len(corp)))
    docs = corp + rng.sample(others, min(args.distractors, len(others)))
    sample_paths = {e["path"] for e in sample}
    golden = [g for g in json.loads(Path(args.golden).read_text()) if set(g["expect_paths"]) & sample_paths]
    print(f"{len(docs)} indexed documents ({len(sample)} replaced by OCR text), {len(golden)} questions", flush=True)

    texts: dict[str, dict[str, str]] = {"original text": {}}
    secs = {}
    for level in args.levels.split(","):
        texts[f"OCR, {level} scan"] = {}
        secs[level] = [0.0, 0]
    for k, e in enumerate(sample):
        original = (share / e["path"]).read_text("utf-8", errors="replace")
        pdf = text_page_pdf(original)
        for level in args.levels.split(","):
            scan = scan_pdf(pdf, level, seed=args.seed + k)
            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as fh:
                fh.write(scan)
            t0 = time.perf_counter()
            parsed = parse_pdf(fh.name, ParseOptions(ocr="always", dpi=args.dpi, max_side=args.max_side))
            secs[level][0] += time.perf_counter() - t0
            secs[level][1] += len(parsed.pages)
            texts[f"OCR, {level} scan"][e["path"]] = "\n\n".join(parsed.texts())
        if k % 10 == 0:
            print(f"  scanned {k + 1}/{len(sample)}", flush=True)

    base = get_scale_settings()
    embedder = RE.CachedEmbedder(get_embedder(base), args.cache, base.embed_model)
    principals = {e["group"] for e in entries} | {"*"}
    original_read = RE.read_doc
    report = {}
    try:
        for cond, override in texts.items():
            RE.read_doc = lambda sh, rel, _o=override: _o.get(rel) if rel in _o else original_read(sh, rel)      # type: ignore[assignment]
            cfg = base.model_copy(update={"collection": f"rlocr{abs(hash(cond)) % 10**6}"})
            RE.index_corpus(cfg, share, [{"path": e["path"], "group": e["group"]} for e in docs], embedder, log=lambda *_: None)
            res = RE.run_config(cfg, golden, principals, embedder)
            res.pop("rows")
            report[cond] = res
            s = res["summary"]
            print(f"[{cond:20}] " + "  ".join(f"{m}={s[m]}" for m in ("hit@1", "hit@3", "hit@10", "mrr", "missed")), flush=True)
            client = ix.get_client(cfg)
            client.delete_collection(ix.collection_name(cfg))
            ix.reset_client_cache()
    finally:
        RE.read_doc = original_read
    report["seconds_per_page"] = {lvl: round(t / max(n, 1), 2) for lvl, (t, n) in secs.items()}
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("layout", "ocr"):
        p = sub.add_parser(name)
        p.add_argument("--share", required=True)
        p.add_argument("--manifest", required=True)
        p.add_argument("--prefix", default="mdn/,k8s/,news/", help="comma-separated path prefixes to draw prose from")
        p.add_argument("--docs", type=int, default=12)
        p.add_argument("--seed", type=int, default=7)
        p.add_argument("--levels", default="clean,scan,poor")
        p.add_argument("--dpi", type=int, default=150)
        p.add_argument("--max-side", type=int, default=2200)
        p.add_argument("--restore-only", action="store_true", help="skip the raw-OCR run (halves the time)")
        p.add_argument("--interleave", action="store_true", help="write the PDF's text line by line across columns (print-to-PDF / OCR'd style)")
        p.add_argument("--out")
    r = sub.add_parser("retrieval")
    r.add_argument("--share", required=True)
    r.add_argument("--manifest", required=True)
    r.add_argument("--golden", required=True)
    r.add_argument("--prefix", default="corp/")
    r.add_argument("--docs", type=int, default=100)
    r.add_argument("--distractors", type=int, default=2000)
    r.add_argument("--levels", default="clean,scan,poor")
    r.add_argument("--dpi", type=int, default=150)
    r.add_argument("--max-side", type=int, default=2200)
    r.add_argument("--cache", default="scale_eval_embeddings.sqlite")
    r.add_argument("--seed", type=int, default=7)
    r.add_argument("--out")
    args = parser.parse_args(argv)
    import logging

    logging.disable(logging.INFO)
    report = {"layout": cmd_layout, "ocr": cmd_ocr, "retrieval": cmd_retrieval}[args.cmd](args)
    print(json.dumps(report, indent=1))
    if args.out:
        Path(args.out).write_text(json.dumps(report, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
