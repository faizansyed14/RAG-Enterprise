"""Upload files straight into a source (admin).

  POST   /api/scale/uploads                      multipart: name, access, principals, files[]   create the source on first use, add files
  GET    /api/scale/uploads/{id}/files           the files in an uploaded source, with their index state
  DELETE /api/scale/uploads/{id}/files?name=     remove one file (it is removed from search at the next scan, queued now)

An uploaded source is an ordinary `local` source whose folder this API fills: the worker scans it like any folder, reads
the files locally (no language model, no API cost) and indexes them. The folder lives under <RAG_STORAGE_PATH>/scale-uploads
(or SCALE_UPLOAD_DIR), a volume the API and the worker both mount; with several hosts it must be a shared volume.

Who may read the files is decided once, when the source is created, and is stored as folder rules the scan applies to every
file: "everyone" (every signed-in user) or a list of principals such as group:finance. Nothing is public by default: the
form has no preselected choice that opens files up.
"""

from __future__ import annotations

import asyncio
import os
import re
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.core.config import get_settings
from app.core.security import CurrentUser, require_admin
from app.scale import queue
from app.scale.acl import normalize_acl
from app.scale.db import engine
from app.scale.settings import get_scale_settings

router = APIRouter(prefix="/api/scale/uploads", tags=["scale-uploads"])

_BAD_CHARS = re.compile(r'[\x00-\x1f\x7f/\\:*?"<>|]')
_CHUNK = 1 << 20


def upload_base() -> Path:
    cfg = get_scale_settings()
    base = Path(cfg.upload_dir) if cfg.upload_dir else Path(get_settings().rag_storage_path) / "scale-uploads"
    base.mkdir(parents=True, exist_ok=True)
    return base.resolve()


def safe_name(raw: str | None) -> str | None:
    """A plain file name, or None. Never a path: directories, drive letters and traversal are all cut off."""
    name = (raw or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
    name = _BAD_CHARS.sub("_", name)
    if not name or name in (".", "..") or name.startswith(".") or name.startswith("~$") or len(name) > 200:
        return None
    return name


def _rules(access: str, principals: str) -> list[dict]:
    if access == "everyone":
        return [{"prefix": "", "principals": ["*"]}]
    if access == "principals":
        items = [p.strip() for p in re.split(r"[,\n]", principals) if p.strip()]
        try:
            normalized = normalize_acl(items)
        except ValueError as exc:
            raise HTTPException(422, f"{exc}. Write them like group:finance, user:alice or sid:S-1-5-21-...") from None
        if not normalized:
            raise HTTPException(422, "List at least one group or user who may read these files, or choose everyone.")
        return [{"prefix": "", "principals": normalized}]
    raise HTTPException(422, "Choose who may read these files: everyone, or specific groups.")


def _find(name: str):
    with engine().connect() as conn:
        return conn.execute(
            text("SELECT connection_id, config FROM source_connections WHERE name = :n AND kind = 'local'"), {"n": name}
        ).first()


def _managed_root(connection_id: uuid.UUID) -> tuple[str, Path]:
    with engine().connect() as conn:
        row = conn.execute(
            text("SELECT name, config FROM source_connections WHERE connection_id = :c AND kind = 'local'"), {"c": str(connection_id)}
        ).first()
    if row is None or not (row[1] or {}).get("managed_upload"):
        raise HTTPException(404, "No uploaded source with that id")
    root = Path(row[1]["root"]).resolve()
    if upload_base() not in root.parents:
        raise HTTPException(404, "No uploaded source with that id")
    return row[0], root


async def _store(root: Path, upload: UploadFile, name: str, max_bytes: int) -> int:
    """Stream one upload to disk next to its final name, then rename: a half-written file is never scanned."""
    root.mkdir(parents=True, exist_ok=True)
    tmp = root / f".part-{uuid.uuid4().hex}"
    size = 0
    try:
        with open(tmp, "wb") as fh:
            while chunk := await upload.read(_CHUNK):
                size += len(chunk)
                if size > max_bytes:
                    raise ValueError(f"larger than the {max_bytes >> 20} MB limit")
                await asyncio.to_thread(fh.write, chunk)
        if size == 0:
            raise ValueError("the file is empty")
        os.replace(tmp, root / name)
        return size
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)


@router.post("", status_code=201)
async def upload(
    name: str = Form(min_length=1, max_length=120),
    access: str = Form(""),
    principals: str = Form(""),
    files: list[UploadFile] = File(...),
    _admin: CurrentUser = Depends(require_admin),
) -> dict:
    cfg = get_scale_settings()
    name = name.strip()
    if not name:
        raise HTTPException(422, "Give this source a name.")
    if len(files) > cfg.upload_max_files:
        raise HTTPException(422, f"At most {cfg.upload_max_files} files per upload. Upload the rest in another batch.")

    existing = await asyncio.to_thread(_find, name)
    if existing is not None and not (existing[1] or {}).get("managed_upload"):
        raise HTTPException(409, f"A source named {name!r} already exists and is not an uploaded-files source.")
    if existing is None:
        rules = _rules(access, principals)                    # refuse before anything is written
        root = upload_base() / uuid.uuid4().hex
        connection_id = None
    else:
        connection_id = str(existing[0])
        root = Path(existing[1]["root"]).resolve()
        if upload_base() not in root.parents:
            raise HTTPException(409, "This source's folder is outside the upload area.")

    max_bytes = min(cfg.max_file_mb, cfg.upload_request_mb) * 1024 * 1024
    saved: list[dict] = []
    rejected: list[dict] = []
    for upload_file in files:
        clean = safe_name(upload_file.filename)
        if clean is None:
            rejected.append({"name": upload_file.filename or "(no name)", "reason": "not a usable file name"})
            continue
        try:
            size = await _store(root, upload_file, clean, max_bytes)
        except ValueError as exc:
            rejected.append({"name": clean, "reason": str(exc)})
            continue
        saved.append({"name": clean, "size": size})

    if not saved:
        if existing is None:
            try:
                root.rmdir()
            except OSError:
                pass
        raise HTTPException(422, "No file was saved: " + "; ".join(f"{r['name']}: {r['reason']}" for r in rejected[:5]))

    if connection_id is None:
        from app.scale.sync import create_connection

        try:
            connection_id = await asyncio.to_thread(
                create_connection, "local", name, {"root": str(root), "acl_rules": rules, "managed_upload": True})
        except IntegrityError:
            raise HTTPException(409, f"A source named {name!r} already exists") from None
    job = await asyncio.to_thread(queue.enqueue, "scan", {"connection_id": connection_id}, dedupe_key=f"scan:{connection_id}")
    return {"connection_id": connection_id, "created": existing is None, "saved": saved, "rejected": rejected, "scan_queued": job is not None}


@router.get("/{connection_id}/files")
async def list_files(connection_id: uuid.UUID, _admin: CurrentUser = Depends(require_admin)) -> dict:
    name, root = await asyncio.to_thread(_managed_root, connection_id)

    def _list() -> list[dict]:
        states: dict[str, str] = {}
        with engine().connect() as conn:
            for path, state, err in conn.execute(
                text("SELECT path, state, left(coalesce(error, ''), 120) FROM source_items WHERE connection_id = :c AND NOT deleted"),
                {"c": str(connection_id)},
            ):
                states[path] = state if not err else f"{state}: {err}"
        out = []
        if root.is_dir():
            for entry in sorted(root.iterdir(), key=lambda p: p.name.lower()):
                if entry.is_file() and not entry.is_symlink() and not entry.name.startswith("."):
                    st = entry.stat()
                    out.append({"name": entry.name, "size": st.st_size, "modified": st.st_mtime, "state": states.get(entry.name, "waiting")})
        return out

    return {"name": name, "files": await asyncio.to_thread(_list)}


@router.delete("/{connection_id}/files")
async def delete_file(connection_id: uuid.UUID, name: str = Query(min_length=1, max_length=200), _admin: CurrentUser = Depends(require_admin)) -> dict:
    _, root = await asyncio.to_thread(_managed_root, connection_id)
    clean = safe_name(name)
    target = (root / clean).resolve() if clean else None
    if target is None or target.parent != root or not target.is_file():
        raise HTTPException(404, "No such file in this source")
    target.unlink()
    job = await asyncio.to_thread(queue.enqueue, "scan", {"connection_id": str(connection_id)}, dedupe_key=f"scan:{connection_id}")
    return {"removed": clean, "scan_queued": job is not None}
