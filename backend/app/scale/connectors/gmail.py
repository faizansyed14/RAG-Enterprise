"""Gmail messages as `.eml` files: one RFC 822 message per file, so the existing e-mail parser reads headers, body and attachments.

Endpoints (Gmail API v1, "Users.messages", "Users.history", "Users" reference; "Synchronizing Clients with Gmail"):

  GET /users/{userId}/profile                          `emailAddress` (a path component) and the mailbox's current `historyId`.
  GET /users/{userId}/messages?maxResults&pageToken&labelIds&includeSpamTrash&q
        message ids, 500 per page at most, `nextPageToken` until it is absent (`messages` is absent on an empty page).
  GET /users/{userId}/history?startHistoryId&pageToken&maxResults&historyTypes
        what changed after a history id: messagesAdded, messagesDeleted, labelsAdded, labelsRemoved. Every page repeats the
        mailbox's current `historyId`.
  GET /users/{userId}/messages/{id}?format=metadata    labels, `internalDate`, `sizeEstimate`, Subject / Date headers:
        used to settle one message that a history record touched, and (friendly_paths) to name each message.
  GET /users/{userId}/messages/{id}?format=raw         open(): `raw` is the whole message, base64url, decoded to the .eml bytes.

Auth: an OAuth 2.0 ACCESS TOKEN in the environment variable named by `token_env`, read at every request. Scope
`https://www.googleapis.com/auth/gmail.readonly` is enough. Getting one:
  * a person's mailbox: create an OAuth client in Google Cloud (APIs & Services > Credentials), enable the Gmail API, run the
    authorization-code flow once with `access_type=offline` to get a refresh token, then exchange it at
    `https://oauth2.googleapis.com/token` (grant_type=refresh_token) for an access token;
  * a Google Workspace mailbox: a service account with domain-wide delegation (Admin console > Security > API controls),
    scope above, `sub` = the mailbox; exchange its signed JWT at the same token endpoint (grant_type=jwt-bearer).
  Access tokens last about an hour and this connector does not refresh them: whatever starts the sync has to put a fresh token
  in the variable (a scan that outlives its token stops with a 401 and resumes from its cursor on the next run).
  Rate limits: 429, and the 403 `rateLimitExceeded` / `userRateLimitExceeded` Google also uses, are waited out and retried.

Config: token_env, user ("me", the token's mailbox), base_url (`https://gmail.googleapis.com/gmail/v1`), label_ids (messages must
carry all of them), include_spam_trash (false), q (a Gmail search, e.g. `after:2024/01/01 -in:drafts -category:promotions`),
friendly_paths (false), page_size (500), max_items, max_pages, full_resync_hours (24), acl_rules, default_acl.

Objects: external_id `gmail:<message id>`, path `gmail/<mailbox address>/<message id>.eml`. With `friendly_paths` the scan fetches the
headers of every listed message (one request each) and names it `gmail/<mailbox>/<year>/<date> <subject> [<id>].eml`; without it a
scan costs one list request per page and `size` / `modified_at` stay empty. A message never changes (labels do), so its `version`
is constant: it is read once.

Incremental: after a finished scan the cursor holds the mailbox `historyId` read BEFORE the listing began. The next scan reads
`history` from there, settles each touched message (deleted => delete; otherwise its current labels decide) and keeps the new
`historyId`. HTTP 404 on the history ("Requested entity was not found": the id is too old, Gmail keeps it for about a week at least) means
a FULL listing instead, which is authoritative (`complete_scope=""`: a message not listed is gone). Label changes only matter for
SPAM, TRASH and `label_ids`; read / starred / category toggles are ignored. A configured `q` cannot be applied to history, so with
`q` every scan is a full listing. If a history response carries no `historyId` the newest record id read is kept instead (or the old id when
there was nothing to read). A cursor with no history id to read from (or an expired one) starts a fresh, authoritative listing. A scan cut
short by max_items / max_pages never advances the history id, and a resume re-reads the page it was cut in. (Until the sync layer persists
the cursor of a finished scan, every scan is a full one.)

Fail-closed choices: spam and trash are not indexed (a message moved there becomes a delete); a message that cannot be fetched (404)
while it is being settled becomes a delete.

Permissions: PREFIX-LEVEL MAPPING CONFIGURED BY THE CLIENT (`acl_rules` keyed by the path, e.g. `gmail/support@acme.com` readable by
`group:support`; no match and no `default_acl` => nobody). It is NOT Gmail's permission model: a mailbox is private to its owner
in Gmail, and indexing it makes it readable by whoever the rules name. Index shared mailboxes, or get the owner's consent.

What this does NOT do: refresh access tokens, drafts handling (drafts are listed by Gmail; exclude them with `q: -in:drafts`),
threads as units, attachments beyond what the .eml parser extracts, Google Chat, delegates, Pub/Sub push (`users.watch`).

Not exercised against a live Gmail; tested against fixtures shaped like the vendor documentation.
Unsure of (marked in the tests): the 403 reasons Google uses for throttling, that every history page carries `historyId`, and whether `raw` is
base64url padded (both are accepted).
"""

from __future__ import annotations

import base64
import binascii
import io
import logging
import re
import time
from datetime import datetime, timezone
from email.header import decode_header, make_header
from typing import BinaryIO, Callable, Iterator

import httpx

from app.scale.connectors._http import AuthError, Http, HttpError, env_secret, safe_name
from app.scale.connectors._objects import Budget, ScanState, ThrottleAs429, redacted
from app.scale.connectors.base import AclRules, Change, ScanBatch, SourceFile

log = logging.getLogger(__name__)

_ID = re.compile(r"^[A-Za-z0-9_-]{4,64}$")
_USER = re.compile(r"^(me|[^\s/?#]+@[^\s/?#]+)$")
THROTTLE_REASONS = {"ratelimitexceeded", "userratelimitexceeded"}
TRASHED = {"SPAM", "TRASH"}


def _throttle_wait(response: httpx.Response) -> float | None:
    """Google reports some throttling as 403 with a reason; those are retried, other 403s are real permission errors."""
    try:
        errors = response.json().get("error", {}).get("errors") or []
    except (ValueError, AttributeError):
        return None
    reasons = {str(e.get("reason", "")).lower() for e in errors if isinstance(e, dict)}
    return 4.0 if reasons & THROTTLE_REASONS else None


def _decoded(value: str) -> str:
    try:
        return str(make_header(decode_header(value)))
    except Exception:  # noqa: BLE001 - a header that cannot be decoded is shown as it came
        return value


class GmailConnector:
    kind = "gmail"

    def __init__(
        self, config: dict, *, transport: httpx.BaseTransport | None = None, client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.time,
    ) -> None:
        self.cfg = config
        self.user = str(config.get("user", "me"))
        if not _USER.match(self.user):
            raise ValueError("gmail: `user` must be \"me\" or a mailbox address")
        self.labels = [str(x) for x in config.get("label_ids") or []]
        self.spam_trash = bool(config.get("include_spam_trash", False))
        self.q = str(config.get("q") or "").strip()
        self.friendly = bool(config.get("friendly_paths", False))
        self.page_size = max(1, min(int(config.get("page_size", 500)), 500))
        self.full_hours = 0.0 if self.q else float(config.get("full_resync_hours", 24))       # q cannot be applied to history
        self.rules = AclRules(config.get("acl_rules"), config.get("default_acl"))
        self.clock = clock
        if client is None:
            transport = ThrottleAs429(transport or httpx.HTTPTransport(), _throttle_wait)
        self.http = Http(config.get("base_url", "https://gmail.googleapis.com/gmail/v1"), transport=transport, client=client, sleep=sleep)
        self.relevant = set(self.labels) | (set() if self.spam_trash else TRASHED)                # labels whose changes matter

    # ── http ──

    def _get(self, path: str, params=None) -> httpx.Response:
        token = env_secret(self.cfg.get("token_env"), "Gmail access token")
        try:
            return self.http.get(f"/users/{self.user}{path}", params=params, headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
        except AuthError as exc:
            hint = f"the token in environment variable {self.cfg.get('token_env')} is missing, expired (they last about an hour) or lacks the gmail.readonly scope" if exc.status == 401 else ""
            raise redacted(exc, token, hint=hint) from None
        except HttpError as exc:
            raise redacted(exc, token) from None

    def _json(self, path: str, params=None):
        return self._get(path, params).json()

    # ── messages ──

    def _wanted(self, labels) -> bool:
        have = set(labels or [])
        return (self.spam_trash or not have & TRASHED) and set(self.labels) <= have

    def _meta(self, message_id: str) -> dict | None:
        """The message's current labels and headers, or None when it no longer exists."""
        try:
            return self._json(f"/messages/{message_id}", [("format", "metadata"), ("metadataHeaders", "Subject"), ("metadataHeaders", "Date")])
        except HttpError as exc:
            if exc.status == 404:
                return None
            raise

    @staticmethod
    def _header(meta: dict, name: str) -> str:
        for header in (meta.get("payload") or {}).get("headers") or []:
            if str(header.get("name", "")).lower() == name.lower():
                return _decoded(str(header.get("value", "")))
        return ""

    def _file(self, mailbox: str, message_id: str, meta: dict | None) -> SourceFile:
        path, size, when = f"gmail/{mailbox}/{message_id}.eml", 0, None
        if self.friendly and meta:
            try:
                when = datetime.fromtimestamp(int(meta["internalDate"]) / 1000, tz=timezone.utc)
            except (KeyError, ValueError, TypeError, OverflowError, OSError):
                when = None
            subject = self._header(meta, "Subject") or "(no subject)"
            stem = safe_name(f"{when:%Y-%m-%d} {subject}" if when else subject, "message", 100)
            path = f"gmail/{mailbox}/{when.year if when else 'undated'}/{stem} [{message_id}].eml"
            size = int(meta.get("sizeEstimate") or 0)
        return SourceFile(
            external_id=f"gmail:{message_id}", path=path, name=path.rsplit("/", 1)[-1], size=size, modified_at=when, version="1",
            acl=self.rules.for_path(path), mime="message/rfc822", extra={"id": message_id},
        )

    def _upsert(self, mailbox: str, message_id: str, meta: dict | None = None) -> Change:
        if self.friendly and meta is None:
            meta = self._meta(message_id)
            if meta is None:
                return Change("delete", f"gmail:{message_id}")
        return Change("upsert", f"gmail:{message_id}", self._file(mailbox, message_id, meta))

    def _settle(self, mailbox: str, message_id: str) -> Change:
        """What a history record means for one message, decided by the message as it is now."""
        meta = self._meta(message_id)
        if meta is None or not self._wanted(meta.get("labelIds")):
            return Change("delete", f"gmail:{message_id}")
        return Change("upsert", f"gmail:{message_id}", self._file(mailbox, message_id, meta))

    # ── scan: full listing ──

    def _full(self, state: ScanState, budget: Budget, mailbox: str, token: str | None, hid: str, prior: str | None) -> Iterator[ScanBatch]:
        params: list[tuple[str, object]] = [("maxResults", self.page_size)]
        params += [("labelIds", x) for x in self.labels]
        if self.spam_trash:
            params.append(("includeSpamTrash", "true"))
        if self.q:
            params.append(("q", self.q))
        extra = {"prior": prior} if prior else {}
        while True:
            if not budget.allow_page(("list", token)):
                return
            body = self._json("/messages", params + ([("pageToken", token)] if token else []))
            ids = [str(m["id"]) for m in body.get("messages") or [] if m.get("id")]
            changes = [self._upsert(mailbox, mid) for mid in budget.clip(ids)]
            following = body.get("nextPageToken") or None
            if budget.truncated:                     # part of this page was left out: a resume must read this page again
                cursor = state.cursor(phase="list", token=token, hid=hid, **extra)
            elif following:
                cursor = state.cursor(phase="list", token=following, hid=hid, **extra)
            else:
                cursor = state.cursor(phase="done", hid=hid, **extra)
            yield ScanBatch(changes, cursor)
            if budget.truncated or not following:
                return
            token = following

    # ── scan: history ──

    def _history_types(self) -> list[tuple[str, str]]:
        types = ["messageAdded", "messageDeleted"] + (["labelAdded", "labelRemoved"] if self.relevant else [])
        return [("historyTypes", t) for t in types]

    def _incremental(self, state: ScanState, budget: Budget, mailbox: str, start: str, token: str | None, hid: str | None) -> Iterator[ScanBatch]:
        """History pages after `start`. Raises HttpError(404) when Gmail no longer has that history."""
        while True:
            if not budget.allow_page(("history", start, token)):
                return
            params = [("startHistoryId", start), ("maxResults", self.page_size), *self._history_types()] + ([("pageToken", token)] if token else [])
            body = self._json("/history", params)
            records = body.get("history") or []
            following = body.get("nextPageToken") or None
            hid = hid or (str(body["historyId"]) if body.get("historyId") else None)
            if not hid and not following:            # no `historyId` anywhere: the newest record read is the best id we have
                newest = [int(r["id"]) for r in records if str(r.get("id", "")).isdigit()]
                hid = str(max(newest)) if newest else None
            touched: dict[str, str] = {}
            for record in records:
                for event in record.get("messagesDeleted") or []:
                    touched[str(event["message"]["id"])] = "gone"
                for event in record.get("messagesAdded") or []:
                    touched.setdefault(str(event["message"]["id"]), "check")
                for event in (record.get("labelsAdded") or []) + (record.get("labelsRemoved") or []):
                    if self.relevant & set(event.get("labelIds") or []):
                        touched.setdefault(str(event["message"]["id"]), "check")
            changes = []
            for mid in budget.clip(list(touched)):
                changes.append(Change("delete", f"gmail:{mid}") if touched[mid] == "gone" else self._settle(mailbox, mid))
            if budget.truncated:                     # part of this page was left out: a resume must read this page again
                cursor = state.cursor(phase="history", start=start, token=token, hid=hid)
            elif following:
                cursor = state.cursor(phase="history", start=start, token=following, hid=hid)
            else:
                cursor = state.cursor(phase="done", hid=hid)
            yield ScanBatch(changes, cursor)
            if budget.truncated or not following:
                return
            token = following

    # ── scan ──

    def _fresh(self) -> ScanState:
        """A new, authoritative scan: used when the saved position is useless (history too old, no history id)."""
        state = ScanState({}, full_resync_hours=self.full_hours, clock=self.clock)
        state.observe(datetime.fromtimestamp(state.started, tz=timezone.utc))
        return state

    def scan(self, cursor: dict) -> Iterator[ScanBatch]:
        state = ScanState(cursor, full_resync_hours=self.full_hours, clock=self.clock)
        budget = Budget.from_config(self.cfg)
        saved = str((cursor or {}).get("history") or "") or None
        if not state.resumed:
            state.observe(datetime.fromtimestamp(state.started, tz=timezone.utc))
        if not budget.allow_page(("profile",)):
            yield ScanBatch([], state.final(truncated=True), done=True)
            return
        profile = self._json("/profile")
        mailbox = safe_name(str(profile.get("emailAddress") or self.user), "mailbox", 100)
        pos = dict(state.pos)
        prior = pos.get("prior") or saved or pos.get("start")
        hid: str | None = pos.get("hid")

        if pos.get("phase") == "done":
            pass                                     # every page was read before the crash: only the closing batch is left
        else:
            incremental = state.incremental and bool(pos.get("start") or saved)
            if state.incremental and not incremental:
                state, pos, hid = self._fresh(), {}, None          # nothing to read history from: list everything, authoritatively
            if incremental:
                try:
                    last = None
                    for batch in self._incremental(state, budget, mailbox, str(pos.get("start") or saved), pos.get("token"), hid):
                        last = batch
                        yield batch
                    hid = (last.cursor.get("pos") or {}).get("hid") if last else hid
                except HttpError as exc:
                    if exc.status != 404:
                        raise
                    log.warning("gmail: history id too old (HTTP 404); listing the whole mailbox instead")
                    state, pos, hid, incremental = self._fresh(), {}, None, False
            if not incremental:
                hid = pos.get("hid") or str(profile.get("historyId") or "") or None
                yield from self._full(state, budget, mailbox, pos.get("token") if state.resumed else None, hid or "", prior)
        final = state.final(truncated=budget.truncated)
        if not budget.truncated and hid:
            final["history"] = hid
        elif prior:
            final["history"] = prior
        yield ScanBatch([], final, done=True, complete_scope=state.complete_scope("", truncated=budget.truncated))

    # ── bytes and permissions ──

    def open(self, file: SourceFile) -> BinaryIO:
        prefix, _, message_id = file.external_id.partition(":")
        if prefix != "gmail" or not _ID.match(message_id):
            raise HttpError(f"gmail: {file.external_id!r} is not a Gmail message id")
        body = self._json(f"/messages/{message_id}", {"format": "raw"})
        raw = body.get("raw")
        if not raw:
            raise HttpError(f"gmail: message {message_id} came back without `raw`", 404)
        try:
            return io.BytesIO(base64.urlsafe_b64decode(str(raw) + "=" * (-len(str(raw)) % 4)))
        except (binascii.Error, ValueError):
            raise HttpError(f"gmail: message {message_id} has an undecodable `raw`") from None

    def refresh_acl(self, file: SourceFile) -> list[str]:
        return self.rules.for_path(file.path)
