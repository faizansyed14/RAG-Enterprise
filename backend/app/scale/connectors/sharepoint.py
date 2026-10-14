"""SharePoint Online document libraries, read only.

Config
  tenant_id, client_id      the Entra app registration (Directory ID and Application ID)
  client_secret_env         NAME of a server environment variable that holds the client secret value
  sites                     list of site addresses, e.g. ["https://contoso.sharepoint.com/sites/Finance"]. Empty or missing
                            means every site the app can see (needs Sites.Read.All; with Sites.Selected only granted sites show)
  libraries                 optional list of library names to take, e.g. ["Documents", "Contracts"]; empty = all libraries
  access                    {"mode": "source"} (default: mirror each file's own sharing) | {"mode": "everyone"} |
                            {"mode": "principals", "principals": ["group:finance"]}
  include_extensions        optional, e.g. ["pdf", "docx"]
  exclude_paths             optional patterns, e.g. ["*/Archive", "Drafts"]
  modified_after            optional date: older files are ignored
  max_file_mb, site_group_map, use_sharing_headers, page_size   see graph_common.py

Entra app permissions (type Application, admin consent): Sites.Read.All and Files.Read.All, or Sites.Selected plus a read grant
per site. Nothing here ever writes to SharePoint: see `assert_read_only` in graph_common.py.
"""

from __future__ import annotations

import logging

from app.scale.connectors.graph_common import (
    GRAPH, Drive, GraphDriveConnector, GraphError, quote_path, site_path_from_url, validate_credentials,
)

log = logging.getLogger(__name__)
MAX_SITES = 2000


class SharePointConnector(GraphDriveConnector):
    kind = "sharepoint"

    def __init__(self, config: dict, **kwargs) -> None:
        if "token_provider" not in kwargs or kwargs["token_provider"] is None:
            validate_credentials(config)
        sites = config.get("sites") or []
        if not isinstance(sites, list):
            raise ValueError("sites must be a list of site addresses")
        self.site_targets = [site_path_from_url(str(s)) for s in sites]
        self.library_names = {str(n).strip().lower() for n in (config.get("libraries") or []) if str(n).strip()}
        super().__init__(config, **kwargs)

    def _resolve_site(self, host: str, path: str) -> dict | None:
        ref = f"{host}:{quote_path(path)}" if path else host
        response = self._request("GET", f"{GRAPH}/sites/{ref}?$select=id,displayName,webUrl", allow=(403, 404))
        if response.status_code >= 400:
            self.notes.append(f"{host}{path}: {'not found' if response.status_code == 404 else 'the app may not read it'}")
            return None
        return response.json()

    def _all_sites(self) -> list[dict]:
        sites: list[dict] = []
        for page in self._pages(f"{GRAPH}/sites?search=*&$select=id,displayName,webUrl&$top=200"):
            for s in page.get("value", []):
                if "-my.sharepoint.com" in str(s.get("webUrl", "")).lower():
                    continue                                    # personal OneDrive sites belong to the onedrive source
                sites.append(s)
            if len(sites) >= MAX_SITES:
                self.notes.append(f"Stopped at {MAX_SITES} sites; list the ones you want in `sites`.")
                break
        return sites

    def _discover_drives(self) -> list[Drive]:
        self.notes = []
        if self.site_targets:
            sites = [s for s in (self._resolve_site(h, p) for h, p in self.site_targets) if s]
        else:
            sites = self._all_sites()
        drives: list[Drive] = []
        for site in sites:
            name = site.get("displayName") or site["id"]
            try:
                pages = list(self._pages(f"{GRAPH}/sites/{site['id']}/drives?$select=id,name,driveType,webUrl,system"))
            except GraphError as exc:
                self.notes.append(f"{name}: libraries could not be listed ({str(exc)[:120]})")
                continue
            for page in pages:
                for d in page.get("value", []):
                    if d.get("driveType") != "documentLibrary" or "system" in d:
                        continue
                    if self.library_names and str(d.get("name", "")).lower() not in self.library_names:
                        continue
                    drives.append(Drive(d["id"], f"{name} / {d.get('name', 'Documents')}", "documentLibrary",
                                        d.get("webUrl", ""), name))
        if self.library_names and not drives and sites:
            self.notes.append("None of the sites has a library with those names.")
        return drives
