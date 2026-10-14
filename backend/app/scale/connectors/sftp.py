"""SFTP (SSH File Transfer Protocol) through `paramiko` (pip install "paramiko>=3.2").

Protocol operations used (the SFTP subsystem of an SSH server, draft-ietf-secsh-filexfer-02 as implemented by OpenSSH
and paramiko): SSH key exchange + host key check + password or public key authentication, then OPENDIR / READDIR
(`listdir_attr`), and OPEN / READ for the bytes (`getfo`, with read-ahead). Nothing that writes, renames or deletes
is ever called.

Behaviour
  * Crawl style on the shared engine in webdav.py (`NetCrawlConnector`): one total walk order, cursor
    {"after": <last path>}, complete_scope="" for a scan from the start to the end, None for a resumed scan or one
    that had to skip a directory the account may not read (SFTP "permission denied"). A directory listing that
    fails for any other reason aborts the scan; it is never mistaken for an empty directory.
  * version = "<mtime>:<size>" (SFTP gives mtime in whole seconds, so an edit that keeps the size AND happens inside
    the same second as the previous version is not noticed). `external_id` = `path` = path below `base_path`.
  * Symbolic links are not followed (their attributes are not S_ISREG / S_ISDIR), so a link cannot lead out of the
    configured tree or into a loop.
  * Retries: a dropped connection, timeout, "Error reading SSH protocol banner" (what OpenSSH does when MaxStartups
    is hit) and other transport failures reconnect and retry with bounded exponential backoff; SFTP has no rate-limit
    status or Retry-After. A rejected login raises `AuthError` with no credential in the message; a host key that is
    unknown or does not match is a hard error that is never retried.
  * Host keys are verified. Pick one: `known_hosts_file` (OpenSSH format; the system known_hosts is read too), or
    `host_key_sha256` (the "SHA256:..." fingerprint printed by `ssh-keygen -lf`; when set it is the only trust anchor
    and known_hosts is not consulted), or, for a lab only, `insecure_skip_host_key_check: true` (trust whatever the
    server presents: no protection against a man in the middle). With none of them the connection is refused.
  * One connection is kept for the listing walk of a scan (per thread) and closed when the scan ends; each `open()`
    uses its own connection, so workers can read files concurrently.

Config: host (required), port (22), username (required), password_env, or private_key_path / private_key_env (the PEM
text itself, from the named environment variable) with optional passphrase_env, base_path (default "." = the login
directory; absolute paths work), known_hosts_file, host_key_sha256, insecure_skip_host_key_check, timeout (30),
batch_size, max_depth, skip_hidden, skip_names, max_file_bytes (200 MB), max_retries, acl_rules, default_acl.
Passwords, key text and passphrases are read from the environment when a connection is made and are never stored.

Permissions: folder -> principals through `AclRules` on the relative path; not POSIX modes or ACLs on the server. No
matching rule and no `default_acl` means nobody can read the file (fail closed).

Not exercised against OpenSSH, Azure Blob SFTP, AWS Transfer Family or any commercial SFTP server. Tested against an
in-process REAL SSH + SFTP server built with paramiko's server API (real key exchange, host key verification,
password and public key authentication, SFTP protocol) on a local port.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import io
import logging
import os
import posixpath
import socket
import stat
import tempfile
import threading
import time
from typing import BinaryIO, Callable, Iterator

from app.scale.connectors._http import AuthError, HttpError, TooLarge, env_secret
from app.scale.connectors.base import ScanBatch
from app.scale.connectors.webdav import DirUnreadable, NetCrawlConnector, NetEntry

log = logging.getLogger(__name__)


class HostKeyError(HttpError):
    """The server's host key is unknown or does not match what was configured. Never retried."""


def fingerprint_sha256(key) -> str:
    return "SHA256:" + base64.b64encode(hashlib.sha256(key.asbytes()).digest()).decode().rstrip("=")


class SftpFS:
    def __init__(self, cfg: dict, sleep: Callable[[float], None]) -> None:
        self.cfg = cfg
        self.host: str = cfg["host"]
        self.port = int(cfg.get("port", 22))
        self.username: str = cfg["username"]
        self.base = str(cfg.get("base_path", ".")) or "."
        self.timeout = float(cfg.get("timeout", 30))
        self.max_retries = int(cfg.get("max_retries", 5))
        self.max_wait = float(cfg.get("max_wait", 60.0))
        self._sleep = sleep
        self._local = threading.local()

    # ── connections ──

    def _scrub(self, text: str) -> str:
        for name in (self.cfg.get("password_env"), self.cfg.get("private_key_env"), self.cfg.get("passphrase_env")):
            secret = os.environ.get(name or "", "")
            if secret:
                text = text.replace(secret, "***")
        return text

    def _pkey(self):
        import paramiko

        passphrase = os.environ.get(self.cfg.get("passphrase_env") or "", "") or None
        if self.cfg.get("private_key_env"):
            pem = env_secret(self.cfg["private_key_env"], "SFTP private key")
            last: Exception | None = None
            for cls in (paramiko.Ed25519Key, paramiko.ECDSAKey, paramiko.RSAKey):
                try:
                    return cls.from_private_key(io.StringIO(pem), password=passphrase)
                except (paramiko.SSHException, ValueError) as exc:
                    last = exc
            raise AuthError(f"SFTP private key from {self.cfg['private_key_env']} could not be read ({type(last).__name__})")
        if self.cfg.get("private_key_path"):
            try:
                return paramiko.PKey.from_path(self.cfg["private_key_path"], passphrase.encode("utf-8") if passphrase else None)
            except (paramiko.SSHException, ValueError, OSError) as exc:
                raise AuthError(f"SFTP private key file {self.cfg['private_key_path']!r} could not be read ({type(exc).__name__})") from None
        return None

    def _connect(self):
        import paramiko

        client = paramiko.SSHClient()
        if self.cfg.get("host_key_sha256"):
            expected = str(self.cfg["host_key_sha256"]).strip()

            class Pinned(paramiko.MissingHostKeyPolicy):
                def missing_host_key(self, _client, hostname, key):
                    if not hmac.compare_digest(fingerprint_sha256(key), expected):
                        raise HostKeyError(f"SFTP host key of {hostname} does not match host_key_sha256")

            client.set_missing_host_key_policy(Pinned())          # no known_hosts: the pin is the only trust anchor
        else:
            client.load_system_host_keys()
            if self.cfg.get("known_hosts_file"):
                client.load_host_keys(self.cfg["known_hosts_file"])
            client.set_missing_host_key_policy(
                paramiko.AutoAddPolicy() if self.cfg.get("insecure_skip_host_key_check") else paramiko.RejectPolicy())
        pkey = self._pkey()
        password = env_secret(self.cfg["password_env"], "SFTP password") if self.cfg.get("password_env") else None
        if pkey is None and password is None:
            raise AuthError("SFTP needs password_env, private_key_path or private_key_env")
        try:
            client.connect(
                self.host, self.port, username=self.username, password=password, pkey=pkey, timeout=self.timeout,
                banner_timeout=self.timeout, auth_timeout=self.timeout, allow_agent=False, look_for_keys=False,
            )
            return client, client.open_sftp()
        except paramiko.AuthenticationException:
            client.close()
            raise AuthError(f"SFTP authentication to {self.host} as {self.username!r} was rejected") from None
        except paramiko.BadHostKeyException:
            client.close()
            raise HostKeyError(f"SFTP host key of {self.host} does not match known_hosts") from None
        except HostKeyError:
            client.close()
            raise
        except paramiko.SSHException as exc:
            client.close()
            if "not found in known_hosts" in str(exc):
                raise HostKeyError(
                    f"SFTP host key of {self.host} is unknown; set known_hosts_file or host_key_sha256") from None
            raise
        except BaseException:
            client.close()
            raise

    @staticmethod
    def _close(session) -> None:
        if session is None:
            return
        try:
            session[1].close()
        except Exception:  # noqa: BLE001
            pass
        try:
            session[0].close()
        except Exception:  # noqa: BLE001
            pass

    def close_listing(self) -> None:
        session = getattr(self._local, "session", None)
        self._local.session = None
        self._close(session)

    def _with_session(self, op: str, fn: Callable[[object], object], *, shared: bool):
        import paramiko
        from paramiko.ssh_exception import NoValidConnectionsError

        last = ""
        for attempt in range(self.max_retries + 1):
            session = None
            healthy = False
            try:
                if shared:
                    session = getattr(self._local, "session", None) or self._connect()
                    self._local.session = session
                else:
                    session = self._connect()
                result = fn(session[1])
                healthy = True
                return result
            except (PermissionError, FileNotFoundError):
                healthy = True                              # SFTP status codes, not a broken connection
                raise
            except OSError as exc:
                network = isinstance(exc, (ConnectionError, TimeoutError, socket.gaierror, NoValidConnectionsError))
                if exc.errno is None and not network:
                    healthy = True                          # an SFTP "Failure" status (e.g. not a directory): permanent
                    raise HttpError(f"SFTP {op} on {self.host}: {self._scrub(str(exc))[:120]}") from None
                last = f"{type(exc).__name__} {self._scrub(str(exc))[:120]}".strip()
            except (paramiko.SSHException, EOFError) as exc:
                last = f"{type(exc).__name__} {self._scrub(str(exc))[:120]}".strip()
            finally:
                if session is not None:
                    if shared and healthy:
                        pass
                    else:
                        if shared:
                            self._local.session = None
                        self._close(session)
            if attempt >= self.max_retries:
                raise HttpError(f"SFTP {op} on {self.host}: {last}")
            wait = min(2 ** attempt, self.max_wait)
            log.warning("SFTP %s on %s failed (%s); reconnecting in %.0fs", op, self.host, last[:60], wait)
            self._sleep(wait)
        raise HttpError(f"SFTP {op} on {self.host}: retries exhausted ({last})")

    # ── listing and bytes ──

    def _abs(self, rel: str) -> str:
        return posixpath.join(self.base, rel) if rel else self.base

    def listdir(self, rel_dir: str) -> list[NetEntry]:
        path = self._abs(rel_dir)
        try:
            attrs = self._with_session("READDIR", lambda sftp: sftp.listdir_attr(path), shared=True)
        except PermissionError:
            if rel_dir:
                raise DirUnreadable(rel_dir) from None
            raise AuthError(f"SFTP base_path {path!r} may not be listed by {self.username!r}") from None
        except FileNotFoundError:
            if rel_dir:
                return []                                   # vanished since its parent was listed
            raise HttpError(f"SFTP base_path {path!r} does not exist on {self.host}") from None
        out = []
        for a in attrs:  # type: ignore[union-attr]
            mode = a.st_mode or 0
            if stat.S_ISDIR(mode):
                out.append(NetEntry(a.filename, True, 0, float(a.st_mtime) if a.st_mtime is not None else None))
            elif stat.S_ISREG(mode):
                out.append(NetEntry(a.filename, False, int(a.st_size or 0), float(a.st_mtime) if a.st_mtime is not None else None))
        return out

    def open(self, rel_path: str, max_bytes: int) -> BinaryIO:
        path = self._abs(rel_path)

        def fetch(sftp) -> BinaryIO:
            size = sftp.stat(path).st_size
            if size is not None and size > max_bytes:
                raise TooLarge(f"SFTP file {rel_path!r} is {size} bytes, larger than {max_bytes}")
            tmp = tempfile.TemporaryFile()

            def check(done: int, _total: int) -> None:
                if done > max_bytes:
                    raise TooLarge(f"SFTP file {rel_path!r} is larger than {max_bytes} bytes")

            try:
                sftp.getfo(path, tmp, callback=check)
                tmp.seek(0)
                return tmp
            except BaseException:
                tmp.close()
                raise

        try:
            return self._with_session("READ", fetch, shared=False)  # type: ignore[return-value]
        except FileNotFoundError:
            raise HttpError(f"SFTP file {rel_path!r} does not exist", 404) from None
        except PermissionError:
            raise AuthError(f"SFTP file {rel_path!r} may not be read by {self.username!r}", 403) from None


class SftpConnector(NetCrawlConnector):
    kind = "sftp"

    def __init__(self, config: dict, *, sleep: Callable[[float], None] = time.sleep) -> None:
        super().__init__(SftpFS(config, sleep), config)

    def scan(self, cursor: dict) -> Iterator[ScanBatch]:
        try:
            yield from super().scan(cursor)
        finally:
            self.fs.close_listing()
