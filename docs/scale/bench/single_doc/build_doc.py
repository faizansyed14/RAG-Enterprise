"""Build the single test document: a real Kubernetes doc page (Storage Classes, Apache-2.0 / CC-BY-4.0 documentation text) printed as a PDF,
plus scanned copies at two quality levels and the answer key.

  PYTHONPATH=backend python build_doc.py storage-classes.md OUTPUT_DIR
"""
import io, json, re, sys
from pathlib import Path
SRC = Path(sys.argv[1])
OUT = Path(sys.argv[2])

def clean_inline(s):
    s = re.sub(r"\{\{<\s*skew currentVersion\s*>\}\}", "1.35", s)
    s = re.sub(r"\{#[\w\-]+\}", "", s)
    s = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", s)
    s = s.replace("`", "").replace("**", "").replace("_", "_")
    s = s.replace("&#x2713;", "Yes")
    s = re.sub(r"\s+", " ", s).strip()
    return s

def parse_md(text):
    text = re.sub(r"^---\n.*?\n---\n", "", text, count=1, flags=re.S)
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    lines = text.splitlines()
    blocks = []   # (kind, payload)
    i = 0; para = []
    def flush():
        nonlocal para
        if para:
            blocks.append(("p", clean_inline(" ".join(para)))); para = []
    while i < len(lines):
        ln = lines[i]
        if ln.strip().startswith("{{%") or ln.strip().startswith("{{< table") or ln.strip().startswith("{{< /table") or "feature-state" in ln:
            flush(); i += 1; continue
        if ln.strip() == "{{< note >}}":
            flush(); i += 1; note = []
            while i < len(lines) and lines[i].strip() != "{{< /note >}}":
                if "feature-state" not in lines[i]: note.append(lines[i].strip())
                i += 1
            blocks.append(("p", "Note: " + clean_inline(" ".join(note)))); i += 1; continue
        m = re.match(r"^(#{2,4})\s+(.*)", ln)
        if m:
            flush(); blocks.append(("h" + str(len(m.group(1))), clean_inline(m.group(2)))); i += 1; continue
        if ln.strip().startswith("```"):
            flush(); i += 1; code = []
            while i < len(lines) and not lines[i].strip().startswith("```"):
                code.append(lines[i].strip()); i += 1
            i += 1
            blocks.append(("code", "\n".join(c for c in code if c))); continue
        if ln.strip().startswith("|"):
            flush(); rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                cells = [clean_inline(c) for c in lines[i].strip().strip("|").split("|")]
                if not all(re.fullmatch(r":?-+:?", c.strip()) or c == "" for c in cells) or not any(cells):
                    if not all(re.fullmatch(r":?-+:?", c) for c in cells if c):
                        rows.append(cells)
                i += 1
            blocks.append(("table", rows)); continue
        m = re.match(r"^\s*(?:[-*]|\d+\.)\s+(.*)", ln)
        if m:
            flush()
            item = [m.group(1)]; i += 1
            while i < len(lines) and lines[i].strip() and not re.match(r"^\s*(?:[-*]|\d+\.)\s+", lines[i]) and not lines[i].strip().startswith(("```", "|", "#", "{{")):
                item.append(lines[i].strip()); i += 1
            blocks.append(("li", clean_inline(" ".join(item)))); continue
        if not ln.strip():
            flush(); i += 1; continue
        para.append(ln.strip()); i += 1
    flush()
    return [b for b in blocks if b[1]]

def render(blocks, title, header_l, header_r):
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.pdfgen import canvas as cv
    from reportlab.platypus import BaseDocTemplate, Frame, PageTemplate, Paragraph, Preformatted, Spacer, Table, TableStyle
    from xml.sax.saxutils import escape
    ss = getSampleStyleSheet()
    body = ParagraphStyle("b", parent=ss["BodyText"], fontName="Helvetica", fontSize=10.5, leading=14.5, spaceAfter=6)
    li = ParagraphStyle("li", parent=body, leftIndent=14, bulletIndent=4)
    h2 = ParagraphStyle("h2", parent=ss["Heading2"], fontName="Helvetica-Bold", fontSize=15, spaceBefore=10, spaceAfter=6)
    h3 = ParagraphStyle("h3", parent=ss["Heading3"], fontName="Helvetica-Bold", fontSize=12.5, spaceBefore=8, spaceAfter=4)
    h4 = ParagraphStyle("h4", parent=ss["Heading4"], fontName="Helvetica-Bold", fontSize=11, spaceBefore=6, spaceAfter=3)
    code = ParagraphStyle("c", fontName="Courier", fontSize=8.5, leading=10.5, leftIndent=8, backColor=colors.whitesmoke)
    cell = ParagraphStyle("cell", parent=body, fontSize=9.5, leading=12, spaceAfter=0)
    class NC(cv.Canvas):
        def __init__(self, *a, **k):
            super().__init__(*a, **k); self._pages = []
        def showPage(self):
            self._pages.append(dict(self.__dict__)); self._startPage()
        def save(self):
            n = len(self._pages)
            for st in self._pages:
                self.__dict__.update(st)
                self.setFont("Helvetica", 8.5); self.setFillColor(colors.grey)
                self.drawString(20 * mm, A4[1] - 12 * mm, header_l); self.drawRightString(A4[0] - 20 * mm, A4[1] - 12 * mm, header_r)
                self.drawCentredString(A4[0] / 2, 10 * mm, f"Page {self._pageNumber} of {n}")
                super().showPage()
            super().save()
    buf = io.BytesIO()
    doc = BaseDocTemplate(buf, pagesize=A4, leftMargin=20 * mm, rightMargin=20 * mm, topMargin=22 * mm, bottomMargin=20 * mm)
    doc.addPageTemplates([PageTemplate(id="p", frames=[Frame(doc.leftMargin, doc.bottomMargin, doc.width, doc.height, id="f")])])
    story = [Paragraph(escape(title), ParagraphStyle("t", parent=ss["Title"], fontName="Helvetica-Bold", fontSize=22, spaceAfter=10))]
    for kind, p in blocks:
        if kind == "p": story.append(Paragraph(escape(p), body))
        elif kind == "li": story.append(Paragraph(escape(p), li, bulletText="•"))
        elif kind == "h2": story.append(Paragraph(escape(p), h2))
        elif kind == "h3": story.append(Paragraph(escape(p), h3))
        elif kind == "h4": story.append(Paragraph(escape(p), h4))
        elif kind == "code": story += [Preformatted(p, code), Spacer(1, 6)]
        elif kind == "table":
            ncol = max(len(r) for r in p)
            data = [[Paragraph(escape(c), cell) for c in (r + [""] * (ncol - len(r)))] for r in p]
            t = Table(data, repeatRows=1, colWidths=[doc.width / ncol] * ncol)
            t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.5, colors.black), ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey), ("VALIGN", (0, 0), (-1, -1), "TOP")]))
            story += [t, Spacer(1, 8)]
    doc.build(story, canvasmaker=NC)
    return buf.getvalue()

if __name__ == "__main__":
    blocks = parse_md(SRC.read_text())
    title = "Storage Classes"
    hl, hr = "Kubernetes Documentation - Concepts / Storage", "Storage Classes"
    pdf = render(blocks, title, hl, hr)
    (OUT / "storage_classes.pdf").write_bytes(pdf)
    from app.scale.docparse.synth import scan_pdf
    for lvl in ("scan", "poor"):
        (OUT / f"storage_classes_{lvl}.pdf").write_bytes(scan_pdf(pdf, lvl, seed=3))
    body_blocks = [b for b in blocks if b[0] != "table"]
    truth = {"title": title, "body": [title] + [b[1] for b in body_blocks], "tables": [b[1] for b in blocks if b[0] == "table"],
             "furniture": [hl, hr, "Page"], "all_text": "\n".join([title] + [b[1] if b[0] != "table" else " ".join(" ".join(r) for r in b[1]) for b in blocks])}
    (OUT / "truth.json").write_text(json.dumps(truth, indent=1))
    print("blocks", len(blocks), "words", len(truth["all_text"].split()), "tables", len(truth["tables"]), "bytes", len(pdf))
