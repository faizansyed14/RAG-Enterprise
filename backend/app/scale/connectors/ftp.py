"""FTP and explicit FTPS (FTP over TLS) through the standard library's `ftplib`.

Protocol commands used (RFC 959, RFC 2228 / 4217 for TLS, RFC 3659 for MLSD / MDTM / SIZE):
  * USER / PASS (and AUTH TLS + PBSZ 0 + PROT P when `tls: "explicit"`), PASV, TYPE I, CWD, QUIT
  * MLSD  (machine-readable listing: type, size, modify)  -- preferred
  * LIST  (+ MDTM per file)                                -- fallback only for servers that do not understand MLSD
                                                              (vsftpd and some embedded servers); Unix `ls -l` and
                                                              MS-DOS / IIS line formats are parsed
  * RETR  to stream a file into a temporary file with a size cap
Nothing that writes (STOR, DELE, MKD, RNFR ...) is ever sent.

Behaviour
  * Crawl style on the shared engine in webdav.py (`NetCrawlConnector`): the tree is walked in one total order,
    the cursor is {"after": <last path>}, a scan from the start to the end sets complete_scope="" (the sync layer
    then deletes unseen files); a resumed scan, or one that skipped a directory the account may not list
    (reply 550), is not authoritative. A listing that FAILS (connection lost, 4xx after the retries) aborts the
    scan: it is never mistaken for an empty directory.
  * version = "<modify>:<size>" from MLSD (second resolution; MLSD may give fractions). In LIST fallback mode the
    mtime comes from MDTM (`list_fallback_mdtm`, default true: one extra command per file); without it the LIST
    date is used, which has only minute resolution for recent files and day resolution for old ones, so a same-size
    edit inside that window would not be noticed.
  * `external_id` = `path` = path below `base_path`; `open()` needs nothing else.
  * Symlinks are not followed (MLSD type other than file / dir, LIST type "l"). A name containing CR / LF cannot be
    fetched (ftplib refuses to put a newline in a command) and surfaces as a clear error from `open()`.
  * Retries: a 4xx reply (421 "too many connections / try later", 450, 451, 452), a dropped control connection and
    timeouts reconnect and retry with bounded exponential backoff (`max_retries`, default 5). FTP has no
    Retry-After. A rejected login (530 / 430 / 5xx at login) raises `AuthError` with the reply code only.
  * One control connection is kept for the listing walk of a scan (per thread) and closed when the scan ends;
    every `open()` uses its own short connection, so workers can read files concurrently.

Config: host (required), port (21), username (default "anonymous"), password_env (name of the environment variable;
omitted => anonymous e-mail password), base_path ("/"), tls ("none" | "explicit"; default "none"), verify_tls (true),
ca_bundle, encoding ("utf-8"), timeout (60), use_mlsd (true), list_fallback_mdtm (true), batch_size, max_depth,
skip_hidden, skip_names, max_file_bytes (200 MB), max_retries, acl_rules, default_acl.

Plain FTP sends the password and the file contents unencrypted: use `tls: "explicit"` outside a trusted network.
Implicit FTPS (port 990) is not supported.

Permissions: folder -> principals through `AclRules` on the relative path; not the FTP server's own file modes. No
matching rule and no `default_acl` means nobody can read the file (fail closed).

Not exercised against vsftpd, ProFTPD, FileZilla Server or IIS FTP. Tested against a REAL FTP server (pyftpdlib on
a local port: MLSD listing, the LIST + MDTM fallback with MLSD disabled, real 421 and 550 replies, real explicit FTPS
with a self-signed certificate). The MS-DOS / IIS LIST format is only unit-tested on sample lines.
"""

from __future__ import annotations

import ftplib
import logging
import os
import re
import ssl
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import BinaryIO, Callable, Iterator

from app.scale.connectors._http import AuthError, HttpError, TooLarge, env_secret
from app.scale.connectors.base import ScanBatch
from app.scale.connectors.webdav import DirUnreadable, NetCrawlConnector, NetEntry

log = logging.getLogger(__name__)

_MLSD_UNSUPPORTED = ("500", "501", "502", "504")
_LOGIN_REJECTED = ("530", "430", "331", "332", "532", "550")
_UNIX = re.compile(
    r"^(?P<type>[-dlcbpsD])(?P<perm>[rwxsStTl\-?]{9})[+@.]?\s+\d+\s+\S+(?:\s+\S+)?\s+(?P<size>\d+)\s+"
    r"(?P<mon>[A-Za-z]{3})\s+(?P<day>\d{1,2})\s+(?P<yt>\d{4}|\d{1,2}:\d{2})\s+(?P<name>.+)$"
)
_DOS = re.compile(
    r"^(?P<m>\d{2})-(?P<d>\d{2})-(?P<y>\d{2,4})\s+(?P<h>\d{1,2}):(?P<min>\d{2})\s*(?P<ap>[AaPp][Mm])?\s+"
    r"(?P<size>\d+|<DIR>)\s+(?P<name>.+)$"
)
_MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def parse_mlsd_time(value: str | None) -> float | None:
    """MLSD `modify` fact: YYYYMMDDHHMMSS[.sss], always UTC."""
    if not value or len(value) < 14 or not value[:14].isdigit():
        return None
    try:
        when = datetime.strptime(value[:14], "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
        return when.timestamp() + (float("0" + value[14:]) if value[14:15] == "." else 0.0)
    except ValueError:
        return None


def parse_list_line(line: str, now: datetime | None = None) -> NetEntry | None:
    """One line of a LIST reply (Unix `ls -l` or MS-DOS style) -> entry; None for links, totals and junk."""
    now = now or datetime.now(timezone.utc)
    line = line.rstrip("\r\n")
    match = _UNIX.match(line)
    if match:
        kind, name = match["type"], match["name"]
        if kind not in ("-", "d") or name in (".", ".."):
            return None
        month = _MONTHS.get(match["mon"].lower())
        mtime = None
        if month:
            try:
                if ":" in match["yt"]:
                    hour, minute = (int(x) for x in match["yt"].split(":"))
                    when = datetime(now.year, month, int(match["day"]), hour, minute, tzinfo=timezone.utc)
                    if when > now + timedelta(days=1):
                        when = when.replace(year=now.year - 1)
                else:
                    when = datetime(int(match["yt"]), month, int(match["day"]), tzinfo=timezone.utc)
                mtime = when.timestamp()
            except ValueError:
                mtime = None
        return NetEntry(name, kind == "d", 0 if kind == "d" else int(match["size"]), mtime)
    match = _DOS.match(line)
    if match:
        name = match["name"]
        if name in (".", ".."):
            return None
        year = int(match["y"])
        year += 2000 if year < 70 else (1900 if year < 100 else 0)
        hour = int(match["h"]) % 12 + (12 if (match["ap"] or "").lower() == "pm" else 0) if match["ap"] else int(match["h"])
        try:
            mtime = datetime(year, int(match["m"]), int(match["d"]), hour, int(match["min"]), tzinfo=timezone.utc).timestamp()
        except ValueError:
            mtime = None
        is_dir = match["size"] == "<DIR>"
        return NetEntry(name, is_dir, 0 if is_dir else int(match["size"]), mtime)
    return None


def _reply_code(exc: Exception) -> str:
    return str(exc).strip()[:3]


class FtpFS:
    def __init__(self, cfg: dict, sleep: Callable[[float], None]) -> None:
        self.cfg = cfg
        self.host: str = cfg["host"]
        self.port = int(cfg.get("port", 21))
        self.base = "/" + str(cfg.get("base_path", "/")).strip("/")
        self.tls = str(cfg.get("tls", "none")).lower()
        if self.tls not in ("none", "explicit"):
            raise ValueError("tls must be 'none' or 'explicit' (implicit FTPS is not supported)")
        self.timeout = float(cfg.get("timeout", 60))
        self.encoding = cfg.get("encoding", "utf-8")
        self.max_retries = int(cfg.get("max_retries", 5))
        self.max_wait = float(cfg.get("max_wait", 60.0))
        self.use_mlsd = bool(cfg.get("use_mlsd", True))
        self.mdtm = bool(cfg.get("list_fallback_mdtm", True))
        self._sleep = sleep
        self._local = threading.local()

    # ── connections ──

    def _secret(self) -> str:
        return env_secret(self.cfg["password_env"], "FTP password") if self.cfg.get("password_env") else "anonymous@"

    def _scrub(self, text: str) -> str:
        password = os.environ.get(self.cfg.get("password_env") or "", "")
        return text.replace(password, "***") if password else text

    def _connect(self) -> ftplib.FTP:
        user = self.cfg.get("username") or "anonymous"
        password = self._secret()
        if self.tls == "explicit":
            ctx = ssl.create_default_context(cafile=self.cfg.get("ca_bundle") or None)
            if not self.cfg.get("verify_tls", True):
                ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE
            ftp: ftplib.FTP = ftplib.FTP_TLS(context=ctx, timeout=self.timeout, encoding=self.encoding)
        else:
            ftp = ftplib.FTP(timeout=self.timeout, encoding=self.encoding)
        try:
            ftp.connect(self.host, self.port)
            if self.tls == "explicit":
                try:
                    ftp.auth()                                   # type: ignore[attr-defined]
                except ftplib.error_perm as exc:
                    raise HttpError(f"FTP server {self.host} does not accept AUTH TLS: {_reply_code(exc)}") from None
            try:
                ftp.login(user, password)
            except ftplib.error_perm as exc:
                raise AuthError(f"FTP login to {self.host} as {user!r} was rejected: {_reply_code(exc)}", None) from None
            except ftplib.error_temp as exc:
                if _reply_code(exc) == "430":
                    raise AuthError(f"FTP login to {self.host} as {user!r} was rejected: 430", None) from None
                raise
            if self.tls == "explicit":
                ftp.prot_p()
            ftp.set_pasv(True)
            return ftp
        except BaseException:
            self._close(ftp, clean=False)
            raise

    @staticmethod
    def _close(ftp: ftplib.FTP | None, clean: bool = True) -> None:
        if ftp is None:
            return
        try:
            if clean:
                ftp.quit()
            else:
                ftp.close()
        except Exception:  # noqa: BLE001
            try:
                ftp.close()
            except Exception:  # noqa: BLE001
                pass

    def close_listing(self) -> None:
        ftp = getattr(self._local, "ftp", None)
        self._local.ftp = None
        self._close(ftp)

    def _with_session(self, op: str, fn: Callable[[ftplib.FTP], object], *, shared: bool):
        """Run `fn(ftp)`; reconnect and retry on transient failures. 5xx replies reach the caller as ftplib.error_perm."""
        last = ""
        for attempt in range(self.max_retries + 1):
            ftp: ftplib.FTP | None = None
            healthy = False
            try:
                if shared:
                    ftp = getattr(self._local, "ftp", None) or self._connect()
                    self._local.ftp = ftp
                else:
                    ftp = self._connect()
                result = fn(ftp)
                healthy = True
                return result
            except ftplib.error_perm:
                healthy = True                      # a refusal, not a broken connection: the caller decides
                raise
            except ssl.SSLCertVerificationError as exc:
                raise HttpError(f"FTP {op} on {self.host}: TLS certificate verification failed ({exc.verify_message})") from None
            except (ftplib.error_temp, ftplib.error_reply, ftplib.error_proto, EOFError, OSError) as exc:
                last = f"{type(exc).__name__} {self._scrub(str(exc))[:120]}".strip()
                if attempt >= self.max_retries:
                    code = _reply_code(exc) if isinstance(exc, ftplib.Error) else ""
                    raise HttpError(f"FTP {op} on {self.host}: {last}", int(code) if code.isdigit() else None) from None
                wait = min(2 ** attempt, self.max_wait)
                log.warning("FTP %s on %s failed (%s); reconnecting in %.0fs", op, self.host, last[:60], wait)
                self._sleep(wait)
            finally:
                if ftp is not None:
                    if shared and healthy:
                        pass                        # keep the listing connection for the next directory
                    else:
                        if shared:
                            self._local.ftp = None
                        self._close(ftp, clean=healthy)
        raise HttpError(f"FTP {op} on {self.host}: retries exhausted ({last})")

    # ── listing ──

    def _abs(self, rel: str) -> str:
        return self.base if not rel else (self.base.rstrip("/") + "/" + rel)

    def listdir(self, rel_dir: str) -> list[NetEntry]:
        path = self._abs(rel_dir)

        def run(ftp: ftplib.FTP) -> list[NetEntry]:
            ftp.cwd(path)
            return self._list(ftp)

        try:
            entries = self._with_session("LIST", run, shared=True)
        except ftplib.error_perm as exc:
            # 550 is both "not found" and "permission denied". Either way we cannot see the directory, so the scan
            # must not conclude that its files are gone: skip it and stop being authoritative.
            if rel_dir:
                raise DirUnreadable(rel_dir) from None
            raise HttpError(f"FTP base_path {path!r} cannot be listed: {self._scrub(str(exc))[:120]}", int(_reply_code(exc) or 0) or None) from None
        return entries  # type: ignore[return-value]

    def _list(self, ftp: ftplib.FTP) -> list[NetEntry]:
        if self.use_mlsd:
            try:
                return self._mlsd(ftp)
            except ftplib.error_perm as exc:
                if _reply_code(exc) not in _MLSD_UNSUPPORTED:
                    raise
                log.info("FTP server %s does not understand MLSD; falling back to LIST", self.host)
                self.use_mlsd = False
        return self._list_fallback(ftp)

    @staticmethod
    def _mlsd(ftp: ftplib.FTP) -> list[NetEntry]:
        out = []
        for name, facts in ftp.mlsd(facts=["type", "size", "modify"]):
            kind = facts.get("type", "").lower()
            if kind not in ("file", "dir") or name in (".", ".."):
                continue                                   # cdir / pdir / symlinks / devices
            size = int(facts["size"]) if kind == "file" and facts.get("size", "").isdigit() else 0
            out.append(NetEntry(name, kind == "dir", size, parse_mlsd_time(facts.get("modify"))))
        return out

    def _list_fallback(self, ftp: ftplib.FTP) -> list[NetEntry]:
        lines: list[str] = []
        ftp.retrlines("LIST", lines.append)
        entries = [e for e in (parse_list_line(line) for line in lines) if e is not None]
        if self.mdtm:
            for entry in entries:
                if entry.is_dir:
                    continue
                try:
                    reply = ftp.voidcmd("MDTM " + entry.name)          # relative to the cwd set by listdir
                    exact = parse_mlsd_time(reply.split()[-1])
                    if exact is not None:
                        entry.mtime = exact
                except (ftplib.error_perm, ValueError):
                    pass                                                  # keep the LIST date
        return entries

    # ── bytes ──

    def open(self, rel_path: str, max_bytes: int) -> BinaryIO:
        path = self._abs(rel_path)

        def fetch(ftp: ftplib.FTP) -> BinaryIO:
            tmp = tempfile.TemporaryFile()
            total = 0

            def sink(chunk: bytes) -> None:
                nonlocal total
                total += len(chunk)
                if total > max_bytes:
                    raise TooLarge(f"FTP file {rel_path!r} is larger than {max_bytes} bytes")
                tmp.write(chunk)

            try:
                ftp.retrbinary("RETR " + path, sink, 1 << 20)
                tmp.seek(0)
                return tmp
            except BaseException:
                tmp.close()
                raise

        try:
            return self._with_session("RETR", fetch, shared=False)  # type: ignore[return-value]
        except ftplib.error_perm as exc:
            raise HttpError(f"FTP RETR {rel_path!r} failed: {self._scrub(str(exc))[:120]}", int(_reply_code(exc) or 0) or None) from None
        except ValueError as exc:
            raise HttpError(f"FTP RETR {rel_path!r}: {exc}") from None


class FtpConnector(NetCrawlConnector):
    kind = "ftp"

    def __init__(self, config: dict, *, sleep: Callable[[float], None] = time.sleep) -> None:
        super().__init__(FtpFS(config, sleep), config)

    def scan(self, cursor: dict) -> Iterator[ScanBatch]:
        try:
            yield from super().scan(cursor)
        finally:
            self.fs.close_listing()
