"""IMAP connector tests.

HOW THIS IS TESTED (read this before trusting a green run)

  * The behaviour and the failure injection run against a FAKE: `FakeImap`, a class that is compatible with `imaplib.IMAP4`
    for exactly what the connector may use (login, list, select(readonly=), response, uid('SEARCH' | 'FETCH', ...), logout,
    shutdown). It implements LIST, SELECT / EXAMINE, UID SEARCH and UID FETCH on an in-memory mailbox store, follows
    imaplib's return shapes (bytes lines, `(text, literal)` tuples plus a trailing bytes element, `response('UIDVALIDITY')`
    after a select, `n:*` always matching the newest message) and its argument rules (it does not quote mailbox names for
    the caller, so an unquoted `Sent Items` is a BAD). Any other imaplib call (STORE, EXPUNGE, APPEND, COPY, CREATE,
    DELETE, a plain FETCH, UID STORE ...) raises AssertionError, so the connector cannot write to the mailbox unnoticed.
  * The SAME store also sits behind `WireServer`, a small real IMAP server written for these tests (TCP on 127.0.0.1,
    IMAP4rev1 wire protocol: CAPABILITY, STARTTLS, LOGIN, LIST, EXAMINE / SELECT, UID SEARCH, UID FETCH with literals and
    partial BODY.PEEK[]<offset.length>, LOGOUT). The second half of this file drives the connector over TCP with the REAL
    `imaplib` (IMAP4 and IMAP4_SSL, real TLS with a throw-away certificate made by the `openssl` command line tool; those
    tests are skipped when `openssl` is missing). `test_fake_and_wire_server_give_identical_scans` requires both fronts to
    produce the same batches, cursors and the same commands.
  * NOT tested: any real IMAP server (Dovecot, Cyrus, Exchange, Gmail, iCloud ...). Both fronts were written by the same
    person from the RFC and the imaplib source; where a real server differs, these tests cannot show it.

Only time is faked: the connector's sleep and clock are injected, so back-off and the full-resync interval take no real time.
`Mirror` replays what `sync.run_scan` does with a scan (upsert by external_id; `complete_scope` deletes what was not seen in
this scan under that prefix), which is how the deletion tests are phrased.
"""

import base64
import json
import logging
import re
import shutil
import socket
import socketserver
import ssl
import subprocess
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.message import EmailMessage
from email import policy as email_policy
import imaplib

import pytest

from app.scale.connectors._http import AuthError, HttpError, TooLarge
from app.scale.connectors import imap as imap_mod
from app.scale.connectors.imap import ImapConnector, decode_mutf7, encode_mutf7, parse_fetch, parse_list

USER = "alice"
PASSWORD = "imap-pw-MUST-NOT-LEAK-42"
ENV = "T_IMAP_PW"
DATE = "05-Mar-2024 10:30:00 +0000"
NOW = 1_700_000_000.0
UTC = timezone.utc


# ══════════════════════════════════════════════ the mailbox store (shared by the fake and the wire server) ══════════════════════════════════════════════


def build(subject: str | None, body: str = "body text", **headers) -> bytes:
    msg = EmailMessage(policy=email_policy.SMTP)
    msg["From"], msg["To"], msg["Date"] = "bob@example.com", "alice@example.com", "Tue, 05 Mar 2024 10:30:00 +0000"
    if subject is not None:
        msg["Subject"] = subject
    for key, value in headers.items():
        msg[key.replace("_", "-")] = value
    msg.set_content(body)
    return msg.as_bytes(policy=email_policy.SMTP)


@dataclass
class Msg:
    uid: int
    raw: bytes
    flags: set
    date: str


@dataclass
class Box:
    name: str                      # the name on the wire: modified UTF-7
    uidvalidity: int = 1000
    delim: str | None = "/"
    attrs: tuple = ()
    msgs: dict = field(default_factory=dict)
    next_uid: int = 1


@dataclass
class Conn:
    id: int
    user: str | None = None
    box: Box | None = None
    readonly: bool = True
    closed: bool = False
    dead: bool = False


_SUBJECT = re.compile(rb"(?im)^Subject:[^\r\n]*(?:\r\n[ \t][^\r\n]*)*\r\n")
_ITEM = re.compile(r"BODY(?P<peek>\.PEEK)?\[(?P<sec>HEADER\.FIELDS \(SUBJECT\))?\](?:<(?P<off>\d+)\.(?P<len>\d+)>)?|RFC822\.SIZE|RFC822|INTERNALDATE|FLAGS|UID")
_TOKEN = re.compile(r'"((?:[^"\\]|\\.)*)"|(\S+)')


def tokens(args: str) -> list[str]:
    return [re.sub(r"\\(.)", r"\1", m[1]) if m[1] is not None else m[2] for m in _TOKEN.finditer(args)]


def quote_name(name: str) -> str:
    return name if re.fullmatch(r"[A-Za-z0-9&._\-/]+", name) else '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'


def render_items(items: list) -> bytes:
    parts = []
    for label, value in items:
        if isinstance(value, bytes):
            parts.append(label.encode() + b" {%d}\r\n" % len(value) + value)
        else:
            parts.append(f"{label} {value}".encode())
    return b" ".join(parts)


def as_imaplib(payload: bytes) -> list:
    """What `imaplib.IMAP4._get_response` stores for one untagged response whose text (after '* TYPE ') is `payload`."""
    out, buf = [], payload
    while True:
        line, _, buf = buf.partition(b"\r\n")
        literal = re.search(rb"\{(\d+)\}$", line)
        if not literal:
            out.append(line)
            return out
        size = int(literal.group(1))
        out.append((line, buf[:size]))
        buf = buf[size:]


class Store:
    def __init__(self):
        self.user, self.password = USER, PASSWORD
        self.boxes: dict[str, Box] = {}
        self.log: list[tuple[int, str, str]] = []
        self.conns: list[Conn] = []
        self.attempts = 0
        self.drops: list[dict] = []
        self.refuse: set[str] = set()
        self.refusals: dict[str, tuple[str, str]] = {}
        self.hide_uidvalidity = False
        self.fetch_order = "natural"
        self.list_literals = False
        self.size_skew = 0                  # added to every RFC822.SIZE the server announces (servers that state it inexactly)
        self.glitch = None                  # "empty": body literals come back empty; "long": partial fetches return 5 bytes too many
        self.down = False
        self.on_connect = None
        self.on_command = None
        self.lock = threading.RLock()
        self.box("INBOX")

    # content
    def box(self, name, *, uidvalidity=1000, delim="/", attrs=()):
        if name not in self.boxes:
            self.boxes[name] = Box(name, uidvalidity, delim, tuple(attrs))
        return self.boxes[name]

    def add(self, folder, subject="Hello", body="body text", *, flags=(), date=DATE, raw=None):
        box = self.box(folder)
        uid = box.next_uid
        box.next_uid += 1
        box.msgs[uid] = Msg(uid, raw if raw is not None else build(subject, body), set(flags), date)
        return uid

    def expunge(self, folder, *uids):
        for uid in uids:
            del self.boxes[folder].msgs[uid]

    def set_flags(self, folder, uid, flags):
        self.boxes[folder].msgs[uid].flags = set(flags)

    def renumber(self, folder, order=None):
        box = self.boxes[folder]
        old = [box.msgs[u] for u in (order or sorted(box.msgs))]
        box.uidvalidity += 1
        box.msgs, box.next_uid = {}, 1
        for m in old:
            box.msgs[box.next_uid] = Msg(box.next_uid, m.raw, m.flags, m.date)
            box.next_uid += 1

    def uids(self, folder):
        return sorted(self.boxes[folder].msgs)

    def raw(self, folder, uid):
        return self.boxes[folder].msgs[uid].raw

    def flag_snapshot(self):
        return {(n, u): frozenset(m.flags) for n, b in self.boxes.items() for u, m in b.msgs.items()}

    def drop(self, verb, skip=0, times=1):
        self.drops.append({"verb": verb, "skip": skip, "times": times})

    def cmds(self, verb, conn=None):
        return [a for c, v, a in self.log if v == verb and (conn is None or c == conn)]

    def verbs(self):
        return {v for _, v, _ in self.log}

    def factory(self):
        return lambda: FakeImap(self)

    # connections and commands
    def connect(self) -> Conn:
        with self.lock:
            self.attempts += 1
            if self.down:
                raise ConnectionRefusedError(111, "Connection refused")
            conn = Conn(len(self.conns) + 1)
            self.conns.append(conn)
            if self.on_connect:
                self.on_connect(conn)
            return conn

    def command(self, conn, verb, args) -> bool:
        """Record a command; True when the server must drop the connection instead of answering it."""
        with self.lock:
            self.log.append((conn.id, verb, args))
            if self.on_command:
                self.on_command(conn, verb, args)
            for rule in self.drops:
                if rule["verb"] != verb:
                    continue
                if rule["skip"] > 0:
                    rule["skip"] -= 1
                elif rule["times"] is None or rule["times"] > 0:
                    if rule["times"] is not None:
                        rule["times"] -= 1
                    conn.dead = True
                    return True
            return False

    # semantics
    def login(self, conn, user, password) -> bool:
        ok = user == self.user and password == self.password
        if ok:
            conn.user = user
        return ok

    def list_payloads(self) -> list[bytes]:
        out = []
        for name, box in self.boxes.items():
            head = f"({' '.join(box.attrs)}) " + (f'"{box.delim}"' if box.delim else "NIL")
            if self.list_literals:
                data = name.encode()
                out.append(f"{head} {{{len(data)}}}".encode() + b"\r\n" + data)
            else:
                out.append(f"{head} {quote_name(name)}".encode())
        return out

    def select(self, conn, wire_name, readonly):
        box = self.boxes.get(wire_name)
        if box is None or wire_name in self.refuse or "\\Noselect" in box.attrs:
            conn.box = None
            return False, 0, None
        conn.box, conn.readonly = box, readonly
        return True, len(box.msgs), None if self.hide_uidvalidity else box.uidvalidity

    def search(self, conn, criteria: str) -> list[int]:
        uids = sorted(conn.box.msgs)
        text = criteria.strip().upper()
        if text == "ALL":
            return uids
        m = re.fullmatch(r"UID (\d+):(\d+|\*)", text)
        if not m:
            raise ValueError(criteria)
        if not uids:
            return []
        lo, hi = int(m[1]), (uids[-1] if m[2] == "*" else int(m[2]))
        if lo > hi:                                           # RFC 3501 6.4.8: `n:*` with n above the newest is `newest:n`
            lo, hi = hi, lo
        return [u for u in uids if lo <= u <= hi]

    def fetch(self, conn, uidset: str, items: str) -> list:
        box = conn.box
        known = sorted(box.msgs)
        top = known[-1] if known else 0
        wanted: set[int] = set()
        for part in uidset.split(","):
            lo, _, hi = part.partition(":")
            a = top if lo == "*" else int(lo)
            b = a if not hi else (top if hi == "*" else int(hi))
            a, b = min(a, b), max(a, b)
            wanted.update(u for u in known if a <= u <= b)
        text = items.strip()
        text = text[1:-1] if text.startswith("(") and text.endswith(")") else text
        specs, pos = [], 0
        for m in _ITEM.finditer(text):
            if text[pos:m.start()].strip():
                raise ValueError(items)
            specs.append(m)
            pos = m.end()
        if text[pos:].strip() or not specs:
            raise ValueError(items)
        rows = []
        for seq, uid in enumerate(known, 1):
            if uid not in wanted:
                continue
            msg = box.msgs[uid]
            before = set(msg.flags)
            out: list = [("UID", str(uid))]
            for m in specs:
                name = m.group(0)
                if name == "UID":
                    continue
                if name == "RFC822.SIZE":
                    out.append((name, str(max(0, len(msg.raw) + self.size_skew))))
                elif name == "INTERNALDATE":
                    out.append((name, f'"{msg.date}"'))
                elif name == "FLAGS":
                    out.append((name, None))
                elif name == "RFC822":
                    out.append((name, msg.raw))
                    self._see(conn, msg)
                else:
                    if m["sec"]:
                        found = _SUBJECT.search(msg.raw.split(b"\r\n\r\n", 1)[0] + b"\r\n")
                        out.append(("BODY[HEADER.FIELDS (SUBJECT)]", (found.group(0) if found else b"") + b"\r\n"))
                    elif m["off"] is not None:
                        off = int(m["off"])
                        length = int(m["len"]) + (5 if self.glitch == "long" else 0)
                        if off >= len(msg.raw) and self.glitch != "empty":
                            out.append((f"BODY[]<{off}>", "NIL"))                                      # RFC 3501: a window that starts past the end is empty
                        else:
                            out.append((f"BODY[]<{off}>", b"" if self.glitch == "empty" else msg.raw[off:off + length]))
                    else:
                        out.append(("BODY[]", b"" if self.glitch == "empty" else msg.raw))
                    if not m["peek"]:
                        self._see(conn, msg)
            if msg.flags != before and not any(label == "FLAGS" for label, _ in out):
                out.append(("FLAGS", None))
            flags = "(" + " ".join(sorted(msg.flags)) + ")"
            out = [(label, flags if label == "FLAGS" else value) for label, value in out]
            if self.fetch_order == "reversed":
                out.reverse()
            rows.append((seq, out))
        return rows

    @staticmethod
    def _see(conn, msg):
        if not conn.readonly:
            msg.flags.add("\\Seen")


# ══════════════════════════════════════════════ front 1: the fake imaplib ══════════════════════════════════════════════


class FakeImap:
    """The slice of imaplib.IMAP4 the connector may use. Everything else is an AssertionError."""

    def __init__(self, store: Store):
        self.db = store
        self.conn = store.connect()
        self.state = "NONAUTH"
        self.untagged: dict = {}

    def _enter(self, verb, args, states):
        if self.conn.dead:
            raise imaplib.IMAP4.abort("socket error: EOF")
        if self.state not in states:
            raise imaplib.IMAP4.error(f"command {verb.split()[0]} illegal in state {self.state}, only allowed in states {', '.join(states)}")
        if self.db.command(self.conn, verb, args):
            raise imaplib.IMAP4.abort("socket error: EOF")
        if verb in self.db.refusals:
            status, text = self.db.refusals[verb]
            if status == "BAD":
                raise imaplib.IMAP4.error(f"{verb.split()[0]} command error: BAD [{text.encode()!r}]")
            return status, [text.encode()]
        return None

    def login(self, user, password):
        user.encode("ascii"), password.encode("ascii")          # imaplib encodes its arguments as ASCII
        refused = self._enter("LOGIN", user, ("NONAUTH",))
        if refused:
            raise imaplib.IMAP4.error(refused[1][-1])
        if not self.db.login(self.conn, user, password):
            raise imaplib.IMAP4.error(f"[AUTHENTICATIONFAILED] Authentication failed for {user} using password {password}".encode())
        self.state = "AUTH"
        return "OK", [b"Logged in"]

    def list(self, directory='""', pattern="*"):
        assert (directory, pattern) == ('""', "*"), f"unexpected LIST arguments {directory!r} {pattern!r}"
        refused = self._enter("LIST", f"{directory} {pattern}", ("AUTH", "SELECTED"))
        if refused:
            return refused
        data = [item for payload in self.db.list_payloads() for item in as_imaplib(payload)]
        return "OK", data or [None]

    def select(self, mailbox="INBOX", readonly=False):
        verb = "EXAMINE" if readonly else "SELECT"
        refused = self._enter(verb, mailbox, ("AUTH", "SELECTED"))
        if refused:
            return refused
        self.untagged = {}
        names = tokens(mailbox)
        if len(names) != 1:
            raise imaplib.IMAP4.error(f"{verb} command error: BAD [b'Missing or invalid mailbox name']")
        ok, exists, uidvalidity = self.db.select(self.conn, names[0], readonly)
        if not ok:
            self.state = "AUTH"
            return "NO", [b"[NONEXISTENT] Unknown Mailbox"]
        self.state = "SELECTED"
        self.untagged["EXISTS"] = [str(exists).encode()]
        if uidvalidity is not None:
            self.untagged["UIDVALIDITY"] = [str(uidvalidity).encode()]
        return "OK", [str(exists).encode()]

    def response(self, code):
        return code, self.untagged.pop(code.upper(), [None])

    def uid(self, command, *args):
        command = command.upper()
        assert command in ("SEARCH", "FETCH"), f"UID {command} must never be used by a read-only connector"
        refused = self._enter(f"UID {command}", " ".join(args), ("SELECTED",))
        if refused:
            return refused
        try:
            if command == "SEARCH":
                return "OK", [" ".join(map(str, self.db.search(self.conn, " ".join(args)))).encode()]
            rows = self.db.fetch(self.conn, args[0], args[1])
        except ValueError:
            raise imaplib.IMAP4.error("UID command error: BAD [b'Invalid arguments']") from None
        data = [item for seq, items in rows for item in as_imaplib(b"%d (" % seq + render_items(items) + b")")]
        return "OK", data or [None]

    def logout(self):
        if self.conn.dead:
            raise imaplib.IMAP4.abort("socket error: EOF")
        self.db.command(self.conn, "LOGOUT", "")
        self.conn.closed = True
        self.state = "LOGOUT"
        return "BYE", [b"Logging out"]

    def shutdown(self):
        self.conn.closed = True

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        raise AssertionError(f"imaplib.IMAP4.{name}() is not something a read-only connector may call")


# ══════════════════════════════════════════════ front 2: a real IMAP server on a socket ══════════════════════════════════════════════


class WireHandler(socketserver.BaseRequestHandler):
    def handle(self):
        srv, self.sock = self.server, self.request
        if srv.tls == "implicit":
            self.sock = srv.ctx.wrap_socket(self.sock, server_side=True)
        self.rfile = self.sock.makefile("rb")
        self.conn = srv.store.connect()
        try:
            self.say(b"* OK IMAP4rev1 test server ready")
            self.loop()
        except (OSError, ssl.SSLError, ValueError):
            pass
        finally:
            self.conn.closed = True
            for closable in (self.rfile, self.sock):
                try:
                    closable.close()
                except Exception:                                # noqa: BLE001
                    pass

    def say(self, data: bytes):
        self.sock.sendall(data + b"\r\n")

    def loop(self):
        srv, store, conn = self.server, self.server.store, self.conn
        tls_on = srv.tls == "implicit"
        while True:
            raw = self.rfile.readline(1 << 16)
            if not raw:
                return
            tag, _, rest = raw.decode("utf-8", "replace").rstrip("\r\n").partition(" ")
            verb, _, args = rest.partition(" ")
            verb = verb.upper()
            if verb == "UID":
                sub, _, args = args.partition(" ")
                verb = "UID " + sub.upper()
            ok = lambda text="completed": self.say(f"{tag} OK {text}".encode())          # noqa: E731
            bad = lambda text="invalid arguments": self.say(f"{tag} BAD {text}".encode())  # noqa: E731
            if verb == "CAPABILITY":
                self.say(b"* CAPABILITY IMAP4rev1" + (b" STARTTLS" if srv.tls == "starttls" and not tls_on else b""))
                ok()
                continue
            if store.command(conn, verb, (tokens(args) or [""])[0] if verb == "LOGIN" else args):
                return                                                   # the connection is dropped without an answer
            if verb in store.refusals:
                status, text = store.refusals[verb]
                self.say(f"{tag} {status} {text}".encode())
                continue
            if verb == "STARTTLS":
                if srv.tls != "starttls" or tls_on:
                    bad("no such command")
                    continue
                ok("Begin TLS negotiation now")
                self.sock = srv.ctx.wrap_socket(self.sock, server_side=True)
                self.rfile = self.sock.makefile("rb")
                tls_on = True
            elif verb == "LOGIN":
                parts = tokens(args)
                if len(parts) == 2 and store.login(conn, *parts):
                    ok("Logged in")
                else:
                    self.say(f"{tag} NO [AUTHENTICATIONFAILED] Authentication failed for {parts[:1]} using password {parts[1:]}".encode())
            elif verb == "LOGOUT":
                self.say(b"* BYE logging out")
                ok("LOGOUT completed")
                return
            elif conn.user is None:
                bad("log in first")
            elif verb == "LIST":
                if tokens(args) != ["", "*"]:
                    bad()
                    continue
                for payload in store.list_payloads():
                    self.say(b"* LIST " + payload)
                ok()
            elif verb in ("EXAMINE", "SELECT"):
                names = tokens(args)
                if len(names) != 1:
                    bad("missing or invalid mailbox name")
                    continue
                found, exists, uidvalidity = store.select(conn, names[0], verb == "EXAMINE")
                if not found:
                    self.say(f"{tag} NO [NONEXISTENT] unknown mailbox".encode())
                    continue
                self.say(b"* FLAGS (\\Answered \\Flagged \\Deleted \\Seen \\Draft)")
                self.say(f"* {exists} EXISTS".encode())
                self.say(b"* 0 RECENT")
                if uidvalidity is not None:
                    self.say(f"* OK [UIDVALIDITY {uidvalidity}] UIDs valid".encode())
                self.say(b"* OK [UIDNEXT 99999] predicted next uid")
                ok("[READ-ONLY] EXAMINE completed" if verb == "EXAMINE" else "[READ-WRITE] SELECT completed")
            elif verb in ("UID SEARCH", "UID FETCH") and conn.box is None:
                bad("no mailbox selected")
            elif verb == "UID SEARCH":
                try:
                    found_uids = store.search(conn, args)
                except ValueError:
                    bad("invalid search criteria")
                    continue
                self.say(("* SEARCH " + " ".join(map(str, found_uids))).rstrip().encode())
                ok()
            elif verb == "UID FETCH":
                uidset, _, items = args.partition(" ")
                try:
                    rows = store.fetch(conn, uidset, items)
                except ValueError:
                    bad("invalid fetch items")
                    continue
                for seq, its in rows:
                    self.say(b"* %d FETCH (" % seq + render_items(its) + b")")
                ok()
            else:
                bad(f"{verb} is not supported by this server")


class WireServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, store, tls=None, ctx=None):
        super().__init__(("127.0.0.1", 0), WireHandler)
        self.store, self.tls, self.ctx = store, tls, ctx

    def handle_error(self, request, client_address):
        pass

    @property
    def port(self):
        return self.server_address[1]


# ══════════════════════════════════════════════ helpers ══════════════════════════════════════════════


class Clock:
    def __init__(self, now=NOW):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, hours):
        self.now += hours * 3600


class Mirror:
    """What the sync layer does with a scan: upserts by external_id; `complete_scope` deletes the unseen items under that prefix."""

    def __init__(self):
        self.items: dict = {}
        self.seen: set = set()

    def scan(self, batches):
        self.seen = set()
        return self.resume(batches)

    def resume(self, batches):
        for batch in batches:
            for change in batch.changes:
                if change.kind == "upsert":
                    self.items[change.external_id] = change.file
                    self.seen.add(change.external_id)
                else:
                    self.items.pop(change.external_id, None)
            if batch.complete_scope is not None:
                for key in [k for k in self.items if k.startswith(batch.complete_scope) and k not in self.seen]:
                    del self.items[key]
        return self

    def paths(self):
        return sorted(f.path for f in self.items.values())


def mk(store, clock=None, **cfg):
    config = {"host": "imap.test", "username": USER, "password_env": ENV, "default_acl": ["group:staff"], **cfg}
    sleeps: list = []
    connector = ImapConnector(config, imap_factory=store.factory(), sleep=sleeps.append, clock=clock or Clock())
    connector.sleeps = sleeps
    return connector


def scan_all(connector, cursor=None):
    return list(connector.scan({} if cursor is None else cursor))


def files(batches):
    return {c.external_id: c.file for b in batches for c in b.changes if c.kind == "upsert"}


def scopes(batches):
    return [b.complete_scope for b in batches if b.complete_scope is not None]


def collect(generator):
    out, error = [], None
    try:
        for batch in generator:
            out.append(batch)
    except Exception as exc:                                        # noqa: BLE001
        error = exc
    return out, error


def persisted(cursor):
    """The cursor as the sync layer keeps it: through JSON."""
    return json.loads(json.dumps(cursor))


def read(connector, file):
    with connector.open(file) as handle:
        return handle.read()


def assert_peek_only(store):
    fetches = store.cmds("UID FETCH")
    assert fetches
    for args in fetches:
        assert not re.search(r"BODY\[|RFC822(?!\.SIZE)", args), args


def populate(store):
    """A mailbox with the awkward cases: spaces, a non-ASCII folder, a \\Noselect parent, a trash folder, deleted and folded subjects."""
    store.add("INBOX", "Quarterly report", "numbers", flags=("\\Seen",), date="05-Mar-2024 10:30:00 +0200")
    store.add("INBOX", "Lunch?", "tacos")
    store.add("INBOX", "Gone", "bye", flags=("\\Deleted",))
    store.add("INBOX", raw=b"From: a@b\r\nSubject: A folded\r\n  subject line\r\n\r\nbody")
    store.box("Sent Items")
    store.add("Sent Items", "=?utf-8?B?" + base64.b64encode("Grüße aus Köln".encode()).decode() + "?=", "hallo")
    store.box("Entw&APw-rfe", delim=".")
    store.add("Entw&APw-rfe", "Draft", "later")
    store.box("Projects", attrs=("\\Noselect", "\\HasChildren"))
    store.add("Projects/Alpha", "Kickoff", "plan", date="31-Dec-2023 23:59:59 -0500")
    store.box("Trash", attrs=("\\Trash",))
    store.add("Trash", "Old", "junk")


# ══════════════════════════════════════════════ fixtures ══════════════════════════════════════════════


@pytest.fixture(autouse=True)
def password(monkeypatch):
    monkeypatch.setenv(ENV, PASSWORD)


@pytest.fixture
def store():
    return Store()


@pytest.fixture
def servers():
    started: list = []

    def start(store, tls=None, ctx=None):
        srv = WireServer(store, tls, ctx)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        started.append(srv)
        return srv

    yield start
    for srv in started:
        srv.shutdown()
        srv.server_close()


def over_tcp(srv, **cfg):
    config = {"host": "127.0.0.1", "port": srv.port, "ssl": False, "username": USER, "password_env": ENV, "default_acl": ["group:staff"],
              "timeout": 5, **cfg}
    sleeps: list = []
    connector = ImapConnector(config, sleep=sleeps.append, clock=Clock())
    connector.sleeps = sleeps
    return connector


# ══════════════════════════════════════════════ the test doubles themselves (a test that cannot fail proves nothing) ══════════════════════════════════════════════


def test_the_fake_refuses_every_call_that_could_write(store):
    im = FakeImap(store)
    im.login(USER, PASSWORD)
    for call in (lambda: im.store("1", "+FLAGS", "\\Seen"), lambda: im.expunge(), lambda: im.append("INBOX", None, None, b"x"),
                 lambda: im.copy("1", "Trash"), lambda: im.create("X"), lambda: im.delete("INBOX"), lambda: im.fetch("1", "(RFC822)"),
                 lambda: im.uid("STORE", "1", "+FLAGS", "\\Seen"), lambda: im.uid("COPY", "1", "Trash"), lambda: im.noop()):
        with pytest.raises(AssertionError):
            call()


def test_the_oracle_notices_a_fetch_that_marks_a_message_read(store):
    store.add("INBOX", "x")
    im = FakeImap(store)
    im.login(USER, PASSWORD)
    im.select('"INBOX"', readonly=False)
    im.uid("FETCH", "1", "(BODY[])")
    assert "\\Seen" in store.flag_snapshot()[("INBOX", 1)]
    store.set_flags("INBOX", 1, ())
    im.select('"INBOX"', readonly=True)
    im.uid("FETCH", "1", "(BODY[])")                                 # a read-only mailbox cannot be marked...
    assert store.flag_snapshot()[("INBOX", 1)] == frozenset()
    with pytest.raises(AssertionError):                              # ...but the command log still shows the non-PEEK fetch
        assert_peek_only(store)


def test_the_fake_follows_imaplib_rules(store):
    store.add("INBOX", "x")
    store.box("Sent Items")
    im = FakeImap(store)
    with pytest.raises(imaplib.IMAP4.error, match="illegal in state"):
        im.list()
    im.login(USER, PASSWORD)
    with pytest.raises(imaplib.IMAP4.error, match="BAD"):            # imaplib does not quote a mailbox name for the caller
        im.select("Sent Items", readonly=True)
    assert im.select('"Sent Items"', readonly=True) == ("OK", [b"0"])
    assert im.response("UIDVALIDITY") == ("UIDVALIDITY", [b"1000"])
    assert im.response("UIDVALIDITY") == ("UIDVALIDITY", [None])     # imaplib hands each untagged response out once
    assert im.select('"Nope"', readonly=True)[0] == "NO"
    im.select('"INBOX"', readonly=True)
    assert im.uid("SEARCH", "UID", "9:*") == ("OK", [b"1"])         # `n:*` always includes the newest message
    assert im.uid("SEARCH", "UID", "1:*") == ("OK", [b"1"])


# ══════════════════════════════════════════════ units ══════════════════════════════════════════════


def test_modified_utf7_follows_rfc_3501():
    assert decode_mutf7("~peter/mail/&U,BTFw-/&ZeVnLIqe-") == "~peter/mail/台北/日本語"            # the example of RFC 3501 5.1.3
    assert encode_mutf7("~peter/mail/台北/日本語") == "~peter/mail/&U,BTFw-/&ZeVnLIqe-"
    assert decode_mutf7("Entw&APw-rfe") == "Entwürfe" and encode_mutf7("Entwürfe") == "Entw&APw-rfe"
    assert decode_mutf7("Q&-A") == "Q&A" and encode_mutf7("Q&A") == "Q&-A"
    for name in ("INBOX", "A & B", "Привет/мир", "😀 party", "a.b.c", "日本語&x", "Ünï/cödé"):
        assert decode_mutf7(encode_mutf7(name)) == name


def test_parse_list_reads_every_shape_imaplib_returns():
    items = [b'(\\HasNoChildren) "/" INBOX', b'(\\HasChildren) "/" "Sent Items"', b'(\\Noselect \\HasChildren) "." "Archive"', b'() NIL Flat',
             (b'(\\HasNoChildren) "/" {11}', b"Literal Name"), b"", None, b'(\\HasNoChildren) "/" "Say \\"hi\\" \\\\ x"',
             b'(\\HasNoChildren) "/" "Entw&APw-rfe"']
    got = parse_list(items)
    assert [(f.name, f.delimiter, sorted(f.attributes)) for f in got] == [
        ("INBOX", "/", ["\\hasnochildren"]), ("Sent Items", "/", ["\\haschildren"]), ("Archive", ".", ["\\haschildren", "\\noselect"]),
        ("Flat", None, []), ("Literal Name", "/", ["\\hasnochildren"]), ('Say "hi" \\ x', "/", ["\\hasnochildren"]), ("Entwürfe", "/", ["\\hasnochildren"])]
    assert parse_list([None]) == [] and parse_list([]) == []


def test_parse_fetch_reads_any_item_order_and_ignores_noise():
    data = [
        (b'1 (UID 7 RFC822.SIZE 123 INTERNALDATE " 5-Mar-2024 10:30:00 +0200" FLAGS (\\Seen \\Flagged) BODY[HEADER.FIELDS (SUBJECT)] {15}', b"Subject: Hi\r\n\r\n"), b")",
        (b"2 (BODY[HEADER.FIELDS (SUBJECT)] {2}", b"\r\n"), b' FLAGS () UID 8 INTERNALDATE "31-Dec-2023 23:59:59 -0500" RFC822.SIZE 9)',
        b"3 (FLAGS (\\Seen))",                                       # an unsolicited update: no UID, nothing to say
        b"5 (X-GUID 77 FLAGS ())",                                    # "UID" inside another word is not a UID
        b"4 (UID 9 RFC822.SIZE 5 FLAGS ())",
        b")",
    ]
    metas = parse_fetch(data)
    assert sorted(metas) == [7, 8, 9]
    assert (metas[7].size, metas[7].header, metas[7].flags) == (123, b"Subject: Hi\r\n\r\n", {"\\seen", "\\flagged"})
    assert metas[7].date == datetime(2024, 3, 5, 8, 30, tzinfo=UTC)
    assert (metas[8].size, metas[8].header, metas[8].flags) == (9, b"\r\n", set()) and metas[8].date == datetime(2024, 1, 1, 4, 59, 59, tzinfo=UTC)
    assert (metas[9].size, metas[9].date) == (5, None)
    assert parse_fetch([None]) == {} and parse_fetch([]) == {}


@pytest.mark.parametrize("cfg, text", [
    ({"username": USER, "password_env": ENV}, "host"),
    ({"host": "h", "password_env": ENV}, "username"),
    ({"host": "h", "username": USER}, "password_env"),
    ({"host": "h", "username": USER, "password_env": ENV, "starttls": True}, "starttls"),
])
def test_configuration_is_validated(cfg, text):
    with pytest.raises(ValueError, match=text):
        ImapConnector(cfg)


def test_ports_default_to_the_protocol_ports():
    base = {"host": "h", "username": USER, "password_env": ENV}
    assert ImapConnector(base).port == 993
    assert ImapConnector({**base, "ssl": False}).port == 143
    assert ImapConnector({**base, "ssl": False, "starttls": True}).port == 143
    assert ImapConnector({**base, "port": 1993}).port == 1993


# ══════════════════════════════════════════════ what a scan yields ══════════════════════════════════════════════


def test_every_message_becomes_an_eml_file_with_a_hierarchical_path(store):
    store.add("INBOX", "Quarterly report", "numbers")
    store.add("INBOX", "Lunch?", "tacos")
    store.box("Projects", attrs=("\\Noselect",))
    store.add("Projects/Alpha", "Kickoff", "plan")
    batches = scan_all(mk(store))
    got = files(batches)
    assert set(got) == {"imap:INBOX:1", "imap:INBOX:2", "imap:Projects%2FAlpha:1"}
    first = got["imap:INBOX:1"]
    assert (first.path, first.name, first.mime) == ("imap/INBOX/1-Quarterly report.eml", "1-Quarterly report.eml", "message/rfc822")
    assert first.size == len(store.raw("INBOX", 1)) and first.acl == ["group:staff"]
    assert got["imap:Projects%2FAlpha:1"].path == "imap/Projects/Alpha/1-Kickoff.eml"
    assert got["imap:INBOX:2"].path == "imap/INBOX/2-Lunch.eml"                                   # `?` is not allowed in a file name
    assert batches[-1].done and not any(b.done for b in batches[:-1])
    assert store.cmds("EXAMINE") == ['"INBOX"', '"Projects/Alpha"']                               # the \Noselect parent is never opened


def test_the_name_option_chooses_the_virtual_root(store):
    store.add("INBOX", "Hello")
    assert [f.path for f in files(scan_all(mk(store, name="mail alice"))).values()] == ["mail alice/INBOX/1-Hello.eml"]


def test_the_folders_own_delimiter_becomes_the_path_separator(store):
    store.box("INBOX.Sub.Deeper", delim=".")
    store.add("INBOX.Sub.Deeper", "Hi")
    assert [f.path for f in files(scan_all(mk(store))).values() if "Deeper" in f.path] == ["imap/INBOX/Sub/Deeper/1-Hi.eml"]


def test_folder_names_are_sent_quoted_and_in_modified_utf7(store):
    for name in ("Sent Items", "Entw&APw-rfe", 'Odd "name" \\ x'):
        store.add(name, "m")
    got = files(scan_all(mk(store)))
    assert sorted(f.extra["folder"] for f in got.values()) == sorted(["Sent Items", "Entwürfe", 'Odd "name" \\ x'])
    assert sorted(f.path for f in got.values()) == ["imap/Entwürfe/1-m.eml", "imap/Odd name x/1-m.eml", "imap/Sent Items/1-m.eml"]
    assert sorted(store.cmds("EXAMINE")) == sorted(['"INBOX"', '"Sent Items"', '"Entw&APw-rfe"', '"Odd \\"name\\" \\\\ x"'])


def test_trash_and_junk_are_left_out_by_default(store):
    for name in ("Trash", "Junk", "Deleted Items", "[Gmail]/Spam", "Keep"):
        store.add(name, "m")
    for name, attrs in (("Papierkorb", ("\\Trash",)), ("Spamordner", ("\\Junk",))):
        store.add(name, "m")
        store.boxes[name].attrs = attrs
    store.box("Gone", attrs=("\\Noselect",))
    store.add("INBOX", "mail")
    scan_all(mk(store))
    assert sorted(store.cmds("EXAMINE")) == ['"INBOX"', '"Keep"']
    store.log.clear()
    scan_all(mk(store, exclude_folders=[]))                                                          # the default list can be dropped; what the server marks stays out
    assert sorted(store.cmds("EXAMINE")) == sorted(['"INBOX"', '"Keep"', '"Trash"', '"Junk"', '"Deleted Items"', '"[Gmail]/Spam"'])


def test_folders_and_exclude_folders_take_names_and_patterns_in_any_case(store):
    for name in ("INBOX", "Projects/Alpha", "Projects/Old", "Projects/Beta", "Private"):
        store.add(name, "m")
    scan_all(mk(store, folders=["inbox", "PROJECTS/*"], exclude_folders=["projects/old"]))
    assert sorted(store.cmds("EXAMINE")) == ['"INBOX"', '"Projects/Alpha"', '"Projects/Beta"']


def test_modified_at_is_the_internaldate_in_utc(store):
    store.add("INBOX", "a", date="05-Mar-2024 10:30:00 +0200")
    store.add("INBOX", "b", date="31-Dec-2023 23:59:59 -0500")
    store.add("INBOX", "c", date=" 7-Jul-2024 00:00:00 +0000")
    got = files(scan_all(mk(store)))
    assert [got[f"imap:INBOX:{n}"].modified_at for n in (1, 2, 3)] == [
        datetime(2024, 3, 5, 8, 30, tzinfo=UTC), datetime(2024, 1, 1, 4, 59, 59, tzinfo=UTC), datetime(2024, 7, 7, 0, 0, tzinfo=UTC)]


def test_subjects_are_decoded_and_made_safe_for_paths(store):
    b64 = base64.b64encode("Grüße aus Köln".encode()).decode()
    cases = [
        (b"=?utf-8?B?" + b64.encode() + b"?=", "Grüße aus Köln"),
        (b"=?iso-8859-1?Q?Caf=E9_menu?=", "Café menu"),
        (b"A folded\r\n  subject line", "A folded subject line"),
        (b"a/b\\c:d*e?f", "a b c d e f"),
        (b"../../etc/passwd", "etc passwd"),
        (b"x" * 300, "x" * 80),
        (b"", "no subject"),
    ]
    for subject, _ in cases:
        store.add("INBOX", raw=b"From: a@b\r\nSubject: " + subject + b"\r\n\r\nbody")
    store.add("INBOX", raw=b"From: a@b\r\n\r\nno subject header at all")
    got = files(scan_all(mk(store)))
    names = [got[f"imap:INBOX:{n}"].path for n in range(1, len(cases) + 2)]
    assert names == [f"imap/INBOX/{n}-{want}.eml" for n, (_, want) in enumerate(cases, 1)] + [f"imap/INBOX/{len(cases) + 1}-no subject.eml"]
    for path in names:
        assert path.count("/") == 2 and ".." not in path.split("/")


def test_version_changes_with_the_message_or_its_numbering_and_never_with_flags(store):
    store.add("INBOX", "A", "one")
    raw = store.raw("INBOX", 1)
    before = files(scan_all(mk(store)))["imap:INBOX:1"]
    assert before.version == f"1000:1:{len(raw)}"
    store.set_flags("INBOX", 1, {"\\Seen", "\\Flagged", "\\Answered"})                              # read, starred, answered: same mail
    assert files(scan_all(mk(store)))["imap:INBOX:1"].version == before.version
    store.add("INBOX", "A", "one")                                                                   # an identical mail is a new message
    twin = files(scan_all(mk(store)))
    assert twin["imap:INBOX:2"].version == f"1000:2:{len(raw)}" and twin["imap:INBOX:2"].external_id != before.external_id
    store.renumber("INBOX")                                                                          # new UIDVALIDITY: every version moves on
    after = files(scan_all(mk(store)))
    assert after["imap:INBOX:1"].version == f"1001:1:{len(raw)}" != before.version


def test_external_ids_are_stable_across_scans(store):
    populate(store)
    first, second = files(scan_all(mk(store))), files(scan_all(mk(store)))
    assert first.keys() == second.keys() and {k: v.version for k, v in first.items()} == {k: v.version for k, v in second.items()}


# ══════════════════════════════════════════════ read-only ══════════════════════════════════════════════


def test_scan_and_open_send_nothing_that_writes_or_marks(store):
    populate(store)
    store.add("INBOX", "unread", flags=())
    before = store.flag_snapshot()
    c = mk(store)
    for f in files(scan_all(c)).values():
        c.open(f).close()
    assert store.verbs() <= {"LOGIN", "LIST", "EXAMINE", "UID SEARCH", "UID FETCH", "LOGOUT"} and "EXAMINE" in store.verbs()
    assert store.flag_snapshot() == before
    assert_peek_only(store)
    assert all("BODY.PEEK" in a for a in store.cmds("UID FETCH") if "BODY" in a)


def test_scans_fetch_headers_and_sizes_but_never_bodies(store):
    store.add("INBOX", "A", "x" * 5000)
    scan_all(mk(store))
    for args in store.cmds("UID FETCH"):
        assert "BODY.PEEK[HEADER.FIELDS (SUBJECT)]" in args and "BODY.PEEK[]" not in args and "RFC822.SIZE" in args


def test_every_connection_is_closed(store):
    populate(store)
    c = mk(store)
    got = files(scan_all(c))
    assert store.conns and all(x.closed for x in store.conns)
    store.conns.clear()
    c.open(got["imap:INBOX:1"]).close()
    assert len(store.conns) == 1 and store.conns[0].closed
    store.conns.clear()
    generator = c.scan({})
    next(generator)
    generator.close()                                                                                  # a consumer that stops early
    assert store.conns and all(x.closed for x in store.conns)
    store.conns.clear()
    store.password = "changed"
    with pytest.raises(AuthError):
        scan_all(c)
    assert len(store.conns) == 1 and store.conns[0].closed


# ══════════════════════════════════════════════ batching and the fetch window ══════════════════════════════════════════════


def test_batches_follow_batch_size_and_only_the_last_is_done(store):
    for n in range(25):
        store.add("INBOX", f"m{n}")
    batches = scan_all(mk(store, batch_size=10, fetch_chunk=5))
    assert [len(b.changes) for b in batches] == [10, 10, 5, 0]
    assert [b.done for b in batches] == [False, False, False, True]
    assert scopes(batches) == ["imap:INBOX:"] and batches[2].complete_scope == "imap:INBOX:"
    assert sorted(files(batches)) == sorted(f"imap:INBOX:{n}" for n in range(1, 26))


def test_a_fetch_never_asks_for_more_uids_than_fetch_chunk(store):
    for n in range(23):
        store.add("INBOX", f"m{n}")
    scan_all(mk(store, fetch_chunk=7, batch_size=1000))
    sizes = [len(a.split(" ", 1)[0].split(",")) for a in store.cmds("UID FETCH")]
    assert sizes == [7, 7, 7, 2]


def test_an_empty_folder_still_completes_its_scope(store):
    store.box("Empty")
    batches = scan_all(mk(store))
    assert scopes(batches) == ["imap:Empty:", "imap:INBOX:"] and not files(batches)
    assert store.cmds("UID SEARCH") == [] and store.cmds("UID FETCH") == []


# ══════════════════════════════════════════════ the cursor ══════════════════════════════════════════════


def test_cursors_are_json_and_carry_no_secrets(store):
    populate(store)
    batches = scan_all(mk(store, batch_size=2, fetch_chunk=1))
    blob = json.dumps([b.cursor for b in batches])
    assert len(batches) > 4
    for secret in (PASSWORD, USER, "imap.test", ENV, "alice@example.com", "bob@example.com"):
        assert secret not in blob
    assert sorted(batches[-1].cursor) == ["folders"] and "pos" not in batches[-1].cursor
    assert all(sorted(v) == ["full", "last", "v"] for v in batches[-1].cursor["folders"].values())


def test_resuming_from_any_batch_gives_the_same_result_without_repeats(store):
    for name in ("A", "B", "C"):
        for n in range(7):
            store.add(name, f"{name}{n}")
    store.add("A", "x", flags=("\\Deleted",))
    full = scan_all(mk(store, batch_size=4, fetch_chunk=3))
    expected = Mirror().scan(full)
    assert len(expected.items) == 21 and len(full) > 6
    for k in range(len(full) - 1):                                                                       # crash after batch k was recorded
        mirror = Mirror().scan(full[:k + 1])
        rest = scan_all(mk(store, batch_size=4, fetch_chunk=3), persisted(full[k].cursor))
        again = set(files(full[:k + 1])) & set(files(rest))
        assert not again, (k, again)
        mirror.resume(rest)
        assert {i: f.version for i, f in mirror.items.items()} == {i: f.version for i, f in expected.items.items()}, k
        assert rest[-1].done


def test_a_resumed_scan_skips_finished_folders_and_continues_after_the_last_finished_uid(store):
    for name in ("A", "B", "C"):
        for n in range(6):
            store.add(name, f"{name}{n}")
    full = scan_all(mk(store, batch_size=3, fetch_chunk=3))
    k = next(i for i, b in enumerate(full) if b.cursor.get("pos", {}).get("folder") == "B" and "after" in b.cursor["pos"])
    after = full[k].cursor["pos"]["after"]
    store.log.clear()
    rest = scan_all(mk(store, batch_size=3, fetch_chunk=3), persisted(full[k].cursor))
    assert store.cmds("EXAMINE") == ['"B"', '"C"', '"INBOX"']
    assert store.cmds("UID SEARCH") == [f"UID {after + 1}:*", "ALL"]
    assert "imap:B:" not in scopes(rest) and "imap:C:" in scopes(rest) and "imap:A:" not in scopes(rest)


def test_resuming_after_the_last_folder_does_not_read_any_folder_again(store):
    for name in ("A", "B"):
        for n in range(3):
            store.add(name, f"{name}{n}")
    stale = {"folders": {"Z": {"v": 1000, "last": 5, "full": NOW}}}                                      # Z is not on the server any more
    full = scan_all(mk(store), stale)
    assert scopes(full) == ["imap:A:", "imap:B:", "imap:INBOX:", "imap:Z:"]
    k = next(i for i, b in enumerate(full) if b.complete_scope == "imap:Z:")
    store.log.clear()
    rest = scan_all(mk(store), persisted(full[k].cursor))
    assert store.cmds("EXAMINE") == [] and len(rest) == 1 and rest[0].done and not rest[0].changes
    assert sorted(rest[0].cursor["folders"]) == ["A", "B", "INBOX"]


def test_folders_are_scanned_in_sorted_order_whatever_order_the_server_lists_them(store):
    for name in ("Zeta", "Beta", "Alpha"):
        store.add(name, "m")
    scan_all(mk(store))
    assert store.cmds("EXAMINE") == ['"Alpha"', '"Beta"', '"INBOX"', '"Zeta"']


def test_after_a_resumed_scan_the_next_scan_is_a_full_pass_again(store):
    for name in ("A", "B"):
        for n in range(6):
            store.add(name, f"{name}{n}")
    full = scan_all(mk(store, batch_size=3, fetch_chunk=3))
    k = next(i for i, b in enumerate(full) if b.cursor.get("pos", {}).get("folder") == "B" and "after" in b.cursor["pos"])
    rest = scan_all(mk(store, batch_size=3, fetch_chunk=3), persisted(full[k].cursor))
    assert rest[-1].cursor["folders"]["B"]["full"] is None and rest[-1].cursor["folders"]["A"]["full"] == NOW
    assert scopes(scan_all(mk(store), rest[-1].cursor)) == ["imap:B:"]                                  # B gets its authoritative pass, A stays incremental


def test_a_cursor_from_a_renumbered_folder_restarts_that_folder(store):
    for n in range(6):
        store.add("INBOX", f"m{n}")
    full = scan_all(mk(store, batch_size=3, fetch_chunk=3))
    cursor = persisted(full[0].cursor)
    assert cursor["pos"]["after"] == 3 and cursor["pos"]["v"] == 1000
    store.renumber("INBOX")
    store.log.clear()
    rest = scan_all(mk(store, batch_size=3, fetch_chunk=3), cursor)
    assert store.cmds("UID SEARCH") == ["ALL"] and scopes(rest) == ["imap:INBOX:"]
    assert sorted(files(rest)) == [f"imap:INBOX:{n}" for n in range(1, 7)]


@pytest.mark.parametrize("cursor", [
    None, {}, {"folders": "x"}, {"folders": []}, {"pos": 5}, {"pos": {"folder": 3}}, {"folders": {"INBOX": {"v": "a", "last": 1}}},
    {"folders": {"INBOX": 7}}, {"pos": {"folder": "INBOX"}}, {"pos": {"folder": "INBOX", "v": 1000}}, {"folders": {"INBOX": {"v": 1000, "last": "9"}}},
    [], "junk",
])
def test_a_damaged_cursor_means_a_full_scan_not_a_crash(store, cursor):
    store.add("INBOX", "a")
    store.add("INBOX", "b")
    batches = list(mk(store).scan(cursor if cursor is not None else {}))
    assert sorted(files(batches)) == ["imap:INBOX:1", "imap:INBOX:2"] and scopes(batches) == ["imap:INBOX:"]


# ══════════════════════════════════════════════ incremental scans ══════════════════════════════════════════════


def test_an_incremental_scan_reads_only_uids_above_the_last_seen(store):
    for n in range(5):
        store.add("INBOX", f"m{n}")
    clock = Clock()
    first = scan_all(mk(store, clock))
    cursor = persisted(first[-1].cursor)
    assert cursor["folders"]["INBOX"] == {"v": 1000, "last": 5, "full": NOW}
    store.add("INBOX", "new1")
    store.add("INBOX", "new2")
    store.log.clear()
    clock.advance(1)
    second = scan_all(mk(store, clock), cursor)
    assert sorted(files(second)) == ["imap:INBOX:6", "imap:INBOX:7"] and scopes(second) == []
    assert store.cmds("UID SEARCH") == ["UID 6:*"] and [a.split(" ", 1)[0] for a in store.cmds("UID FETCH")] == ["6,7"]
    assert second[-1].cursor["folders"]["INBOX"] == {"v": 1000, "last": 7, "full": NOW}


def test_an_incremental_scan_with_nothing_new_changes_nothing(store):
    for n in range(3):
        store.add("INBOX", f"m{n}")
    clock = Clock()
    cursor = persisted(scan_all(mk(store, clock))[-1].cursor)
    store.log.clear()
    clock.advance(1)
    again = scan_all(mk(store, clock), cursor)
    assert not files(again) and scopes(again) == [] and store.cmds("UID SEARCH") == ["UID 4:*"]    # the server answers 3 (`n:*` includes the newest); it is dropped
    assert store.cmds("UID FETCH") == [] and again[-1].cursor["folders"]["INBOX"]["last"] == 3


def test_incremental_scans_do_not_report_deletions_only_full_passes_do(store):
    for n in range(4):
        store.add("INBOX", f"m{n}")
    clock = Clock()
    mirror = Mirror().scan(scan_all(mk(store, clock)))
    cursor = persisted(scan_all(mk(store, clock))[-1].cursor)
    store.expunge("INBOX", 2)
    clock.advance(23)
    mirror.scan(scan_all(mk(store, clock), cursor))
    assert "imap:INBOX:2" in mirror.items                                                              # not noticed yet: no full pass has run
    clock.advance(2)                                                                                   # 25 h after the full pass
    mirror.scan(scan_all(mk(store, clock), cursor))
    assert sorted(mirror.items) == ["imap:INBOX:1", "imap:INBOX:3", "imap:INBOX:4"]


def test_the_full_resync_interval_is_honoured_at_the_boundary(store):
    store.add("INBOX", "a")
    clock = Clock()
    cursor = persisted(scan_all(mk(store, clock, full_resync_hours=24))[-1].cursor)
    for hours, scope in ((23.99, []), (24, ["imap:INBOX:"]), (30, ["imap:INBOX:"])):
        probe = Clock(NOW)
        probe.advance(hours)
        store.log.clear()
        batches = scan_all(mk(store, probe, full_resync_hours=24), cursor)
        assert scopes(batches) == scope, hours
        assert store.cmds("UID SEARCH") == (["ALL"] if scope else ["UID 2:*"]), hours


def test_a_full_pass_deletes_what_was_expunged_or_flagged_deleted(store):
    for n in range(6):
        store.add("INBOX", f"m{n}")
    mirror = Mirror().scan(scan_all(mk(store)))
    assert len(mirror.items) == 6
    store.expunge("INBOX", 2)
    store.set_flags("INBOX", 4, ("\\Deleted",))
    mirror.scan(scan_all(mk(store, full_resync_hours=0)))
    assert sorted(mirror.items) == ["imap:INBOX:1", "imap:INBOX:3", "imap:INBOX:5", "imap:INBOX:6"]
    store.expunge("INBOX", 1, 3, 5, 6)
    mirror.scan(scan_all(mk(store, full_resync_hours=0)))
    assert mirror.items == {}                                                                          # an emptied folder empties the index


def test_messages_flagged_deleted_are_not_indexed_even_when_new(store):
    store.add("INBOX", "keep")
    store.add("INBOX", "dead", flags=("\\Deleted", "\\Seen"))
    c = mk(store)
    assert sorted(files(scan_all(c))) == ["imap:INBOX:1"] and c.stats["flagged_deleted"] == 1


def test_a_changed_uidvalidity_is_a_full_resync_of_that_folder_only(store):
    for n in range(5):
        store.add("B", f"b{n}")
    for n in range(3):
        store.add("A", f"a{n}")
    clock = Clock()
    mirror = Mirror().scan(scan_all(mk(store, clock)))
    cursor = persisted(scan_all(mk(store, clock))[-1].cursor)
    store.expunge("B", 2)
    store.renumber("B")                                                                               # B now has uids 1-4 under UIDVALIDITY 1001
    store.add("A", "late")
    store.log.clear()
    clock.advance(1)
    batches = scan_all(mk(store, clock), cursor)
    assert store.cmds("UID SEARCH") == ["UID 4:*", "ALL"]                                              # A incremental, B from scratch (INBOX is empty: no search)
    assert scopes(batches) == ["imap:B:"]
    mirror.scan(batches)
    assert sorted(i for i in mirror.items if i.startswith("imap:B:")) == [f"imap:B:{n}" for n in range(1, 5)]       # imap:B:5 belonged to the old numbering
    assert all(f.version.startswith("1001:") for i, f in mirror.items.items() if i.startswith("imap:B:"))
    assert "imap:A:4" in mirror.items and mirror.items["imap:A:1"].version.startswith("1000:")


def test_a_folder_whose_new_numbering_is_lower_than_the_old_last_uid_is_still_read(store):
    for n in range(8):
        store.add("INBOX", f"m{n}")
    clock = Clock()
    cursor = persisted(scan_all(mk(store, clock))[-1].cursor)
    assert cursor["folders"]["INBOX"]["last"] == 8
    store.expunge("INBOX", 3, 4, 5, 6, 7, 8)
    store.renumber("INBOX")
    batches = scan_all(mk(store, clock), cursor)
    assert sorted(files(batches)) == ["imap:INBOX:1", "imap:INBOX:2"] and scopes(batches) == ["imap:INBOX:"]


def test_a_folder_that_vanished_from_the_server_is_deleted_and_its_neighbours_are_not(store):
    store.add("INBOX", "i")
    store.add("INBOX/Sub", "s")
    store.add("A", "a")
    store.add("A:1", "colon")
    store.add("A/B", "slash")
    clock = Clock()
    mirror = Mirror().scan(scan_all(mk(store, clock)))
    cursor = persisted(scan_all(mk(store, clock))[-1].cursor)
    assert sorted(mirror.items) == ["imap:A%2FB:1", "imap:A%3A1:1", "imap:A:1", "imap:INBOX%2FSub:1", "imap:INBOX:1"]
    del store.boxes["INBOX/Sub"]
    del store.boxes["A:1"]
    store.log.clear()
    batches = scan_all(mk(store, clock), cursor)
    assert scopes(batches) == ["imap:A%3A1:", "imap:INBOX%2FSub:"]
    assert [b for b in batches if b.complete_scope and b.changes] == []
    mirror.scan(batches)
    assert sorted(mirror.items) == ["imap:A%2FB:1", "imap:A:1", "imap:INBOX:1"]
    assert batches[-1].cursor["folders"].keys() == {"A", "A/B", "INBOX"}


def test_a_full_pass_of_one_folder_never_deletes_in_a_folder_whose_name_starts_the_same(store):
    for name in ("A", "A:1", "A/B", "A:1:2"):
        store.add(name, "m")
    clock = Clock()
    mirror = Mirror().scan(scan_all(mk(store, clock)))
    cursor = persisted(scan_all(mk(store, clock))[-1].cursor)
    assert len(mirror.items) == 4
    cursor["folders"]["A"]["full"] = NOW - 48 * 3600                                                    # A is due for a full pass; its look-alikes get incremental ones
    store.expunge("A", 1)
    batches = scan_all(mk(store, clock), cursor)
    assert scopes(batches) == ["imap:A:"] and not files(batches)
    assert sorted(mirror.scan(batches).items) == ["imap:A%2FB:1", "imap:A%3A1%3A2:1", "imap:A%3A1:1"]


def test_a_folder_that_no_longer_matches_the_configuration_is_deleted(store):
    store.add("INBOX", "i")
    store.add("Projects", "p")
    mirror = Mirror().scan(scan_all(mk(store)))
    cursor = persisted(scan_all(mk(store))[-1].cursor)
    narrowed = mk(store, exclude_folders=["Projects"])
    batches = scan_all(narrowed, cursor)
    assert scopes(batches) == ["imap:Projects:"]
    assert sorted(mirror.scan(batches).items) == ["imap:INBOX:1"]


def test_a_folder_that_cannot_be_opened_is_left_exactly_as_it_was(store):
    store.add("INBOX", "i")
    store.add("Secret", "s")
    clock = Clock()
    mirror = Mirror().scan(scan_all(mk(store, clock)))
    cursor = persisted(scan_all(mk(store, clock))[-1].cursor)
    store.refuse.add("Secret")
    store.add("INBOX", "new")
    clock.advance(30)
    c = mk(store, clock)
    batches = scan_all(c, cursor)
    assert scopes(batches) == ["imap:INBOX:"] and c.stats["unreadable_folders"] == 1
    mirror.scan(batches)
    assert sorted(mirror.items) == ["imap:INBOX:1", "imap:INBOX:2", "imap:Secret:1"]                     # Secret was not deleted
    assert batches[-1].cursor["folders"]["Secret"] == cursor["folders"]["Secret"]
    fresh = scan_all(mk(store, clock))                                                                  # never seen before and unreadable: no error, no state
    assert "Secret" not in fresh[-1].cursor["folders"] and "imap:Secret:" not in scopes(fresh)


# ══════════════════════════════════════════════ permissions ══════════════════════════════════════════════


def test_acl_rules_follow_the_folder_paths_and_everything_else_is_unreadable(store):
    store.add("INBOX", "mail")
    store.add("Projects/Alpha", "plan")
    store.add("Projects/Beta", "secret plan")
    store.add("Elsewhere", "other")
    rules = [{"prefix": "imap/Projects", "principals": ["group:Projects"]}, {"prefix": "imap/Projects/Beta", "principals": ["user:Ann"]},
             {"prefix": "imap/INBOX", "principals": ["user:Alice", "group:Staff"]}]
    c = mk(store, default_acl=None, acl_rules=rules)
    got = {f.path.split("/", 1)[1]: f for f in files(scan_all(c)).values()}
    assert got["INBOX/1-mail.eml"].acl == ["group:staff", "user:alice"]
    assert got["Projects/Alpha/1-plan.eml"].acl == ["group:projects"] and got["Projects/Beta/1-secret plan.eml"].acl == ["user:ann"]
    assert got["Elsewhere/1-other.eml"].acl == []                                                        # no rule, no default: nobody (fail closed)
    assert c.refresh_acl(got["Projects/Beta/1-secret plan.eml"]) == ["user:ann"] and c.refresh_acl(got["Elsewhere/1-other.eml"]) == []
    changed = mk(store, default_acl=None, acl_rules=[{"prefix": "imap/Projects/Beta", "principals": ["group:everyone-now"]}])                  # the rules changed since the scan
    assert changed.refresh_acl(got["Projects/Beta/1-secret plan.eml"]) == ["group:everyone-now"] and changed.refresh_acl(got["INBOX/1-mail.eml"]) == []
    nothing = mk(store, default_acl=None)
    assert all(f.acl == [] for f in files(scan_all(nothing)).values())
    assert all(f.acl == ["*"] for f in files(scan_all(mk(store, default_acl=["*"]))).values())


# ══════════════════════════════════════════════ open() ══════════════════════════════════════════════


def test_open_returns_the_exact_bytes_of_the_message(store):
    binary = b"From: a@b\r\nSubject: bin\r\n\r\n" + bytes(range(256)) * 9 + b"\r\nlone LF\nand CR\r\x00 tail \xff\xfe"
    store.add("INBOX", "plain", "hello wörld")
    store.add("INBOX", raw=binary)
    store.add("INBOX", raw=b"")
    c = mk(store)
    got = files(scan_all(c))
    assert read(c, got["imap:INBOX:1"]) == store.raw("INBOX", 1)
    assert read(c, got["imap:INBOX:2"]) == binary
    assert read(c, got["imap:INBOX:3"]) == b""


def test_big_messages_are_fetched_in_pieces_and_streamed_to_a_temp_file(store):
    size = 5000
    raw = b"Subject: big\r\n\r\n" + bytes((i * 7) % 256 for i in range(size - 16))
    assert len(raw) == size
    store.add("INBOX", raw=raw)
    store.add("INBOX", raw=raw[:3072])                                                                   # an exact multiple of the piece size
    store.add("INBOX", raw=raw[:1024])                                                                   # exactly one piece
    c = mk(store, chunk_bytes=1024)
    got = files(scan_all(c))
    store.log.clear()
    handle = c.open(got["imap:INBOX:1"])
    assert handle.read() == raw and handle.fileno() >= 0
    pieces = [a for a in store.cmds("UID FETCH") if "BODY.PEEK" in a]
    assert [p.split("(")[1].rstrip(")") for p in pieces] == [f"BODY.PEEK[]<{o}.1024>" for o in (0, 1024, 2048, 3072, 4096)]            # the short last piece ends it
    store.log.clear()
    assert read(c, got["imap:INBOX:2"]) == raw[:3072]
    assert [p.split("(")[1].rstrip(")") for p in store.cmds("UID FETCH") if "BODY.PEEK" in p] == [f"BODY.PEEK[]<{o}.1024>" for o in (0, 1024, 2048, 3072)]    # ...or an empty one
    store.log.clear()
    assert read(c, got["imap:INBOX:3"]) == raw[:1024]
    assert [p.split("(")[1].rstrip(")") for p in store.cmds("UID FETCH") if "BODY.PEEK" in p] == ["BODY.PEEK[]"]       # fits one piece: fetched whole
    assert_peek_only(store)


def test_open_refuses_a_message_over_the_size_cap_without_fetching_it(store):
    store.add("INBOX", raw=b"Subject: big\r\n\r\n" + b"z" * 5000)
    store.add("INBOX", raw=b"Subject: ok\r\n\r\nsmall")
    c = mk(store, max_message_bytes=100_000)
    got = files(scan_all(c))
    capped = mk(store, max_message_bytes=1000)
    store.log.clear()
    with pytest.raises(TooLarge, match="max_message_bytes"):
        capped.open(got["imap:INBOX:1"])
    assert not [a for a in store.cmds("UID FETCH") if "BODY" in a]
    assert read(capped, got["imap:INBOX:2"]).endswith(b"small")


def test_open_of_a_message_that_is_gone_or_renumbered_is_a_404(store):
    store.add("INBOX", "a")
    store.add("INBOX", "b")
    c = mk(store)
    got = files(scan_all(c))
    store.expunge("INBOX", 2)
    with pytest.raises(HttpError) as gone:
        c.open(got["imap:INBOX:2"])
    assert gone.value.status == 404
    store.renumber("INBOX")                                                                             # uid 1 now exists, but it is not the file that was scanned
    with pytest.raises(HttpError) as renumbered:
        c.open(got["imap:INBOX:1"])
    assert renumbered.value.status == 404 and "renumbered" in str(renumbered.value)
    del store.boxes["INBOX"]
    with pytest.raises(HttpError) as folder:
        c.open(got["imap:INBOX:1"])
    assert folder.value.status == 404


def test_open_rejects_a_foreign_external_id(store):
    c = mk(store)
    store.add("INBOX", "a")
    f = files(scan_all(c))["imap:INBOX:1"]
    for bad in ("imap:INBOX", "other:INBOX:1", "imap:INBOX:x", "imap:INBOX:1:2", ""):
        f2 = type(f)(**{**f.__dict__, "external_id": bad})
        with pytest.raises(HttpError):
            c.open(f2)


def test_open_survives_a_dropped_connection_in_the_middle_of_a_big_message(store):
    raw = b"Subject: big\r\n\r\n" + bytes((i * 11) % 256 for i in range(6000))
    store.add("INBOX", raw=raw)
    c = mk(store, chunk_bytes=1024)
    f = files(scan_all(c))["imap:INBOX:1"]
    store.drop("UID FETCH", skip=3)                                                                    # the size query and pieces 0 and 1 are answered; piece 2 is dropped
    assert read(c, f) == raw
    assert c.sleeps == [1] and len(store.conns) == 3


def test_open_reads_what_the_server_has_even_when_it_announced_the_wrong_size(store):
    big = b"Subject: big\r\n\r\n" + bytes((i * 3) % 256 for i in range(5000))
    small = b"Subject: small\r\n\r\nhello"
    store.add("INBOX", raw=big)
    store.add("INBOX", raw=small)
    for skew in (-30, 0, 30):
        store.size_skew = skew
        c = mk(store, chunk_bytes=1024)
        got = files(scan_all(c))
        assert got["imap:INBOX:1"].size == len(big) + skew and read(c, got["imap:INBOX:1"]) == big and read(c, got["imap:INBOX:2"]) == small
    store.size_skew = -4900                                                                                 # announced 116 bytes, 5016 arrive: the cap counts what arrived
    c = mk(store, max_message_bytes=1000)
    got = files(scan_all(c))
    assert got["imap:INBOX:1"].size == len(big) - 4900
    with pytest.raises(TooLarge, match="max_message_bytes"):
        c.open(got["imap:INBOX:1"])


def test_a_server_that_answers_a_piece_with_nothing_or_too_much_is_an_error_not_a_loop(store, monkeypatch):
    store.add("INBOX", raw=b"Subject: big\r\n\r\n" + b"q" * 3000)
    store.add("INBOX", raw=b"Subject: small\r\n\r\nx")
    c = mk(store, chunk_bytes=1024)
    got = files(scan_all(c))
    opened: list = []
    real = imap_mod.tempfile.TemporaryFile
    monkeypatch.setattr(imap_mod.tempfile, "TemporaryFile", lambda *a, **k: opened.append(real(*a, **k)) or opened[-1])
    store.glitch = "empty"
    with pytest.raises(HttpError, match="could not be read at offset 0"):
        c.open(got["imap:INBOX:1"])
    with pytest.raises(HttpError, match="could not be read at offset 0"):
        c.open(got["imap:INBOX:2"])
    store.glitch = "long"
    with pytest.raises(HttpError, match="returned 1029 bytes for a window of 1024"):
        c.open(got["imap:INBOX:1"])
    assert len(opened) == 3 and all(f.closed for f in opened)                                             # no temp file is left behind by a failed read
    store.glitch = None
    assert read(c, got["imap:INBOX:1"]).endswith(b"q" * 3000)


# ══════════════════════════════════════════════ size cap, vanishing messages, odd servers ══════════════════════════════════════════════


def test_messages_over_max_message_bytes_are_skipped_not_fetched(store):
    store.add("INBOX", "small", "x")
    store.add("INBOX", raw=b"Subject: big\r\n\r\n" + b"y" * 4000)
    store.add("INBOX", "small too", "z")
    c = mk(store, max_message_bytes=2000)
    batches = scan_all(c)
    assert sorted(files(batches)) == ["imap:INBOX:1", "imap:INBOX:3"] and c.stats["oversize"] == 1
    assert not [a for a in store.cmds("UID FETCH") if "BODY.PEEK[]" in a]


def test_a_message_expunged_between_search_and_fetch_is_skipped(store):
    for n in range(4):
        store.add("INBOX", f"m{n}")

    def hook(conn, verb, args):
        if verb == "UID FETCH" and 2 in store.boxes["INBOX"].msgs:
            store.expunge("INBOX", 2)

    store.on_command = hook
    c = mk(store)
    batches = scan_all(c)
    assert sorted(files(batches)) == ["imap:INBOX:1", "imap:INBOX:3", "imap:INBOX:4"] and c.stats["vanished_while_scanning"] == 1
    assert batches[-1].cursor["folders"]["INBOX"]["last"] == 4


def test_a_server_that_hides_uidvalidity_is_an_error_not_a_guess(store):
    store.add("INBOX", "a")
    store.hide_uidvalidity = True
    with pytest.raises(HttpError, match="UIDVALIDITY"):
        scan_all(mk(store))


def test_a_folder_listed_by_the_server_but_refused_by_a_non_ok_examine_is_skipped_quietly(store):
    store.add("INBOX", "a")
    store.refusals["EXAMINE"] = ("NO", "[NOPERM] no access")
    c = mk(store)
    batches = scan_all(c)
    assert not files(batches) and c.stats["unreadable_folders"] == 1 and scopes(batches) == []


# ══════════════════════════════════════════════ failures ══════════════════════════════════════════════


def test_a_wrong_password_is_an_auth_error_that_says_nothing_about_the_password(store, caplog):
    caplog.set_level(logging.DEBUG)
    store.add("INBOX", "a")
    store.password = "the-real-one"
    c = mk(store)
    with pytest.raises(AuthError) as err:
        scan_all(c)
    text = str(err.value) + repr(err.value) + repr(err.value.__cause__) + repr(err.value.__context__)
    assert PASSWORD not in text and "Authentication failed" not in text and "AUTHENTICATIONFAILED" not in text and "alice" in text
    assert PASSWORD not in caplog.text
    assert store.attempts == 1 and c.sleeps == []                                                        # not retried: the answer will not change


def test_the_password_is_read_when_connecting_and_an_unset_variable_is_a_clear_error(store, monkeypatch):
    store.add("INBOX", "a")
    c = mk(store)
    monkeypatch.setenv(ENV, "rotated-" + PASSWORD)
    with pytest.raises(AuthError) as err:
        scan_all(c)                                                                                       # picked up without a new connector
    assert "rotated-" not in str(err.value) + repr(err.value)
    monkeypatch.setenv(ENV, PASSWORD)
    assert files(scan_all(c))
    monkeypatch.delenv(ENV)
    store.conns.clear()
    store.attempts = 0
    with pytest.raises(HttpError, match=ENV):
        scan_all(c)
    assert store.attempts == 0                                                                           # nothing was dialled without a password


def test_a_dropped_connection_is_retried_from_where_it_stopped(store):
    for n in range(6):
        store.add("INBOX", f"m{n}")
    store.add("Other", "o")
    store.drop("UID FETCH", skip=1)                                                                      # the second FETCH of the scan loses the connection
    c = mk(store, fetch_chunk=2, batch_size=100)
    batches = scan_all(c)
    assert sorted(files(batches)) == [f"imap:INBOX:{n}" for n in range(1, 7)] + ["imap:Other:1"] and c.sleeps == [1]
    first, second = store.conns[0], store.conns[1]
    mine = [(v, a) for cid, v, a in store.log if cid == second.id]
    assert mine[:3] == [("LOGIN", USER), ("EXAMINE", '"INBOX"'), ("UID FETCH", mine[2][1])] and mine[2][1].startswith("3,4 ")                  # logged in, re-opened the folder, retried the same UIDs
    assert first.closed and second.closed and batches[-1].done


def test_a_drop_during_login_is_retried_and_is_not_a_wrong_password(store):
    store.add("INBOX", "a")
    store.drop("LOGIN")
    c = mk(store)
    assert files(scan_all(c)) and c.sleeps == [1]


def test_a_persistent_failure_gives_up_after_max_retries_with_growing_waits(store, caplog):
    caplog.set_level(logging.DEBUG)
    store.add("INBOX", "a")
    store.drop("UID SEARCH", times=None)
    c = mk(store, max_retries=3)
    with pytest.raises(HttpError, match="UID SEARCH") as err:
        scan_all(c)
    assert store.attempts == 4 and c.sleeps == [1, 2, 4] and PASSWORD not in str(err.value) + caplog.text
    capped = mk(store, max_retries=4, max_wait=3)
    store.attempts = 0
    with pytest.raises(HttpError):
        scan_all(capped)
    assert capped.sleeps == [1, 2, 3, 3]
    none = mk(store, max_retries=0)
    store.attempts = 0
    with pytest.raises(HttpError):
        scan_all(none)
    assert store.attempts == 1 and none.sleeps == []


def test_a_server_that_is_down_is_retried_then_reported(store):
    store.down = True
    c = mk(store, max_retries=2)
    with pytest.raises(HttpError, match="ConnectionRefusedError"):
        scan_all(c)
    assert store.attempts == 3 and c.sleeps == [1, 2]


def test_a_refusal_with_the_password_in_it_is_scrubbed(store, caplog):
    caplog.set_level(logging.DEBUG)
    store.add("INBOX", "a")
    store.refusals["LIST"] = ("BAD", f"command not understood, your password {PASSWORD} was in it")
    c = mk(store)
    with pytest.raises(HttpError) as err:
        scan_all(c)
    assert "***" in str(err.value) and PASSWORD not in str(err.value) + repr(err.value) + repr(err.value.__context__) + caplog.text
    assert store.attempts == 1 and c.sleeps == []                                                       # a refusal is an answer: not retried
    store.refusals["LIST"] = ("NO", "denied")
    with pytest.raises(HttpError, match="answered NO"):
        scan_all(mk(store))


def test_an_error_in_the_middle_of_a_folder_never_ends_in_a_completed_scope(store):
    for n in range(8):
        store.add("INBOX", f"m{n}")
    store.drop("UID FETCH", skip=2, times=None)
    out, error = collect(mk(store, batch_size=2, fetch_chunk=2, max_retries=1).scan({}))
    assert isinstance(error, HttpError) and out and scopes(out) == [] and not any(b.done for b in out)
    store.drops.clear()
    rest = scan_all(mk(store, batch_size=2, fetch_chunk=2), persisted(out[-1].cursor))
    assert sorted(set(files(out)) | set(files(rest))) == [f"imap:INBOX:{n}" for n in range(1, 9)] and not (set(files(out)) & set(files(rest)))


def test_a_renumbering_during_a_reconnect_fails_the_scan_and_the_next_one_starts_over(store):
    for n in range(6):
        store.add("INBOX", f"m{n}")
    store.drop("UID FETCH", skip=1)
    store.on_connect = lambda conn: store.renumber("INBOX") if conn.id == 2 else None
    out, error = collect(mk(store, batch_size=2, fetch_chunk=2).scan({}))
    assert isinstance(error, HttpError) and "renumbered" in str(error) and scopes(out) == [] and out
    store.log.clear()
    rest = scan_all(mk(store, batch_size=2, fetch_chunk=2), persisted(out[-1].cursor))
    assert store.cmds("UID SEARCH") == ["ALL"] and scopes(rest) == ["imap:INBOX:"] and sorted(files(rest)) == [f"imap:INBOX:{n}" for n in range(1, 7)]


def test_nothing_secret_reaches_the_logs_or_the_stats(store, caplog):
    caplog.set_level(logging.DEBUG)
    populate(store)
    store.drop("UID FETCH")
    c = mk(store)
    batches = scan_all(c)
    for f in files(batches).values():
        c.open(f).close()
    blob = caplog.text + repr(c.stats) + repr([f.extra for f in files(batches).values()])
    assert PASSWORD not in blob and caplog.text


# ══════════════════════════════════════════════ the same behaviour over a real socket and the real imaplib ══════════════════════════════════════════════


def normalized(batches):
    return [(b.done, b.complete_scope, b.cursor,
             [(c.kind, c.external_id, c.file.path, c.file.name, c.file.size, c.file.modified_at, c.file.version, c.file.acl, c.file.extra) for c in b.changes])
            for b in batches]


def per_connection(store):
    runs: dict = {}
    for cid, verb, args in store.log:
        runs.setdefault(cid, []).append((verb, args))
    return [runs[k] for k in sorted(runs)]


@pytest.mark.parametrize("order, literals", [("natural", False), ("reversed", False), ("natural", True), ("reversed", True)])
def test_fake_and_wire_server_give_identical_scans(servers, order, literals):
    fake_store, wire_store = Store(), Store()
    for s in (fake_store, wire_store):
        populate(s)
        s.fetch_order, s.list_literals = order, literals
    cfg = {"batch_size": 2, "fetch_chunk": 2}
    fake = normalized(scan_all(mk(fake_store, **cfg)))
    real = normalized(scan_all(over_tcp(servers(wire_store), **cfg)))
    assert real == fake and len(fake) > 5
    assert per_connection(wire_store) == per_connection(fake_store)
    paths = sorted(c[2] for b in real for c in b[3])
    assert paths == sorted(["imap/INBOX/1-Quarterly report.eml", "imap/INBOX/2-Lunch.eml", "imap/INBOX/4-A folded subject line.eml", "imap/Sent Items/1-Grüße aus Köln.eml",
                            "imap/Entwürfe/1-Draft.eml", "imap/Projects/Alpha/1-Kickoff.eml"])


def test_real_imaplib_reads_every_message_exactly_over_tcp(store, servers):
    raw = b"Subject: big\r\n\r\n" + bytes((i * 13) % 256 for i in range(70_000)) + b"\r\nend\r\n"
    store.add("INBOX", raw=raw)
    store.add("INBOX", "tiny", "x")
    store.add("INBOX", raw=b"")
    c = over_tcp(servers(store), chunk_bytes=4096)
    got = files(scan_all(c))
    assert read(c, got["imap:INBOX:1"]) == raw and read(c, got["imap:INBOX:2"]) == store.raw("INBOX", 2) and read(c, got["imap:INBOX:3"]) == b""
    assert len([a for a in store.cmds("UID FETCH") if "BODY.PEEK[]<" in a]) == -(-len(raw) // 4096)
    assert_peek_only(store)


def test_over_tcp_nothing_is_written_or_marked_and_every_connection_says_goodbye(store, servers):
    populate(store)
    before = store.flag_snapshot()
    c = over_tcp(servers(store))
    for f in files(scan_all(c)).values():
        c.open(f).close()
    assert store.verbs() <= {"LOGIN", "LIST", "EXAMINE", "UID SEARCH", "UID FETCH", "LOGOUT"} and "SELECT" not in store.verbs()
    assert store.flag_snapshot() == before
    assert_peek_only(store)
    assert {cid for cid, v, _ in store.log if v == "LOGOUT"} == {x.id for x in store.conns}


def test_over_tcp_names_with_spaces_quotes_and_unicode_reach_the_server_intact(store, servers):
    for name in ("Sent Items", "Entw&APw-rfe", 'Odd "name" \\ x', "A:1", "P&AOQ-ck"):
        store.add(name, "m")
    c = over_tcp(servers(store))
    got = files(scan_all(c))
    assert {f.extra["folder"] for f in got.values()} == {"Sent Items", "Entwürfe", 'Odd "name" \\ x', "A:1", "Päck"}
    assert sorted(store.cmds("EXAMINE")) == sorted(['"INBOX"', '"Sent Items"', '"Entw&APw-rfe"', '"Odd \\"name\\" \\\\ x"', '"A:1"', '"P&AOQ-ck"'])


def test_over_tcp_incremental_scans_use_uid_ranges_the_server_understands(store, servers):
    for n in range(4):
        store.add("INBOX", f"m{n}")
    c = over_tcp(servers(store))
    cursor = persisted(scan_all(c)[-1].cursor)
    store.add("INBOX", "new")
    store.log.clear()
    again = scan_all(c, cursor)
    assert sorted(files(again)) == ["imap:INBOX:5"] and store.cmds("UID SEARCH") == ["UID 5:*"] and scopes(again) == []
    store.log.clear()
    cursor2 = persisted(again[-1].cursor)
    assert not files(scan_all(c, cursor2)) and store.cmds("UID FETCH") == []


def test_over_tcp_a_wrong_password_is_an_auth_error_without_the_servers_words(store, servers):
    store.password = "the-real-one"
    c = over_tcp(servers(store))
    with pytest.raises(AuthError) as err:
        scan_all(c)
    text = str(err.value) + repr(err.value) + repr(err.value.__cause__) + repr(err.value.__context__)
    assert PASSWORD not in text and "AUTHENTICATIONFAILED" not in text and "using password" not in text
    assert store.cmds("LOGIN") == [USER] and c.sleeps == []


def test_over_tcp_a_non_ascii_password_is_a_clear_error(store, servers, monkeypatch):
    monkeypatch.setenv(ENV, "pässwörd-€")
    with pytest.raises(HttpError, match="ASCII"):
        scan_all(over_tcp(servers(store)))


def test_over_tcp_a_connection_the_server_drops_is_retried(store, servers):
    for n in range(5):
        store.add("INBOX", f"m{n}")
    store.drop("UID FETCH", skip=1)
    c = over_tcp(servers(store), fetch_chunk=2)
    assert sorted(files(scan_all(c))) == [f"imap:INBOX:{n}" for n in range(1, 6)] and c.sleeps == [1]


def test_over_tcp_a_server_that_never_answers_times_out_instead_of_hanging():
    silent = socket.socket()
    silent.bind(("127.0.0.1", 0))
    silent.listen(5)
    try:
        c = ImapConnector({"host": "127.0.0.1", "port": silent.getsockname()[1], "ssl": False, "username": USER, "password_env": ENV, "timeout": 0.3,
                           "max_retries": 1}, sleep=lambda s: None)
        with pytest.raises(HttpError, match="TimeoutError|timed out"):
            scan_all(c)
    finally:
        silent.close()


def test_over_tcp_a_closed_port_is_reported_after_the_retries():
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    sleeps: list = []
    c = ImapConnector({"host": "127.0.0.1", "port": port, "ssl": False, "username": USER, "password_env": ENV, "max_retries": 2}, sleep=sleeps.append)
    with pytest.raises(HttpError, match="ConnectionRefusedError") as err:
        scan_all(c)
    assert sleeps == [1, 2] and PASSWORD not in str(err.value)


def test_over_tcp_the_server_refusing_examine_skips_the_folder(store, servers):
    store.add("INBOX", "a")
    store.add("Secret", "s")
    store.refuse.add("Secret")
    c = over_tcp(servers(store))
    assert sorted(files(scan_all(c))) == ["imap:INBOX:1"] and c.stats["unreadable_folders"] == 1


# ── TLS: real certificates ──


@pytest.fixture(scope="module")
def certificate(tmp_path_factory):
    if not shutil.which("openssl"):
        pytest.skip("the openssl command line tool is needed to make a throw-away certificate")
    folder = tmp_path_factory.mktemp("tls")
    key, cert = folder / "key.pem", folder / "cert.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key), "-out", str(cert), "-days", "3", "-subj", "/CN=localhost",
                    "-addext", "subjectAltName=DNS:localhost", "-addext", "basicConstraints=critical,CA:TRUE",
                    "-addext", "keyUsage=critical,digitalSignature,keyEncipherment,keyCertSign"], check=True, capture_output=True)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(cert), str(key))
    return str(cert), ctx


def tls_connector(srv, **cfg):
    return over_tcp(srv, **{"ssl": True, "host": "localhost", **cfg})


def test_tls_an_untrusted_certificate_is_refused_and_the_password_is_never_sent(store, servers, certificate):
    store.add("INBOX", "a")
    c = tls_connector(servers(store, "implicit", certificate[1]))
    with pytest.raises(HttpError, match="certificate verification failed") as err:
        scan_all(c)
    assert store.cmds("LOGIN") == [] and c.sleeps == [] and PASSWORD not in str(err.value)           # a bad certificate is not retried


def test_tls_a_ca_bundle_makes_the_same_server_trusted(store, servers, certificate):
    store.add("INBOX", "a")
    c = tls_connector(servers(store, "implicit", certificate[1]), ca_bundle=certificate[0])
    assert sorted(files(scan_all(c))) == ["imap:INBOX:1"] and store.cmds("LOGIN") == [USER]


def test_tls_verification_can_be_switched_off_explicitly(store, servers, certificate):
    store.add("INBOX", "a")
    assert sorted(files(scan_all(tls_connector(servers(store, "implicit", certificate[1]), verify_tls=False)))) == ["imap:INBOX:1"]


def test_tls_a_certificate_for_another_host_name_is_refused_even_with_the_right_ca(store, servers, certificate):
    store.add("INBOX", "a")
    srv = servers(store, "implicit", certificate[1])
    with pytest.raises(HttpError, match="certificate verification failed"):
        scan_all(tls_connector(srv, ca_bundle=certificate[0], host="127.0.0.1"))                      # the certificate names `localhost`, not this address
    assert store.cmds("LOGIN") == []
    assert sorted(files(scan_all(tls_connector(srv, ca_bundle=certificate[0], host="localhost")))) == ["imap:INBOX:1"]


def test_starttls_upgrades_the_connection_before_the_password_is_sent(store, servers, certificate):
    store.add("INBOX", "a")
    c = over_tcp(servers(store, "starttls", certificate[1]), starttls=True, ca_bundle=certificate[0], host="localhost")
    assert sorted(files(scan_all(c))) == ["imap:INBOX:1"]
    verbs = [v for cid, v, _ in store.log if cid == 1]
    assert verbs[:2] == ["STARTTLS", "LOGIN"]


def test_starttls_that_the_server_does_not_offer_never_falls_back_to_plain_text(store, servers):
    store.add("INBOX", "a")
    c = over_tcp(servers(store), starttls=True)
    with pytest.raises(HttpError, match="does not offer STARTTLS") as err:
        scan_all(c)
    assert store.cmds("LOGIN") == [] and PASSWORD not in str(err.value) and c.sleeps == [] and len(store.conns) == 1       # permanent: not retried


def test_starttls_with_an_untrusted_certificate_never_sends_the_password(store, servers, certificate):
    store.add("INBOX", "a")
    c = over_tcp(servers(store, "starttls", certificate[1]), starttls=True, host="localhost")
    with pytest.raises(HttpError, match="certificate verification failed"):
        scan_all(c)
    assert store.cmds("STARTTLS") == [""] and store.cmds("LOGIN") == []
