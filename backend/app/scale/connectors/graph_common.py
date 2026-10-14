"""Shared Microsoft Graph plumbing for the SharePoint, OneDrive and generic `graph` connectors. READ ONLY.

What "read only" means here, in the code and not just in the documentation
  * Every request goes through `assert_read_only` BEFORE it is sent. Allowed: GET to graph.microsoft.com; the OAuth token
    request (POST to login.microsoftonline.com .../oauth2/v2.0/token); and POST /v1.0/$batch whose every sub-request is a GET.
    Anything else (PUT, PATCH, DELETE, any other POST, any other host) raises `ReadOnlyViolation` and never leaves the process.
  * The bearer token is only ever sent to graph.microsoft.com. Next and delta links from a response (or from a stored
    cursor) that point anywhere else are refused, so a poisoned link cannot make us hand the token to another server.
  * File bytes are fetched from the pre-authenticated URL Graph returns, without the bearer token, with a hard size cap.
  * The client secret is read from the environment at call time; the connection only stores the NAME of the variable.
  * Application permissions that are enough: Sites.Read.All (+ Files.Read.All) for SharePoint, Files.Read.All
    (+ User.Read.All only to list every user) for OneDrive. Sites.Selected limits the app to sites you grant one by one.

Behaviour, following Microsoft's documented contract for driveItem delta:
  * Enumerate with GET /drives/{id}/root/delta; follow @odata.nextLink until @odata.deltaLink, and store the deltaLink:
    the next scan sends only what changed. The same item can appear more than once in a feed; the LAST occurrence wins.
    Deleted items carry a `deleted` facet. HTTP 410 means the token is stale: start over (the full listing is then
    authoritative for that drive). 429 / 503 / 504: honour Retry-After.
  * Permissions are read per file through /items/{id}/permissions, 20 at a time with JSON batching (all GETs).

What this does NOT do (stated so nobody assumes otherwise)
  * SharePoint *site groups* are local to a site and not expandable through the Graph /permissions response. They are mapped
    through `site_group_map`; an unmapped site group grants nothing (fail closed).
  * Entra group *membership* is not expanded here. Users carry their group ids in user_principals (filled at SSO login).
  * If the app may not read permissions (403), files get NO readers (fail closed) unless the connection says
    `access: {"mode": "everyone"}` or `{"mode": "principals", ...}`; `test()` reports which case you are in.
  * Drives that disappear from discovery (a removed site, a user without a OneDrive any more) are not deleted from the index
    automatically: a transient lookup failure must never wipe a library. Their files stay until removed by hand.

Not exercised against a live Microsoft tenant by the author: the tests use a simulated Graph server.
"""

from __future__ import annotations

import base64
import fnmatch
import json
import logging
import os
import re
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import BinaryIO, Callable, Iterator
from urllib.parse import quote, urlsplit

import httpx

from app.scale.acl import normalize_acl
from app.scale.connectors.base import Change, ScanBatch, SourceFile
from app.scale.settings import get_scale_settings

log = logging.getLogger(__name__)

GRAPH_HOST = "graph.microsoft.com"
LOGIN_HOST = "login.microsoftonline.com"
GRAPH = f"https://{GRAPH_HOST}/v1.0"
_READ_ROLES = {"read", "write", "owner", "sp.full control", "sp.read", "sp.edit", "sp.contribute"}
_GUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_TENANT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{0,251}\.[A-Za-z]{2,}$")
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
# Application roles that can change data. We never use them; the test report tells the admin they are granted.
_WRITE_ROLE = re.compile(r"(ReadWrite|FullControl|Manage|Write)", re.I)


class GraphError(RuntimeError):
    pass


class ReadOnlyViolation(GraphError):
    """Something tried to send a request that could change data in Microsoft 365. It was stopped before it left."""


def assert_read_only(method: str, url: str, body: dict | None = None) -> None:
    parts = urlsplit(url)
    method = method.upper()
    if parts.scheme != "https":
        raise ReadOnlyViolation(f"refusing a non-https request to {parts.hostname}")
    host = (parts.hostname or "").lower()
    if method == "GET":
        if host != GRAPH_HOST:
            raise ReadOnlyViolation(f"refusing to send credentials to {host}")
        return
    if method == "POST" and host == LOGIN_HOST and parts.path.endswith("/oauth2/v2.0/token"):
        return
    if method == "POST" and host == GRAPH_HOST and parts.path == "/v1.0/$batch":
        subs = (body or {}).get("requests")
        if isinstance(subs, list) and subs and all(
            isinstance(s, dict) and str(s.get("method", "")).upper() == "GET" and str(s.get("url", "")).startswith("/")
            and "body" not in s for s in subs
        ):
            return
        raise ReadOnlyViolation("a $batch request may contain only GET sub-requests")
    raise ReadOnlyViolation(f"{method} {host}{parts.path} is not allowed: these connectors only read")


def is_graph_url(url: str) -> bool:
    parts = urlsplit(url)
    return parts.scheme == "https" and (parts.hostname or "").lower() == GRAPH_HOST


def valid_tenant(value: str) -> bool:
    return bool(_GUID.match(value) or _TENANT_NAME.match(value))


def valid_guid(value: str) -> bool:
    return bool(_GUID.match(value))


def validate_credentials(cfg: dict) -> None:
    """Shape checks on the three values every Graph source needs. No network. Raises ValueError with a plain message."""
    if not valid_tenant(str(cfg.get("tenant_id", ""))):
        raise ValueError("tenant_id must be the Directory (tenant) ID, a GUID such as 11111111-2222-3333-4444-555555555555")
    if not valid_guid(str(cfg.get("client_id", ""))):
        raise ValueError("client_id must be the Application (client) ID, a GUID")
    if not _ENV_NAME.match(str(cfg.get("client_secret_env", ""))):
        raise ValueError("client_secret_env must be the NAME of a server environment variable, such as SHAREPOINT_CLIENT_SECRET")


def token_roles(token: str) -> list[str]:
    """The application roles inside an access token, for display only. The token is not verified here (Graph does that)."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return sorted(json.loads(base64.urlsafe_b64decode(payload)).get("roles") or [])
    except Exception:  # noqa: BLE001 -- display helper; a token we cannot read just shows no roles
        return []


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _identity_principals(identity: dict | None, site_group_map: dict[str, list[str]]) -> list[str]:
    out: list[str] = []
    if not identity:
        return out
    user = identity.get("user") or identity.get("siteUser")
    if user:
        for key in ("id", "email", "loginName"):
            val = user.get(key)
            if val:
                val = str(val)
                if key == "loginName" and "|" in val:
                    val = val.split("|")[-1]  # i:0#.f|membership|user@corp.com -> user@corp.com
                out.append(f"user:{val}")
    group = identity.get("group")
    if group:
        for key in ("id", "email"):
            if group.get(key):
                out.append(f"group:{group[key]}")
    site_group = identity.get("siteGroup")
    if site_group:
        for key in ("id", "displayName", "loginName"):
            mapped = site_group_map.get(str(site_group.get(key, "")))
            if mapped:
                out.extend(mapped)
    return out


def permissions_to_acl(
    permissions: list[dict], *, site_group_map: dict[str, list[str]] | None = None,
    link_scopes_as_access: list[str] | None = None,
) -> list[str]:
    """Translate Graph permission objects into the principal list stored on chunks."""
    site_group_map = site_group_map or {}
    link_scopes = {s.lower() for s in (link_scopes_as_access or [])}
    principals: list[str] = []
    for perm in permissions:
        roles = {str(r).lower() for r in (perm.get("roles") or [])}
        if roles and not (roles & _READ_ROLES):
            continue  # e.g. a permission that grants nothing readable
        link = perm.get("link")
        if link:
            scope = str(link.get("scope", "")).lower()
            if scope in ("anonymous", "organization") and scope in link_scopes:
                principals.append("*")
            # scope == 'users': the named recipients are in grantedToIdentitiesV2 below
        for identity in perm.get("grantedToIdentitiesV2") or []:
            principals.extend(_identity_principals(identity, site_group_map))
        granted = perm.get("grantedToV2")
        if granted:
            principals.extend(_identity_principals(granted, site_group_map))
    return normalize_acl(principals)


@dataclass
class Drive:
    drive_id: str
    label: str                      # shown in the path of every file, e.g. "Finance / Documents"
    kind: str = "documentLibrary"   # or "personal"
    web_url: str = ""
    owner: str = ""                 # the site or user it belongs to


@dataclass
class Filters:
    """Which files a source takes. Everything is allowed unless a rule says otherwise."""
    extensions: set[str] = field(default_factory=set)
    exclude_paths: list[str] = field(default_factory=list)
    modified_after: datetime | None = None

    @classmethod
    def from_config(cls, cfg: dict) -> "Filters":
        exts = {str(e).lower().lstrip(".").strip() for e in (cfg.get("include_extensions") or []) if str(e).strip()}
        excl = [str(p).strip().strip("/") for p in (cfg.get("exclude_paths") or []) if str(p).strip()]
        after = cfg.get("modified_after")
        when = None
        if after:
            when = _parse_dt(str(after)) or _parse_dt(f"{after}T00:00:00+00:00")
            if when is None:
                raise ValueError("modified_after must be a date such as 2024-01-31")
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
        return cls(exts, excl, when)

    def allows(self, path: str, name: str, modified: datetime | None) -> bool:
        if self.extensions:
            ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
            if ext not in self.extensions:
                return False
        for pattern in self.exclude_paths:
            if fnmatch.fnmatch(path.lower(), pattern.lower()) or fnmatch.fnmatch(path.lower(), pattern.lower() + "/*") \
                    or f"/{pattern.lower()}/" in f"/{path.lower()}/":
                return False
        if self.modified_after and modified and modified < self.modified_after:
            return False
        return True


def parse_access(cfg: dict) -> tuple[str, list[str]]:
    """('source', []) mirrors the file's own sharing; ('everyone', ['*']) lets every signed-in user read; ('principals', [...])."""
    access = cfg.get("access") or {}
    mode = str(access.get("mode", "source")).lower()
    if mode not in ("source", "everyone", "principals"):
        raise ValueError("access.mode must be source, everyone or principals")
    if mode == "everyone":
        return mode, ["*"]
    if mode == "principals":
        principals = normalize_acl([str(p) for p in access.get("principals", [])])
        if not principals:
            raise ValueError("access.principals must list at least one principal, for example group:finance")
        return mode, principals
    return mode, []


class GraphDriveConnector:
    """Delta scan of one or more drives. Subclasses decide WHICH drives (`_discover_drives`)."""

    kind = "graph"
    drive_based = True              # external ids are "<drive id>:<item id>"; sync and pipeline read this

    def __init__(
        self, config: dict, *, token_provider: Callable[[], str] | None = None,
        transport: httpx.BaseTransport | None = None, sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.cfg = config
        self.site_group_map: dict[str, list[str]] = config.get("site_group_map", {})
        self.use_sharing_headers = bool(config.get("use_sharing_headers", False))
        self.page_size = int(config.get("page_size", 200))
        self.max_bytes = int(config.get("max_file_mb", get_scale_settings().max_file_mb)) * 1024 * 1024
        self.filters = Filters.from_config(config)
        self.access_mode, self._fixed_acl = parse_access(config)
        self._token_provider = token_provider or self._client_credentials_token
        self._token: tuple[str, float] | None = None
        self._http = httpx.Client(transport=transport, timeout=httpx.Timeout(60.0, connect=15.0), follow_redirects=False)
        self._sleep = sleep
        self._link_scopes = get_scale_settings().link_scopes_as_access
        self._labels: dict[str, str] = {}
        self.notes: list[str] = []                  # things discovery could not do, for the test report and the log

    # ── drives ──

    def _discover_drives(self) -> list[Drive]:
        return [Drive(str(d), str(d)[:8]) for d in self.cfg["drive_ids"]]

    # ── auth / http ──

    def _client_credentials_token(self) -> str:
        now = time.time()
        if self._token and self._token[1] > now + 60:
            return self._token[0]
        secret = os.environ.get(self.cfg["client_secret_env"], "")
        if not secret:
            raise GraphError(f"environment variable {self.cfg['client_secret_env']} is empty")
        url = f"https://{LOGIN_HOST}/{self.cfg['tenant_id']}/oauth2/v2.0/token"
        assert_read_only("POST", url)
        response = self._http.post(
            url,
            data={
                "client_id": self.cfg["client_id"], "client_secret": secret,
                "scope": "https://graph.microsoft.com/.default", "grant_type": "client_credentials",
            },
        )
        response.raise_for_status()       # callers turn httpx.HTTPStatusError into their own, scrubbed message
        body = response.json()
        self._token = (body["access_token"], now + int(body.get("expires_in", 3000)))
        return self._token[0]

    def _request(self, method: str, url: str, *, headers: dict | None = None, json: dict | None = None,
                 allow: tuple[int, ...] = ()) -> httpx.Response:
        assert_read_only(method, url, json)
        for attempt in range(6):
            hdrs = {"Authorization": f"Bearer {self._token_provider()}", **(headers or {})}
            response = self._http.request(method, url, headers=hdrs, json=json)
            if response.status_code in (429, 503, 504):
                wait = float(response.headers.get("Retry-After", min(2 ** attempt, 30)))
                log.warning("graph throttled (%s); sleeping %.0fs", response.status_code, wait)
                self._sleep(min(wait, 120.0))
                continue
            if response.status_code == 401 and attempt == 0:
                self._token = None
                continue
            if response.status_code >= 400 and response.status_code not in allow:
                raise GraphError(f"{method} {url} -> {response.status_code}: {response.text[:300]}")
            return response
        raise GraphError(f"{method} {url}: still throttled after retries")

    def _pages(self, url: str, *, allow: tuple[int, ...] = ()) -> Iterator[dict]:
        """Follow @odata.nextLink. Every link must stay on graph.microsoft.com."""
        while url:
            response = self._request("GET", url, allow=allow)
            if response.status_code in allow and response.status_code >= 400:
                return
            body = response.json()
            yield body
            url = body.get("@odata.nextLink") or ""
            if url and not is_graph_url(url):
                raise GraphError("Graph returned a next link to another host; refusing to follow it")

    # ── scanning ──

    def scan(self, cursor: dict) -> Iterator[ScanBatch]:
        drives = dict(cursor.get("drives", {}))
        found = self._discover_drives()
        if not found:
            yield ScanBatch([], {"drives": drives}, done=True)
            return
        for position, drive in enumerate(found):
            self._labels[drive.drive_id] = drive.label
            yield from self._scan_drive(drive.drive_id, drives, last_drive=position == len(found) - 1)

    def _delta_headers(self) -> dict:
        if not self.use_sharing_headers:
            return {}
        return {"Prefer": "deltashowremovedasdeleted, deltatraversepermissiongaps, deltashowsharingchanges"}

    def _scan_drive(self, drive_id: str, drives: dict, *, last_drive: bool) -> Iterator[ScanBatch]:
        saved = drives.get(drive_id)
        if saved and not is_graph_url(saved):
            log.warning("stored link for drive %s does not point at Graph; starting over", drive_id)
            saved = None
        url = saved or f"{GRAPH}/drives/{drive_id}/root/delta?$top={self.page_size}"
        resync = False
        while url:
            response = self._request("GET", url, headers=self._delta_headers(), allow=(410,))
            if response.status_code == 410:
                # Token expired or invalid: Graph tells us to enumerate from scratch.
                log.warning("graph delta token for drive %s rejected (410); full resync", drive_id)
                drives.pop(drive_id, None)
                url = f"{GRAPH}/drives/{drive_id}/root/delta?$top={self.page_size}"
                resync = True
                continue
            body = response.json()
            latest: dict[str, dict] = {}
            for item in body.get("value", []):
                latest[item["id"]] = item  # last occurrence wins
            changes = self._to_changes(drive_id, list(latest.values()))
            next_link, delta_link = body.get("@odata.nextLink"), body.get("@odata.deltaLink")
            for link in (next_link, delta_link):
                if link and not is_graph_url(link):
                    raise GraphError("Graph returned a link to another host; refusing to follow it")
            if next_link:
                drives[drive_id] = next_link
            if delta_link:
                drives[drive_id] = delta_link
            finished = delta_link is not None
            yield ScanBatch(
                changes, {"drives": dict(drives)},
                done=finished and last_drive,
                # A from-scratch enumeration of this drive is authoritative for this drive only.
                complete_scope=f"{drive_id}:" if (resync and finished) else None,
            )
            url = next_link

    def _to_changes(self, drive_id: str, items: list[dict]) -> list[Change]:
        changes: list[Change] = []
        files: list[dict] = []
        for item in items:
            external_id = f"{drive_id}:{item['id']}"
            if "deleted" in item:
                changes.append(Change("delete", external_id))
            elif "file" in item:
                files.append(item)
            # folders / packages: nothing to index
        label = self._labels.get(drive_id) or drive_id[:8]
        wanted: list[tuple[dict, str]] = []
        for item in files:
            parent = (item.get("parentReference") or {}).get("path", "")
            parent = parent.split("root:", 1)[-1].strip("/") if parent else ""
            path = "/".join(p for p in (label, parent, item.get("name", "")) if p)
            if self.filters.allows(path, item.get("name", ""), _parse_dt(item.get("lastModifiedDateTime"))):
                wanted.append((item, path))
            else:
                changes.append(Change("delete", f"{drive_id}:{item['id']}"))   # no-op unless it was indexed before a rule changed
        acls = self._permissions_for(drive_id, [f["id"] for f, _ in wanted])
        for item, path in wanted:
            sf = SourceFile(
                external_id=f"{drive_id}:{item['id']}",
                path=path,
                name=item.get("name", ""),
                size=int(item.get("size", 0)),
                modified_at=_parse_dt(item.get("lastModifiedDateTime")),
                version=str(item.get("eTag") or item.get("cTag") or item.get("lastModifiedDateTime") or ""),
                acl=acls.get(item["id"], []),
                mime=(item.get("file") or {}).get("mimeType", ""),
                extra={"web_url": item.get("webUrl", ""), "drive_id": drive_id, "item_id": item["id"],
                       "sharing_changed": item.get("@microsoft.graph.sharedChanged") == "True"},
            )
            changes.append(Change("upsert", sf.external_id, sf))
        return changes

    # ── permissions (JSON batching, 20 per request, GET sub-requests only) ──

    def _permissions_for(self, drive_id: str, item_ids: list[str]) -> dict[str, list[str]]:
        if self.access_mode != "source":
            return {i: list(self._fixed_acl) for i in item_ids}      # no calls: the connection states who may read
        out: dict[str, list[str]] = {}
        for start in range(0, len(item_ids), 20):
            group = item_ids[start: start + 20]
            batch = {
                "requests": [
                    {"id": str(i), "method": "GET", "url": f"/drives/{drive_id}/items/{item_id}/permissions"}
                    for i, item_id in enumerate(group)
                ]
            }
            response = self._request("POST", f"{GRAPH}/$batch", json=batch)
            retry: list[str] = []
            for sub in response.json().get("responses", []):
                item_id = group[int(sub["id"])]
                status = int(sub.get("status", 500))
                if status == 200:
                    out[item_id] = permissions_to_acl(
                        sub["body"].get("value", []), site_group_map=self.site_group_map,
                        link_scopes_as_access=self._link_scopes,
                    )
                elif status in (429, 503, 504):
                    retry.append(item_id)
                else:
                    log.warning("permissions for %s -> %s; treating as no access", item_id, status)
                    out[item_id] = []  # fail closed
            if retry:
                self._sleep(5)
                out.update(self._permissions_for(drive_id, retry))
        return out

    def refresh_acl(self, file: SourceFile) -> list[str]:
        return self._permissions_for(file.extra["drive_id"], [file.extra["item_id"]]).get(file.extra["item_id"], [])

    def source_link(self, file: SourceFile) -> str | None:
        """The address that opens this file in SharePoint / OneDrive (the viewer signs in with their own Microsoft account, so
        Microsoft decides whether they may see it). One read-only request, made when someone clicks a citation."""
        response = self._request("GET", f"{GRAPH}/drives/{file.extra['drive_id']}/items/{file.extra['item_id']}?$select=webUrl", allow=(403, 404))
        return (response.json().get("webUrl") or None) if response.status_code == 200 else None

    # ── bytes ──

    def open(self, file: SourceFile) -> BinaryIO:
        drive_id, item_id = file.extra["drive_id"], file.extra["item_id"]
        url = f"{GRAPH}/drives/{drive_id}/items/{item_id}/content"
        response = self._request("GET", url, allow=(302,))
        tmp = tempfile.TemporaryFile()
        if response.status_code == 302:  # pre-authenticated download URL: do not send our bearer token to it
            location = response.headers["Location"]
            if urlsplit(location).scheme != "https":
                raise GraphError("the download link is not https; refusing it")
            with self._http.stream("GET", location) as stream:
                stream.raise_for_status()
                total = 0
                for chunk in stream.iter_bytes(1 << 20):
                    total += len(chunk)
                    if total > self.max_bytes:
                        raise GraphError(f"{file.path} exceeds the {self.max_bytes >> 20} MB limit")
                    tmp.write(chunk)
        else:
            if len(response.content) > self.max_bytes:
                raise GraphError(f"{file.path} exceeds the {self.max_bytes >> 20} MB limit")
            tmp.write(response.content)
        tmp.seek(0)
        return tmp

    # ── "Test connection": what would a scan see? Reads a little, changes nothing, stores nothing ──

    def test(self) -> dict:
        report: dict = {"ok": False, "roles": [], "write_roles": [], "drives": [], "notes": [], "permissions_readable": None,
                        "access_mode": self.access_mode, "sample_files": 0}
        try:
            token = self._token_provider()
        except httpx.HTTPStatusError as exc:
            try:
                why = str(exc.response.json().get("error_description", ""))
            except ValueError:
                why = ""
            report["error"] = f"Microsoft refused the sign-in ({exc.response.status_code}): " + (
                why.split("\r\n")[0][:220] or "check the tenant id, client id and the secret")
            return report
        except GraphError as exc:
            report["error"] = str(exc)
            return report
        except Exception as exc:  # noqa: BLE001
            report["error"] = f"could not sign in to Microsoft: {type(exc).__name__}"
            return report
        report["roles"] = token_roles(token)
        report["write_roles"] = [r for r in report["roles"] if _WRITE_ROLE.search(r)]
        try:
            drives = self._discover_drives()
        except GraphError as exc:
            report["error"] = str(exc)[:400]
            return report
        report["notes"] = list(self.notes)
        probe_item: tuple[str, str] | None = None
        for drive in drives[:25]:
            entry = {"id": drive.drive_id, "label": drive.label, "owner": drive.owner, "web_url": drive.web_url, "files_sampled": 0}
            try:
                listing = self._request("GET", f"{GRAPH}/drives/{drive.drive_id}/root/children?$top=25&$select=id,name,file,folder",
                                        allow=(403, 404)).json()
                if "error" in listing:
                    entry["error"] = "the app cannot read this library"
                else:
                    files = [i for i in listing.get("value", []) if "file" in i]
                    entry["files_sampled"] = len(files)
                    report["sample_files"] += len(files)
                    if files and probe_item is None:
                        probe_item = (drive.drive_id, files[0]["id"])
            except GraphError as exc:
                entry["error"] = str(exc)[:160]
            report["drives"].append(entry)
        report["drive_count"] = len(drives)
        if probe_item and self.access_mode == "source":
            try:
                self._probe_permissions(*probe_item)
                report["permissions_readable"] = True
            except GraphError:
                report["permissions_readable"] = False
        report["ok"] = bool(drives) and any("error" not in d for d in report["drives"])
        if not drives:
            report["error"] = "No document libraries were found. " + (" ".join(report["notes"]) or "Check the sites or users you listed.")
        return report

    def _probe_permissions(self, drive_id: str, item_id: str) -> None:
        batch = {"requests": [{"id": "0", "method": "GET", "url": f"/drives/{drive_id}/items/{item_id}/permissions"}]}
        sub = self._request("POST", f"{GRAPH}/$batch", json=batch).json().get("responses", [{}])[0]
        if int(sub.get("status", 500)) != 200:
            raise GraphError(f"permissions not readable ({sub.get('status')})")


def site_path_from_url(url: str) -> tuple[str, str]:
    """('contoso.sharepoint.com', '/sites/Finance') from a SharePoint URL. Raises ValueError for anything else."""
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or not host.endswith(".sharepoint.com") or host.endswith("-my.sharepoint.com"):
        raise ValueError(f"{url!r} is not a SharePoint site address (expected https://<tenant>.sharepoint.com/sites/<name>)")
    path = "/" + parts.path.strip("/")
    path = path if path != "/" else ""
    path = re.split(r"/(?:Shared%20Documents|Shared Documents|SitePages|Lists|_layouts|Forms)\b", path, maxsplit=1)[0]
    if re.search(r"[?#\\]", path):
        raise ValueError(f"{url!r} has characters that cannot be in a site address")
    return host, path


def quote_path(path: str) -> str:
    return quote(path, safe="/")
