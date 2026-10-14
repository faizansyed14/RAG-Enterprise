"""Azure Blob connector tests.

REAL server: Azurite, Microsoft's Azure Storage emulator (`npm install azurite`; the `azurite-blob` binary is found through
AZURITE_BIN, PATH or the directory of the Python interpreter; these tests are SKIPPED when it is missing). The test starts it on a
free port with a temporary data directory and stops it by PID at the end. Azurite verifies Shared Key signatures and SAS tokens
like the service does, enforces the x-ms-version header, pages List Blobs with `marker` / `NextMarker`, and serves Get Blob. Containers
are seeded (and SAS tokens signed) with the official azure-storage-blob SDK, so the credentials and the data come from an
implementation independent of the connector. The Shared Key string-to-sign is also compared with the SDK's own implementation.
Throttling answers (429 / 503 with Retry-After) are injected by a transport that wraps the real HTTP transport and forwards
everything else to Azurite.

MOCK: the Entra bearer mode, names the service percent-encodes, empty pages with a continuation marker, a rejected marker, the Archive
tier, odd error bodies and configuration errors use httpx.MockTransport fixtures that follow the documented XML shapes.
"""

import base64
import datetime
import hashlib
import itertools
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import uuid
from urllib.parse import quote

import httpx
import pytest

from app.scale import sync
from app.scale.connectors._http import AuthError, HttpError, TooLarge
from app.scale.connectors.azure_blob import API_VERSION, AzureBlobConnector, shared_key_signature
from app.scale.connectors.base import SourceFile

ACCOUNT = "devstoreaccount1"                      # Azurite's well-known development account and key (public, documented)
KEY = "Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/K1SZFPTOtr/KBHBeksoGMGw=="
SDK_VERSION = "2021-12-02"


def md5_b64(data: bytes) -> str:
    return base64.b64encode(hashlib.md5(data).digest()).decode()


def xml_error(code, message="boom"):
    return f'<?xml version="1.0" encoding="utf-8"?><Error><Code>{code}</Code><Message>{message}\nRequestId:abc\nTime:now</Message></Error>'


def find_azurite():
    candidates = [os.environ.get("AZURITE_BIN"), shutil.which("azurite-blob"), os.path.join(os.path.dirname(sys.executable), "azurite-blob")]
    for path in candidates:
        if path and os.path.exists(path) and shutil.which("node"):
            return path
    return None


# ── real server ──


class Chaos(httpx.BaseTransport):
    """Real HTTP to Azurite, except for the scripted failures."""

    def __init__(self):
        self.inner = httpx.HTTPTransport()
        self.script: list[tuple[str, int, dict, str]] = []             # ("list" | "get" | "any", status, headers, body)
        self.requests: list[httpx.Request] = []

    def fail(self, when, status, headers=None, body="", times=1):
        self.script.extend([(when, status, headers or {}, body)] * times)

    def handle_request(self, request):
        self.requests.append(request)
        is_list = request.url.params.get("comp") == "list"
        for i, (when, status, headers, body) in enumerate(self.script):
            if when == "any" or (when == "list") == is_list:
                self.script.pop(i)
                return httpx.Response(status, headers=headers, content=body.encode())
        return self.inner.handle_request(request)


@pytest.fixture(scope="module")
def azurite(tmp_path_factory):
    binary = find_azurite()
    blob = pytest.importorskip("azure.storage.blob", reason="azure-storage-blob is needed to seed Azurite")
    if not binary:
        pytest.skip("Azurite is not installed (npm install azurite, or set AZURITE_BIN)")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    data = tmp_path_factory.mktemp("azurite")
    proc = subprocess.Popen([binary, "--blobHost", "127.0.0.1", "--blobPort", str(port), "--location", str(data), "--silent"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=str(data))
    try:
        deadline = time.time() + 40
        while time.time() < deadline:
            if proc.poll() is not None:
                pytest.skip("Azurite exited at start-up")
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
                break
            except OSError:
                time.sleep(0.2)
        else:
            pytest.skip("Azurite did not start in time")
        endpoint = f"http://127.0.0.1:{port}/{ACCOUNT}"
        cs = f"DefaultEndpointsProtocol=http;AccountName={ACCOUNT};AccountKey={KEY};BlobEndpoint={endpoint};"
        yield {"endpoint": endpoint, "service": blob.BlobServiceClient.from_connection_string(cs, api_version=SDK_VERSION)}
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


class Container:
    def __init__(self, azurite):
        self.endpoint = azurite["endpoint"]
        self.name = "t" + uuid.uuid4().hex[:12]
        self.client = azurite["service"].create_container(self.name)

    def upload(self, name, data=b"", **kw):
        self.client.upload_blob(name, data, overwrite=True, **kw)
        return name

    def delete(self, name):
        self.client.delete_blob(name)

    def sas(self, **kw):
        from azure.storage.blob import ContainerSasPermissions, generate_container_sas
        perms = kw.pop("permission", ContainerSasPermissions(read=True, list=True))
        expiry = kw.pop("expiry", datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1))
        return generate_container_sas(ACCOUNT, self.name, account_key=KEY, permission=perms, expiry=expiry, version=SDK_VERSION, **kw)


@pytest.fixture
def cont(azurite):
    return Container(azurite)


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("T_AZ_KEY", KEY)
    monkeypatch.setenv("T_AZ_BEARER", "bearer-token-for-mocks")


def make(cont, **cfg):
    sleeps: list[float] = []
    chaos = Chaos()
    config = {"container": cont.name, "account_name": ACCOUNT, "endpoint_url": cont.endpoint, "account_key_env": "T_AZ_KEY",
              "default_acl": ["*"], **cfg}
    c = AzureBlobConnector(config, transport=chaos, sleep=sleeps.append)
    c.sleeps, c.chaos = sleeps, chaos
    return c


def scan_all(connector, cursor=None):
    batches = list(itertools.islice(connector.scan(cursor or {}), 200))
    assert len(batches) < 200, "the scan never terminated"
    return [c for b in batches for c in b.changes], batches


def by_path(changes):
    return {c.file.path: c.file for c in changes}


# ── listing and pagination (real server) ──


def test_shared_key_scan_pages_through_the_container_and_maps_blobs(cont):
    for i in range(8):
        cont.upload(f"docs/file{i}.txt", f"content {i}".encode())
    cont.upload("top.pdf", b"%PDF-1.4 top")
    c = make(cont, page_size=3)
    changes, batches = scan_all(c)
    got = by_path(changes)
    assert sorted(got) == sorted([f"docs/file{i}.txt" for i in range(8)] + ["top.pdf"])
    f = got["docs/file3.txt"]
    assert (f.external_id, f.name, f.size, f.mime) == ("docs/file3.txt", "file3.txt", 9, "text/plain")
    assert f.version == f"{md5_b64(b'content 3')}:9" and f.modified_at is not None and f.modified_at.tzinfo is not None
    assert len(batches) == 3 and [b.done for b in batches] == [False, False, True]
    assert all(c.kind == "upsert" for c in changes)
    lists = [r for r in c.chaos.requests if r.url.params.get("comp") == "list"]
    assert [r.url.params.get("marker") for r in lists][0] is None and all(r.url.params.get("marker") for r in lists[1:])
    assert {r.url.params["maxresults"] for r in lists} == {"3"} and all(r.headers["x-ms-version"] == API_VERSION for r in lists)
    assert batches[-1].complete_scope == "" and all(b.complete_scope is None for b in batches[:-1])
    assert [b.cursor for b in batches[:-1]] == [{"marker": b.cursor["marker"]} for b in batches[:-1]] and batches[-1].cursor == {}
    json.dumps([b.cursor for b in batches])


def test_page_size_is_capped_at_the_service_maximum(cont):
    cont.upload("a.txt", b"x")
    c = make(cont, page_size=900_000)
    scan_all(c)
    assert c.chaos.requests[0].url.params["maxresults"] == "5000"
    d = make(cont)
    scan_all(d)
    assert d.chaos.requests[0].url.params["maxresults"] == "5000"


def test_prefix_limits_the_scan_and_paths_are_relative_to_it(cont):
    cont.upload("team/a.txt", b"a")
    cont.upload("team/sub/b.txt", b"b")
    cont.upload("teamwork/c.txt", b"c")
    cont.upload("other/d.txt", b"d")
    for prefix in ("team", "team/", "/team"):
        changes, _ = scan_all(make(cont, prefix=prefix))
        assert sorted(by_path(changes)) == ["a.txt", "sub/b.txt"]                     # "teamwork/" is not under "team/"
    assert {c.external_id for c in changes} == {"team/a.txt", "team/sub/b.txt"}


AWKWARD = ["sp ace/file name.txt", "plus+sign.txt", "ünï/日本語 ファイル.txt", "hash#and?mark.txt", "percent%20sign.txt",
           "semi;colon&amp=eq.txt", "paren(1)!~'q'.txt", "dotted.name.v2.final.txt", "comma,name.txt", "at@sign.txt"]


@pytest.mark.parametrize("mode", ["shared_key", "sas"])
def test_awkward_blob_names_are_listed_and_downloaded_byte_for_byte(cont, monkeypatch, mode):
    for i, name in enumerate(AWKWARD):
        cont.upload(name, f"bytes of {i}".encode() * 3)
    if mode == "sas":
        monkeypatch.setenv("T_AZ_SAS", cont.sas())
        c = make(cont, account_key_env=None, sas_token_env="T_AZ_SAS")
    else:
        c = make(cont)
    changes, _ = scan_all(c)
    got = by_path(changes)
    assert sorted(got) == sorted(AWKWARD)
    for i, name in enumerate(AWKWARD):
        with c.open(got[name]) as fh:
            assert fh.read() == f"bytes of {i}".encode() * 3, name


# ── versions and deletions (real server) ──


def test_version_follows_the_content_not_the_etag(cont):
    cont.upload("a.txt", b"hello")
    cont.upload("b.txt", b"hello")
    cont.upload("c.txt", b"hello")
    c = make(cont)
    before = by_path(scan_all(c)[0])
    assert before["a.txt"].version == before["b.txt"].version == f"{md5_b64(b'hello')}:5"
    cont.upload("a.txt", b"hello")                                                    # same bytes again: new Etag, same content
    cont.upload("b.txt", b"HELLO")                                                    # same size, different bytes
    cont.upload("c.txt", b"hello world")
    after = by_path(scan_all(c)[0])
    assert after["a.txt"].version == before["a.txt"].version
    assert len({before["b.txt"].version, after["b.txt"].version, after["c.txt"].version}) == 3


def test_full_scan_is_authoritative_so_deleted_blobs_are_simply_absent(cont):
    for name in ("a.txt", "b.txt", "c/d.txt"):
        cont.upload(name, b"x")
    c = make(cont)
    first = {x.external_id for x in scan_all(c)[0]}
    cont.delete("b.txt")
    cont.delete("c/d.txt")
    changes, batches = scan_all(c)
    assert first - {x.external_id for x in changes} == {"b.txt", "c/d.txt"} and batches[-1].complete_scope == ""


def test_a_crashed_scan_resumes_at_the_saved_marker_and_is_not_authoritative(cont):
    expected = sorted(f"f{i:02d}.txt" for i in range(11))
    for name in expected:
        cont.upload(name, b"x")
    baseline = make(cont, page_size=3)
    scan_all(baseline)
    total_pages = len([r for r in baseline.chaos.requests if r.url.params.get("comp") == "list"])
    for crash_after in (1, 2, 3):
        stream = make(cont, page_size=3).scan({})
        got = [next(stream) for _ in range(crash_after)]
        stream.close()
        saved = json.loads(json.dumps(got[-1].cursor))                                # it went through the database
        resumed = make(cont, page_size=3)
        rest, batches = scan_all(resumed, saved)
        seen = [c.file.path for b in got for c in b.changes] + [c.file.path for c in rest]
        assert sorted(seen) == expected and len(seen) == 11                           # nothing repeated, nothing missed
        assert batches[-1].done and batches[-1].complete_scope is None
        assert len([r for r in resumed.chaos.requests if r.url.params.get("comp") == "list"]) == total_pages - crash_after


def test_blobs_that_must_not_be_indexed_are_skipped_and_counted(cont):
    cont.upload("ok.txt", b"ok")
    cont.upload("empty.txt", b"")
    cont.upload("dir/", b"")                                                          # a "folder" blob
    cont.upload("Thumbs.db", b"j")
    cont.upload("deep/~$lock.docx", b"j")
    cont.upload("big.bin", b"b" * 50)
    cont.upload("exact.bin", b"e" * 20)
    c = make(cont, max_file_bytes=20)
    changes, _ = scan_all(c)
    assert sorted(by_path(changes)) == ["exact.bin", "ok.txt"]
    assert c.stats == {"empty": 1, "folder_marker": 1, "skipped_name": 2, "oversize": 1}
    everything = make(cont, skip_names=[], max_file_bytes=1000)
    assert "Thumbs.db" in by_path(scan_all(everything)[0])


def test_archived_blobs_are_skipped_by_default(cont):
    cont.upload("cold.txt", b"cold")
    cont.upload("hot.txt", b"hot")
    from azure.storage.blob import StandardBlobTier
    cont.client.get_blob_client("cold.txt").set_standard_blob_tier(StandardBlobTier.ARCHIVE)
    c = make(cont)
    assert sorted(by_path(scan_all(c)[0])) == ["hot.txt"] and c.stats == {"archived": 1}
    assert sorted(by_path(scan_all(make(cont, skip_archived=False))[0])) == ["cold.txt", "hot.txt"]


# ── bytes (real server) ──


def test_open_returns_exact_bytes_of_a_large_blob_and_works_from_a_rebuilt_source_file(cont):
    payload = bytes(range(256)) * 50_000                                               # ~12.8 MB, every byte value
    cont.upload("dir/blob.bin", payload)
    c = make(cont)
    sf = scan_all(c)[0][0].file
    with c.open(sf) as fh:
        assert fh.read() == payload
    rebuilt = SourceFile("dir/blob.bin", "dir/blob.bin", "blob.bin", len(payload), None, "v", [])
    with c.open(rebuilt) as fh:
        assert fh.read(300) == payload[:300]


def test_open_refuses_a_blob_larger_than_max_file_bytes_even_if_the_listing_said_small(cont):
    cont.upload("grew.bin", b"s" * 10)
    c = make(cont, max_file_bytes=100)
    sf = scan_all(c)[0][0].file
    cont.upload("grew.bin", b"b" * 5000)
    with pytest.raises(TooLarge):
        c.open(sf)


def test_open_reports_a_vanished_blob_with_azures_error_code(cont):
    cont.upload("gone.txt", b"x")
    c = make(cont)
    sf = scan_all(c)[0][0].file
    cont.delete("gone.txt")
    with pytest.raises(HttpError) as err:
        c.open(sf)
    assert err.value.status == 404 and "BlobNotFound" in str(err.value) and not isinstance(err.value, AuthError)


def test_a_missing_container_aborts_the_scan_instead_of_looking_like_an_empty_source(cont):
    c = make(cont, container="no-such-container")
    with pytest.raises(HttpError) as err:
        scan_all(c)
    assert err.value.status == 404 and "ContainerNotFound" in str(err.value)


# ── permissions ──


def test_acl_rules_apply_by_path_and_fail_closed(cont):
    for name in ("Docs/readme.txt", "Docs/2026/q1.pdf", "pay.csv"):
        cont.upload(name, b"x")
    rules = [{"prefix": "Docs", "principals": ["group:Staff"]}, {"prefix": "Docs/2026", "principals": ["group:finance"]}]
    c = make(cont, acl_rules=rules, default_acl=None)
    got = by_path(scan_all(c)[0])
    assert got["Docs/readme.txt"].acl == ["group:staff"] and got["Docs/2026/q1.pdf"].acl == ["group:finance"] and got["pay.csv"].acl == []
    assert c.refresh_acl(got["Docs/2026/q1.pdf"]) == ["group:finance"] and c.refresh_acl(got["pay.csv"]) == []
    rebuilt = SourceFile("Docs/2026/q1.pdf", "Docs/2026/q1.pdf", "q1.pdf", 1, None, "v", [])
    assert c.refresh_acl(rebuilt) == ["group:finance"]


def test_acl_paths_are_relative_to_the_prefix(cont):
    cont.upload("team/Finance/pay.csv", b"1")
    cont.upload("team/readme.txt", b"1")
    c = make(cont, prefix="team", acl_rules=[{"prefix": "Finance", "principals": ["group:finance"]}], default_acl=["group:all"])
    assert {p: f.acl for p, f in by_path(scan_all(c)[0]).items()} == {"Finance/pay.csv": ["group:finance"], "readme.txt": ["group:all"]}


# ── authentication (real server) ──


def test_sas_token_authenticates_with_or_without_the_leading_question_mark(cont, monkeypatch):
    cont.upload("a.txt", b"hello")
    for value in (cont.sas(), "?" + cont.sas()):
        monkeypatch.setenv("T_AZ_SAS", value)
        c = make(cont, account_key_env=None, sas_token_env="T_AZ_SAS")
        changes, _ = scan_all(c)
        with c.open(changes[0].file) as fh:
            assert fh.read() == b"hello"
        assert not [r for r in c.chaos.requests if "authorization" in r.headers]           # the SAS is the credential, no header


def test_a_sas_without_list_permission_is_a_clear_auth_error(cont, monkeypatch):
    from azure.storage.blob import ContainerSasPermissions
    cont.upload("a.txt", b"hello")
    monkeypatch.setenv("T_AZ_SAS", cont.sas(permission=ContainerSasPermissions(read=True)))
    c = make(cont, account_key_env=None, sas_token_env="T_AZ_SAS")
    with pytest.raises(AuthError) as err:
        scan_all(c)
    assert err.value.status == 403 and c.sleeps == []
    assert str(err.value).startswith("Azure Blob rejected the credentials (403")


def test_expired_and_tampered_sas_tokens_are_auth_errors_that_do_not_leak_the_signature(cont, monkeypatch):
    cont.upload("a.txt", b"hello")
    past = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=1)
    expired = cont.sas(expiry=past, start=past - datetime.timedelta(hours=1))
    good = cont.sas()
    tampered = good.replace("sig=", "sig=AAAA")
    for label, token in (("expired", expired), ("tampered", tampered)):
        monkeypatch.setenv("T_AZ_SAS", token)
        c = make(cont, account_key_env=None, sas_token_env="T_AZ_SAS")
        with pytest.raises(AuthError) as err:
            scan_all(c)
        blob = str(err.value) + repr(err.value)
        sig = [p.split("=", 1)[1] for p in token.split("&") if p.startswith("sig=")][0]
        assert err.value.status == 403 and ("AuthenticationFailed" in blob or "AuthorizationFailure" in blob), (label, blob)
        assert sig not in blob and "sig=" not in blob and "sv=" not in blob and c.sleeps == []


def test_a_wrong_account_key_is_an_auth_error_without_the_key_or_the_signature(cont, monkeypatch):
    wrong = base64.b64encode(b"not the real key" * 4).decode()
    monkeypatch.setenv("T_AZ_KEY", wrong)
    c = make(cont)
    with pytest.raises(AuthError) as err:
        scan_all(c)
    sent = c.chaos.requests[0].headers["authorization"]
    blob = str(err.value) + repr(err.value)
    # Azure answers AuthenticationFailed for a bad signature, Azurite AuthorizationFailure: the code is passed through either way
    assert err.value.status == 403 and str(err.value) in {"Azure Blob rejected the credentials (403, AuthenticationFailed)",
                                                          "Azure Blob rejected the credentials (403, AuthorizationFailure)"}
    assert wrong not in blob and sent.split(":", 1)[1] not in blob and KEY not in blob and c.sleeps == []


def test_a_key_that_is_not_base64_or_not_set_is_reported_by_variable_name(cont, monkeypatch):
    monkeypatch.setenv("T_AZ_KEY", "this is *not* base64!")
    c = make(cont)
    with pytest.raises(HttpError, match="T_AZ_KEY") as err:
        scan_all(c)
    assert "this is" not in str(err.value) and not c.chaos.requests
    monkeypatch.delenv("T_AZ_KEY")
    with pytest.raises(HttpError, match="T_AZ_KEY.*not set"):
        scan_all(make(cont))
    monkeypatch.delenv("T_AZ_SAS", raising=False)
    with pytest.raises(HttpError, match="T_AZ_SAS"):
        scan_all(make(cont, account_key_env=None, sas_token_env="T_AZ_SAS"))


def test_the_api_version_header_is_configurable_and_the_service_checks_it(cont):
    cont.upload("a.txt", b"x")
    c = make(cont, api_version="2020-10-02")
    assert len(scan_all(c)[0]) == 1 and c.chaos.requests[0].headers["x-ms-version"] == "2020-10-02"
    with pytest.raises(HttpError) as err:
        scan_all(make(cont, api_version="2099-01-01"))
    assert err.value.status == 400 and "InvalidHeaderValue" in str(err.value)


def test_secrets_never_reach_cursors_source_files_or_the_stored_config(cont, monkeypatch):
    for i in range(5):
        cont.upload(f"f{i}.txt", b"x")
    token = cont.sas()
    monkeypatch.setenv("T_AZ_SAS", token)
    for c in (make(cont, page_size=2), make(cont, page_size=2, account_key_env=None, sas_token_env="T_AZ_SAS")):
        changes, batches = scan_all(c)
        blob = repr(changes) + repr([b.cursor for b in batches]) + json.dumps(c.cfg)
        assert KEY not in blob and token not in blob and "sig=" not in blob


# ── rate limits (real server behind a failure-injecting transport) ──


@pytest.mark.parametrize("status,headers,code", [(503, {"Retry-After": "4"}, "ServerBusy"), (503, {}, "ServerBusy"),
                                                  (429, {"Retry-After": "2"}, "TooManyRequests"), (504, {}, "GatewayTimeout")])
def test_throttling_is_retried_for_listing_and_download(cont, status, headers, code):
    cont.upload("a.txt", b"hello")
    c = make(cont)
    c.chaos.fail("list", status, headers, xml_error(code), times=2)
    changes, _ = scan_all(c)
    assert len(changes) == 1 and len(c.sleeps) == 2
    assert c.sleeps == ([float(headers["Retry-After"])] * 2 if headers else [1, 2])
    c.chaos.fail("get", status, headers, xml_error(code))
    with c.open(changes[0].file) as fh:
        assert fh.read() == b"hello"
    assert len(c.sleeps) == 3
    signed = [r for r in c.chaos.requests if r.headers.get("authorization", "").startswith(f"SharedKey {ACCOUNT}:")]
    assert len(signed) == len(c.chaos.requests)                                       # every retry was signed again


def test_persistent_throttling_gives_up_with_a_clear_non_auth_error(cont):
    c = make(cont, max_retries=2)
    c.chaos.fail("list", 503, {"Retry-After": "9"}, xml_error("ServerBusy"), times=9)
    with pytest.raises(HttpError) as err:
        scan_all(c)
    assert not isinstance(err.value, AuthError) and err.value.status == 503 and c.sleeps == [9.0, 9.0]


def test_a_server_side_500_is_not_retried(cont):
    c = make(cont)
    c.chaos.fail("list", 500, {}, xml_error("OperationTimedOut"))
    with pytest.raises(HttpError) as err:
        scan_all(c)
    assert err.value.status == 500 and c.sleeps == [] and len(c.chaos.requests) == 1


# ── sync layer (real server) ──


def test_sync_layer_deletes_via_complete_scope(cont):
    from sqlalchemy import text
    from app.scale.db import engine

    for name in ("a.txt", "b.txt", "dir/c.txt"):
        cont.upload(name, b"x")
    c = make(cont)
    # source_connections.kind has a CHECK (local|smb|graph) until the integrator's migration widens it; run_scan only
    # uses the kind to build a connector when none is passed
    cid = sync.create_connection("local", f"t-{time.time_ns()}", {"root": "/unused"})
    stats = sync.run_scan(cid, connector=c)
    assert stats["upserts"] == 3 and stats["queued"] == 3
    cont.delete("b.txt")
    stats = sync.run_scan(cid, connector=c)
    with engine().connect() as conn:
        rows = dict(conn.execute(text("SELECT external_id, deleted FROM source_items WHERE connection_id = :c"), {"c": cid}).all())
    assert stats["deletes"] == 1 and rows["b.txt"] is True and rows["a.txt"] is False and rows["dir/c.txt"] is False


# ── Shared Key signing against the SDK's implementation ──


SHAPES = [
    ("GET", "/devstoreaccount1/docs", {"restype": "container", "comp": "list", "maxresults": "3"}),
    ("GET", "/devstoreaccount1/docs", {"restype": "container", "comp": "list", "marker": "2!92!MDAwMDI4ZmlsZTA2LnR4dCEwMDAwMjghOTk5OS0xMi0zMVQyMzo1OTo1OS45OTk5OTk5WiE-", "prefix": "a b/"}),
    ("GET", "/devstoreaccount1/docs/dir/file%20name%23%3F.txt", {}),
    ("GET", "/devstoreaccount1/docs/%C3%BCn%C3%AF/%E6%97%A5%E6%9C%AC.txt", {}),
    ("GET", "/devstoreaccount1/docs/plus%2Bsign.txt", {"timeout": "30"}),
]


@pytest.mark.parametrize("method,path,query", SHAPES)
def test_shared_key_string_to_sign_matches_the_azure_sdk(method, path, query):
    pipeline = pytest.importorskip("azure.core.pipeline")
    transport = pytest.importorskip("azure.core.pipeline.transport")
    try:
        from azure.storage.blob._shared.authentication import SharedKeyCredentialPolicy
    except ImportError:
        pytest.skip("the SDK moved its signing policy")
    from urllib.parse import urlencode
    headers = {"x-ms-date": "Sat, 10 Oct 2026 08:15:15 GMT", "x-ms-version": SDK_VERSION, "x-ms-client-request-id": "req-1"}
    url = "http://127.0.0.1:10000" + path + (("?" + urlencode(query, quote_via=quote)) if query else "")
    request = pipeline.PipelineRequest(transport.HttpRequest(method, url, headers=dict(headers)), pipeline.PipelineContext(None))
    SharedKeyCredentialPolicy(ACCOUNT, KEY).on_request(request)
    ours = shared_key_signature(method, path, list(query.items()), {**headers, "accept": "*/*", "user-agent": "x"}, ACCOUNT, KEY)
    assert ours == request.http_request.headers["Authorization"]


def test_shared_key_covers_content_headers_conditions_and_repeated_parameters():
    base = {"x-ms-date": "Sat, 10 Oct 2026 08:15:15 GMT", "x-ms-version": SDK_VERSION}
    plain = shared_key_signature("GET", "/a/c", [("comp", "list")], base, "a", KEY)
    assert plain == shared_key_signature("GET", "/a/c", [("comp", "list")], {**base, "Accept": "x", "User-Agent": "y"}, "a", KEY)
    assert plain != shared_key_signature("HEAD", "/a/c", [("comp", "list")], base, "a", KEY)
    assert plain != shared_key_signature("GET", "/a/c", [("comp", "list")], {**base, "x-ms-date": "Sat, 10 Oct 2026 08:15:16 GMT"}, "a", KEY)
    assert plain != shared_key_signature("GET", "/a/c", [("comp", "list")], {**base, "Range": "bytes=0-9"}, "a", KEY)
    assert plain != shared_key_signature("GET", "/a/c", [("comp", "list")], {**base, "If-Match": '"x"'}, "a", KEY)
    assert plain != shared_key_signature("GET", "/a/other", [("comp", "list")], base, "a", KEY)
    assert plain != shared_key_signature("GET", "/a/c", [("comp", "list")], base, "b", KEY)
    # query names are case-insensitive and sorted; repeated values are sorted and comma-joined
    one = shared_key_signature("GET", "/a/c", [("Comp", "list"), ("include", "metadata"), ("include", "copy")], base, "a", KEY)
    two = shared_key_signature("GET", "/a/c", [("include", "copy"), ("include", "metadata"), ("comp", "list")], base, "a", KEY)
    assert one == two and one != plain
    assert shared_key_signature("GET", "/a/c", [], {**base, "Content-Length": "0"}, "a", KEY) == shared_key_signature("GET", "/a/c", [], base, "a", KEY)


# ── MOCK fixtures ──


def blob_xml(blobs, next_marker="", prefix="", extra=""):
    items = ""
    for b in blobs:
        props = "".join(f"<{k}>{v}</{k}>" for k, v in b.get("props", {}).items())
        name_attr = ' Encoded="true"' if b.get("encoded") else ""
        items += f"<Blob><Name{name_attr}>{b['name']}</Name><Properties>{props}</Properties></Blob>"
    return (f'<?xml version="1.0" encoding="utf-8"?><EnumerationResults ServiceEndpoint="http://x/a/" ContainerName="c">'
            f"<Prefix>{prefix}</Prefix><MaxResults>5000</MaxResults><Blobs>{items}</Blobs><NextMarker>{next_marker}</NextMarker>"
            f"{extra}</EnumerationResults>")


def props(size=5, md5="", etag="0x8DB1", tier="Hot", modified="Sat, 10 Oct 2026 08:15:15 GMT"):
    out = {"Last-Modified": modified, "Etag": etag, "Content-Length": str(size), "BlobType": "BlockBlob", "AccessTier": tier}
    if md5:
        out["Content-MD5"] = md5
    return out


class Mock:
    def __init__(self, handler):
        self.handler, self.requests, self.sleeps = handler, [], []

    def __call__(self, request):
        self.requests.append(request)
        return self.handler(request)


def mock_connector(handler, **cfg):
    config = {"container": "c", "account_name": "a", "access_token_env": "T_AZ_BEARER", "default_acl": ["*"], **cfg}
    m = Mock(handler)
    c = AzureBlobConnector(config, transport=httpx.MockTransport(m), sleep=m.sleeps.append, clock=lambda: 1_800_000_000.0)
    c.mock = m
    return c


def test_entra_bearer_token_mode_sends_the_token_and_the_service_version(monkeypatch):
    monkeypatch.setenv("T_AZ_BEARER", "eyJ.ENTRA-TOKEN.MUST-NOT-LEAK")

    def handler(request):
        assert request.headers["authorization"] == "Bearer eyJ.ENTRA-TOKEN.MUST-NOT-LEAK" and request.url.host == "a.blob.core.windows.net"
        assert request.headers["x-ms-version"] == API_VERSION and "sig" not in request.url.params
        if request.url.params.get("comp") == "list":
            return httpx.Response(200, text=blob_xml([{"name": "x.txt", "props": props()}]))
        return httpx.Response(200, content=b"12345")

    c = mock_connector(handler)
    batch = list(c.scan({}))[0]
    assert batch.changes[0].file.external_id == "x.txt"
    with c.open(batch.changes[0].file) as fh:
        assert fh.read() == b"12345"
    monkeypatch.setenv("T_AZ_BEARER", "expired-WRONG")
    bad = mock_connector(lambda r: httpx.Response(401, text=xml_error("InvalidAuthenticationInfo", "Server failed to authenticate expired-WRONG")))
    with pytest.raises(AuthError) as err:
        list(bad.scan({}))
    assert str(err.value) == "Azure Blob rejected the credentials (401, InvalidAuthenticationInfo)" and "WRONG" not in repr(err.value)
    monkeypatch.delenv("T_AZ_BEARER")
    with pytest.raises(HttpError, match="T_AZ_BEARER"):
        list(mock_connector(handler).scan({}))


def test_anonymous_mode_sends_no_credentials():
    def handler(request):
        assert "authorization" not in request.headers and "sig" not in request.url.params
        return httpx.Response(200, text=blob_xml([{"name": "x.txt", "props": props()}]))

    c = mock_connector(handler, access_token_env=None, anonymous=True)
    assert len(list(c.scan({}))[0].changes) == 1


def test_configuration_errors_are_clear():
    with pytest.raises(ValueError, match="sas_token_env, account_key_env"):
        AzureBlobConnector({"container": "c", "account_name": "a"})
    with pytest.raises(ValueError, match="only one credential"):
        AzureBlobConnector({"container": "c", "account_name": "a", "sas_token_env": "X", "account_key_env": "Y"})
    with pytest.raises(ValueError, match="account_name or endpoint_url"):
        AzureBlobConnector({"container": "c", "sas_token_env": "X"})
    with pytest.raises(ValueError, match="needs account_name"):
        AzureBlobConnector({"container": "c", "endpoint_url": "http://h/x", "account_key_env": "X"})
    with pytest.raises(KeyError):
        AzureBlobConnector({"account_name": "a", "sas_token_env": "X"})                  # container is required


def test_percent_encoded_names_are_decoded_and_fetched_by_their_real_name():
    seen = []

    def handler(request):
        if request.url.params.get("comp") == "list":
            return httpx.Response(200, text=blob_xml([{"name": "dir/caf%C3%A9%20%26%20bar%0D.txt", "encoded": True, "props": props()},
                                                        {"name": "plain 100%.txt", "props": props()}]))
        seen.append(request.url.raw_path.decode())
        return httpx.Response(200, content=b"12345")

    c = mock_connector(handler)
    changes = list(c.scan({}))[0].changes
    assert [x.file.external_id for x in changes] == ["dir/café & bar\r.txt", "plain 100%.txt"]
    with c.open(changes[0].file):
        pass
    with c.open(changes[1].file):
        pass
    assert seen == ["/c/dir/caf%C3%A9%20%26%20bar%0D.txt", "/c/plain%20100%25.txt"]


def test_empty_pages_with_a_marker_are_followed_and_a_repeated_marker_stops_the_scan():
    pages = {"": blob_xml([], "m1"), "m1": blob_xml([{"name": "a.txt", "props": props()}], "m2"), "m2": blob_xml([], "")}
    c = mock_connector(lambda r: httpx.Response(200, text=pages[r.url.params.get("marker", "")]))
    batches = list(c.scan({}))
    assert [len(b.changes) for b in batches] == [0, 1, 0] and [b.cursor for b in batches] == [{"marker": "m1"}, {"marker": "m2"}, {}]
    assert batches[-1].complete_scope == ""
    loop = mock_connector(lambda r: httpx.Response(200, text=blob_xml([], "same")))
    with pytest.raises(HttpError, match="same continuation marker"):
        list(loop.scan({}))


def test_a_rejected_marker_restarts_the_listing_from_the_start():
    def handler(request):
        marker = request.url.params.get("marker")
        if marker == "stale":
            return httpx.Response(400, text=xml_error("InvalidQueryParameterValue", "Value for one of the query parameters is invalid."))
        return httpx.Response(200, text=blob_xml([{"name": "a.txt", "props": props()}]))

    c = mock_connector(handler)
    batches = list(c.scan({"marker": "stale"}))
    assert [x.file.path for b in batches for x in b.changes] == ["a.txt"] and c.stats == {"marker_reset": 1}
    assert batches[-1].complete_scope is None                                              # it began as a resumed scan
    assert [r.url.params.get("marker") for r in c.mock.requests] == ["stale", None]
    boom = mock_connector(lambda r: httpx.Response(400, text=xml_error("InvalidQueryParameterValue")))
    with pytest.raises(HttpError) as err:
        list(boom.scan({}))
    assert err.value.status == 400                                                          # no marker to blame: a real error


def test_properties_that_are_missing_or_odd_do_not_break_the_scan():
    page = blob_xml([
        {"name": "ok.txt", "props": props(md5="QUJD")},
        {"name": "no-props.txt"},                                                             # no Properties content at all
        {"name": "bad-size.txt", "props": {"Content-Length": "lots"}},
        {"name": "etag-only.txt", "props": {"Content-Length": "7", "Etag": '"0x9"'}},
        {"name": "bad-date.txt", "props": {"Content-Length": "7", "Last-Modified": "yesterday-ish", "Etag": "0x1"}},
    ])
    c = mock_connector(lambda r: httpx.Response(200, text=page))
    got = {x.file.path: x.file for x in list(c.scan({}))[0].changes}
    assert sorted(got) == ["bad-date.txt", "etag-only.txt", "ok.txt"] and c.stats == {"empty": 2}
    assert got["ok.txt"].version == "QUJD:5" and got["etag-only.txt"].version == "0x9:7" and got["bad-date.txt"].modified_at is None
    assert got["ok.txt"].modified_at.isoformat() == "2026-10-10T08:15:15+00:00"


def test_archive_tier_is_skipped_and_can_be_allowed():
    page = blob_xml([{"name": "cold.txt", "props": props(tier="Archive")}, {"name": "hot.txt", "props": props()},
                     {"name": "cool.txt", "props": props(tier="Cool")}])
    c = mock_connector(lambda r: httpx.Response(200, text=page))
    assert [x.file.path for x in list(c.scan({}))[0].changes] == ["hot.txt", "cool.txt"] and c.stats == {"archived": 1}
    allow = mock_connector(lambda r: httpx.Response(200, text=page), skip_archived=False)
    assert len(list(allow.scan({}))[0].changes) == 3


def test_authentication_errors_expose_only_the_status_and_the_error_code():
    nasty = ('<?xml version="1.0" encoding="utf-8"?><Error><Code>AuthenticationFailed</Code><Message>Server failed to authenticate the '
             "request.</Message><AuthenticationErrorDetail>The MAC signature found in the HTTP request 'SECRETSIG123==' is not the same "
             "as any computed signature. Server used following string to sign: 'GET\\n\\n...'</AuthenticationErrorDetail></Error>")
    c = mock_connector(lambda r: httpx.Response(403, text=nasty))
    with pytest.raises(AuthError) as err:
        list(c.scan({}))
    assert str(err.value) == "Azure Blob rejected the credentials (403, AuthenticationFailed)" and err.value.status == 403
    odd = mock_connector(lambda r: httpx.Response(403, text="<html>Forbidden</html>"))
    with pytest.raises(AuthError) as err2:
        list(odd.scan({}))
    assert str(err2.value) == "Azure Blob rejected the credentials (403)"
    with pytest.raises(AuthError) as err3:
        c.open(SourceFile("a.txt", "a.txt", "a.txt", 1, None, "v", []))
    assert str(err3.value) == "Azure Blob rejected the credentials (403, AuthenticationFailed)"


def test_malformed_xml_is_a_clear_error_not_an_empty_listing():
    c = mock_connector(lambda r: httpx.Response(200, text="<EnumerationResults><Blobs><Blob>"))
    with pytest.raises(HttpError, match="malformed XML"):
        list(c.scan({}))
    bomb = mock_connector(lambda r: httpx.Response(200, text='<!DOCTYPE x [<!ENTITY a "aaaa">]><EnumerationResults>&a;</EnumerationResults>'))
    with pytest.raises(HttpError, match="DOCTYPE"):
        list(bomb.scan({}))
