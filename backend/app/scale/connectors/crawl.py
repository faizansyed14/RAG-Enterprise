"""Shared crawl engine for sources we can only enumerate (mounted folders, SMB shares).

Enumeration is in sorted order and resumable by path, so a crashed multi-hour scan of a huge share
continues where it stopped. Unchanged files are cheap: the sync layer compares `version`
(mtime+size) with what it already indexed and queues nothing.
"""

from __future__ import annotations

import mimetypes
import posixpath
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import BinaryIO, Iterator, Protocol

from app.scale.connectors.base import AclRules, Change, ScanBatch, SourceFile


@dataclass
class FsEntry:
    name: str
    is_dir: bool
    size: int = 0
    mtime_ns: int = 0


class FileSystem(Protocol):
    def scandir(self, rel_dir: str) -> list[FsEntry]: ...
    def open(self, rel_path: str) -> BinaryIO: ...


class CrawlConnector:
    kind = "crawl"

    def __init__(self, fs: FileSystem, config: dict) -> None:
        self.fs = fs
        self.rules = AclRules(config.get("acl_rules"), config.get("default_acl"))
        self.batch_size = int(config.get("batch_size", 500))
        self.skip_hidden = bool(config.get("skip_hidden", True))
        self.skip_names = {n.lower() for n in config.get("skip_names", ["thumbs.db", "desktop.ini", "~$*"])}

    def _skipped(self, name: str) -> bool:
        lowered = name.lower()
        if self.skip_hidden and lowered.startswith("."):
            return True
        return any(lowered == s or (s.endswith("*") and lowered.startswith(s[:-1])) for s in self.skip_names)

    def _walk(self, rel_dir: str, after: str | None) -> Iterator[tuple[str, FsEntry]]:
        try:
            entries = sorted(self.fs.scandir(rel_dir), key=lambda e: e.name)
        except (PermissionError, FileNotFoundError, OSError):
            return
        for entry in entries:
            if self._skipped(entry.name):
                continue
            rel = posixpath.join(rel_dir, entry.name) if rel_dir else entry.name
            if entry.is_dir:
                if after is not None and not after.startswith(rel + "/") and (rel + "/") < after[: len(rel) + 1]:
                    continue  # the whole directory sorts before the resume point
                yield from self._walk(rel, after)
            elif after is None or rel > after:
                yield rel, entry

    def scan(self, cursor: dict) -> Iterator[ScanBatch]:
        after = cursor.get("after")
        batch: list[Change] = []
        last = after
        for rel, entry in self._walk("", after):
            sf = SourceFile(
                external_id=rel, path=rel, name=entry.name, size=entry.size,
                modified_at=datetime.fromtimestamp(entry.mtime_ns / 1e9, tz=timezone.utc) if entry.mtime_ns else None,
                version=f"{entry.mtime_ns}:{entry.size}", acl=self.rules.for_path(rel),
                mime=mimetypes.guess_type(entry.name)[0] or "",
            )
            batch.append(Change("upsert", rel, sf))
            last = rel
            if len(batch) >= self.batch_size:
                yield ScanBatch(batch, {"after": last})
                batch = []
        # A resumed scan only saw the tail of the tree, so it cannot declare the rest deleted.
        yield ScanBatch(batch, {}, done=True, complete_scope=None if after is not None else "")

    def open(self, file: SourceFile) -> BinaryIO:
        return self.fs.open(file.external_id)

    def refresh_acl(self, file: SourceFile) -> list[str]:
        return self.rules.for_path(file.path)
