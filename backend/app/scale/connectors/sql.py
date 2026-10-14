"""A read-only SQL query as a source: every row (or group of rows) of ONE query becomes a virtual Markdown file.

Covers: any database SQLAlchemy can reach whose dialect has a way to make a transaction read-only (PostgreSQL and
SQLite are implemented; see "Read-only" below). The connector never writes; it opens one connection, one read-only
transaction and one server-side cursor per scan.

Calls it makes (SQLAlchemy 2.x, `Connection.execute` with `stream_results=True, yield_per=<fetch_size>`):

    SET TRANSACTION READ ONLY                (PostgreSQL; SQLite: PRAGMA query_only = ON)  -- first statement
    SHOW transaction_read_only               (PostgreSQL; SQLite: PRAGMA query_only)       -- verified, else refuse
    SELECT * FROM (<query>) AS _src
        WHERE <key> IS NOT NULL [AND <key> > :after] [AND <updated> > :since] ORDER BY <key>      -- scan
    SELECT * FROM (<query>) AS _src WHERE <key> = :key                                           -- open(), 1 row/doc
    SELECT * FROM (<query>) AS _src WHERE <key> >= :key ORDER BY <key>  (reads group_by_rows rows) -- open(), groups
    SELECT * FROM (<query>) AS _src WHERE <key> IS NOT NULL                                      -- once, to learn the key type

Config
    url_env          name of the environment variable holding the SQLAlchemy URL (required; the URL carries the
                     password, so it is only ever read from the environment, at call time).
    query            one SELECT / WITH statement (required). Must start with SELECT or WITH, contain no `;`, and
                     contain none of INSERT UPDATE DELETE DROP ALTER CREATE TRUNCATE GRANT as a whole word
                     (case-insensitive, anywhere in the text, string literals and comments included -- a blunt, fail-closed
                     check; a `WHERE status = 'update'` is refused, use a view). It is wrapped as a sub-select, so give
                     every column a unique name. `:name` inside the query is NOT a bind parameter (escaped for you).
    key_column       result column that identifies a row (required): unique, not NULL (rows with a NULL key are ignored);
                     integer, text, uuid, numeric, date / timestamp keys work. A repeated key aborts the scan.
    updated_column   optional: a timestamp / number that grows when a row changes. Enables incremental scans (below).
    title_column     optional: the column used as the document title (default `<name> <key>`).
    name             virtual folder (default "sql"): path `<name>/<key>.md`, external_id `sql:<name>/<key>`.
    group_by_rows    rows per virtual document (default 1). With N > 1, N consecutive rows in key order make one
                     document `<name>/<first key>..<last key>.md` whose external_id is `sql:<name>/<first key>`.
    fetch_size       rows per server-side cursor fetch (`yield_per`, default 1000).
    batch_size       changes per ScanBatch (default 500; a multiple of one document, never splits a group).
    max_rows         optional hard cap of rows per scan; a capped scan is never authoritative.
    max_file_bytes   a rendered document above this (default 10 MB) is skipped (and reported as a delete); open() refuses it.
    max_cell_chars   a single value longer than this (default 100000) is cut, with a marker.
    full_resync_hours  (default 24) how often an incremental connection does a full pass (the only way to see deletions).
    overlap_seconds  (default 0) re-read this much before a timestamp watermark (commit lag of the source's writers).
    allow_unenforced_read_only  (default false) allow a dialect for which this module cannot make the transaction read-only.
    acl_rules, default_acl

Rendering: `_http.render_document`. A single-line value is a `column: value` line; NULL and empty values are left out; a
value with line breaks goes under the lines as `column:` + the text; bytes are summarised, never dumped.

Scans
  * Full scan from the start: authoritative, `complete_scope = "sql:<name>/"`, so rows that vanished from the query are
    deleted by the sync layer. A resumed scan (cursor `after` = last key of the last finished document), an incremental
    scan, a scan stopped by `max_rows` are not authoritative.
  * Incremental (only with `updated_column`, and only with group_by_rows = 1, a group needs all of its rows): rows with
    `updated_column > since`; the cursor carries the largest value seen; deletions are NOT seen until the next full pass.
    Rows whose updated_column is NULL are only picked up by a full pass.
  * version = hash of the rendered text + path, so it changes iff what is indexed changes.
  * Cursors hold keys and watermarks only (type-tagged JSON): never a URL, a password or a row's content.
  * Until the sync layer persists the cursor of a finished scan, every scan is a full one (it saves {} at the end).

Read-only: the keyword check above is the first line of defence and is easy to get around (a function that writes, `nextval()`,
a stored procedure behind a view), so the DATABASE enforces it: the whole scan runs in a transaction that was set READ ONLY as its first statement and
whose state is read back (a transaction that is not read-only is refused), and on PostgreSQL the connection also starts with
`-c default_transaction_read_only=on` (merged into any `options` in the URL). Still give the connector a database role that
may only SELECT.

Limitations
  * One long read transaction and server-side cursor per scan: it holds a snapshot while the sync layer works through the
    batches; a server `idle_in_transaction_session_timeout` or `statement_timeout` kills the scan, which then resumes from its
    cursor on the next run. Set timeouts through the URL (`?options=-c statement_timeout=...`).
  * The ORDER BY key is a sort of the whole query unless the key is indexed.
  * group_by_rows > 1 shifts every later group when a row is inserted or deleted before it (those documents are re-indexed).
    open() re-reads the live database, so it can be newer than the scan that announced the version.
  * A column that is a huge object is cut at `max_cell_chars`; binary columns are not indexed.
  * MySQL, Oracle, SQL Server: refused unless `allow_unenforced_read_only` (then only the keyword check protects you).

Tested against a REAL PostgreSQL 16 through psycopg2 (SQLAlchemy 2.1.4 and the pinned 2.0.54) and a real SQLite file: writes
through a function / nextval refused by the database, a backend killed mid-scan, a wrong password for a real role. Not
exercised against MySQL, MariaDB, Oracle, SQL Server, Snowflake, BigQuery or psycopg 3 / asyncpg (the read-only code path
of those dialects does not exist here). A plain `postgresql://` URL is run with psycopg2 when psycopg 3 is not installed
(SQLAlchemy 2.1 would otherwise ask for psycopg 3). Needs the SQLAlchemy driver of your database (psycopg2-binary for PostgreSQL).
"""

from __future__ import annotations

import datetime as dt
import decimal
import importlib.util
import json
import logging
import re
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from typing import Any, BinaryIO, Callable, Iterator

import sqlalchemy as sa
from sqlalchemy import exc as sa_exc

from app.scale.connectors._http import AuthError, HttpError, TooLarge, env_secret, render_document, safe_name
from app.scale.connectors._objects import parse_dt, short_hash
from app.scale.connectors.base import AclRules, Change, ScanBatch, SourceFile

log = logging.getLogger(__name__)

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_STARTS = re.compile(r"(select|with)\b", re.I)
_FORBIDDEN = re.compile(r"\b(insert|update|delete|drop|alter|create|truncate|grant)\b", re.I)
_COLON_PARAM = re.compile(r"(?<![:\w\x5c]):(\w+)(?!:)", re.UNICODE)   # what SQLAlchemy's text() treats as a bind parameter
_NONE = object()


class SqlSourceError(HttpError):
    """Anything that went wrong talking to the database. The message never carries the URL, a password or a row."""


def check_query(query: object) -> str:
    """The query if it is acceptable for a read-only source, else ValueError saying why."""
    if not isinstance(query, str) or not query.strip():
        raise ValueError("sql: `query` is required")
    text = query.strip()
    if "\x00" in text:
        raise ValueError("sql: the query contains a NUL byte")
    if ";" in text:
        raise ValueError("sql: the query must be a single statement without `;`")
    if not _STARTS.match(text):
        raise ValueError("sql: the query must start with SELECT or WITH")
    bad = _FORBIDDEN.search(text)
    if bad:
        raise ValueError(f"sql: the query contains {bad.group(1).upper()}, which is not allowed in a read-only source")
    return text


# ── typed JSON for keys and watermarks (cursors must survive a trip through JSON) ──


def _enc(value: Any) -> list:
    if isinstance(value, bool):
        return ["str", str(value)]
    if isinstance(value, int):
        return ["int", value]
    if isinstance(value, float):
        return ["float", value]
    if isinstance(value, decimal.Decimal):
        return ["dec", str(value)]
    if isinstance(value, dt.datetime):
        return ["dt", value.isoformat()]
    if isinstance(value, dt.date):
        return ["date", value.isoformat()]
    if isinstance(value, uuid.UUID):
        return ["uuid", str(value)]
    return ["str", str(value)]


_DEC = {
    "int": int, "float": float, "dec": decimal.Decimal, "dt": dt.datetime.fromisoformat, "date": dt.date.fromisoformat,
    "uuid": uuid.UUID, "str": str,
}


def _dec(item: Any) -> Any:
    """Inverse of `_enc`; None for anything that is not a well-formed tagged value (a damaged cursor is ignored)."""
    try:
        kind, value = item
        return _DEC[kind](value)
    except (TypeError, ValueError, KeyError, decimal.InvalidOperation):
        return None


def _key_text(key: Any) -> str:
    return key.isoformat() if isinstance(key, (dt.datetime, dt.date)) else str(key)


def _as_datetime(value: Any) -> dt.datetime | None:
    if isinstance(value, dt.datetime):
        return value if value.tzinfo else value.replace(tzinfo=dt.timezone.utc)
    if isinstance(value, dt.date):
        return dt.datetime(value.year, value.month, value.day, tzinfo=dt.timezone.utc)
    if isinstance(value, str):
        return parse_dt(value)
    return None                                   # a number may be an epoch or a version counter: not a modification time


class _State:
    """Full vs incremental vs resumed, the watermark and every cursor (same protocol as `_objects.ScanState`, typed)."""

    def __init__(self, cursor: dict, started: float, *, has_updated: bool, full_hours: float) -> None:
        cursor = cursor if isinstance(cursor, dict) else {}
        mode = cursor.get("mode")
        if "mode" in cursor:                       # a mid-scan cursor must be whole and fit this configuration, else start over
            usable = mode in ("full", "inc") and _dec(cursor.get("after")) is not None
            usable = usable and (mode == "full" or (has_updated and _dec(cursor.get("since")) is not None))
            if not usable:
                cursor, mode = {}, None
        self.started, self.has_updated = started, has_updated
        self.resumed = mode is not None
        self.last_full = cursor.get("last_full") if isinstance(cursor.get("last_full"), (int, float)) else None
        self.high = _dec(cursor.get("high")) if cursor.get("high") else None
        since = _dec(cursor.get("since")) if (has_updated and cursor.get("since")) else None
        self.after = _dec(cursor.get("after")) if self.resumed else None
        if self.resumed:
            self.mode, self.since = mode, since
        else:
            full_due = since is None or self.last_full is None or (started - self.last_full) >= full_hours * 3600
            self.mode, self.since = ("full", None) if full_due else ("inc", since)

    @property
    def incremental(self) -> bool:
        return self.mode == "inc" and self.since is not None

    @property
    def authoritative(self) -> bool:
        return self.mode == "full" and not self.resumed

    def observe(self, value: Any) -> None:
        if value is None:
            return
        try:
            if self.high is None or value > self.high:
                self.high = value
        except TypeError:
            pass                                   # an incomparable value never moves the watermark

    def cursor(self, last_key: Any) -> dict:
        return {
            "mode": self.mode, "since": _enc(self.since) if self.since is not None else None,
            "high": _enc(self.high) if self.high is not None else None, "last_full": self.last_full,
            "after": _enc(last_key) if last_key is not None else None,
        }

    def final(self, truncated: bool) -> dict:
        if not self.has_updated:
            return {}
        if truncated:                              # a capped scan does not advance the watermark
            keep: dict = {"last_full": self.last_full}
            if self.since is not None:
                keep["since"] = _enc(self.since)
            return keep
        known = [v for v in (self.high, self.since) if v is not None]
        try:
            newest = max(known) if known else None
        except TypeError:
            newest = self.high
        out: dict = {"last_full": self.started if self.authoritative else self.last_full}
        if newest is not None:
            out["since"] = _enc(newest)
        return out


class SqlConnector:
    kind = "sql"

    def __init__(
        self, config: dict, *, engine_factory: Callable[[sa.URL], sa.Engine] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.cfg = config
        self.url_env = config.get("url_env")
        if not self.url_env:
            raise ValueError("sql: config needs `url_env`, the name of the environment variable holding the SQLAlchemy URL")
        self.query = check_query(config.get("query"))
        self.key_column = self._column(config, "key_column", required=True)
        self.updated_column = self._column(config, "updated_column")
        self.title_column = self._column(config, "title_column")
        self.name = str(config.get("name") or "sql")
        if safe_name(self.name) != self.name:
            raise ValueError(f"sql: `name` {self.name!r} cannot be used as a folder name")
        self.group_by_rows = int(config.get("group_by_rows", 1))
        if self.group_by_rows < 1:
            raise ValueError("sql: `group_by_rows` must be at least 1")
        if self.updated_column and self.group_by_rows > 1:
            raise ValueError("sql: `updated_column` (incremental scans) cannot be combined with group_by_rows > 1")
        self.fetch_size = max(1, int(config.get("fetch_size", 1000)))
        self.batch_size = max(1, int(config.get("batch_size", 500)))
        self.max_rows = int(config["max_rows"]) if config.get("max_rows") else None
        self.max_bytes = int(config.get("max_file_bytes", 10 * 1024 * 1024))
        self.max_cell = max(16, int(config.get("max_cell_chars", 100_000)))
        self.full_hours = float(config.get("full_resync_hours", 24))
        self.overlap = float(config.get("overlap_seconds", 0))
        self.allow_unenforced = bool(config.get("allow_unenforced_read_only", False))
        self.rules = AclRules(config.get("acl_rules"), config.get("default_acl"))
        self.clock = clock
        self.scope = f"sql:{self.name}/"
        self.stats: dict[str, int] = {}
        self._factory = engine_factory or self._default_engine
        self._engine: sa.Engine | None = None
        self._engine_url = ""
        self._lock = threading.Lock()
        self._secrets: set[str] = set()
        self._key_type: type | None = None
        self._sql_query = _COLON_PARAM.sub(r"\\:\1", self.query)

    @staticmethod
    def _column(config: dict, field: str, *, required: bool = False) -> str:
        value = config.get(field)
        if not value:
            if required:
                raise ValueError(f"sql: config needs `{field}`")
            return ""
        if not isinstance(value, str) or not _IDENT.match(value):
            raise ValueError(f"sql: `{field}` must be a plain column name, got {value!r}")
        return value

    def _count(self, reason: str, n: int = 1) -> None:
        self.stats[reason] = self.stats.get(reason, 0) + n

    # ── connection ──

    @staticmethod
    def _default_engine(url: sa.URL) -> sa.Engine:
        kwargs: dict[str, Any] = {}
        backend = url.get_backend_name()
        if backend == "postgresql" and url.get_driver_name() in ("psycopg2", "psycopg"):
            options = " ".join(x for x in (str(url.query.get("options") or ""), "-c default_transaction_read_only=on") if x)
            kwargs["connect_args"] = {"options": options}
        if backend != "sqlite":
            kwargs.update(pool_size=2, max_overflow=4, pool_recycle=1800)
        return sa.create_engine(url, **kwargs)

    def _url(self) -> str:
        url = env_secret(self.url_env, "SQLAlchemy database URL")
        self._secrets.add(url)
        try:
            password = sa.make_url(url).password
        except (sa_exc.ArgumentError, ValueError):
            password = None
        if password:
            self._secrets.add(password)
        return url

    def _scrub(self, text: str) -> str:
        for secret in sorted(self._secrets, key=len, reverse=True):
            if secret:
                text = text.replace(secret, "***")
        return text

    def _engine_for(self, url: str) -> sa.Engine:
        with self._lock:
            if self._engine is not None and self._engine_url == url:
                return self._engine
            try:
                parsed = sa.make_url(url)
                if parsed.drivername == "postgresql" and importlib.util.find_spec("psycopg") is None \
                        and importlib.util.find_spec("psycopg2") is not None:
                    parsed = parsed.set(drivername="postgresql+psycopg2")       # SQLAlchemy 2.1 would pick psycopg 3
                engine = self._factory(parsed)
            except (sa_exc.ArgumentError, sa_exc.NoSuchModuleError, ValueError) as exc:
                raise SqlSourceError(
                    f"sql: environment variable {self.url_env} does not hold a usable SQLAlchemy URL ({type(exc).__name__})"
                ) from None
            except ImportError as exc:
                raise SqlSourceError(f"sql: the database driver for this URL is not installed (missing module {exc.name})") from None
            old, self._engine, self._engine_url = self._engine, engine, url
            if old is not None:
                old.dispose()
            return engine

    def close(self) -> None:
        with self._lock:
            if self._engine is not None:
                self._engine.dispose()
            self._engine, self._engine_url = None, ""

    def _error(self, what: str, exc: Exception) -> HttpError:
        base = getattr(exc, "orig", None) or exc
        code = getattr(base, "pgcode", None) or ""
        raw = str(base).strip()
        first = self._scrub(raw.splitlines()[0] if raw else type(base).__name__)[:300]
        lowered = first.lower()
        if code in ("28P01", "28000") or "password authentication failed" in lowered or "access denied" in lowered:
            return AuthError(f"sql {what}: the database rejected the login ({code or 'authentication'}): check {self.url_env}", None)
        suffix = {"25006": " (the read-only transaction refused a write)",
                  "42703": " (key_column / updated_column must name a column that the query returns)"}.get(code, "")
        return SqlSourceError(f"sql {what} failed [{type(base).__name__}{'/' + code if code else ''}]: {first}{suffix}", None)

    def _db(self, what: str, fn: Callable, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except sa_exc.SQLAlchemyError as exc:
            raise self._error(what, exc) from None

    def _begin_read_only(self, conn: sa.Connection) -> None:
        backend = conn.dialect.name
        if backend == "postgresql":
            self._db("read-only setup", conn.exec_driver_sql, "SET TRANSACTION READ ONLY")
            state = self._db("read-only check", conn.exec_driver_sql, "SHOW transaction_read_only").scalar()
            enforced = str(state).lower() == "on"
        elif backend == "sqlite":
            self._db("read-only setup", conn.exec_driver_sql, "PRAGMA query_only = ON")
            enforced = self._db("read-only check", conn.exec_driver_sql, "PRAGMA query_only").scalar() in (1, "1")
        elif self.allow_unenforced:
            log.warning("sql: dialect %s cannot be made read-only by this connector; only the keyword check protects it", backend)
            return
        else:
            raise SqlSourceError(
                f"sql: cannot make a {backend} transaction read-only from here; use a SELECT-only database role and set "
                "`allow_unenforced_read_only` to accept the keyword check alone"
            )
        if not enforced:
            raise SqlSourceError("sql: the database did not put the transaction in read-only mode; refusing to run the query")

    @contextmanager
    def _session(self, engine: sa.Engine) -> Iterator[sa.Connection]:
        conn = self._db("connect", engine.connect)
        try:
            self._begin_read_only(conn)
            yield conn
        finally:
            try:
                conn.rollback()                      # nothing to commit: this connection only ever reads
            except Exception:                        # noqa: BLE001
                log.debug("sql: rollback failed while closing", exc_info=True)
            finally:
                conn.close()

    # ── statements ──

    def _quote(self, conn: sa.Connection, name: str) -> str:
        return conn.dialect.identifier_preparer.quote(name)

    def _select(self, conn: sa.Connection, where: list[str], order: bool = True) -> str:
        key = self._quote(conn, self.key_column)
        sql = f"SELECT * FROM (\n{self._sql_query}\n) AS _src WHERE " + " AND ".join([f"{key} IS NOT NULL", *where])
        return sql + (f" ORDER BY {key}" if order else "")

    def _execute(self, conn: sa.Connection, sql: str, params: dict):
        statement = sa.text(sql).execution_options(stream_results=True, yield_per=self.fetch_size)
        return self._db("query", conn.execute, statement, params)

    def _columns(self, result) -> list[str]:
        columns = list(result.keys())
        for field, name in (("key_column", self.key_column), ("updated_column", self.updated_column), ("title_column", self.title_column)):
            if name and name not in columns:
                raise SqlSourceError(f"sql: `{field}` {name!r} is not a column of the query (columns: {', '.join(columns)})")
        return columns

    # ── rendering ──

    def _fmt(self, value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, bool):
            text = "true" if value else "false"
        elif isinstance(value, (bytes, bytearray, memoryview)):
            text = f"<{len(value)} bytes of binary data>"
        elif isinstance(value, (dt.datetime, dt.date, dt.time)):
            text = value.isoformat()
        elif isinstance(value, decimal.Decimal):
            text = format(value, "f")
        elif isinstance(value, (dict, list, tuple)):
            text = json.dumps(value, sort_keys=True, default=str, ensure_ascii=False)
        else:
            text = str(value)
        text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
        if len(text) > self.max_cell:
            self._count("cut_cells")
            text = text[: self.max_cell] + f" ... [cut: {len(text) - self.max_cell} more characters]"
        return text

    def _block(self, row: dict) -> tuple[dict[str, str], str]:
        """(single-line `column: value` pairs, text of the multi-line columns)"""
        singles: dict[str, str] = {}
        longs: list[str] = []
        for column, value in row.items():
            text = self._fmt(value)
            if not text:
                continue
            if "\n" in text:
                longs.append(f"{column}:\n{text}")
            else:
                singles[column] = text
        return singles, "\n\n".join(longs)

    def _title(self, row: dict) -> str:
        title = re.sub(r"\s+", " ", self._fmt(row.get(self.title_column)) if self.title_column else "").strip()
        return title or f"{self.name} {_key_text(row[self.key_column])}"

    def _render(self, rows: list[dict]) -> bytes:
        if len(rows) == 1 and self.group_by_rows == 1:
            singles, longs = self._block(rows[0])
            return render_document(self._title(rows[0]), singles, longs)
        parts = []
        for row in rows:
            singles, longs = self._block(row)
            lines = [f"{c}: {v}" for c, v in singles.items()]
            parts.append("\n".join([f"## {self._title(row)}", "", *lines, *(["", longs] if longs else [])]))
        first, last = _key_text(rows[0][self.key_column]), _key_text(rows[-1][self.key_column])
        meta = {"rows": str(len(rows)), f"first {self.key_column}": first, f"last {self.key_column}": last}
        return render_document(f"{self.name}: {first} to {last}", meta, "\n\n".join(parts))

    def _change(self, rows: list[dict]) -> Change:
        first, last = _key_text(rows[0][self.key_column]), _key_text(rows[-1][self.key_column])
        external_id = f"{self.scope}{first}"
        text = self._render(rows)
        if len(text) > self.max_bytes:
            self._count("oversize")
            return Change("delete", external_id)       # it may have been indexed when it was smaller
        leaf = first if len(rows) == 1 and self.group_by_rows == 1 else f"{first}..{last}"
        path = f"{self.name}/{safe_name(leaf, 'row', 100)}.md"
        stamps = [t for t in (_as_datetime(r.get(self.updated_column)) for r in rows) if t] if self.updated_column else []
        sf = SourceFile(
            external_id=external_id, path=path, name=path.rsplit("/", 1)[-1], size=len(text),
            modified_at=max(stamps) if stamps else None, version=short_hash(path, text, size=16),
            acl=self.rules.for_path(path), mime="text/markdown", extra={"key": first, "rows": len(rows)},
        )
        return Change("upsert", external_id, sf)

    # ── scan ──

    def _scan_statement(self, conn: sa.Connection, state: _State) -> tuple[str, dict]:
        key, params, where = self._quote(conn, self.key_column), {}, []
        if state.after is not None:
            where.append(f"{key} > :after")
            params["after"] = state.after
        if state.incremental:
            since = state.since
            if self.overlap and isinstance(since, dt.datetime):
                since = since - dt.timedelta(seconds=self.overlap)
            where.append(f"{self._quote(conn, self.updated_column)} > :since")
            params["since"] = since
        return self._select(conn, where), params

    def scan(self, cursor: dict) -> Iterator[ScanBatch]:
        state = _State(cursor, self.clock(), has_updated=bool(self.updated_column), full_hours=self.full_hours)
        engine = self._engine_for(self._url())
        changes: list[Change] = []
        group: list[dict] = []
        last_key, previous, seen, truncated = state.after, _NONE, 0, False
        with self._session(engine) as conn:
            sql, params = self._scan_statement(conn, state)
            result = self._execute(conn, sql, params)
            try:
                columns: list[str] | None = None
                while not truncated:
                    part = self._db("fetch", result.fetchmany, self.fetch_size)
                    if not part:
                        break
                    columns = columns or self._columns(result)
                    for raw in part:
                        row = dict(zip(columns, raw))
                        key = row[self.key_column]
                        if previous is not _NONE and key == previous:
                            raise SqlSourceError(f"sql: key_column {self.key_column!r} is not unique (value {_key_text(key)[:80]!r} repeats)")
                        if self.max_rows is not None and seen >= self.max_rows:
                            truncated = True
                            break
                        previous, seen = key, seen + 1
                        if self.updated_column:
                            state.observe(row[self.updated_column])
                        group.append(row)
                        if len(group) >= self.group_by_rows:
                            changes.append(self._change(group))
                            last_key, group = key, []
                            if len(changes) >= self.batch_size:
                                yield ScanBatch(changes, state.cursor(last_key))
                                changes = []
            finally:
                try:
                    result.close()
                except Exception:                    # noqa: BLE001
                    log.debug("sql: closing the cursor failed", exc_info=True)
        if group:
            changes.append(self._change(group))
        self._count("rows", seen)
        if truncated:
            self._count("truncated")
        complete = self.scope if (state.authoritative and not truncated) else None
        yield ScanBatch(changes, state.final(truncated), done=True, complete_scope=complete)

    # ── bytes and permissions ──

    def _parse_key(self, conn: sa.Connection, text: str) -> Any:
        if self._key_type is None:
            probe = self._execute(conn, self._select(conn, [], order=False), {})
            try:
                columns = None
                part = self._db("fetch", probe.fetchmany, 1)
                if part:
                    columns = self._columns(probe)
                    self._key_type = type(part[0][columns.index(self.key_column)])
            finally:
                probe.close()
        kind = self._key_type
        try:
            if kind is None or kind is str:
                return text
            if kind is int:
                return int(text)
            if kind is float:
                return float(text)
            if kind is decimal.Decimal:
                return decimal.Decimal(text)
            if kind is uuid.UUID:
                return uuid.UUID(text)
            if issubclass(kind, dt.datetime):
                return dt.datetime.fromisoformat(text)
            if issubclass(kind, dt.date):
                return dt.date.fromisoformat(text)
        except (ValueError, decimal.InvalidOperation):
            raise SqlSourceError(f"sql: {text[:80]!r} is not a valid {kind.__name__} key", 404) from None
        return text

    def open(self, file: SourceFile) -> BinaryIO:
        if not file.external_id.startswith(self.scope):
            raise SqlSourceError(f"sql: {file.external_id!r} is not an object of this connection")
        key_text = file.external_id[len(self.scope):]
        engine = self._engine_for(self._url())
        with self._session(engine) as conn:
            key = self._quote(conn, self.key_column)
            key_value = self._parse_key(conn, key_text)
            single = self.group_by_rows == 1
            result = self._execute(conn, self._select(conn, [f"{key} = :key" if single else f"{key} >= :key"], order=not single),
                                   {"key": key_value})
            rows: list[dict] = []
            need = self.group_by_rows + (1 if single else 0)           # one extra row proves a "unique" key is not
            try:
                while len(rows) < need:
                    part = self._db("fetch", result.fetchmany, need - len(rows))
                    if not part:
                        break
                    columns = self._columns(result)
                    rows.extend(dict(zip(columns, raw)) for raw in part)
            finally:
                result.close()
        if single and len(rows) > 1:
            raise SqlSourceError(f"sql: key_column {self.key_column!r} is not unique (value {key_text[:80]!r} matches {len(rows)} rows)")
        if not rows:
            raise SqlSourceError(f"sql: {file.external_id!r} no longer exists in the query result", 404)
        text = self._render(rows)
        if len(text) > self.max_bytes:
            raise TooLarge(f"sql: {file.external_id!r} renders to {len(text)} bytes, more than max_file_bytes ({self.max_bytes})")
        out = tempfile.SpooledTemporaryFile(max_size=4 * 1024 * 1024)
        out.write(text)
        out.seek(0)
        return out                                 # type: ignore[return-value]

    def refresh_acl(self, file: SourceFile) -> list[str]:
        return self.rules.for_path(file.path)
