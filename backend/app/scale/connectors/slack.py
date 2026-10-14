"""Slack channel history as virtual files: one Markdown file per channel per day (UTC), threads included.

Endpoints (Slack Web API methods "conversations.list", "conversations.history", "conversations.replies",
"users.list"; docs.slack.dev "Pagination", "Rate limits" and the "ok / error" response convention):

  GET conversations.list?types=public_channel,private_channel&exclude_archived=true&limit=200&cursor=C
        cursor pagination: `response_metadata.next_cursor`, empty on the last page.
  GET conversations.history?channel=C&oldest=TS&latest=TS&inclusive=true&limit=200&cursor=C
        newest message first; `has_more` + `response_metadata.next_cursor`. `oldest` is always a day boundary, so a
        day is never cut in half; the scan streams days (newest to oldest) and keeps only one day in memory.
  GET conversations.replies?channel=C&ts=PARENT_TS&limit=200&cursor=C
        for every thread parent (`reply_count` > 0); the first message returned is the parent itself.
  GET users.list?limit=200&cursor=C      user ids -> names, fetched once per connector and cached. Needs `users:read`;
        without it names stay as ids (the scan does not fail).

Errors: Slack answers HTTP 200 with {"ok": false, "error": "..."}. `invalid_auth`, `not_authed`, `token_revoked`,
`account_inactive`, `missing_scope`... raise AuthError (never retried, never showing the token); `ratelimited` (also when
sent as 200) honours Retry-After; a channel that answers `not_in_channel` / `channel_not_found` / `is_archived` is skipped.
HTTP 429 / 5xx are retried by `_http.Http`.

Config: token_env (a bot token with channels:history, channels:read, groups:history, groups:read, users:read),
base_url, channels (names or ids to include; empty = every channel the token can see), exclude_channels, types,
since (ISO date: oldest day to index), rescan_days (7), include_threads (true), resolve_users (true), page_size (200),
batch_size (50 files per batch), max_items, max_pages, full_resync_hours (24), acl_rules, default_acl.

Objects: external_id `slack:<channel id>:<YYYY-MM-DD>`, path `slack/<channel name>/<YYYY-MM-DD>.md`, version
`<message count>-<hash of the rendered day>` (Slack has no per-day version; edits and new replies change the hash).
A thread's replies are rendered under the parent message, in the parent's day, even when posted later.

Incremental: after a finished scan the cursor holds the newest message time seen. The next scan re-reads the last
`rescan_days` before it (edits, deletions and new thread replies carry no timestamp that history can filter on, so
a window is the only way to see them) and emits a delete for every day in that window that has no messages left
(a full scan needs no such deletes: it is authoritative).
Changes older than the window (an edit to a month-old message, a reply to an old thread) are picked up by the
periodic full scan (`full_resync_hours`), which is also the only authoritative one. (Until the sync layer persists the
cursor of a finished scan, every scan is a full one.) Slack's own limits apply: since 2025 commercially distributed
apps that are not in the Marketplace get very low rate limits on conversations.history / replies.

Permissions: PREFIX-LEVEL MAPPING CONFIGURED BY THE CLIENT (`acl_rules` keyed by the path, e.g. `slack/hr-private` is
readable by `group:hr`; no match and no `default_acl` => nobody). It is NOT Slack's membership model: channel
membership is not mirrored, so private channels need an explicit rule that says who may read them.

What this does NOT do: direct messages unless asked for through `types` (and then they need rules too), files and
canvases (only their names), reactions, message edit history, huge-workspace sharding, Enterprise Grid org-wide tokens.

Not exercised against a live Slack workspace; tested against fixtures shaped like the vendor documentation.
"""

from __future__ import annotations

import fnmatch
import io
import logging
import re
import time
from datetime import date, datetime, timedelta, timezone
from typing import BinaryIO, Callable, Iterator

import httpx

from app.scale.connectors._http import AuthError, Http, HttpError, env_secret, parse_retry_after, render_document, safe_name
from app.scale.connectors._objects import Budget, ScanState, parse_dt, redacted, short_hash
from app.scale.connectors.base import AclRules, Change, ScanBatch, SourceFile

log = logging.getLogger(__name__)

AUTH_ERRORS = {"invalid_auth", "not_authed", "token_revoked", "token_expired", "account_inactive", "missing_scope",
               "no_permission", "org_login_required", "access_denied", "team_access_not_granted", "invalid_token"}
SKIP_CHANNEL_ERRORS = {"not_in_channel", "channel_not_found", "is_archived", "missing_channel", "access_denied_channel"}
SKIP_SUBTYPES = {"channel_join", "channel_leave", "group_join", "group_leave", "channel_topic", "channel_purpose",
                 "channel_name", "channel_archive", "channel_unarchive", "group_topic", "group_purpose", "group_name",
                 "group_archive", "group_unarchive", "pinned_item", "unpinned_item", "tombstone", "reminder_add"}
_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_CHANNEL_ID = re.compile(r"^[A-Z0-9]{2,32}$")
_TOKEN = re.compile(r"<([^<>]+)>")


class SlackError(HttpError):
    def __init__(self, message: str, status: int | None = None, code: str = "") -> None:
        super().__init__(message, status)
        self.code = code


def slack_text(text: str, users: dict[str, str], channels: dict[str, str]) -> str:
    """Slack mrkdwn -> readable text: mentions, channel refs, links and HTML entities."""

    def replace(match: re.Match) -> str:
        body = match.group(1)
        head, _, label = body.partition("|")
        if head.startswith("@"):
            uid = head[1:]
            return "@" + (label or users.get(uid) or uid)
        if head.startswith("#"):
            cid = head[1:]
            return "#" + (label or channels.get(cid) or cid)
        if head.startswith("!"):
            name = head[1:].split("^", 1)[0]
            if name in ("here", "channel", "everyone"):
                return f"@{name}"
            if name == "subteam":
                return label if label.startswith("@") else f"@{label or 'group'}"
            return label or name                                   # <!date^...|fallback> and friends
        if head.startswith(("http://", "https://", "mailto:")):
            shown = label or head.removeprefix("mailto:")
            return head if shown == head else (shown if head.startswith("mailto:") else f"[{shown}]({head})")
        return match.group(0)

    text = _TOKEN.sub(replace, text or "")
    return text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


class SlackConnector:
    kind = "slack"

    def __init__(
        self, config: dict, *, transport: httpx.BaseTransport | None = None, client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.time,
    ) -> None:
        self.cfg = config
        self.types = config.get("types", "public_channel,private_channel")
        self.include = [c.lstrip("#").lower() for c in config.get("channels") or []]
        self.exclude = [c.lstrip("#").lower() for c in config.get("exclude_channels") or []]
        self.since_floor = parse_dt(config.get("since"))
        self.rescan = timedelta(days=int(config.get("rescan_days", 7)))
        self.include_threads = bool(config.get("include_threads", True))
        self.resolve_users = bool(config.get("resolve_users", True))
        self.page_size = int(config.get("page_size", 200))
        self.batch_size = int(config.get("batch_size", 50))
        self.full_hours = float(config.get("full_resync_hours", 24))
        self.rules = AclRules(config.get("acl_rules"), config.get("default_acl"))
        self.clock, self._sleep = clock, sleep
        self.http = Http(config.get("base_url", "https://slack.com/api"), transport=transport, client=client, sleep=sleep)
        self._users: dict[str, str] | None = None
        self._channel_names: dict[str, str] = {}

    # ── api ──

    def _call(self, method: str, params: dict) -> dict:
        token = env_secret(self.cfg.get("token_env"), "Slack bot / user token")
        for _ in range(8):
            try:
                response = self.http.get(f"/{method}", params=params, headers={"Authorization": f"Bearer {token}"})
            except HttpError as exc:
                raise redacted(exc, token, hint=f"check the token in environment variable {self.cfg.get('token_env')}" if isinstance(exc, AuthError) else "") from None
            try:
                body = response.json()
            except ValueError:
                raise HttpError(f"slack {method}: the response was not JSON", response.status_code) from None
            if body.get("ok"):
                return body
            error = str(body.get("error", "unknown_error"))
            if error == "ratelimited":
                self._sleep(min(parse_retry_after(response.headers.get("Retry-After"), 1.0), 120.0))
                continue
            if error in AUTH_ERRORS:
                needed = f", needed scope: {body['needed']}" if body.get("needed") else ""
                raise AuthError(f"slack {method}: {error}{needed} (check the token in environment variable {self.cfg.get('token_env')})", 401)
            raise SlackError(f"slack {method}: {error}", None, error)
        raise HttpError(f"slack {method}: still rate limited after retries", 429)

    def _pages(self, method: str, key: str, params: dict) -> Iterator[list[dict]]:
        cursor, seen = None, set()
        while True:
            body = self._call(method, {**params, **({"cursor": cursor} if cursor else {})})
            yield body.get(key) or []
            cursor = (body.get("response_metadata") or {}).get("next_cursor")
            if not cursor:
                return
            if cursor in seen:
                raise HttpError(f"slack {method}: pagination did not advance (the same cursor came back twice)")
            seen.add(cursor)

    # ── channels and users ──

    def _wanted(self, channel: dict) -> bool:
        names = {str(channel.get("name", "")).lower(), str(channel.get("id", "")).lower()}
        if self.include and not any(fnmatch.fnmatch(n, pat) for n in names for pat in self.include):
            return False
        return not any(fnmatch.fnmatch(n, pat) for n in names for pat in self.exclude)

    def _channels(self, budget: Budget) -> list[dict]:
        found: list[dict] = []
        cursor = None
        while True:
            if not budget.allow_page(("channels", cursor)):
                break
            body = self._call("conversations.list", {"types": self.types, "exclude_archived": "true", "limit": 200,
                                                     **({"cursor": cursor} if cursor else {})})
            found.extend(c for c in body.get("channels") or [] if not c.get("is_archived") and self._wanted(c))
            cursor = (body.get("response_metadata") or {}).get("next_cursor")
            if not cursor:
                break
        for c in found:
            self._channel_names[c["id"]] = c.get("name") or c["id"]
        return sorted(found, key=lambda c: c["id"])

    def _user_names(self) -> dict[str, str]:
        if self._users is None:
            self._users = {}
            if self.resolve_users:
                try:
                    for members in self._pages("users.list", "members", {"limit": 200}):
                        for m in members:
                            profile = m.get("profile") or {}
                            self._users[m["id"]] = (m.get("real_name") or profile.get("real_name") or profile.get("display_name")
                                                    or m.get("name") or m["id"])
                except AuthError as exc:
                    log.warning("slack: users.list refused (%s); user ids will not be resolved to names", exc)
        return self._users

    # ── days ──

    @staticmethod
    def _day_of(ts: str) -> date:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).date()

    @staticmethod
    def _boundary(day: date, plus_days: int = 0) -> str:
        return f"{(datetime(day.year, day.month, day.day, tzinfo=timezone.utc) + timedelta(days=plus_days)).timestamp():.6f}"

    @staticmethod
    def _top_level(message: dict) -> bool:
        if message.get("subtype") in SKIP_SUBTYPES:
            return False
        thread = message.get("thread_ts")
        return not thread or thread == message.get("ts")        # replies (and their broadcasts) live under the parent

    def _history_days(self, channel_id: str, oldest: str | None, latest: str | None, budget: Budget | None
                      ) -> Iterator[tuple[date, list[dict], str | None]]:
        """(day, messages oldest-first, resume bound) newest day first. A day is yielded only once complete."""
        cursor, day, bucket = None, None, []
        while True:
            if budget is not None and not budget.allow_page(("history", channel_id, latest, cursor)):
                return
            params = {"channel": channel_id, "limit": self.page_size}
            if oldest:
                params.update(oldest=oldest, inclusive="true")
            if latest:
                params["latest"] = latest
            if cursor:
                params["cursor"] = cursor
            body = self._call("conversations.history", params)
            for message in body.get("messages") or []:
                if not self._top_level(message) or not message.get("ts"):
                    continue
                if latest and float(message["ts"]) >= float(latest):
                    continue                                       # `inclusive` applies to both bounds; `latest` is exclusive here
                message_day = self._day_of(message["ts"])
                if day is not None and message_day != day:
                    yield day, list(reversed(bucket)), self._boundary(message_day, 1)
                    bucket = []
                day = message_day
                bucket.append(message)
            cursor = (body.get("response_metadata") or {}).get("next_cursor") if body.get("has_more") else None
            if not cursor:
                break
        if bucket:
            yield day, list(reversed(bucket)), None

    def _replies(self, channel_id: str, thread_ts: str) -> list[dict]:
        replies: list[dict] = []
        try:
            for page in self._pages("conversations.replies", "messages", {"channel": channel_id, "ts": thread_ts, "limit": self.page_size}):
                replies.extend(m for m in page if m.get("ts") != thread_ts and m.get("subtype") not in SKIP_SUBTYPES)
        except SlackError as exc:
            if exc.code != "thread_not_found":
                raise
        return replies

    # ── rendering ──

    def _who(self, message: dict) -> str:
        users = self._user_names()
        uid = message.get("user")
        return (users.get(uid) if uid else None) or message.get("username") or (message.get("bot_profile") or {}).get("name") \
            or uid or message.get("bot_id") or "unknown"

    def _message_text(self, message: dict) -> str:
        users, names = self._user_names(), self._channel_names
        parts = [slack_text(message.get("text", ""), users, names).strip()]
        for attachment in message.get("attachments") or []:
            piece = attachment.get("text") or attachment.get("fallback") or attachment.get("title") or ""
            if piece.strip():
                parts.append(slack_text(piece, users, names).strip())
        for item in message.get("files") or []:
            parts.append(f"[file: {item.get('name') or item.get('title') or 'unnamed'}]")
        return "\n".join(p for p in parts if p)

    def _line(self, message: dict, indent: str = "") -> str:
        when = datetime.fromtimestamp(float(message["ts"]), tz=timezone.utc)
        first, *rest = (self._message_text(message) or "(no text)").split("\n")
        tail = "".join(f"\n{indent}    {line}" for line in rest)
        return f"{indent}[{when:%H:%M}] {self._who(message)}: {first}{tail}{' (edited)' if message.get('edited') else ''}"

    def _render_day(self, channel_id: str, channel_name: str, day: date, messages: list[dict]) -> bytes:
        lines: list[str] = []
        count = 0
        for message in messages:
            lines.append(self._line(message))
            count += 1
            if self.include_threads and (message.get("reply_count") or 0) > 0:
                for reply in self._replies(channel_id, message.get("thread_ts") or message["ts"]):
                    lines.append(self._line(reply, "    ↳ "))
                    count += 1
        title = f"#{channel_name} {day.isoformat()}"
        return render_document(title, {"channel": f"#{channel_name}", "date": f"{day.isoformat()} (UTC)", "messages": count}, "\n".join(lines))

    @staticmethod
    def _external_id(channel_id: str, day: date) -> str:
        return f"slack:{channel_id}:{day.isoformat()}"

    def _change(self, channel: dict, day: date, messages: list[dict], state: ScanState) -> Change:
        name = channel.get("name") or channel["id"]
        text = self._render_day(channel["id"], name, day, messages)
        for message in messages:
            state.observe(float(message["ts"]))
        path = f"slack/{safe_name(name, channel['id'], 80)}/{day.isoformat()}.md"
        ext = self._external_id(channel["id"], day)
        modified = max(datetime.fromtimestamp(float(m["ts"]), tz=timezone.utc) for m in messages)
        sf = SourceFile(
            external_id=ext, path=path, name=f"{safe_name(name, channel['id'], 80)}-{day.isoformat()}.md", size=len(text),
            modified_at=modified, version=f"{len(messages)}-{short_hash(text)}", acl=self.rules.for_path(path),
            mime="text/markdown", extra={"channel_id": channel["id"], "channel": name},
        )
        return Change("upsert", ext, sf)

    # ── scan ──

    def _window(self, state: ScanState) -> datetime | None:
        """Midnight (UTC) of the oldest day to read, or None for the whole history."""
        start = None
        if state.incremental and state.since:
            watermark = parse_dt(state.since)
            start = watermark - self.rescan
        if self.since_floor and (start is None or self.since_floor > start):
            start = self.since_floor
        return datetime(start.year, start.month, start.day, tzinfo=timezone.utc) if start else None

    def scan(self, cursor: dict) -> Iterator[ScanBatch]:
        state = ScanState(cursor, full_resync_hours=self.full_hours, clock=self.clock)
        budget = Budget.from_config(self.cfg)
        channels = [] if state.pos.get("finished") else self._channels(budget)
        window = self._window(state)
        oldest = f"{window.timestamp():.6f}" if window else None
        resume_id, resume_latest = state.pos.get("ch"), state.pos.get("latest")
        today = datetime.fromtimestamp(state.started, tz=timezone.utc).date()
        pending: list[Change] = []
        for index, channel in enumerate(channels):
            if resume_id and channel["id"] < resume_id:
                continue
            latest = resume_latest if channel["id"] == resume_id else None
            seen: set[date] = set()
            skipped = False
            try:
                for day, messages, bound in self._history_days(channel["id"], oldest, latest, budget):
                    if not budget.clip([day]):
                        break
                    pending.append(self._change(channel, day, messages, state))
                    seen.add(day)
                    if bound is not None and len(pending) >= self.batch_size:
                        yield ScanBatch(pending, state.cursor(ch=channel["id"], latest=bound))
                        pending = []
            except SlackError as exc:
                if exc.code not in SKIP_CHANNEL_ERRORS:
                    raise
                log.warning("slack: channel %s cannot be read (%s); it is treated as empty", channel.get("name"), exc.code)
                skipped = True
            if budget.truncated:
                if pending:
                    yield ScanBatch(pending, state.cursor(finished=True))
                break
            if state.incremental and window and latest is None and not skipped:   # window days with nothing left in them
                day = window.date()
                while day <= today:
                    if day not in seen:
                        pending.append(Change("delete", self._external_id(channel["id"], day)))
                    day += timedelta(days=1)
            upcoming = channels[index + 1]["id"] if index + 1 < len(channels) else None
            yield ScanBatch(pending, state.cursor(ch=upcoming) if upcoming else state.cursor(finished=True))
            pending = []
        yield ScanBatch([], state.final(truncated=budget.truncated), done=True,
                        complete_scope=state.complete_scope("", truncated=budget.truncated))

    # ── bytes and permissions ──

    def open(self, file: SourceFile) -> BinaryIO:
        parts = file.external_id.split(":")
        if len(parts) != 3 or parts[0] != "slack" or not _CHANNEL_ID.match(parts[1]) or not _DAY.match(parts[2]):
            raise HttpError(f"slack: {file.external_id!r} is not a channel-day id")
        channel_id, day = parts[1], date.fromisoformat(parts[2])
        segments = file.path.split("/")
        name = segments[1] if len(segments) >= 3 else channel_id
        self._channel_names.setdefault(channel_id, name)
        messages: list[dict] = []
        for found_day, found, _ in self._history_days(channel_id, self._boundary(day), self._boundary(day, 1), None):
            if found_day == day:
                messages = found
        return io.BytesIO(self._render_day(channel_id, name, day, messages))

    def refresh_acl(self, file: SourceFile) -> list[str]:
        return self.rules.for_path(file.path)
