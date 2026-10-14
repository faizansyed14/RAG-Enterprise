"""FTP / FTPS connector tests.

They run against a REAL FTP server: pyftpdlib on a local port (real control and data connections, real MLSD, LIST,
MDTM, RETR, real 421 / 530 / 550 replies, real explicit FTPS with a self-signed certificate generated for the
test). Failure injection is done inside the server's own command handlers (refuse a connection with the server's
421 reply, drop the connection while a command is being processed), not by stubbing ftplib.

The MS-DOS / IIS LIST format cannot be produced by pyftpdlib, so it is only unit-tested on sample lines.
"""

import datetime as dt
import itertools
import json
import logging
import os
import threading

import pytest
from pyftpdlib.authorizers import DummyAuthorizer
from pyftpdlib.handlers import FTPHandler
from pyftpdlib.servers import ThreadedFTPServer

from app.scale.connectors._http import AuthError, HttpError, TooLarge
from app.scale.connectors.base import SourceFile
from app.scale.connectors.ftp import FtpConnector, parse_list_line, parse_mlsd_time

PASSWORD = "ftp-pw-MUST-NOT-LEAK-42"
logging.getLogger("pyftpdlib").setLevel(logging.ERROR)
# When the test server hangs up on purpose (421 refusal, dropped connection) pyftpdlib's threaded accept loop sometimes
# tries to register the already-closed socket and logs an exception in the SERVER thread. That is the injected fault.
pytestmark = pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")


class Ftpd:
    def __init__(self, root, server, handler, port):
        self.root, self.server, self.handler, self.port = root, server, handler, port

    def write(self, rel, data=b"x", mtime=None):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data if isinstance(data, bytes) else data.encode())
        if mtime is not None:
            os.utime(path, (mtime, mtime))
        return path

    def connector(self, **cfg):
        sleeps: list[float] = []
        config = {"host": "127.0.0.1", "port": self.port, "username": "alice", "password_env": "T_FTP_PW",
                  "default_acl": ["*"], "timeout": 10, **cfg}
        return FtpConnector(config, sleep=sleeps.append), sleeps

    @property
    def commands(self):
        return [c for c, _ in self.handler.seen]


def make_server(root, *, mlsd=True, tls=None, anonymous=False):
    authorizer = DummyAuthorizer()
    authorizer.add_user("alice", PASSWORD, str(root), perm="elr")
    if anonymous:
        authorizer.add_anonymous(str(root), perm="elr")
    base = FTPHandler
    if tls:
        from pyftpdlib.handlers import TLS_FTPHandler
        base = TLS_FTPHandler
    protocol = dict(base.proto_cmds)
    if not mlsd:
        protocol.pop("MLSD"), protocol.pop("MLST")

    class Handler(base):
        proto_cmds = protocol
        seen: list = []
        refusals = 0          # answer the next N connections with a real 421 "too many connections" reply
        auth_failed_timeout = 0   # pyftpdlib delays a 530 by 3 s to slow brute force; not needed here
        drops = 0             # hang up on the next N MLSD / LIST commands

        def handle(self):
            if type(self).refusals > 0:
                type(self).refusals -= 1
                self.respond("421 Too many connections. Service temporarily unavailable.")
                return self.close_when_done()
            return super().handle()

        echo_password = False  # answer PASS with a 421 that repeats what the client sent (a careless server)

        def pre_process_command(self, line, cmd, arg):
            type(self).seen.append((cmd, arg))
            if cmd == "PASS" and type(self).echo_password:
                self.respond(f"421 service busy, you sent {arg}")
                return
            if cmd in ("MLSD", "LIST") and type(self).drops > 0:
                type(self).drops -= 1
                self.close()
                return
            return super().pre_process_command(line, cmd, arg)

    Handler.authorizer = authorizer
    Handler.seen = []
    if tls:
        Handler.certfile, Handler.keyfile = tls
        Handler.tls_control_required = Handler.tls_data_required = True
    server = ThreadedFTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"timeout": 0.05, "handle_exit": False}, daemon=True)
    thread.start()
    return Ftpd(root, server, Handler, server.address[1]), thread


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("T_FTP_PW", PASSWORD)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")


@pytest.fixture
def ftpd(tmp_path):
    root = tmp_path / "ftp"
    root.mkdir()
    server, thread = make_server(root)
    yield server
    server.server.close_all()
    thread.join(5)


def scan_all(connector, cursor=None):
    batches = list(itertools.islice(connector.scan(cursor or {}), 500))
    assert len(batches) < 500, "the scan never terminated"
    return [c for b in batches for c in b.changes], batches


def ids(changes):
    return [c.external_id for c in changes]


TREE = {"readme.txt": "r", "docs/a b.txt": "ab", "docs/sub dir/é ü.txt": "u", "docs/sub dir/日本.md": "j",
        "100%.txt": "p", "docs/z.txt": "z", "report-2020.txt": "x", "report/inside.txt": "y"}


# ── listing ──


def test_lists_a_nested_tree_with_mlsd_in_path_order_and_never_sends_a_write_command(ftpd):
    for rel, body in TREE.items():
        ftpd.write(rel, body)
    (ftpd.root / "empty").mkdir()
    c, _ = ftpd.connector(batch_size=3)
    changes, batches = scan_all(c)
    assert ids(changes) == sorted(TREE) and [len(b.changes) for b in batches] == [3, 3, 2]
    f = next(x.file for x in changes if x.external_id == "docs/a b.txt")
    assert (f.path, f.name, f.size, f.mime) == ("docs/a b.txt", "a b.txt", 2, "text/plain")
    assert f.modified_at is not None and f.version.endswith(":2")
    assert batches[-1].done and batches[-1].complete_scope == "" and batches[-1].cursor == {}
    json.dumps([b.cursor for b in batches])
    used = set(ftpd.commands)
    assert ftpd.commands.count("USER") == 1                  # one control connection for the whole listing walk
    assert c.fs._local.ftp is None                           # ... closed when the scan ended
    assert "MLSD" in used and "LIST" not in used
    assert used <= {"USER", "PASS", "TYPE", "PASV", "EPSV", "CWD", "MLSD", "OPTS", "QUIT", "FEAT", "PWD", "SYST"}, used


def test_hidden_and_junk_names_are_skipped(ftpd):
    for rel in ("keep.txt", ".hidden", ".git/x", "Thumbs.db", "~$tmp.docx", "d/ok.txt"):
        ftpd.write(rel)
    c, _ = ftpd.connector()
    assert ids(scan_all(c)[0]) == ["d/ok.txt", "keep.txt"]


def test_anonymous_login_is_the_default_without_credentials(ftpd):
    ftpd.server.handler.authorizer.add_anonymous(str(ftpd.root), perm="elr")
    ftpd.write("pub.txt", "hello")
    c = FtpConnector({"host": "127.0.0.1", "port": ftpd.port, "default_acl": ["*"]})
    changes, _ = scan_all(c)
    assert ids(changes) == ["pub.txt"]
    with c.open(changes[0].file) as fh:
        assert fh.read() == b"hello"


def test_base_path_scopes_the_walk(ftpd):
    ftpd.write("inside/a.txt")
    ftpd.write("outside.txt")
    c, _ = ftpd.connector(base_path="/inside")
    changes, _ = scan_all(c)
    assert ids(changes) == ["a.txt"]
    with c.open(changes[0].file) as fh:
        assert fh.read() == b"x"


def test_listing_falls_back_to_list_and_mdtm_when_the_server_has_no_mlsd(tmp_path):
    root = tmp_path / "ftp"
    root.mkdir()
    server, thread = make_server(root, mlsd=False)
    try:
        for rel, body in TREE.items():
            server.write(rel, body, mtime=1_700_000_000)
        c, _ = server.connector()
        changes, batches = scan_all(c)
        assert ids(changes) == sorted(TREE) and batches[-1].complete_scope == ""
        assert "LIST" in server.commands and "MDTM" in server.commands
        before = {x.external_id: x.file.version for x in changes}
        assert before["docs/z.txt"] == "1700000000000:1"                # exact, from MDTM, not the LIST minute
        server.write("docs/z.txt", "Z", mtime=1_700_000_001)             # same size, one second later
        after = {x.external_id: x.file.version for x in scan_all(c)[0]}
        assert after["docs/z.txt"] != before["docs/z.txt"] and after["readme.txt"] == before["readme.txt"]
        # without MDTM the LIST date (minute resolution for recent files) is all there is
        c2, _ = server.connector(list_fallback_mdtm=False)
        assert all(x.file.modified_at is not None for x in scan_all(c2)[0])
    finally:
        server.server.close_all()
        thread.join(5)


# ── versions, deletes, resume ──


def test_version_changes_when_content_changes(ftpd):
    ftpd.write("a.txt", "hello", mtime=1_700_000_000)
    ftpd.write("b.txt", "stay", mtime=1_700_000_000)
    c, _ = ftpd.connector()
    before = {x.external_id: x.file.version for x in scan_all(c)[0]}
    assert before == {x.external_id: x.file.version for x in scan_all(c)[0]}
    ftpd.write("a.txt", "HELLO", mtime=1_700_000_100)
    after = {x.external_id: x.file.version for x in scan_all(c)[0]}
    assert after["a.txt"] != before["a.txt"] and after["b.txt"] == before["b.txt"]
    ftpd.write("a.txt", "HELLO!", mtime=1_700_000_100)                   # size alone is also a change
    assert {x.external_id: x.file.version for x in scan_all(c)[0]}["a.txt"] not in (before["a.txt"], after["a.txt"])


def test_deleted_files_are_missing_from_an_authoritative_listing(ftpd):
    ftpd.write("keep.txt")
    gone = ftpd.write("sub/gone.txt")
    c, _ = ftpd.connector()
    indexed = set(ids(scan_all(c)[0]))
    gone.unlink()
    changes, batches = scan_all(c)
    assert batches[-1].complete_scope == "" and indexed - set(ids(changes)) == {"sub/gone.txt"}


def test_resume_from_every_batch_boundary_is_exact_and_never_authoritative(ftpd):
    names = ["report/z.txt", "report/a.txt", "report-2020.txt", "report.txt", "a/b/c.txt", "a/b.txt", "a b.txt",
             "a+b.txt", "a.txt", "b/x.txt", "bb.txt", "B.txt", "é.txt", "a,b.txt", "a-b.txt"]
    for rel in names:
        ftpd.write(rel)
    c, _ = ftpd.connector(batch_size=2)
    everything, batches = scan_all(c)
    assert ids(everything) == sorted(names)
    for k in range(len(batches) - 1):
        before = [x for b in batches[: k + 1] for x in ids(b.changes)]
        resumed, rb = scan_all(c, batches[k].cursor)
        assert before + ids(resumed) == ids(everything), f"resume after batch {k}"
        assert rb[-1].complete_scope is None


def test_a_directory_the_account_may_not_list_is_skipped_and_the_scan_stops_being_authoritative(ftpd):
    ftpd.write("a/ok.txt")
    ftpd.write("private/p.txt")
    ftpd.write("z/ok.txt")
    ftpd.server.handler.authorizer.override_perm("alice", str(ftpd.root / "private"), "", recursive=True)
    c, sleeps = ftpd.connector()
    changes, batches = scan_all(c)
    assert ids(changes) == ["a/ok.txt", "z/ok.txt"] and batches[-1].complete_scope is None and sleeps == []
    assert c.stats["unreadable_dir"] == 1
    assert ftpd.commands.count("USER") == 1          # a 550 is a refusal, not a broken connection: no reconnect


def test_missing_base_path_is_a_clear_error_not_an_empty_listing(ftpd):
    c, _ = ftpd.connector(base_path="/does/not/exist")
    with pytest.raises(HttpError, match="base_path"):
        scan_all(c)


# ── permissions ──


def test_acl_rules_longest_prefix_and_fail_closed(ftpd):
    for rel in ("finance/q1.xlsx", "finance/public/notice.pdf", "hr/pay.csv", "readme.txt"):
        ftpd.write(rel)
    rules = [{"prefix": "finance", "principals": ["group:Finance"]}, {"prefix": "finance/public", "principals": ["*"]}]
    c, _ = ftpd.connector(default_acl=None, acl_rules=rules)
    by = {x.external_id: x.file for x in scan_all(c)[0]}
    assert by["finance/q1.xlsx"].acl == ["group:finance"] and by["finance/public/notice.pdf"].acl == ["*"]
    assert by["hr/pay.csv"].acl == [] and by["readme.txt"].acl == []
    assert c.refresh_acl(by["finance/q1.xlsx"]) == ["group:finance"] and c.refresh_acl(by["hr/pay.csv"]) == []


# ── bytes ──


def test_open_returns_exact_bytes(ftpd):
    payloads = {"a b.txt": b"hello\n", "dir/100% #1.bin": bytes(range(256)) * 5 + b"\x00", "é/日本.bin": os.urandom(2_600_000)}
    for rel, data in payloads.items():
        ftpd.write(rel, data)
    c, _ = ftpd.connector()
    for change in scan_all(c)[0]:
        with c.open(change.file) as fh:
            assert fh.read() == payloads[change.external_id]
    with c.open(SourceFile("a b.txt", "a b.txt", "a b.txt", 6, None, "v", [])) as fh:
        assert fh.read() == b"hello\n"


def test_open_of_a_missing_file_is_a_clear_error_without_retrying(ftpd):
    c, sleeps = ftpd.connector()
    with pytest.raises(HttpError) as err:
        c.open(SourceFile("gone.txt", "gone.txt", "gone.txt", 1, None, "v", []))
    assert err.value.status == 550 and sleeps == []


def test_a_name_with_a_newline_cannot_be_used_to_smuggle_a_command(ftpd):
    c, _ = ftpd.connector()
    with pytest.raises(HttpError):
        c.open(SourceFile("x.txt\r\nDELE important.txt", "x", "x", 1, None, "v", []))
    assert "DELE" not in ftpd.commands


def test_mlsd_entries_that_are_not_plain_files_or_directories_are_ignored():
    from app.scale.connectors.ftp import FtpFS

    class Fake:
        def mlsd(self, facts):
            yield ".", {"type": "cdir"}
            yield "..", {"type": "pdir"}
            yield "link", {"type": "OS.unix=slink:/etc"}
            yield "dev", {"type": "OS.unix=chr"}
            yield "real.txt", {"type": "file", "size": "5", "modify": "20231114221320"}
            yield "sub", {"type": "dir", "modify": "20231114221320"}
            yield "nosize", {"type": "file"}

    entries = FtpFS._mlsd(Fake())
    assert [(e.name, e.is_dir, e.size) for e in entries] == [("real.txt", False, 5), ("sub", True, 0), ("nosize", False, 0)]


def test_oversize_files_are_skipped_in_the_scan_and_refused_by_open(ftpd):
    ftpd.write("small.txt", b"s" * 100)
    ftpd.write("big.bin", b"b" * 5000)
    c, _ = ftpd.connector(max_file_bytes=1000)
    changes, batches = scan_all(c)
    assert ids(changes) == ["small.txt"] and batches[-1].done and c.stats["oversize"] == 1
    with pytest.raises(TooLarge):
        c.open(SourceFile("big.bin", "big.bin", "big.bin", 100, None, "v", []))


# ── rate limits, drops, auth ──


def test_a_421_too_many_connections_reply_is_retried_with_backoff(ftpd):
    ftpd.write("a.txt", b"payload")
    ftpd.handler.refusals = 2
    c, sleeps = ftpd.connector()
    changes, _ = scan_all(c)
    assert ids(changes) == ["a.txt"] and sleeps == [1, 2]
    ftpd.handler.refusals = 1
    with c.open(changes[0].file) as fh:
        assert fh.read() == b"payload"
    assert sleeps == [1, 2, 1]


def test_persistent_421_aborts_the_scan_instead_of_looking_like_an_empty_server(ftpd):
    ftpd.write("a.txt")
    ftpd.handler.refusals = 99
    c, sleeps = ftpd.connector(max_retries=3)
    with pytest.raises(HttpError) as err:
        scan_all(c)
    assert err.value.status == 421 and not isinstance(err.value, AuthError) and sleeps == [1, 2, 4]


def test_a_connection_dropped_mid_listing_reconnects_and_completes(ftpd):
    ftpd.write("d/a.txt")
    ftpd.write("top.txt")
    ftpd.handler.drops = 2
    c, sleeps = ftpd.connector()
    changes, batches = scan_all(c)
    assert ids(changes) == ["d/a.txt", "top.txt"] and batches[-1].complete_scope == "" and len(sleeps) == 2


def test_wrong_password_is_an_auth_error_that_does_not_leak_it(ftpd, monkeypatch):
    monkeypatch.setenv("T_FTP_PW", "WRONG-" + PASSWORD)
    c, sleeps = ftpd.connector()
    with pytest.raises(AuthError) as err:
        scan_all(c)
    blob = str(err.value) + repr(err.value)
    assert "530" in blob and PASSWORD not in blob and "WRONG-" not in blob and sleeps == []


def test_a_server_that_echoes_the_password_in_an_error_reply_does_not_leak_it(ftpd):
    ftpd.handler.echo_password = True
    c, _ = ftpd.connector(max_retries=1)
    with pytest.raises(HttpError) as err:
        scan_all(c)
    assert "service busy" in str(err.value) and PASSWORD not in str(err.value) + repr(err.value)


def test_abandoned_scan_closes_its_listing_connection(ftpd):
    for i in range(4):
        ftpd.write(f"d{i}/f.txt")
    c, _ = ftpd.connector(batch_size=1)
    stream = c.scan({})
    next(stream)
    assert c.fs._local.ftp is not None
    stream.close()
    assert c.fs._local.ftp is None


def test_unset_password_variable_is_named_and_no_connection_is_made(ftpd, monkeypatch):
    monkeypatch.delenv("T_FTP_PW")
    c, _ = ftpd.connector()
    with pytest.raises(HttpError) as err:
        scan_all(c)
    assert "T_FTP_PW" in str(err.value) and ftpd.commands == []


def test_unreachable_server_is_retried_then_reported():
    c = FtpConnector({"host": "127.0.0.1", "port": 1, "username": "alice", "password_env": "T_FTP_PW", "max_retries": 2},
                     sleep=(sleeps := []).append)
    with pytest.raises(HttpError) as err:
        scan_all(c)
    assert PASSWORD not in str(err.value) and sleeps == [1, 2]


def test_secrets_never_reach_cursors_or_source_files(ftpd):
    ftpd.write("a.txt")
    ftpd.write("b.txt")
    c, _ = ftpd.connector(batch_size=1)
    changes, batches = scan_all(c)
    assert PASSWORD not in repr(changes) + repr([b.cursor for b in batches]) + json.dumps(c.cfg)


# ── FTPS ──


@pytest.fixture
def cert(tmp_path):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    import ipaddress

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = dt.datetime.now(dt.timezone.utc)
    certificate = (
        x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number()).not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=2))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), True)
        .sign(key, hashes.SHA256())
    )
    pem = tmp_path / "cert.pem"
    keyfile = tmp_path / "key.pem"
    pem.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    keyfile.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
                                          serialization.NoEncryption()))
    return str(pem), str(keyfile)


@pytest.fixture
def ftps(tmp_path, cert):
    root = tmp_path / "ftps"
    root.mkdir()
    server, thread = make_server(root, tls=cert)
    server.cert = cert[0]
    yield server
    server.server.close_all()
    thread.join(5)


def test_explicit_ftps_lists_and_downloads_when_the_certificate_is_trusted(ftps):
    ftps.write("secure/a.txt", b"over tls")
    ftps.write("b.bin", os.urandom(300_000))
    c, _ = ftps.connector(tls="explicit", ca_bundle=ftps.cert)
    changes, batches = scan_all(c)
    assert ids(changes) == ["b.bin", "secure/a.txt"] and batches[-1].complete_scope == ""
    with c.open(next(x.file for x in changes if x.external_id == "secure/a.txt")) as fh:
        assert fh.read() == b"over tls"
    assert "AUTH" in ftps.commands and "PROT" in ftps.commands


def test_ftps_with_an_untrusted_certificate_fails_loudly_and_is_not_retried(ftps):
    ftps.write("a.txt")
    c, sleeps = ftps.connector(tls="explicit")           # system trust store does not know the test certificate
    with pytest.raises(HttpError, match="certificate verification failed"):
        scan_all(c)
    assert sleeps == []
    insecure, _ = ftps.connector(tls="explicit", verify_tls=False)       # explicit opt-out
    assert ids(scan_all(insecure)[0]) == ["a.txt"]


def test_plain_ftp_client_cannot_talk_to_a_tls_only_server(ftps):
    c, _ = ftps.connector(max_retries=0)                  # tls: none against a server that requires TLS
    with pytest.raises((HttpError, AuthError)):
        scan_all(c)


# ── parsers ──


def test_mlsd_time_parsing():
    assert parse_mlsd_time("20231114221320") == 1_700_000_000
    assert parse_mlsd_time("20231114221320.250") == 1_700_000_000.25
    assert parse_mlsd_time("garbage") is None and parse_mlsd_time(None) is None and parse_mlsd_time("20231340000000") is None


NOW = dt.datetime(2026, 10, 10, 12, 0, tzinfo=dt.timezone.utc)


@pytest.mark.parametrize("line,expected", [
    ("-rw-r--r--   1 alice    staff        1234 Oct  9 08:15 report final.pdf", ("report final.pdf", False, 1234)),
    ("drwxr-xr-x   2 alice    staff        4096 Jan 01  2024 old dir", ("old dir", True, 0)),
    ("-rw-r--r--   1 ftp      ftp             7 Dec 31 23:59 future-looking.txt", ("future-looking.txt", False, 7)),
    ("-rw-r--r--   1 owner 99 Mar  3  2020 no group column.txt", ("no group column.txt", False, 99)),
    ("-rw-r--r--@  1 alice    staff          10 Oct  9 08:15 mac-xattr.txt", ("mac-xattr.txt", False, 10)),
    ("10-09-26  08:15AM       <DIR>          Archive 2026", ("Archive 2026", True, 0)),
    ("10-09-26  08:15PM                 1234 Quarterly Report.docx", ("Quarterly Report.docx", False, 1234)),
    ("12-31-1999  12:00AM                  5 y2k.txt", ("y2k.txt", False, 5)),
])
def test_list_line_parsing(line, expected):
    entry = parse_list_line(line, NOW)
    assert (entry.name, entry.is_dir, entry.size) == expected and entry.mtime is not None


@pytest.mark.parametrize("line", [
    "total 48", "", "lrwxrwxrwx   1 root root 11 Oct  9 08:15 link -> target",
    "drwxr-xr-x   2 alice staff 4096 Oct  9 08:15 .", "drwxr-xr-x   2 alice staff 4096 Oct  9 08:15 ..",
    "crw-rw-rw-   1 root root 1, 3 Oct  9 08:15 null", "garbage line without structure",
])
def test_list_lines_that_must_be_ignored(line):
    assert parse_list_line(line, NOW) is None


def test_list_year_inference_never_invents_a_future_date():
    recent = parse_list_line("-rw-r--r-- 1 a b 1 Oct  9 08:15 x", NOW)
    december = parse_list_line("-rw-r--r-- 1 a b 1 Dec 31 23:59 x", NOW)
    assert dt.datetime.fromtimestamp(recent.mtime, dt.timezone.utc).year == 2026
    assert dt.datetime.fromtimestamp(december.mtime, dt.timezone.utc).year == 2025       # "Dec 31 23:59" in October = last year
