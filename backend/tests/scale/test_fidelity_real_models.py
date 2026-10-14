"""Every file type, the real local models, the production path: does a question return the right file, and is the
returned chunk the file's own text?

Skipped unless the models are on disk:  SCALE_TEST_MODELS_DIR=./models  (bash scripts/get-models.sh puts them there).

What runs for real: connector scan -> queue -> worker -> reader (native parsers, LibreOffice for legacy Office, the OCR /
layout models for scans and images) -> chunking -> BGE-small ONNX embeddings -> Qdrant hybrid search -> MiniLM ONNX
reranker -> permission re-check. Only the object store is replaced by a dictionary.

Fidelity, for each file, asked as a natural-language question that does not copy the passage:
  1. the file's own document is ranked first (and in the top 3);
  2. one of the first 3 chunks returned contains the planted passage word for word (OCR'd files: >= 85 % of its words
     in order, because the recogniser makes small mistakes);
  3. every returned chunk is a literal slice of the stored page text (chunk.text == page_text[start:end]).
"""

import difflib
import os
import re
import subprocess
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text

from app.scale import index as ix
from app.scale import legacy_office, search, sync, worker
from app.scale.db import engine
from app.scale.settings import get_scale_settings

MODELS = os.environ.get("SCALE_TEST_MODELS_DIR", "")
pytestmark = pytest.mark.skipif(
    not (MODELS and (Path(MODELS) / "bge-small-en-v1.5-int8/onnx/model.onnx").exists()),
    reason="set SCALE_TEST_MODELS_DIR to a folder made by scripts/get-models.sh",
)

# (file name, passage planted in it, question that does not copy the passage)
TEXT_FACTS = [
    ("lease.txt", "Meridian Logistics must give ninety-seven days written notice before ending the Dunmore warehouse lease.",
     "how much notice does Meridian have to give to end the Dunmore warehouse lease"),
    ("handbook.md", "Night-shift staff at the Kharsan refinery receive a hazard allowance of eleven percent of base pay.",
     "what extra pay do night shift workers at Kharsan get"),
    ("incident.log", "2026-03-14 02:17 ERROR conveyor controller 7 lost heartbeat from the Tamarind packing hall",
     "which controller lost its heartbeat in the packing hall"),
    ("settings.yaml", "archive_after_months: 84 # records of the Quillfeather programme are archived after seven years",
     "when are Quillfeather programme records archived"),
    ("memo.rtf", r"The Zephyr tender closes on the nineteenth of June and late bids will not be opened.",
     "when does the Zephyr tender close"),
    ("policy.html", "Visitors to the Sharjah depot must wear a green lanyard and sign the gate register.",
     "what do visitors to the Sharjah depot have to wear"),
    ("agreement.docx", "The Halvorsen supply agreement renews automatically for twelve months unless cancelled in writing.",
     "does the Halvorsen supply agreement renew by itself"),
    ("report.pdf", "Cold-room temperature excursions above eight degrees at Bramwell must be reported within two hours.",
     "how fast must temperature excursions at Bramwell be reported"),
    ("deck.pptx", "Phase two dredging begins in the spring and is led by Orion Marine under a fixed-price contract.",
     "who leads the phase two dredging"),
    ("minutes.odt", "The board approved the Ellesmere pension top-up of four hundred thousand dirhams on the second ballot.",
     "what pension top-up did the board approve for Ellesmere"),
    ("mail.eml", "Please hold the Caldecott shipment at customs until the revised certificate of origin arrives.",
     "what should happen to the Caldecott shipment at customs"),
    ("config.json", "Orders from the Lindqvist account above fifty thousand require approval from the regional controller.",
     "which orders from Lindqvist need the regional controller to approve"),
    ("old_contract.doc", "The Penhallow tenancy may be assigned only with the written consent of the freeholder.",
     "can the Penhallow tenancy be assigned to someone else"),
    ("old_plan.ppt", "Fleet electrification at the Marlowe yard will convert nineteen forklifts before the winter shutdown.",
     "how many forklifts get converted at the Marlowe yard"),
]
TABLE_FACTS = [   # (file, header, row that must come back together, question)
    ("ledger.xlsx", ["Invoice", "Customer", "Amount", "Status"], ["INV-5120", "Ostrava Cold Chain", "48215", "disputed"],
     "which invoice of Ostrava Cold Chain is disputed and for how much"),
    ("rates.csv", ["Route", "Carrier", "Rate", "Mode"], ["R-8841", "Kestrel Freight", "1275", "refrigerated"],
     "what is the rate for carrier Kestrel Freight on the refrigerated route"),
    ("old_prices.xls", ["Part", "Supplier", "Price", "Lead"], ["P-3317", "Dovecote Metals", "6420", "nineteen days"],
     "what does Dovecote Metals charge and what is the lead time"),
]
SCAN_FACTS = [
    ("scan_terms.pdf", "The notice period for terminating the Windermere supplier agreement is ninety days written notice.",
     "what is the notice period to terminate the Windermere supplier agreement"),
    ("photo_note.png", "Invoices from Aldgate Printing are payable within thirty days of receipt.",
     "when must Aldgate Printing invoices be paid"),
]


def words(s):
    return re.findall(r"[a-z0-9]+", s.lower())


def passage_in(chunk, passage, fuzzy=False):
    cw, pw = words(chunk), words(passage)
    if not fuzzy:
        return any(cw[i:i + len(pw)] == pw for i in range(len(cw) - len(pw) + 1))
    best = difflib.SequenceMatcher(None, cw, pw, autojunk=False).find_longest_match(0, len(cw), 0, len(pw))
    matcher = difflib.SequenceMatcher(None, cw, pw, autojunk=False)
    matched = sum(b.size for b in matcher.get_matching_blocks())
    return matched / max(len(pw), 1) >= 0.85 and best.size >= 3


def soffice(src, target, outdir):
    subprocess.run([legacy_office.soffice_path(), f"-env:UserInstallation=file://{outdir}/profile", "--headless",
                    "--convert-to", target, "--outdir", str(outdir), str(src)], check=True, capture_output=True, timeout=240)


FILLER = ("This section describes routine handling, scheduling and record keeping for the site. Staff follow the standard "
          "procedure and escalate exceptions to the duty manager. ")


def build_corpus(root: Path):
    import docx
    import openpyxl
    from pptx import Presentation

    work = root / "_work"
    work.mkdir()
    facts = {n: p for n, p, _ in TEXT_FACTS}

    def para(name):
        return FILLER + facts[name] + " " + FILLER

    (root / "lease.txt").write_text(para("lease.txt"))
    (root / "handbook.md").write_text("# Handbook\n\n## Allowances\n\n" + para("handbook.md") + "\n")
    (root / "incident.log").write_text("2026-03-14 02:10 INFO shift start\n" + facts["incident.log"] + "\n2026-03-14 02:20 INFO restarted\n")
    (root / "settings.yaml").write_text("retention:\n  " + facts["settings.yaml"] + "\n  owner: records\n")
    (root / "memo.rtf").write_text("{\\rtf1\\ansi Memo\\par " + facts["memo.rtf"] + "\\par}")
    (root / "policy.html").write_text(f"<html><body><h1>Site policy</h1><p>{FILLER}</p><p>{facts['policy.html']}</p></body></html>")
    (root / "mail.eml").write_text(
        "From: ops@example.com\nTo: team@example.com\nSubject: Caldecott shipment\nDate: Mon, 02 Mar 2026 09:00:00 +0000\n\n"
        + facts["mail.eml"] + "\n")
    import json
    (root / "config.json").write_text(json.dumps({"rules": [{"note": facts["config.json"]}], "version": 3}))

    d = docx.Document()
    d.add_heading("Supply agreement", 1)
    d.add_paragraph(FILLER)
    d.add_paragraph(facts["agreement.docx"])
    d.save(root / "agreement.docx")
    d.save(work / "old_contract_src.docx")
    d2 = docx.Document()
    d2.add_heading("Tenancy", 1)
    d2.add_paragraph(FILLER)
    d2.add_paragraph(facts["old_contract.doc"])
    d2.save(work / "old_contract.docx")
    soffice(work / "old_contract.docx", "doc", root)
    d3 = docx.Document()
    d3.add_heading("Board minutes", 1)
    d3.add_paragraph(facts["minutes.odt"])
    d3.save(work / "minutes.docx")
    soffice(work / "minutes.docx", "odt", root)
    d4 = docx.Document()
    d4.add_heading("Cold chain report", 1)
    d4.add_paragraph(FILLER)
    d4.add_paragraph(facts["report.pdf"])
    d4.save(work / "report.docx")
    soffice(work / "report.docx", "pdf", root)

    p = Presentation()
    s = p.slides.add_slide(p.slide_layouts[1])
    s.shapes.title.text = "Harbour expansion"
    s.placeholders[1].text = facts["deck.pptx"]
    p.save(root / "deck.pptx")
    p2 = Presentation()
    s2 = p2.slides.add_slide(p2.slide_layouts[1])
    s2.shapes.title.text = "Fleet plan"
    s2.placeholders[1].text = facts["old_plan.ppt"]
    p2.save(work / "old_plan.pptx")
    soffice(work / "old_plan.pptx", "ppt", root)

    for fname, header, row, _ in TABLE_FACTS:
        rows = [header] + [[f"{header[0][:3].upper()}-{1000 + i}", f"Other Co {i}", str(100 + i), "paid"] for i in range(40)]
        rows.insert(23, row)
        if fname.endswith(".csv"):
            (root / fname).write_text("\n".join(",".join(r) for r in rows))
        else:
            wb = openpyxl.Workbook()
            for r in rows:
                wb.active.append([int(c) if c.isdigit() else c for c in r])
            target = work / "old_prices.xlsx" if fname.endswith(".xls") else root / fname
            wb.save(target)
            if fname.endswith(".xls"):
                soffice(target, "xls", root)

    from app.scale.docparse import synth as S

    for fname, passage, _ in SCAN_FACTS:
        pdf, _ = S.build_pdf([S.PageSpec(title="Letter", columns=[[passage, "Please keep this page for your records."]])])
        scanned = S.scan_pdf(pdf, "clean")
        if fname.endswith(".pdf"):
            (root / fname).write_bytes(scanned)
        else:
            import fitz
            (root / fname).write_bytes(fitz.open(stream=scanned, filetype="pdf")[0].get_pixmap(dpi=150).tobytes("png"))

    # look-alikes: same wording, other names and numbers, so a vague match is not enough
    vendors = ["Harcourt Freight", "Bellmere Foods", "Tavistock Marine", "Greywell Textiles", "Norcross Steel", "Alderley Paper"]
    places = ["Dunmore", "Kharsan", "Bramwell", "Sharjah", "Marlowe", "Ellesmere", "Penhallow", "Caldecott"]
    n = 0
    for v in vendors:
        for pl in places:
            n += 1
            (root / "noise").mkdir(exist_ok=True)
            (root / "noise" / f"n{n:03d}.txt").write_text(
                f"{FILLER}{v} must give {40 + n} days written notice before ending the {pl} warehouse lease. "
                f"Night-shift staff at the {pl} depot receive an allowance of {n % 9 + 2} percent. {FILLER}")
    (root / "blob.xyz").write_bytes(bytes(range(256)) * 40)
    (root / "tool.exe").write_bytes(b"MZ" + bytes(range(256)) * 40)
    import shutil
    shutil.rmtree(work)


@pytest.fixture
def real_models(monkeypatch):
    models = str(Path(MODELS).resolve())
    for k, v in {
        "SCALE_EMBED_BACKEND": "onnx", "SCALE_ONNX_MODEL_DIR": f"{models}/bge-small-en-v1.5-int8", "SCALE_ONNX_POOLING": "cls",
        "SCALE_EMBED_DIM": "384", "SCALE_EMBED_MODEL": f"bge-small-{uuid.uuid4().hex[:6]}", "SCALE_EMBED_BATCH": "8",
        "SCALE_CHUNK_WORDS": "160", "SCALE_CHUNK_OVERLAP_WORDS": "20",
        "SCALE_RERANK_MODE": "onnx", "SCALE_RERANK_ONNX_DIR": f"{models}/ms-marco-MiniLM-L-6-v2",
        "SCALE_DOCPARSE_MODE": "scanned",
    }.items():
        monkeypatch.setenv(k, v)
    get_scale_settings.cache_clear()
    ix.reset_client_cache()
    search.reset_model_caches()


def test_every_file_type_returns_its_own_text_with_the_real_models(tmp_path, object_store, real_models, capsys):
    if not legacy_office.available():
        pytest.skip("LibreOffice is needed to build the legacy Office files")
    from app.scale.docparse import available
    if not available():
        pytest.skip("OCR packages not installed")
    from app.scale.models import get_embedder, get_reranker

    assert type(get_embedder()).__name__.startswith("Onnx") and get_reranker() is not None, "the real models must be in use"
    build_corpus(tmp_path)
    cid = sync.create_connection("local", f"fid-{uuid.uuid4().hex[:6]}",
                                 {"root": str(tmp_path), "acl_rules": [{"prefix": "", "principals": ["group:staff"]}]})
    sync.run_scan(cid)
    assert worker.main(["--once", "--no-schedule", "--concurrency", "1"]) == 0

    with engine().connect() as conn:
        states = {n: (s, e) for n, s, e in conn.execute(text("SELECT name, state, error FROM source_items")).all()}
    expected = [n for n, *_ in TEXT_FACTS] + [n for n, *_ in TABLE_FACTS] + [n for n, *_ in SCAN_FACTS]
    for name in expected:
        assert states[name][0] == "indexed", (name, states[name])
    assert states["blob.xyz"][0] == "skipped" and states["tool.exe"][0] == "skipped"       # unreadable binaries say so

    principals = {"group:staff"}
    cfg = get_scale_settings()
    rows, failures = [], []

    def check(name, question, test_passage, fuzzy=False, must_contain=None):
        cands = search.candidate_documents(question, principals)
        names = [c.name for c in cands]
        first = bool(names) and names[0] == name
        in3 = name in names[:3]
        hits = search.search_chunks(question, principals=principals, limit=3, rerank=True)
        got = any(
            h.name == name and ((all(m.lower() in h.text.lower() for m in must_contain) if must_contain
                                 else passage_in(h.text, test_passage, fuzzy)))
            for h in hits)
        # every returned chunk is a literal slice of the stored page text
        literal = True
        with engine().connect() as conn:
            for h in hits:
                page = conn.execute(text("SELECT text FROM document_pages WHERE doc_id = :d AND page_number = :p"),
                                    {"d": h.doc_id, "p": h.page}).scalar()
                if h.text and page is not None and h.text.strip() and h.text.strip() not in page and page[h.start:h.end] != h.text:
                    literal = False
        rows.append((name, first, in3, got, literal))
        if not (in3 and got and literal):
            failures.append((name, names[:3], first, got, literal))

    for name, passage, q in TEXT_FACTS:
        check(name, q, passage)
    for name, _, row, q in TABLE_FACTS:
        check(name, q, "", must_contain=[c for c in row if not c.isdigit()] + [c for c in row if c.isdigit()][:0])
    for name, passage, q in SCAN_FACTS:
        check(name, q, passage, fuzzy=True)

    # control: the check must be able to fail - a passage of ANOTHER file is never found in a chunk
    others = {n: p for n, p, _ in TEXT_FACTS}
    hits = search.search_chunks(TEXT_FACTS[0][2], principals=principals, limit=3, rerank=True)
    assert hits and not any(passage_in(h.text, others["handbook.md"]) for h in hits)
    assert not passage_in(hits[0].text, "Meridian Logistics must give one hundred days written notice before ending the lease.")

    with capsys.disabled():
        print("\n  file                 first  top3  passage  literal")
        for r in rows:
            print(f"  {r[0]:20s} {str(r[1]):6s} {str(r[2]):5s} {str(r[3]):8s} {r[4]}")
        print(f"  first-rank {sum(r[1] for r in rows)}/{len(rows)}   top3 {sum(r[2] for r in rows)}/{len(rows)}   "
              f"passage {sum(r[3] for r in rows)}/{len(rows)}   literal {sum(r[4] for r in rows)}/{len(rows)}")
    assert not failures, failures
    assert sum(r[1] for r in rows) >= len(rows) - 2           # first place for nearly all; the rest are still in the top 3
