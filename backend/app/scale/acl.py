"""Access control: who may read which document, enforced where the search happens.

Principals are plain strings:

    *                everyone who is signed in (what every pre-existing upload carries)
    user:<id|upn>    one person            (lower-cased)
    group:<id>       an AD / Entra group   (lower-cased)
    sid:S-1-5-...    an NTFS security id   (upper-cased)

A document is readable when ANY of its principals is among the caller's principals. An empty ACL
means nobody: the system fails closed. The same rule is applied twice -- inside the Qdrant query
(payload filter on `acl`) and against Postgres (`document_access`) -- so a stale or missing index
payload can narrow results but cannot widen them.
"""

from __future__ import annotations

import logging
import uuid
from typing import Iterable

from sqlalchemy import text

from app.scale.db import engine
from app.scale.settings import get_scale_settings

log = logging.getLogger(__name__)

EVERYONE = "*"

# Lightweight table handle so API queries can join on document_access without a full ORM model.
from sqlalchemy import Column, MetaData, Table, Text  # noqa: E402
from sqlalchemy.dialects.postgresql import UUID as PG_UUID  # noqa: E402

document_access_table = Table(
    "document_access", MetaData(), Column("document_id", PG_UUID(as_uuid=True)), Column("principal", Text)
)


def grant_everyone(document_id: uuid.UUID | str) -> None:
    """A document uploaded through the UI is readable by every signed-in user, as it was before access
    control existed. (Source-backed documents get their principals from the source instead.)"""
    with engine().begin() as conn:
        conn.execute(
            text("INSERT INTO document_access (document_id, principal) VALUES (:d, :p) ON CONFLICT DO NOTHING"),
            {"d": str(document_id), "p": EVERYONE},
        )


def normalize_principal(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    if value == EVERYONE:
        return EVERYONE
    scheme, sep, rest = value.partition(":")
    if not sep:
        raise ValueError(f"principal {value!r} needs a scheme: user:, group: or sid:")
    scheme = scheme.lower()
    rest = rest.strip()
    if scheme == "sid":
        return f"sid:{rest.upper()}"
    if scheme in ("user", "group"):
        return f"{scheme}:{rest.lower()}"
    raise ValueError(f"unknown principal scheme {scheme!r}")


def normalize_acl(values: Iterable[str]) -> list[str]:
    out: list[str] = []
    for value in values:
        norm = normalize_principal(value)
        if norm and norm not in out:
            out.append(norm)
    return sorted(out)


def principals_for_user(user_id: uuid.UUID | str, username: str) -> set[str]:
    """Everything this login may match at query time: '*', the user's id and username, and any
    group / SID principals mapped to them in user_principals (filled by SSO login or the CLI)."""
    cap = get_scale_settings().max_principals
    principals = {EVERYONE, f"user:{str(user_id).lower()}"}
    if username:
        principals.add(f"user:{username.strip().lower()}")
    with engine().connect() as conn:
        rows = conn.execute(
            text("SELECT principal FROM user_principals WHERE user_id = :u ORDER BY principal LIMIT :cap"),
            {"u": str(user_id), "cap": cap},
        ).all()
    if len(rows) >= cap:
        # Deterministic (sorted) truncation, and loud: a user with more principals than the cap silently
        # loses access to documents that only the dropped principals grant.
        log.warning("user %s has >= %d principals; the rest are ignored (raise SCALE_MAX_PRINCIPALS)", user_id, cap)
    principals.update(normalize_principal(r[0]) for r in rows)
    principals.discard("")
    return principals


def visible_document_ids(document_ids: list[uuid.UUID | str], principals: set[str]) -> set[str]:
    """Of these documents, the ones the principals may read. One indexed query."""
    if not document_ids or not principals:
        return set()
    with engine().connect() as conn:
        rows = conn.execute(
            text(
                "SELECT DISTINCT document_id::text FROM document_access "
                "WHERE document_id = ANY(CAST(:ids AS uuid[])) AND principal = ANY(:principals)"
            ),
            {"ids": [str(i) for i in document_ids], "principals": sorted(principals)},
        ).all()
    return {r[0] for r in rows}


def can_read(document_id: uuid.UUID | str, principals: set[str]) -> bool:
    return str(document_id) in visible_document_ids([document_id], principals)


def recompute_document_access(conn, document_id: uuid.UUID | str) -> list[str]:
    """Rebuild document_access for a document from its live source items.

    A document reached from several places (identical bytes saved in two folders) is readable by
    the UNION of those places' ACLs -- otherwise whoever ingested second would lose access to a file
    they can open at the source. A document with no live source item is left untouched (manual
    uploads keep '*'); a document whose every source item was deleted is made unreadable.
    """
    rows = conn.execute(
        text("SELECT acl, deleted FROM source_items WHERE document_id = :d"), {"d": str(document_id)}
    ).all()
    if not rows:
        return []
    union: set[str] = set()
    for acl, deleted in rows:
        if not deleted:
            union.update(acl or [])
    conn.execute(text("DELETE FROM document_access WHERE document_id = :d"), {"d": str(document_id)})
    for principal in sorted(union):
        conn.execute(
            text("INSERT INTO document_access (document_id, principal) VALUES (:d, :p)"),
            {"d": str(document_id), "p": principal},
        )
    return sorted(union)


def qdrant_acl_filter(principals: set[str]):
    """Payload filter for the vector index: chunk.acl must share at least one principal with the caller."""
    from qdrant_client import models as qm

    return qm.FieldCondition(key="acl", match=qm.MatchAny(any=sorted(principals)))
