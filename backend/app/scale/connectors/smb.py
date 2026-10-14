"""SMB / CIFS file shares (Windows file servers, NAS) through `smbprotocol` (pip install smbprotocol).

Crawl style, same engine as the local folder connector. Permissions: this version maps folders to
principals with `acl_rules` (AclRules in base.py) -- the client states once that "Finance\\" is readable
by group:finance. It does NOT read NTFS security descriptors yet. That is the largest known gap in this
connector, and the reason is practical: faithful NTFS ACL evaluation needs SID -> AD group resolution
against the client's directory. The extension point is `SmbConnector.refresh_acl`; the UPGRADE_PROMPT
describes the exact acceptance test for it.

Not exercised against a real SMB server by the author of this pack (the tests use an in-memory fake
with the same shape as smbclient). Run it against a test share first.
"""

from __future__ import annotations

import posixpath
from typing import BinaryIO

from app.scale.connectors.crawl import CrawlConnector, FsEntry


class SmbFS:
    def __init__(self, server: str, share: str, base_path: str = "", username: str = "", password: str = "",
                 smbclient_module=None) -> None:
        if smbclient_module is None:
            import smbclient as smbclient_module  # type: ignore[no-redef]
        self._smb = smbclient_module
        self.server, self.share = server, share
        self.base = base_path.strip("/\\")
        if username:
            self._smb.register_session(server, username=username, password=password)

    def _unc(self, rel: str) -> str:
        parts = [p for p in (self.base, rel) if p]
        tail = "\\".join(posixpath.join(*parts).split("/")) if parts else ""
        return f"\\\\{self.server}\\{self.share}" + (f"\\{tail}" if tail else "")

    def scandir(self, rel_dir: str) -> list[FsEntry]:
        out = []
        for e in self._smb.scandir(self._unc(rel_dir)):
            if e.is_dir():
                out.append(FsEntry(e.name, True))
            elif e.is_file():
                st = e.stat()
                out.append(FsEntry(e.name, False, st.st_size, getattr(st, "st_mtime_ns", int(st.st_mtime * 1e9))))
        return out

    def open(self, rel_path: str) -> BinaryIO:
        return self._smb.open_file(self._unc(rel_path), mode="rb")


class SmbConnector(CrawlConnector):
    kind = "smb"

    def __init__(self, config: dict, *, smbclient_module=None) -> None:
        import os

        password = os.environ.get(config.get("password_env", ""), "") if config.get("password_env") else ""
        fs = SmbFS(
            config["server"], config["share"], config.get("base_path", ""),
            config.get("username", ""), password, smbclient_module,
        )
        super().__init__(fs, config)
