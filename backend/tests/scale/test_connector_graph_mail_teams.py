"""Outlook mail and Teams connectors against a stateful fake of Microsoft Graph: the Entra token endpoint (client credentials),
mail-folder and channel DELTA feeds (`@odata.nextLink` with an opaque $skiptoken, a final `@odata.deltaLink` with a $deltatoken,
`{"id", "@removed": {"reason"}}` stubs, HTTP 410 for an expired token), `Prefer: odata.maxpagesize`, `$select`, single-item GETs,
attachment and reply collections that page through `@odata.nextLink`, channel listing.

The fake keeps a real change log per feed, so the incremental tests drive the connector through realistic sequences (edit, delete,
expire the token) instead of canned pages. Delta tokens and skip tokens are opaque base64 blobs, as in Graph: a connector that edits
a link instead of following it fails.

Written from memory of the reference, not certain of: whether the Teams channel delta carries replies (the fake can do both) and whether a
reply bumps its post's lastModifiedDateTime (the connector does not rely on either), the `$top` limit and `$filter` support of the Teams
delta, the exact `error.code` of a 410 (the connector keys on the status), and the full set of chatMessage fields beyond those used.
"""

import base64
import copy
import json
import re
from datetime import datetime, timezone
from urllib.parse import parse_qs, unquote

import httpx
import pytest

from app.scale.connectors._http import AuthError, HttpError
from app.scale.connectors._objects import short_hash
from app.scale.connectors.base import SourceFile
from app.scale.connectors.graph_mail_teams import OutlookMailConnector, TeamsConnector

TENANT = "7c1e5b3a-0000-4000-8000-123456789abc"
CLIENT = "c1d2e3f4-0000-4000-8000-abcdefabcdef"
SECRET = "Zx~secret-client-value-9q8w7e"
TEAM = "0f1c8bd9-1f14-4f2a-9c5d-7a1c2e3d4f50"
CH1 = "19:2f3e4d5c6b7a8f9e0a1b2c3d4e5f6a7b@thread.tacv2"
CH2 = "19:aabbccddeeff00112233445566778899@thread.tacv2"
USER = "support@acme.com"
GRAPH_HOST = "graph.microsoft.com"


def mid(n):
    return f"AAMkADU2-{n:03d}_Q="                                   # Outlook ids are base64url with '=' padding


def pid(n):
    return str(1616990032000 + n)                                    # Teams message ids are epoch-millisecond strings


def person(name, address):
    return {"emailAddress": {"name": name, "address": address}}


ADA, GRACE, LINUS, MARGARET = (person("Ada Lovelace", "ada@acme.com"), person("Grace Hopper", "grace@acme.com"),
                               person("Linus T", "linus@acme.com"), person("Margaret H", "margaret@acme.com"))


def msg(n, subject, *, received="2026-03-03T09:15:00Z", modified="2026-03-03T09:16:00Z", html=None, text=None, sender=ADA,
        to=(GRACE,), cc=(), attachments=(), draft=False, key=None):
    body = {"contentType": "html", "content": html} if html is not None else {"contentType": "text", "content": text or f"body {n}"}
    return {"@odata.type": "#microsoft.graph.message", "id": mid(n), "subject": subject, "receivedDateTime": received, "sentDateTime": received,
            "lastModifiedDateTime": modified, "changeKey": key or f"CQAAABYAAAB{n:03d}", "isDraft": draft, "hasAttachments": bool(attachments),
            "body": body, "from": sender, "sender": sender, "toRecipients": list(to), "ccRecipients": list(cc), "parentFolderId": "inbox-id",
            "_attachments": list(attachments)}


def attachment(n, name, *, inline=False, kind="application/pdf", size=1234):
    return {"@odata.type": "#microsoft.graph.fileAttachment", "id": f"AAMkAtt{n}=", "name": name, "contentType": kind, "size": size,
            "isInline": inline, "contentBytes": "SEVBVlk="}


def post(n, text, *, subject=None, created="2026-03-03T09:15:00Z", modified=None, html=False, user="Ada Lovelace", reply_to=None,
         kind="message", deleted=None, attachments=()):
    return {"@odata.type": "#microsoft.graph.chatMessage", "id": pid(n), "replyToId": reply_to, "etag": pid(n), "messageType": kind,
            "createdDateTime": created, "lastModifiedDateTime": modified or created, "lastEditedDateTime": None, "deletedDateTime": deleted,
            "subject": subject, "importance": "normal", "chatId": None,
            "from": {"application": None, "device": None, "user": {"id": "u1", "displayName": user, "userIdentityType": "aadUser"}} if user else None,
            "body": {"contentType": "html" if html else "text", "content": text},
            "attachments": [{"id": f"att{n}", "contentType": "reference", "contentUrl": "https://acme.sharepoint.com/x", "name": name} for name in attachments],
            "mentions": [], "reactions": []}


class Feed:
    """The server side of one delta collection: its current items, a change log and a floor under which delta tokens are gone."""

    def __init__(self, items=()):
        self.items: dict[str, dict] = {}
        self.log: list[tuple[int, str, str]] = []
        self.seq = 0
        self.floor = 0
        for item in items:
            self.put(item)

    def put(self, item):
        self.seq += 1
        self.items[item["id"]] = copy.deepcopy(item)
        self.log.append((self.seq, item["id"], "upsert"))

    def remove(self, item_id, reason="deleted"):
        self.seq += 1
        self.items.pop(item_id, None)
        self.log.append((self.seq, item_id, reason))

    def edit(self, item_id, **fields):
        item = self.items[item_id]
        item.update(fields)
        self.put(item)

    def expire(self):
        self.floor = self.seq + 1

    @staticmethod
    def token(*parts):
        return base64.urlsafe_b64encode(json.dumps(list(parts)).encode()).decode().rstrip("=")

    @staticmethod
    def untoken(text):
        return json.loads(base64.urlsafe_b64decode(text + "=" * (-len(text) % 4)))

    def delta(self, base, q, size, project, include=lambda item: True):
        """(status, body) of a delta request."""
        select = None
        if "$deltatoken" in q:
            since, _, select = self.untoken(q["$deltatoken"])
            snap = self.seq
            if since < self.floor:
                return 410, {"error": {"code": "syncStateNotFound", "message": "The sync state generation is not found."}}
            offset = 0
        elif "$skiptoken" in q:
            since, offset, snap, select = self.untoken(q["$skiptoken"])
        else:
            since, offset, snap, select = -1, 0, self.seq, (q.get("$select") or "").split(",") if q.get("$select") else None
        events: dict[str, str] = {}
        for seq, item_id, kind in self.log:
            if since < seq <= snap:
                events[item_id] = kind
        if since == -1:
            entries = [project(i, select) for i in self.items.values() if include(i)]
        else:
            entries = [({"id": i, "@removed": {"reason": k}} if k != "upsert" or i not in self.items else project(self.items[i], select))
                       for i, k in events.items()]
        page = entries[offset:offset + size]
        body = {"@odata.context": "https://graph.microsoft.com/v1.0/$metadata#Collection(message)", "value": page}
        if offset + size < len(entries):
            body["@odata.nextLink"] = f"{base}?$skiptoken={self.token(since, offset + size, snap, select)}"
        else:
            body["@odata.deltaLink"] = f"{base}?$deltatoken={self.token(snap, 0, select)}"
        return 200, body


def project(item, select):
    clean = {k: v for k, v in item.items() if not k.startswith("_")}
    if not select:
        return clean
    return {k: v for k, v in clean.items() if k in select or k in ("id", "@odata.type")}


class Graph:
    def __init__(self, *, page=2, replies_in_feed=False):
        self.mail: dict[tuple[str, str], Feed] = {}
        self.teams: dict[tuple[str, str], Feed] = {}
        self.channels: dict[str, list[dict]] = {}
        self.replies: dict[tuple[str, str], list[dict]] = {}
        self.page, self.replies_in_feed = page, replies_in_feed
        self.requests: list[httpx.Request] = []
        self.token_requests: list[dict] = []
        self.script: list[tuple[str, httpx.Response]] = []
        self.issued = 0
        self.valid = set()
        self.reject_all = False
        self.token_error: httpx.Response | None = None

    # ── state ──

    def inbox(self, *messages, user=USER, folder="inbox"):
        feed = self.mail.setdefault((user, folder), Feed())
        for m in messages:
            feed.put(m)
        return feed

    def channel(self, *posts, team=TEAM, channel=CH1, name="General", replies=None):
        feed = self.teams.setdefault((team, channel), Feed())
        if not any(c["id"] == channel for c in self.channels.get(team, [])):
            self.channels.setdefault(team, []).append({"id": channel, "displayName": name, "membershipType": "standard"})
        for p in posts:
            feed.put(p)
        for key, items in (replies or {}).items():
            self.replies.setdefault((channel, key), []).extend(copy.deepcopy(items))
        return feed

    def revoke_tokens(self):
        self.valid.clear()

    # ── protocol ──

    def reset(self):
        self.requests.clear()
        self.token_requests.clear()

    def graph(self, tail=None):
        return [r for r in self.requests if r.url.host == GRAPH_HOST and (tail is None or r.url.path.endswith(tail))]

    def deltas(self):
        return [r for r in self.graph() if "/messages/delta" in r.url.path]

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.host == "login.microsoftonline.com":
            return self._token(request)
        for index, (needle, response) in enumerate(self.script):
            if needle in str(request.url):
                self.script.pop(index)
                return response
        token = request.headers.get("authorization", "").removeprefix("Bearer ")
        if self.reject_all or token not in self.valid:
            return httpx.Response(401, json={"error": {"code": "InvalidAuthenticationToken", "message": "Access token validation failure."}})
        path = unquote(request.url.path).removeprefix("/v1.0/")
        q = dict(request.url.params)
        size = int(re.search(r"maxpagesize=(\d+)", request.headers.get("prefer", "")).group(1)) if "maxpagesize" in request.headers.get("prefer", "") else int(q.get("$top", self.page))
        size = min(size, self.page)
        parts = path.split("/")
        base = f"https://{GRAPH_HOST}/v1.0/{request.url.path.removeprefix('/v1.0/')}"
        if parts[0] == "users":
            return self._mail(parts, q, size, base, request)
        if parts[0] == "teams":
            return self._teams(parts, q, size, base)
        return httpx.Response(404, json={"error": {"code": "BadRequest", "message": "Resource not found for the segment"}})

    def _token(self, request):
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        self.token_requests.append(form)
        if self.token_error is not None:
            return self.token_error
        assert request.url.path == f"/{TENANT}/oauth2/v2.0/token" and form["grant_type"] == "client_credentials"
        assert form["scope"] == "https://graph.microsoft.com/.default"
        if form["client_id"] != CLIENT or form["client_secret"] != SECRET:
            return httpx.Response(401, json={"error": "invalid_client", "error_codes": [7000215], "trace_id": "t", "correlation_id": "c",
                                             "error_description": "AADSTS7000215: Invalid client secret provided. Ensure the secret being sent in the request is the client secret value."})
        self.issued += 1
        access = f"eyJ0eXAi.ACCESS-{self.issued}.sig"
        self.valid.add(access)
        return httpx.Response(200, json={"token_type": "Bearer", "expires_in": 3599, "ext_expires_in": 3599, "access_token": access})

    # mail

    def _mail(self, parts, q, size, base, request):
        user = parts[1]
        if parts[2] == "mailFolders" and parts[4:6] == ["messages", "delta"]:
            assert request.headers.get("prefer") == f"odata.maxpagesize={int(request.headers['prefer'].split('=')[1])}", "Prefer is sent on every page"
            feed = self.mail.get((user, parts[3]))
            if feed is None:
                return httpx.Response(404, json={"error": {"code": "ErrorInvalidIdMalformed", "message": "The folder does not exist."}})
            status, body = feed.delta(base, q, size, project)
            return httpx.Response(status, json=body)
        if parts[2] == "messages":
            found = next((f.items[parts[3]] for (u, _), f in self.mail.items() if u == user and parts[3] in f.items), None)
            if found is None:
                return httpx.Response(404, json={"error": {"code": "ErrorItemNotFound", "message": "The specified object was not found in the store."}})
            if len(parts) == 4:
                return httpx.Response(200, json=project(found, (q.get("$select") or "").split(",") if q.get("$select") else None))
            if parts[4] == "attachments":
                items = [project(a, q["$select"].split(",") if q.get("$select") else None) for a in found["_attachments"]]
                offset = int(q.get("$skip", 0))
                body = {"value": items[offset:offset + 2]}
                if offset + 2 < len(items):
                    body["@odata.nextLink"] = f"{base}?$select={q.get('$select', '')}&$skip={offset + 2}"
                return httpx.Response(200, json=body)
        return httpx.Response(404, json={"error": {"code": "BadRequest", "message": "nope"}})

    # teams

    def _teams(self, parts, q, size, base):
        team = parts[1]
        if parts[2:] == ["channels"]:
            items = self.channels.get(team)
            if items is None:
                return httpx.Response(404, json={"error": {"code": "NotFound", "message": "Team not found"}})
            offset = int(q.get("$skiptoken", 0))
            body = {"value": [project(c, q["$select"].split(",") if q.get("$select") else None) for c in items[offset:offset + 2]]}
            if offset + 2 < len(items):
                body["@odata.nextLink"] = f"{base}?$select={q.get('$select', '')}&$skiptoken={offset + 2}"
            return httpx.Response(200, json=body)
        channel = parts[3]
        if len(parts) == 4:
            info = next((c for c in self.channels.get(team, []) if c["id"] == channel), None)
            return httpx.Response(200, json=info) if info else httpx.Response(403, json={"error": {"code": "Forbidden", "message": "no"}})
        feed = self.teams.get((team, channel))
        if feed is None:
            return httpx.Response(404, json={"error": {"code": "NotFound", "message": "Channel not found"}})
        if parts[4:6] == ["messages", "delta"]:
            if "$filter" in q:
                assert re.fullmatch(r"lastModifiedDateTime gt \d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", q["$filter"])
            include = (lambda i: True) if self.replies_in_feed else (lambda i: not i.get("replyToId"))
            status, body = feed.delta(base, q, size, project, include)
            if status == 200 and not self.replies_in_feed:
                body["value"] = [v for v in body["value"] if not (feed.items.get(v["id"]) or {}).get("replyToId")]
            return httpx.Response(status, json=body)
        if parts[4] == "messages" and len(parts) == 6:
            found = feed.items.get(parts[5]) or next((r for (c, _), rs in self.replies.items() if c == channel for r in rs if r["id"] == parts[5]), None)
            return httpx.Response(200, json=project(found, None)) if found else httpx.Response(404, json={"error": {"code": "NotFound", "message": "Message not found"}})
        if parts[4] == "messages" and parts[6] == "replies":
            items = self.replies.get((channel, parts[5]), [])
            offset = int(q.get("$skiptoken", 0))
            body = {"value": items[offset:offset + size]}
            if offset + size < len(items):
                body["@odata.nextLink"] = f"{base}?$top={q.get('$top', '')}&$skiptoken={offset + size}"
            return httpx.Response(200, json=body)
        return httpx.Response(404, json={"error": {"code": "BadRequest", "message": "nope"}})


NOW = datetime(2026, 3, 10, 12, 0, tzinfo=timezone.utc).timestamp()


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("GRAPH_SECRET", SECRET)


def base_config(**cfg):
    return {"tenant_id": TENANT, "client_id": CLIENT, "client_secret_env": "GRAPH_SECRET", "page_size": 2, "default_acl": ["group:all"], **cfg}


def make_mail(site, **cfg):
    sleeps = []
    connector = OutlookMailConnector(base_config(users=[USER], **cfg), transport=httpx.MockTransport(site.handler), sleep=sleeps.append)
    connector.sleeps = sleeps
    return connector


def make_teams(site, **cfg):
    sleeps = []
    cfg = {"team_id": TEAM, "channels": [{"id": CH1, "name": "General"}], **cfg}
    connector = TeamsConnector(base_config(**cfg), transport=httpx.MockTransport(site.handler), sleep=sleeps.append)
    connector.sleeps = sleeps
    return connector


def run(connector, cursor=None):
    batches = list(connector.scan(cursor or {}))
    return [c for b in batches for c in b.changes], batches


def persisted(sf):
    return SourceFile(sf.external_id, sf.path, sf.name, sf.size, None, sf.version, list(sf.acl))


def read(connector, sf):
    with connector.open(persisted(sf)) as fh:
        return fh.read().decode()


def ids(changes):
    return [c.external_id.split(":", 3)[3] for c in changes]


def five_mails(**kw):
    site = Graph(**kw)
    site.inbox(*[msg(n, f"Subject {n}") for n in range(1, 6)])
    return site


# ═════════════════════════════════ Outlook mail ═════════════════════════════════


def test_mail_delta_follows_next_link_until_the_delta_link_and_sends_prefer_and_select_on_every_page():
    site = five_mails()
    changes, batches = run(make_mail(site))
    assert ids(changes) == [mid(n) for n in range(1, 6)]
    deltas = site.deltas()
    assert len(deltas) == 3 and "$skiptoken" not in deltas[0].url.params and "$skiptoken" in deltas[1].url.params and "$skiptoken" in deltas[2].url.params
    assert deltas[0].url.path == f"/v1.0/users/{USER}/mailFolders/inbox/messages/delta"
    assert deltas[0].url.params["$select"] == "subject,receivedDateTime,lastModifiedDateTime,changeKey,isDraft"      # no bodies in a scan
    assert all(r.headers["prefer"] == "odata.maxpagesize=2" for r in deltas)
    assert [len(b.changes) for b in batches] == [2, 2, 1, 0]
    assert batches[-1].done and batches[-1].changes == [] and not any(b.done for b in batches[:-1])
    final = batches[-2].cursor["feeds"][f"{USER}|inbox"]
    assert "$deltatoken" in final and batches[-1].cursor["feeds"] == batches[-2].cursor["feeds"]


def test_a_first_enumeration_is_authoritative_for_its_own_feed_only_on_the_batch_that_reaches_the_delta_link():
    site = five_mails()
    _, batches = run(make_mail(site))
    scopes = [b.complete_scope for b in batches]
    assert scopes == [None, None, f"outlook_mail:{USER}:inbox:", None]


def test_incremental_scan_uses_the_saved_delta_link_and_reports_edits_new_mail_and_removals():
    site = five_mails()
    _, batches = run(make_mail(site))
    cursor = batches[-1].cursor
    json.dumps(cursor)
    site.mail[(USER, "inbox")].edit(mid(2), subject="Subject 2 (edited)", changeKey="CQAAABYAAAB2-v2")
    site.mail[(USER, "inbox")].remove(mid(3))
    site.inbox(msg(6, "Brand new", received="2026-03-09T10:00:00Z"))
    site.reset()
    changes, inc = run(make_mail(site), cursor)
    assert {(c.kind, c.external_id.split(":", 3)[3]) for c in changes} == {("upsert", mid(2)), ("delete", mid(3)), ("upsert", mid(6))}
    first, second = site.deltas()                                                                       # three changes at two per page: two requests
    assert "$deltatoken" in first.url.params and "$select" not in first.url.params and "$skiptoken" in second.url.params   # the links, untouched
    assert all(b.complete_scope is None for b in inc) and inc[-1].done
    edited = next(c for c in changes if c.external_id.endswith(mid(2))).file
    assert edited.version == "CQAAABYAAAB2-v2" and "Subject 2 (edited)" in edited.path
    assert inc[-1].cursor["feeds"][f"{USER}|inbox"] != cursor["feeds"][f"{USER}|inbox"]                 # the new delta link replaces the old


@pytest.mark.parametrize("reason", ["deleted", "changed"])
def test_removed_entries_of_either_reason_are_deletes(reason):
    site = five_mails()
    _, batches = run(make_mail(site))
    site.mail[(USER, "inbox")].remove(mid(4), reason=reason)
    changes, _ = run(make_mail(site), batches[-1].cursor)
    assert [(c.kind, c.external_id) for c in changes] == [("delete", f"outlook_mail:{USER}:inbox:{mid(4)}")]


def test_a_message_listed_twice_in_a_feed_is_settled_by_its_last_entry():
    class Twice(Graph):
        def handler(self, request):
            response = super().handler(request)
            if "/messages/delta" in request.url.path and response.status_code == 200:
                body = response.json()
                if body["value"]:
                    first = body["value"][0]
                    stub = {"id": first["id"], "@removed": {"reason": "deleted"}}
                    body["value"] = ([first, stub, dict(first, subject="resurrected")] if first["id"] == mid(5) else [stub, first]) + body["value"][1:]
                return httpx.Response(200, json=body)
            return response

    site = Twice(page=10)
    site.inbox(msg(5, "five"), msg(4, "four"))
    changes, _ = run(make_mail(site))
    by = {c.external_id.split(":", 3)[3]: c for c in changes}
    assert by[mid(5)].kind == "upsert" and "resurrected" in by[mid(5)].file.path          # upsert, delete, upsert: the last wins
    assert len(changes) == 2 and by[mid(4)].kind == "upsert"


def test_http_410_on_a_delta_link_restarts_the_feed_and_the_new_enumeration_is_authoritative():
    site = five_mails()
    _, batches = run(make_mail(site))
    cursor = batches[-1].cursor
    site.mail[(USER, "inbox")].remove(mid(1))
    site.mail[(USER, "inbox")].expire()
    site.reset()
    changes, resumed = run(make_mail(site), cursor)
    assert [r.url.params.get("$deltatoken") is not None for r in site.deltas()][:2] == [True, False]       # the rejected link, then the first request
    assert sorted(ids(changes)) == sorted(mid(n) for n in (2, 3, 4, 5)) and all(c.kind == "upsert" for c in changes)
    assert [b.complete_scope for b in resumed].count(f"outlook_mail:{USER}:inbox:") == 1                   # message 1 is simply absent => deleted downstream
    assert "$deltatoken" in resumed[-1].cursor["feeds"][f"{USER}|inbox"]


def test_a_410_on_the_very_first_request_is_an_error_not_a_loop():
    site = five_mails()
    site.script = [("messages/delta", httpx.Response(410, json={"error": {"code": "syncStateNotFound", "message": "gone"}}))]
    with pytest.raises(HttpError) as err:
        run(make_mail(site))
    assert err.value.status == 410 and len(site.deltas()) == 1


def test_resume_mid_feed_continues_at_the_saved_next_link_and_is_not_authoritative():
    site = five_mails()
    batches = list(make_mail(site).scan({}))
    first = batches[0]
    assert "$skiptoken" in first.cursor["feeds"][f"{USER}|inbox"]
    site.reset()
    changes, resumed = run(make_mail(site), first.cursor)
    assert ids(changes) == [mid(3), mid(4), mid(5)] and len(site.deltas()) == 2
    assert all(b.complete_scope is None for b in resumed) and resumed[-1].done


def test_resume_between_feeds_repolls_the_finished_one_by_its_delta_link_and_starts_the_next_from_scratch():
    site = Graph()
    site.inbox(msg(1, "a"), msg(2, "b"), msg(3, "c"))
    site.inbox(msg(11, "x"), msg(12, "y"), user=USER, folder="sentitems")
    connector = make_mail(site, folders=["inbox", "sentitems"])
    batches = list(connector.scan({}))
    boundary = next(b for b in batches if f"{USER}|sentitems" in b.cursor["feeds"])
    assert "$deltatoken" in boundary.cursor["feeds"][f"{USER}|inbox"]
    site.reset()
    changes, resumed = run(make_mail(site, folders=["inbox", "sentitems"]), {"feeds": {f"{USER}|inbox": boundary.cursor["feeds"][f"{USER}|inbox"]}})
    assert sorted(ids(changes)) == [mid(11), mid(12)]                           # inbox: nothing new through its delta link
    assert [b.complete_scope for b in resumed if b.complete_scope] == [f"outlook_mail:{USER}:sentitems:"]
    assert "/mailFolders/inbox/messages/delta" in site.deltas()[0].url.path and "$deltatoken" in site.deltas()[0].url.params


def test_several_mailboxes_and_folders_have_their_own_feeds_links_and_scopes():
    site = Graph()
    site.inbox(msg(1, "a"), user="a@acme.com")
    site.inbox(msg(2, "b"), user="b@acme.com", folder="archive")
    site.inbox(msg(3, "c"), user="b@acme.com")
    connector = OutlookMailConnector(base_config(users=["a@acme.com", "b@acme.com"], folders=["inbox", {"id": "archive", "name": "Old mail"}]),
                                     transport=httpx.MockTransport(site.handler))
    # a@/archive does not exist for the fake: it answers 404 like Graph does for an unknown folder
    with pytest.raises(HttpError) as err:
        run(connector)
    assert err.value.status == 404 and "a@acme.com/mailFolders/archive" in str(err.value)
    site.inbox(msg(4, "d"), user="a@acme.com", folder="archive")
    site.reset()
    changes, batches = run(connector)
    assert sorted(c.file.path.split("/")[1] + "/" + c.file.path.split("/")[2] for c in changes) == ["a@acme.com/Old mail", "a@acme.com/inbox", "b@acme.com/Old mail", "b@acme.com/inbox"]
    assert sorted(b.complete_scope for b in batches if b.complete_scope) == sorted(
        f"outlook_mail:{u}:{f}:" for u in ("a@acme.com", "b@acme.com") for f in ("inbox", "archive"))
    assert set(batches[-1].cursor["feeds"]) == {"a@acme.com|inbox", "a@acme.com|archive", "b@acme.com|inbox", "b@acme.com|archive"}


def test_drafts_are_not_indexed_unless_asked_for_and_a_message_that_becomes_a_draft_is_deleted():
    site = Graph()
    site.inbox(msg(1, "sent"), msg(2, "draft", draft=True))
    changes, batches = run(make_mail(site))
    assert [(c.kind, c.external_id.split(":", 3)[3]) for c in changes] == [("upsert", mid(1)), ("delete", mid(2))]
    kept, _ = run(make_mail(site, include_drafts=True))
    assert [c.kind for c in kept] == ["upsert", "upsert"]


def test_since_filters_only_the_first_request_of_a_feed():
    site = five_mails()
    connector = make_mail(site, since="2026-01-01")
    run(connector)
    first = site.deltas()[0].url.params
    assert first["$filter"] == "receivedDateTime ge 2026-01-01T00:00:00Z" and first["$orderby"] == "receivedDateTime desc"
    assert all("$filter" not in r.url.params for r in site.deltas()[1:])                        # the nextLink carries it already
    with pytest.raises(ValueError):
        make_mail(site, since="not a date")


def test_version_is_the_change_key_with_the_last_modified_time_as_fallback():
    site = Graph()
    site.inbox(msg(1, "a", key="CQAAABYAAABK1"), dict(msg(2, "b", modified="2026-03-04T10:00:00Z"), changeKey=None))
    changes, _ = run(make_mail(site))
    assert [c.file.version for c in changes] == ["CQAAABYAAABK1", "2026-03-04T10:00:00Z"]
    assert changes[0].file.size == 0 and changes[0].file.mime == "text/markdown"
    assert changes[0].file.modified_at == datetime(2026, 3, 3, 9, 16, tzinfo=timezone.utc)


def test_paths_are_mailbox_folder_year_and_dated_subject_with_a_stable_id_hash():
    site = Graph()
    site.inbox(msg(1, "Re: Q1/Q2 plan?", received="2025-12-30T08:00:00Z"), msg(2, "", received="2026-03-03T09:15:00Z"))
    changes, _ = run(make_mail(site))
    one, two = changes[0].file, changes[1].file
    assert one.path == f"outlook/{USER}/inbox/2025/2025-12-30 Re Q1 Q2 plan [{short_hash(mid(1), size=8)}].md"
    assert two.path == f"outlook/{USER}/inbox/2026/2026-03-03 (no subject) [{short_hash(mid(2), size=8)}].md" and one.name == one.path.rsplit("/", 1)[-1]
    assert one.external_id == f"outlook_mail:{USER}:inbox:{mid(1)}"


def test_acl_rules_by_mailbox_prefix_and_fail_closed_default():
    site = Graph()
    site.inbox(msg(1, "a"), user="support@acme.com")
    site.inbox(msg(2, "b"), user="ceo@acme.com")
    rules = [{"prefix": "outlook/support@acme.com", "principals": ["group:support"]}]
    connector = OutlookMailConnector(base_config(users=["support@acme.com", "ceo@acme.com"], acl_rules=rules, default_acl=None),
                                     transport=httpx.MockTransport(site.handler))
    by = {c.file.path.split("/")[1]: c.file for c in run(connector)[0]}
    assert by["support@acme.com"].acl == ["group:support"] and by["ceo@acme.com"].acl == []
    assert connector.refresh_acl(persisted(by["support@acme.com"])) == ["group:support"] and connector.refresh_acl(persisted(by["ceo@acme.com"])) == []


# ── open() ──


HTML = ("<html><head><style>p{color:red}</style></head><body><p>Hello <b>team</b>,</p>"
        "<p>Numbers attached. See <a href=\"https://acme.io/q1\">the Q1 sheet</a>.</p><ul><li>Revenue up</li><li>Costs flat</li></ul>"
        "<script>alert(1)</script></body></html>")


def test_open_renders_headers_and_converts_an_html_body_to_text():
    site = Graph()
    site.inbox(msg(1, "Quarterly report", html=HTML, to=(GRACE, LINUS), cc=(MARGARET,), received="2026-03-03T09:15:00Z"))
    connector = make_mail(site)
    sf = run(connector)[0][0].file
    site.reset()
    text = read(connector, sf)
    assert text.startswith(
        "# Quarterly report\n\n"
        "from: Ada Lovelace <ada@acme.com>\nto: Grace Hopper <grace@acme.com>, Linus T <linus@acme.com>\ncc: Margaret H <margaret@acme.com>\n"
        f"date: 2026-03-03T09:15:00Z\nmailbox: {USER}\nfolder: inbox\n\n")
    assert "Hello **team**," in text and "[the Q1 sheet](https://acme.io/q1)" in text and "- Revenue up\n- Costs flat" in text
    assert "alert" not in text and "color:red" not in text and "<" not in text.split("\n\n", 2)[2]
    get = site.graph()[0]
    assert get.url.path == f"/v1.0/users/{USER}/messages/{mid(1)}" and "body" in get.url.params["$select"] and len(site.graph()) == 1       # no attachments call


def test_open_keeps_a_plain_text_body_and_lists_attachments_page_by_page_skipping_inline_images():
    atts = [attachment(1, "report.pdf"), attachment(2, "logo.png", inline=True, kind="image/png"), attachment(3, "data.xlsx", kind="application/vnd.ms-excel", size=99)]
    site = Graph()
    site.inbox(msg(1, "With files", text="Line one\r\nLine two", attachments=atts))
    connector = make_mail(site)
    sf = run(connector)[0][0].file
    site.reset()
    text = read(connector, sf)
    assert "Line one\nLine two" in text
    assert "attachments: report.pdf (application/pdf, 1234 bytes); data.xlsx (application/vnd.ms-excel, 99 bytes)" in text and "logo.png" not in text
    calls = site.graph("/attachments")
    assert len(calls) == 2 and "$skip" in calls[1].url.params and calls[0].url.params["$select"] == "name,contentType,size,isInline"   # names only, never contentBytes
    assert "SEVBVlk" not in text


def test_open_of_a_message_that_is_gone_reports_404_and_bad_ids_are_rejected_without_a_request():
    site = five_mails()
    connector = make_mail(site)
    with pytest.raises(HttpError) as err:
        connector.open(SourceFile(f"outlook_mail:{USER}:inbox:{mid(99)}", "p", "n", 0, None, "v", []))
    assert err.value.status == 404 and "ErrorItemNotFound" in str(err.value)
    site.reset()
    for bad in ("outlook_mail:x", f"outlook_mail:{USER}:inbox:", f"outlook_mail:{USER}:inbox:a/b", f"outlook_mail::inbox:{mid(1)}",
                f"teams:{USER}:inbox:{mid(1)}", f"outlook_mail:{USER}:inbox:{mid(1)}%2F..", "outlook_mail:" + USER + ":inbox:" + "A" * 700):
        with pytest.raises(HttpError):
            connector.open(SourceFile(bad, "p", "n", 0, None, "v", []))
    assert site.requests == []


def test_a_message_with_an_unexpected_id_is_skipped_rather_than_indexed_unopenable():
    site = Graph()
    site.inbox(msg(1, "fine"), dict(msg(2, "odd"), id="has space/slash"))
    changes, _ = run(make_mail(site))
    assert ids(changes) == [mid(1)]


def test_a_message_moved_between_folders_is_a_delete_in_one_feed_and_a_new_object_in_the_other():
    site = Graph()
    site.inbox(msg(1, "moving"), folder="inbox")
    site.inbox(user=USER, folder="archive")
    connector = make_mail(site, folders=["inbox", "archive"])
    _, batches = run(connector)
    site.mail[(USER, "inbox")].remove(mid(1), reason="changed")
    site.inbox(msg(1, "moving"), folder="archive")
    changes, _ = run(make_mail(site, folders=["inbox", "archive"]), batches[-1].cursor)
    assert {(c.kind, c.external_id) for c in changes} == {("delete", f"outlook_mail:{USER}:inbox:{mid(1)}"), ("upsert", f"outlook_mail:{USER}:archive:{mid(1)}")}


# ═════════════════════════════════ Teams ═════════════════════════════════


def threads():
    site = Graph(page=2)
    site.channel(*[post(n, f"Post {n} body", subject=f"Topic {n}", created=f"2026-03-0{n}T09:00:00Z") for n in range(1, 6)],
                 replies={pid(1): [post(11, "first reply", reply_to=pid(1), created="2026-03-01T09:05:00Z", user="Grace Hopper")]})
    return site


def tids(changes):
    return [c.external_id.split(":", 3)[3] for c in changes]


def test_channel_delta_is_followed_through_next_links_with_top_and_ends_at_the_delta_link():
    site = threads()
    changes, batches = run(make_teams(site))
    assert tids(changes) == [pid(n) for n in range(1, 6)]
    deltas = site.deltas()
    assert len(deltas) == 3 and deltas[0].url.params["$top"] == "2" and "$skiptoken" in deltas[1].url.params
    assert deltas[0].url.path == f"/v1.0/teams/{TEAM}/channels/{CH1}/messages/delta"             # ':' and '@' stay readable in the path
    assert [len(b.changes) for b in batches] == [2, 2, 1, 0] and batches[-1].done
    assert [b.complete_scope for b in batches] == [None, None, f"teams:{TEAM}:{CH1.replace(':', '%3A').replace('@', '%40')}:", None]
    sf = changes[0].file
    assert sf.external_id == f"teams:{TEAM}:19%3A2f3e4d5c6b7a8f9e0a1b2c3d4e5f6a7b%40thread.tacv2:{pid(1)}"
    assert sf.path == f"teams/{TEAM}/General/2026/2026-03-01 Topic 1 [{pid(1)}].md" and sf.mime == "text/markdown"


def test_a_thread_file_holds_the_post_and_its_replies_in_order_and_size_and_version_describe_it():
    replies = [post(13, "<p>third <at id=\"0\">Ada Lovelace</at></p>", html=True, reply_to=pid(1), created="2026-03-01T09:30:00Z", user="Linus T"),
               post(12, "second\nwith two lines", reply_to=pid(1), created="2026-03-01T09:10:00Z", user="Grace Hopper"),
               post(14, "gone", reply_to=pid(1), created="2026-03-01T09:40:00Z", deleted="2026-03-01T09:45:00Z"),
               post(15, "Linus joined", reply_to=pid(1), created="2026-03-01T09:41:00Z", kind="systemEventMessage")]
    site = Graph()
    site.channel(post(1, "<div>Please review the <b>release plan</b>.<attachment id=\"att1\"></attachment></div>", subject="Release plan", html=True,
                      attachments=["plan.docx"]), replies={pid(1): replies})
    connector = make_teams(site, teams=[{"id": TEAM, "name": "Engineering", "channels": [{"id": CH1, "name": "General"}]}], team_id=None)
    sf = run(connector)[0][0].file
    text = read(make_teams(site, teams=[{"id": TEAM, "name": "Engineering", "channels": [{"id": CH1, "name": "General"}]}], team_id=None), sf)
    assert text.startswith("# Release plan\n\nteam: Engineering\nchannel: General\nfrom: Ada Lovelace\ndate: 2026-03-03T09:15:00Z\nattachments: plan.docx\n\n")
    assert "Please review the **release plan**." in text and "<attachment" not in text
    assert text.index("second\n    with two lines") > text.index("## Replies")
    assert "[2026-03-01 09:10] Grace Hopper: second\n    with two lines" in text and "[2026-03-01 09:30] Linus T: third @Ada Lovelace" in text
    assert text.index("Grace Hopper") < text.index("Linus T") and "gone" not in text and "Linus joined" not in text
    assert sf.size == len(text.encode()) and sf.version == short_hash(text, size=16)


def test_the_version_follows_the_thread_content_a_new_edited_or_removed_reply_changes_it():
    def version(site):
        return run(make_teams(site))[0][0].file.version

    base = threads()
    v0 = version(base)
    assert version(threads()) == v0
    added = threads()
    added.replies[(CH1, pid(1))].append(post(12, "another", reply_to=pid(1), created="2026-03-01T10:00:00Z"))
    edited = threads()
    edited.replies[(CH1, pid(1))][0]["body"]["content"] = "first reply (edited)"
    removed = threads()
    removed.replies[(CH1, pid(1))][0]["deletedDateTime"] = "2026-03-02T00:00:00Z"
    assert len({v0, version(added), version(edited), version(removed)}) == 4


def test_replies_are_paged_by_next_link_and_include_replies_false_makes_no_reply_requests():
    site = Graph(page=2)
    site.channel(post(1, "root", subject="T"), replies={pid(1): [post(20 + n, f"reply {n}", reply_to=pid(1), created=f"2026-03-01T09:0{n}:00Z") for n in range(5)]})
    connector = make_teams(site)
    sf = run(connector)[0][0].file
    replies = [r for r in site.graph() if r.url.path.endswith("/replies")]
    assert len(replies) == 3 and "$skiptoken" in replies[1].url.params and all(r.url.params["$top"] == "2" for r in replies)
    assert all(f"reply {n}" in read(make_teams(site), sf) for n in range(5))
    bare = Graph(page=2)
    bare.channel(post(1, "root", subject="T"), replies={pid(1): [post(21, "hidden reply", reply_to=pid(1))]})
    solo = make_teams(bare, include_replies=False)
    sf = run(solo)[0][0].file
    assert not [r for r in bare.graph() if r.url.path.endswith("/replies")] and "hidden reply" not in read(solo, sf)
    assert not [r for r in bare.graph() if r.url.path.endswith("/replies")]


def test_titles_come_from_the_subject_then_the_first_line_and_authors_from_user_or_application():
    bot = post(2, "Build finished\nall green", user=None)
    bot["from"] = {"application": {"displayName": "CI Bot", "id": "app1"}, "device": None, "user": None}
    site = Graph()
    site.channel(post(1, "No subject here, just a long first line\nsecond line"), bot, post(3, "", subject=None))
    connector = make_teams(site)
    changes, _ = run(connector)
    by = {tids([c])[0]: c.file for c in changes}
    assert "No subject here, just a long first line [" in by[pid(1)].path and by[pid(3)].path.count("Teams message") == 1
    text = read(connector, by[pid(2)])
    assert text.startswith("# Build finished\n") and "from: CI Bot" in text


def test_system_messages_and_soft_deleted_posts_are_not_indexed_and_deleted_posts_become_deletes():
    site = Graph()
    site.channel(post(1, "keep"), post(2, "joined", kind="systemEventMessage"), post(3, "was removed", deleted="2026-03-04T00:00:00Z"))
    changes, _ = run(make_teams(site))
    assert [(c.kind, tids([c])[0]) for c in changes] == [("upsert", pid(1)), ("delete", pid(3))]


def test_incremental_teams_scan_reports_edits_new_posts_and_removed_posts_through_the_delta_link():
    site = threads()
    _, batches = run(make_teams(site))
    cursor = batches[-1].cursor
    feed = site.teams[(TEAM, CH1)]
    feed.edit(pid(2), body={"contentType": "text", "content": "Post 2 edited"})
    feed.remove(pid(4))
    site.channel(post(6, "fresh", subject="Topic 6", created="2026-03-09T09:00:00Z"))
    site.reset()
    changes, inc = run(make_teams(site), cursor)
    assert {(c.kind, tids([c])[0]) for c in changes} == {("upsert", pid(2)), ("delete", pid(4)), ("upsert", pid(6))}
    first, second = site.deltas()
    assert "$deltatoken" in first.url.params and "$skiptoken" in second.url.params and all(b.complete_scope is None for b in inc)


def test_http_410_restarts_a_channel_feed_and_deletes_what_the_fresh_enumeration_no_longer_lists():
    site = threads()
    _, batches = run(make_teams(site))
    feed = site.teams[(TEAM, CH1)]
    feed.remove(pid(5))
    feed.expire()
    site.reset()
    changes, resumed = run(make_teams(site), batches[-1].cursor)
    assert sorted(tids(changes)) == sorted(pid(n) for n in (1, 2, 3, 4))
    assert [b.complete_scope for b in resumed if b.complete_scope] == [f"teams:{TEAM}:{CH1.replace(':', '%3A').replace('@', '%40')}:"]
    assert [("$deltatoken" in r.url.params) for r in site.deltas()][:2] == [True, False]


def test_resume_mid_channel_continues_at_the_saved_link_without_authority():
    site = threads()
    first = list(make_teams(site).scan({}))[0]
    site.reset()
    changes, resumed = run(make_teams(site), first.cursor)
    assert tids(changes) == [pid(3), pid(4), pid(5)] and all(b.complete_scope is None for b in resumed)


def test_a_reply_that_shows_up_in_the_feed_refreshes_its_post_once_even_if_the_post_itself_did_not_change():
    site = Graph(page=10, replies_in_feed=True)                              # UNSURE whether Graph's channel delta includes replies
    site.channel(post(1, "root", subject="T1"), post(2, "other", subject="T2"),
                 replies={pid(1): [post(11, "reply a", reply_to=pid(1)), post(12, "reply b", reply_to=pid(1), created="2026-03-03T09:20:00Z")]})
    feed = site.teams[(TEAM, CH1)]
    changes, batches = run(make_teams(site))
    base_versions = {tids([c])[0]: c.file.version for c in changes}
    assert set(base_versions) == {pid(1), pid(2)}                              # replies are not files of their own
    site.replies[(CH1, pid(1))].append(post(13, "reply c", reply_to=pid(1), created="2026-03-03T10:00:00Z"))
    feed.put(site.replies[(CH1, pid(1))][-1])                                  # the feed announces only the reply
    site.teams[(TEAM, CH1)].items[pid(13)]["replyToId"] = pid(1)
    site.reset()
    inc, _ = run(make_teams(site), batches[-1].cursor)
    assert [(c.kind, tids([c])[0]) for c in inc] == [("upsert", pid(1))] and inc[0].file.version != base_versions[pid(1)]
    root_gets = [r for r in site.graph() if r.url.path.endswith(f"/messages/{pid(1)}")]
    assert len(root_gets) == 1


def test_several_replies_to_one_post_and_a_post_listed_with_its_reply_cost_one_refresh_or_none():
    site = Graph(page=10, replies_in_feed=True)
    site.channel(post(1, "root one", subject="T1"), post(2, "root two", subject="T2"),
                 replies={pid(1): [post(11, "a", reply_to=pid(1)), post(12, "b", reply_to=pid(1), created="2026-03-03T09:20:00Z")],
                          pid(2): [post(21, "c", reply_to=pid(2))]})
    feed = site.teams[(TEAM, CH1)]
    _, batches = run(make_teams(site))
    for reply in site.replies[(CH1, pid(1))]:
        feed.put(reply)                                                       # two replies to post 1 ...
    feed.edit(pid(2), body={"contentType": "text", "content": "root two edited"})
    feed.put(site.replies[(CH1, pid(2))][0])                                  # ... and post 2 itself plus a reply to it
    site.reset()
    inc, _ = run(make_teams(site), batches[-1].cursor)
    assert sorted(tids(inc)) == [pid(1), pid(2)] and {c.kind for c in inc} == {"upsert"}
    gets = [r.url.path.rsplit("/", 1)[-1] for r in site.graph() if re.search(r"/messages/\d+$", r.url.path)]
    assert gets == [pid(1)]                                                   # post 1 fetched once; post 2 came with the feed and is not fetched


def test_a_reply_whose_post_has_vanished_deletes_the_post():
    site = Graph(page=10, replies_in_feed=True)
    site.channel(post(1, "root", subject="T1"))
    _, batches = run(make_teams(site))
    feed = site.teams[(TEAM, CH1)]
    feed.items.pop(pid(1))                                                    # the post is gone but a reply to it was announced
    feed.put(post(11, "orphan reply", reply_to=pid(1)))
    site.reset()
    inc, _ = run(make_teams(site), batches[-1].cursor)
    assert [(c.kind, tids([c])[0]) for c in inc] == [("delete", pid(1))]


def test_channels_are_listed_with_paging_when_not_configured_and_excluded_by_name_or_id():
    site = Graph(page=2)
    for n, name in enumerate(["General", "Random", "Secret", "Dev"]):
        channel = f"19:{n:032x}@thread.tacv2"
        site.channel(post(n + 1, f"in {name}", subject=name), channel=channel, name=name)
    connector = make_teams(site, channels=None, exclude_channels=["SECRET"])
    changes, batches = run(connector)
    listing = [r for r in site.graph() if r.url.path == f"/v1.0/teams/{TEAM}/channels"]
    assert len(listing) == 2 and "$skiptoken" in listing[1].url.params                      # four channels, two per page
    assert sorted(c.file.path.split("/")[2] for c in changes) == ["Dev", "General", "Random"]
    assert len([b for b in batches if b.complete_scope]) == 3
    only_ids = make_teams(site, channels=None, teams=[{"id": TEAM, "exclude_channels": [f"19:{0:032x}@thread.tacv2", "random"]}], team_id=None)
    assert sorted(c.file.path.split("/")[2] for c in run(only_ids)[0]) == ["Dev", "Secret"]


def test_a_configured_channel_without_a_name_is_named_by_its_display_name_once_and_open_agrees():
    site = Graph(page=10)
    site.channel(post(1, "hello", subject="Hi"), name="Platform Team")
    connector = make_teams(site, channels=[CH1])
    sf = run(connector)[0][0].file
    assert sf.path.split("/")[2] == "Platform Team"
    assert len([r for r in site.graph() if r.url.path == f"/v1.0/teams/{TEAM}/channels/{CH1}"]) == 1
    fresh = make_teams(site, channels=[CH1])
    assert "channel: Platform Team" in read(fresh, sf)                        # a connector that never scanned resolves the same name
    site.channels[TEAM] = []                                                  # no longer readable: both fall back to the id
    assert "channel: " + CH1 in read(make_teams(site, channels=[CH1]), sf)


def test_a_configured_channel_name_labels_the_path_without_asking_graph_and_a_failing_lookup_is_not_hidden():
    site = Graph(page=10)
    site.channel(post(1, "hello", subject="Hi"), name="General")
    configured = make_teams(site, channels=[{"id": CH1, "name": "Renamed"}])
    sf = run(configured)[0][0].file
    assert sf.path.split("/")[2] == "Renamed" and "channel: Renamed" in read(configured, sf)
    assert not [r for r in site.graph() if r.url.path == f"/v1.0/teams/{TEAM}/channels/{CH1}"]
    site.script = [(f"/channels/{CH1}?", httpx.Response(500, json={"error": {"code": "InternalServerError", "message": "boom"}}))] * 5
    with pytest.raises(HttpError) as err:
        run(make_teams(site, channels=[CH1]))
    assert err.value.status == 500


def test_page_size_is_capped_at_fifty_for_both_connectors():
    site = five_mails()
    run(make_mail(site, page_size=500))
    assert {r.headers["prefer"] for r in site.deltas()} == {"odata.maxpagesize=50"}
    teams = threads()
    run(make_teams(teams, page_size=500))
    assert teams.deltas()[0].url.params["$top"] == "50"


def test_open_round_trips_the_percent_encoded_channel_and_rejects_bad_ids_without_requests():
    site = threads()
    scanned = run(make_teams(site))[0]
    fresh = make_teams(threads())
    for change in scanned:
        text = read(fresh, change.file)
        assert len(text.encode()) == change.file.size and text.startswith("# Topic ")
    site.reset()
    connector = make_teams(site)
    for bad in ("teams:x", f"teams:{TEAM}::{pid(1)}", f"teams:{TEAM}:{CH1}:../x", f"teams:short:{CH1}:{pid(1)}", f"outlook_mail:{TEAM}:{CH1}:{pid(1)}", f"teams:{TEAM}:{CH1}:"):
        with pytest.raises(HttpError):
            connector.open(SourceFile(bad, "p", "n", 0, None, "v", []))
    assert site.requests == []
    with pytest.raises(HttpError) as err:
        connector.open(SourceFile(f"teams:{TEAM}:{CH1.replace(':', '%3A')}:{pid(99)}", "p", "n", 0, None, "v", []))
    assert err.value.status == 404


def test_teams_since_filters_the_first_request_only_and_acl_rules_work_by_team_and_channel():
    site = threads()
    run(make_teams(site, since="2026-02-01"))
    assert site.deltas()[0].url.params["$filter"] == "lastModifiedDateTime gt 2026-02-01T00:00:00Z" and all("$filter" not in r.url.params for r in site.deltas()[1:])
    rules = [{"prefix": f"teams/{TEAM}", "principals": ["group:eng"]}, {"prefix": f"teams/{TEAM}/Secret", "principals": ["group:leads"]}]
    two = Graph()
    two.channel(post(1, "a"), channel=CH1, name="General")
    two.channel(post(2, "b"), channel=CH2, name="Secret")
    connector = make_teams(two, channels=None, acl_rules=rules, default_acl=None)
    by = {c.file.path.split("/")[2]: c.file for c in run(connector)[0]}
    assert by["General"].acl == ["group:eng"] and by["Secret"].acl == ["group:leads"]
    assert connector.refresh_acl(persisted(by["Secret"])) == ["group:leads"]
    closed = make_teams(two, channels=None, acl_rules=[{"prefix": f"teams/{TEAM}/General", "principals": ["group:eng"]}], default_acl=None)
    assert {c.file.path.split("/")[2]: c.file.acl for c in run(closed)[0]} == {"General": ["group:eng"], "Secret": []}


# ═════════════════════════════════ shared: auth, errors, rate limits ═════════════════════════════════


def test_the_client_credentials_token_is_requested_once_with_the_documented_form_and_reused():
    site = five_mails()
    run(make_mail(site))
    assert len(site.token_requests) == 1
    assert site.token_requests[0] == {"client_id": CLIENT, "client_secret": SECRET, "scope": "https://graph.microsoft.com/.default", "grant_type": "client_credentials"}
    assert all(r.headers["authorization"] == "Bearer eyJ0eXAi.ACCESS-1.sig" for r in site.graph())
    assert all(SECRET not in str(r.url) for r in site.requests)


def test_an_expired_access_token_is_replaced_once_and_the_request_repeated():
    site = five_mails()
    connector = make_mail(site)
    batches = connector.scan({})
    next(batches)
    site.revoke_tokens()                                                       # Graph now rejects the cached token
    rest = list(batches)
    assert rest[-1].done and len(site.token_requests) == 2
    assert any(r.headers["authorization"].endswith("ACCESS-2.sig") for r in site.graph())


def test_a_token_graph_keeps_rejecting_is_a_clear_auth_error_that_names_the_variable_and_leaks_nothing():
    site = five_mails()
    site.reject_all = True
    connector = make_mail(site)
    with pytest.raises(AuthError) as err:
        run(connector)
    message = str(err.value)
    assert err.value.status == 401 and "GRAPH_SECRET" in message and "InvalidAuthenticationToken" in message
    assert SECRET not in message and "ACCESS-" not in message
    assert len(site.token_requests) == 2 and len(site.graph()) == 2             # one refresh, then it gives up


def test_entra_rejecting_the_client_secret_is_an_auth_error_with_the_aadsts_code_and_without_the_secret(monkeypatch):
    monkeypatch.setenv("GRAPH_SECRET", "wrong-secret-value-123")
    site = five_mails()
    with pytest.raises(AuthError) as err:
        run(make_mail(site))
    message = str(err.value)
    assert "AADSTS7000215" in message and "invalid_client" in message and "GRAPH_SECRET" in message and "tenant_id" in message
    assert "wrong-secret-value-123" not in message and SECRET not in message and site.graph() == []


def test_a_server_that_echoes_the_secret_or_token_does_not_get_it_into_the_error():
    site = five_mails()
    site.script = [("messages/delta", httpx.Response(400, json={"error": {"code": "BadRequest", "message": f"bad request with {SECRET}"}}))]
    with pytest.raises(HttpError) as err:
        run(make_mail(site))
    assert SECRET not in str(err.value) and "***" in str(err.value) and err.value.status == 400


def test_a_missing_secret_variable_is_named_and_no_graph_request_is_made(monkeypatch):
    monkeypatch.delenv("GRAPH_SECRET")
    site = five_mails()
    with pytest.raises(HttpError, match="GRAPH_SECRET"):
        run(make_mail(site))
    assert site.graph() == [] and site.token_requests == []


def test_a_token_provider_replaces_the_client_credentials_flow():
    site = five_mails()
    site.valid.add("STATIC-ADC-TOKEN")
    connector = OutlookMailConnector({"users": [USER], "default_acl": ["group:all"]}, token_provider=lambda: "STATIC-ADC-TOKEN",
                                     transport=httpx.MockTransport(site.handler))
    changes, _ = run(connector)
    assert len(changes) == 5 and site.token_requests == [] and all(r.headers["authorization"] == "Bearer STATIC-ADC-TOKEN" for r in site.graph())


def test_403_is_an_auth_error_that_names_the_permission_and_is_not_retried():
    mail = five_mails()
    mail.script = [("messages/delta", httpx.Response(403, json={"error": {"code": "ErrorAccessDenied", "message": "Access is denied."}}))]
    connector = make_mail(mail)
    with pytest.raises(AuthError, match="Mail.Read") as err:
        run(connector)
    assert err.value.status == 403 and connector.sleeps == [] and len(mail.deltas()) == 1
    teams = threads()
    teams.script = [("messages/delta", httpx.Response(403, json={"error": {"code": "Forbidden", "message": "no"}}))]
    with pytest.raises(AuthError, match="ChannelMessage.Read.All"):
        run(make_teams(teams))


def test_429_and_503_are_waited_out_with_retry_after_and_exhausted_throttling_is_a_429_error():
    site = five_mails()
    site.script = [("messages/delta", httpx.Response(429, headers={"Retry-After": "7"}, json={"error": {"code": "TooManyRequests", "message": "slow down"}})),
                   ("messages/delta", httpx.Response(503, headers={"Retry-After": "3"}, json={"error": {"code": "ServiceNotAvailable", "message": "later"}}))]
    connector = make_mail(site)
    changes, _ = run(connector)
    assert len(changes) == 5 and connector.sleeps == [7.0, 3.0]
    site = five_mails()
    site.script = [("messages/delta", httpx.Response(429, headers={"Retry-After": "1"}, json={"error": {"code": "TooManyRequests", "message": "x"}}))] * 10
    with pytest.raises(HttpError) as err:
        run(make_mail(site))
    assert err.value.status == 429 and "throttled" in str(err.value)


def test_a_delta_response_with_neither_link_and_a_link_that_repeats_are_errors_not_loops():
    site = five_mails()
    site.script = [("messages/delta", httpx.Response(200, json={"value": []}))]
    with pytest.raises(HttpError, match="neither"):
        run(make_mail(site))

    same = f"https://{GRAPH_HOST}/v1.0/users/{USER}/mailFolders/inbox/messages/delta?$skiptoken=same"

    class Stuck(Graph):
        def handler(self, request):
            if request.url.params.get("$skiptoken") == "same":
                self.requests.append(request)
                return httpx.Response(200, json={"value": [], "@odata.nextLink": same})        # the server hands out the link it was just given
            response = super().handler(request)
            if "/messages/delta" in request.url.path and response.status_code == 200:
                body = response.json()
                body.pop("@odata.deltaLink", None)
                body["@odata.nextLink"] = same
                return httpx.Response(200, json=body)
            return response

    stuck = Stuck()
    stuck.inbox(msg(1, "a"))
    with pytest.raises(HttpError, match="did not advance"):
        run(make_mail(stuck))


def test_cursors_are_json_and_hold_no_secret_or_access_token():
    site = five_mails()
    _, batches = run(make_mail(site))
    blob = json.dumps([b.cursor for b in batches])
    assert SECRET not in blob and "ACCESS-" not in blob and "eyJ0eXAi" not in blob
    teams = threads()
    _, batches = run(make_teams(teams))
    assert SECRET not in json.dumps([b.cursor for b in batches])


def test_configuration_errors_are_raised_early():
    for cls, extra in ((OutlookMailConnector, {"users": [USER]}), (TeamsConnector, {"team_id": TEAM})):
        for missing in ("tenant_id", "client_id", "client_secret_env"):
            cfg = base_config(**extra)
            cfg.pop(missing)
            with pytest.raises(ValueError, match=missing):
                cls(cfg)
    with pytest.raises(ValueError, match="users"):
        OutlookMailConnector(base_config())
    for bad in ("a/b", "a b", "a:b", "a?b"):
        with pytest.raises(ValueError):
            OutlookMailConnector(base_config(users=[bad]))
    with pytest.raises(ValueError):
        OutlookMailConnector(base_config(users=[USER], folders=["in/box"]))
    with pytest.raises(ValueError, match="team_id"):
        TeamsConnector(base_config())
    with pytest.raises(ValueError):
        TeamsConnector(base_config(team_id="not a guid"))
    assert OutlookMailConnector.kind == "outlook_mail" and TeamsConnector.kind == "teams"
