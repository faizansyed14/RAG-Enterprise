"""Admin HTTP surface for sources and access groups (mounted with the rest of the scale API).

  GET    /api/scale/kinds                          connector kinds, with a config template for each
  GET    /api/scale/connections                    every source with its scan state and item counts
  POST   /api/scale/connections                    add a source (and queue its first scan)
  PATCH  /api/scale/connections/{id}               enable / disable / rename
  DELETE /api/scale/connections/{id}               remove a source that has no live files
  POST   /api/scale/connections/{id}/scan          queue a scan now
  GET    /api/scale/connections/{id}/problems      files that were skipped or failed, with the reason

  GET    /api/scale/principals                     which groups each login carries at query time
  POST   /api/scale/principals                     give a login a group / SID / user principal
  DELETE /api/scale/principals                     take one away

Everything here is admin-only. Credentials never enter the database: a connection's config names the
environment variable that holds the secret (``password_env``, ``client_secret_env`` ...), and a config that
carries a secret inline is refused.
"""

from __future__ import annotations

import asyncio
import os
import re
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.core.security import CurrentUser, require_admin
from app.scale import acl as acl_mod
from app.scale import managed, queue
from app.scale.connectors import CONNECTORS, build_connector
from app.scale.db import engine
from app.scale.settings import get_scale_settings

router = APIRouter(prefix="/api/scale", tags=["scale-sources"])

# kind -> (group, label, config template). Kinds not listed here still work; they get an empty template and the
# group "other" (docs/scale/CONNECTORS.md lists the config keys of every kind).
_RULES = [{"prefix": "Finance", "principals": ["group:finance"]}, {"prefix": "", "principals": ["*"]}]
_TEMPLATES: dict[str, tuple[str, str, dict]] = {
    "local": ("files", "Folder or mount", {"root": "/mnt/sources", "acl_rules": [{"prefix": "", "principals": ["*"]}]}),
    "smb": ("files", "Windows file share (SMB)", {
        "server": "fileserver.example.com", "share": "Documents", "base_path": "", "username": "svc-rag",
        "password_env": "SMB_PASSWORD", "acl_rules": _RULES}),
    "sharepoint": ("files", "SharePoint", {
        "tenant_id": "00000000-0000-0000-0000-000000000000", "client_id": "00000000-0000-0000-0000-000000000000",
        "client_secret_env": "SHAREPOINT_CLIENT_SECRET", "sites": ["https://contoso.sharepoint.com/sites/Finance"],
        "libraries": [], "access": {"mode": "source"}}),
    "onedrive": ("files", "OneDrive", {
        "tenant_id": "00000000-0000-0000-0000-000000000000", "client_id": "00000000-0000-0000-0000-000000000000",
        "client_secret_env": "ONEDRIVE_CLIENT_SECRET", "users": ["ana@contoso.com"], "access": {"mode": "source"}}),
    "graph": ("files", "Microsoft 365 drives by id", {
        "tenant_id": "00000000-0000-0000-0000-000000000000", "client_id": "00000000-0000-0000-0000-000000000000",
        "client_secret_env": "GRAPH_CLIENT_SECRET", "drive_ids": ["b!replace-with-the-drive-id"]}),
    "s3": ("files", "S3-compatible object storage", {"bucket": "my-bucket", "prefix": "", "acl_rules": _RULES}),
    "webdav": ("files", "WebDAV (Nextcloud, ownCloud ...)", {}),
    "sftp": ("files", "SFTP", {}),
    "ftp": ("files", "FTP / FTPS", {}),
    "web": ("web", "Website, sitemap or feed", {}),
    "sql": ("data", "SQL query (read-only)", {}),
}
_GROUP_OF_OTHERS = "other"

_SECRET_KEY = re.compile(r"(password|passwd|secret|token|api[_-]?key|private[_-]?key|credential)", re.I)


def _inline_secrets(value, path: str = "") -> list[str]:
    """Config keys that look like they carry a secret rather than the NAME of an environment variable."""
    found: list[str] = []
    if isinstance(value, dict):
        for key, inner in value.items():
            here = f"{path}.{key}" if path else str(key)
            if _SECRET_KEY.search(str(key)) and not str(key).endswith("_env") and inner not in ("", None, False):
                found.append(here)
            found.extend(_inline_secrets(inner, here))
    elif isinstance(value, list):
        for i, inner in enumerate(value):
            found.extend(_inline_secrets(inner, f"{path}[{i}]"))
    return found


def _check_local_root(config: dict) -> None:
    """A browser session must not be able to point a crawler at an arbitrary server path."""
    allowed = [os.path.realpath(r) for r in get_scale_settings().api_local_roots]
    root = os.path.realpath(str(config.get("root", "")))
    if not any(root == a or root.startswith(a + os.sep) for a in allowed):
        raise HTTPException(
            422, f"A folder source must be inside one of: {', '.join(allowed) or '(none configured)'}. "
                 "Mount the files there (SCALE_SOURCE_DIR) or change SCALE_API_LOCAL_ROOTS.")
    if not os.path.isdir(root):
        raise HTTPException(422, f"{config.get('root')!r} is not a folder inside the server.")


class ConnectionIn(BaseModel):
    kind: str
    name: str = Field(min_length=1, max_length=120)
    config: dict = Field(default_factory=dict)
    scan_now: bool = True


class ConnectionPatch(BaseModel):
    enabled: bool | None = None
    name: str | None = Field(default=None, min_length=1, max_length=120)


class PrincipalIn(BaseModel):
    username: str = Field(min_length=1, max_length=200)
    principal: str = Field(min_length=1, max_length=500)


@router.get("/kinds")
async def kinds(_admin: CurrentUser = Depends(require_admin)) -> dict:
    out = []
    for kind in sorted(CONNECTORS):
        group, label, template = _TEMPLATES.get(kind, (_GROUP_OF_OTHERS, kind.replace("_", " ").title(), {}))
        out.append({"kind": kind, "group": group, "label": label, "template": template})
    cfg = get_scale_settings()
    return {"kinds": out, "limits": {"upload_max_files": cfg.upload_max_files, "upload_max_file_mb": min(cfg.max_file_mb, cfg.upload_request_mb),
                                     "upload_request_mb": cfg.upload_request_mb}}


def _list_connections() -> list[dict]:
    with engine().connect() as conn:
        rows = conn.execute(
            text(
                "SELECT c.connection_id, c.kind, c.name, c.enabled, c.created_at, c.config, "
                "       s.last_scan_started, s.last_scan_finished, s.last_error "
                "FROM source_connections c LEFT JOIN connector_state s USING (connection_id) ORDER BY c.name"
            )
        ).all()
        counts: dict[str, dict[str, int]] = {}
        for cid, state, n in conn.execute(
            text("SELECT connection_id, state, count(*) FROM source_items WHERE NOT deleted GROUP BY 1, 2")
        ):
            counts.setdefault(str(cid), {})[state] = n
        queued = {
            r[0] for r in conn.execute(
                text("SELECT payload->>'connection_id' FROM scale_jobs WHERE kind = 'scan' AND status IN ('queued', 'running')")
            )
        }
    return [
        {
            "connection_id": str(r[0]), "kind": r[1], "name": r[2], "enabled": r[3], "created_at": r[4].isoformat(),
            "config": r[5], "last_scan_started": r[6].isoformat() if r[6] else None,
            "last_scan_finished": r[7].isoformat() if r[7] else None, "last_error": r[8],
            "items": counts.get(str(r[0]), {}), "scan_queued": str(r[0]) in queued,
        }
        for r in rows
    ]


class ConnectionTest(BaseModel):
    kind: str
    config: dict = Field(default_factory=dict)


@router.post("/connections/test")
async def test_connection(body: ConnectionTest, _admin: CurrentUser = Depends(require_admin)) -> dict:
    """Try a configuration without saving anything: sign in, find the libraries or drives it would scan, read a few file
    names, and say whether the account's permission data is readable. Reads only; the connector cannot write."""
    if body.kind not in CONNECTORS:
        raise HTTPException(422, f"Unknown source type {body.kind!r}")
    secrets = _inline_secrets(body.config)
    if secrets:
        raise HTTPException(422, f"Do not put secrets in the configuration ({', '.join(secrets)}). Name the environment variable instead.")
    try:
        connector = await asyncio.to_thread(build_connector, body.kind, body.config)
    except KeyError as exc:
        raise HTTPException(422, f"The configuration is missing {exc}") from None
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(422, f"The configuration was rejected: {str(exc)[:300]}") from None
    runner = getattr(connector, "test", None)
    if runner is None:
        raise HTTPException(422, "This source type has no connection test yet. Add it and watch the first scan instead.")
    try:
        return await asyncio.wait_for(asyncio.to_thread(runner), timeout=90)
    except asyncio.TimeoutError:
        raise HTTPException(504, "The test took longer than 90 seconds. Try a smaller list of sites or users.") from None


_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")


@router.get("/env-check")
async def env_check(names: str = Query(max_length=600), _admin: CurrentUser = Depends(require_admin)) -> dict:
    """For each environment variable NAME: is it set on the server? Only yes or no is returned, never a value."""
    out: dict[str, bool] = {}
    for name in [n.strip() for n in names.split(",") if n.strip()][:20]:
        if not _ENV_NAME.match(name):
            raise HTTPException(422, f"{name!r} is not a valid environment variable name")
        out[name] = bool(os.environ.get(name))
    return {"set": out}


# ---- connectors configured entirely from the server's environment: the screen only connects and disconnects ----


def _managed_row(kind: str) -> dict | None:
    with engine().connect() as conn:
        row = conn.execute(
            text("SELECT connection_id, name, enabled FROM source_connections WHERE kind = :k AND config ->> 'from_env' = 'true' "
                 "ORDER BY created_at LIMIT 1"), {"k": kind}).first()
    return {"connection_id": str(row[0]), "name": row[1], "enabled": row[2]} if row else None


@router.get("/managed")
async def managed_connectors(_admin: CurrentUser = Depends(require_admin)) -> dict:
    """Which environment variables each environment-configured connector reads (names and set / not set only) and whether
    it is connected. A value is never returned."""
    def _build() -> list[dict]:
        return [{"kind": k, "label": managed.LABELS[k], "env": managed.status(k), "problems": managed.problems(k),
                 "ready": not managed.problems(k), "connection": _managed_row(k)} for k in managed.LABELS]
    return {"connectors": await asyncio.to_thread(_build)}


@router.post("/managed/{kind}/connect")
async def managed_connect(kind: str, _admin: CurrentUser = Depends(require_admin)) -> dict:
    """Test the credentials found in the environment and, if they work, create (or switch back on) the source and queue a scan."""
    if not managed.is_managed(kind):
        raise HTTPException(404, "Unknown connector")
    issues = managed.problems(kind)
    if issues:
        raise HTTPException(422, "The server's .env is incomplete: " + "; ".join(issues) + ". Add the missing variables and restart the backend and worker.")
    config = {"from_env": True}
    try:
        connector = await asyncio.to_thread(build_connector, kind, config)
    except Exception as exc:  # noqa: BLE001 -- connectors raise their own error types for a bad config
        raise HTTPException(422, f"The settings in .env were rejected: {str(exc)[:300]}") from None
    try:
        report = await asyncio.wait_for(asyncio.to_thread(connector.test), timeout=90)
    except asyncio.TimeoutError:
        raise HTTPException(504, "The test took longer than 90 seconds. Try fewer sites or users in .env.") from None
    if not report.get("ok"):
        raise HTTPException(422, str(report.get("error") or "Microsoft 365 refused the credentials in .env."))

    def _connect() -> dict:
        existing = _managed_row(kind)
        if existing:
            _patch(uuid.UUID(existing["connection_id"]), ConnectionPatch(enabled=True))
            job = queue.enqueue("scan", {"connection_id": existing["connection_id"]}, dedupe_key=f"scan:{existing['connection_id']}")
            return {"connection_id": existing["connection_id"], "scan_job": job}
        return _create(ConnectionIn(kind=kind, name=managed.LABELS[kind], config=config))

    return {**await asyncio.to_thread(_connect), "report": report}


@router.post("/managed/{kind}/disconnect")
async def managed_disconnect(kind: str, _admin: CurrentUser = Depends(require_admin)) -> dict:
    """Stop scanning. Files already indexed stay searchable; connecting again resumes where it left off."""
    if not managed.is_managed(kind):
        raise HTTPException(404, "Unknown connector")
    existing = await asyncio.to_thread(_managed_row, kind)
    if existing is None:
        raise HTTPException(404, "This connector is not connected")
    await asyncio.to_thread(_patch, uuid.UUID(existing["connection_id"]), ConnectionPatch(enabled=False))
    return {"ok": True}


@router.get("/connections")
async def list_connections(_admin: CurrentUser = Depends(require_admin)) -> dict:
    return {"connections": await asyncio.to_thread(_list_connections)}


def _create(body: ConnectionIn) -> dict:
    from app.scale.sync import create_connection

    try:
        connection_id = create_connection(body.kind, body.name.strip(), body.config)
    except IntegrityError:
        raise HTTPException(409, f"A source named {body.name!r} already exists") from None
    job = queue.enqueue("scan", {"connection_id": connection_id}, dedupe_key=f"scan:{connection_id}") if body.scan_now else None
    return {"connection_id": connection_id, "scan_job": job}


@router.post("/connections", status_code=201)
async def create_connection_endpoint(body: ConnectionIn, _admin: CurrentUser = Depends(require_admin)) -> dict:
    if body.kind not in CONNECTORS:
        raise HTTPException(422, f"Unknown source type {body.kind!r}")
    secrets = _inline_secrets(body.config)
    if secrets:
        raise HTTPException(
            422, f"Do not put secrets in the configuration ({', '.join(secrets)}). Put the secret in the server's "
                 "environment and name the variable instead, for example \"password_env\": \"SMB_PASSWORD\".")
    if body.kind == "local":
        _check_local_root(body.config)
    try:
        await asyncio.to_thread(build_connector, body.kind, body.config)      # validates the config, no network
    except KeyError as exc:
        raise HTTPException(422, f"The configuration is missing {exc}") from None
    except ImportError as exc:
        raise HTTPException(422, f"This source type needs a package that is not installed: {exc}") from None
    except Exception as exc:  # noqa: BLE001 -- connectors raise their own error types for a bad config
        raise HTTPException(422, f"The configuration was rejected: {str(exc)[:300]}") from None
    return await asyncio.to_thread(_create, body)


def _patch(connection_id: uuid.UUID, body: ConnectionPatch) -> bool:
    sets, params = [], {"c": str(connection_id)}
    if body.enabled is not None:
        sets.append("enabled = :e")
        params["e"] = body.enabled
    if body.name is not None:
        sets.append("name = :n")
        params["n"] = body.name.strip()
    if not sets:
        return True
    with engine().begin() as conn:
        return conn.execute(
            text(f"UPDATE source_connections SET {', '.join(sets)} WHERE connection_id = :c"), params
        ).rowcount > 0


@router.patch("/connections/{connection_id}")
async def patch_connection(connection_id: uuid.UUID, body: ConnectionPatch, _admin: CurrentUser = Depends(require_admin)) -> dict:
    try:
        found = await asyncio.to_thread(_patch, connection_id, body)
    except IntegrityError:
        raise HTTPException(409, f"A source named {body.name!r} already exists") from None
    if not found:
        raise HTTPException(404, "No such source")
    return {"ok": True}


def _delete(connection_id: uuid.UUID) -> str:
    with engine().begin() as conn:
        exists = conn.execute(
            text("SELECT 1 FROM source_connections WHERE connection_id = :c"), {"c": str(connection_id)}
        ).first()
        if exists is None:
            return "missing"
        live = conn.execute(
            text("SELECT count(*) FROM source_items WHERE connection_id = :c AND NOT deleted"), {"c": str(connection_id)}
        ).scalar_one()
        if live:
            return "has_files"
        conn.execute(text("DELETE FROM source_connections WHERE connection_id = :c"), {"c": str(connection_id)})
    return "ok"


@router.delete("/connections/{connection_id}")
async def delete_connection(connection_id: uuid.UUID, _admin: CurrentUser = Depends(require_admin)) -> dict:
    outcome = await asyncio.to_thread(_delete, connection_id)
    if outcome == "missing":
        raise HTTPException(404, "No such source")
    if outcome == "has_files":
        raise HTTPException(
            409, "This source still has indexed files. Disable it to stop scanning; its documents stay searchable "
                 "until they are deleted at the source or removed from the library.")
    return {"ok": True}


@router.post("/connections/{connection_id}/scan")
async def scan_now(connection_id: uuid.UUID, _admin: CurrentUser = Depends(require_admin)) -> dict:
    def _run() -> str | None:
        with engine().connect() as conn:
            row = conn.execute(
                text("SELECT enabled FROM source_connections WHERE connection_id = :c"), {"c": str(connection_id)}
            ).first()
        if row is None:
            raise HTTPException(404, "No such source")
        if not row[0]:
            raise HTTPException(409, "This source is disabled")
        return queue.enqueue("scan", {"connection_id": str(connection_id)}, dedupe_key=f"scan:{connection_id}")

    job = await asyncio.to_thread(_run)
    return {"queued": job is not None}


@router.get("/connections/{connection_id}/problems")
async def problems(connection_id: uuid.UUID, state: str = Query("failed", pattern="^(failed|skipped)$"),
                   limit: int = Query(50, ge=1, le=200), _admin: CurrentUser = Depends(require_admin)) -> dict:
    def _run() -> list[dict]:
        with engine().connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT path, name, size_bytes, left(coalesce(error, ''), 300) FROM source_items "
                    "WHERE connection_id = :c AND state = :s AND NOT deleted ORDER BY modified_at DESC NULLS LAST LIMIT :l"
                ),
                {"c": str(connection_id), "s": state, "l": limit},
            ).all()
        return [{"path": r[0], "name": r[1], "size_bytes": r[2], "reason": r[3]} for r in rows]

    return {"items": await asyncio.to_thread(_run)}


def _principals() -> list[dict]:
    with engine().connect() as conn:
        rows = conn.execute(
            text(
                "SELECT u.username, p.principal, p.source, p.synced_at FROM user_principals p "
                "JOIN users u USING (user_id) ORDER BY lower(u.username), p.principal"
            )
        ).all()
    return [{"username": r[0], "principal": r[1], "source": r[2], "synced_at": r[3].isoformat() if r[3] else None} for r in rows]


@router.get("/principals")
async def list_principals(_admin: CurrentUser = Depends(require_admin)) -> dict:
    return {"principals": await asyncio.to_thread(_principals)}


@router.post("/principals", status_code=201)
async def add_principal(body: PrincipalIn, _admin: CurrentUser = Depends(require_admin)) -> dict:
    try:
        principal = acl_mod.normalize_principal(body.principal)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    if not principal or principal == acl_mod.EVERYONE:
        raise HTTPException(422, "Use a group:, sid: or user: principal")

    def _run() -> bool:
        with engine().begin() as conn:
            row = conn.execute(
                text("SELECT user_id FROM users WHERE lower(username) = lower(:u)"), {"u": body.username.strip()}
            ).first()
            if row is None:
                return False
            conn.execute(
                text(
                    "INSERT INTO user_principals (user_id, principal, source) VALUES (:u, :p, 'admin') "
                    "ON CONFLICT (user_id, principal) DO UPDATE SET synced_at = now()"
                ),
                {"u": row[0], "p": principal},
            )
        return True

    if not await asyncio.to_thread(_run):
        raise HTTPException(404, f"No user named {body.username!r}")
    return {"ok": True, "principal": principal}


@router.delete("/principals")
async def remove_principal(body: PrincipalIn, _admin: CurrentUser = Depends(require_admin)) -> dict:
    try:
        principal = acl_mod.normalize_principal(body.principal)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None

    def _run() -> int:
        with engine().begin() as conn:
            return conn.execute(
                text(
                    "DELETE FROM user_principals WHERE principal = :p AND user_id = "
                    "(SELECT user_id FROM users WHERE lower(username) = lower(:u))"
                ),
                {"p": principal, "u": body.username.strip()},
            ).rowcount

    if not await asyncio.to_thread(_run):
        raise HTTPException(404, "That login does not carry that principal")
    return {"ok": True}
