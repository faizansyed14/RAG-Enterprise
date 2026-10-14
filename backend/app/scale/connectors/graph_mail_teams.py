"""Outlook mail (`outlook_mail`) and Teams channel posts (`teams`) through Microsoft Graph delta queries.

Both classes extend `GraphConnector` (graph.py) only to reuse its application-permission plumbing: the client-credentials token
(`https://login.microsoftonline.com/<tenant>/oauth2/v2.0/token`, scope `https://graph.microsoft.com/.default`, cached until it is
about to expire, dropped and re-requested once on a 401) and its request loop (429 / 503 / 504 honour `Retry-After`). What is added
here: delta feeds of mail folders and channels, rendering messages to Markdown "virtual files", errors that are `_http.AuthError`
/ `HttpError`, and the usual prefix-level ACL rules. graph.py is not modified.

Endpoints (Microsoft Graph v1.0 reference, learn.microsoft.com/graph/api: "message: delta", "Get message", "List attachments",
"chatMessage: delta", "Get channel message", "List replies", "List channels", "Delta query overview", "Throttling"):

  GET /users/{user}/mailFolders/{folder}/messages/delta?$select=...[&$filter=receivedDateTime ge T&$orderby=receivedDateTime desc]
        header `Prefer: odata.maxpagesize=N`. Follow `@odata.nextLink` until a page carries `@odata.deltaLink`; the link is the
        cursor of the next scan. A message that left the folder or was deleted comes as {"id": ..., "@removed": {"reason": ...}}.
        `folder` is a folder id or a well-known name (inbox, sentitems, archive, ...).
  GET /users/{user}/messages/{id}?$select=...                    open(): subject, addresses, dates, `body` {contentType, content}
  GET /users/{user}/messages/{id}/attachments?$select=name,contentType,size,isInline        names of the attachments (not contents)
  GET /teams/{team}/channels/{channel}/messages/delta?$top=N[&$filter=lastModifiedDateTime gt T]
        the same nextLink / deltaLink / `@removed` mechanism over the posts of a channel.
  GET /teams/{team}/channels/{channel}/messages/{id}              open(): the post   (`body` {contentType, content}, `from`, ...)
  GET /teams/{team}/channels/{channel}/messages/{id}/replies?$top=N     the replies of a post, paged by `@odata.nextLink`
  GET /teams/{team}/channels?$select=id,displayName,membershipType    only when `channels` is not configured

HTTP 410 Gone on a delta link (an expired or invalid token: "syncStateNotFound") starts that feed over from its first request and
treats the new enumeration as authoritative for the feed. A feed that has no saved link (the first scan, a lost cursor, a restart
after 410) is a from-scratch enumeration: when it runs to its `deltaLink` the batch carries `complete_scope` = the feed's external-id
prefix, so whatever was not listed is deleted downstream (the sync layer currently discards the final cursor, which makes every scan a
from-scratch one; this is what keeps deletions correct then). A feed resumed from a saved link never sets it.

Auth: application permissions, secret by environment variable name: `tenant_id`, `client_id`, `client_secret_env` (read when a token is
needed); `token_provider=` (a callable returning an access token) replaces the client-credentials flow in code. Outlook needs the
application permission `Mail.Read`, which by default reaches EVERY mailbox in the tenant: restrict the app registration with an Exchange
Online application access policy / RBAC for Applications before pointing this at production. Teams needs `ChannelMessage.Read.All`
(and `Channel.ReadBasic.All` to list channels); Microsoft treats the Teams message APIs as protected APIs (an approval / billing model
applies to application access: check your tenant). Sovereign clouds are not supported (graph.py fixes the login host).

Config (both): tenant_id, client_id, client_secret_env, since (ISO date: only newer mail / posts on the FIRST request of a feed),
page_size (50), acl_rules, default_acl.
  outlook_mail: users (list of addresses / ids; or `user`), folders (list of well-known names or ids, or {"id", "name"} to label the
        path; default ["inbox"]), include_drafts (false: a draft is reported as a delete).
  teams: teams [{"id", "name", "channels": [id | {"id", "name"}], "exclude_channels": [names or ids]}] (or `team_id` + `channels`);
        no channels => every channel of the team, listed through the API. include_replies (true).

Objects
  Outlook: external_id `outlook_mail:<user>:<folder>:<message id>`, path `outlook/<user>/<folder>/<year>/<date> <subject> [<hash>].md`,
        version = the message's `changeKey` (Graph's version of a message; a read-flag change also changes it, which costs one
        re-parse), size 0 (bodies are not fetched by a scan). The Markdown holds subject, from / to / cc, date, attachment names and the
        body; HTML bodies are converted to text. Drafts, bodies of attachments and inline images are not indexed.
  Teams: ONE FILE PER TOP-LEVEL POST (a chat line alone is a poor unit to retrieve): external_id
        `teams:<team>:<channel, percent-encoded>:<post id>`, path `teams/<team>/<channel>/<year>/<date> <title> [<post id>].md`,
        version = a hash of the rendered thread, size = its length. With `include_replies` the file holds the replies as well; the scan
        fetches them for every post it renders (one request per post, so a first scan of a busy channel is slow) and open() fetches them again.
        System messages and messages with `deletedDateTime` are not indexed (the latter become deletes).

Permissions: PREFIX-LEVEL MAPPING CONFIGURED BY THE CLIENT (`acl_rules` keyed by the path, e.g. `outlook/support@acme.com` readable by
`group:support`, `teams/Engineering/General` by `group:eng`; no match and no `default_acl` => nobody). It is NOT Exchange's or Teams'
permission model: a mailbox or channel is readable only by whoever the rules name, whoever could read it in Microsoft 365.

What this does NOT do: attachment contents, inline images, calendar / contacts, chats (1:1 and group), meeting transcripts, reactions,
edit history, shared channels hosted by another tenant, immutable ids (a message moved to another folder is a delete plus a new object).

Not exercised against a live tenant; tested against fixtures shaped like the vendor documentation.
Unsure of (marked in the tests): whether the Teams channel delta carries replies at all and whether a reply bumps its post's
`lastModifiedDateTime` (both are handled: a reply seen in the feed refreshes its post, and replies are fetched per post), the `$top` limit
of the Teams delta, whether `$filter=lastModifiedDateTime gt` is honoured there, and the exact `error.code` of a 410.
"""

from __future__ import annotations

import io
import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import BinaryIO, Callable, Iterator
from urllib.parse import quote, unquote, urlencode

import httpx

from app.scale.connectors._http import AuthError, HttpError, render_document, safe_name
from app.scale.connectors._objects import html_to_text, iso_z, parse_dt, short_hash
from app.scale.connectors.base import AclRules, Change, ScanBatch, SourceFile
from app.scale.connectors.graph import GRAPH, GraphConnector, GraphError

log = logging.getLogger(__name__)

_ANY_ERROR = tuple(range(400, 600))                   # _request returns these instead of raising; they are classified here
_ID = re.compile(r"^[A-Za-z0-9_\-=.+]{1,600}$")
_AT = re.compile(r"<at\b[^>]*>(.*?)</at>", re.S | re.I)
_ATTACHMENT_TAG = re.compile(r"<attachment\b[^>]*>\s*(</attachment>)?", re.I)


def _excerpt(response: httpx.Response, limit: int = 200) -> str:
    try:
        return re.sub(r"\s+", " ", response.text)[:limit]
    except Exception:  # noqa: BLE001
        return ""


def _query(params: dict[str, str]) -> str:
    return urlencode(params, quote_via=quote, safe="$,")


def _day(moment: datetime | None) -> str:
    return f"{moment:%Y-%m-%d}" if moment else "undated"


def _people(items) -> str:
    out = []
    for item in items or []:
        address = (item or {}).get("emailAddress") or {}
        name, mail = address.get("name"), address.get("address")
        out.append(f"{name} <{mail}>" if name and mail and name != mail else (mail or name or ""))
    return ", ".join(p for p in out if p)


def _body_text(body: dict | None) -> str:
    body = body or {}
    content = str(body.get("content") or "")
    if str(body.get("contentType", "")).lower() == "html":
        content = _ATTACHMENT_TAG.sub("", _AT.sub(lambda m: "@" + m.group(1), content))
        return html_to_text(content)
    return content.replace("\r\n", "\n").strip()


@dataclass
class _Feed:
    key: str                         # the cursor key
    url: str                         # the first delta request
    prefix: str                      # external-id prefix of everything this feed produces
    info: dict = field(default_factory=dict)


class _GraphFeeds(GraphConnector):
    kind = ""
    drive_based = False             # not drive items: the sync layer must not treat their ids as "<drive>:<item>"

    def __init__(self, config: dict, *, token_provider: Callable[[], str] | None = None,
                 transport: httpx.BaseTransport | None = None, sleep: Callable[[float], None] = time.sleep) -> None:
        if token_provider is None:
            for key in ("tenant_id", "client_id", "client_secret_env"):
                if not config.get(key):
                    raise ValueError(f"{self.kind}: `{key}` is required (or pass token_provider)")
        super().__init__({**config, "drive_ids": []}, token_provider=token_provider, transport=transport, sleep=sleep)
        self.rules = AclRules(config.get("acl_rules"), config.get("default_acl"))
        self.page_size = max(1, min(int(config.get("page_size", 50)), 50))
        self.since = parse_dt(config.get("since")) if config.get("since") else None
        if config.get("since") and self.since is None:
            raise ValueError(f"{self.kind}: `since` must be an ISO date")

    # ── auth and http (the token code is graph.py's) ──

    def _client_credentials_token(self) -> str:
        try:
            return super()._client_credentials_token()
        except httpx.HTTPStatusError as exc:
            detail = ""
            try:
                body = exc.response.json()
                detail = f", {body.get('error')}: {str(body.get('error_description', ''))[:160]}"
            except ValueError:
                pass
            raise AuthError(self._scrub(
                f"Microsoft identity platform rejected the client credentials (HTTP {exc.response.status_code}{detail}); "
                f"check tenant_id, client_id and the secret in environment variable {self.cfg.get('client_secret_env')}"),
                exc.response.status_code) from None

    def _scrub(self, text: str) -> str:
        secrets = [self._token[0] if self._token else "", os.environ.get(str(self.cfg.get("client_secret_env")), "")]
        for secret in secrets:
            if secret and len(secret) >= 4:
                text = text.replace(secret, "***")
        return text

    def _send(self, url: str, *, headers: dict | None = None, ok: tuple[int, ...] = ()) -> httpx.Response:
        shown = url.split("?", 1)[0]
        try:
            response = self._request("GET", url, headers=headers, allow=_ANY_ERROR)
        except AuthError:
            raise
        except GraphError as exc:
            throttled = "throttled" in str(exc)
            raise HttpError(self._scrub(f"GET {shown}: {exc}"), 429 if throttled else None) from None
        except httpx.TransportError as exc:
            raise HttpError(f"GET {shown}: {type(exc).__name__}") from None
        status = response.status_code
        if status < 400 or status in ok:
            return response
        text = self._scrub(f"GET {shown} -> {status}: {_excerpt(response)}")
        if status == 401:
            raise AuthError(f"{text} (the access token was rejected: check tenant_id, client_id and the secret in environment variable "
                            f"{self.cfg.get('client_secret_env')})", 401)
        if status == 403:
            raise AuthError(f"{text} ({self._permission_hint()})", 403)
        raise HttpError(text, status)

    def _permission_hint(self) -> str:
        return "the app registration needs the application permission with admin consent"

    def _json(self, url: str, **kwargs) -> dict:
        return self._send(url, **kwargs).json()

    def _pages(self, url: str, *, headers: dict | None = None) -> Iterator[dict]:
        """A collection that pages through `@odata.nextLink`."""
        seen: set[str] = set()
        while url:
            if url in seen:
                raise HttpError(f"GET {url.split('?', 1)[0]}: pagination did not advance (the same nextLink came back twice)")
            seen.add(url)
            body = self._json(url, headers=headers)
            yield body
            url = body.get("@odata.nextLink") or ""

    # ── delta feeds ──

    def _feeds(self) -> list[_Feed]:
        raise NotImplementedError

    def _feed_headers(self) -> dict | None:
        return None

    def _changes(self, feed: _Feed, entries: list[dict]) -> list[Change]:
        raise NotImplementedError

    def scan(self, cursor: dict) -> Iterator[ScanBatch]:
        links: dict[str, str] = dict((cursor or {}).get("feeds") or {})
        for feed in self._feeds():
            yield from self._scan_feed(feed, links)
        yield ScanBatch([], {"feeds": dict(links)}, done=True)

    def _scan_feed(self, feed: _Feed, links: dict[str, str]) -> Iterator[ScanBatch]:
        saved = links.get(feed.key)
        url, fresh = saved or feed.url, saved is None
        seen: set[str] = set()
        while url:
            if url in seen:
                raise HttpError(f"GET {feed.url.split('?', 1)[0]}: pagination did not advance (the same link came back twice)")
            seen.add(url)
            response = self._send(url, headers=self._feed_headers(), ok=(410,))
            if response.status_code == 410:
                if url == feed.url:
                    raise HttpError(f"GET {url.split('?', 1)[0]} -> 410: the first delta request was refused as gone", 410)
                log.warning("%s: delta link for %s rejected (410); enumerating from scratch", self.kind, feed.key)
                url, fresh = feed.url, True
                continue
            body = response.json()
            latest: dict[str, dict] = {}
            for entry in body.get("value") or []:
                if isinstance(entry, dict) and entry.get("id"):
                    latest[str(entry["id"])] = entry                      # the same item can appear twice: the last wins
            changes = self._changes(feed, list(latest.values()))
            next_link, delta_link = body.get("@odata.nextLink"), body.get("@odata.deltaLink")
            if not next_link and not delta_link:
                raise HttpError(f"GET {feed.url.split('?', 1)[0]}: the delta response had neither @odata.nextLink nor @odata.deltaLink")
            links[feed.key] = next_link or delta_link
            yield ScanBatch(changes, {"feeds": dict(links)}, complete_scope=feed.prefix if (fresh and delta_link) else None)
            url = next_link or ""

    def refresh_acl(self, file: SourceFile) -> list[str]:
        return self.rules.for_path(file.path)


# ───────────────────────────── Outlook mail ─────────────────────────────


FEED_SELECT = "subject,receivedDateTime,lastModifiedDateTime,changeKey,isDraft"
MESSAGE_SELECT = "subject,from,sender,toRecipients,ccRecipients,receivedDateTime,sentDateTime,body,hasAttachments,isDraft"


class OutlookMailConnector(_GraphFeeds):
    kind = "outlook_mail"

    def __init__(self, config: dict, *, token_provider: Callable[[], str] | None = None,
                 transport: httpx.BaseTransport | None = None, sleep: Callable[[float], None] = time.sleep) -> None:
        super().__init__(config, token_provider=token_provider, transport=transport, sleep=sleep)
        users = config.get("users") or ([config["user"]] if config.get("user") else [])
        if not users:
            raise ValueError("outlook_mail: configure `users` (mailbox addresses or ids)")
        for user in users:
            if not re.match(r"^[^\s/\\?#:]+$", str(user)):
                raise ValueError(f"outlook_mail: {user!r} is not a mailbox address or id")
        self.users = [str(u) for u in users]
        self.folders = [{"id": str(f), "name": str(f)} if isinstance(f, str) else {"id": str(f["id"]), "name": str(f.get("name") or f["id"])}
                        for f in config.get("folders") or ["inbox"]]
        for folder in self.folders:
            if not re.match(r"^[^\s/\\?#:]+$", folder["id"]):
                raise ValueError(f"outlook_mail: {folder['id']!r} is not a folder id or well-known name")
        self.include_drafts = bool(config.get("include_drafts", False))

    def _permission_hint(self) -> str:
        return ("the app registration needs the application permission Mail.Read with admin consent, and an Exchange application "
                "access policy that allows this mailbox")

    def _feed_headers(self) -> dict:
        return {"Prefer": f"odata.maxpagesize={self.page_size}"}

    @staticmethod
    def _user(user: str) -> str:
        return quote(user, safe="@")

    def _external_id(self, user: str, folder: str, message_id: str) -> str:
        return f"outlook_mail:{quote(user, safe='@')}:{quote(folder, safe='')}:{message_id}"

    def _feeds(self) -> list[_Feed]:
        feeds = []
        for user in self.users:
            for folder in self.folders:
                params = {"$select": FEED_SELECT}
                if self.since:
                    params.update({"$filter": f"receivedDateTime ge {iso_z(self.since)}", "$orderby": "receivedDateTime desc"})
                url = f"{GRAPH}/users/{self._user(user)}/mailFolders/{quote(folder['id'], safe='')}/messages/delta?{_query(params)}"
                prefix = f"outlook_mail:{quote(user, safe='@')}:{quote(folder['id'], safe='')}:"
                feeds.append(_Feed(f"{user}|{folder['id']}", url, prefix, {"user": user, "folder": folder}))
        return feeds

    def _changes(self, feed: _Feed, entries: list[dict]) -> list[Change]:
        user, folder = feed.info["user"], feed.info["folder"]
        changes = []
        for entry in entries:
            if not _ID.match(str(entry["id"])):
                log.warning("outlook_mail: skipping a message whose id has unexpected characters")
                continue
            ext = self._external_id(user, folder["id"], str(entry["id"]))
            if "@removed" in entry or (entry.get("isDraft") and not self.include_drafts):
                changes.append(Change("delete", ext))
                continue
            when = parse_dt(entry.get("receivedDateTime"))
            subject = str(entry.get("subject") or "").strip() or "(no subject)"
            stem = safe_name(f"{_day(when)} {subject}", "message", 100)
            path = f"outlook/{safe_name(user, 'mailbox', 100)}/{safe_name(folder['name'], 'folder', 80)}/{when.year if when else 'undated'}/{stem} [{short_hash(entry['id'], size=8)}].md"
            sf = SourceFile(
                external_id=ext, path=path, name=path.rsplit("/", 1)[-1], size=0, modified_at=parse_dt(entry.get("lastModifiedDateTime")),
                version=str(entry.get("changeKey") or entry.get("lastModifiedDateTime") or ""), acl=self.rules.for_path(path),
                mime="text/markdown", extra={"user": user, "folder": folder["id"], "id": str(entry["id"])},
            )
            changes.append(Change("upsert", ext, sf))
        return changes

    # ── bytes ──

    def open(self, file: SourceFile) -> BinaryIO:
        parts = file.external_id.split(":", 3)
        if len(parts) != 4 or parts[0] != "outlook_mail" or not parts[1] or not parts[2] or not _ID.match(parts[3]):
            raise HttpError(f"outlook_mail: {file.external_id!r} is not a message id")
        user, folder, message_id = unquote(parts[1]), unquote(parts[2]), parts[3]
        base = f"{GRAPH}/users/{self._user(user)}/messages/{quote(message_id, safe='')}"
        message = self._json(f"{base}?{_query({'$select': MESSAGE_SELECT})}")
        names: list[str] = []
        if message.get("hasAttachments"):
            for page in self._pages(f"{base}/attachments?{_query({'$select': 'name,contentType,size,isInline'})}"):
                for item in page.get("value") or []:
                    if not item.get("isInline") and item.get("name"):
                        names.append(f"{item['name']} ({item.get('contentType') or 'unknown type'}, {int(item.get('size') or 0)} bytes)")
        label = next((f["name"] for f in self.folders if f["id"] == folder), folder)
        meta = {
            "from": _people([message.get("from") or message.get("sender")]), "to": _people(message.get("toRecipients")),
            "cc": _people(message.get("ccRecipients")), "date": message.get("receivedDateTime") or message.get("sentDateTime"),
            "mailbox": user, "folder": label, "attachments": "; ".join(names),
        }
        return io.BytesIO(render_document(str(message.get("subject") or "").strip() or "(no subject)", meta, _body_text(message.get("body"))))


# ───────────────────────────── Teams ─────────────────────────────


class TeamsConnector(_GraphFeeds):
    kind = "teams"

    def __init__(self, config: dict, *, token_provider: Callable[[], str] | None = None,
                 transport: httpx.BaseTransport | None = None, sleep: Callable[[float], None] = time.sleep) -> None:
        super().__init__(config, token_provider=token_provider, transport=transport, sleep=sleep)
        raw = config.get("teams") or ([{"id": config["team_id"], "channels": config.get("channels"),
                                        "exclude_channels": config.get("exclude_channels")}] if config.get("team_id") else [])
        if not raw:
            raise ValueError("teams: configure `team_id` (and `channels`) or `teams`")
        self.teams = []
        for team in raw:
            team_id = str(team.get("id", ""))
            if not re.match(r"^[0-9A-Za-z-]{8,64}$", team_id):
                raise ValueError(f"teams: {team_id!r} is not a team id (a GUID)")
            channels = [{"id": str(c), "name": ""} if isinstance(c, str) else {"id": str(c["id"]), "name": str(c.get("name") or "")}
                        for c in team.get("channels") or []]
            self.teams.append({"id": team_id, "name": str(team.get("name") or team_id), "channels": channels,
                               "exclude": [str(x).lower() for x in team.get("exclude_channels") or []]})
        self.include_replies = bool(config.get("include_replies", True))
        self._labels: dict[tuple[str, str], str] = {}

    def _permission_hint(self) -> str:
        return "the app registration needs the application permissions ChannelMessage.Read.All and Channel.ReadBasic.All with admin consent"

    @staticmethod
    def _channel_path(channel: str) -> str:
        return quote(channel, safe=":@")

    def _external_id(self, team: str, channel: str, message_id: str) -> str:
        return f"teams:{team}:{quote(channel, safe='')}:{message_id}"

    def _label(self, team_id: str, channel_id: str, name: str = "") -> str:
        """The channel's folder name: configured, listed, or read once from Graph; the same on every path so open() renders what the scan did."""
        key = (team_id, channel_id)
        if name:
            self._labels[key] = name
        if key not in self._labels:
            try:
                info = self._json(f"{GRAPH}/teams/{team_id}/channels/{self._channel_path(channel_id)}?{_query({'$select': 'id,displayName'})}")
                self._labels[key] = str(info.get("displayName") or channel_id)
            except HttpError as exc:
                if exc.status not in (403, 404):
                    raise
                self._labels[key] = channel_id
        return self._labels[key]

    def _team_channels(self, team: dict) -> list[dict]:
        if team["channels"]:
            return team["channels"]
        found = []
        url = f"{GRAPH}/teams/{team['id']}/channels?{_query({'$select': 'id,displayName,membershipType'})}"
        for page in self._pages(url):
            for channel in page.get("value") or []:
                names = {str(channel.get("id", "")).lower(), str(channel.get("displayName", "")).lower()}
                if channel.get("id") and not names & set(team["exclude"]):
                    found.append({"id": channel["id"], "name": channel.get("displayName") or ""})
        return found

    def _feeds(self) -> list[_Feed]:
        feeds = []
        for team in self.teams:
            for channel in self._team_channels(team):
                params = {"$top": str(self.page_size)}
                if self.since:
                    params["$filter"] = f"lastModifiedDateTime gt {iso_z(self.since)}"
                url = f"{GRAPH}/teams/{team['id']}/channels/{self._channel_path(channel['id'])}/messages/delta?{_query(params)}"
                self._label(team["id"], channel["id"], channel["name"])
                feeds.append(_Feed(f"{team['id']}|{channel['id']}", url, f"teams:{team['id']}:{quote(channel['id'], safe='')}:",
                                   {"team": team, "channel": channel}))
        return feeds

    # ── rendering ──

    @staticmethod
    def _author(message: dict) -> str:
        sender = message.get("from") or {}
        for key in ("user", "application", "device"):
            if (sender.get(key) or {}).get("displayName"):
                return str(sender[key]["displayName"])
        return "unknown"

    def _replies(self, team: str, channel: str, message_id: str) -> list[dict]:
        replies: list[dict] = []
        url = f"{GRAPH}/teams/{team}/channels/{self._channel_path(channel)}/messages/{message_id}/replies?{_query({'$top': str(self.page_size)})}"
        for page in self._pages(url):
            replies.extend(r for r in page.get("value") or [] if self._indexable(r))
        return sorted(replies, key=lambda r: str(r.get("createdDateTime") or ""))

    @staticmethod
    def _indexable(message: dict) -> bool:
        return message.get("messageType", "message") == "message" and not message.get("deletedDateTime")

    def _render(self, team_label: str, channel_label: str, post: dict, replies: list[dict]) -> tuple[bytes, str]:
        text = _body_text(post.get("body"))
        title = str(post.get("subject") or "").strip() or (text.splitlines()[0][:80] if text.strip() else "") or "Teams message"
        names = [a.get("name") for a in post.get("attachments") or [] if a.get("name")]
        meta = {"team": team_label, "channel": channel_label, "from": self._author(post), "date": post.get("createdDateTime"),
                "edited": post.get("lastEditedDateTime"), "attachments": ", ".join(names)}
        body = [text]
        if replies:
            body.append("\n## Replies\n")
            for reply in replies:
                when = parse_dt(reply.get("createdDateTime"))
                first, *rest = (_body_text(reply.get("body")) or "(no text)").split("\n")
                body.append(f"[{when:%Y-%m-%d %H:%M}] {self._author(reply)}: {first}" + "".join(f"\n    {line}" for line in rest))
        return render_document(title, meta, "\n".join(body)), title

    def _thread(self, feed_info: dict, post: dict) -> Change:
        team, channel = feed_info["team"], feed_info["channel"]
        label = self._label(team["id"], channel["id"], channel["name"])
        replies = self._replies(team["id"], channel["id"], str(post["id"])) if self.include_replies else []
        data, title = self._render(team["name"], label, post, replies)
        when = parse_dt(post.get("createdDateTime"))
        path = (f"teams/{safe_name(team['name'], team['id'], 80)}/{safe_name(label, 'channel', 80)}/{when.year if when else 'undated'}/"
                f"{safe_name(f'{_day(when)} {title}', 'message', 100)} [{post['id']}].md")
        ext = self._external_id(team["id"], channel["id"], str(post["id"]))
        sf = SourceFile(
            external_id=ext, path=path, name=path.rsplit("/", 1)[-1], size=len(data),
            modified_at=parse_dt(post.get("lastModifiedDateTime")), version=short_hash(data, size=16), acl=self.rules.for_path(path),
            mime="text/markdown", extra={"team": team["id"], "channel": channel["id"], "id": str(post["id"])},
        )
        return Change("upsert", ext, sf)

    def _changes(self, feed: _Feed, entries: list[dict]) -> list[Change]:
        team, channel = feed.info["team"], feed.info["channel"]
        changes: list[Change] = []
        posts: dict[str, dict] = {}
        parents: list[str] = []
        for entry in entries:
            mid = str(entry["id"])
            ext = self._external_id(team["id"], channel["id"], mid)
            if "@removed" in entry or entry.get("deletedDateTime"):
                changes.append(Change("delete", ext))
            elif not self._indexable(entry):
                continue
            elif entry.get("replyToId"):
                parents.append(str(entry["replyToId"]))               # a reply in the feed: its post is what changed
            else:
                posts[mid] = entry
        removed = {c.external_id for c in changes}
        for parent in parents:
            if parent in posts or self._external_id(team["id"], channel["id"], parent) in removed:
                continue
            try:
                root = self._json(f"{GRAPH}/teams/{team['id']}/channels/{self._channel_path(channel['id'])}/messages/{parent}")
            except HttpError as exc:
                if exc.status != 404:
                    raise
                changes.append(Change("delete", self._external_id(team["id"], channel["id"], parent)))
                continue
            if self._indexable(root) and not root.get("replyToId"):
                posts[parent] = root
        changes.extend(self._thread(feed.info, post) for post in posts.values())
        return changes

    # ── bytes ──

    def open(self, file: SourceFile) -> BinaryIO:
        parts = file.external_id.split(":", 3)
        if (len(parts) != 4 or parts[0] != "teams" or not re.match(r"^[0-9A-Za-z-]{8,64}$", parts[1]) or not parts[2]
                or not re.match(r"^[0-9A-Za-z_.\-]{1,64}$", parts[3])):
            raise HttpError(f"teams: {file.external_id!r} is not a post id")
        team_id, channel, post_id = parts[1], unquote(parts[2]), parts[3]
        team = next((t for t in self.teams if t["id"] == team_id), None) or {"id": team_id, "name": team_id, "channels": []}
        configured = next((c["name"] for c in team["channels"] if c["id"] == channel), "")
        post = self._json(f"{GRAPH}/teams/{team_id}/channels/{self._channel_path(channel)}/messages/{post_id}")
        replies = self._replies(team_id, channel, post_id) if self.include_replies else []
        return io.BytesIO(self._render(team["name"], self._label(team_id, channel, configured), post, replies)[0])
