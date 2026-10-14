"""Local storage / mounted folder connector. Also the right adapter for an on-prem application that
can export files to a directory (point `root` at the export)."""

from __future__ import annotations

import os
from typing import BinaryIO

from app.scale.connectors.crawl import CrawlConnector, FsEntry


class LocalFS:
    def __init__(self, root: str) -> None:
        self.root = os.path.abspath(root)

    def _resolve(self, rel: str) -> str:
        path = os.path.realpath(os.path.join(self.root, rel))
        if path != self.root and not path.startswith(self.root + os.sep):
            raise PermissionError(f"path escapes the connector root: {rel}")
        return path

    def scandir(self, rel_dir: str) -> list[FsEntry]:
        out = []
        with os.scandir(self._resolve(rel_dir)) as it:
            for e in it:
                if e.is_symlink():
                    continue  # never follow links out of the root
                if e.is_dir(follow_symlinks=False):
                    out.append(FsEntry(e.name, True))
                elif e.is_file(follow_symlinks=False):
                    st = e.stat(follow_symlinks=False)
                    out.append(FsEntry(e.name, False, st.st_size, st.st_mtime_ns))
        return out

    def open(self, rel_path: str) -> BinaryIO:
        return open(self._resolve(rel_path), "rb")


class LocalFolderConnector(CrawlConnector):
    kind = "local"

    def __init__(self, config: dict) -> None:
        super().__init__(LocalFS(config["root"]), config)
