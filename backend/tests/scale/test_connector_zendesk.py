"""Zendesk connector against a fake API shaped like the Zendesk documentation: Help Center articles / sections /
categories with `next_page` offset pagination, the time-based Help Center incremental feed (which, like Zendesk's, keeps
answering with the same `next_page` once it has caught up), the cursor-based ticket export (`after_cursor`,
`end_of_stream`), ticket comments with the `users` side-load.

Not certain of (written from memory of the docs): the exact response shape of the Help Center incremental feed, whether
the cursor-based ticket export takes `per_page`, and the wording of Zendesk's error bodies.
"""

import base64
import copy
import json
import re
from datetime import datetime, timezone
from urllib.parse import urlencode

import httpx
import pytest

from app.scale.connectors._http import AuthError, HttpError
from app.scale.connectors.base import SourceFile
from app.scale.connectors.zendesk import ZendeskConnector

TOKEN = "zdtoken-SECRET0123456789"
EMAIL = "agent@acme.example"
NOW = datetime(2026, 3, 10, 12, 0, tzinfo=timezone.utc).timestamp()
HOST = "acme.zendesk.com"
BASE = f"https://{HOST}"
BASIC = "Basic " + base64.b64encode(f"{EMAIL}/token:{TOKEN}".encode()).decode()


def epoch(iso: str) -> int:
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())


def article(aid, title, *, body="<p>Reset it</p>", section_id=10, updated="2026-03-01T10:00:00Z", locale="en-us", draft=False, user_segment_id=None, **kw):
    return {"id": aid, "url": f"{BASE}/api/v2/help_center/{locale}/articles/{aid}.json", "html_url": f"{BASE}/hc/{locale}/articles/{aid}",
            "author_id": 5, "comments_disabled": False, "draft": draft, "promoted": False, "position": 0, "vote_sum": 0, "vote_count": 0,
            "section_id": section_id, "created_at": "2026-02-01T09:00:00Z", "updated_at": updated, "edited_at": updated, "name": title,
            "title": title, "source_locale": locale, "locale": locale, "outdated": False, "outdated_locales": [], "label_names": ["billing"],
            "body": body, "user_segment_id": user_segment_id, "permission_group_id": 1, **kw}


SECTIONS = [{"id": 10, "category_id": 1, "name": "Payments", "locale": "en-us"}, {"id": 11, "category_id": 1, "name": "Refunds", "locale": "en-us"},
            {"id": 20, "category_id": 2, "name": "Getting started", "locale": "en-us"}]
CATEGORIES = [{"id": 1, "name": "Billing", "locale": "en-us"}, {"id": 2, "name": "Onboarding", "locale": "en-us"}]


def user(uid, name, role="end-user"):
    return {"id": uid, "name": name, "role": role, "email": f"{name.split()[0].lower()}@example.com"}


def ticket(tid, subject, *, status="open", group_id=77, updated="2026-03-01T10:00:00Z", requester_id=900, **kw):
    return {"id": tid, "url": f"{BASE}/api/v2/tickets/{tid}.json", "subject": subject, "raw_subject": subject, "description": "first message",
            "status": status, "priority": "normal", "type": "incident", "requester_id": requester_id, "submitter_id": requester_id,
            "assignee_id": 5, "organization_id": 33, "group_id": group_id, "tags": ["vip", "refund"], "created_at": "2026-02-01T09:00:00Z",
            "updated_at": updated, "generated_timestamp": epoch(updated), "is_public": True, **kw}


def zcomment(cid, author_id, body, *, public=True, created="2026-03-02T08:00:00Z", **kw):
    return {"id": cid, "type": "Comment", "author_id": author_id, "body": body, "html_body": f"<div>{body}</div>", "plain_body": body,
            "public": public, "created_at": created, "attachments": [], "via": {"channel": "email"}, **kw}


class Zendesk:
    sessions = 0

    def __init__(self, *, articles=(), tickets=(), comments=None, users=(), cap=2, header=BASIC, expire_cursors=False):
        self.articles, self.tickets = copy.deepcopy(list(articles)), copy.deepcopy(list(tickets))
        self.sections, self.categories = copy.deepcopy(SECTIONS), copy.deepcopy(CATEGORIES)
        self.comments, self.users = copy.deepcopy(comments or {}), copy.deepcopy(list(users))
        self.cap, self.header, self.expire_cursors, self.window, self.issued = cap, header, expire_cursors, 0, set()
        Zendesk.sessions += 1
        self.nonce = f"s{Zendesk.sessions}-"                      # cursors are only good for the server session that issued them
        self.requests: list[httpx.Request] = []
        self.script: list[httpx.Response] = []

    def reset(self):
        self.requests.clear()

    def paths(self):
        return [r.url.path for r in self.requests]

    def calls(self, pattern):
        return [r for r in self.requests if re.search(pattern, r.url.path)]

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.script:
            return self.script.pop(0)
        if request.headers.get("authorization") != self.header:
            return httpx.Response(401, json={"error": "Couldn't authenticate you"})
        path, q = request.url.path, dict(request.url.params)
        m = re.fullmatch(r"/api/v2/help_center/incremental/articles", path)
        if m:
            return self.hc_incremental(q)
        m = re.fullmatch(r"/api/v2/help_center/([A-Za-z0-9_-]+)/(articles|sections|categories)", path)
        if m:
            locale, kind = m.groups()
            items = [a for a in getattr(self, kind) if a.get("locale", "en-us") == locale]
            return self.offset_page(items, kind, q, path)
        m = re.fullmatch(r"/api/v2/help_center/([A-Za-z0-9_-]+)/articles/(\d+)", path)
        if m:
            found = [a for a in self.articles if a["id"] == int(m.group(2)) and a["locale"] == m.group(1)]
            return httpx.Response(200, json={"article": found[0]}) if found else httpx.Response(404, json={"error": "RecordNotFound", "description": "Not found"})
        if path == "/api/v2/incremental/tickets/cursor.json":
            return self.ticket_export(q)
        m = re.fullmatch(r"/api/v2/tickets/(\d+)/comments\.json", path)
        if m:
            tid = int(m.group(1))
            assert q.get("include") == "users" and q.get("sort_order") == "asc"
            r = self.offset_page(self.comments.get(tid, []), "comments", q, path)
            body = r.json()
            body["users"] = self.users
            return httpx.Response(200, json=body)
        m = re.fullmatch(r"/api/v2/tickets/(\d+)\.json", path)
        if m:
            found = [t for t in self.tickets if t["id"] == int(m.group(1))]
            return httpx.Response(200, json={"ticket": found[0]}) if found else httpx.Response(404, json={"error": "RecordNotFound"})
        return httpx.Response(404, json={"error": "NotFound"})

    def offset_page(self, items, key, q, path):
        page, per = int(q.get("page", 1)), min(int(q.get("per_page", 30)), self.cap)
        part = items[(page - 1) * per: page * per]
        more = page * per < len(items)
        nq = {**{k: v for k, v in q.items() if k != "page"}, "page": page + 1}
        return httpx.Response(200, json={key: part, "next_page": f"{BASE}{path}?{urlencode(nq)}" if more else None,
                                         "previous_page": None, "page": page, "per_page": per, "page_count": -(-len(items) // per), "count": len(items)})

    def hc_incremental(self, q):
        start = int(q["start_time"])
        items = sorted((a for a in self.articles if epoch(a["updated_at"]) >= start), key=lambda a: a["updated_at"])[: self.cap]
        end = epoch(items[-1]["updated_at"]) if items else start
        return httpx.Response(200, json={"articles": items, "count": len(items), "end_time": end,
                                         "next_page": f"{BASE}/api/v2/help_center/incremental/articles?start_time={end}"})

    def ticket_export(self, q):
        if "cursor" in q:
            if self.expire_cursors and q["cursor"] not in self.issued:                 # a cursor from an earlier session
                return httpx.Response(400, json={"error": "InvalidCursor", "description": "The cursor has expired"})
            offset = int(q["cursor"].rsplit("c", 1)[1])
        else:
            assert "start_time" in q
            offset, self.window = 0, int(q["start_time"])
        ordered = [t for t in sorted(self.tickets, key=lambda t: t["generated_timestamp"]) if t["generated_timestamp"] >= self.window]
        size = min(int(q.get("per_page", 1000)), self.cap)
        after = f"{self.nonce}c{offset + size}"
        self.issued.add(after)
        return httpx.Response(200, json={"tickets": ordered[offset: offset + size], "after_cursor": after,
                                         "after_url": f"{BASE}/api/v2/incremental/tickets/cursor.json?cursor={after}",
                                         "before_cursor": None, "before_url": None, "end_of_stream": offset + size >= len(ordered)})


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("ZD_TOKEN", TOKEN)


def make(site, clock=lambda: NOW, **cfg):
    sleeps = []
    config = {"subdomain": "acme", "email": EMAIL, "token_env": "ZD_TOKEN", "default_acl": ["group:support"], **cfg}
    connector = ZendeskConnector(config, transport=httpx.MockTransport(site.handler), sleep=sleeps.append, clock=clock)
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


def scopes(batches):
    return [b.complete_scope for b in batches if b.complete_scope]


ARTICLES = [article(1, "Refund policy", body="<h2>Refunds</h2><p>We refund within <b>14 days</b>. See <a href=\"https://acme.example/terms\">terms</a>.</p><ul><li>Card</li><li>PayPal</li></ul>",
                    section_id=11, updated="2026-03-01T10:00:00Z"),
            article(2, "Pay an invoice", section_id=10, updated="2026-03-02T10:00:00Z"),
            article(3, "First steps", body="<p>Welcome &amp; hello</p><script>track()</script>", section_id=20, updated="2026-03-03T10:00:00Z"),
            article(4, "Card declined", section_id=10, updated="2026-03-04T10:00:00Z"),
            article(5, "Receipts", section_id=10, updated="2026-03-05T10:00:00Z")]


def help_center(**kw):
    return Zendesk(articles=ARTICLES, **kw)


def tickets_only(site, **cfg):
    return make(site, include_articles=False, include_tickets=True, **cfg)


# ── articles ──


def test_a_full_listing_follows_next_page_and_names_the_path_after_category_and_section():
    site = help_center()
    changes, batches = run(make(site))
    assert [c.external_id for c in changes] == [f"zendesk:article/en-us/{n}" for n in range(1, 6)]
    by = {c.external_id: c.file for c in changes}
    assert by["zendesk:article/en-us/1"].path == "zendesk/help/en-us/Billing/Refunds/1-Refund policy.md"
    assert by["zendesk:article/en-us/3"].path == "zendesk/help/en-us/Onboarding/Getting started/3-First steps.md"
    listing = site.calls(r"/en-us/articles$")
    assert [r.url.params.get("page") for r in listing] == [None, "2", "3"] and listing[0].url.params["per_page"] == "100"
    assert scopes(batches) == ["zendesk:article/en-us/"] and batches[-1].done
    assert len(site.calls(r"/sections$")) == 2 and len(site.calls(r"/categories$")) == 1             # names are fetched once per scan, paginated too
    f = by["zendesk:article/en-us/2"]
    assert f.acl == ["group:support"] and f.mime == "text/markdown" and f.modified_at.isoformat().startswith("2026-03-02T10:00:00")


def test_article_text_is_the_body_as_readable_text_with_title_and_metadata():
    site = help_center()
    connector = make(site)
    by = {c.external_id: c.file for c in run(connector)[0]}
    text = read(connector, by["zendesk:article/en-us/1"])
    assert text.startswith("# Refund policy\n") and "labels: billing" in text and f"url: {BASE}/hc/en-us/articles/1" in text
    assert "Refunds" in text and "We refund within **14 days**." in text and "[terms](https://acme.example/terms)" in text
    assert "Card" in text and "PayPal" in text and "<" not in text.split("\n\n", 1)[1]
    third = read(connector, by["zendesk:article/en-us/3"])
    assert "Welcome & hello" in third and "track()" not in third and "&amp;" not in third
    assert by["zendesk:article/en-us/1"].size == len(text.encode())
    assert "/api/v2/help_center/en-us/articles/1" in site.paths()


def test_open_renders_without_asking_for_sections_and_categories():
    site = help_center()
    by = {c.external_id: c.file for c in run(make(site))[0]}
    site.reset()
    read(make(site), by["zendesk:article/en-us/2"])
    assert site.paths() == ["/api/v2/help_center/en-us/articles/2"]


def test_each_configured_locale_is_listed_and_declared_on_its_own():
    german = [article(31, "Rückerstattung", locale="de", section_id=10, updated="2026-03-01T10:00:00Z")]
    site = Zendesk(articles=ARTICLES + german)
    site.sections += [{"id": 10, "category_id": 1, "name": "Zahlungen", "locale": "de"}]
    site.categories += [{"id": 1, "name": "Abrechnung", "locale": "de"}]
    changes, batches = run(make(site, locales=["en-us", "de"]))
    assert scopes(batches) == ["zendesk:article/en-us/", "zendesk:article/de/"]
    de = next(c.file for c in changes if c.external_id == "zendesk:article/de/31")
    assert de.path == "zendesk/help/de/Abrechnung/Zahlungen/31-Rückerstattung.md" and len(changes) == 6


def test_a_next_page_on_another_host_is_refused_and_the_credentials_never_go_there():
    site = help_center()
    original = site.offset_page

    def evil(items, key, q, path):
        r = original(items, key, q, path)
        body = r.json()
        if key == "articles" and body["next_page"]:
            body["next_page"] = "https://evil.example/steal?page=2"
        return httpx.Response(200, json=body)
    site.offset_page = evil
    with pytest.raises(HttpError, match="another host"):
        run(make(site))
    assert {r.url.host for r in site.requests} == {HOST}


def test_drafts_and_user_segment_restricted_articles_are_not_indexed_and_are_deleted_if_they_were():
    mixed = [article(1, "Public"), article(2, "Draft", draft=True, updated="2026-03-02T10:00:00Z"),
             article(3, "Staff only", user_segment_id=555, updated="2026-03-03T10:00:00Z")]
    site = Zendesk(articles=mixed)
    changes, batches = run(make(site))
    assert [(c.kind, c.external_id) for c in changes] == [("upsert", "zendesk:article/en-us/1"), ("delete", "zendesk:article/en-us/2"), ("delete", "zendesk:article/en-us/3")]
    changes, _ = run(make(site, include_drafts=True))
    assert [c.kind for c in changes] == ["upsert", "upsert", "delete"]
    changes, _ = run(make(site, include_restricted_articles=True))
    assert [c.kind for c in changes] == ["upsert", "delete", "upsert"]
    with pytest.raises(HttpError) as err:
        make(site).open(SourceFile("zendesk:article/en-us/3", "p", "n", 0, None, "v", []))
    assert err.value.status == 403
    assert "Staff only" in read(make(site, include_restricted_articles=True), SourceFile("zendesk:article/en-us/3", "p", "n", 0, None, "v", []))


def test_version_follows_content_and_location_but_not_vote_counts():
    base = [article(1, "Refund policy", section_id=11)]
    v1 = run(make(Zendesk(articles=base)))[0][0].file.version
    assert run(make(Zendesk(articles=copy.deepcopy(base))))[0][0].file.version == v1
    voted = copy.deepcopy(base)
    voted[0].update({"vote_sum": 4, "vote_count": 6, "updated_at": "2026-03-09T10:00:00Z"})
    assert run(make(Zendesk(articles=voted)))[0][0].file.version == v1                       # metadata churn is not a content change
    edited = copy.deepcopy(base)
    edited[0]["body"] = "<p>Changed</p>"
    assert run(make(Zendesk(articles=edited)))[0][0].file.version != v1
    retitled = copy.deepcopy(base)
    retitled[0]["title"] = "Refunds"
    assert run(make(Zendesk(articles=retitled)))[0][0].file.version != v1
    site = Zendesk(articles=copy.deepcopy(base))
    site.sections[1]["name"] = "Money back"
    assert run(make(site))[0][0].file.version != v1                                             # the path moved


def test_article_scan_after_a_finished_one_uses_the_incremental_feed_and_stops_when_it_catches_up():
    site = help_center()
    _, batches = run(make(site))
    cursor = batches[-1].cursor
    assert cursor["since"] == "2026-03-05T10:00:00Z" and cursor["last_full"] == NOW
    site.articles[3]["title"] = "Card declined (updated)"
    site.articles[3]["updated_at"] = "2026-03-08T10:00:00Z"
    site.reset()
    changes, batches = run(make(site, clock=lambda: NOW + 3600), cursor)
    feed = site.calls(r"/incremental/articles$")
    assert feed[0].url.params["start_time"] == str(epoch("2026-03-05T10:00:00Z") - 300)       # watermark minus the overlap
    assert not site.calls(r"/en-us/articles$") and len(feed) == 2           # the second page repeats the last article; its next_page is the URL just asked
    assert sorted({c.external_id for c in changes}) == ["zendesk:article/en-us/4", "zendesk:article/en-us/5"]
    assert scopes(batches) == [] and batches[-1].cursor["since"] == "2026-03-08T10:00:00Z" and batches[-1].cursor["last_full"] == NOW
    assert any("(updated)" in c.file.path for c in changes if c.kind == "upsert")


def test_the_incremental_feed_keeps_only_the_configured_locales():
    other = article(77, "Hallo", locale="fr", updated="2026-03-07T10:00:00Z")
    site = Zendesk(articles=ARTICLES + [other])
    cursor = {"since": "2026-03-04T10:00:00Z", "last_full": NOW}
    changes, _ = run(make(site, clock=lambda: NOW + 60), cursor)
    assert {c.external_id for c in changes} == {"zendesk:article/en-us/4", "zendesk:article/en-us/5"}


def test_a_page_of_the_incremental_feed_that_holds_only_other_locales_does_not_end_the_feed():
    others = [article(80 + n, f"Bonjour {n}", locale="fr", updated=f"2026-03-0{n}T10:00:00Z") for n in (6, 7)]
    site = Zendesk(articles=ARTICLES + others + [article(9, "Late English", updated="2026-03-09T10:00:00Z")])
    cursor = {"since": "2026-03-05T10:00:00Z", "last_full": NOW}
    changes, _ = run(make(site, clock=lambda: NOW + 60, locales=["EN-us"]), cursor)
    assert {c.external_id for c in changes} == {"zendesk:article/en-us/5", "zendesk:article/en-us/9"}


def test_full_resync_when_due_lists_every_locale_again_and_declares_the_scope():
    site = help_center()
    cursor = {"since": "2026-03-05T10:00:00Z", "last_full": NOW}
    changes, batches = run(make(site, clock=lambda: NOW + 25 * 3600), cursor)
    assert len(changes) == 5 and scopes(batches) == ["zendesk:article/en-us/"] and not site.calls("/incremental/")


def test_a_listing_resumes_at_the_saved_next_page_and_does_not_claim_completeness():
    site = help_center()
    mid = next(b for b in make(site).scan({}) if b.cursor["pos"].get("next"))
    assert mid.cursor["pos"]["phase"] == "articles" and mid.cursor["pos"]["next"].endswith("page=2")
    assert json.loads(json.dumps(mid.cursor)) == mid.cursor
    site.reset()
    changes, batches = run(make(site), mid.cursor)
    assert [c.external_id for c in changes] == [f"zendesk:article/en-us/{n}" for n in (3, 4, 5)] and scopes(batches) == []
    assert site.calls(r"/en-us/articles$")[0].url.params["page"] == "2"


def test_a_resume_in_the_second_locale_skips_the_first():
    german = [article(31, "Rückerstattung", locale="de", updated="2026-03-01T10:00:00Z")]
    site = Zendesk(articles=ARTICLES + german)
    site.sections += [{"id": 10, "category_id": 1, "name": "Zahlungen", "locale": "de"}]
    site.categories += [{"id": 1, "name": "Abrechnung", "locale": "de"}]
    batches = list(make(site, locales=["en-us", "de"]).scan({}))
    boundary = next(b for b in batches if b.complete_scope == "zendesk:article/en-us/")
    assert boundary.cursor["pos"] == {"phase": "articles", "src": 1}
    site.reset()
    changes, resumed = run(make(site, locales=["en-us", "de"]), boundary.cursor)
    assert [c.external_id for c in changes] == ["zendesk:article/de/31"] and not site.calls("/en-us/")
    assert scopes(resumed) == ["zendesk:article/de/"]


def test_max_items_and_max_pages_truncate_and_never_declare_completeness():
    site = help_center()
    changes, batches = run(make(site, max_items=3))
    assert len(changes) == 3 and scopes(batches) == [] and "since" not in batches[-1].cursor
    site.reset()
    changes, batches = run(make(site, max_pages=2))
    assert len(site.calls(r"/en-us/articles$")) == 2 and len(changes) == 4 and scopes(batches) == [] and "since" not in batches[-1].cursor


# ── tickets ──

TICKETS = [ticket(n, f"Printer {n} on fire", updated=f"2026-03-0{n}T10:00:00Z", group_id=77 if n % 2 else 88) for n in range(1, 6)]
USERS = [user(900, "Ada Lovelace"), user(5, "Grace Hopper", role="agent")]
COMMENTS = {n: [zcomment(n * 10, 900, f"Help number {n}"), zcomment(n * 10 + 1, 5, "internal only", public=False), zcomment(n * 10 + 2, 5, "We are on it", created="2026-03-02T09:00:00Z")]
            for n in range(1, 6)}


def with_tickets(**kw):
    return Zendesk(tickets=TICKETS, comments=COMMENTS, users=USERS, **kw)


def test_the_ticket_export_is_followed_by_cursor_until_end_of_stream_and_deleted_tickets_become_deletes():
    tickets = copy.deepcopy(TICKETS)
    tickets[2]["status"] = "deleted"
    site = Zendesk(tickets=tickets, comments=COMMENTS, users=USERS)
    changes, batches = run(tickets_only(site))
    assert [(c.kind, c.external_id) for c in changes] == [("upsert", "zendesk:ticket/1"), ("upsert", "zendesk:ticket/2"), ("delete", "zendesk:ticket/3"),
                                                          ("upsert", "zendesk:ticket/4"), ("upsert", "zendesk:ticket/5")]
    export = site.calls(r"/incremental/tickets/cursor\.json$")
    assert [r.url.params.get("cursor") for r in export] == [None, f"{site.nonce}c2", f"{site.nonce}c4"]
    assert export[0].url.params["start_time"] == "0" and export[0].url.params["per_page"] == "100"
    assert scopes(batches) == ["zendesk:ticket/"] and batches[-1].done and not site.calls(r"/tickets/3/comments")


def test_ticket_text_has_metadata_names_and_public_comments_only():
    site = with_tickets()
    connector = tickets_only(site)
    changes, _ = run(connector)
    f = changes[0].file
    assert f.path == "zendesk/tickets/group-77/1-Printer 1 on fire.md" and changes[1].file.path.startswith("zendesk/tickets/group-88/")
    text = read(connector, f)
    for line in ("type: ticket", "status: open", "priority: normal", "requester: Ada Lovelace", "tags: vip, refund", f"url: {BASE}/agent/tickets/1"):
        assert line in text
    assert text.startswith("# Ticket #1: Printer 1 on fire\n")
    assert "### Ada Lovelace (2026-03-02T08:00:00Z)\n\nHelp number 1" in text and "### Grace Hopper (2026-03-02T09:00:00Z)\n\nWe are on it" in text
    assert "internal only" not in text
    internal = read(tickets_only(site, include_internal_comments=True), f)
    assert "internal only" in internal and f.size == len(text.encode())


def test_comments_are_paginated_and_html_only_comments_become_text():
    many = [zcomment(n, 900, f"message {n}") for n in range(1, 6)]
    many[4].update({"body": "", "plain_body": "", "html_body": "<p>from <b>html</b></p>"})
    site = Zendesk(tickets=[ticket(7, "Chatty")], comments={7: many}, users=USERS)
    connector = tickets_only(site)
    sf = run(connector)[0][0].file
    assert len(site.calls(r"/tickets/7/comments\.json$")) == 3
    text = read(connector, sf)
    assert all(f"message {n}" in text for n in (1, 2, 3, 4)) and "from **html**" in text


def test_a_ticket_without_a_group_goes_under_group_none_and_acl_rules_follow_the_group():
    site = Zendesk(tickets=[ticket(1, "A", group_id=77), ticket(2, "B", group_id=None, updated="2026-03-02T10:00:00Z"), ticket(3, "C", group_id=88, updated="2026-03-03T10:00:00Z")],
                   comments=COMMENTS, users=USERS)
    rules = [{"prefix": "zendesk/tickets/group-77", "principals": ["group:tier1"]}, {"prefix": "zendesk/help", "principals": ["*"]}]
    connector = tickets_only(site, acl_rules=rules, default_acl=None)
    by = {c.external_id: c.file for c in run(connector)[0]}
    assert by["zendesk:ticket/1"].acl == ["group:tier1"] and by["zendesk:ticket/2"].acl == [] and by["zendesk:ticket/3"].acl == []
    assert by["zendesk:ticket/2"].path.startswith("zendesk/tickets/group-none/")
    assert connector.refresh_acl(persisted(by["zendesk:ticket/1"])) == ["group:tier1"] and connector.refresh_acl(persisted(by["zendesk:ticket/3"])) == []


def test_ticket_scan_after_a_finished_one_exports_from_the_watermark_and_is_not_authoritative():
    site = with_tickets()
    _, batches = run(tickets_only(site))
    cursor = batches[-1].cursor
    assert cursor["since"] == "2026-03-05T10:00:00Z" and cursor["last_full"] == NOW
    site.tickets[4]["subject"] = "Printer 5 on fire (again)"
    site.tickets[4]["updated_at"] = "2026-03-08T10:00:00Z"
    site.tickets[4]["generated_timestamp"] = epoch("2026-03-08T10:00:00Z")
    site.reset()
    changes, batches = run(tickets_only(site, clock=lambda: NOW + 3600), cursor)
    assert site.calls(r"/cursor\.json$")[0].url.params["start_time"] == str(epoch("2026-03-05T10:00:00Z") - 300)
    assert [c.external_id for c in changes] == ["zendesk:ticket/5"] and scopes(batches) == []
    assert batches[-1].cursor["since"] == "2026-03-08T10:00:00Z"


def test_full_resync_of_tickets_and_tickets_since_limit_the_first_export():
    site = with_tickets()
    cursor = {"since": "2026-03-05T10:00:00Z", "last_full": NOW}
    changes, batches = run(tickets_only(site, clock=lambda: NOW + 25 * 3600), cursor)
    assert len(changes) == 5 and scopes(batches) == ["zendesk:ticket/"]
    site.reset()
    changes, _ = run(tickets_only(site, tickets_since="2026-03-04"))
    assert site.calls(r"/cursor\.json$")[0].url.params["start_time"] == str(epoch("2026-03-04T00:00:00Z"))
    assert [c.external_id for c in changes] == ["zendesk:ticket/4", "zendesk:ticket/5"]


def test_ticket_export_resumes_at_the_saved_cursor_and_restarts_when_the_cursor_has_expired():
    site = with_tickets()
    connector = tickets_only(site)
    mid = next(b for b in connector.scan({}) if b.cursor["pos"].get("cursor"))
    assert mid.cursor["pos"] == {"phase": "tickets", "cursor": f"{site.nonce}c2"} and json.loads(json.dumps(mid.cursor)) == mid.cursor
    site.reset()
    changes, batches = run(tickets_only(site), mid.cursor)
    assert [c.external_id for c in changes] == [f"zendesk:ticket/{n}" for n in (3, 4, 5)] and scopes(batches) == []
    expiring = with_tickets(expire_cursors=True)
    changes, batches = run(tickets_only(expiring), mid.cursor)
    assert [c.external_id for c in changes][:2] == ["zendesk:ticket/1", "zendesk:ticket/2"] and len(changes) == 5
    assert scopes(batches) == ["zendesk:ticket/"]                                                 # it ran from the start to the end after all


def test_ticket_version_follows_the_text_and_open_refetches_the_ticket():
    site = with_tickets()
    by = {c.external_id: c.file for c in run(tickets_only(site))[0]}
    v1 = by["zendesk:ticket/2"].version
    edited = with_tickets()
    edited.comments[2].append(zcomment(99, 5, "Fixed", created="2026-03-03T09:00:00Z"))
    assert run(tickets_only(edited))[0][1].file.version != v1
    assert run(tickets_only(with_tickets()))[0][1].file.version == v1
    site.reset()
    for sf in by.values():
        assert len(read(make(site), sf).encode()) == sf.size
    site.tickets[0]["status"] = "deleted"
    with pytest.raises(HttpError) as err:
        make(site).open(SourceFile("zendesk:ticket/1", "p", "n", 0, None, "v", []))
    assert err.value.status == 404
    with pytest.raises(HttpError) as err:
        make(site).open(SourceFile("zendesk:ticket/404", "p", "n", 0, None, "v", []))
    assert err.value.status == 404


def test_tickets_max_items_and_max_pages_truncate_without_claiming_completeness():
    site = with_tickets()
    changes, batches = run(tickets_only(site, max_items=3))
    assert len(changes) == 3 and scopes(batches) == [] and "since" not in batches[-1].cursor
    site.reset()
    changes, batches = run(tickets_only(site, max_pages=2))
    assert len(site.calls(r"/cursor\.json$")) == 2 and len(changes) == 4 and scopes(batches) == []


def test_articles_come_first_then_tickets_each_with_its_own_scope():
    site = Zendesk(articles=ARTICLES, tickets=TICKETS, comments=COMMENTS, users=USERS)
    changes, batches = run(make(site, include_tickets=True))
    assert [c.external_id.split("/")[0] for c in changes] == ["zendesk:article"] * 5 + ["zendesk:ticket"] * 5
    assert scopes(batches) == ["zendesk:article/en-us/", "zendesk:ticket/"]
    boundary = next(b for b in batches if b.complete_scope == "zendesk:article/en-us/")
    assert boundary.cursor["pos"] == {"phase": "tickets"}
    site.reset()
    changes, _ = run(make(site, include_tickets=True), boundary.cursor)
    assert {c.external_id.split("/")[0] for c in changes} == {"zendesk:ticket"} and not site.calls("/help_center/")


# ── permissions, rate limits, auth ──


def test_acl_rules_per_path_prefix_fail_closed_for_articles():
    site = help_center()
    rules = [{"prefix": "zendesk/help/en-us/Billing", "principals": ["group:billing"]}, {"prefix": "zendesk/help/en-us/Billing/Refunds", "principals": ["*"]}]
    connector = make(site, acl_rules=rules, default_acl=None)
    by = {c.external_id: c.file for c in run(connector)[0]}
    assert by["zendesk:article/en-us/1"].acl == ["*"] and by["zendesk:article/en-us/2"].acl == ["group:billing"]
    assert by["zendesk:article/en-us/3"].acl == []
    assert connector.refresh_acl(persisted(by["zendesk:article/en-us/3"])) == [] and connector.refresh_acl(persisted(by["zendesk:article/en-us/2"])) == ["group:billing"]


def test_429_is_retried_after_retry_after():
    site = help_center()
    site.script = [httpx.Response(429, headers={"retry-after": "11"}, json={"error": "Rate limit"}), httpx.Response(503, text="busy")]
    connector = make(site)
    changes, _ = run(connector)
    assert len(changes) == 5 and connector.sleeps == [11.0, 2]


def test_401_never_shows_the_token_the_email_or_the_encoded_credentials_and_403_names_the_variable(monkeypatch):
    monkeypatch.setenv("ZD_TOKEN", "zdtoken-wrongwrongwrong")
    site = help_center()
    connector = make(site)
    with pytest.raises(AuthError) as err:
        run(connector)
    message = str(err.value)
    assert "401" in message and "zdtoken-wrongwrongwrong" not in message and TOKEN not in message and "ZD_TOKEN" in message
    assert len(site.requests) == 1 and connector.sleeps == []
    site = help_center()
    site.script = [httpx.Response(403, json={"error": "Forbidden"})]
    with pytest.raises(AuthError) as err:
        run(make(site))
    assert "403" in str(err.value) and "ZD_TOKEN" in str(err.value)


def test_bearer_auth_sends_the_oauth_token_without_an_email():
    site = Zendesk(articles=ARTICLES, header=f"Bearer {TOKEN}")
    connector = ZendeskConnector({"subdomain": "acme", "auth": "bearer", "token_env": "ZD_TOKEN", "default_acl": ["*"]},
                                 transport=httpx.MockTransport(site.handler), sleep=lambda s: None, clock=lambda: NOW)
    assert len(run(connector)[0]) == 5


def test_a_server_that_echoes_the_credentials_in_an_error_body_never_gets_them_into_the_message():
    site = with_tickets()
    site.script = [httpx.Response(500, text=f"rejected {TOKEN} / {BASIC.split(' ', 1)[1]}")] * 6
    with pytest.raises(HttpError) as err:
        run(make(site))
    assert TOKEN not in str(err.value) and BASIC.split(" ", 1)[1] not in str(err.value) and "***" in str(err.value)


def test_cursors_hold_no_secret_and_a_missing_variable_is_named(monkeypatch):
    _, batches = run(make(with_tickets(), include_tickets=True, include_articles=False))
    assert TOKEN not in json.dumps([b.cursor for b in batches]) and EMAIL not in json.dumps([b.cursor for b in batches])
    monkeypatch.delenv("ZD_TOKEN")
    with pytest.raises(HttpError, match="ZD_TOKEN"):
        run(make(help_center()))


def test_open_rejects_ids_that_are_not_zendesk_objects_without_a_request():
    site = help_center()
    connector = make(site)
    for ext in ("zendesk:article/en-us/abc", "zendesk:article/../1", "zendesk:article/en us/1", "zendesk:ticket/x", "zendesk:other/1", "github:article/en-us/1",
                "zendesk:article/en-us/1/../2"):
        with pytest.raises(HttpError):
            connector.open(SourceFile(ext, "p", "n", 0, None, "v", []))
    assert site.requests == []


def test_configuration_errors_are_rejected_up_front():
    with pytest.raises(ValueError, match="subdomain"):
        ZendeskConnector({"email": EMAIL, "token_env": "X"})
    for bad in ("ac me", "acme.evil.example", "../x", "acme/x"):
        with pytest.raises(ValueError, match="subdomain"):
            ZendeskConnector({"subdomain": bad, "email": EMAIL})
    with pytest.raises(ValueError, match="email"):
        ZendeskConnector({"subdomain": "acme"})
    with pytest.raises(ValueError, match="auth"):
        ZendeskConnector({"subdomain": "acme", "auth": "basic"})
    with pytest.raises(ValueError, match="locales"):
        ZendeskConnector({"subdomain": "acme", "email": EMAIL, "locales": ["en us"]})
    with pytest.raises(ValueError, match="nothing to index"):
        ZendeskConnector({"subdomain": "acme", "email": EMAIL, "include_articles": False})
    assert ZendeskConnector({"base_url": "https://help.example.com/", "email": EMAIL}).base == "https://help.example.com"


def test_a_cursor_written_for_a_different_configuration_starts_over_instead_of_failing():
    cursor = {"mode": "full", "since": None, "last_full": None, "high": None, "pos": {"phase": "tickets", "cursor": "c2"}}
    changes, _ = run(make(help_center()), cursor)
    assert len(changes) == 5


# ── loops, fallbacks and the edges of "complete" (added after a mutation run) ──


def test_a_next_page_that_repeats_is_an_error_for_sections_and_comments():
    for key, config in (("sections", {}), ("comments", {"include_articles": False, "include_tickets": True})):
        site = help_center() if key == "sections" else with_tickets()
        original, calls = site.offset_page, []

        def stuck(items, k, q, path, original=original, key=key, calls=calls):
            body = original(items, k, q, path).json()
            if k == key:
                calls.append(path)
                body["next_page"] = f"{BASE}{path}?include=users&sort_order=asc&page=2" if len(calls) < 8 else None        # the same URL again and again (it would end after 8)
            return httpx.Response(200, json=body)
        site.offset_page = stuck
        with pytest.raises(HttpError, match="did not advance"):
            run(make(site, **config))
        assert len(calls) == 2                                                      # the repeat is refused before it is requested


def test_section_and_category_names_are_fetched_again_by_the_next_scan():
    site = help_center()
    connector = make(site)
    first = {c.external_id: c.file for c in run(connector)[0]}
    assert first["zendesk:article/en-us/1"].path == "zendesk/help/en-us/Billing/Refunds/1-Refund policy.md"
    site.sections[1]["name"] = "Money back"
    site.categories[0]["name"] = "Payments and billing"
    second = {c.external_id: c.file for c in run(connector)[0]}
    assert second["zendesk:article/en-us/1"].path == "zendesk/help/en-us/Payments and billing/Money back/1-Refund policy.md"


def test_articles_in_unknown_or_unnamed_sections_and_categories_get_fallback_folders():
    site = Zendesk(articles=[article(1, "Orphan", section_id=99), article(2, "No category", section_id=12), article(3, "Unnamed", section_id=13)])
    site.sections += [{"id": 12, "category_id": 7, "name": "Loose", "locale": "en-us"}, {"id": 13, "category_id": 1, "name": "", "locale": "en-us"}]
    by = {c.external_id: c.file.path for c in run(make(site))[0]}
    assert by["zendesk:article/en-us/1"] == "zendesk/help/en-us/category-none/section-99/1-Orphan.md"
    assert by["zendesk:article/en-us/2"] == "zendesk/help/en-us/category-7/Loose/2-No category.md"
    assert by["zendesk:article/en-us/3"] == "zendesk/help/en-us/Billing/section-13/3-Unnamed.md"


def test_the_incremental_feed_matches_locales_without_regard_to_case():
    site = Zendesk(articles=ARTICLES + [article(55, "Mixed case", locale="en-US", updated="2026-03-07T10:00:00Z")])
    changes, _ = run(make(site, clock=lambda: NOW + 60), {"since": "2026-03-06T10:00:00Z", "last_full": NOW})
    assert {c.external_id for c in changes} == {"zendesk:article/en-US/55"}


def test_an_empty_page_of_the_incremental_feed_ends_it_even_when_it_advertises_another_page():
    site = Zendesk(articles=ARTICLES)
    served = []

    def endless(q):
        served.append(q)
        start = int(q["start_time"])
        return httpx.Response(200, json={"articles": [], "count": 0, "end_time": start,
                                         "next_page": f"{BASE}/api/v2/help_center/incremental/articles?start_time={start + 1}" if len(served) < 10 else None})
    site.hc_incremental = endless
    changes, batches = run(make(site, clock=lambda: NOW + 60), {"since": "2026-03-10T00:00:00Z", "last_full": NOW})
    assert changes == [] and len(served) == 1 and batches[-1].done


def test_end_of_stream_on_a_feed_page_ends_the_feed():
    site = Zendesk(articles=ARTICLES)
    page = {"articles": [ARTICLES[4]], "count": 1, "end_of_stream": True, "next_page": f"{BASE}/api/v2/help_center/incremental/articles?start_time=1999999999"}
    site.script = [httpx.Response(200, json=page)]
    changes, _ = run(make(site, clock=lambda: NOW + 60), {"since": "2026-03-04T10:00:00Z", "last_full": NOW})
    assert [c.external_id for c in changes] == ["zendesk:article/en-us/5"] and len(site.calls(r"/incremental/articles$")) == 1


def test_a_final_page_that_was_cut_short_by_max_items_never_declares_completeness():
    site = Zendesk(articles=ARTICLES, cap=5)                                   # everything on one page, no next_page
    changes, batches = run(make(site, max_items=3))
    assert len(changes) == 3 and scopes(batches) == [] and "since" not in batches[-1].cursor
    changes, batches = run(make(Zendesk(articles=ARTICLES, cap=5), max_items=5))               # exactly everything: nothing was cut
    assert len(changes) == 5 and scopes(batches) == ["zendesk:article/en-us/"] and "since" in batches[-1].cursor
    changes, batches = run(tickets_only(Zendesk(tickets=TICKETS, comments=COMMENTS, users=USERS, cap=5), max_items=3))
    assert len(changes) == 3 and scopes(batches) == [] and "since" not in batches[-1].cursor
    changes, batches = run(tickets_only(Zendesk(tickets=TICKETS, comments=COMMENTS, users=USERS, cap=5), max_items=5))
    assert len(changes) == 5 and scopes(batches) == ["zendesk:ticket/"]


def test_comment_text_prefers_plain_body_and_comments_without_any_text_are_left_out():
    comments = {7: [zcomment(1, 900, "**bold** body", plain_body="bold plain"),
                    zcomment(2, 5, "", html_body="", plain_body=""),
                    zcomment(3, 900, "", html_body="<p>only <b>html</b></p>", plain_body=""),
                    zcomment(4, 5, "   ", html_body="  ", plain_body="  ")]}
    site = Zendesk(tickets=[ticket(7, "Sparse")], comments=comments, users=USERS)
    connector = tickets_only(site)
    text = read(connector, run(connector)[0][0].file)
    assert "bold plain" in text and "**bold** body" not in text and "only **html**" in text
    assert text.count("### ") == 2 and "Grace Hopper" not in text                  # the two empty comments were agent comments


def test_the_ticket_watermark_follows_generated_timestamp_and_falls_back_to_updated_at():
    generated = epoch("2026-03-07T00:00:00Z")
    site = Zendesk(tickets=[ticket(1, "A", updated="2026-03-01T10:00:00Z", generated_timestamp=generated)], comments=COMMENTS, users=USERS)
    assert run(tickets_only(site))[1][-1].cursor["since"] == "2026-03-07T00:00:00Z"
    site = Zendesk(tickets=[ticket(1, "A", updated="2026-03-01T10:00:00Z", generated_timestamp=0)], comments=COMMENTS, users=USERS)
    assert run(tickets_only(site))[1][-1].cursor["since"] == "2026-03-01T10:00:00Z"


def test_only_an_expired_cursor_restarts_the_export_and_every_other_error_is_raised():
    cursor = {"mode": "full", "since": None, "last_full": None, "high": None, "pos": {"phase": "tickets", "cursor": "old-session-c2"}}
    for response in (httpx.Response(500, text="boom"), httpx.Response(403, json={"error": "Forbidden"})):
        site = with_tickets()
        site.script = [response]
        with pytest.raises(HttpError):
            run(tickets_only(site), cursor)
        assert len(site.calls(r"/cursor\.json$")) == 1                          # no silent restart from the beginning
    for status in (400, 404, 410, 422):
        site = with_tickets()
        site.script = [httpx.Response(status, json={"error": "gone"})]
        changes, batches = run(tickets_only(site), cursor)
        assert len(changes) == 5 and scopes(batches) == ["zendesk:ticket/"], status


def test_a_ticket_export_page_with_neither_after_cursor_nor_end_of_stream_is_an_error_not_the_end():
    for body in ({"tickets": [TICKETS[0]], "end_of_stream": False}, {"tickets": [], "after_cursor": None}, {"tickets": []}):
        site = with_tickets()
        site.script = [httpx.Response(200, json=body)]
        with pytest.raises(HttpError, match="after_cursor"):
            run(tickets_only(site))
    site = with_tickets()
    site.script = [httpx.Response(200, json={"tickets": [TICKETS[0]], "end_of_stream": True, "after_cursor": None})]
    changes, batches = run(tickets_only(site))
    assert len(changes) == 1 and scopes(batches) == ["zendesk:ticket/"]


def test_open_of_an_article_or_ticket_the_api_no_longer_returns_is_a_404():
    for ext, body in (("zendesk:article/en-us/9", {}), ("zendesk:article/en-us/9", {"article": None}), ("zendesk:ticket/9", {"ticket": None})):
        site = Zendesk()
        site.script = [httpx.Response(200, json=body)]
        with pytest.raises(HttpError) as err:
            make(site).open(SourceFile(ext, "p", "n", 0, None, "v", []))
        assert err.value.status == 404
