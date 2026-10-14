"""An IMAP mailbox as a source: every message is one `.eml` virtual file, read without touching its flags.

Commands sent, through the standard library's `imaplib` (IMAP4rev1, RFC 3501; STARTTLS RFC 2595; mailbox names are
modified UTF-7, RFC 3501 5.1.3):
  * connect with IMAP4_SSL (default, port 993) or IMAP4 (port 143, optionally STARTTLS), then LOGIN <user> <password>
  * LIST "" "*"                                             the folders, their hierarchy delimiter and attributes
  * EXAMINE <folder>                                        = `select(readonly=True)`: the folder is opened read-only,
                                                            nothing is marked \\Recent or \\Seen; reads UIDVALIDITY and EXISTS
  * UID SEARCH ALL                                          first pass or full resync
  * UID SEARCH UID <last+1>:*                               incremental pass (a result <= <last> is dropped: `n:*` always
                                                            includes the newest message, RFC 3501 6.4.8)
  * UID FETCH <uid,uid,...> (UID RFC822.SIZE INTERNALDATE FLAGS BODY.PEEK[HEADER.FIELDS (SUBJECT)])   the scan (no bodies)
  * UID FETCH <uid> (RFC822.SIZE), then UID FETCH <uid> (BODY.PEEK[])  or  (BODY.PEEK[]<offset.chunk_bytes>) piece after
    piece until the server answers with fewer bytes than asked, streamed into a temporary file, size capped  -- open()
  * LOGOUT
Nothing that writes or marks is ever sent: no STORE, APPEND, EXPUNGE, COPY, MOVE, CREATE, DELETE, RENAME, and never
BODY[] / RFC822 without PEEK (those set \\Seen). The mailbox is exactly as it was.

Identity and versions
  * `external_id` = `imap:<percent-encoded folder>:<uid>`; the folder is encoded so that the scope `imap:INBOX:` does not
    contain `imap:INBOX%2FSub:` (hierarchy never confuses deletions). `path` = `<name>/<folder path>/<uid>-<subject>.eml`
    (`name` defaults to "imap"; the folder's own hierarchy delimiter becomes "/"), so prefix rules of `acl_rules` can be
    written per folder. The subject is RFC 2047-decoded and made safe for a file name; it only decorates the path.
  * `version` = `<uidvalidity>:<uid>:<size>`. A message is immutable for a given (UIDVALIDITY, UID), so this changes iff the
    message (or the numbering of the folder) changed; flag changes (read, starred, answered) do not re-index a message.
  * `modified_at` = INTERNALDATE (arrival time at the server), in UTC.

Scans (cursor = {"folders": {name: {"v": UIDVALIDITY, "last": highest UID seen, "full": epoch of the last full pass}}})
  * Per folder, in sorted order: UIDVALIDITY unchanged and a full pass less than `full_resync_hours` ago (default 24) =>
    incremental (only UIDs above "last"); otherwise a full pass (UID SEARCH ALL). A CHANGED UIDVALIDITY is a full resync of
    that folder: the old numbering is meaningless, so every message is re-read under the new one.
  * A full pass that ran from the beginning to the end of a folder yields a batch with `complete_scope="imap:<folder>:"`,
    so the sync layer deletes the folder's messages that were not seen (expunged, or flagged \\Deleted, which are skipped).
    Incremental passes never delete anything: expunges are noticed by the next full pass. A folder that disappeared from
    the server's LIST (deleted, renamed, or no longer matched by `folders` / `exclude_folders`) gets an empty batch with its
    scope, i.e. its messages are deleted; a folder that cannot be opened (EXAMINE refused) keeps its old state and is not
    touched. A scan resumed from a mid-folder cursor continues after the last finished UID and is not authoritative.
  * Mid-scan cursors add {"pos": {folder, v, after}} (or {"folder", "complete": true}); no password, host or user is
    ever stored in a cursor. Until the sync layer persists the cursor of a finished scan (it saves {} at the end), every scan
    is a full pass.

open(): the size the server announces (RFC822.SIZE, which some servers state inexactly) only decides between one fetch and
pieces and feeds `version`; the bytes returned are what is kept, and `max_message_bytes` is checked against the bytes that arrived.
A piece longer than the window asked for, or an empty answer for a message that is not empty, is an error (never a loop).

Failures: a dropped connection, a timeout or `imaplib.IMAP4.abort` reconnects, logs in and selects the folder again and
retries with bounded exponential backoff (`max_retries`, default 3); if UIDVALIDITY changed in between the scan fails (the
next one handles the new numbering). A rejected login raises `AuthError` with no server text in it; other refusals (NO / BAD)
raise `HttpError` with the server's first line, the password scrubbed. A TLS certificate that does not verify is not retried.

Config: host (required), port (993 with ssl, else 143), ssl (true), starttls (false; with ssl false), verify_tls (true),
ca_bundle, username (required), password_env (required: name of the environment variable, read at connect time),
folders (["*"]; names or fnmatch patterns, case-insensitive), exclude_folders (Trash, Deleted Items, Junk, Spam ... and the
folders the server marks \\Trash or \\Junk), name ("imap"), batch_size (100), fetch_chunk (50 UIDs per FETCH), chunk_bytes
(1 MiB), max_message_bytes (50 MB; bigger messages are skipped), full_resync_hours (24), timeout (60), max_retries (3),
max_wait (30), acl_rules, default_acl.

Permissions: folder -> principals through `AclRules` on the virtual path; nothing from the server (shared-folder ACLs, RFC 4314,
are not read). No matching rule and no `default_acl` => nobody can read the message (fail closed). A mailbox is private data:
give each mailbox its own connection and its own rule.

Not done: OAuth2 / XOAUTH2 (Gmail and Microsoft 365 need an app password or IMAP access that allows LOGIN), IDLE, CONDSTORE /
QRESYNC (so flag changes and expunges are only seen by a full pass), parsing of the message (the `.eml` goes through the normal
document parser), multi-line subjects beyond what `email` decodes, folders with a name that is not valid modified UTF-7, a
server-side Gmail "All Mail" duplicate (exclude it with `exclude_folders`).

Tested with a FAKE imaplib-compatible class (LIST, EXAMINE/SELECT, UID SEARCH, UID FETCH, response(), login, logout, with
imaplib's own quoting rules and response shapes) for the behaviour and the failure injection, and with the real `imaplib`
(IMAP4 and IMAP4_SSL, STARTTLS, a throw-away certificate, timeouts, refused connections) against a small socket-level IMAP
server written for the tests (the same message store behind both; checked to give identical scans and commands). Run on
Python 3.13 only (the project targets >= 3.12). Never run against Dovecot, Cyrus, Exchange, Gmail, iCloud or any other real
server; their quirks (folder naming, FETCH layouts, RFC822.SIZE, throttling, app passwords) are not covered.
"""

from __future__ import annotations

import base64
import fnmatch
import imaplib
import logging
import os
import re
import ssl
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email import policy
from email.parser import BytesHeaderParser
from typing import Any, BinaryIO, Callable, Iterator
from urllib.parse import quote, unquote

from app.scale.connectors._http import AuthError, HttpError, TooLarge, env_secret, safe_name
from app.scale.connectors.base import AclRules, Change, ScanBatch, SourceFile

log = logging.getLogger(__name__)

SCOPE = "imap:"
_DEFAULT_EXCLUDES = ["Trash", "Deleted Items", "Deleted Messages", "Junk", "Junk E-mail", "Junk Email", "Spam", "[Gmail]/Trash", "[Gmail]/Spam"]
_SKIP_ATTRIBUTES = {"\\noselect", "\\nonexistent"}
_SKIP_SPECIAL_USE = {"\\trash", "\\junk"}
_LIST_LINE = re.compile(rb'^\((?P<flags>[^)]*)\)\s+(?P<delim>"(?:[^"\\]|\\.)*"|NIL)\s+(?P<name>.*)$', re.S | re.I)
_MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
_INTERNALDATE = re.compile(r'INTERNALDATE\s+"\s*(\d{1,2})-([A-Za-z]{3})-(\d{4}) (\d{2}):(\d{2}):(\d{2}) ([+-]\d{4})"', re.I)


class _Unreadable(Exception):
    """EXAMINE was refused for a folder (permissions, or it vanished)."""


# ── modified UTF-7 (RFC 3501 5.1.3) ──


def decode_mutf7(name: str) -> str:
    def part(match: re.Match) -> str:
        if not match.group(1):
            return "&"
        raw = match.group(1).replace(",", "/")
        return base64.b64decode(raw + "=" * (-len(raw) % 4)).decode("utf-16-be")

    return re.sub(r"&([^-]*)-", part, name)


def encode_mutf7(name: str) -> str:
    out: list[str] = []
    pending: list[str] = []

    def flush() -> None:
        if pending:
            raw = "".join(pending).encode("utf-16-be")
            out.append("&" + base64.b64encode(raw).decode("ascii").rstrip("=").replace("/", ",") + "-")
            pending.clear()

    for char in name:
        if 0x20 <= ord(char) <= 0x7E:
            flush()
            out.append("&-" if char == "&" else char)
        else:
            pending.append(char)
    flush()
    return "".join(out)


def _quote(name: str) -> str:
    """imaplib does not quote mailbox names for us: a name with a space must arrive as a quoted string."""
    return '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _unquote(raw: bytes) -> str:
    text = raw.decode("utf-8", "replace").strip()
    if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
        return re.sub(r"\\(.)", r"\1", text[1:-1])
    return text


def _close(imap: Any) -> None:
    """LOGOUT politely; when that fails (the connection is already dead), release the socket anyway."""
    try:
        imap.logout()
    except Exception:                                      # noqa: BLE001
        try:
            imap.shutdown()
        except Exception:                                  # noqa: BLE001
            pass


@dataclass
class Folder:
    name: str                                   # decoded, as shown
    delimiter: str | None
    attributes: set[str] = field(default_factory=set)


def parse_list(items: list) -> list[Folder]:
    """The folders of a LIST reply as imaplib returns it: bytes lines, or (line, literal) tuples for names sent as literals."""
    out: list[Folder] = []
    for item in items or []:
        if item is None:
            continue
        line, literal = (item[0], item[1]) if isinstance(item, tuple) else (item, None)
        match = _LIST_LINE.match(line.strip() if isinstance(line, bytes) else line)
        if not match:
            continue
        delim = None if match["delim"].upper() == b"NIL" else _unquote(match["delim"])
        name = literal.decode("utf-8", "replace") if literal is not None else _unquote(match["name"])
        attributes = {a.lower() for a in match["flags"].decode("ascii", "replace").split()}
        out.append(Folder(decode_mutf7(name), delim, attributes))
    return out


# ── FETCH replies ──


@dataclass
class _Meta:
    uid: int
    size: int
    date: datetime | None
    flags: set[str]
    header: bytes


def _internaldate(text: str) -> datetime | None:
    match = _INTERNALDATE.search(text)
    month = _MONTHS.get(match.group(2).lower()) if match else None
    if not match or not month:
        return None
    sign = -1 if match.group(7)[0] == "-" else 1
    offset = timedelta(hours=int(match.group(7)[1:3]), minutes=int(match.group(7)[3:5])) * sign
    local = datetime(int(match.group(3)), month, int(match.group(1)), int(match.group(4)), int(match.group(5)), int(match.group(6)))
    return (local - offset).replace(tzinfo=timezone.utc)


def parse_fetch(data: list) -> dict[int, _Meta]:
    """imaplib FETCH data -> {uid: _Meta}. A reply is a tuple (text before a literal, literal) followed by a bytes element holding
    the text after it, or a single bytes element when there is no literal; the items can come in any order."""
    records: list[tuple[str, bytes]] = []
    open_text, open_literal = "", b""
    expecting_tail = False
    for item in data or []:
        if isinstance(item, tuple):
            open_text += (" " if open_text else "") + item[0].decode("utf-8", "replace")
            open_literal = open_literal or item[1]
            expecting_tail = True
        elif isinstance(item, (bytes, bytearray)):
            text = bytes(item).decode("utf-8", "replace")
            if expecting_tail:
                records.append((open_text + " " + text, open_literal))
                open_text, open_literal, expecting_tail = "", b"", False
            elif text.strip() not in ("", ")"):
                records.append((text, b""))
    out: dict[int, _Meta] = {}
    for text, literal in records:
        uid = re.search(r"\bUID\s+(\d+)", text)
        if not uid:
            continue
        size = re.search(r"RFC822\.SIZE\s+(\d+)", text)
        flags = re.search(r"FLAGS\s+\(([^)]*)\)", text)
        out[int(uid.group(1))] = _Meta(
            int(uid.group(1)), int(size.group(1)) if size else 0, _internaldate(text),
            {f.lower() for f in (flags.group(1).split() if flags else [])}, literal,
        )
    return out


def _subject(header: bytes) -> str:
    try:
        value = BytesHeaderParser(policy=policy.default).parsebytes(header).get("Subject")
        return str(value) if value else ""
    except Exception:                                   # noqa: BLE001  a malformed header must not stop a scan
        return ""


# ── cursor ──


class _Cursor:
    def __init__(self, cursor: dict) -> None:
        cursor = cursor if isinstance(cursor, dict) else {}
        self.folders: dict[str, dict] = {}
        for name, state in (cursor.get("folders") or {}).items() if isinstance(cursor.get("folders"), dict) else []:
            if isinstance(state, dict) and isinstance(state.get("v"), int) and isinstance(state.get("last"), int):
                self.folders[str(name)] = {"v": state["v"], "last": state["last"], "full": state.get("full")}
        pos = cursor.get("pos")
        self.pos: dict | None = None
        if isinstance(pos, dict) and isinstance(pos.get("folder"), str):
            if pos.get("complete") is True or (isinstance(pos.get("v"), int) and isinstance(pos.get("after"), int)):
                self.pos = pos
        self.resumed = self.pos is not None

    def skip(self, name: str) -> bool:
        """Folders sorted before the one a resumed scan stopped in (or the one it had finished) are done."""
        return bool(self.pos) and (name < self.pos["folder"] or (name == self.pos["folder"] and self.pos.get("complete") is True))

    def snapshot(self, pos: dict | None) -> dict:
        out: dict = {"folders": {n: dict(s) for n, s in self.folders.items()}}
        if pos:
            out["pos"] = pos
        return out


class _Session:
    """One IMAP connection that logs in, selects and re-selects after a reconnect, and retries transient failures."""

    def __init__(self, owner: "ImapConnector") -> None:
        self.o = owner
        self.imap: Any = None
        self.selected: str | None = None
        self.expect: tuple[str, int] | None = None

    def drop(self) -> None:
        imap, self.imap, self.selected = self.imap, None, None
        if imap is not None:
            _close(imap)

    close = drop

    def _ensure(self, folder: str | None) -> None:
        if self.imap is None:
            self.imap, self.selected = self.o._connect(), None
        if folder is not None and self.selected != folder:
            try:
                uv, _ = self.o._examine(self.imap, folder)
            except _Unreadable:
                raise HttpError(f"IMAP folder {folder!r} can no longer be opened") from None
            if self.expect and self.expect[0] == folder and self.expect[1] != uv:
                raise HttpError(f"IMAP folder {folder!r} was renumbered (UIDVALIDITY changed) during the scan; it is read again next time")
            self.selected = folder

    def run(self, what: str, fn: Callable[[Any], Any], folder: str | None = None) -> Any:
        host = self.o.host
        for attempt in range(self.o.max_retries + 1):
            failure = ""                                       # errors are built outside the `except` blocks so none keeps the server's words as __context__
            try:
                self._ensure(folder)
                return fn(self.imap)
            except ssl.SSLCertVerificationError:
                self.drop()
                failure = f"IMAP {what} on {host}: TLS certificate verification failed"
            except (imaplib.IMAP4.abort, OSError, EOFError) as exc:
                self.drop()
                failure = f"IMAP {what} on {host}: {type(exc).__name__} {self.o._scrub(str(exc))[:120]}".strip()
                if attempt < self.o.max_retries:
                    log.warning("IMAP %s on %s failed (%s); reconnecting", what, host, type(exc).__name__)
                    failure = ""
            except imaplib.IMAP4.error as exc:
                failure = f"IMAP {what} on {host} was refused: {self.o._scrub(str(exc))[:200]}"
            if failure:
                raise HttpError(failure)
            self.o._sleep(min(2 ** attempt, self.o.max_wait))
        raise HttpError(f"IMAP {what} on {host}: retries exhausted")

    def select(self, folder: str) -> tuple[int, int]:
        result = self.run("EXAMINE", lambda im: self.o._examine(im, folder))
        self.selected, self.expect = folder, (folder, result[0])
        return result


class ImapConnector:
    kind = "imap"

    def __init__(
        self, config: dict, *, imap_factory: Callable[[], Any] | None = None, sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.cfg = config
        self.host = str(config.get("host") or "")
        if not self.host and imap_factory is None:
            raise ValueError("imap: config needs `host`")
        self.host = self.host or "imap"
        self.username = str(config.get("username") or "")
        if not self.username or not config.get("password_env"):
            raise ValueError("imap: config needs `username` and `password_env` (the name of the environment variable holding the password)")
        self.use_ssl = bool(config.get("ssl", True))
        self.starttls = bool(config.get("starttls", False))
        if self.use_ssl and self.starttls:
            raise ValueError("imap: `starttls` is for plain connections; set `ssl` to false")
        self.port = int(config.get("port") or (993 if self.use_ssl else 143))
        self.timeout = float(config.get("timeout", 60))
        self.max_retries = int(config.get("max_retries", 3))
        self.max_wait = float(config.get("max_wait", 30))
        self.patterns = [str(p).lower() for p in config.get("folders", ["*"])]
        self.excludes = [str(p).lower() for p in config.get("exclude_folders", _DEFAULT_EXCLUDES)]
        self.name = safe_name(str(config.get("name") or "imap"), "imap", 60)
        self.batch_size = max(1, int(config.get("batch_size", 100)))
        self.fetch_chunk = max(1, int(config.get("fetch_chunk", 50)))
        self.chunk_bytes = max(1024, int(config.get("chunk_bytes", 1 << 20)))
        self.max_bytes = int(config.get("max_message_bytes", 50 * 1024 * 1024))
        self.full_hours = float(config.get("full_resync_hours", 24))
        self.rules = AclRules(config.get("acl_rules"), config.get("default_acl"))
        self._factory, self._sleep, self.clock = imap_factory, sleep, clock
        self.stats: dict[str, int] = {}

    def _count(self, reason: str, n: int = 1) -> None:
        self.stats[reason] = self.stats.get(reason, 0) + n

    # ── connection ──

    def _scrub(self, text: str) -> str:
        password = os.environ.get(self.cfg.get("password_env") or "", "")
        return text.replace(password, "***") if password else text

    def _connect(self) -> Any:
        password = env_secret(self.cfg["password_env"], "IMAP password")
        if self._factory is not None:
            imap = self._factory()
        else:
            ctx = ssl.create_default_context(cafile=self.cfg.get("ca_bundle") or None)
            if not self.cfg.get("verify_tls", True):
                ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
            if self.use_ssl:
                imap = imaplib.IMAP4_SSL(self.host, self.port, ssl_context=ctx, timeout=self.timeout)
            else:
                imap = imaplib.IMAP4(self.host, self.port, timeout=self.timeout)
                if self.starttls:
                    if "STARTTLS" not in getattr(imap, "capabilities", ()):
                        _close(imap)
                        raise HttpError(f"IMAP server {self.host} does not offer STARTTLS; the password is not sent in clear text")
                    try:
                        imap.starttls(ssl_context=ctx)          # no STARTTLS offered / refused / bad certificate: the password is never sent
                    except BaseException:
                        _close(imap)
                        raise
        rejected = not_ascii = False
        try:
            imap.login(self.username, password)
        except imaplib.IMAP4.abort:
            _close(imap)
            raise                                            # the connection dropped during LOGIN: not a verdict on the password
        except imaplib.IMAP4.error:
            rejected = True
        except UnicodeError:
            not_ascii = True
        if rejected or not_ascii:
            _close(imap)
            if rejected:                                     # raised outside the `except`: the server's reply (which may echo the password) is not kept as __context__
                raise AuthError(f"IMAP login to {self.host} as {self.username!r} was rejected", None)
            raise HttpError("IMAP LOGIN needs an ASCII password")
        return imap

    def _examine(self, imap: Any, folder: str) -> tuple[int, int]:
        typ, data = imap.select(_quote(encode_mutf7(folder)), readonly=True)
        if typ != "OK":
            raise _Unreadable(folder)
        _, found = imap.response("UIDVALIDITY")
        try:
            uidvalidity = int(found[0])
            exists = int((data or [b"0"])[0] or 0)
        except (TypeError, ValueError, IndexError):
            raise HttpError(f"IMAP folder {folder!r}: the server did not report UIDVALIDITY / EXISTS") from None
        return uidvalidity, exists

    # ── folders ──

    def _wanted(self, folder: Folder) -> bool:
        if folder.attributes & _SKIP_ATTRIBUTES or folder.attributes & _SKIP_SPECIAL_USE:
            return False
        lowered = folder.name.lower()
        if any(self._matches(lowered, p) for p in self.excludes):
            return False
        return any(self._matches(lowered, p) for p in self.patterns)

    @staticmethod
    def _matches(lowered: str, pattern: str) -> bool:
        """A pattern is a literal name or an fnmatch pattern; the literal comes first because `[Gmail]/Trash` is a character class to fnmatch."""
        return lowered == pattern or fnmatch.fnmatchcase(lowered, pattern)

    def _folders(self, sess: _Session) -> list[Folder]:
        typ, data = sess.run("LIST", lambda im: im.list('""', "*"))
        if typ != "OK":
            raise HttpError(f"IMAP LIST on {self.host} answered {typ}")
        return sorted((f for f in parse_list(data) if self._wanted(f)), key=lambda f: f.name)

    @staticmethod
    def _scope(name: str) -> str:
        return f"{SCOPE}{quote(name, safe='')}:"

    def _path(self, folder: Folder, uid: int, subject: str) -> str:
        parts = folder.name.split(folder.delimiter) if folder.delimiter else [folder.name]
        directory = "/".join(safe_name(p, "folder", 80) for p in parts if p) or "folder"
        return f"{self.name}/{directory}/{uid}-{safe_name(subject, 'no subject', 80)}.eml"

    # ── scan ──

    def _uids(self, sess: _Session, folder: str, after: int) -> list[int]:
        args = ("ALL",) if after <= 0 else ("UID", f"{after + 1}:*")
        typ, data = sess.run("UID SEARCH", lambda im: im.uid("SEARCH", *args), folder)
        if typ != "OK":
            raise HttpError(f"IMAP UID SEARCH in {folder!r} answered {typ}")
        return sorted({int(x) for x in b" ".join(d for d in data if isinstance(d, bytes)).split() if x.isdigit() and int(x) > after})

    def _metas(self, sess: _Session, folder: str, uids: list[int]) -> dict[int, _Meta]:
        items = "(UID RFC822.SIZE INTERNALDATE FLAGS BODY.PEEK[HEADER.FIELDS (SUBJECT)])"
        typ, data = sess.run("UID FETCH", lambda im: im.uid("FETCH", ",".join(map(str, uids)), items), folder)
        if typ != "OK":
            raise HttpError(f"IMAP UID FETCH in {folder!r} answered {typ}")
        return parse_fetch(data)

    def _file(self, folder: Folder, uv: int, meta: _Meta) -> SourceFile:
        path = self._path(folder, meta.uid, _subject(meta.header))
        return SourceFile(
            external_id=f"{self._scope(folder.name)}{meta.uid}", path=path, name=path.rsplit("/", 1)[-1], size=meta.size,
            modified_at=meta.date, version=f"{uv}:{meta.uid}:{meta.size}", acl=self.rules.for_path(path), mime="message/rfc822",
            extra={"folder": folder.name, "uid": meta.uid, "uidvalidity": uv},
        )

    def _full_due(self, known: dict, now: float) -> bool:
        return not isinstance(known.get("full"), (int, float)) or now - float(known["full"]) >= self.full_hours * 3600

    def _scan_folder(self, sess: _Session, folder: Folder, cur: _Cursor, now: float, pending: list[Change]) -> Iterator[ScanBatch]:
        try:
            uv, exists = sess.select(folder.name)
        except _Unreadable:
            self._count("unreadable_folders")
            log.warning("imap: folder %r cannot be opened; left as it was", folder.name)
            return
        known = cur.folders.get(folder.name)
        pos = cur.pos if (cur.pos and cur.pos["folder"] == folder.name and cur.pos.get("complete") is not True) else None
        if pos and pos.get("v") == uv:                      # continue after the last finished UID; a continued pass is never authoritative
            after, full = int(pos["after"]), False
        elif known and known["v"] == uv and not self._full_due(known, now):
            after, full = known["last"], False
        else:                                               # first sight, a full pass is due, or the folder was renumbered
            after, full = 0, True
            if known and known["v"] != uv:
                self._count("renumbered_folders")
        uids = self._uids(sess, folder.name, after) if exists else []
        highest = after
        for start in range(0, len(uids), self.fetch_chunk):
            chunk = uids[start:start + self.fetch_chunk]
            metas = self._metas(sess, folder.name, chunk)
            for uid in chunk:
                meta = metas.get(uid)
                if meta is None:
                    self._count("vanished_while_scanning")
                elif "\\deleted" in meta.flags:
                    self._count("flagged_deleted")
                elif meta.size > self.max_bytes:
                    self._count("oversize")
                else:
                    pending.append(Change("upsert", f"{self._scope(folder.name)}{uid}", self._file(folder, uv, meta)))
                    self._count("messages")
            highest = chunk[-1]
            if len(pending) >= self.batch_size:
                out = list(pending)
                pending.clear()
                snapshot = {"folder": folder.name, "v": uv, "after": highest}
                yield ScanBatch(out, cur.snapshot(snapshot))
        previous_full = known.get("full") if (known and known["v"] == uv) else None
        cur.folders[folder.name] = {"v": uv, "last": highest, "full": now if full else previous_full}
        done = {"folder": folder.name, "complete": True}
        if full:
            out = list(pending)
            pending.clear()
            yield ScanBatch(out, cur.snapshot(done), complete_scope=self._scope(folder.name))
        cur.pos = done

    def scan(self, cursor: dict) -> Iterator[ScanBatch]:
        cur = _Cursor(cursor)
        now = self.clock()
        sess = _Session(self)
        pending: list[Change] = []
        try:
            folders = self._folders(sess)
            for folder in folders:
                if not cur.skip(folder.name):
                    yield from self._scan_folder(sess, folder, cur, now, pending)
            listed = {f.name for f in folders}
            for gone in sorted(n for n in cur.folders if n not in listed):
                del cur.folders[gone]
                self._count("vanished_folders")
                out = list(pending)
                pending.clear()
                yield ScanBatch(out, cur.snapshot(cur.pos), complete_scope=self._scope(gone))
        finally:
            sess.close()
        yield ScanBatch(pending, {"folders": cur.folders}, done=True)

    # ── bytes and permissions ──

    def open(self, file: SourceFile) -> BinaryIO:
        match = re.fullmatch(re.escape(SCOPE) + r"([^:]*):(\d+)", file.external_id)
        if not match:
            raise HttpError(f"imap: {file.external_id!r} is not a message of an IMAP connection")
        folder, uid = unquote(match.group(1)), int(match.group(2))
        sess = _Session(self)
        try:
            try:
                uv, _ = sess.select(folder)
            except _Unreadable:
                raise HttpError(f"imap: folder {folder!r} can no longer be opened", 404) from None
            claimed = re.match(r"(\d+):", file.version or "")
            if claimed and int(claimed.group(1)) != uv:
                raise HttpError(f"imap: folder {folder!r} was renumbered (UIDVALIDITY {claimed.group(1)} -> {uv}); the message is read again at the next scan", 404)
            typ, data = sess.run("UID FETCH", lambda im: im.uid("FETCH", str(uid), "(RFC822.SIZE)"), folder)
            sizes = re.search(rb"RFC822\.SIZE\s+(\d+)", b" ".join(d if isinstance(d, bytes) else d[0] for d in data or [] if d))
            if typ != "OK" or not sizes:
                raise HttpError(f"imap: message {uid} is no longer in {folder!r}", 404)
            size = int(sizes.group(1))
            if size > self.max_bytes:
                raise TooLarge(f"imap: message {uid} in {folder!r} is {size} bytes, more than max_message_bytes ({self.max_bytes})")
            tmp = tempfile.TemporaryFile()
            try:
                offset = 0
                while True:
                    whole = offset == 0 and size <= self.chunk_bytes
                    what = "BODY.PEEK[]" if whole else f"BODY.PEEK[]<{offset}.{self.chunk_bytes}>"
                    typ, data = sess.run("UID FETCH", lambda im: im.uid("FETCH", str(uid), f"({what})"), folder)
                    part = next((d[1] for d in data or [] if isinstance(d, tuple)), b"")
                    if typ != "OK" or (not part and offset == 0 and size > 0):
                        raise HttpError(f"imap: message {uid} in {folder!r} could not be read at offset {offset}")
                    if not whole and len(part) > self.chunk_bytes:
                        raise HttpError(f"imap: message {uid} in {folder!r}: the server returned {len(part)} bytes for a window of {self.chunk_bytes} at offset {offset}")
                    offset += len(part)
                    if offset > self.max_bytes:                 # the real length, not the size the server announced
                        raise TooLarge(f"imap: message {uid} in {folder!r} is more than max_message_bytes ({self.max_bytes})")
                    tmp.write(part)
                    if whole or len(part) < self.chunk_bytes:   # a short (or empty) piece is the end of the message
                        break
                tmp.seek(0)
                return tmp
            except BaseException:
                tmp.close()
                raise
        finally:
            sess.close()

    def refresh_acl(self, file: SourceFile) -> list[str]:
        return self.rules.for_path(file.path)
