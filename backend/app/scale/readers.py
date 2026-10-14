"""One place that turns any non-PDF file into the "blocks" every later step already understands
({"type": "heading" | "paragraph" | "table" | "code" | "page_break"}): the upload path and the connector path both call
`blocks_for`, so a file reads the same whichever way it arrived.

  built in, no extra software   pptx odt rtf html htm  (app/scale/formats.py)
  through LibreOffice           doc dot docm xls xlt xlsm ods ppt pps pptm odp ...  (app/scale/legacy_office.py)
  as text                       log xml yaml ini sql sh js ts java c cpp go rs ... (code-like text keeps its layout)
                                markdown / rst / adoc are read as Markdown
Types the original ingesters own (docx xlsx csv eml txt json xer py md) are not handled here.
"""

from __future__ import annotations

from pathlib import Path

from app.scale import legacy_office
from app.scale.formats import BUILTIN_TYPES, UnreadableFile, extract_blocks as builtin_blocks

class Unsupported(UnreadableFile):
    """A binary file of a kind nothing here can read (audio, video, executables, archives ...)."""


# Never read or even downloaded: known binary kinds. Anything not listed here and not registered is sniffed once downloaded.
BINARY_TYPES = frozenset(
    "zip 7z rar tar gz tgz bz2 xz iso dmg exe dll so dylib bin msi class jar pyc o a lib "
    "mp3 wav flac aac ogg m4a wma mp4 mov avi mkv wmv flv webm "
    "gif ico psd ai eps heic heif svgz raw cr2 nef dwg dxf dwf sldprt step stp "
    "db sqlite mdb accdb pst ost nsf parquet npy npz pkl pickle h5 hdf5 ttf otf woff woff2 "
    "enc gpg pgp".split()
)

CODE_LIKE_TYPES = frozenset(
    "log xml yaml yml ini cfg conf toml properties env sql sh bash bat cmd ps1 js jsx mjs ts tsx java kt scala c h cpp cc hpp "
    "cs go rs rb php swift lua pl r css scss less vue svelte gradle tex tsv".split()
)
MARKDOWN_LIKE_TYPES = frozenset({"markdown", "rst", "adoc"})
TEXT_TYPES = CODE_LIKE_TYPES | MARKDOWN_LIKE_TYPES
CONVERTIBLE_TYPES = frozenset(legacy_office.CONVERTIBLE)
OTHER_TYPES = frozenset(BUILTIN_TYPES) | CONVERTIBLE_TYPES | TEXT_TYPES


def blocks_for(path: str, ext: str, tmp_dir: str) -> list[dict]:
    """Blocks for a file of one of OTHER_TYPES. Raises UnreadableFile with a message fit to show an administrator."""
    ext = ext.lower()
    if ext in BUILTIN_TYPES:
        return builtin_blocks(path, ext)
    if ext in CONVERTIBLE_TYPES:
        try:
            converted, new_ext = legacy_office.convert(path, ext, tmp_dir)
        except legacy_office.ConversionError as exc:
            raise UnreadableFile(str(exc)) from exc
        return _modern_blocks(converted, new_ext)
    if ext in MARKDOWN_LIKE_TYPES:
        from app.ingestion import markdown_ingest

        return markdown_ingest.extract_blocks(path)
    if ext in CODE_LIKE_TYPES:
        from app.ingestion import python_ingest

        return python_ingest.extract_blocks(path)
    # An extension nobody registered (.cfg2, .rpt, no extension at all): text a person could read is indexed as text,
    # anything else is a binary this system cannot read.
    if looks_like_text(path):
        from app.ingestion import python_ingest

        return python_ingest.extract_blocks(path)
    raise Unsupported(f"unsupported_type:{ext or 'none'}")


def _modern_blocks(path: str, ext: str) -> list[dict]:
    if ext == "docx":
        from app.ingestion import docx_ingest

        return docx_ingest.extract_blocks(path)
    if ext == "xlsx":
        from app.ingestion import xlsx_ingest

        return xlsx_ingest.extract_blocks(path)
    return builtin_blocks(path, ext)


def looks_like_text(path: str, sample: int = 65536) -> bool:
    """For a file with an extension nobody registered: is it text a person could read? (No NUL bytes and it decodes.)"""
    data = Path(path).read_bytes()[:sample]
    if not data or b"\x00" in data:
        return False
    try:
        data.decode("utf-8")
    except UnicodeDecodeError as exc:
        if exc.start < len(data) - 4:             # a cut-off multi-byte character at the sample edge is fine
            return False
    return True
