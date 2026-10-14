"""OneDrive for Business, read only: the personal drive of each named user.

Config
  tenant_id, client_id, client_secret_env   as for SharePoint
  users                     list of e-mail addresses / user principal names, e.g. ["ana@contoso.com"]
  all_users                 true = every enabled member account in the tenant (needs User.Read.All); `users` is then ignored
  max_users                 safety cap for all_users (default 5000)
  access, include_extensions, exclude_paths, modified_after, max_file_mb, page_size   as for SharePoint

Entra app permissions (type Application, admin consent): Files.Read.All, plus User.Read.All only when all_users is true.
By default every file keeps its own sharing (`access.mode` "source"), which for a personal drive normally means only its owner;
choose "everyone" or "principals" in the connection if the files should be searchable for others.
"""

from __future__ import annotations

import re

from app.scale.connectors.graph_common import (
    GRAPH, Drive, GraphDriveConnector, validate_credentials,
)

_UPN = re.compile(r"^[^\s@/\\?#]+@[^\s@/\\?#]+\.[A-Za-z]{2,}$")
_GUID = re.compile(r"^[0-9a-fA-F]{8}-([0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$")


class OneDriveConnector(GraphDriveConnector):
    kind = "onedrive"

    def __init__(self, config: dict, **kwargs) -> None:
        if kwargs.get("token_provider") is None:
            validate_credentials(config)
        self.all_users = bool(config.get("all_users", False))
        users = config.get("users") or []
        if not isinstance(users, list):
            raise ValueError("users must be a list of e-mail addresses")
        self.users = [str(u).strip() for u in users if str(u).strip()]
        for u in self.users:
            if not (_UPN.match(u) or _GUID.match(u)):
                raise ValueError(f"{u!r} is not an e-mail address (user principal name) or user id")
        if not self.all_users and not self.users:
            raise ValueError("list at least one user, or set all_users to true")
        self.max_users = int(config.get("max_users", 5000))
        super().__init__(config, **kwargs)

    def _every_user(self) -> list[str]:
        out: list[str] = []
        url = f"{GRAPH}/users?$select=id,userPrincipalName,accountEnabled,userType&$top=500"
        for page in self._pages(url):
            for u in page.get("value", []):
                if u.get("accountEnabled") is False or str(u.get("userType", "Member")).lower() == "guest":
                    continue
                if u.get("userPrincipalName"):
                    out.append(u["userPrincipalName"])
            if len(out) >= self.max_users:
                self.notes.append(f"Stopped at {self.max_users} users (max_users).")
                return out[: self.max_users]
        return out

    def _discover_drives(self) -> list[Drive]:
        self.notes = []
        users = self._every_user() if self.all_users else self.users
        drives: list[Drive] = []
        for upn in users:
            response = self._request("GET", f"{GRAPH}/users/{upn}/drive?$select=id,name,driveType,webUrl", allow=(403, 404))
            if response.status_code == 404:
                self.notes.append(f"{upn}: no OneDrive (never signed in, or no licence)")
                continue
            if response.status_code >= 400:
                self.notes.append(f"{upn}: the app may not read this OneDrive")
                continue
            d = response.json()
            drives.append(Drive(d["id"], f"OneDrive - {upn}", "personal", d.get("webUrl", ""), upn))
        return drives
