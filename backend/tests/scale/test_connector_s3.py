"""S3 connector tests.

These run against a REAL S3 protocol server: moto's ThreadedMotoServer on a local port, driven by real boto3
(real SigV4 signing, real XML, real ListObjectsV2 pagination and key encoding). The throttling and
authentication-failure paths use botocore `before-send` hooks that hand back genuine HTTP 503 / 403 XML
responses, because moto does not throttle or check credentials by itself.
"""

import itertools
import json
import os
import uuid

import boto3
import pytest
from botocore.awsrequest import AWSResponse
from botocore.exceptions import ConnectionClosedError  # noqa: F401  (documented transient type)
from moto.server import ThreadedMotoServer
from sqlalchemy import text

from app.scale import sync
from app.scale.db import engine
from app.scale.connectors._http import AuthError, HttpError, TooLarge
from app.scale.connectors.s3 import S3Connector

ACCESS_KEY = "AKIATESTACCESSKEY1"
SECRET_KEY = "s3cr3t-VALUE-must-never-leak-9f8e7d"


@pytest.fixture(scope="module")
def endpoint():
    server = ThreadedMotoServer(ip_address="127.0.0.1", port=0, verbose=False)
    server.start()
    host, port = server.get_host_and_port()
    yield f"http://{host}:{port}"
    server.stop()


@pytest.fixture(autouse=True)
def creds(monkeypatch):
    monkeypatch.setenv("T_S3_KEY", ACCESS_KEY)
    monkeypatch.setenv("T_S3_SECRET", SECRET_KEY)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")


@pytest.fixture
def s3(endpoint):
    return boto3.client("s3", endpoint_url=endpoint, aws_access_key_id=ACCESS_KEY, aws_secret_access_key=SECRET_KEY,
                        region_name="us-east-1")


@pytest.fixture
def bucket(s3):
    name = f"b-{uuid.uuid4().hex[:12]}"
    s3.create_bucket(Bucket=name)
    return name


def put(s3, bucket, key, body=b"x", **kw):
    s3.put_object(Bucket=bucket, Key=key, Body=body, **kw)


def make(endpoint, bucket, **cfg):
    sleeps: list[float] = []
    config = {"bucket": bucket, "endpoint_url": endpoint, "region": "us-east-1", "access_key_env": "T_S3_KEY",
              "secret_key_env": "T_S3_SECRET", "default_acl": ["*"], **cfg}
    return S3Connector(config, sleep=sleeps.append), sleeps


def scan_all(connector, cursor=None):
    batches = list(itertools.islice(connector.scan(cursor or {}), 200))
    assert len(batches) < 200, "the scan never terminated"        # a broken pagination loop must fail, not hang
    return [c for b in batches for c in b.changes], batches


def ids(changes):
    return [c.external_id for c in changes]


class Hook:
    """A botocore before-send hook that answers the first `times` calls of one operation with a canned
    HTTP error response and lets everything after that through to the real server."""

    def __init__(self, client, operation, status, code, message="slow down", times=2, headers=None):
        self.status, self.code, self.message, self.times, self.headers = status, code, message, times, headers or {}
        self.calls = 0
        client.meta.events.register(f"before-send.s3.{operation}", self)

    def __call__(self, request, **kwargs):
        self.calls += 1
        if self.calls > self.times:
            return None
        body = f"<Error><Code>{self.code}</Code><Message>{self.message}</Message></Error>".encode()

        class Raw:
            def stream(self, *a, **k):
                yield body

        return AWSResponse(request.url, self.status, {"Content-Type": "application/xml", **self.headers}, Raw())


# ── listing, pagination, resume ──


def test_lists_every_object_across_pages_using_continuation_tokens(endpoint, bucket, s3):
    for i in range(7):
        put(s3, bucket, f"docs/file{i}.txt", f"body {i}".encode())
    c, _ = make(endpoint, bucket, page_size=3)
    seen_urls = []
    c._s3().meta.events.register("before-send.s3.ListObjectsV2", lambda request, **k: seen_urls.append(request.url))

    changes, batches = scan_all(c)

    assert ids(changes) == [f"docs/file{i}.txt" for i in range(7)]
    assert [len(b.changes) for b in batches] == [3, 3, 1]
    assert [b.done for b in batches] == [False, False, True]
    assert len(seen_urls) == 3 and "continuation-token" in seen_urls[1] and "continuation-token" not in seen_urls[0]
    assert batches[0].cursor == {"after": "docs/file2.txt"} and batches[1].cursor == {"after": "docs/file5.txt"}
    assert batches[-1].cursor == {} and batches[-1].complete_scope == ""
    first = changes[0].file
    assert (first.name, first.size, first.mime) == ("file0.txt", 6, "text/plain") and first.modified_at is not None


def test_prefix_scopes_the_listing_and_paths_are_relative_to_it(endpoint, bucket, s3):
    put(s3, bucket, "docs/a.txt")
    put(s3, bucket, "docs/sub/b.txt")
    put(s3, bucket, "docs2/not-mine.txt")       # "docs" must mean the folder docs/, not every key starting with "docs"
    put(s3, bucket, "other/c.txt")
    for prefix in ("docs", "docs/", "/docs/"):
        c, _ = make(endpoint, bucket, prefix=prefix)
        changes, _ = scan_all(c)
        assert ids(changes) == ["docs/a.txt", "docs/sub/b.txt"]
        assert [x.file.path for x in changes] == ["a.txt", "sub/b.txt"]


def test_resumed_scan_continues_after_the_cursor_and_is_not_authoritative(endpoint, bucket, s3):
    for i in range(8):
        put(s3, bucket, f"k{i}.txt")
    c, _ = make(endpoint, bucket, page_size=3)
    stream = c.scan({})
    first = next(stream)
    stream.close()                                    # the scan "crashes" after one batch
    assert ids(first.changes) == ["k0.txt", "k1.txt", "k2.txt"] and first.cursor == {"after": "k2.txt"}

    changes, batches = scan_all(c, first.cursor)

    assert ids(changes) == [f"k{i}.txt" for i in range(3, 8)]
    assert batches[-1].done and batches[-1].complete_scope is None      # a partial view must never delete the rest
    json.dumps([b.cursor for b in batches])                              # cursors are JSON-serialisable


def test_resume_is_exact_for_awkward_keys_at_every_possible_crash_point(endpoint, bucket, s3):
    keys = ["a b.txt", "a+b.txt", "a%20b.txt", "a.txt", "a/x.txt", "é.txt", "日本語.txt", "UPPER.txt", "z&y=1.txt"]
    for k in keys:
        put(s3, bucket, k)
    c, _ = make(endpoint, bucket, page_size=2)
    everything, batches = scan_all(c)
    assert sorted(ids(everything)) == sorted(keys)
    for crash_after in range(len(batches) - 1):
        cursor = batches[crash_after].cursor
        done_before = [x for b in batches[: crash_after + 1] for x in ids(b.changes)]
        resumed, _ = scan_all(c, cursor)
        assert done_before + ids(resumed) == ids(everything), f"resume after batch {crash_after}"


# ── versions and deletions ──


def test_version_changes_when_content_changes_even_if_size_does_not(endpoint, bucket, s3):
    put(s3, bucket, "a.txt", b"hello")
    put(s3, bucket, "b.txt", b"stays")
    c, _ = make(endpoint, bucket)
    before = {x.external_id: x.file.version for x in scan_all(c)[0]}
    again = {x.external_id: x.file.version for x in scan_all(c)[0]}
    assert before == again                                                   # unchanged => identical version

    put(s3, bucket, "a.txt", b"HELLO")                                       # same length, different bytes
    after = {x.external_id: x.file.version for x in scan_all(c)[0]}
    assert after["a.txt"] != before["a.txt"] and after["b.txt"] == before["b.txt"]
    put(s3, bucket, "a.txt", b"hello world")                                 # different length too
    assert scan_all(c)[0][0].file.version not in (before["a.txt"], after["a.txt"])


def test_deleted_objects_are_absent_from_an_authoritative_full_listing(endpoint, bucket, s3):
    for k in ("keep.txt", "gone.txt"):
        put(s3, bucket, k)
    c, _ = make(endpoint, bucket, page_size=1)
    changes, batches = scan_all(c)
    indexed = set(ids(changes))
    assert indexed == {"keep.txt", "gone.txt"}

    s3.delete_object(Bucket=bucket, Key="gone.txt")
    changes, batches = scan_all(c)
    assert batches[-1].complete_scope == ""                                  # authoritative: the sync layer deletes ...
    assert indexed - set(ids(changes)) == {"gone.txt"}                       # ... exactly what was not seen
    assert all(ch.kind == "upsert" for ch in changes)


def test_sync_layer_marks_missing_objects_deleted_and_leaves_resumed_scans_alone(endpoint, bucket, s3):
    for k in ("a.txt", "b.txt", "c.txt"):
        put(s3, bucket, k)
    c, _ = make(endpoint, bucket, page_size=2)
    # source_connections.kind has a CHECK (local|smb|graph) until the integrator's migration widens it, and
    # run_scan only uses the kind to build a connector when none is passed, so the row is created as "local".
    cid = sync.create_connection("local", f"t-{uuid.uuid4().hex[:6]}", {"root": "/unused"})
    stats = sync.run_scan(cid, connector=c)
    assert stats["status"] == "done" and stats["upserts"] == 3 and stats["queued"] == 3

    def state():
        with engine().connect() as conn:
            return dict(conn.execute(text("SELECT external_id, deleted FROM source_items WHERE connection_id = :c"),
                                     {"c": cid}).all())

    assert state() == {"a.txt": False, "b.txt": False, "c.txt": False}
    s3.delete_object(Bucket=bucket, Key="b.txt")
    stats = sync.run_scan(cid, connector=c)
    assert stats["deletes"] == 1 and state() == {"a.txt": False, "b.txt": True, "c.txt": False}


# ── skipping ──


def test_folder_markers_keep_files_junk_empty_and_archived_objects_are_skipped(endpoint, bucket, s3):
    put(s3, bucket, "real/doc.txt", b"content")
    put(s3, bucket, "real/", b"")                          # console-style "folder"
    put(s3, bucket, "real/.keep", b"k")
    put(s3, bucket, "real/.gitkeep", b"k")
    put(s3, bucket, "Thumbs.db", b"junk")
    put(s3, bucket, "~$lock.docx", b"junk")
    put(s3, bucket, "emptydir_$folder$", b"x")             # Hadoop-style marker
    put(s3, bucket, "empty.txt", b"")
    put(s3, bucket, "cold.txt", b"frozen", StorageClass="GLACIER")
    put(s3, bucket, "warm.txt", b"fine", StorageClass="STANDARD_IA")
    c, _ = make(endpoint, bucket)
    changes, _ = scan_all(c)
    assert ids(changes) == ["real/doc.txt", "warm.txt"]
    assert c.stats == {"folder_marker": 2, "skipped_name": 4, "empty": 1, "archived": 1}
    c2, _ = make(endpoint, bucket, skip_archived=False, skip_names=["thumbs.db"])
    assert "cold.txt" in ids(scan_all(c2)[0]) and "real/.keep" in ids(scan_all(c2)[0])


def test_objects_over_max_file_bytes_are_skipped_not_fatal(endpoint, bucket, s3):
    put(s3, bucket, "small.txt", b"s" * 500)
    put(s3, bucket, "big.bin", b"b" * 5000)
    c, _ = make(endpoint, bucket, max_file_bytes=1000)
    changes, batches = scan_all(c)
    assert ids(changes) == ["small.txt"] and batches[-1].done
    assert c.stats["oversize"] == 1


def test_open_refuses_an_object_that_grew_past_the_cap_since_the_scan(endpoint, bucket, s3):
    put(s3, bucket, "f.bin", b"a" * 100)
    big, _ = make(endpoint, bucket)
    sf = scan_all(big)[0][0].file
    put(s3, bucket, "f.bin", b"a" * 5000)
    small, _ = make(endpoint, bucket, max_file_bytes=1000)
    with pytest.raises(TooLarge):
        small.open(sf)


def test_open_enforces_the_cap_while_streaming_when_no_content_length_is_sent():
    class Body:
        def iter_chunks(self, size):
            yield b"x" * 600
            yield b"x" * 600

        def close(self):
            self.closed = True

    body = Body()

    class Client:
        def get_object(self, **kw):
            return {"Body": body}                 # no ContentLength

    c = S3Connector({"bucket": "b", "max_file_bytes": 1000}, client=Client())
    from app.scale.connectors.base import SourceFile
    with pytest.raises(TooLarge):
        c.open(SourceFile("k", "k", "k", 0, None, "v", []))
    assert body.closed


# ── permissions ──


def test_acl_rules_apply_by_relative_path_longest_prefix_and_fail_closed(endpoint, bucket, s3):
    for k in ("data/finance/q1.xlsx", "data/finance/public/notice.pdf", "data/hr/pay.csv", "data/readme.txt"):
        put(s3, bucket, k)
    rules = [{"prefix": "finance", "principals": ["group:Finance"]},
             {"prefix": "finance/public", "principals": ["*"]}]
    c = S3Connector({"bucket": bucket, "endpoint_url": endpoint, "region": "us-east-1", "prefix": "data",
                    "access_key_env": "T_S3_KEY", "secret_key_env": "T_S3_SECRET", "acl_rules": rules})
    by = {x.external_id: x.file for x in scan_all(c)[0]}
    assert by["data/finance/q1.xlsx"].acl == ["group:finance"]
    assert by["data/finance/public/notice.pdf"].acl == ["*"]
    assert by["data/hr/pay.csv"].acl == []                  # no rule, no default => nobody
    assert by["data/readme.txt"].acl == []
    assert c.refresh_acl(by["data/finance/q1.xlsx"]) == ["group:finance"]
    assert c.refresh_acl(by["data/hr/pay.csv"]) == []

    d = S3Connector({"bucket": bucket, "endpoint_url": endpoint, "region": "us-east-1", "prefix": "data",
                     "access_key_env": "T_S3_KEY", "secret_key_env": "T_S3_SECRET", "acl_rules": rules,
                     "default_acl": ["group:everyone-else"]})
    assert {x.external_id: x.file.acl for x in scan_all(d)[0]}["data/hr/pay.csv"] == ["group:everyone-else"]


# ── bytes ──


def test_open_returns_the_exact_bytes_and_works_as_a_context_manager(endpoint, bucket, s3):
    payloads = {"small.txt": b"hello\n", "binary.bin": bytes(range(256)) * 3 + b"\x00\x00",
                "big.bin": os.urandom(3 * 1024 * 1024 + 17), "ünï cödé & space.txt": "ünï".encode()}
    for k, v in payloads.items():
        put(s3, bucket, k, v)
    c, _ = make(endpoint, bucket)
    for change in scan_all(c)[0]:
        with c.open(change.file) as fh:
            assert fh.read() == payloads[change.external_id]
    # open() needs only what the sync layer persists: external_id, path, name (not `extra`)
    from app.scale.connectors.base import SourceFile
    with c.open(SourceFile("small.txt", "small.txt", "small.txt", 6, None, "v", [])) as fh:
        assert fh.read() == b"hello\n"


def test_open_of_a_missing_object_is_a_clear_error(endpoint, bucket):
    c, _ = make(endpoint, bucket)
    from app.scale.connectors.base import SourceFile
    with pytest.raises(HttpError) as err:
        c.open(SourceFile("nope.txt", "nope.txt", "nope.txt", 1, None, "v", []))
    assert err.value.status == 404 and "NoSuchKey" in str(err.value)


# ── rate limits, auth, transport errors ──


@pytest.mark.parametrize("status,code", [(503, "SlowDown"), (429, "TooManyRequests"), (500, "InternalError")])
def test_throttling_is_retried_honouring_retry_after(endpoint, bucket, s3, status, code):
    put(s3, bucket, "a.txt", b"payload")
    c, sleeps = make(endpoint, bucket)
    hook = Hook(c._s3(), "ListObjectsV2", status, code, times=2, headers={"Retry-After": "4"})
    changes, _ = scan_all(c)
    assert ids(changes) == ["a.txt"] and hook.calls == 3
    assert sleeps == [4.0, 4.0]

    get = Hook(c._s3(), "GetObject", status, code, times=1)
    with c.open(changes[0].file) as fh:
        assert fh.read() == b"payload"
    assert get.calls == 2 and len(sleeps) == 3 and sleeps[2] == 1          # exponential default without Retry-After


def test_throttling_gives_up_after_max_retries_with_a_clear_error(endpoint, bucket):
    c, sleeps = make(endpoint, bucket, max_retries=3)
    hook = Hook(c._s3(), "ListObjectsV2", 503, "SlowDown", times=99)
    with pytest.raises(HttpError) as err:
        scan_all(c)
    assert err.value.status == 503 and "SlowDown" in str(err.value) and not isinstance(err.value, AuthError)
    assert hook.calls == 4 and sleeps == [1, 2, 4]                           # 1 try + 3 retries, bounded backoff


@pytest.mark.parametrize("status,code", [(403, "InvalidAccessKeyId"), (403, "SignatureDoesNotMatch"), (400, "ExpiredToken")])
def test_auth_failure_is_a_clear_error_that_does_not_leak_the_secret(endpoint, bucket, status, code):
    c, sleeps = make(endpoint, bucket)
    echo = f"The key {ACCESS_KEY} with secret {SECRET_KEY} is wrong"   # a server that echoes credentials back
    Hook(c._s3(), "ListObjectsV2", status, code, message=echo, times=99)
    with pytest.raises(AuthError) as err:
        scan_all(c)
    text_ = str(err.value) + repr(err.value) + "".join(map(str, err.value.args))
    assert code in text_ and SECRET_KEY not in text_ and ACCESS_KEY not in text_
    assert sleeps == []                                                       # never retried
    assert SECRET_KEY not in repr(err.value.__cause__) and err.value.__suppress_context__


def test_a_missing_credential_variable_is_named_but_nothing_secret_is_printed(endpoint, bucket, monkeypatch):
    monkeypatch.delenv("T_S3_SECRET")
    c, _ = make(endpoint, bucket)
    with pytest.raises(HttpError) as err:
        scan_all(c)
    assert "T_S3_SECRET" in str(err.value) and SECRET_KEY not in str(err.value)


def test_unreachable_endpoint_is_retried_then_reported_without_details(bucket):
    c, sleeps = make("http://127.0.0.1:1", bucket, max_retries=2)
    with pytest.raises(HttpError) as err:
        scan_all(c)
    assert "EndpointConnectionError" in str(err.value) and sleeps == [1, 2]


def test_scan_output_never_contains_credentials(endpoint, bucket, s3):
    put(s3, bucket, "a.txt")
    c, _ = make(endpoint, bucket)
    changes, batches = scan_all(c)
    blob = repr(changes) + repr([b.cursor for b in batches])
    assert SECRET_KEY not in blob and ACCESS_KEY not in blob


def test_config_names_variables_only(endpoint, bucket):
    c, _ = make(endpoint, bucket)
    assert SECRET_KEY not in json.dumps(c.cfg) and ACCESS_KEY not in json.dumps(c.cfg)
