"""SQL source connector tests.

They run against a REAL PostgreSQL (the rag_engine_test database: a temp table, function, sequence and login role are
created per test and dropped afterwards; the connector connects with psycopg2 exactly as in production) and, for the
second backend, a real SQLite file. Failure injection is done in the database itself: a plpgsql function that
writes, `nextval()`, a backend killed with pg_terminate_backend, a wrong password for a real role.

Nothing here stubs SQLAlchemy or the DBAPI. The only fakes: the "unsupported dialect" test renames the dialect of a
real SQLite engine (the branch only looks at the name), and the "database did not confirm read-only" test makes the
real database undo the connector's `SET TRANSACTION READ ONLY` from a cursor event.
"""

import datetime as dt
import decimal
import itertools
import json
import os
import sqlite3
import threading
import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy import event

from app.scale.connectors._http import AuthError, HttpError, TooLarge
from app.scale.connectors._objects import short_hash
from app.scale.connectors.base import SourceFile
from app.scale.connectors.sql import SqlConnector, SqlSourceError, _dec, _enc, check_query

DSN = os.environ.get("POSTGRES_DSN", "postgresql://rag:change_me@localhost:5432/rag_engine_test")
UTC_URL = "?options=-c%20TimeZone%3DUTC%20-c%20application_name%3D{app}"
ROLE_PW = "sql-pw-MUST-NOT-LEAK-42"
BASE = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
SELECT = "SELECT id, title, body, amount, updated_at, flag, meta, note FROM {t}"


def admin_engine():
    return sa.create_engine(DSN.replace("postgresql://", "postgresql+psycopg2://", 1))


class Db:
    """A throw-away table in the test database plus a connector factory that records what the connector executes."""

    def __init__(self, engine, monkeypatch):
        self.engine, self.mp = engine, monkeypatch
        self.suffix = uuid.uuid4().hex[:10]
        self.table = f"sqlsrc_{self.suffix}"
        self.app = f"sqlsrc_{self.suffix}"
        self.now = 1_800_000_000.0
        self.engines: list[sa.Engine] = []
        self.statements: list[tuple[str, object]] = []
        self.extra_roles: list[str] = []
        self.extra_objects: list[str] = []
        monkeypatch.setenv("T_SQL_URL", DSN + UTC_URL.format(app=self.app))
        self.run(f"""CREATE TABLE {self.table} (id int PRIMARY KEY, title text, body text, amount numeric(10,2),
                     updated_at timestamptz, flag boolean, meta jsonb, note text, blob bytea, internal text, rev int DEFAULT 1)""")
        self.insert(range(1, 26))

    # -- database helpers --

    def run(self, sql, **params):
        with self.engine.begin() as conn:
            result = conn.execute(sa.text(sql), params)
            return result.fetchall() if result.returns_rows else None

    def insert(self, ids):
        rows = [
            {"id": i, "title": f"Title {i}", "body": f"first line {i}\nsecond line" if i % 5 == 0 else f"short body {i}",
             "amount": decimal.Decimal(i) * decimal.Decimal("1.5"), "updated_at": BASE + dt.timedelta(minutes=i),
             "flag": i % 2 == 0, "meta": json.dumps({"b": [1, 2], "a": i}), "note": None if i % 3 else f"note {i}"}
            for i in ids
        ]
        with self.engine.begin() as conn:
            conn.execute(sa.text(
                f"INSERT INTO {self.table} (id, title, body, amount, updated_at, flag, meta, note) "
                "VALUES (:id, :title, :body, :amount, :updated_at, :flag, CAST(:meta AS jsonb), :note)"), rows)

    def touch(self, i, minutes, **columns):
        sets = ", ".join(f"{c} = :{c}" for c in columns)
        self.run(f"UPDATE {self.table} SET {sets + ', ' if sets else ''}updated_at = :ts WHERE id = :id",
                 ts=BASE + dt.timedelta(minutes=minutes), id=i, **columns)

    # -- connectors --

    def config(self, **cfg):
        return {"url_env": "T_SQL_URL", "query": SELECT.format(t=self.table), "key_column": "id", "name": "orders",
                "title_column": "title", "default_acl": ["*"], **cfg}

    def connector(self, **cfg):
        def factory(url):
            engine = SqlConnector._default_engine(url)
            self.engines.append(engine)
            event.listen(engine, "before_cursor_execute",
                         lambda conn, cursor, statement, params, ctx, many: self.statements.append((statement, getattr(cursor, "name", None))))
            return engine

        return SqlConnector(self.config(**cfg), engine_factory=factory, clock=lambda: self.now)

    def pool_checked_out(self):
        return sum(e.pool.checkedout() for e in self.engines)

    def create_role(self, password=ROLE_PW):
        role = f"sqlsrc_role_{self.suffix}"
        self.run(f"CREATE ROLE {role} LOGIN PASSWORD '{password}'")
        self.run(f"GRANT SELECT ON {self.table} TO {role}")
        self.extra_roles.append(role)
        return role

    def url_for(self, role, password):
        return sa.make_url(DSN).set(username=role, password=password).render_as_string(hide_password=False)

    def drop(self):
        for e in self.engines:
            e.dispose()
        for sql in self.extra_objects:
            self.run(sql)
        self.run(f"DROP TABLE IF EXISTS {self.table}")
        for role in self.extra_roles:
            self.run(f"DROP OWNED BY {role}")
            self.run(f"DROP ROLE IF EXISTS {role}")


@pytest.fixture
def db(monkeypatch):
    engine = admin_engine()
    d = Db(engine, monkeypatch)
    yield d
    d.drop()
    engine.dispose()


def scan_all(connector, cursor=None):
    batches = list(itertools.islice(connector.scan(cursor or {}), 500))
    assert len(batches) < 500, "the scan never terminated"
    return [c for b in batches for c in b.changes], batches


def ids(changes):
    return [c.external_id.rsplit("/", 1)[1] for c in changes]


def versions(changes):
    return {c.external_id: c.file.version for c in changes if c.kind == "upsert"}


def read(connector, file):
    with connector.open(file) as fh:
        return fh.read()


# ── the query is checked before anything runs ──


@pytest.mark.parametrize("word", ["INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "CREATE", "TRUNCATE", "GRANT"])
def test_each_write_keyword_is_refused_as_a_whole_word_in_any_case(word):
    for text in (f"SELECT 1 {word}", f"select 1 {word.lower()}", f"SELECT 1 /* {word.title()} */",
                 f"WITH x AS ({word} INTO t VALUES (1) RETURNING 1) SELECT * FROM x", f"SELECT 1 WHERE 'a'='{word}'"):
        with pytest.raises(ValueError, match=word):
            check_query(text)


def test_words_that_merely_contain_a_keyword_and_other_valid_queries_pass():
    for text in ("SELECT updated_at, created_by, insert_date, dropdown, grants, deleted FROM t",
                 "  select * from t  ", "WITH a AS (SELECT 1) SELECT * FROM a", "SELECT x::int FROM t WHERE a LIKE 'u%'"):
        assert check_query(text) == text.strip()


@pytest.mark.parametrize("bad", ["", "   ", None, 5, "SELECT 1; SELECT 2", "SELECT 1;", "VALUES (1)", "EXPLAIN SELECT 1",
                                 "(SELECT 1)", "COPY t TO '/tmp/x'", "SELECT 1\x00"])
def test_other_statements_multiple_statements_and_junk_are_refused(bad):
    with pytest.raises(ValueError):
        check_query(bad)


def test_configuration_is_validated_when_the_connector_is_built():
    base = {"url_env": "X", "query": "SELECT 1 AS id", "key_column": "id"}
    SqlConnector(base)
    for patch, match in [({"url_env": ""}, "url_env"), ({"key_column": ""}, "key_column"), ({"key_column": "id; --"}, "plain column"),
                         ({"title_column": "a b"}, "title_column"), ({"query": "DROP TABLE t"}, "SELECT or WITH"),
                         ({"query": "SELECT 1 -- UPDATE"}, "UPDATE"), ({"name": "a/b"}, "folder name"), ({"group_by_rows": 0}, "at least 1"),
                         ({"updated_column": "u", "group_by_rows": 3}, "group_by_rows")]:
        with pytest.raises(ValueError, match=match):
            SqlConnector({**base, **patch})


# ── scanning ──


def test_every_row_is_a_virtual_markdown_file_with_exact_rendered_content(db):
    c = db.connector(updated_column="updated_at")
    changes, batches = scan_all(c)
    assert ids(changes) == [str(i) for i in range(1, 26)]                 # key order, numerically
    f = {x.external_id: x.file for x in changes}["sql:orders/7"]
    assert (f.path, f.name, f.mime, f.acl) == ("orders/7.md", "7.md", "text/markdown", ["*"])
    assert f.modified_at == BASE + dt.timedelta(minutes=7)                # only an updated_column gives a modification time
    assert all(x.file.modified_at is None for x in scan_all(db.connector())[0])
    expected = (
        "# Title 7\n\nid: 7\ntitle: Title 7\nbody: short body 7\namount: 10.50\nupdated_at: 2026-01-01T00:07:00+00:00\n"
        'flag: false\nmeta: {"a": 7, "b": [1, 2]}\n'
    )
    assert read(c, f) == expected.encode() and f.size == len(expected)
    five = {x.external_id: x.file for x in changes}["sql:orders/5"]
    assert read(c, five).decode() == (
        "# Title 5\n\nid: 5\ntitle: Title 5\namount: 7.50\nupdated_at: 2026-01-01T00:05:00+00:00\nflag: false\n"
        'meta: {"a": 5, "b": [1, 2]}\n\nbody:\nfirst line 5\nsecond line\n'
    )
    assert batches[-1].done and not any(b.done for b in batches[:-1])


def test_title_defaults_to_name_and_key_and_null_or_empty_values_are_left_out(db):
    db.run(f"UPDATE {db.table} SET note = '', title = NULL WHERE id = 3")
    c = db.connector(title_column=None)
    f = next(x.file for x in scan_all(c)[0] if x.external_id.endswith("/3"))
    text = read(c, f).decode()
    assert text.startswith("# orders 3\n") and "note:" not in text and "title:" not in text
    c2 = db.connector()                                                    # title_column set but the value is NULL
    assert read(c2, next(x.file for x in scan_all(c2)[0] if x.external_id.endswith("/3"))).decode().startswith("# orders 3\n")


def test_batches_follow_batch_size_and_every_cursor_is_json(db):
    c = db.connector(batch_size=10, fetch_size=4)
    changes, batches = scan_all(c)
    assert [len(b.changes) for b in batches] == [10, 10, 5]
    assert [b.done for b in batches] == [False, False, True] and [b.complete_scope for b in batches] == [None, None, "sql:orders/"]
    assert batches[0].cursor["after"] == ["int", 10] and batches[1].cursor["after"] == ["int", 20]
    dumped = json.dumps([b.cursor for b in batches])
    assert "postgresql" not in dumped and "change_me" not in dumped and "T_SQL_URL" not in dumped
    assert json.loads(dumped) == [b.cursor for b in batches]


def test_rows_are_fetched_through_a_server_side_cursor(db):
    scan_all(db.connector(fetch_size=3))
    data = [(sql, name) for sql, name in db.statements if "_src" in sql]
    assert len(data) == 1 and data[0][1], "the data query must run on a named (server-side) cursor"
    assert "ORDER BY id" in data[0][0] and "id IS NOT NULL" in data[0][0]


def test_resume_from_every_batch_boundary_is_exact_and_never_authoritative(db):
    c = db.connector(batch_size=4)
    everything, batches = scan_all(c)
    assert len(batches) == 7
    for k in range(len(batches) - 1):
        head = [x for b in batches[: k + 1] for x in ids(b.changes)]
        resumed, rb = scan_all(db.connector(batch_size=4), json.loads(json.dumps(batches[k].cursor)))   # through JSON, fresh connector
        assert head + ids(resumed) == ids(everything), f"resume after batch {k}"
        assert rb[-1].complete_scope is None


def test_a_connection_killed_mid_scan_fails_clearly_releases_the_pool_and_the_cursor_resumes(db):
    c = db.connector(batch_size=5, fetch_size=5)
    stream = c.scan({})
    first = next(stream)
    assert len(first.changes) == 5
    killed = db.run("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE application_name = :a AND pid <> pg_backend_pid()", a=db.app)
    assert killed and all(row[0] for row in killed)
    with pytest.raises(SqlSourceError):
        list(stream)
    assert db.pool_checked_out() == 0
    rest, rb = scan_all(db.connector(batch_size=5), first.cursor)
    assert ids(first.changes) + ids(rest) == [str(i) for i in range(1, 26)] and rb[-1].complete_scope is None


def test_abandoning_a_scan_returns_the_connection_to_the_pool(db):
    stream = db.connector(batch_size=2).scan({})
    next(stream)
    assert db.pool_checked_out() == 1
    stream.close()
    assert db.pool_checked_out() == 0


def test_rows_deleted_at_the_source_are_missing_from_an_authoritative_scan(db):
    c = db.connector()
    indexed = {x.external_id for x in scan_all(c)[0]}
    db.run(f"DELETE FROM {db.table} WHERE id IN (3, 4)")
    changes, batches = scan_all(c)
    seen = {x.external_id for x in changes}
    scope = batches[-1].complete_scope
    assert scope == "sql:orders/" and {e for e in indexed - seen if e.startswith(scope)} == {"sql:orders/3", "sql:orders/4"}
    assert all(e.startswith(scope) for e in indexed)


def test_version_changes_only_when_the_rendered_content_changes(db):
    c = db.connector()
    before = versions(scan_all(c)[0])
    assert before == versions(scan_all(c)[0])
    db.run(f"UPDATE {db.table} SET title = 'Renamed' WHERE id = 2")
    after = versions(scan_all(c)[0])
    assert {k for k in after if after[k] != before[k]} == {"sql:orders/2"}
    db.run(f"UPDATE {db.table} SET title = 'Renamed' WHERE id = 2")        # same value again
    assert versions(scan_all(c)[0]) == after
    db.run(f"UPDATE {db.table} SET internal = 'not in the query', blob = '\\x00ff' WHERE id = 9")   # columns the query does not select
    assert versions(scan_all(c)[0]) == after
    db.run(f"UPDATE {db.table} SET body = 'x' WHERE id = 9")
    assert {k for k, v in versions(scan_all(c)[0]).items() if v != after[k]} == {"sql:orders/9"}


def test_version_is_the_hash_of_path_and_the_bytes_open_returns(db):
    c = db.connector()
    for change in scan_all(c)[0]:
        assert short_hash(change.file.path, read(c, change.file), size=16) == change.file.version


def test_max_rows_caps_the_scan_and_a_capped_scan_is_not_authoritative(db):
    c = db.connector(max_rows=7)
    changes, batches = scan_all(c)
    assert ids(changes) == [str(i) for i in range(1, 8)] and batches[-1].complete_scope is None and c.stats["truncated"] == 1
    exact, rb = scan_all(db.connector(max_rows=25))
    assert len(exact) == 25 and rb[-1].complete_scope == "sql:orders/"      # exactly the cap is not "more than"


def test_oversize_documents_are_skipped_and_reported_as_deletes_and_long_cells_are_cut(db):
    db.run(f"UPDATE {db.table} SET body = repeat('x', 50000) WHERE id = 6")
    c = db.connector(max_file_bytes=5000)
    changes, _ = scan_all(c)
    by = {x.external_id: x for x in changes}
    assert by["sql:orders/6"].kind == "delete" and by["sql:orders/6"].file is None and c.stats["oversize"] == 1
    assert len([x for x in changes if x.kind == "upsert"]) == 24
    with pytest.raises(TooLarge):
        read(c, SourceFile("sql:orders/6", "orders/6.md", "6.md", 1, None, "v", []))
    cut = db.connector(max_cell_chars=100)
    f = next(x.file for x in scan_all(cut)[0] if x.external_id.endswith("/6"))
    text = read(cut, f).decode()
    assert "x" * 100 + " ... [cut: 49900 more characters]" in text and f.size < 600 and cut.stats["cut_cells"] >= 1


def test_colons_and_percent_signs_in_the_query_are_literal(db):
    db.run(f"UPDATE {db.table} SET note = :n WHERE id <= 2", n="12:30 :x ::y")
    c = db.connector(title_column=None, query=f"SELECT id, note, id::text || ':' AS tag FROM {db.table} WHERE (note LIKE '12:%' OR title LIKE 'Title 1%') AND coalesce(note, '') <> 'skip :me'")
    changes, _ = scan_all(c)
    assert ids(changes)[:3] == ["1", "2", "10"]
    assert "note: 12:30 :x ::y" in read(c, changes[0].file).decode() and "tag: 1:" in read(c, changes[0].file).decode()


# ── incremental scans ──


def test_incremental_scan_reads_only_changed_rows_and_moves_the_watermark(db):
    c = db.connector(updated_column="updated_at")
    full, batches = scan_all(c)
    cursor = batches[-1].cursor
    assert len(full) == 25 and batches[-1].complete_scope == "sql:orders/" and cursor["last_full"] == db.now
    assert cursor["since"] == ["dt", (BASE + dt.timedelta(minutes=25)).isoformat()]

    db.now += 60
    none, nb = scan_all(c, json.loads(json.dumps(cursor)))
    assert none == [] and nb[-1].complete_scope is None and nb[-1].cursor["since"] == cursor["since"]
    assert nb[-1].cursor["last_full"] == cursor["last_full"]

    db.touch(2, 100, title="Edited 2")
    db.touch(9, 90, title="Edited 9")
    db.insert([26])
    db.run(f"UPDATE {db.table} SET updated_at = :ts WHERE id = 26", ts=BASE + dt.timedelta(minutes=95))
    changes, ib = scan_all(c, cursor)
    assert ids(changes) == ["2", "9", "26"] and ib[-1].complete_scope is None
    assert ib[-1].cursor["since"] == ["dt", (BASE + dt.timedelta(minutes=100)).isoformat()]
    assert "updated_at > %(since)s" in [s for s, _ in db.statements if "ORDER BY" in s][-1]        # the filter ran in the database
    assert "Edited 2" in read(c, changes[0].file).decode()


def test_incremental_scan_cannot_see_deletes_until_the_next_full_pass(db):
    c = db.connector(updated_column="updated_at", full_resync_hours=24)
    cursor = scan_all(c)[1][-1].cursor
    db.run(f"DELETE FROM {db.table} WHERE id = 5")
    db.now += 3600
    changes, batches = scan_all(c, cursor)
    assert changes == [] and batches[-1].complete_scope is None           # not noticed, and not claimed
    db.now += 24 * 3600                                                    # the periodic full pass
    changes, batches = scan_all(c, cursor)
    assert len(changes) == 24 and "5" not in ids(changes) and batches[-1].complete_scope == "sql:orders/"
    assert batches[-1].cursor["last_full"] == db.now


def test_resuming_an_incremental_scan_keeps_the_filter_and_the_watermark_seen_before_the_crash(db):
    c = db.connector(updated_column="updated_at", batch_size=2)
    cursor = scan_all(c)[1][-1].cursor
    for i in (1, 4, 6, 8, 10):
        db.touch(i, 200 + i, title=f"E{i}")
    stream = c.scan(cursor)
    first = next(stream)
    stream.close()
    assert ids(first.changes) == ["1", "4"] and first.cursor["mode"] == "inc" and first.cursor["since"] == cursor["since"]
    rest, rb = scan_all(db.connector(updated_column="updated_at", batch_size=2), first.cursor)
    assert ids(rest) == ["6", "8", "10"] and rb[-1].complete_scope is None
    assert rb[-1].cursor["since"] == ["dt", (BASE + dt.timedelta(minutes=210)).isoformat()]


def test_overlap_rereads_rows_committed_late_behind_the_watermark(db):
    cursor = scan_all(db.connector(updated_column="updated_at"))[1][-1].cursor
    db.run(f"UPDATE {db.table} SET title = 'late', updated_at = :ts WHERE id = 3", ts=BASE + dt.timedelta(minutes=25, seconds=-6))   # committed late
    assert scan_all(db.connector(updated_column="updated_at"), cursor)[0] == []
    assert ids(scan_all(db.connector(updated_column="updated_at", overlap_seconds=10), cursor)[0]) == ["3", "25"]    # + the boundary row


def test_integer_version_counters_work_as_the_updated_column(db):
    c = db.connector(updated_column="rev", query=f"SELECT id, title, rev FROM {db.table}")
    cursor = scan_all(c)[1][-1].cursor
    assert cursor["since"] == ["int", 1]
    db.run(f"UPDATE {db.table} SET rev = 7 WHERE id IN (4, 5)")
    changes, batches = scan_all(c, cursor)
    assert ids(changes) == ["4", "5"] and batches[-1].cursor["since"] == ["int", 7]
    assert all(x.file.modified_at is None for x in changes)               # a counter is not a time


def test_typed_watermarks_survive_json_and_a_damaged_cursor_means_a_fresh_full_scan(db):
    for value in (5, 2.5, decimal.Decimal("1.50"), BASE, BASE.date(), uuid.UUID(int=7), "text", dt.datetime(2026, 1, 1, 12, 0)):
        assert _dec(json.loads(json.dumps(_enc(value)))) == value
    c = db.connector(updated_column="updated_at")
    for junk in ({"mode": "full", "after": "garbage"}, {"mode": "inc", "since": ["nope", 1]}, {"since": 5}, {"mode": "weird"}, None):
        changes, batches = scan_all(c, junk)
        assert len(changes) == 25 and batches[-1].complete_scope == "sql:orders/"
    plain = db.connector()
    changes, batches = scan_all(plain, {"mode": "inc", "since": ["int", 3], "after": ["int", 20]})    # cursor of another configuration
    assert len(changes) == 25 and batches[-1].complete_scope == "sql:orders/"


# ── groups of rows ──


def test_group_by_rows_makes_one_document_per_group_and_groups_never_straddle_a_batch(db):
    c = db.connector(group_by_rows=4, batch_size=2, query=f"SELECT id, title, amount FROM {db.table} WHERE id <= 10")
    changes, batches = scan_all(c)
    assert ids(changes) == ["1", "5", "9"] and [len(b.changes) for b in batches] == [2, 1]
    assert batches[0].cursor["after"] == ["int", 8]                       # the last row of the last finished group
    by = {x.external_id: x.file for x in changes}
    assert by["sql:orders/1"].path == "orders/1..4.md" and by["sql:orders/9"].path == "orders/9..10.md"
    assert by["sql:orders/1"].extra == {"key": "1", "rows": 4} and by["sql:orders/9"].extra["rows"] == 2
    text = read(c, by["sql:orders/5"]).decode()
    assert text == (
        "# orders: 5 to 8\n\nrows: 4\nfirst id: 5\nlast id: 8\n\n"
        "## Title 5\n\nid: 5\ntitle: Title 5\namount: 7.50\n\n## Title 6\n\nid: 6\ntitle: Title 6\namount: 9.00\n\n"
        "## Title 7\n\nid: 7\ntitle: Title 7\namount: 10.50\n\n## Title 8\n\nid: 8\ntitle: Title 8\namount: 12.00\n"
    )
    resumed, rb = scan_all(db.connector(group_by_rows=4, batch_size=2, query=c.cfg["query"]), batches[0].cursor)
    assert ids(resumed) == ["9"] and read(c, resumed[0].file) == read(c, by["sql:orders/9"]) and rb[-1].complete_scope is None


def test_removing_a_row_changes_its_group_and_the_groups_after_it(db):
    cfg = {"group_by_rows": 4, "query": f"SELECT id, title FROM {db.table} WHERE id <= 10"}
    c = db.connector(**cfg)
    before = versions(scan_all(c)[0])
    db.run(f"DELETE FROM {db.table} WHERE id = 6")
    changes, batches = scan_all(c)
    after = versions(changes)
    assert set(before) - set(after) == {"sql:orders/9"} and set(after) - set(before) == {"sql:orders/10"}
    assert after["sql:orders/1"] == before["sql:orders/1"] and after["sql:orders/5"] != before["sql:orders/5"]
    assert batches[-1].complete_scope == "sql:orders/"                    # so the vanished group id is deleted by the sync layer
    assert read(c, changes[1].file).decode().count("## ") == 4            # open() sees the same grouping as the scan


# ── permissions ──


def test_acl_rules_longest_prefix_wins_and_everything_else_is_unreadable(db):
    rules = [{"prefix": "orders", "principals": ["group:Staff"]}, {"prefix": "orders/1*", "principals": ["group:Boss", "user:Ann@x.io"]}]
    c = db.connector(default_acl=None, acl_rules=rules)
    by = {x.external_id: x.file for x in scan_all(c)[0]}
    assert by["sql:orders/2"].acl == ["group:staff"]                                         # the folder rule
    assert by["sql:orders/12"].acl == ["group:boss", "user:ann@x.io"]                       # the longer rule wins, principals normalised
    assert c.refresh_acl(by["sql:orders/2"]) == ["group:staff"] and c.refresh_acl(by["sql:orders/12"]) == ["group:boss", "user:ann@x.io"]
    other = db.connector(default_acl=None, name="elsewhere", acl_rules=rules)                 # no rule covers this folder: nobody
    assert all(x.file.acl == [] for x in scan_all(other)[0])
    plain = db.connector(default_acl=None)                                                    # no rules, no default: nobody
    assert all(x.file.acl == [] for x in scan_all(plain)[0]) and plain.refresh_acl(by["sql:orders/2"]) == []
    open_to_all = db.connector(default_acl=["*"], acl_rules=[{"prefix": "orders/2*", "principals": ["group:two"]}])
    got = {x.external_id: x.file.acl for x in scan_all(open_to_all)[0]}
    assert got["sql:orders/20"] == ["group:two"] and got["sql:orders/3"] == ["*"]


# ── open() ──


def test_open_returns_the_exact_rendered_bytes_of_a_fresh_connector_for_every_value_type(db):
    db.run(f"UPDATE {db.table} SET title = :t, note = 'a: b', blob = :b, body = :body WHERE id = 11",
           t="Ünï — 日本語 100% /\\: #", b=b"\x00\xff\x10", body="one\r\ntwo\n\nthree")
    query = f"SELECT id, title, body, note, amount, flag, meta, blob, updated_at::date AS day FROM {db.table}"
    scanner = db.connector(query=query)
    changes, _ = scan_all(scanner)
    fresh = db.connector(query=query)                                        # never scanned: learns the key type itself
    for change in changes:
        assert read(fresh, change.file) == read(scanner, change.file)
    text = read(fresh, next(x.file for x in changes if x.external_id.endswith("/11"))).decode()
    assert text == (
        "# Ünï — 日本語 100% /\\: #\n\nid: 11\ntitle: Ünï — 日本語 100% /\\: #\nnote: a: b\namount: 16.50\nflag: false\n"
        'meta: {"a": 11, "b": [1, 2]}\nblob: <3 bytes of binary data>\nday: 2026-01-01\n\nbody:\none\ntwo\n\nthree\n'
    )
    assert next(x.file for x in changes if x.external_id.endswith("/11")).path == "orders/11.md"


def test_open_of_a_missing_row_a_foreign_id_or_a_malformed_key_is_a_clear_error(db):
    c = db.connector()
    for external_id, status in (("sql:orders/999", 404), ("sql:orders/abc", 404), ("sql:orders/", 404), ("sql:other/1", None), ("local/1", None)):
        with pytest.raises(SqlSourceError) as err:
            c.open(SourceFile(external_id, "p", "n", 1, None, "v", []))
        assert err.value.status == status
    f = next(x.file for x in scan_all(c)[0] if x.external_id.endswith("/5"))
    db.run(f"DELETE FROM {db.table} WHERE id = 5")
    with pytest.raises(SqlSourceError) as err:
        c.open(f)
    assert err.value.status == 404 and "no longer exists" in str(err.value)


def test_concurrent_opens_each_get_their_own_rows(db):
    c = db.connector()
    files = {x.external_id: x.file for x in scan_all(c)[0]}
    expected = {e: read(c, f) for e, f in files.items()}
    got, errors = {}, []

    def worker(chunk):
        try:
            for e in chunk:
                got[e] = read(c, files[e])
        except Exception as exc:                                         # noqa: BLE001
            errors.append(exc)

    keys = sorted(files)
    threads = [threading.Thread(target=worker, args=(keys[i::5],)) for i in range(5)]
    [t.start() for t in threads]
    [t.join(30) for t in threads]
    assert not errors and got == expected and db.pool_checked_out() == 0


def test_open_of_a_non_unique_key_is_refused_instead_of_picking_a_row(db):
    c = db.connector(query=f"SELECT id % 5 AS id, title FROM {db.table}")
    with pytest.raises(SqlSourceError, match="not unique"):
        c.open(SourceFile("sql:orders/2", "p", "n", 1, None, "v", []))


# ── keys of other types ──


def test_text_uuid_and_date_keys_scan_resume_and_open(db):
    ns = uuid.UUID(int=0xABC0000000000000000000000000000)
    db.run(f"ALTER TABLE {db.table} ADD COLUMN code text, ADD COLUMN guid uuid, ADD COLUMN day date")
    for i in range(1, 26):
        db.run(f"UPDATE {db.table} SET code = :c, guid = :g, day = :d WHERE id = :i",
               c=f"sku/{i:02d} 100%" if i % 2 else f"Sku-é{i:02d}", g=str(uuid.UUID(int=ns.int + i * 7919)), d=dt.date(2026, 1, 1) + dt.timedelta(days=i), i=i)
    for column in ("code", "guid", "day"):
        query = f"SELECT {column}, title FROM {db.table}"
        c = db.connector(query=query, key_column=column, batch_size=6)
        changes, batches = scan_all(c)
        in_db_order = [f"sql:orders/{r[0].isoformat() if hasattr(r[0], 'isoformat') else r[0]}"
                       for r in db.run(f"SELECT {column} FROM {db.table} ORDER BY {column}")]
        assert [x.external_id for x in changes] == in_db_order and len(set(in_db_order)) == 25
        cursor = json.loads(json.dumps(batches[1].cursor))                                # 12 keys done, through JSON
        resumed, rb = scan_all(db.connector(query=query, key_column=column, batch_size=6), cursor)
        assert [x.external_id for x in resumed] == in_db_order[12:] and rb[-1].complete_scope is None
        fresh = db.connector(query=query, key_column=column)
        for change in (changes[0], changes[13], changes[-1]):
            assert read(fresh, change.file) == read(c, change.file) and column in read(fresh, change.file).decode()
    # a path that is a safe file name even when the key is not
    c = db.connector(query=f"SELECT code, title FROM {db.table}", key_column="code")
    paths = {x.external_id: x.file.path for x in scan_all(c)[0]}
    assert paths["sql:orders/sku/01 100%"] == "orders/sku 01 100%.md"


def test_rows_with_a_null_key_are_ignored_and_a_repeated_key_aborts_the_scan(db):
    c = db.connector(query=f"SELECT CASE WHEN id = 4 THEN NULL ELSE id END AS id, title FROM {db.table}")
    changes, batches = scan_all(c)
    assert len(changes) == 24 and "4" not in ids(changes) and batches[-1].complete_scope == "sql:orders/"
    dup = db.connector(query=f"SELECT id / 2 AS id, title FROM {db.table}", batch_size=3)
    with pytest.raises(SqlSourceError, match="not unique"):
        scan_all(dup)
    assert db.pool_checked_out() == 0


def test_a_key_or_title_column_that_is_not_in_the_result_names_the_columns(db):
    for patch in ({"updated_column": "ghost"}, {"title_column": "missing"}):
        with pytest.raises(SqlSourceError, match=r"is not a column of the query \(columns: id, title"):
            scan_all(db.connector(query=f"SELECT id, title FROM {db.table}", **patch))
    with pytest.raises(SqlSourceError, match=r'column "nope" does not exist.*must name a column that the query returns'):
        scan_all(db.connector(query=f"SELECT id, title FROM {db.table}", key_column="nope"))


# ── the database enforces read-only ──


def test_a_function_that_writes_cannot_write_through_the_connector(db):
    db.run("CREATE TABLE {}_log (n int)".format(db.table))
    db.run(f"CREATE FUNCTION {db.table}_bump(i int) RETURNS int LANGUAGE plpgsql AS $f$ BEGIN INSERT INTO {db.table}_log VALUES (i); RETURN i; END $f$")
    db.extra_objects += [f"DROP FUNCTION IF EXISTS {db.table}_bump(int)", f"DROP TABLE IF EXISTS {db.table}_log"]
    query = f"SELECT id, {db.table}_bump(id) AS bumped FROM {db.table}"
    check_query(query)                                                      # the keyword check cannot see the write
    with pytest.raises(SqlSourceError) as err:
        scan_all(db.connector(query=query))
    assert "read-only transaction" in str(err.value) and "25006" in str(err.value)
    assert db.run(f"SELECT count(*) FROM {db.table}_log")[0][0] == 0
    assert db.run(f"SELECT {db.table}_bump(1)")[0][0] == 1 and db.run(f"SELECT count(*) FROM {db.table}_log")[0][0] == 1   # control: it does write
    with pytest.raises(SqlSourceError, match="read-only"):
        db.connector(query=query).open(SourceFile("sql:orders/1", "p", "n", 1, None, "v", []))
    assert db.run(f"SELECT count(*) FROM {db.table}_log")[0][0] == 1


def test_nextval_is_refused_too(db):
    db.run(f"CREATE SEQUENCE {db.table}_seq")
    db.extra_objects.append(f"DROP SEQUENCE IF EXISTS {db.table}_seq")
    with pytest.raises(SqlSourceError, match="nextval"):
        scan_all(db.connector(query=f"SELECT id, nextval('{db.table}_seq') AS n FROM {db.table}"))
    assert db.run(f"SELECT is_called FROM {db.table}_seq")[0][0] is False


def test_connections_start_read_only_and_keep_the_options_already_in_the_url(db):
    c = db.connector()
    scan_all(c)
    with db.engines[0].connect() as conn:
        assert conn.exec_driver_sql("SHOW default_transaction_read_only").scalar() == "on"
        assert conn.exec_driver_sql("SHOW TimeZone").scalar() == "UTC"                 # from the URL's own options
        assert conn.exec_driver_sql("SHOW application_name").scalar() == db.app
    transaction_modes = [s for s, _ in db.statements if s.startswith(("SET TRANSACTION", "SHOW transaction"))]
    assert transaction_modes == ["SET TRANSACTION READ ONLY", "SHOW transaction_read_only"]


def test_a_transaction_the_database_did_not_make_read_only_is_refused(db):
    c = db.connector()
    original = c._factory

    def undo(url):
        engine = original(url)

        @event.listens_for(engine, "after_cursor_execute")
        def _(conn, cursor, statement, params, context, executemany):
            if statement == "SET TRANSACTION READ ONLY":
                cursor.execute("SET TRANSACTION READ WRITE")                      # something re-enabled writes
        return engine

    c._factory = undo
    with pytest.raises(SqlSourceError, match="did not put the transaction in read-only mode"):
        scan_all(c)
    assert db.pool_checked_out() == 0


def test_a_role_with_only_select_works_and_a_wrong_password_is_an_auth_error_that_does_not_leak(db, monkeypatch):
    role = db.create_role()
    monkeypatch.setenv("T_SQL_URL", db.url_for(role, ROLE_PW))
    c = db.connector()
    assert len(scan_all(c)[0]) == 25
    monkeypatch.setenv("T_SQL_URL", db.url_for(role, "WRONG-" + ROLE_PW))             # read at call time: the same connector now fails
    with pytest.raises(AuthError) as err:
        scan_all(c)
    blob = str(err.value) + repr(err.value) + repr(err.value.__cause__) + repr(err.value.__context__ and str(err.value.__context__))
    assert "WRONG-" not in blob and ROLE_PW not in blob and "postgresql://" not in blob and "T_SQL_URL" in str(err.value)
    monkeypatch.setenv("T_SQL_URL", db.url_for(role, ROLE_PW))
    assert len(scan_all(c)[0]) == 25                                                # rotation picked up without a new connector
    with pytest.raises(SqlSourceError) as err:                                      # a role may not read what it was not granted
        scan_all(db.connector(query="SELECT 1 AS id FROM pg_authid"))
    assert ROLE_PW not in str(err.value) and "permission denied" in str(err.value)


def test_the_password_is_scrubbed_even_from_an_error_message_that_the_database_wrote(db, monkeypatch):
    role = db.create_role()
    monkeypatch.setenv("T_SQL_URL", db.url_for(role, ROLE_PW))
    c = db.connector(query=f"SELECT id, CAST('{ROLE_PW}' AS int) AS n FROM {db.table}")
    with pytest.raises(SqlSourceError) as err:
        scan_all(c)
    assert "invalid input syntax for type integer" in str(err.value) and ROLE_PW not in str(err.value) + repr(err.value) and "***" in str(err.value)


def test_missing_unusable_and_unreachable_urls_fail_clearly_without_echoing_them(db, monkeypatch):
    monkeypatch.delenv("T_SQL_URL")
    c = db.connector()
    with pytest.raises(HttpError, match="T_SQL_URL"):
        scan_all(c)
    assert db.engines == []                                                          # nothing was even attempted
    monkeypatch.setenv("T_SQL_URL", "not-a-url SECRET-in-a-bad-url")
    with pytest.raises(SqlSourceError) as err:
        scan_all(c)
    assert "SECRET" not in str(err.value) and "T_SQL_URL" in str(err.value)
    monkeypatch.setenv("T_SQL_URL", "nosuchdialect://u:SECRET-pw@h/db")
    with pytest.raises(SqlSourceError) as err:
        scan_all(c)
    assert "SECRET" not in str(err.value) + repr(err.value)
    monkeypatch.setenv("T_SQL_URL", "postgresql+psycopg2://u:SECRET-pw@127.0.0.1:1/db?connect_timeout=2")
    with pytest.raises(SqlSourceError) as err:
        scan_all(c)
    assert "SECRET" not in str(err.value) + repr(err.value) and "refused" in str(err.value).lower()
    monkeypatch.setenv("T_SQL_URL", "mysql+nosuchdriver://u:SECRET-pw@h/db")
    with pytest.raises(SqlSourceError) as err:
        scan_all(c)
    assert "SECRET" not in str(err.value) + repr(err.value)


def test_a_syntax_error_in_the_query_is_reported_without_the_url_or_the_sql_parameters(db):
    c = db.connector(query=f"SELECT id FROM {db.table} WHERE", batch_size=2)
    with pytest.raises(SqlSourceError) as err:
        scan_all(c)
    message = str(err.value)
    assert "syntax error" in message and "change_me" not in message and "postgresql://" not in message and "[parameters" not in message
    assert db.pool_checked_out() == 0


# ── SQLite, a second real backend ──


@pytest.fixture
def lite(tmp_path, monkeypatch):
    path = tmp_path / "src.db"
    con = sqlite3.connect(path)
    con.executescript("CREATE TABLE t (id INTEGER PRIMARY KEY, title TEXT, v INTEGER); CREATE TABLE log (n INTEGER);")
    con.executemany("INSERT INTO t VALUES (?, ?, ?)", [(i, f"Row {i}", 1) for i in range(1, 8)])
    con.commit()
    con.close()
    monkeypatch.setenv("T_LITE_URL", f"sqlite:///{path}")
    return path


def lite_connector(**cfg):
    def factory(url):
        engine = SqlConnector._default_engine(url)

        @event.listens_for(engine, "connect")
        def _(dbapi, record):
            def bump(i):
                dbapi.execute("INSERT INTO log VALUES (?)", (i,))
                return i
            dbapi.create_function("bump", 1, bump)
        return engine

    return SqlConnector({"url_env": "T_LITE_URL", "query": "SELECT id, title, v FROM t", "key_column": "id", "name": "lite",
                         "default_acl": ["*"], **cfg}, engine_factory=factory)


def test_sqlite_scan_batches_resume_delete_and_open(lite):
    c = lite_connector(batch_size=3, fetch_size=2, updated_column="v")
    changes, batches = scan_all(c)
    assert ids(changes) == [str(i) for i in range(1, 8)] and [len(b.changes) for b in batches] == [3, 3, 1]
    assert batches[-1].complete_scope == "sql:lite/" and batches[-1].cursor["since"] == ["int", 1]
    resumed, rb = scan_all(lite_connector(batch_size=3), batches[0].cursor)
    assert ids(resumed) == ["4", "5", "6", "7"] and rb[-1].complete_scope is None
    assert read(c, changes[2].file) == b"# lite 3\n\nid: 3\ntitle: Row 3\nv: 1\n"
    con = sqlite3.connect(lite)
    con.execute("DELETE FROM t WHERE id = 2")
    con.execute("UPDATE t SET title = 'New', v = 2 WHERE id = 5")
    con.commit()
    con.close()
    again = scan_all(c)[0]
    assert "2" not in ids(again) and versions(again)["sql:lite/5"] != versions(changes)["sql:lite/5"]
    inc, ib = scan_all(c, {**batches[-1].cursor, "last_full": c.clock()})
    assert ids(inc) == ["5"] and ib[-1].complete_scope is None and ib[-1].cursor["since"] == ["int", 2]


def test_sqlite_function_that_writes_is_blocked_by_query_only(lite):
    con = sqlite3.connect(lite)
    con.create_function("bump", 1, lambda i: con.execute("INSERT INTO log VALUES (?)", (i,)) and i)
    assert con.execute("SELECT bump(1)").fetchone() == (1,) and con.execute("SELECT count(*) FROM log").fetchone() == (1,)   # control
    con.execute("DELETE FROM log")
    con.commit()
    con.close()
    with pytest.raises(SqlSourceError, match="user-defined function raised exception"):     # sqlite hides the cause: the log is the proof
        scan_all(lite_connector(query="SELECT id, bump(id) AS b FROM t"))
    con = sqlite3.connect(lite)
    assert con.execute("SELECT count(*) FROM log").fetchone() == (0,)
    con.close()


def test_a_dialect_that_cannot_be_made_read_only_is_refused_unless_explicitly_accepted(lite):
    def renamed(url):
        engine = SqlConnector._default_engine(url)
        engine.dialect.name = "mysql"
        return engine

    cfg = {"url_env": "T_LITE_URL", "query": "SELECT id, title FROM t", "key_column": "id"}
    with pytest.raises(SqlSourceError, match="cannot make a mysql transaction read-only"):
        scan_all(SqlConnector(cfg, engine_factory=renamed))
    changes, _ = scan_all(SqlConnector({**cfg, "allow_unenforced_read_only": True}, engine_factory=renamed))
    assert len(changes) == 7
