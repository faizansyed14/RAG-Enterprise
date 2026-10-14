"""Upload hardening: never trust the client's filename, content type, or size.
Everything here is pure and unit-tested; api/documents.py just calls it."""

import io
import json
import re
import unicodedata
import zipfile

from fastapi import HTTPException, UploadFile, status

# The content type stored on the object and served back via presigned URL comes from
# the validated extension, never from the client's header.
CONTENT_TYPES = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "csv": "text/csv",
    "eml": "message/rfc822",
    "txt": "text/plain",
    "json": "application/json",
    "xer": "text/plain",
    "py": "text/x-python",
    "md": "text/markdown",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "odt": "application/vnd.oasis.opendocument.text",
    "rtf": "application/rtf",
    "html": "text/html",
    "htm": "text/html",
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "tif": "image/tiff",
    "tiff": "image/tiff",
    "bmp": "image/bmp",
    "webp": "image/webp",
    "doc": "application/msword", "dot": "application/msword",
    "docm": "application/vnd.ms-word.document.macroEnabled.12",
    "dotx": "application/vnd.openxmlformats-officedocument.wordprocessingml.template",
    "xls": "application/vnd.ms-excel", "xlt": "application/vnd.ms-excel",
    "xlsm": "application/vnd.ms-excel.sheet.macroEnabled.12",
    "xltx": "application/vnd.openxmlformats-officedocument.spreadsheetml.template",
    "ods": "application/vnd.oasis.opendocument.spreadsheet",
    "ppt": "application/vnd.ms-powerpoint", "pps": "application/vnd.ms-powerpoint", "pot": "application/vnd.ms-powerpoint",
    "pptm": "application/vnd.ms-powerpoint.presentation.macroEnabled.12",
    "ppsx": "application/vnd.openxmlformats-officedocument.presentationml.slideshow",
    "potx": "application/vnd.openxmlformats-officedocument.presentationml.template",
    "odp": "application/vnd.oasis.opendocument.presentation",
}
# Anything else that is accepted is text or code and is stored as text/plain.

# Legacy / macro / template Office files are converted by LibreOffice, which decides what it can read; here only the
# container is checked (old binary OLE2 file, or a zip package), with a text fallback for the "xls" that is really HTML.
_LEGACY_OFFICE = frozenset(
    "doc dot docm dotx xls xlt xlsm xltx ods ppt pps pot pptm ppsx potx odp".split()
)
_OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"

# Scanned pages and photos: read by the local OCR / layout models (SCALE_DOCPARSE_MODE), so they are accepted only when
# those are switched on and installed.
IMAGE_TYPES = frozenset({"png", "jpg", "jpeg", "tif", "tiff", "bmp", "webp"})
_IMAGE_MAGIC = {
    "png": (b"\x89PNG\r\n\x1a\n",), "jpg": (b"\xff\xd8\xff",), "jpeg": (b"\xff\xd8\xff",),
    "tif": (b"II*\x00", b"MM\x00*"), "tiff": (b"II*\x00", b"MM\x00*"), "bmp": (b"BM",),
}

_MAX_FILENAME = 150
_MAX_ZIP_UNCOMPRESSED = 500 * 1024 * 1024
_MAX_ZIP_RATIO = 200


def _bad(message: str) -> HTTPException:
    return HTTPException(status.HTTP_400_BAD_REQUEST, message)


def sanitize_filename(raw: str | None) -> str:
    """Basename only, no control characters or path separators, bounded length,
    extension preserved. Safe for the DB, the storage key and the UI."""
    name = unicodedata.normalize("NFKC", raw or "")
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    name = re.sub(r"[\x00-\x1f\x7f]", "", name).strip().strip(".")
    name = re.sub(r"[^\w.\- ()\[\]]", "_", name)
    if len(name) > _MAX_FILENAME:
        stem, dot, ext = name.rpartition(".")
        name = (stem[: _MAX_FILENAME - len(ext) - 1] + "." + ext) if dot else name[:_MAX_FILENAME]
    return name or "upload"


def extension_of(filename: str) -> str:
    return filename.rsplit(".", 1)[-1].lower() if "." in filename else ""


async def read_capped(file: UploadFile, max_bytes: int) -> bytes:
    """Read the upload in chunks and stop as soon as it exceeds the cap."""
    chunks: list[bytes] = []
    total = 0
    while chunk := await file.read(1024 * 1024):
        total += len(chunk)
        if total > max_bytes:
            raise HTTPException(
                status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, f"File too large (limit {max_bytes // (1024 * 1024)} MB)"
            )
        chunks.append(chunk)
    return b"".join(chunks)


def _check_zip(data: bytes, required: tuple[str, ...], label: str) -> None:
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise _bad(f"That doesn't look like a valid {label} file") from exc
    with archive:
        names = set(archive.namelist())
        if not all(part in names for part in required):
            raise _bad(f"That doesn't look like a valid {label} file")
        infos = archive.infolist()
        total = sum(info.file_size for info in infos)
        if total > _MAX_ZIP_UNCOMPRESSED or total > max(len(data), 1) * _MAX_ZIP_RATIO:
            raise _bad(f"That {label} file expands to an unreasonable size")


def validate_content(ext: str, data: bytes) -> None:
    """Check the bytes really are what the extension claims. Raises HTTP 400."""
    if not data:
        raise _bad("The file is empty")
    if ext == "pdf":
        if b"%PDF-" not in data[:1024]:
            raise _bad("That doesn't look like a valid PDF file")
    elif ext == "docx":
        _check_zip(data, ("[Content_Types].xml", "word/document.xml"), "Word")
    elif ext == "xlsx":
        _check_zip(data, ("[Content_Types].xml", "xl/workbook.xml"), "Excel")
    elif ext == "pptx":
        _check_zip(data, ("[Content_Types].xml", "ppt/presentation.xml"), "PowerPoint")
    elif ext == "odt":
        _check_zip(data, ("mimetype", "content.xml"), "OpenDocument text")
    elif ext in IMAGE_TYPES:
        if ext == "webp":
            ok = data[:4] == b"RIFF" and data[8:12] == b"WEBP"
        else:
            ok = data.startswith(_IMAGE_MAGIC[ext])
        if not ok:
            raise _bad(f"That doesn't look like a valid .{ext} image")
    elif ext in _LEGACY_OFFICE:
        if data.startswith(b"PK"):
            _check_zip(data, (), "Office")
        elif not (data.startswith(_OLE_MAGIC) or data.lstrip()[:1] in (b"<", b"{") or b"\x00" not in data[:8192]):
            raise _bad(f"That doesn't look like a valid .{ext} file")
    elif ext == "rtf":
        if not data.lstrip().startswith(b"{\\rtf"):
            raise _bad("That doesn't look like a valid RTF file")
    else:
        if b"\x00" in data[:8192]:
            raise _bad(f"That doesn't look like a valid .{ext} text file")
        head = data[:4096].lstrip(b"\xef\xbb\xbf \t\r\n")
        if ext == "json":
            try:
                json.loads(data.decode("utf-8-sig"))
            except (UnicodeDecodeError, ValueError) as exc:
                raise _bad("That file is not valid JSON") from exc
        elif ext == "xer":
            if not head.startswith(b"ERMHDR"):
                raise _bad("That doesn't look like a valid Primavera XER file")
        elif ext == "eml":
            if not re.search(rb"(?im)^(received|from|to|subject|date|mime-version|message-id):", data[:4096]):
                raise _bad("That doesn't look like a valid email (.eml) file")
