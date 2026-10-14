"""Old and macro-enabled Office files (.doc .xls .ppt and friends) and OpenDocument sheets / slides, read by converting
them to the modern format with LibreOffice running headless, then through the same readers as any .docx / .xlsx / .pptx.

LibreOffice is the only free tool that reads the legacy binary formats faithfully (tables stay tables). It is installed in
the Docker images (build argument INSTALL_LEGACY_OFFICE, on by default). Without it these files are reported as skipped
with a reason that says what to install; everything else is unaffected.

Each conversion gets its own throw-away profile directory, so several workers can convert at the same time, and a hard
time limit, so a damaged file cannot hold a worker.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
from pathlib import Path

from app.scale.settings import get_scale_settings

log = logging.getLogger(__name__)

# source extension -> the modern format it is converted to
CONVERTIBLE = {
    "doc": "docx", "dot": "docx", "docm": "docx", "dotx": "docx",
    "xls": "xlsx", "xlt": "xlsx", "xlsm": "xlsx", "xltx": "xlsx", "ods": "xlsx",
    "ppt": "pptx", "pps": "pptx", "pot": "pptx", "pptm": "pptx", "ppsx": "pptx", "potx": "pptx", "odp": "pptx",
}


class ConversionError(Exception):
    pass


def soffice_path() -> str | None:
    return shutil.which("soffice") or shutil.which("libreoffice")


def available() -> bool:
    return soffice_path() is not None


def convert(path: str, ext: str, out_dir: str) -> tuple[str, str]:
    """-> (path of the converted file, its extension)."""
    target = CONVERTIBLE[ext.lower()]
    binary = soffice_path()
    if binary is None:
        raise ConversionError(
            f".{ext} files are read through LibreOffice, which is not installed here "
            "(the Docker images include it; or install libreoffice-writer / -calc / -impress)")
    timeout = get_scale_settings().legacy_convert_timeout_s
    with tempfile.TemporaryDirectory(prefix="lo-profile-") as profile:
        cmd = [binary, f"-env:UserInstallation=file://{profile}", "--headless", "--norestore", "--nolockcheck",
               "--convert-to", target, "--outdir", out_dir, path]
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=timeout, text=True)
        except subprocess.TimeoutExpired as exc:
            raise ConversionError(f"LibreOffice took more than {timeout:g} s on this file") from exc
    produced = Path(out_dir) / (Path(path).stem + "." + target)
    if proc.returncode != 0 or not produced.exists() or produced.stat().st_size == 0:
        log.warning("LibreOffice could not convert %s: %s", path, (proc.stderr or proc.stdout or "")[:300])
        raise ConversionError(f"LibreOffice could not read this .{ext} file (damaged or password protected?)")
    return str(produced), target
