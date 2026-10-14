"""End to end on REAL files: does the system return the real document, the real passage, and only to people allowed to see it?

  python -m scripts.scale_e2e_real --source DIR --manifest m.jsonl --golden golden.json [--docs-file paths.txt]
         [--sample 1500] [--formats pdf=0.25,docx=0.15,html=0.1] [--workers 2] [--out report.json]

source      a folder of text-like files (txt / md). manifest.jsonl: {"path": "rel/path", "group": "group:x"} per file.
golden      [{"q", "expect_paths": [rel...], "style", "source"}]: the questions and the files that really answer them.

What it does (every step uses the production code path: connector -> scan -> queue -> worker -> parse -> chunk ->
embed -> index -> access-checked search; nothing is mocked except the object store, which is a temp folder):

  1. copies a sample into a work folder, converting part of it to PDF / DOCX / HTML so the real parsers run
  2. scans the folder (folder -> principal rules) and drains the worker; reports documents per second
  3. asks every question as a user who may read everything and measures where the right FILE ranks
  4. FIDELITY, on the top result for each question and on the file that answers it:
       - the document is the file at that path: its stored content hash equals sha256 of the bytes on disk
       - the passage is the file's own text: each of the top 3 returned chunks is compared with the ORIGINAL text the
         file was made from - "exact" (same words in the same order) or "near" (>= 70 % of its 6-word sequences: the
         reader normalised Markdown / code-fence markup); a control compares the same chunks with random OTHER
         documents and must match none, which proves the check can fail
       - the stored page text agrees with the passage (the agent reads pages; they must hold what was found)
  5. ACCESS: every question is asked again as a user who holds only one group, and as a user with none; counts any
     result outside the allowed folders (must be 0)
  6. CHANGE: edits some files, deletes some, adds some; rescans; checks the edit is found, the deleted file is gone
     and unchanged files were not reprocessed

Never run against a production database: it creates and deletes rows (the guard below insists the database name
says test / e2e / bench).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
import shutil
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

RULE_ORDER = ["news", "mdn", "k8s", "licenses", "corp"]


class DirObjectStore:
    """The object store the pipeline writes raw files and previews to, as a temp folder."""

    def __init__(self, root: Path) -> None:
        self.root = root
        root.mkdir(parents=True, exist_ok=True)

    def _p(self, key: str) -> Path:
        p = self.root / key
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    def put_object(self, key, data, content_type="application/octet-stream"):
        self._p(key).write_bytes(data)
        return key

    def get_object(self, key):
        return self._p(key).read_bytes()

    def delete_object(self, key):
        self._p(key).unlink(missing_ok=True)

    def presigned_url(self, key, expires_in=None):
        return f"file://{self._p(key)}"

    def ensure_bucket(self):
        pass


def norm(s: str) -> str:
    return " ".join(s.split())


WORD = re.compile(r"\w+", re.UNICODE)


def words(s: str) -> list[str]:
    return [w.lower() for w in WORD.findall(s)]


def contains_word_run(doc_words: list[str], run_words: list[str]) -> bool:
    """Do these words occur, in order and adjacent, somewhere in the document's words?"""
    if not run_words:
        return True
    n, first = len(run_words), run_words[0]
    for i, w in enumerate(doc_words):
        if w == first and doc_words[i: i + n] == run_words:
            return True
    return False


NEAR_SHARE = 0.7          # share of a passage's 6-word sequences that must exist in the original to count as "same text, different markup"


def passage_match(chunk_text: str, original_text: str) -> tuple[str, float]:
    """Is this returned passage the document's own text?

    Words are compared (letters and digits, lower case), so line wrapping and punctuation do not matter. Results:
      "exact"  every word of the passage, in order and adjacent, occurs in the original (a title line that a
               renderer put in front of the passage is allowed to be missing from the original)
      "near"   at least 70 % of the passage's 6-word sequences occur in the original: the reader normalised markup
               (code fences, Markdown / Hugo syntax, front matter) or a renderer soft-wrapped a very long string
      "none"   anything else - text that is not in this document
    The second value is the share of 6-word sequences found (1.0 for "exact")."""
    cw, ow = words(chunk_text), words(original_text)
    if len(cw) < 4 or not ow:
        return "none", 0.0
    variants = [cw]
    if "\n" in chunk_text:
        rest = words(chunk_text.split("\n", 1)[1])
        if len(rest) >= 4:
            variants.append(rest)
    for v in variants:
        if contains_word_run(ow, v):
            return "exact", 1.0
    best = 0.0
    for v in variants:
        n = 6 if len(v) >= 12 else max(2, len(v) // 2)
        grams = {tuple(ow[i: i + n]) for i in range(len(ow) - n + 1)}
        mine = [tuple(v[i: i + n]) for i in range(len(v) - n + 1)]
        if mine:
            best = max(best, sum(1 for g in mine if g in grams) / len(mine))
    return ("near", best) if best >= NEAR_SHARE else ("none", best)


# ------------------------------------------------------------------------------------------ building the work folder

def convert(src_text: str, rel: str, fmt: str, dst: Path) -> Path:
    stem = Path(rel).stem
    if fmt == "pdf":
        from app.ingestion.text_to_pdf import render_blocks_as_pdf

        blocks = [{"type": "heading", "text": stem}]
        for para in re.split(r"\n\s*\n", src_text.strip()):
            para = " ".join(para.split())
            if para:
                blocks.append({"type": "paragraph", "text": para[:6000]})
        out = dst.with_suffix(".pdf")
        render_blocks_as_pdf(blocks, str(out))
        return out
    if fmt == "docx":
        import docx

        d = docx.Document()
        d.add_heading(stem, level=1)
        for para in re.split(r"\n\s*\n", src_text.strip()):
            para = " ".join(para.split())
            if para:
                d.add_paragraph(para)
        out = dst.with_suffix(".docx")
        d.save(str(out))
        return out
    if fmt == "html":
        import html

        body = "".join(f"<p>{html.escape(' '.join(p.split()))}</p>" for p in re.split(r"\n\s*\n", src_text.strip()) if p.strip())
        out = dst.with_suffix(".html")
        out.write_text(f"<html><head><title>{html.escape(stem)}</title></head><body><h1>{html.escape(stem)}</h1>{body}</body></html>")
        return out
    if fmt == "pptx":
        import pptx
        from pptx.util import Inches

        prs = pptx.Presentation()
        paras = [" ".join(p.split()) for p in re.split(r"\n\s*\n", src_text.strip()) if p.strip()]
        slides, current, count = [], [], 0
        for para in paras:
            current.append(para)
            count += len(para.split())
            if count >= 120:
                slides.append(current)
                current, count = [], 0
        if current:
            slides.append(current)
        for n, group in enumerate(slides[:40], start=1):
            slide = prs.slides.add_slide(prs.slide_layouts[5])
            slide.shapes.title.text = f"{stem} part {n}"
            box = slide.shapes.add_textbox(Inches(0.5), Inches(1.5), Inches(9), Inches(5)).text_frame
            box.word_wrap = True
            box.text = group[0]
            for extra in group[1:]:
                box.add_paragraph().text = extra
        out = dst.with_suffix(".pptx")
        prs.save(str(out))
        return out
    if fmt == "rtf":
        def esc(t: str) -> str:
            return "".join(c if c.isascii() and c not in "\\{}" else (f"\\u{ord(c)}?" if ord(c) < 32768 and not c.isascii() else "\\" + c if c in "\\{}" else "?") for c in t)

        body = "\\par ".join(esc(" ".join(p.split())) + "\\par" for p in re.split(r"\n\s*\n", src_text.strip()) if p.strip())
        out = dst.with_suffix(".rtf")
        out.write_text("{\\rtf1\\ansi " + body + "}", encoding="ascii", errors="replace")
        return out
    raise ValueError(fmt)


def build_workdir(source: Path, manifest: list[dict], work: Path, formats: dict[str, float], seed: int) -> dict[str, dict]:
    """Copies/converts files. Returns {original_rel: {"actual": rel, "fmt": "txt|pdf|docx|html", "group": g, "text": original}}."""
    rng = random.Random(seed)
    out: dict[str, dict] = {}
    for m in manifest:
        rel = m["path"]
        text = (source / rel).read_text("utf-8", errors="replace")
        dst = work / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        roll, fmt, acc = rng.random(), "orig", 0.0
        for name, share in formats.items():
            acc += share
            if roll < acc:
                fmt = name
                break
        if fmt == "orig" or len(text) < 400:
            shutil.copyfile(source / rel, dst)
            actual, kind = rel, "orig"
        else:
            try:
                actual = str(convert(text, rel, fmt, dst).relative_to(work))
                kind = fmt
            except Exception:  # noqa: BLE001 -- a file the converter cannot render stays as it was
                shutil.copyfile(source / rel, dst)
                actual, kind = rel, "orig"
        out[rel] = {"actual": actual, "fmt": kind, "group": m["group"], "text": text}
    return out


def acl_rules(manifest: list[dict]) -> list[dict]:
    seen: dict[str, str] = {}
    for m in manifest:
        seen.setdefault(m["path"].split("/")[0], m["group"])
    return [{"prefix": k, "principals": [v]} for k, v in seen.items()]


# ------------------------------------------------------------------------------------------ the run

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", required=True)
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--golden", required=True)
    ap.add_argument("--docs-file")
    ap.add_argument("--sample", type=int, default=1500, help="documents to ingest (all the answer files of the sampled questions are kept)")
    ap.add_argument("--questions", type=int, default=300)
    ap.add_argument("--formats", default="pdf=0.20,docx=0.12,html=0.08,pptx=0.08,rtf=0.05", help="share of files converted to each format")
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--seed", type=int, default=11)
    ap.add_argument("--out")
    ap.add_argument("--allow-any-db", action="store_true")
    args = ap.parse_args(argv)

    from sqlalchemy import text as sql

    from app.scale import acl, search, sync, worker
    from app.scale.db import engine

    dbname = engine().url.database or ""
    if not args.allow_any_db and not any(t in dbname for t in ("test", "e2e", "bench")):
        print(f"refusing: database '{dbname}' does not look like a test database (use --allow-any-db to override)")
        return 2

    with engine().begin() as conn:                      # a clean slate: leftovers of an earlier run would look like leaks
        conn.execute(sql("DELETE FROM scale_jobs"))
        conn.execute(sql("DELETE FROM source_connections"))
        conn.execute(sql("DELETE FROM document_trees WHERE doc_id IN (SELECT rag_doc_id FROM documents WHERE tier = 'lite')"))
        conn.execute(sql("DELETE FROM documents WHERE tier = 'lite'"))
    from app.scale import index as chunk_index

    try:                                                 # ... and its chunks: points left from an earlier run would answer for documents that no longer exist
        chunk_index.get_client().delete_collection(chunk_index.collection_name())
    except Exception:  # noqa: BLE001 -- no such collection yet
        pass
    rng = random.Random(args.seed)
    source = Path(args.source)
    golden_all = json.loads(Path(args.golden).read_text())
    manifest_all = [json.loads(line) for line in Path(args.manifest).read_text().splitlines() if line.strip()]
    if args.docs_file:
        keep = {line.strip() for line in Path(args.docs_file).read_text().splitlines() if line.strip()}
        manifest_all = [m for m in manifest_all if m["path"] in keep]
    available = {m["path"] for m in manifest_all}
    golden_all = [g for g in golden_all if all(p in available for p in g["expect_paths"])]

    # questions: a stratified sample; their answer files are always part of the ingest
    by_key: dict[Any, list[dict]] = {}
    for g in golden_all:
        by_key.setdefault((g.get("source"), g.get("style")), []).append(g)
    golden: list[dict] = []
    per = max(1, args.questions // max(1, len(by_key)))
    for _, items in sorted(by_key.items(), key=lambda kv: str(kv[0])):
        rng.shuffle(items)
        golden.extend(items[:per])
    must = {p for g in golden for p in g["expect_paths"]}
    rest = [m for m in manifest_all if m["path"] not in must]
    rng.shuffle(rest)
    chosen = [m for m in manifest_all if m["path"] in must] + rest[: max(0, args.sample - len(must))]
    formats = {k: float(v) for k, v in (x.split("=") for x in args.formats.split(",") if x)}

    tmp = Path(tempfile.mkdtemp(prefix="scale-e2e-"))
    work, store_dir = tmp / "share", tmp / "objects"
    import app.core.object_store as object_store_mod

    object_store_mod.get_object_store = lambda: DirObjectStore(store_dir)          # type: ignore[assignment]
    t0 = time.time()
    files = build_workdir(source, chosen, work, formats, args.seed)
    fmt_counts = {k: sum(1 for f in files.values() if f["fmt"] == k) for k in ("orig", "pdf", "docx", "html", "pptx", "rtf")}
    print(f"work folder: {len(files)} files {fmt_counts}, {len(golden)} questions ({time.time() - t0:.0f}s)", flush=True)

    report: dict[str, Any] = {"files": len(files), "formats": fmt_counts, "questions": len(golden)}

    # ---- 2. scan + ingest
    rules = acl_rules(chosen)
    cid = sync.create_connection("local", f"e2e-{uuid.uuid4().hex[:6]}", {"root": str(work), "acl_rules": rules})
    t0 = time.time()
    stats = sync.run_scan(cid)
    scan_s = time.time() - t0
    t0 = time.time()
    if worker.main(["--once", "--no-schedule", "--concurrency", str(args.workers)]) != 0:
        print("worker exited with errors")
    ingest_s = time.time() - t0
    with engine().connect() as conn:
        states = dict(conn.execute(sql("SELECT state, count(*) FROM source_items GROUP BY state")).all())
        bad = conn.execute(sql("SELECT path, state, error FROM source_items WHERE state IN ('failed','pending') LIMIT 10")).all()
        skipped = conn.execute(sql("SELECT path, error FROM source_items WHERE state = 'skipped' LIMIT 10")).all()
        rows = conn.execute(sql(
            "SELECT s.path, d.document_id::text, d.content_hash, d.rag_doc_id FROM source_items s JOIN documents d USING (document_id) "
            "WHERE s.deleted = false"
        )).all()
    by_path = {r[0]: {"document_id": r[1], "hash": r[2], "rag_doc_id": r[3]} for r in rows}
    by_doc = {r[1]: r[0] for r in rows}
    print(f"scan {scan_s:.0f}s ({stats.get('queued')} queued); ingest {ingest_s:.0f}s = {len(rows) / max(ingest_s, 1e-9):.1f} documents/s; states {states}", flush=True)
    report["ingest"] = {"scan_s": round(scan_s, 1), "ingest_s": round(ingest_s, 1), "docs_per_s": round(len(rows) / max(ingest_s, 1e-9), 2),
                        "states": states, "failed_or_pending": [list(map(str, b)) for b in bad], "skipped": [list(map(str, b)) for b in skipped]}

    actual_of = {o: f["actual"] for o, f in files.items()}
    original_of = {f["actual"]: o for o, f in files.items()}
    all_groups = sorted({f["group"] for f in files.values()})
    everyone = set(all_groups)

    # ---- 3 + 4. retrieval and fidelity
    from scripts.scale_realeval import rank_of, score_ranks

    ranks, lat = [], []
    fidelity = {"checked": 0, "hash_ok": 0, "path_ok": 0, "chunk_checked": 0, "chunk_exact": 0, "chunk_near": 0, "chunk_none": 0,
                "page_ok": 0, "page_checked": 0, "control_checked": 0, "control_matched": 0}
    all_texts = [f["text"] for f in files.values()]
    problems: list[dict] = []
    page_cache: dict[tuple[str, int], str | None] = {}

    def page_text(rag_doc_id: str, page: int) -> str | None:
        key = (rag_doc_id, page)
        if key not in page_cache:
            with engine().connect() as c2:
                row = c2.execute(sql("SELECT text FROM document_pages WHERE doc_id = :d AND page_number = :p"), {"d": rag_doc_id, "p": page}).first()
            page_cache[key] = row[0] if row else None
        return page_cache[key]

    for g in golden:
        t = time.perf_counter()
        cands = search.candidate_documents(g["q"], everyone)
        lat.append((time.perf_counter() - t) * 1000)
        paths = [by_doc.get(c.document_id, "?") for c in cands]
        expect = [actual_of[p] for p in g["expect_paths"]]
        r = rank_of(paths, expect)
        ranks.append(r)
        if not cands:
            continue
        # fidelity on the top candidate (what a user would open first)
        top = cands[0]
        top_path = by_doc.get(top.document_id)
        fidelity["checked"] += 1
        on_disk = work / top_path if top_path else None
        if top_path and on_disk.exists():
            fidelity["path_ok"] += 1
            if hashlib.sha256(on_disk.read_bytes()).hexdigest() == by_path[top_path]["hash"]:
                fidelity["hash_ok"] += 1
            else:
                problems.append({"q": g["q"], "problem": "stored hash differs from the file on disk", "path": top_path})
        else:
            problems.append({"q": g["q"], "problem": "returned document has no file at its path", "path": top_path})
        hits = search.search_chunks(g["q"], principals=everyone, limit=5, rerank=False)
        for h in hits[:3]:
            hp = by_doc.get(h.document_id)
            if not hp:
                continue
            fidelity["chunk_checked"] += 1
            f = files[original_of[hp]] if hp in original_of else None
            tier, share = passage_match(h.text, f["text"]) if f else ("none", 0.0)
            fidelity["chunk_" + tier] += 1
            if tier == "none":
                problems.append({"q": g["q"], "problem": "returned passage not found in the original document", "path": hp, "chunk": h.text[:160], "share": round(share, 2)})
            if fidelity["control_checked"] < 300:          # control: the same passage against a RANDOM OTHER document must NOT match
                other = rng.choice(all_texts)
                if other is not (f or {}).get("text"):
                    fidelity["control_checked"] += 1
                    if passage_match(h.text, other)[0] != "none":
                        fidelity["control_matched"] += 1
            fidelity["page_checked"] += 1
            pt = page_text(h.doc_id, h.page)
            if pt is not None and (norm(pt[h.start: h.end]) == norm(h.text) or contains_word_run(words(pt), words(h.text))):
                fidelity["page_ok"] += 1
            else:
                problems.append({"q": g["q"], "problem": "stored page text does not contain the returned passage", "path": hp, "page": h.page})
    summary = score_ranks(ranks)
    lat.sort()
    summary["p50_ms"] = round(lat[len(lat) // 2], 1)
    summary["p95_ms"] = round(lat[int(len(lat) * 0.95)], 1)
    print("retrieval through the real pipeline:", {k: summary[k] for k in ("hit@1", "hit@5", "hit@10", "hit@40", "mrr", "p50_ms", "p95_ms")}, flush=True)
    print("fidelity:", fidelity, flush=True)
    report["retrieval"] = summary
    report["fidelity"] = fidelity
    report["problems"] = problems[:25]

    # ---- 5. access control
    leaks, asked, empty_ok = [], 0, 0
    for grp in all_groups + ["group:nobody"]:
        allowed_paths = {p for p, f in ((f["actual"], f) for f in files.values()) if f["group"] == grp}
        for g in rng.sample(golden, min(len(golden), 120)):
            cands = search.candidate_documents(g["q"], {grp})
            asked += 1
            for c in cands:
                p = by_doc.get(c.document_id)
                if p not in allowed_paths:
                    leaks.append({"as": grp, "q": g["q"], "got": p})
            if grp == "group:nobody" and not cands:
                empty_ok += 1
        # and the document-level visibility check agrees
        ids = [d for d, p in list(by_doc.items())[:400]]
        vis = acl.visible_document_ids(ids, {grp})
        for d in vis:
            if by_doc[d] not in allowed_paths:
                leaks.append({"as": grp, "visible_document_ids": by_doc[d]})
    print(f"access: {asked} searches as restricted users, {len(leaks)} results outside the allowed folders", flush=True)
    report["access"] = {"searches": asked, "leaks": len(leaks), "examples": leaks[:5], "nobody_got_nothing": empty_ok}

    # ---- 6. change: edit, delete, add; unchanged files must not be reprocessed
    victims = rng.sample([o for o, f in files.items() if f["fmt"] == "orig"], 12)
    edit, delete, keep_same = victims[:5], victims[5:10], victims[10:]
    marker = {}
    for o in edit:
        token = f"ZQX{rng.randint(100000, 999999)}"
        marker[o] = token
        p = work / actual_of[o]
        p.write_text(p.read_text("utf-8", errors="replace") + f"\n\nAddendum: the amendment reference is {token} and it replaces the earlier clause.\n")
    for o in delete:
        (work / actual_of[o]).unlink()
    added = work / "corp" / "added-after-first-scan.txt"
    added.parent.mkdir(parents=True, exist_ok=True)
    added.write_text("The Zephyr Annex was added after the first scan. Its reference is ZQX424242 and it concerns harbour dredging.\n")
    stats2 = sync.run_scan(cid)
    worker.main(["--once", "--no-schedule", "--concurrency", str(args.workers)])
    with engine().connect() as conn:
        rows2 = conn.execute(sql(
            "SELECT s.path, d.document_id::text FROM source_items s JOIN documents d USING (document_id) WHERE s.deleted = false"
        )).all()
    by_doc2 = {r[1]: r[0] for r in rows2}
    found_edits = 0
    for o, token in marker.items():
        cands = search.candidate_documents(f"amendment reference {token}", everyone)
        if cands and by_doc2.get(cands[0].document_id) == actual_of[o]:
            found_edits += 1
    cands = search.candidate_documents("Zephyr Annex harbour dredging ZQX424242", everyone)
    added_ok = bool(cands) and by_doc2.get(cands[0].document_id) == "corp/added-after-first-scan.txt"
    gone = 0
    for o in delete:
        title = Path(o).stem
        cands = search.candidate_documents(title, everyone)
        if all(by_doc2.get(c.document_id) != actual_of[o] for c in cands):
            gone += 1
    queued2 = stats2.get("queued")
    print(f"change: rescan queued {queued2} (expected {len(edit) + 1}); edits found {found_edits}/{len(edit)}; added found {added_ok}; deleted gone {gone}/{len(delete)}", flush=True)
    report["change"] = {"queued_on_rescan": queued2, "expected_queued": len(edit) + 1, "edits_found": f"{found_edits}/{len(edit)}",
                        "added_found": added_ok, "deleted_gone": f"{gone}/{len(delete)}", "stats": {k: v for k, v in stats2.items() if isinstance(v, (int, str))}}

    if args.out:
        Path(args.out).write_text(json.dumps(report, indent=1, default=str))
    shutil.rmtree(tmp, ignore_errors=True)
    unmatched_ok = fidelity["chunk_none"] <= 0.01 * max(1, fidelity["chunk_checked"]) and fidelity["control_matched"] == 0
    ok = (not leaks and fidelity["hash_ok"] == fidelity["checked"] and unmatched_ok and fidelity["page_ok"] == fidelity["page_checked"]
          and found_edits == len(edit) and added_ok and gone == len(delete))
    print("RESULT:", "PASS" if ok else "CHECK THE REPORT", flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
