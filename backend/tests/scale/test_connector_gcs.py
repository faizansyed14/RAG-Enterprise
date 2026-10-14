"""Google Cloud Storage connector tests (MOCK fixtures, not a real service).

There is no pip-installable GCS emulator, so these tests run the connector against `FakeGcs`: a stateful in-memory imitation of the
Cloud Storage JSON API behind an httpx.MockTransport. It implements objects.list (prefix, maxResults, opaque pageToken, `fields`
partial responses, `items` absent for an empty listing, userProject for Requester Pays buckets), objects.get (`alt=media`, the object
name as ONE percent-encoded path segment, `generation=`, optional Object Versioning, gzip-stored objects with decompressive transcoding),
Bearer authentication, Google-shaped error bodies (`error.errors[].reason`) and the OAuth token endpoint of the service-account flow,
which verifies the RS256 signature of the JWT with the public key. Response bodies follow the documented shapes (storage#objects,
storage#object). Passing here proves the connector's logic against these shapes; it does not prove Google behaves identically.
"""

import base64
import gzip
import hashlib
import itertools
import json
import re
import time
from urllib.parse import parse_qs, quote, unquote

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from app.scale import sync
from app.scale.connectors._http import AuthError, HttpError, TooLarge
from app.scale.connectors.base import SourceFile
from app.scale.connectors.gcs import LIST_FIELDS, SCOPE, GcsConnector

TOKEN = "ya29.a0AfH6SMB-ACCESS-TOKEN-MUST-NOT-LEAK"
BUCKET = "acme-docs"


def error_body(code, reason, message="boom"):
    return {"error": {"code": code, "message": message, "errors": [{"message": message, "domain": "global", "reason": reason}]}}


def reply(status, body, headers=None):
    return httpx.Response(status, json=body, headers=headers or {})


class FakeGcs:
    def __init__(self, bucket=BUCKET):
        self.bucket = bucket
        self.objects: dict[str, dict] = {}
        self.old: dict[tuple[str, int], dict] = {}                 # noncurrent generations (Object Versioning)
        self.versioning = False
        self.requester_pays = False
        self.public = False
        self.generations = itertools.count(1_758_000_000_000_001)   # real generations are microsecond timestamps
        self.clock = itertools.count(1)
        self.issued: set[str] = set()
        self.expired: set[str] = set()
        self.requests: list[httpx.Request] = []
        self.list_bodies: list[dict] = []
        self.script: list[tuple[str, httpx.Response]] = []
        self.valid_token = TOKEN
        self.token_requests: list[dict] = []
        self.token_response: httpx.Response | None = None
        self.sa_public_key = None
        self.last_claims: dict = {}

    # ── state ──

    def put(self, name, data=b"", *, content_type="text/plain", storage_class="STANDARD", compress=False, **extra):
        current = self.objects.get(name)
        if current is not None and self.versioning:
            self.old[(name, current["generation"])] = current
        stored = gzip.compress(data) if compress else data
        self.objects[name] = {"name": name, "generation": next(self.generations), "metageneration": 1, "data": data, "stored": stored,
                              "compress": compress, "contentType": content_type, "storageClass": storage_class,
                              "updated": f"2026-09-01T10:{next(self.clock):02d}:00.000Z", **extra}

    def patch(self, name, **fields):
        """A metadata-only update: metageneration moves, generation does not."""
        self.objects[name]["metageneration"] += 1
        self.objects[name].update(fields)

    def delete(self, name):
        current = self.objects.pop(name)
        if self.versioning:
            self.old[(name, current["generation"])] = current

    def expire_tokens(self):
        self.expired |= self.issued

    def generation_of(self, name):
        return str(self.objects[name]["generation"])

    def calls(self, kind):
        return [r for r in self.requests if r.url.host != "oauth2.googleapis.com" and ((kind == "list") == r.url.raw_path.decode().split("?")[0].endswith("/o"))]

    # ── protocol ──

    def resource(self, obj):
        name, gen = obj["name"], obj["generation"]
        out = {
            "kind": "storage#object", "id": f"{self.bucket}/{name}/{gen}",
            "selfLink": f"https://www.googleapis.com/storage/v1/b/{self.bucket}/o/{quote(name, safe='')}",
            "mediaLink": f"https://storage.googleapis.com/download/storage/v1/b/{self.bucket}/o/{quote(name, safe='')}?generation={gen}&alt=media",
            "name": name, "bucket": self.bucket, "generation": str(gen), "metageneration": str(obj["metageneration"]),
            "contentType": obj["contentType"], "storageClass": obj["storageClass"], "size": str(len(obj["stored"])),
            "md5Hash": base64.b64encode(hashlib.md5(obj["stored"]).digest()).decode(), "crc32c": "AAAAAA==", "etag": "CJm1",
            "timeCreated": "2026-08-30T08:00:00.000Z", "updated": obj["updated"], "timeStorageClassUpdated": "2026-08-30T08:00:00.000Z",
        }
        if obj["compress"]:
            out["contentEncoding"] = "gzip"
        if obj.get("customer_encrypted"):
            out["customerEncryption"] = {"encryptionAlgorithm": "AES256", "keySha256": "abc="}
        if obj.get("no_generation"):
            del out["generation"]
        return out

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.host == "oauth2.googleapis.com":
            return self.token_endpoint(request)
        for index, (needle, response) in enumerate(self.script):
            if needle in str(request.url):
                self.script.pop(index)
                return response
        auth = request.headers.get("authorization")
        if not (self.public and auth is None) and auth != f"Bearer {self.valid_token}":
            return reply(401, error_body(401, "authError", "Invalid Credentials"))
        match = re.fullmatch(r"/storage/v1/b/([^/]+)/o(?:/([^/]+))?", request.url.raw_path.decode().split("?")[0])
        assert match, request.url.raw_path
        if match.group(1) != self.bucket:
            return reply(404, error_body(404, "notFound", "The specified bucket does not exist."))
        p = request.url.params
        if self.requester_pays and not p.get("userProject"):
            return reply(400, error_body(400, "required", "Bucket is a requester pays bucket but no user project provided."))
        if match.group(2) is None:
            return self.list(p)
        return self.get(unquote(match.group(2)), p, request)

    def list(self, p):
        prefix, after = p.get("prefix", ""), ""
        if p.get("pageToken"):
            token = p["pageToken"]
            try:
                decoded = json.loads(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)))
            except ValueError:
                decoded = {}
            if token in self.expired or token not in self.issued or decoded.get("p") != prefix:
                return reply(400, error_body(400, "invalid", f"Invalid pageToken value '{token}'."))
            after = decoded["a"]
        size = int(p.get("maxResults", 1000))
        assert 1 <= size <= 1000
        names = sorted(n for n in self.objects if n.startswith(prefix) and n > after)
        page = names[:size]
        body: dict = {"kind": "storage#objects"}
        if len(names) > size:
            token = base64.urlsafe_b64encode(json.dumps({"a": page[-1], "p": prefix, "n": len(self.issued)}).encode()).decode().rstrip("=")
            self.issued.add(token)
            body["nextPageToken"] = token
        if page:                                                      # an empty listing has NO "items" key
            items = [self.resource(self.objects[n]) for n in page]
            if p.get("fields"):
                inner = re.search(r"items\(([^)]*)\)", p["fields"]).group(1).split(",")
                items = [{k: v for k, v in item.items() if k in inner} for item in items]
            body["items"] = items
        if p.get("fields"):
            body = {k: v for k, v in body.items() if k in ("nextPageToken", "items")}
        self.list_bodies.append(body)
        return reply(200, body)

    def get(self, name, p, request):
        obj = self.objects.get(name)
        if p.get("generation") is not None:
            wanted = int(p["generation"])
            obj = obj if obj and obj["generation"] == wanted else self.old.get((name, wanted))
        if obj is None:
            return reply(404, error_body(404, "notFound", f"No such object: {self.bucket}/{name}"))
        if p.get("alt") != "media":
            return reply(200, self.resource(obj))
        if obj.get("forbidden"):
            return reply(403, error_body(403, "forbidden", f"caller does not have storage.objects.get access to the Google Cloud Storage object {name}"))
        headers = {"x-goog-generation": str(obj["generation"]), "content-type": obj["contentType"]}
        if obj["compress"] and "gzip" in request.headers.get("accept-encoding", ""):
            return httpx.Response(200, content=obj["stored"], headers={**headers, "content-encoding": "gzip", "x-goog-stored-content-encoding": "gzip"})
        return httpx.Response(200, content=obj["data"], headers=headers)

    def token_endpoint(self, request):
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        self.token_requests.append(form)
        if self.token_response is not None:
            return self.token_response
        header_b64, claims_b64, sig_b64 = form["assertion"].split(".")
        pad = lambda s: s + "=" * (-len(s) % 4)  # noqa: E731
        self.sa_public_key.verify(base64.urlsafe_b64decode(pad(sig_b64)), f"{header_b64}.{claims_b64}".encode(), padding.PKCS1v15(), hashes.SHA256())
        self.last_claims = json.loads(base64.urlsafe_b64decode(pad(claims_b64)))
        self.valid_token = f"ya29.minted-{len(self.token_requests)}-" + "x" * 20
        return reply(200, {"access_token": self.valid_token, "expires_in": 3599, "token_type": "Bearer"})


class Clock:
    def __init__(self, now=1_800_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("T_GCS_TOKEN", TOKEN)


@pytest.fixture
def site():
    return FakeGcs()


def make(site, **cfg):
    sleeps: list[float] = []
    clock = cfg.pop("clock", None) or Clock()
    config = {"bucket": BUCKET, "access_token_env": "T_GCS_TOKEN", "default_acl": ["*"], "page_size": 3, **cfg}
    connector = GcsConnector(config, transport=httpx.MockTransport(site.handler), sleep=sleeps.append, clock=clock)
    connector.sleeps, connector.clock = sleeps, clock
    return connector


def scan_all(connector, cursor=None):
    batches = list(itertools.islice(connector.scan(cursor or {}), 200))
    assert len(batches) < 200, "the scan never terminated"
    return [c for b in batches for c in b.changes], batches


def by_id(changes):
    return {c.external_id: c for c in changes}


def persisted(sf):
    """What the sync layer keeps of a SourceFile: no `extra`."""
    return SourceFile(sf.external_id, sf.path, sf.name, sf.size, None, sf.version, list(sf.acl))


def read(connector, sf):
    with connector.open(persisted(sf)) as fh:
        return fh.read()


NAMES = ["README.md", "docs/2026/q1.pdf", "docs/2026/q2.pdf", "docs/a.txt", "docs/b.txt", "img/logo.png", "z.csv"]


def populate(site):
    site.put("README.md", b"# readme", content_type="text/markdown")
    site.put("docs/a.txt", b"alpha text")
    site.put("docs/b.txt", b"bravo text")
    site.put("docs/2026/q1.pdf", b"%PDF-1.4 q1", content_type="application/pdf")
    site.put("docs/2026/q2.pdf", b"%PDF-1.4 q2", content_type="application/pdf")
    site.put("img/logo.png", b"\x89PNG....", content_type="image/png")
    site.put("z.csv", b"a,b\n1,2\n", content_type="text/csv")


# ═══════════════════════════ listing and pagination ═══════════════════════════


def test_listing_follows_next_page_token_to_the_last_page_with_the_documented_parameters(site):
    populate(site)
    changes, batches = scan_all(make(site))
    assert [c.external_id for c in changes] == NAMES                                     # every object once, in name order
    assert [len(b.changes) for b in batches] == [3, 3, 1] and [b.done for b in batches] == [False, False, True]
    lists = site.calls("list")
    assert len(lists) == 3 and all(r.url.raw_path.decode().startswith(f"/storage/v1/b/{BUCKET}/o?") for r in lists)
    assert all(r.url.params["maxResults"] == "3" for r in lists)
    assert "pageToken" not in lists[0].url.params
    assert lists[1].url.params["pageToken"] == site.list_bodies[0]["nextPageToken"]      # the token the server handed out, untouched
    assert lists[2].url.params["pageToken"] == site.list_bodies[1]["nextPageToken"]
    assert not any(k in lists[0].url.params for k in ("prefix", "delimiter", "versions", "userProject"))
    assert all(r.headers["authorization"] == f"Bearer {TOKEN}" for r in lists)


def test_a_partial_response_is_requested_with_the_fields_the_connector_reads(site):
    populate(site)
    scan_all(make(site))
    fields = site.calls("list")[0].url.params["fields"]
    assert fields == LIST_FIELDS and fields.startswith("nextPageToken,items(")
    inner = fields[fields.index("(") + 1:-1].split(",")
    assert {"name", "generation", "size", "updated", "contentType", "storageClass", "customerEncryption"} <= set(inner)
    assert set(site.list_bodies[0]["items"][0]) == set(inner) - {"customerEncryption"}     # the fake sent nothing else


def test_cursor_is_the_next_page_token_and_the_last_batch_clears_it(site):
    populate(site)
    _, batches = scan_all(make(site))
    assert [b.cursor for b in batches] == [{"page": site.list_bodies[0]["nextPageToken"]}, {"page": site.list_bodies[1]["nextPageToken"]}, {}]
    json.dumps([b.cursor for b in batches])


def test_only_a_listing_from_the_first_object_to_the_last_is_authoritative(site):
    populate(site)
    _, batches = scan_all(make(site))
    assert [b.complete_scope for b in batches] == [None, None, ""]
    _, resumed = scan_all(make(site), batches[0].cursor)
    assert [b.complete_scope for b in resumed] == [None, None]                           # resumed: could have missed the first page
    assert resumed[-1].done


def test_a_resumed_scan_continues_at_the_saved_page_token(site):
    populate(site)
    first = next(iter(make(site).scan({})))
    site.requests.clear()
    changes, batches = scan_all(make(site), first.cursor)
    assert [c.external_id for c in changes] == NAMES[3:]
    assert site.calls("list")[0].url.params["pageToken"] == first.cursor["page"] and len(site.calls("list")) == 2
    assert batches[-1].cursor == {} and batches[-1].done


def test_a_page_token_the_server_rejects_restarts_from_the_beginning_and_becomes_authoritative(site):
    populate(site)
    first = next(iter(make(site).scan({})))
    site.expire_tokens()                                                                 # the saved token is no longer valid
    site.requests.clear()
    changes, batches = scan_all(make(site), first.cursor)
    assert [c.external_id for c in changes] == NAMES
    lists = site.calls("list")
    assert lists[0].url.params["pageToken"] == first.cursor["page"] and "pageToken" not in lists[1].url.params
    assert [b.complete_scope for b in batches][-1] == "" and batches[-1].done


def test_a_second_rejected_token_in_one_scan_is_an_error_and_so_is_a_400_without_a_token(site):
    populate(site)
    first = next(iter(make(site).scan({})))
    site.script = [("pageToken", reply(400, error_body(400, "invalid", "Invalid pageToken value"))),
                   ("pageToken", reply(400, error_body(400, "invalid", "Invalid pageToken value")))]
    site.requests.clear()
    with pytest.raises(HttpError) as err:
        scan_all(make(site), first.cursor)
    assert err.value.status == 400 and len(site.calls("list")) == 3                      # rejected, restarted, rejected again: stop
    plain = FakeGcs()
    plain.script = [("maxResults", reply(400, error_body(400, "invalid", "Invalid prefix")))]
    with pytest.raises(HttpError) as err:
        scan_all(make(plain))
    assert err.value.status == 400 and len(plain.requests) == 1


def test_a_page_token_that_comes_back_again_is_an_error_not_a_loop(site):
    site.script = [("storage/v1", reply(200, {"kind": "storage#objects", "nextPageToken": "same", "items": []})) for _ in range(2)]
    with pytest.raises(HttpError, match="same nextPageToken twice"):
        scan_all(make(site))
    assert len(site.requests) == 2


def test_an_empty_listing_has_no_items_key_and_is_authoritative(site):
    changes, batches = scan_all(make(site))
    assert "items" not in site.list_bodies[0]
    assert changes == [] and len(batches) == 1 and batches[0].done and batches[0].complete_scope == ""


def test_a_page_of_only_skipped_objects_does_not_end_the_scan(site):
    site.put("a/", b"", content_type="application/x-www-form-urlencoded;charset=UTF-8")  # the console's folder placeholders
    site.put("b/.keep", b"x")
    site.put("c.txt", b"real")
    changes, batches = scan_all(make(site, page_size=2))
    assert [c.external_id for c in changes] == ["c.txt"]
    assert [len(b.changes) for b in batches] == [0, 1] and batches[-1].done


def test_page_size_is_capped_at_the_documented_maximum_of_1000(site):
    populate(site)
    scan_all(make(site, page_size=5000))
    assert site.calls("list")[0].url.params["maxResults"] == "1000"


def test_a_non_object_answer_is_an_error(site):
    site.script = [("storage/v1", reply(200, ["not", "an", "object"]))]
    with pytest.raises(HttpError, match="not a JSON object"):
        scan_all(make(site))


def test_a_missing_bucket_is_a_404_error_not_an_empty_authoritative_listing(site):
    with pytest.raises(HttpError) as err:
        scan_all(make(site, bucket="no-such-bucket"))
    assert err.value.status == 404 and not isinstance(err.value, AuthError) and "bucket does not exist" in str(err.value)


# ═══════════════════════════ prefix, paths, names ═══════════════════════════


def test_prefix_scopes_the_listing_and_paths_are_relative_to_it(site):
    populate(site)
    site.put("docs-old/z.txt", b"older")
    c = make(site, prefix="docs")                                                        # no trailing slash: must not match docs-old/
    changes, _ = scan_all(c)
    assert all(r.url.params["prefix"] == "docs/" for r in site.calls("list"))
    got = by_id(changes)
    assert sorted(got) == ["docs/2026/q1.pdf", "docs/2026/q2.pdf", "docs/a.txt", "docs/b.txt"]       # external ids stay full names
    assert got["docs/2026/q1.pdf"].file.path == "2026/q1.pdf" and got["docs/a.txt"].file.path == "a.txt"
    assert got["docs/a.txt"].file.name == "a.txt"
    assert make(site, prefix="/docs/").prefix == "docs/" and make(site, prefix="docs/2026/").prefix == "docs/2026/"


def test_names_with_slashes_spaces_and_unicode_keep_their_exact_external_id(site):
    site.put("Quarterly Reports/2026 Q1 (final).txt", b"x")
    site.put("日本/ü ber.txt", b"y")
    changes, _ = scan_all(make(site))
    assert sorted(c.external_id for c in changes) == ["Quarterly Reports/2026 Q1 (final).txt", "日本/ü ber.txt"]
    assert {c.file.name for c in changes} == {"2026 Q1 (final).txt", "ü ber.txt"}


# ═══════════════════════════ what is skipped ═══════════════════════════


def test_unreadable_or_pointless_objects_are_skipped_and_counted(site):
    site.put("keep.txt", b"keep")
    site.put("folder/", b"")                                                             # folder placeholder
    site.put("empty.txt", b"")
    site.put("docs/.keep", b"x")                                                         # skip_names
    site.put("Thumbs.db", b"x")
    site.put("~$lock.docx", b"x")
    site.put("big.bin", b"x" * 50)
    site.put("secret.txt", b"enc", customer_encrypted=True)
    site.put("nogen.txt", b"x", no_generation=True)
    c = make(site, max_file_bytes=40)
    changes, _ = scan_all(c)
    assert [x.external_id for x in changes] == ["keep.txt"]
    assert c.stats == {"folder_marker": 1, "empty": 1, "skipped_name": 3, "oversize": 1, "customer_encrypted": 1, "no_generation": 1}


def test_cold_storage_classes_are_read_unless_configured_away_and_the_class_match_ignores_case(site):
    for cls in ("STANDARD", "NEARLINE", "COLDLINE", "ARCHIVE"):
        site.put(f"{cls.lower()}.txt", b"x", storage_class=cls)
    assert len(scan_all(make(site))[0]) == 4
    c = make(site, skip_storage_classes=["archive", "Coldline"])
    assert sorted(x.external_id for x in scan_all(c)[0]) == ["nearline.txt", "standard.txt"] and c.stats == {"storage_class": 2}


def test_custom_skip_names_replace_the_defaults(site):
    site.put("a.tmp", b"x")
    site.put(".keep", b"x")
    assert [c.external_id for c in scan_all(make(site, skip_names=["*.tmp"]))[0]] == [".keep"]


# ═══════════════════════════ versions, metadata ═══════════════════════════


def test_version_is_the_generation_which_moves_on_overwrite_but_not_on_a_metadata_update(site):
    populate(site)
    before = {c.external_id: c.file.version for c in scan_all(make(site))[0]}
    assert before["docs/a.txt"] == site.generation_of("docs/a.txt") and len(set(before.values())) == 7
    assert before == {c.external_id: c.file.version for c in scan_all(make(site))[0]}   # nothing changed: nothing new
    site.patch("docs/b.txt", contentType="text/x-notes")                                  # metageneration only
    site.put("docs/a.txt", b"alpha text")                                                # the very same bytes uploaded again
    after = {c.external_id: c.file.version for c in scan_all(make(site))[0]}
    assert after["docs/b.txt"] == before["docs/b.txt"]
    assert after["docs/a.txt"] == site.generation_of("docs/a.txt") != before["docs/a.txt"]
    assert {k for k in after if after[k] != before[k]} == {"docs/a.txt"}


def test_size_modified_time_and_mime_come_from_the_listing(site):
    site.put("docs/q1.pdf", b"%PDF-1.4 q1", content_type="application/pdf")
    site.put("notes", b"no extension", content_type="text/plain; charset=utf-8")
    site.put("mystery.csv", b"1,2", content_type="application/octet-stream")              # the default type of an upload: guess by name
    site.put("unknown.zzz", b"x", content_type="application/octet-stream")
    got = {c.external_id: c.file for c in scan_all(make(site))[0]}
    pdf = got["docs/q1.pdf"]
    assert pdf.size == len(b"%PDF-1.4 q1") and pdf.mime == "application/pdf"
    assert pdf.modified_at.isoformat() == "2026-09-01T10:01:00+00:00"
    assert got["notes"].mime == "text/plain"
    assert got["mystery.csv"].mime == "text/csv" and got["unknown.zzz"].mime == ""


def test_secrets_never_reach_cursors_or_source_files(site):
    populate(site)
    c = make(site)
    changes, batches = scan_all(c)
    assert TOKEN not in repr(changes) + repr([b.cursor for b in batches]) + json.dumps(c.cfg)


# ═══════════════════════════ permissions ═══════════════════════════


def test_acl_rules_apply_by_path_relative_to_the_prefix_longest_first_and_fail_closed(site):
    populate(site)
    rules = [{"prefix": "docs", "principals": ["group:Staff"]}, {"prefix": "docs/2026", "principals": ["group:finance"]}]
    c = make(site, acl_rules=rules, default_acl=None)
    got = {x.external_id: x.file for x in scan_all(c)[0]}
    assert got["docs/a.txt"].acl == ["group:staff"] and got["docs/2026/q1.pdf"].acl == ["group:finance"]
    assert got["README.md"].acl == [] and got["img/logo.png"].acl == []                  # no rule, no default: nobody
    assert c.refresh_acl(persisted(got["docs/2026/q1.pdf"])) == ["group:finance"] and c.refresh_acl(persisted(got["z.csv"])) == []
    scoped = make(site, prefix="docs", acl_rules=[{"prefix": "2026", "principals": ["group:finance"]}], default_acl=["group:all"])
    got = {x.external_id: x.file for x in scan_all(scoped)[0]}
    assert got["docs/2026/q1.pdf"].acl == ["group:finance"] and got["docs/a.txt"].acl == ["group:all"]   # the rule's prefix is below `prefix`
    assert got["docs/2026/q1.pdf"].path == "2026/q1.pdf"
    assert scoped.refresh_acl(persisted(got["docs/2026/q1.pdf"])) == ["group:finance"]                   # refresh uses the relative path too
    assert scoped.refresh_acl(persisted(got["docs/a.txt"])) == ["group:all"]


# ═══════════════════════════ open() ═══════════════════════════


def test_open_returns_the_bytes_of_the_generation_that_was_listed(site):
    populate(site)
    c = make(site)
    got = {x.external_id: x.file for x in scan_all(c)[0]}
    site.requests.clear()
    assert read(c, got["docs/a.txt"]) == b"alpha text"
    (request,) = site.requests
    assert request.url.raw_path.decode() == f"/storage/v1/b/{BUCKET}/o/docs%2Fa.txt?alt=media&generation={site.generation_of('docs/a.txt')}"
    assert request.headers["authorization"] == f"Bearer {TOKEN}"


@pytest.mark.parametrize("name,encoded", [
    ("dir/sub/file.txt", "dir%2Fsub%2Ffile.txt"),
    ("a b/c#d?e%f+g.txt", "a%20b%2Fc%23d%3Fe%25f%2Bg.txt"),
    ("日本/ü.txt", "%E6%97%A5%E6%9C%AC%2F%C3%BC.txt"),
    ("100%25 sure.txt", "100%2525%20sure.txt"),
])
def test_object_names_are_one_percent_encoded_path_segment(site, name, encoded):
    site.put(name, b"payload " + name.encode())
    c = make(site)
    (change,) = scan_all(c)[0]
    site.requests.clear()
    assert read(c, change.file) == b"payload " + name.encode()
    assert site.requests[0].url.raw_path.decode().split("?")[0] == f"/storage/v1/b/{BUCKET}/o/{encoded}"


def test_open_works_from_the_persisted_fields_only(site):
    populate(site)
    c = make(site)
    got = {x.external_id: x.file for x in scan_all(c)[0]}
    fresh = make(site)                                                                   # a worker that never scanned
    assert read(fresh, got["z.csv"]) == b"a,b\n1,2\n"


def test_a_replaced_object_is_a_clear_404_in_a_bucket_without_versioning_and_the_old_bytes_with_versioning(site):
    site.put("doc.txt", b"first")
    c = make(site)
    (change,) = scan_all(c)[0]
    site.put("doc.txt", b"second")                                                       # overwritten after the scan
    with pytest.raises(HttpError, match="deleted or replaced since it was listed") as err:
        read(c, change.file)
    assert err.value.status == 404 and f"generation {change.file.version}" in str(err.value)
    assert not isinstance(err.value, AuthError)
    versioned = FakeGcs()
    versioned.versioning = True
    versioned.put("doc.txt", b"first")
    cv = make(versioned)
    (change,) = scan_all(cv)[0]
    versioned.put("doc.txt", b"second")
    assert read(cv, change.file) == b"first"                                             # the bytes that match the recorded version
    assert [c2.file.version for c2 in scan_all(cv)[0]] == [versioned.generation_of("doc.txt")]


def test_a_version_that_is_not_a_generation_is_not_sent_as_one(site):
    site.put("doc.txt", b"data")
    c = make(site)
    (change,) = scan_all(c)[0]
    blank = SourceFile("doc.txt", "doc.txt", "doc.txt", 4, None, "", [])
    odd = SourceFile("doc.txt", "doc.txt", "doc.txt", 4, None, "etag-123&x=1", [])
    site.requests.clear()
    assert read(c, blank) == b"data" and "generation" not in site.requests[0].url.params
    assert read(c, odd) == b"data" and "generation" not in site.requests[1].url.params
    assert change.file.version.isdigit()


def test_a_gzip_stored_object_opens_as_its_plain_bytes_and_the_cap_counts_decoded_bytes(site):
    text = b"line of text\n" * 100
    site.put("logs/app.txt", text, compress=True)
    c = make(site)
    (change,) = scan_all(c)[0]
    assert change.file.size == len(site.objects["logs/app.txt"]["stored"]) < len(text)    # the listing reports the stored size
    assert read(c, change.file) == text
    assert "gzip" in site.requests[-1].headers["accept-encoding"]
    site.put("bomb.txt", b"\0" * 5_000_000, compress=True)                               # tiny when stored, huge when decoded
    small = make(site, max_file_bytes=1_000_000)
    bomb = by_id(scan_all(small)[0])["bomb.txt"].file
    with pytest.raises(TooLarge):
        read(small, bomb)


def test_a_403_on_one_object_is_an_auth_error_that_names_the_permission(site):
    site.put("locked.txt", b"data", forbidden=True)
    c = make(site)
    (change,) = scan_all(c)[0]
    with pytest.raises(AuthError) as err:
        read(c, change.file)
    assert err.value.status == 403 and "storage.objects.get" in str(err.value) and TOKEN not in str(err.value)


# ═══════════════════════════ deletions through the sync layer ═══════════════════════════


def test_a_deleted_object_is_absent_from_the_next_complete_listing(site):
    populate(site)
    c = make(site)
    before, _ = scan_all(c)
    site.delete("docs/b.txt")
    after, batches = scan_all(c)
    assert {x.external_id for x in before} - {x.external_id for x in after} == {"docs/b.txt"}
    assert not any(x.kind == "delete" for x in after) and batches[-1].complete_scope == ""


def test_sync_layer_deletes_overwrites_and_resumes_through_complete_scope(site):
    from sqlalchemy import text
    from app.scale.db import engine

    populate(site)
    c = make(site)
    # source_connections.kind has a CHECK (local|smb|graph) until the integrator's migration widens it; run_scan only
    # uses the kind to build a connector when none is passed
    cid = sync.create_connection("local", f"t-{time.time_ns()}", {"root": "/unused"})
    stats = sync.run_scan(cid, connector=c)
    assert stats["upserts"] == 7 and stats["queued"] == 7 and stats["deletes"] == 0

    def rows():
        with engine().connect() as conn:
            return {r[0]: (r[1], r[2]) for r in conn.execute(
                text("SELECT external_id, deleted, version FROM source_items WHERE connection_id = :c"), {"c": cid}).all()}

    site.delete("docs/b.txt")
    site.put("z.csv", b"a,b\n3,4\n", content_type="text/csv")
    stats = sync.run_scan(cid, connector=c)
    now = rows()
    assert stats["deletes"] == 1 and now["docs/b.txt"][0] is True and now["docs/a.txt"][0] is False
    assert now["z.csv"][1] == site.generation_of("z.csv")                                 # the generation is what is stored as the version

    # a scan that dies half way is resumed at the saved page token and, being a resumed one, deletes nothing
    site.delete("README.md")
    site.script = [("pageToken", reply(500, error_body(500, "backendError", "boom"))) for _ in range(20)]
    with pytest.raises(HttpError):
        sync.run_scan(cid, connector=make(site, max_retries=0))
    assert rows()["README.md"][0] is False
    site.script = []
    site.requests.clear()
    stats = sync.run_scan(cid, connector=c)
    assert "pageToken" in site.calls("list")[0].url.params and stats["deletes"] == 0
    assert rows()["README.md"][0] is False                                               # not seen, but a resumed listing is not authoritative
    stats = sync.run_scan(cid, connector=c)                                              # the next complete listing settles it
    assert stats["deletes"] == 1 and rows()["README.md"][0] is True


# ═══════════════════════════ authentication ═══════════════════════════


def test_the_access_token_is_read_from_the_environment_on_every_request(site, monkeypatch):
    populate(site)
    c = make(site)
    scan_all(c)
    site.valid_token = "rotated-token-value"
    monkeypatch.setenv("T_GCS_TOKEN", "rotated-token-value")
    changes, _ = scan_all(c)
    assert len(changes) == 7 and site.requests[-1].headers["authorization"] == "Bearer rotated-token-value"


def test_a_missing_token_variable_is_named_and_nothing_is_sent(site, monkeypatch):
    monkeypatch.delenv("T_GCS_TOKEN")
    with pytest.raises(HttpError, match="T_GCS_TOKEN") as err:
        scan_all(make(site))
    assert site.requests == [] and not isinstance(err.value, AuthError)


def test_401_is_an_auth_error_with_a_hint_that_is_not_retried_and_does_not_leak_the_token(site, monkeypatch):
    populate(site)
    monkeypatch.setenv("T_GCS_TOKEN", "ya29.wrong-token-value-also-secret")
    c = make(site)
    with pytest.raises(AuthError) as err:
        scan_all(c)
    message = str(err.value) + repr(err.value) + repr(err.value.args)
    assert err.value.status == 401 and "Invalid Credentials" in message and "T_GCS_TOKEN" in message
    assert "wrong-token-value" not in message and TOKEN not in message
    assert c.sleeps == [] and len(site.requests) == 1


def test_401_on_a_download_is_the_same_clear_error(site, monkeypatch):
    populate(site)
    c = make(site)
    got = by_id(scan_all(c)[0])
    site.valid_token = "other"
    with pytest.raises(AuthError) as err:
        read(c, got["z.csv"].file)
    assert err.value.status == 401 and TOKEN not in str(err.value) and c.sleeps == []


def test_403_on_the_listing_names_the_bucket_and_the_permissions_and_is_not_retried(site):
    body = error_body(403, "forbidden", "svc@proj.iam.gserviceaccount.com does not have storage.objects.list access to the Google Cloud "
                                        "Storage bucket. Permission 'storage.objects.list' denied on resource (or it may not exist).")
    site.script = [("storage/v1", reply(403, body))]
    c = make(site)
    with pytest.raises(AuthError) as err:
        scan_all(c)
    message = str(err.value)
    assert err.value.status == 403 and "forbidden" in message and f"bucket {BUCKET}" in message
    assert "storage.objects.list and storage.objects.get" in message and "user_project" in message
    assert c.sleeps == [] and len(site.requests) == 1


def test_the_403_hint_stops_suggesting_user_project_once_it_is_set(site):
    site.script = [("storage/v1", reply(403, error_body(403, "forbidden", "no access")))]
    with pytest.raises(AuthError) as err:
        scan_all(make(site, user_project="billing-proj-1"))
    assert "Storage Object Viewer" in str(err.value) and "user_project" not in str(err.value)


def test_a_redirect_is_not_followed_so_credentials_cannot_travel(site):
    site.script = [("storage/v1", httpx.Response(307, headers={"Location": "https://elsewhere.example/o"}))]
    with pytest.raises(HttpError) as err:
        scan_all(make(site))
    assert err.value.status == 307 and {r.url.host for r in site.requests} == {"storage.googleapis.com"}


def test_a_server_that_echoes_the_token_does_not_get_it_into_the_error(site):
    site.script = [("storage/v1", reply(401, {"error": {"code": 401, "message": f"bad credentials {TOKEN} for caller", "errors": []}}))]
    with pytest.raises(AuthError) as err:
        scan_all(make(site))
    assert TOKEN not in str(err.value) and "***" in str(err.value)
    site.script = [("storage/v1", reply(500, {"error": {"code": 500, "message": f"oops {TOKEN}"}}))]
    with pytest.raises(HttpError) as err:
        scan_all(make(site, max_retries=0))
    assert TOKEN not in str(err.value) and "***" in str(err.value)


def test_anonymous_access_sends_no_credentials(site):
    populate(site)
    site.public = True
    c = make(site, access_token_env=None, anonymous=True)
    assert len(scan_all(c)[0]) == 7
    (change,) = [x for x in scan_all(c)[0] if x.external_id == "z.csv"]
    assert read(c, change.file) == b"a,b\n1,2\n"
    assert all("authorization" not in r.headers for r in site.requests)


def test_a_token_provider_replaces_the_environment_variable(site):
    populate(site)
    site.valid_token = "adc-token"
    c = GcsConnector({"bucket": BUCKET, "default_acl": ["*"]}, transport=httpx.MockTransport(site.handler), token_provider=lambda: "adc-token")
    assert len(scan_all(c)[0]) == 7


def test_configuration_errors_are_raised_when_the_connector_is_built():
    base = {"bucket": BUCKET, "access_token_env": "T_GCS_TOKEN"}
    for bad in (None, "", "UPPER", "a", "has space", "-lead", "trail-", "x" * 300):
        with pytest.raises(ValueError, match="bucket"):
            GcsConnector({**base, "bucket": bad})
    with pytest.raises(ValueError, match="configure access_token_env"):
        GcsConnector({"bucket": BUCKET})
    with pytest.raises(ValueError, match="only one credential"):
        GcsConnector({**base, "service_account_file_env": "X"})
    with pytest.raises(ValueError, match="only one credential"):
        GcsConnector({**base, "anonymous": True})
    with pytest.raises(ValueError, match="only one credential"):
        GcsConnector(base, token_provider=lambda: "t")
    GcsConnector({"bucket": "my.bucket_name-1"}, token_provider=lambda: "t")             # dots, underscores and dashes are legal


# ── service-account authentication (JWT bearer, imported from gdrive.py) ──


@pytest.fixture(scope="module")
def sa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def sa_file(tmp_path, sa_key, monkeypatch, site):
    pem = sa_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
    path = tmp_path / "sa.json"
    path.write_text(json.dumps({"type": "service_account", "client_email": "svc@proj.iam.gserviceaccount.com", "private_key": pem,
                                "private_key_id": "kid-1", "token_uri": "https://oauth2.googleapis.com/token"}))
    monkeypatch.setenv("T_GCS_SA_FILE", str(path))
    monkeypatch.delenv("T_GCS_TOKEN", raising=False)
    site.sa_public_key = sa_key.public_key()
    site.valid_token = "none-yet"
    return path


def test_service_account_flow_signs_a_jwt_for_the_read_only_storage_scope_and_caches_the_access_token(site, sa_file):
    populate(site)
    c = make(site, access_token_env=None, service_account_file_env="T_GCS_SA_FILE")
    changes, _ = scan_all(c)
    assert len(changes) == 7 and len(site.token_requests) == 1                           # one token for three list pages
    got = by_id(changes)
    assert read(c, got["z.csv"].file) == b"a,b\n1,2\n" and len(site.token_requests) == 1  # ... and the download
    claims = site.last_claims                                                            # the fake verified the signature
    assert claims["iss"] == "svc@proj.iam.gserviceaccount.com" and claims["aud"] == "https://oauth2.googleapis.com/token"
    assert claims["scope"] == SCOPE == "https://www.googleapis.com/auth/devstorage.read_only"
    assert claims["exp"] - claims["iat"] == 3600 and "sub" not in claims
    assert site.token_requests[0]["grant_type"] == "urn:ietf:params:oauth:grant-type:jwt-bearer"
    header = json.loads(base64.urlsafe_b64decode(site.token_requests[0]["assertion"].split(".")[0] + "=="))
    assert header == {"alg": "RS256", "typ": "JWT", "kid": "kid-1"}
    c.clock.now += 3000
    scan_all(c)
    assert len(site.token_requests) == 1
    c.clock.now += 400                                                                   # inside the 5 minute safety margin: refreshed
    scan_all(c)
    assert len(site.token_requests) == 2


def test_a_minted_access_token_echoed_by_the_server_is_scrubbed_from_the_error(site, sa_file):
    c = make(site, access_token_env=None, service_account_file_env="T_GCS_SA_FILE")
    scan_all(c)
    minted = site.valid_token
    site.script = [("storage/v1", reply(500, {"error": {"code": 500, "message": f"oops {minted}"}}))]
    with pytest.raises(HttpError) as err:
        scan_all(c)
    assert minted not in str(err.value) and "***" in str(err.value)


def test_a_configured_scope_overrides_the_default(site, sa_file):
    populate(site)
    scan_all(make(site, access_token_env=None, service_account_file_env="T_GCS_SA_FILE", scopes=["https://www.googleapis.com/auth/devstorage.full_control"]))
    assert site.last_claims["scope"] == "https://www.googleapis.com/auth/devstorage.full_control"


def test_a_401_with_a_service_account_mints_a_new_token_once_and_retries(site, sa_file):
    populate(site)
    c = make(site, access_token_env=None, service_account_file_env="T_GCS_SA_FILE")
    scan_all(c)
    site.valid_token = "revoked-elsewhere"                                               # the cached token stops working
    changes, _ = scan_all(c)
    assert len(changes) == 7 and len(site.token_requests) == 2


def test_rejected_service_account_credentials_are_a_clear_auth_error_without_the_key(site, sa_file):
    c = make(site, access_token_env=None, service_account_file_env="T_GCS_SA_FILE")
    site.token_response = reply(400, {"error": "invalid_grant", "error_description": "Invalid JWT Signature."})
    with pytest.raises(AuthError) as err:
        scan_all(c)
    message = str(err.value) + repr(err.value)
    assert "invalid_grant" in message and "PRIVATE KEY" not in message and "assertion" not in message and c.sleeps == []


def test_broken_service_account_files_are_reported_by_variable_name_not_content(site, sa_file, monkeypatch, tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text('{"client_email": "x"}')
    monkeypatch.setenv("T_GCS_SA_FILE", str(bad))
    with pytest.raises(AuthError, match="T_GCS_SA_FILE"):
        scan_all(make(site, access_token_env=None, service_account_file_env="T_GCS_SA_FILE"))
    assert site.requests == []


# ═══════════════════════════ billing project, endpoint ═══════════════════════════


def test_a_requester_pays_bucket_needs_user_project_on_the_listing_and_the_download(site):
    populate(site)
    site.requester_pays = True
    with pytest.raises(HttpError, match="requester pays") as err:
        scan_all(make(site))
    assert err.value.status == 400
    site.requests.clear()
    c = make(site, user_project="billing-proj-1")
    changes, _ = scan_all(c)
    assert len(changes) == 7 and all(r.url.params["userProject"] == "billing-proj-1" for r in site.calls("list"))
    site.requests.clear()
    assert read(c, changes[0].file) == b"# readme" and site.requests[0].url.params["userProject"] == "billing-proj-1"


def test_api_base_points_the_connector_at_an_emulator(site):
    populate(site)
    c = make(site, api_base="http://127.0.0.1:4443/")
    changes, _ = scan_all(c)
    assert len(changes) == 7 and all(r.url.host == "127.0.0.1" and r.url.port == 4443 for r in site.requests)
    assert read(c, changes[0].file) == b"# readme" and site.requests[-1].url.host == "127.0.0.1"


# ═══════════════════════════ rate limits and failures ═══════════════════════════


def test_429_is_retried_with_retry_after_on_the_listing_and_the_download(site):
    populate(site)
    site.script = [("storage/v1", httpx.Response(429, headers={"Retry-After": "3"}, json=error_body(429, "rateLimitExceeded", "slow down")))]
    c = make(site)
    changes, _ = scan_all(c)
    assert len(changes) == 7 and c.sleeps == [3.0]
    site.script = [("alt=media", httpx.Response(429, headers={"Retry-After": "5"}, json=error_body(429, "rateLimitExceeded", "slow down")))]
    assert read(c, by_id(changes)["z.csv"].file) == b"a,b\n1,2\n" and c.sleeps == [3.0, 5.0]


def test_google_style_403_rate_limits_are_retried_like_a_429(site):
    populate(site)
    site.script = [("storage/v1", httpx.Response(403, headers={"Retry-After": "2"}, json=error_body(403, "rateLimitExceeded", "Rate Limit Exceeded")))]
    c = make(site)
    assert len(scan_all(c)[0]) == 7 and c.sleeps == [2.0]


def test_503_and_a_500_backend_error_are_retried_and_a_dropped_connection_too(site):
    populate(site)

    class Flaky:
        calls = 0

    def handler(request):
        Flaky.calls += 1
        if Flaky.calls == 1:
            raise httpx.ConnectError("reset by peer")
        return site.handler(request)

    site.script = [("storage/v1", reply(503, error_body(503, "backendError", "unavailable"))),
                   ("storage/v1", reply(500, error_body(500, "backendError", "Internal Error")))]
    sleeps: list[float] = []
    c = GcsConnector({"bucket": BUCKET, "access_token_env": "T_GCS_TOKEN", "default_acl": ["*"], "page_size": 3},
                     transport=httpx.MockTransport(handler), sleep=sleeps.append)
    assert len(scan_all(c)[0]) == 7 and len(sleeps) == 3                                 # connect error, 503, 500 -> 503: three waits


def test_a_throttled_listing_that_never_recovers_is_an_error_after_bounded_retries(site):
    site.script = [("storage/v1", httpx.Response(429, headers={"Retry-After": "1"}, json=error_body(429, "rateLimitExceeded", "x"))) for _ in range(20)]
    c = make(site, max_retries=3)
    with pytest.raises(HttpError) as err:
        scan_all(c)
    assert err.value.status == 429 and len(site.requests) == 4 and c.sleeps == [1.0, 1.0, 1.0]


def test_a_plain_500_is_an_error_not_retried(site):
    site.script = [("storage/v1", reply(500, error_body(500, "internalError-unknown", "boom")))]
    c = make(site)
    with pytest.raises(HttpError) as err:
        scan_all(c)
    assert err.value.status == 500 and c.sleeps == [] and len(site.requests) == 1
