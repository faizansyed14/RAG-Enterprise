"""SFTP connector tests.

They run against a REAL SSH + SFTP server running inside the test process: paramiko's server API (ServerInterface +
SFTPServerInterface) on a local TCP port, so every test performs a genuine SSH key exchange, host key check,
password / public key authentication and SFTP protocol exchange. The server is read-only and records every
operation it is asked to do, which lets a test prove the connector never writes. Faults are injected in the server:
hang up on new connections (what OpenSSH's MaxStartups does), close the transport in the middle of a READDIR,
answer READDIR with "permission denied".
"""

import io
import itertools
import json
import os
import socket
import threading

import paramiko
import pytest

from app.scale.connectors._http import AuthError, HttpError, TooLarge
from app.scale.connectors.base import SourceFile
from app.scale.connectors.sftp import HostKeyError, SftpConnector, fingerprint_sha256

PASSWORD = "sftp-pw-MUST-NOT-LEAK-42"
PASSPHRASE = "key-passphrase-MUST-NOT-LEAK-7"


class SshServer(paramiko.ServerInterface):
    def __init__(self, owner):
        self.owner, self.transport = owner, None

    def check_channel_request(self, kind, chanid):
        return paramiko.OPEN_SUCCEEDED if kind == "session" else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

    def get_allowed_auths(self, username):
        return "password,publickey"

    def check_auth_password(self, username, password):
        self.owner.auth_attempts.append(("password", username))
        ok = username == "alice" and password == PASSWORD
        return paramiko.AUTH_SUCCESSFUL if ok else paramiko.AUTH_FAILED

    def check_auth_publickey(self, username, key):
        self.owner.auth_attempts.append(("publickey", username))
        ok = username == "alice" and key.asbytes() == self.owner.authorized_key.asbytes()
        return paramiko.AUTH_SUCCESSFUL if ok else paramiko.AUTH_FAILED


class Handle(paramiko.SFTPHandle):
    def stat(self):
        try:
            return paramiko.SFTPAttributes.from_stat(os.fstat(self.readfile.fileno()))
        except OSError as exc:
            return paramiko.SFTPServer.convert_errno(exc.errno)


class Sftpd:
    """A read-only SFTP server over a directory. One instance per test."""

    def __init__(self, root, host_key, authorized_key):
        self.root, self.host_key, self.authorized_key = root, host_key, authorized_key
        self.ops: list[tuple[str, str]] = []
        self.auth_attempts: list[tuple[str, str]] = []
        self.denied: set[str] = set()          # directories whose READDIR answers "permission denied"
        self.missing: set[str] = set()         # directories whose READDIR answers "no such file" (they vanished)
        self.failing: set[str] = set()         # directories whose READDIR answers the generic SFTP "failure" status
        self.drop_connections = 0              # hang up on the next N TCP connections before the SSH banner
        self.drop_in_readdir = 0               # close the transport in the middle of the next N READDIRs
        self.transports: list[paramiko.Transport] = []
        self._stop = False
        self.listener = socket.socket()
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(20)
        self.port = self.listener.getsockname()[1]
        owner = self

        class Iface(paramiko.SFTPServerInterface):
            def __init__(self, server, *a, **k):
                super().__init__(server, *a, **k)
                self.server = server

            def _real(self, path):
                return str(owner.root) + self.canonicalize(path).rstrip("/")

            def list_folder(self, path):
                owner.ops.append(("list", self.canonicalize(path)))
                if self.canonicalize(path).strip("/") in owner.denied:
                    return paramiko.SFTP_PERMISSION_DENIED
                if self.canonicalize(path).strip("/") in owner.missing:
                    return paramiko.SFTP_NO_SUCH_FILE
                if self.canonicalize(path).strip("/") in owner.failing:
                    return paramiko.SFTP_FAILURE
                if owner.drop_in_readdir > 0:
                    owner.drop_in_readdir -= 1
                    self.server.transport.close()
                    return paramiko.SFTP_FAILURE
                try:
                    out = []
                    for name in os.listdir(self._real(path)):
                        attr = paramiko.SFTPAttributes.from_stat(os.lstat(os.path.join(self._real(path), name)))
                        attr.filename = name
                        out.append(attr)
                    return out
                except OSError as exc:
                    return paramiko.SFTPServer.convert_errno(exc.errno)

            def stat(self, path):
                owner.ops.append(("stat", self.canonicalize(path)))
                try:
                    return paramiko.SFTPAttributes.from_stat(os.stat(self._real(path)))
                except OSError as exc:
                    return paramiko.SFTPServer.convert_errno(exc.errno)

            def lstat(self, path):
                owner.ops.append(("lstat", self.canonicalize(path)))
                try:
                    return paramiko.SFTPAttributes.from_stat(os.lstat(self._real(path)))
                except OSError as exc:
                    return paramiko.SFTPServer.convert_errno(exc.errno)

            def open(self, path, flags, attr):
                if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND):
                    owner.ops.append(("WRITE-OPEN", self.canonicalize(path)))
                    return paramiko.SFTP_PERMISSION_DENIED
                owner.ops.append(("read-open", self.canonicalize(path)))
                try:
                    fd = os.open(self._real(path), os.O_RDONLY)
                except OSError as exc:
                    return paramiko.SFTPServer.convert_errno(exc.errno)
                handle = Handle(flags)
                handle.filename = self._real(path)
                handle.readfile = os.fdopen(fd, "rb")
                return handle

            def remove(self, path):
                owner.ops.append(("WRITE-REMOVE", path))
                return paramiko.SFTP_PERMISSION_DENIED

            def rename(self, oldpath, newpath):
                owner.ops.append(("WRITE-RENAME", oldpath))
                return paramiko.SFTP_PERMISSION_DENIED

            def mkdir(self, path, attr):
                owner.ops.append(("WRITE-MKDIR", path))
                return paramiko.SFTP_PERMISSION_DENIED

        self.iface = Iface
        self.thread = threading.Thread(target=self._accept, daemon=True)
        self.thread.start()

    def _accept(self):
        while not self._stop:
            try:
                conn, _ = self.listener.accept()
            except OSError:
                return
            if self.drop_connections > 0:
                self.drop_connections -= 1
                conn.close()
                continue
            transport = paramiko.Transport(conn)
            transport.add_server_key(self.host_key)
            transport.set_subsystem_handler("sftp", paramiko.SFTPServer, self.iface)
            server = SshServer(self)
            server.transport = transport
            self.transports.append(transport)
            transport.start_server(event=threading.Event(), server=server)     # do not block the accept loop

    def stop(self):
        self._stop = True
        self.listener.close()
        for t in self.transports:
            t.close()

    # helpers for tests
    def write(self, rel, data=b"x", mtime=None):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data if isinstance(data, bytes) else data.encode())
        if mtime is not None:
            os.utime(path, (mtime, mtime))
        return path

    def connector(self, **cfg):
        sleeps: list[float] = []
        config = {"host": "127.0.0.1", "port": self.port, "username": "alice", "password_env": "T_SFTP_PW",
                  "insecure_skip_host_key_check": True, "default_acl": ["*"], "timeout": 10, **cfg}
        return SftpConnector(config, sleep=sleeps.append), sleeps

    @property
    def op_names(self):
        return [op for op, _ in self.ops]


@pytest.fixture(scope="module")
def host_key():
    return paramiko.ECDSAKey.generate()


@pytest.fixture(scope="module")
def client_key():
    return paramiko.ECDSAKey.generate()


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("T_SFTP_PW", PASSWORD)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")


@pytest.fixture
def sftpd(tmp_path, host_key, client_key):
    root = tmp_path / "sftp"
    root.mkdir()
    server = Sftpd(root, host_key, client_key)
    yield server
    server.stop()


def scan_all(connector, cursor=None):
    batches = list(itertools.islice(connector.scan(cursor or {}), 500))
    assert len(batches) < 500, "the scan never terminated"
    return [c for b in batches for c in b.changes], batches


def ids(changes):
    return [c.external_id for c in changes]


TREE = {"readme.txt": "r", "docs/a b.txt": "ab", "docs/sub dir/é ü.txt": "u", "docs/sub dir/日本.md": "j",
        "100%.txt": "p", "docs/z.txt": "z", "report-2020.txt": "x", "report/inside.txt": "y"}


# ── listing ──


def test_lists_a_nested_tree_in_path_order_over_one_connection_and_never_writes(sftpd):
    for rel, body in TREE.items():
        sftpd.write(rel, body, mtime=1_700_000_000)
    (sftpd.root / "empty").mkdir()
    c, _ = sftpd.connector(batch_size=3)
    changes, batches = scan_all(c)
    assert ids(changes) == sorted(TREE) and [len(b.changes) for b in batches] == [3, 3, 2]
    f = next(x.file for x in changes if x.external_id == "docs/a b.txt")
    assert (f.path, f.name, f.size, f.mime, f.version) == ("docs/a b.txt", "a b.txt", 2, "text/plain", "1700000000000:2")
    assert f.modified_at.timestamp() == 1_700_000_000
    assert batches[-1].done and batches[-1].complete_scope == "" and batches[-1].cursor == {}
    json.dumps([b.cursor for b in batches])
    assert len(sftpd.auth_attempts) == 1                    # one SSH session for the whole walk
    assert c.fs._local.session is None                      # ... closed when the scan ended
    assert not [op for op in sftpd.op_names if op.startswith("WRITE")]


def test_hidden_and_junk_names_and_symlinks_are_skipped(sftpd, tmp_path):
    for rel in ("keep.txt", ".hidden", ".git/x", "Thumbs.db", "~$tmp.docx", "d/ok.txt"):
        sftpd.write(rel)
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    os.symlink(outside, sftpd.root / "link-to-file.txt")
    os.symlink(sftpd.root, sftpd.root / "loop")             # a directory loop
    c, _ = sftpd.connector()
    assert ids(scan_all(c)[0]) == ["d/ok.txt", "keep.txt"]


def test_base_path_scopes_the_walk(sftpd):
    sftpd.write("inside/a.txt", "A")
    sftpd.write("outside.txt")
    c, _ = sftpd.connector(base_path="/inside")
    changes, _ = scan_all(c)
    assert ids(changes) == ["a.txt"]
    with c.open(changes[0].file) as fh:
        assert fh.read() == b"A"
    default, _ = sftpd.connector()                          # "." = the login directory (here the server root)
    assert "inside/a.txt" in ids(scan_all(default)[0])


# ── versions, deletes, resume ──


def test_version_changes_when_content_changes(sftpd):
    sftpd.write("a.txt", "hello", mtime=1_700_000_000)
    sftpd.write("b.txt", "stay", mtime=1_700_000_000)
    c, _ = sftpd.connector()
    before = {x.external_id: x.file.version for x in scan_all(c)[0]}
    assert before == {x.external_id: x.file.version for x in scan_all(c)[0]}
    sftpd.write("a.txt", "HELLO", mtime=1_700_000_100)
    after = {x.external_id: x.file.version for x in scan_all(c)[0]}
    assert after["a.txt"] != before["a.txt"] and after["b.txt"] == before["b.txt"]
    sftpd.write("a.txt", "HELLO!", mtime=1_700_000_100)
    assert {x.external_id: x.file.version for x in scan_all(c)[0]}["a.txt"] not in (before["a.txt"], after["a.txt"])


def test_deleted_files_are_missing_from_an_authoritative_listing(sftpd):
    sftpd.write("keep.txt")
    gone = sftpd.write("sub/gone.txt")
    c, _ = sftpd.connector()
    indexed = set(ids(scan_all(c)[0]))
    gone.unlink()
    changes, batches = scan_all(c)
    assert batches[-1].complete_scope == "" and indexed - set(ids(changes)) == {"sub/gone.txt"}


def test_resume_from_every_batch_boundary_is_exact_and_never_authoritative(sftpd):
    names = ["report/z.txt", "report/a.txt", "report-2020.txt", "report.txt", "a/b/c.txt", "a/b.txt", "a b.txt",
             "a+b.txt", "a.txt", "b/x.txt", "bb.txt", "B.txt", "é.txt", "a,b.txt", "a-b.txt"]
    for rel in names:
        sftpd.write(rel)
    c, _ = sftpd.connector(batch_size=2)
    everything, batches = scan_all(c)
    assert ids(everything) == sorted(names)
    for k in range(len(batches) - 1):
        before = [x for b in batches[: k + 1] for x in ids(b.changes)]
        resumed, rb = scan_all(c, batches[k].cursor)
        assert before + ids(resumed) == ids(everything), f"resume after batch {k}"
        assert rb[-1].complete_scope is None


def test_a_directory_the_account_may_not_list_is_skipped_and_the_scan_stops_being_authoritative(sftpd):
    sftpd.write("a/ok.txt")
    sftpd.write("private/p.txt")
    sftpd.write("z/ok.txt")
    sftpd.denied.add("private")
    c, sleeps = sftpd.connector()
    changes, batches = scan_all(c)
    assert ids(changes) == ["a/ok.txt", "z/ok.txt"] and batches[-1].complete_scope is None and sleeps == []
    assert c.stats["unreadable_dir"] == 1 and len(sftpd.auth_attempts) == 1   # a refusal does not reconnect


def test_a_directory_that_vanishes_between_listing_and_descending_counts_as_empty(sftpd):
    sftpd.write("ok.txt")
    sftpd.write("vanishing/v.txt")
    sftpd.missing.add("vanishing")
    c, _ = sftpd.connector()
    changes, batches = scan_all(c)
    assert ids(changes) == ["ok.txt"] and batches[-1].complete_scope == ""      # its files really are gone


def test_a_server_side_failure_status_is_a_permanent_error_and_is_not_retried(sftpd):
    sftpd.write("bad/x.txt")
    sftpd.failing.add("bad")
    c, sleeps = sftpd.connector(base_path="/bad")
    with pytest.raises(HttpError) as err:
        scan_all(c)
    assert sleeps == [] and not isinstance(err.value, AuthError)


def test_missing_base_path_is_a_clear_error_not_an_empty_listing(sftpd):
    c, _ = sftpd.connector(base_path="/does/not/exist")
    with pytest.raises(HttpError, match="does not exist"):
        scan_all(c)


# ── permissions ──


def test_acl_rules_longest_prefix_and_fail_closed(sftpd):
    for rel in ("finance/q1.xlsx", "finance/public/notice.pdf", "hr/pay.csv", "readme.txt"):
        sftpd.write(rel)
    rules = [{"prefix": "finance", "principals": ["group:Finance"]}, {"prefix": "finance/public", "principals": ["*"]}]
    c, _ = sftpd.connector(default_acl=None, acl_rules=rules)
    by = {x.external_id: x.file for x in scan_all(c)[0]}
    assert by["finance/q1.xlsx"].acl == ["group:finance"] and by["finance/public/notice.pdf"].acl == ["*"]
    assert by["hr/pay.csv"].acl == [] and by["readme.txt"].acl == []
    assert c.refresh_acl(by["finance/q1.xlsx"]) == ["group:finance"] and c.refresh_acl(by["hr/pay.csv"]) == []


# ── bytes ──


def test_open_returns_exact_bytes(sftpd):
    payloads = {"a b.txt": b"hello\n", "dir/100% #1.bin": bytes(range(256)) * 5 + b"\x00", "é/日本.bin": os.urandom(3_100_000)}
    for rel, data in payloads.items():
        sftpd.write(rel, data)
    c, _ = sftpd.connector()
    for change in scan_all(c)[0]:
        with c.open(change.file) as fh:
            assert fh.read() == payloads[change.external_id]
    with c.open(SourceFile("a b.txt", "a b.txt", "a b.txt", 6, None, "v", [])) as fh:
        assert fh.read() == b"hello\n"
    assert not [op for op in sftpd.op_names if op.startswith("WRITE")]


def test_open_of_a_missing_file_is_a_clear_error_without_retrying(sftpd):
    c, sleeps = sftpd.connector()
    with pytest.raises(HttpError) as err:
        c.open(SourceFile("gone.txt", "gone.txt", "gone.txt", 1, None, "v", []))
    assert err.value.status == 404 and sleeps == []


def test_oversize_files_are_skipped_in_the_scan_and_refused_by_open(sftpd):
    sftpd.write("small.txt", b"s" * 100)
    sftpd.write("big.bin", b"b" * 5000)
    c, _ = sftpd.connector(max_file_bytes=1000)
    changes, batches = scan_all(c)
    assert ids(changes) == ["small.txt"] and batches[-1].done and c.stats["oversize"] == 1
    with pytest.raises(TooLarge):
        c.open(SourceFile("big.bin", "big.bin", "big.bin", 100, None, "v", []))
    assert ("read-open", "/big.bin") not in sftpd.ops              # refused from its size, nothing was downloaded


# ── transient faults ──


def test_connections_the_server_hangs_up_on_are_retried_with_backoff(sftpd):
    sftpd.write("a.txt", b"payload")
    sftpd.drop_connections = 2
    c, sleeps = sftpd.connector()
    changes, _ = scan_all(c)
    assert ids(changes) == ["a.txt"] and sleeps == [1, 2]
    sftpd.drop_connections = 1
    with c.open(changes[0].file) as fh:
        assert fh.read() == b"payload"
    assert sleeps == [1, 2, 1]


def test_persistent_failure_aborts_the_scan_instead_of_looking_like_an_empty_server(sftpd):
    sftpd.write("a.txt")
    sftpd.drop_connections = 99
    c, sleeps = sftpd.connector(max_retries=3)
    with pytest.raises(HttpError) as err:
        scan_all(c)
    assert not isinstance(err.value, AuthError) and sleeps == [1, 2, 4]


def test_a_transport_closed_in_the_middle_of_a_listing_reconnects_and_completes(sftpd):
    sftpd.write("d/a.txt")
    sftpd.write("top.txt")
    sftpd.drop_in_readdir = 2
    c, sleeps = sftpd.connector()
    changes, batches = scan_all(c)
    assert ids(changes) == ["d/a.txt", "top.txt"] and batches[-1].complete_scope == "" and len(sleeps) == 2


# ── authentication and host keys ──


def test_wrong_password_is_an_auth_error_that_does_not_leak_it(sftpd, monkeypatch):
    monkeypatch.setenv("T_SFTP_PW", "WRONG-" + PASSWORD)
    c, sleeps = sftpd.connector()
    with pytest.raises(AuthError) as err:
        scan_all(c)
    blob = str(err.value) + repr(err.value)
    assert "rejected" in blob and PASSWORD not in blob and "WRONG-" not in blob and sleeps == []


def test_unset_password_variable_is_named_and_no_connection_is_made(sftpd, monkeypatch):
    monkeypatch.delenv("T_SFTP_PW")
    c, _ = sftpd.connector()
    with pytest.raises(HttpError) as err:
        scan_all(c)
    assert "T_SFTP_PW" in str(err.value) and sftpd.auth_attempts == []


def test_no_credentials_configured_is_a_clear_error(sftpd):
    c = SftpConnector({"host": "127.0.0.1", "port": sftpd.port, "username": "alice", "insecure_skip_host_key_check": True})
    with pytest.raises(AuthError, match="needs password_env"):
        scan_all(c)


def test_public_key_authentication_from_a_key_file_and_from_the_environment(sftpd, client_key, tmp_path, monkeypatch):
    sftpd.write("a.txt", b"via key")
    keyfile = tmp_path / "id_ecdsa"
    client_key.write_private_key_file(str(keyfile), password=PASSPHRASE)
    monkeypatch.setenv("T_SFTP_PASSPHRASE", PASSPHRASE)
    for cfg in ({"private_key_path": str(keyfile), "passphrase_env": "T_SFTP_PASSPHRASE"},):
        c, _ = sftpd.connector(password_env=None, **cfg)
        changes, _ = scan_all(c)
        assert ids(changes) == ["a.txt"]
        with c.open(changes[0].file) as fh:
            assert fh.read() == b"via key"
    pem = io.StringIO()
    client_key.write_private_key(pem)                      # unencrypted PEM text held in an environment variable
    monkeypatch.setenv("T_SFTP_KEY", pem.getvalue())
    c, _ = sftpd.connector(password_env=None, private_key_env="T_SFTP_KEY")
    assert ids(scan_all(c)[0]) == ["a.txt"]
    assert ("publickey", "alice") in sftpd.auth_attempts and ("password", "alice") not in sftpd.auth_attempts


def test_wrong_key_passphrase_is_an_auth_error_that_does_not_leak_it(sftpd, client_key, tmp_path, monkeypatch):
    keyfile = tmp_path / "id_ecdsa"
    client_key.write_private_key_file(str(keyfile), password=PASSPHRASE)
    monkeypatch.setenv("T_SFTP_PASSPHRASE", "WRONG-" + PASSPHRASE)
    c, _ = sftpd.connector(password_env=None, private_key_path=str(keyfile), passphrase_env="T_SFTP_PASSPHRASE")
    with pytest.raises(AuthError) as err:
        scan_all(c)
    assert PASSPHRASE not in str(err.value) + repr(err.value)


def test_a_key_the_server_does_not_know_is_rejected(sftpd, tmp_path):
    other = paramiko.ECDSAKey.generate()
    keyfile = tmp_path / "other"
    other.write_private_key_file(str(keyfile))
    c, sleeps = sftpd.connector(password_env=None, private_key_path=str(keyfile))
    with pytest.raises(AuthError):
        scan_all(c)
    assert sleeps == []


def test_unknown_host_key_is_refused_by_default_and_never_retried(sftpd):
    sftpd.write("a.txt")
    c, sleeps = sftpd.connector(insecure_skip_host_key_check=False, known_hosts_file=None)
    with pytest.raises(HostKeyError, match="known_hosts_file or host_key_sha256"):
        scan_all(c)
    assert sleeps == [] and sftpd.auth_attempts == []         # no credential is offered to an unverified server


def test_known_hosts_file_and_pinned_fingerprint_are_accepted_and_mismatches_refused(sftpd, host_key, tmp_path):
    sftpd.write("a.txt")
    good = paramiko.HostKeys()
    good.add(f"[127.0.0.1]:{sftpd.port}", host_key.get_name(), host_key)
    good_file = tmp_path / "known_hosts"
    good.save(str(good_file))
    c, _ = sftpd.connector(insecure_skip_host_key_check=False, known_hosts_file=str(good_file))
    assert ids(scan_all(c)[0]) == ["a.txt"]

    other = paramiko.ECDSAKey.generate()
    bad = paramiko.HostKeys()
    bad.add(f"[127.0.0.1]:{sftpd.port}", other.get_name(), other)
    bad_file = tmp_path / "known_hosts_bad"
    bad.save(str(bad_file))
    sftpd.auth_attempts.clear()
    c, sleeps = sftpd.connector(insecure_skip_host_key_check=False, known_hosts_file=str(bad_file))
    with pytest.raises(HostKeyError, match="does not match"):
        scan_all(c)
    assert sleeps == [] and sftpd.auth_attempts == []

    c, _ = sftpd.connector(insecure_skip_host_key_check=False, host_key_sha256=fingerprint_sha256(host_key))
    assert ids(scan_all(c)[0]) == ["a.txt"]
    c, sleeps = sftpd.connector(insecure_skip_host_key_check=False, host_key_sha256=fingerprint_sha256(other))
    with pytest.raises(HostKeyError, match="host_key_sha256"):
        scan_all(c)
    assert sleeps == []


def test_unreachable_server_is_retried_then_reported():
    sleeps: list[float] = []
    c = SftpConnector({"host": "127.0.0.1", "port": 1, "username": "alice", "password_env": "T_SFTP_PW",
                       "insecure_skip_host_key_check": True, "max_retries": 2, "timeout": 5}, sleep=sleeps.append)
    with pytest.raises(HttpError) as err:
        scan_all(c)
    assert PASSWORD not in str(err.value) and sleeps == [1, 2]


def test_secrets_never_reach_cursors_or_source_files(sftpd):
    sftpd.write("a.txt")
    sftpd.write("b.txt")
    c, _ = sftpd.connector(batch_size=1)
    changes, batches = scan_all(c)
    assert PASSWORD not in repr(changes) + repr([b.cursor for b in batches]) + json.dumps(c.cfg)
