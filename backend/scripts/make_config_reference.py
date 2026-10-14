"""Writes docs/CONFIGURATION.md from the two settings classes, so the list of settings can never drift from the code.

  python -m scripts.make_config_reference            # rewrite the file
  python -m scripts.make_config_reference --check    # exit 1 if the file is out of date (used by a test)
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOURCES = [
    ("App settings (environment variables, no prefix)", ROOT / "backend/app/core/config.py", "Settings", ""),
    ("Search, index and worker settings (prefix SCALE_)", ROOT / "backend/app/scale/settings.py", "ScaleSettings", "SCALE_"),
]
OUT = ROOT / "docs/CONFIGURATION.md"
SECRET_NAME = re.compile(r"(_key|_secret|_password|_dsn)$|^(auth_secret|admin_password)$")


def _fields(path: Path, cls: str, prefix: str):
    src = path.read_text()
    lines = src.splitlines()
    tree = ast.parse(src)
    node = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == cls)
    section = ""
    for item in node.body:
        if not isinstance(item, ast.AnnAssign) or not isinstance(item.target, ast.Name):
            continue
        # comment block directly above, and a section banner like "# --- Hybrid index ---" anywhere above
        above, i = [], item.lineno - 2
        while i >= 0 and lines[i].strip().startswith("#"):
            above.append(lines[i].strip().lstrip("#").strip())
            i -= 1
        above.reverse()
        banner = next((a for a in above if a.startswith("---") and a.endswith("---")), None)
        if banner:
            section = banner.strip("- ").strip()
            above = [a for a in above if a != banner]
        inline = ""
        m = re.search(r"#\s*(.*)$", lines[item.lineno - 1])
        if m:
            inline = m.group(1).strip()
        default = ast.get_source_segment(src, item.value) if item.value is not None else "(required)"
        secret = bool(SECRET_NAME.search(item.target.id))
        if secret and default not in ("(required)", '""', "None", "''"):
            default = "(set in .env)"
        yield section, prefix + item.target.id.upper(), (ast.get_source_segment(src, item.annotation) or "").replace("|", "or"), default, " ".join(above + ([inline] if inline else []))


def render() -> str:
    out = [
        "# Configuration reference",
        "",
        "Generated from the settings classes by `python -m scripts.make_config_reference`. Do not edit by hand.",
        "A fresh install needs only the values in `.env.dev.example` / `.env.prod.example`; everything here has a default.",
        "",
    ]
    for title, path, cls, prefix in SOURCES:
        out += [f"## {title}", ""]
        current = None
        for section, name, ann, default, doc in _fields(path, cls, prefix):
            if section != current:
                current = section
                if section:
                    out += ["", f"### {section}", ""]
                out += ["| variable | type | default | what it does |", "|---|---|---|---|"]
            doc = doc.replace("|", "/")
            default = default.replace("|", "/")
            out.append(f"| `{name}` | `{ann}` | `{default}` | {doc} |")
        out.append("")
    return "\n".join(out).replace("|---|---|---|---|\n| variable", "|---|---|---|---|\n| variable") + "\n"


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--check", action="store_true")
    a = p.parse_args(argv)
    text = render()
    if a.check:
        return 0 if OUT.exists() and OUT.read_text() == text else 1
    OUT.write_text(text)
    print(f"wrote {OUT} ({text.count(chr(10))} lines)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
