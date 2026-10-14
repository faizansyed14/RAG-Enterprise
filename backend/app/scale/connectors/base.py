"""The one contract every source speaks.

A connector turns "a place that holds files" into a stream of Changes:

    upsert  this file exists, at this version, readable by these principals
    delete  this file is gone

and can open a file's bytes on demand. Everything downstream (queue, parsing, chunking, indexing,
access control) is identical for every source, which is what keeps adding the 5th source cheap.

Two scan styles exist and both fit the same interface:
  * delta   (Microsoft Graph): the source tells us what changed since a saved token.
  * crawl   (folders, SMB): we enumerate everything; `complete_scope` on the last batch tells the sync
            layer that any item under that scope NOT seen in this scan has been deleted at the source.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from datetime import datetime
from typing import BinaryIO, Literal, Protocol

from app.scale.acl import normalize_acl


@dataclass
class SourceFile:
    external_id: str           # stable id at the source (Graph item id, relative path, ...)
    path: str                  # human-readable path used in citations and as search context
    name: str
    size: int
    modified_at: datetime | None
    version: str               # changes iff the content changes (etag / mtime+size / hash)
    acl: list[str]             # normalized principals; empty => nobody can read it
    mime: str = ""
    extra: dict = field(default_factory=dict)


@dataclass
class Change:
    kind: Literal["upsert", "delete"]
    external_id: str
    file: SourceFile | None = None
    # True when only the permissions changed (version identical): no re-parse, just rewrite the ACL.
    acl_only: bool = False


@dataclass
class ScanBatch:
    changes: list[Change]
    cursor: dict               # persist this once the batch is safely recorded; resume from it
    done: bool = False
    # Crawl style / full resync: on the batch that finishes an authoritative enumeration, every item whose
    # external_id starts with this prefix and was NOT seen in the scan is a deletion. "" = the whole
    # connection; None = this batch is not an authoritative listing (e.g. an incremental delta).
    complete_scope: str | None = None


class Connector(Protocol):
    kind: str

    def scan(self, cursor: dict) -> "iter[ScanBatch]": ...

    def open(self, file: SourceFile) -> BinaryIO: ...

    def refresh_acl(self, file: SourceFile) -> list[str]: ...


class AclRules:
    """Folder -> principals mapping, first the longest matching prefix wins.

    This is the honest minimum for sources that do not expose per-file permissions to us (a mounted
    folder, an on-prem export, an SMB share before NTFS reading is added): the client states "everything
    under Finance/ is readable by group:finance" once, and it is applied to every file below it. Files
    with no matching rule get `default` (empty => nobody, i.e. fail closed).
    """

    def __init__(self, rules: list[dict] | None = None, default: list[str] | None = None) -> None:
        self.rules = sorted(
            ({"prefix": r["prefix"].strip("/\\").replace("\\", "/"), "principals": normalize_acl(r["principals"])}
             for r in (rules or [])),
            key=lambda r: len(r["prefix"]), reverse=True,
        )
        self.default = normalize_acl(default or [])

    def for_path(self, relpath: str) -> list[str]:
        rel = relpath.strip("/\\").replace("\\", "/")
        for rule in self.rules:
            prefix = rule["prefix"]
            if prefix == "" or rel == prefix or rel.startswith(prefix + "/") or fnmatch.fnmatch(rel, prefix):
                return list(rule["principals"])
        return list(self.default)
