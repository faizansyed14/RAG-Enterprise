"""Discord server (guild) channel history as virtual files: one Markdown file per channel per day (UTC).

Endpoints (Discord API v10 documentation: "Guild: Get Guild / Get Guild Channels / List Active Guild Threads",
"Channel: Get Channel Messages", "Message object", "Reference: Rate Limits", "Reference: Snowflakes"):

  GET /guilds/{guild.id}                      the guild name (a path component; a failure falls back to the id).
  GET /guilds/{guild.id}/channels             every channel of the guild (an array; no pagination).
  GET /guilds/{guild.id}/threads/active       `{"threads": [...], "members": [...]}`: public threads that are not archived.
  GET /channels/{channel.id}/messages?limit=100&before={message.id}
        newest first; the next page asks `before` the oldest message id of this one; a page shorter than `limit` is
        the end. Each day is yielded only once complete; a resume position is a day boundary written as a snowflake
        (`(unix ms - 1420070400000) << 22`), so a day is never cut in half.
  GET /channels/{channel.id}                  only to find the guild when open() has to name a mentioned channel.

Auth: `Authorization: Bot <token>`. The bot needs View Channel and Read Message History in each channel; without the
privileged MESSAGE CONTENT intent Discord returns an empty `content` for most messages (the connector then indexes just
authors, attachment names and embeds): enable the intent in the developer portal.

Rate limits: after a response with `X-RateLimit-Remaining: 0` the next request waits `X-RateLimit-Reset-After` seconds
(so a bucket is never overrun); a 429 is retried after `Retry-After`, or after the `retry_after` of its JSON body when
the header is missing (global limits included). 401 is a bad token; a 403 / 404 on one channel (Missing Access, Unknown
Channel) skips that channel only.

Config: token_env (the bot token; never in the config), guilds (list of guild ids, required), base_url
(`https://discord.com/api/v10`), channels (names or ids to include, fnmatch; empty = every text channel the bot can
see), exclude_channels, include_threads (true: active public threads, as sub-folders of their parent), since (ISO date:
oldest day to index), rescan_days (7), include_private_channels (false), page_size (100, Discord's maximum), batch_size
(50 files per batch), max_items, max_pages, full_resync_hours (24), acl_rules, default_acl.

Objects: external_id `discord:<channel or thread id>:<YYYY-MM-DD>`, path `discord/<guild name>/<channel>/<YYYY-MM-DD>.md`
(a thread: `discord/<guild>/<parent>/<thread name>/<YYYY-MM-DD>.md`), version `<message count>-<hash of the rendered day>`.
System messages (joins, pins, boosts, thread-created notices) are left out; replies show whom they answer.

Incremental: after a finished scan the cursor holds the newest message time seen. The next scan re-reads the last
`rescan_days` before it (a deletion or an edit carries no timestamp the listing can filter on) and emits a delete for
every day of that window with no messages left. Older edits are picked up by the periodic full scan, which is also the
only authoritative one (`complete_scope=""`). (Until the sync layer persists the cursor of a finished scan, every scan
is a full one.)

Fail closed: a channel where @everyone is denied View Channel (a private channel) is NOT indexed unless
`include_private_channels` says so, and a channel that stops being listed has its days removed by the next authoritative
scan; private threads are never listed by the active-threads endpoint for a bot that is not a member.

Permissions: PREFIX-LEVEL MAPPING CONFIGURED BY THE CLIENT (`acl_rules` keyed by the path, e.g. `discord/Acme/announcements`
readable by `group:everyone`; no match and no `default_acl` => nobody). It is NOT Discord's role / channel permission
model; roles and overwrites are not mirrored. Mirror them in the rules.

What this does NOT do: archived threads, forum posts that are archived, direct messages, voice / stage channels, message
history before the bot joined, reactions, edit history, stickers, attachment contents (names only), voice transcripts.

Not exercised against a live Discord; tested against fixtures shaped like the vendor documentation.
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

from app.scale.connectors._http import AuthError, Http, HttpError, env_secret, render_document, safe_name
from app.scale.connectors._objects import Budget, ScanState, parse_dt, redacted, short_hash
from app.scale.connectors.base import AclRules, Change, ScanBatch, SourceFile

log = logging.getLogger(__name__)

DISCORD_EPOCH_MS = 1420070400000
TEXT_CHANNELS = {0, 5}                  # GUILD_TEXT, GUILD_ANNOUNCEMENT
PUBLIC_THREADS = {10, 11}               # ANNOUNCEMENT_THREAD, PUBLIC_THREAD
CONTENT_TYPES = {0, 19, 20}             # DEFAULT, REPLY, CHAT_INPUT_COMMAND; joins, pins, boosts... are skipped
VIEW_CHANNEL = 1 << 10
_ID = re.compile(r"^\d{5,25}$")
_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_MENTION = re.compile(r"<(@&|@!?|#|a?:|t:)([^<>:]+)(?::([^<>]*))?>")      # `@&` (a role) before `@`


_UNIX = datetime(1970, 1, 1, tzinfo=timezone.utc)


def snowflake(when: datetime) -> str:
    """The smallest message id created at `when` (a day boundary is a snowflake without any worker / sequence bits).
    Integer arithmetic only: a float timestamp can land one millisecond short."""
    delta = when.astimezone(timezone.utc) - _UNIX
    millis = (delta.days * 86400 + delta.seconds) * 1000 + delta.microseconds // 1000
    return str(max(millis - DISCORD_EPOCH_MS, 0) << 22)


def snowflake_time(value: str) -> datetime:
    return _UNIX + timedelta(milliseconds=(int(value) >> 22) + DISCORD_EPOCH_MS)


class RetryAfterFromBody(httpx.BaseTransport):
    """A Discord 429 carries `retry_after` (seconds, fractional) in its JSON body and, usually, as the `Retry-After`
    header. When the header is missing, add it from the body so `_http.Http` waits as long as asked."""

    def __init__(self, inner: httpx.BaseTransport) -> None:
        self.inner = inner

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        response = self.inner.handle_request(request)
        if response.status_code != 429 or response.headers.get("retry-after"):
            return response
        response.read()
        try:
            wait = float(response.json().get("retry_after"))
        except (ValueError, TypeError, AttributeError):
            return response
        headers = dict(response.headers)
        headers["retry-after"] = f"{max(wait, 0.0):.3f}"
        response.close()
        return httpx.Response(429, headers=headers, content=response.content, request=request)

    def close(self) -> None:
        self.inner.close()


class DiscordConnector:
    kind = "discord"

    def __init__(
        self, config: dict, *, transport: httpx.BaseTransport | None = None, client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.time,
    ) -> None:
        self.cfg = config
        self.guilds = [str(g) for g in config.get("guilds") or []]
        if not self.guilds or not all(_ID.match(g) for g in self.guilds):
            raise ValueError("discord: config needs `guilds` (a list of guild ids, e.g. [\"123456789012345678\"])")
        self.include = [c.lstrip("#").lower() for c in config.get("channels") or []]
        self.exclude = [c.lstrip("#").lower() for c in config.get("exclude_channels") or []]
        self.threads = bool(config.get("include_threads", True))
        self.private = bool(config.get("include_private_channels", False))
        self.since_floor = parse_dt(config.get("since"))
        self.rescan = timedelta(days=int(config.get("rescan_days", 7)))
        self.page_size = max(1, min(int(config.get("page_size", 100)), 100))
        self.batch_size = int(config.get("batch_size", 50))
        self.full_hours = float(config.get("full_resync_hours", 24))
        self.rules = AclRules(config.get("acl_rules"), config.get("default_acl"))
        self.clock, self._sleep = clock, sleep
        if client is None:
            transport = RetryAfterFromBody(transport or httpx.HTTPTransport())
        self.http = Http(config.get("base_url", "https://discord.com/api/v10"), transport=transport, client=client, sleep=sleep)
        self._cooldown = 0.0
        self._channel_names: dict[str, str] = {}

    # ── api ──

    def _get(self, path: str, params: dict | None = None) -> httpx.Response:
        token = env_secret(self.cfg.get("token_env"), "Discord bot token")
        if self._cooldown > 0:                                     # the previous response said its bucket is empty
            self._sleep(self._cooldown)
            self._cooldown = 0.0
        try:
            response = self.http.get(path, params=params, headers={"Authorization": f"Bot {token}"})
        except AuthError as exc:
            raise redacted(exc, token, hint=f"check the token in environment variable {self.cfg.get('token_env')}" if exc.status == 401 else "") from None
        except HttpError as exc:
            raise redacted(exc, token) from None
        if response.headers.get("x-ratelimit-remaining") == "0":
            try:
                self._cooldown = min(max(float(response.headers.get("x-ratelimit-reset-after", "0")), 0.0), 120.0)
            except ValueError:
                self._cooldown = 1.0
        return response

    def _json(self, path: str, params: dict | None = None):
        return self._get(path, params).json()

    # ── channels ──

    def _wanted(self, name: str, channel_id: str, parent: str = "") -> bool:
        names = {name.lower(), channel_id, parent.lower()} - {""}
        if self.include and not any(fnmatch.fnmatch(n, pat) for n in names for pat in self.include):
            return False
        return not any(fnmatch.fnmatch(n, pat) for n in names for pat in self.exclude)

    def _is_private(self, channel: dict, guild_id: str) -> bool:
        """@everyone (the role whose id is the guild id) is denied View Channel."""
        for overwrite in channel.get("permission_overwrites") or []:
            if str(overwrite.get("id")) == guild_id and str(overwrite.get("type", 0)) in ("0", "role"):
                try:
                    if int(overwrite.get("deny") or 0) & VIEW_CHANNEL:
                        return True
                except ValueError:
                    return True                                      # a permission value we cannot read: treat as private
        return False

    def _channels(self, budget: Budget) -> list[dict]:
        found: list[dict] = []
        for guild_id in self.guilds:
            if not budget.allow_page(("channels", guild_id)):
                break
            try:
                guild_name = str(self._json(f"/guilds/{guild_id}").get("name") or guild_id)
            except HttpError as exc:
                if exc.status == 401:
                    raise
                guild_name = guild_id
            top: dict[str, dict] = {}
            for channel in self._json(f"/guilds/{guild_id}/channels"):
                if channel.get("type") not in TEXT_CHANNELS:
                    continue
                if not self.private and self._is_private(channel, guild_id):
                    continue
                if not self._wanted(str(channel.get("name", "")), str(channel["id"])):
                    continue
                folder = safe_name(str(channel.get("name") or channel["id"]), str(channel["id"]), 80)
                top[str(channel["id"])] = {"id": str(channel["id"]), "name": str(channel.get("name") or channel["id"]),
                                           "folder": f"{safe_name(guild_name, guild_id, 80)}/{folder}", "guild": guild_id}
            found.extend(top.values())
            if self.threads:
                for thread in (self._json(f"/guilds/{guild_id}/threads/active") or {}).get("threads") or []:
                    parent = top.get(str(thread.get("parent_id")))
                    if parent is None or thread.get("type") not in PUBLIC_THREADS:
                        continue
                    if not self._wanted(str(thread.get("name", "")), str(thread["id"]), parent["name"]):
                        continue
                    name = str(thread.get("name") or thread["id"])
                    found.append({"id": str(thread["id"]), "name": f"{parent['name']}/{name}", "guild": guild_id,
                                  "folder": f"{parent['folder']}/{safe_name(name, str(thread['id']), 80)}"})
        for channel in found:
            self._channel_names[channel["id"]] = channel["name"]
        return sorted(found, key=lambda c: int(c["id"]))

    # ── days ──

    @staticmethod
    def _stamp(message: dict) -> datetime:
        return parse_dt(message.get("timestamp")) or snowflake_time(message["id"])

    @staticmethod
    def _boundary(day: date, plus_days: int = 0) -> datetime:
        return datetime(day.year, day.month, day.day, tzinfo=timezone.utc) + timedelta(days=plus_days)

    def _history_days(self, channel_id: str, oldest: datetime | None, latest: datetime | None, budget: Budget | None
                      ) -> Iterator[tuple[date, list[dict], datetime | None]]:
        """(day, messages oldest-first, resume bound) newest day first. A day is yielded only once complete."""
        before = snowflake(latest) if latest else None
        day, bucket, done = None, [], False
        while not done:
            if budget is not None and not budget.allow_page(("history", channel_id, before)):
                return
            params = {"limit": self.page_size, **({"before": before} if before else {})}
            page = self._json(f"/channels/{channel_id}/messages", params)
            if not isinstance(page, list):
                raise HttpError(f"discord: the message list of channel {channel_id} was not a list")
            for message in page:
                when = self._stamp(message)
                if oldest and when < oldest:
                    done = True
                    break
                if message.get("type", 0) not in CONTENT_TYPES:
                    continue
                message_day = when.date()
                if day is not None and message_day != day:
                    yield day, list(reversed(bucket)), self._boundary(day)
                    bucket = []
                day = message_day
                bucket.append(message)
            if len(page) < self.page_size:
                done = True
            else:
                before = str(min(int(m["id"]) for m in page))
        if bucket:
            yield day, list(reversed(bucket)), None

    # ── rendering ──

    def _channel_name(self, channel_id: str) -> str:
        return self._channel_names.get(channel_id, channel_id)

    def _ensure_channels(self, channel_id: str) -> None:
        """open() starts with an empty channel map; fetch the guild's channels once, only if a mention needs them."""
        if getattr(self, "_map_loaded", False):
            return
        self._map_loaded = True
        try:
            guild = str(self._json(f"/channels/{channel_id}").get("guild_id") or "")
            if _ID.match(guild):
                for channel in self._json(f"/guilds/{guild}/channels"):
                    self._channel_names.setdefault(str(channel["id"]), str(channel.get("name") or channel["id"]))
        except HttpError as exc:
            if exc.status == 401:
                raise
            log.warning("discord: could not name the channels mentioned in channel %s (%s)", channel_id, exc)

    def _text(self, message: dict, channel_id: str) -> str:
        users = {str(u["id"]): u.get("global_name") or u.get("username") or str(u["id"]) for u in message.get("mentions") or [] if "id" in u}
        content = message.get("content") or ""
        pieces = []
        for embed in message.get("embeds") or []:
            piece = "\n".join(p for p in (embed.get("title"), embed.get("description"),
                                          *[f"{f.get('name')}: {f.get('value')}" for f in embed.get("fields") or []]) if p)
            if piece.strip():
                pieces.append(piece)
        if "<#" in content or any("<#" in piece for piece in pieces):
            self._ensure_channels(channel_id)

        def replace(match: re.Match) -> str:
            kind, ident, extra = match.group(1), match.group(2), match.group(3)
            if kind == "@&":
                return f"@role-{ident}"
            if kind.startswith("@"):
                return "@" + users.get(ident, ident)
            if kind == "#":
                return "#" + self._channel_name(ident)
            if kind == "t:":
                try:
                    return datetime.fromtimestamp(int(ident), tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
                except (ValueError, OverflowError, OSError):
                    return match.group(0)
            return f":{ident}:"                                    # <:name:id> and <a:name:id>

        parts = [_MENTION.sub(replace, content).strip()]
        parts.extend(_MENTION.sub(replace, piece).strip() for piece in pieces)
        for item in message.get("attachments") or []:
            parts.append(f"[file: {item.get('filename') or 'unnamed'}]")
        return "\n".join(p for p in parts if p)

    @staticmethod
    def _who(message: dict) -> str:
        author = message.get("author") or {}
        name = author.get("global_name") or author.get("username") or str(author.get("id") or "unknown")
        return f"{name} (bot)" if author.get("bot") else name

    def _line(self, message: dict, channel_id: str) -> str:
        first, *rest = (self._text(message, channel_id) or "(no text)").split("\n")
        tail = "".join(f"\n    {line}" for line in rest)
        replied = (message.get("referenced_message") or {}).get("author") if message.get("type") == 19 else None
        reply = f" (reply to {replied.get('global_name') or replied.get('username')})" if replied else ""
        edited = " (edited)" if message.get("edited_timestamp") else ""
        return f"[{self._stamp(message):%H:%M}] {self._who(message)}{reply}: {first}{tail}{edited}"

    def _render_day(self, channel_id: str, channel_name: str, day: date, messages: list[dict]) -> bytes:
        body = "\n".join(self._line(m, channel_id) for m in messages)
        return render_document(f"#{channel_name} {day.isoformat()}", {"channel": f"#{channel_name}", "date": f"{day.isoformat()} (UTC)",
                                                                    "messages": len(messages)}, body)

    @staticmethod
    def _external_id(channel_id: str, day: date) -> str:
        return f"discord:{channel_id}:{day.isoformat()}"

    def _change(self, channel: dict, day: date, messages: list[dict], state: ScanState) -> Change:
        text = self._render_day(channel["id"], channel["name"], day, messages)
        for message in messages:
            state.observe(self._stamp(message))
        path = f"discord/{channel['folder']}/{day.isoformat()}.md"
        ext = self._external_id(channel["id"], day)
        sf = SourceFile(
            external_id=ext, path=path, name=f"{safe_name(channel['name'].replace('/', '-'), channel['id'], 80)}-{day.isoformat()}.md", size=len(text),
            modified_at=max(self._stamp(m) for m in messages), version=f"{len(messages)}-{short_hash(text)}", acl=self.rules.for_path(path),
            mime="text/markdown", extra={"channel_id": channel["id"], "channel": channel["name"]},
        )
        return Change("upsert", ext, sf)

    # ── scan ──

    def _window(self, state: ScanState) -> datetime | None:
        """Midnight (UTC) of the oldest day to read, or None for the whole history."""
        start = None
        if state.incremental and state.since:
            start = (parse_dt(state.since) or parse_dt(0)) - self.rescan
        if self.since_floor and (start is None or self.since_floor > start):
            start = self.since_floor
        return datetime(start.year, start.month, start.day, tzinfo=timezone.utc) if start else None

    def scan(self, cursor: dict) -> Iterator[ScanBatch]:
        state = ScanState(cursor, full_resync_hours=self.full_hours, clock=self.clock)
        budget = Budget.from_config(self.cfg)
        self._map_loaded = False
        channels = [] if state.pos.get("finished") else self._channels(budget)
        window = self._window(state)
        resume_id, resume_latest = state.pos.get("ch"), parse_dt(state.pos.get("latest"))
        today = datetime.fromtimestamp(state.started, tz=timezone.utc).date()
        pending: list[Change] = []
        for index, channel in enumerate(channels):
            if resume_id and int(channel["id"]) < int(resume_id):
                continue
            latest = resume_latest if channel["id"] == resume_id else None
            seen: set[date] = set()
            skipped = False
            try:
                for day, messages, bound in self._history_days(channel["id"], window, latest, budget):
                    if not budget.clip([day]):
                        break
                    pending.append(self._change(channel, day, messages, state))
                    seen.add(day)
                    if bound is not None and len(pending) >= self.batch_size:
                        yield ScanBatch(pending, state.cursor(ch=channel["id"], latest=bound.isoformat()))
                        pending = []
            except HttpError as exc:
                if exc.status not in (403, 404):
                    raise
                log.warning("discord: channel %s cannot be read (HTTP %s); it is treated as empty", channel["name"], exc.status)
                skipped = True
            if budget.truncated:
                if pending:
                    yield ScanBatch(pending, state.cursor(finished=True))
                break
            if state.incremental and window and latest is None and not skipped:       # window days with nothing left in them
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
        try:
            if len(parts) != 3 or parts[0] != "discord" or not _ID.match(parts[1]) or not _DAY.match(parts[2]):
                raise ValueError(file.external_id)
            channel_id, day = parts[1], date.fromisoformat(parts[2])
        except ValueError:
            raise HttpError(f"discord: {file.external_id!r} is not a channel-day id") from None
        segments = file.path.split("/")
        name = "/".join(segments[2:-1]) if len(segments) >= 4 else channel_id
        self._channel_names.setdefault(channel_id, name)
        self._map_loaded = False
        messages: list[dict] = []
        for found_day, found, _ in self._history_days(channel_id, self._boundary(day), self._boundary(day, 1), None):
            if found_day == day:
                messages = found
        return io.BytesIO(self._render_day(channel_id, name, day, messages))

    def refresh_acl(self, file: SourceFile) -> list[str]:
        return self.rules.for_path(file.path)
