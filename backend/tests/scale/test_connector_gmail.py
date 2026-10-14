"""Gmail connector against a stateful fake of the Gmail API v1 (users.getProfile, users.messages.list / get, users.history.list)
shaped like the Google documentation: `messages` lists carry only {id, threadId}; `nextPageToken` pages; `format=raw` returns the
whole RFC 822 message base64url encoded; `format=metadata` returns labelIds / internalDate / sizeEstimate / payload.headers; history
records hold messagesAdded / messagesDeleted / labelsAdded / labelsRemoved; an old startHistoryId answers HTTP 404 with the standard
Google error body.

The fake is stateful on purpose: add() / delete() / label() change the mailbox AND append history records, as Gmail does, so the
incremental tests drive the connector through realistic sequences rather than canned answers. It also honours `historyTypes` and
`metadataHeaders`, so a connector that asks for too little (or too much) is caught.

Written from memory of the reference, not certain of (the connector's behaviour on these is tested, the vendor behaviour is not proven):
  * the 403 `reason` values Google uses for throttling (`rateLimitExceeded`, `userRateLimitExceeded`);
  * whether every history page carries `historyId`, and whether `raw` is padded (both forms are accepted);
  * the exact `Retry-After` Google sends with a throttled response (the fake uses the standard header).
"""

import base64
import copy
import json
from datetime import datetime, timezone
from email.message import EmailMessage

import httpx
import pytest

from app.scale.connectors._http import AuthError, HttpError
from app.scale.connectors.base import SourceFile
from app.scale.connectors.gmail import GmailConnector

TOKEN = "ya29.a0-GMAIL-SECRET-TOKEN-9f3"
NOW = datetime(2026, 3, 10, 12, 0, tzinfo=timezone.utc).timestamp()
EMAIL = "support@acme.com"


def mid(n):
    return f"18c3f0a{n:09x}"                         # Gmail message ids are 16 hex characters


def b64url(data: bytes, pad=False):
    text = base64.urlsafe_b64encode(data).decode()
    return text if pad else text.rstrip("=")


def eml(subject, body="Hello team,\r\nplease see below.\r\n", sender="ada@example.com"):
    message = EmailMessage()
    message["From"], message["To"], message["Subject"] = sender, EMAIL, subject
    message["Date"] = "Tue, 03 Mar 2026 09:15:00 +0000"
    message["Message-ID"] = f"<{abs(hash(subject)) % 10**8}@example.com>"
    message.set_content(body)
    return message.as_bytes()


def mail(n, subject=None, *, labels=("INBOX",), when="2026-03-03T09:15:00+00:00", raw=None, sender="ada@example.com"):
    subject = f"Subject {n}" if subject is None else subject
    millis = int(datetime.fromisoformat(when).timestamp() * 1000)
    return {"id": mid(n), "threadId": mid(n), "labelIds": list(labels), "internalDate": str(millis),
            "subject": subject, "from": sender, "raw": raw if raw is not None else eml(subject, sender=sender)}


def google_error(code, reason, message="boom", status=""):
    return {"error": {"code": code, "message": message, "status": status,
                      "errors": [{"message": message, "domain": "global", "reason": reason}]}}


NOT_FOUND = google_error(404, "notFound", "Requested entity was not found.", "NOT_FOUND")


class Gmail:
    """order = newest first, as users.messages.list returns them. `page` caps maxResults like a server limit would."""

    def __init__(self, messages=(), *, page=2, history_page=2, email=EMAIL):
        self.email = email
        self.page, self.history_page = page, history_page
        self.msgs: dict[str, dict] = {}
        self.order: list[str] = []
        self.hid = 5000
        self.floor = 0                    # history ids below this are gone => HTTP 404
        self.records: list[dict] = []
        self.requests: list[httpx.Request] = []
        self.script: list[httpx.Response] = []
        self.script_for: dict[str, list[httpx.Response]] = {}     # path suffix -> responses for the next matching requests
        self.on_list = None               # called at the first messages.list request: a mailbox that changes while we list
        for message in messages:
            self.msgs[message["id"]] = copy.deepcopy(message)
            self.order.insert(0, message["id"])

    # ── mailbox mutations (each appends a history record, as Gmail does) ──

    def _record(self, **events):
        self.hid += 1
        self.records.append({"id": str(self.hid), **events})

    @staticmethod
    def _ref(message):
        return {"id": message["id"], "threadId": message["threadId"], "labelIds": list(message["labelIds"])}

    def add(self, message):
        self.msgs[message["id"]] = copy.deepcopy(message)
        self.order.insert(0, message["id"])
        self._record(messages=[{"id": message["id"], "threadId": message["threadId"]}],
                     messagesAdded=[{"message": self._ref(message)}])

    def delete(self, message_id):
        message = self.msgs.pop(message_id)
        self.order.remove(message_id)
        self._record(messages=[{"id": message_id, "threadId": message["threadId"]}],
                     messagesDeleted=[{"message": self._ref(message)}])

    def label(self, message_id, add=(), remove=()):
        message = self.msgs[message_id]
        message["labelIds"] = [x for x in message["labelIds"] if x not in remove] + [x for x in add if x not in message["labelIds"]]
        events = {"messages": [{"id": message_id, "threadId": message["threadId"]}]}
        if add:
            events["labelsAdded"] = [{"message": self._ref(message), "labelIds": list(add)}]
        if remove:
            events["labelsRemoved"] = [{"message": self._ref(message), "labelIds": list(remove)}]
        self._record(**events)

    def expire_history(self):
        self.floor = self.hid + 1

    # ── protocol ──

    def reset(self):
        self.requests.clear()

    def calls(self, tail):
        return [r for r in self.requests if r.url.path.endswith(tail)]

    def lists(self):
        return [r for r in self.requests if r.url.path.endswith("/messages")]

    def histories(self):
        return [r for r in self.requests if r.url.path.endswith("/history")]

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.script:
            return self.script.pop(0)
        for suffix, queue in self.script_for.items():
            if queue and request.url.path.endswith(suffix):
                return queue.pop(0)
        if request.headers.get("authorization") != f"Bearer {TOKEN}":
            return httpx.Response(401, json=google_error(401, "authError", "Invalid Credentials", "UNAUTHENTICATED"))
        assert "access_token" not in request.url.query.decode() and "key=" not in request.url.query.decode(), "token in the URL"
        prefix = "/gmail/v1/users/"
        path = request.url.path
        assert path.startswith(prefix), path
        user, _, route = path[len(prefix):].partition("/")
        q = request.url.params
        if route == "profile":
            return httpx.Response(200, json={"emailAddress": self.email, "messagesTotal": len(self.msgs), "threadsTotal": len(self.msgs), "historyId": str(self.hid)})
        if route == "messages":
            return self._list(q)
        if route.startswith("messages/"):
            return self._get(route.split("/", 1)[1], q)
        if route == "history":
            return self._history(q)
        return httpx.Response(404, json=NOT_FOUND)

    def _list(self, q):
        if self.on_list:
            hook, self.on_list = self.on_list, None
            hook(self)
        wanted = q.get_list("labelIds")
        include_all = q.get("includeSpamTrash") == "true"
        ids = []
        for message_id in self.order:
            message = self.msgs[message_id]
            if not include_all and {"SPAM", "TRASH"} & set(message["labelIds"]):
                continue
            if not set(wanted) <= set(message["labelIds"]):
                continue
            if q.get("q", "").startswith("from:") and message["from"] != q["q"][5:]:
                continue
            ids.append(message_id)
        size = min(int(q["maxResults"]), self.page)
        offset = int(q["pageToken"][3:7]) if q.get("pageToken") else 0
        if q.get("pageToken"):
            assert q["pageToken"].startswith("AEp") and q["pageToken"].endswith("Zq"), "page tokens are opaque: pass them back untouched"
        part = ids[offset:offset + size]
        body = {"resultSizeEstimate": len(ids)}
        if part:
            body["messages"] = [{"id": i, "threadId": self.msgs[i]["threadId"]} for i in part]
        if offset + size < len(ids):
            body["nextPageToken"] = f"AEp{offset + size:04d}Zq"
        return httpx.Response(200, json=body)

    def _get(self, message_id, q):
        message = self.msgs.get(message_id)
        if message is None:
            return httpx.Response(404, json=NOT_FOUND)
        base = {"id": message_id, "threadId": message["threadId"], "labelIds": message["labelIds"], "sizeEstimate": len(message["raw"]),
                "historyId": str(self.hid)}
        fmt = q.get("format", "full")
        if fmt == "raw":
            return httpx.Response(200, json={**base, "raw": message["raw"] if isinstance(message["raw"], str) else b64url(message["raw"])})
        assert fmt == "metadata", f"the connector asked for format={fmt}"
        wanted = {h.lower() for h in q.get_list("metadataHeaders")}
        headers = [{"name": n, "value": v} for n, v in (("From", message["from"]), ("Subject", message["subject"]), ("Date", "Tue, 03 Mar 2026 09:15:00 +0000"))
                   if not wanted or n.lower() in wanted]
        return httpx.Response(200, json={**base, "snippet": "…", "internalDate": message["internalDate"],
                                         "payload": {"partId": "", "mimeType": "text/plain", "filename": "", "headers": headers}})

    def _history(self, q):
        start = int(q["startHistoryId"])
        if start < self.floor:
            return httpx.Response(404, json=NOT_FOUND)
        types = set(q.get_list("historyTypes")) or {"messageAdded", "messageDeleted", "labelAdded", "labelRemoved"}
        keys = {"messageAdded": "messagesAdded", "messageDeleted": "messagesDeleted", "labelAdded": "labelsAdded", "labelRemoved": "labelsRemoved"}
        wanted = {keys[t] for t in types}
        records = []
        for record in self.records:
            if int(record["id"]) <= start:
                continue
            kept = {k: v for k, v in record.items() if k in ("id", "messages") or k in wanted}
            if set(kept) - {"id", "messages"}:
                records.append(kept)
        size = min(int(q.get("maxResults", 100)), self.history_page)
        offset = int(q["pageToken"][3:7]) if q.get("pageToken") else 0
        part = records[offset:offset + size]
        body = {"historyId": str(self.hid)}
        if part:
            body["history"] = part
        if offset + size < len(records):
            body["nextPageToken"] = f"AEp{offset + size:04d}Zq"
        return httpx.Response(200, json=body)


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("GMAIL_TOKEN", TOKEN)


def make(site, clock=lambda: NOW, **cfg):
    sleeps = []
    config = {"token_env": "GMAIL_TOKEN", "page_size": 2, "default_acl": ["group:all"], **cfg}
    connector = GmailConnector(config, transport=httpx.MockTransport(site.handler), sleep=sleeps.append, clock=clock)
    connector.sleeps = sleeps
    return connector


def run(connector, cursor=None):
    batches = list(connector.scan(cursor or {}))
    return [c for b in batches for c in b.changes], batches


def persisted(sf):
    return SourceFile(sf.external_id, sf.path, sf.name, sf.size, None, sf.version, list(sf.acl))


def kinds(changes):
    return sorted((c.kind, c.external_id) for c in changes)


def five():
    return Gmail([mail(n) for n in range(1, 6)])             # newest first: 5, 4, 3, 2, 1


# ── full listing: pagination ──


def test_messages_list_is_followed_through_next_page_token_until_it_is_absent():
    site = five()
    changes, batches = run(make(site))
    assert [c.external_id for c in changes] == [f"gmail:{mid(n)}" for n in (5, 4, 3, 2, 1)]
    lists = site.lists()
    assert [r.url.params.get("pageToken") for r in lists] == [None, "AEp0002Zq", "AEp0004Zq"]       # three pages for five messages
    assert all(r.url.params["maxResults"] == "2" for r in lists)
    assert [len(b.changes) for b in batches] == [2, 2, 1, 0]
    assert batches[-1].done and batches[-1].changes == [] and batches[-1].complete_scope == ""
    assert all(not b.done and b.complete_scope is None for b in batches[:-1])
    sf = changes[0].file
    assert sf.path == f"gmail/{EMAIL}/{mid(5)}.eml" and sf.name == f"{mid(5)}.eml" and sf.mime == "message/rfc822" and sf.version == "1"
    assert sf.acl == ["group:all"]


def test_page_size_is_capped_at_the_api_maximum_of_500():
    site = five()
    run(make(site, page_size=9999))
    assert site.lists()[0].url.params["maxResults"] == "500"


def test_an_empty_mailbox_is_an_authoritative_empty_listing():
    site = Gmail([])
    changes, batches = run(make(site))
    assert changes == [] and batches[-1].done and batches[-1].complete_scope == ""            # `messages` is absent on an empty page
    assert len(site.lists()) == 1


def test_a_page_with_no_next_token_that_follows_a_full_one_ends_the_listing():
    site = Gmail([mail(n) for n in range(1, 5)])                                              # exactly two full pages
    changes, _ = run(make(site))
    assert len(changes) == 4 and len(site.lists()) == 2


def test_a_server_that_returns_the_same_token_forever_is_an_error_not_a_loop():
    class Stuck(Gmail):
        def _list(self, q):
            response = super()._list(q)
            body = response.json()
            body["nextPageToken"] = "AEp0002Zq"
            return httpx.Response(200, json=body)

    with pytest.raises(HttpError, match="did not advance"):
        run(make(Stuck([mail(n) for n in range(1, 8)])))


def test_label_filters_spam_trash_and_search_are_sent_and_narrow_the_listing():
    msgs = [mail(1, labels=("INBOX", "Label_12")), mail(2, labels=("INBOX",)), mail(3, labels=("TRASH",)), mail(4, labels=("INBOX", "Label_12"), sender="bob@example.com")]
    site = Gmail(msgs, page=10)
    changes, _ = run(make(site, label_ids=["INBOX", "Label_12"]))
    assert site.lists()[0].url.params.get_list("labelIds") == ["INBOX", "Label_12"]
    assert "includeSpamTrash" not in site.lists()[0].url.params
    assert sorted(c.external_id for c in changes) == sorted([f"gmail:{mid(1)}", f"gmail:{mid(4)}"])
    everything, _ = run(make(Gmail(msgs, page=10), include_spam_trash=True))
    assert len(everything) == 4
    searched = Gmail(msgs, page=10)
    changes, _ = run(make(searched, q="from:bob@example.com"))
    assert searched.lists()[0].url.params["q"] == "from:bob@example.com" and [c.external_id for c in changes] == [f"gmail:{mid(4)}"]


# ── friendly paths ──


def test_friendly_paths_fetch_headers_one_request_per_message_and_name_the_file_by_date_and_subject():
    msgs = [mail(1, "Quarterly report/final: Q1?", when="2025-12-24T08:00:00+00:00"), mail(2, "=?utf-8?B?w5xiZXJzaWNodA==?=", when="2026-03-03T09:15:00+00:00"), mail(3, "")]
    site = Gmail(msgs, page=10)
    changes, _ = run(make(site, friendly_paths=True))
    by = {c.external_id: c.file for c in changes}
    one, two, three = by[f"gmail:{mid(1)}"], by[f"gmail:{mid(2)}"], by[f"gmail:{mid(3)}"]
    assert one.path == f"gmail/{EMAIL}/2025/2025-12-24 Quarterly report final Q1 [{mid(1)}].eml"      # `/ : ?` cannot appear in a path
    assert two.path == f"gmail/{EMAIL}/2026/2026-03-03 Übersicht [{mid(2)}].eml"                       # RFC 2047 subject decoded
    assert three.path == f"gmail/{EMAIL}/2026/2026-03-03 (no subject) [{mid(3)}].eml"
    assert one.modified_at == datetime(2025, 12, 24, 8, 0, tzinfo=timezone.utc) and one.size > 0 and one.name == one.path.rsplit("/", 1)[-1]
    gets = [r for r in site.requests if "/messages/" in r.url.path]
    assert len(gets) == 3 and all(r.url.params["format"] == "metadata" for r in gets)
    assert all(sorted(r.url.params.get_list("metadataHeaders")) == ["Date", "Subject"] for r in gets)    # only what is needed


def test_without_friendly_paths_a_scan_costs_one_request_per_page_and_leaves_size_empty():
    site = five()
    changes, _ = run(make(site))
    assert [r.url.path.rsplit("/", 1)[-1] for r in site.requests] == ["profile", "messages", "messages", "messages"]
    assert all(c.file.size == 0 and c.file.modified_at is None for c in changes)


def test_friendly_scan_turns_a_message_that_vanished_between_list_and_get_into_a_delete():
    site = Gmail([mail(1), mail(2)], page=10)
    original = site._get

    def flaky(message_id, q):
        if message_id == mid(2) and q.get("format") == "metadata":
            return httpx.Response(404, json=NOT_FOUND)
        return original(message_id, q)

    site._get = flaky
    changes, batches = run(make(site, friendly_paths=True))
    assert kinds(changes) == sorted([("delete", f"gmail:{mid(2)}"), ("upsert", f"gmail:{mid(1)}")])


# ── open() ──


def test_open_returns_the_decoded_rfc822_bytes_of_format_raw():
    eight_bit = b"From: a@b.c\r\nSubject: caf\xe9 \xfb\xff\xbe\r\n\r\nbody \xfb\xef\xbe>>>???\r\n"
    assert "-" in b64url(eight_bit) or "_" in b64url(eight_bit)           # the fixture really exercises the URL-safe alphabet
    original = eml("Invoice 42", "Total due: 42 EUR\r\n")
    site = Gmail([mail(1, raw=original), mail(2, raw=eight_bit)], page=10)
    connector = make(site)
    by = {c.external_id: c.file for c in run(connector)[0]}
    site.reset()
    with connector.open(persisted(by[f"gmail:{mid(1)}"])) as fh:
        data = fh.read()
    assert data == original
    import email
    parsed = email.message_from_bytes(data)
    assert parsed["Subject"] == "Invoice 42" and "Total due: 42 EUR" in parsed.get_payload()
    request = site.requests[0]
    assert request.url.path == f"/gmail/v1/users/me/messages/{mid(1)}" and request.url.params["format"] == "raw"
    with connector.open(persisted(by[f"gmail:{mid(2)}"])) as fh:
        assert fh.read() == eight_bit


@pytest.mark.parametrize("padded", [True, False])
def test_open_accepts_raw_with_or_without_base64_padding(padded):
    for tail in (b"", b"x", b"xy"):                                       # all three padding lengths
        data = b"Subject: pad\r\n\r\nbody" + tail
        site = Gmail([mail(1, raw=b64url(data, pad=padded))], page=10)
        connector = make(site)
        sf = run(connector)[0][0].file
        with connector.open(persisted(sf)) as fh:
            assert fh.read() == data


def test_open_rejects_ids_that_are_not_gmail_message_ids_without_calling_the_api():
    site = five()
    connector = make(site)
    for bad in ("gmail:../profile", "gmail:", "gmail:a b", "drive:" + mid(1), "gmail:" + mid(1) + "/attachments", "gmail:x"):
        with pytest.raises(HttpError):
            connector.open(SourceFile(bad, "p", "n", 0, None, "1", []))
    assert site.requests == []


def test_open_reports_a_message_that_is_gone_a_missing_raw_and_an_undecodable_raw():
    site = Gmail([mail(1)])
    connector = make(site)
    with pytest.raises(HttpError) as err:
        connector.open(SourceFile("gmail:" + mid(9), "p", "n", 0, None, "1", []))
    assert err.value.status == 404
    site.msgs[mid(1)]["raw"] = ""
    with pytest.raises(HttpError, match="without `raw`"):
        connector.open(SourceFile("gmail:" + mid(1), "p", "n", 0, None, "1", []))
    site.msgs[mid(1)]["raw"] = "a"                                        # one base64 character is never valid
    with pytest.raises(HttpError, match="undecodable"):
        connector.open(SourceFile("gmail:" + mid(1), "p", "n", 0, None, "1", []))


# ── permissions ──


def test_acl_rules_by_mailbox_prefix_longest_wins_and_unmatched_mailboxes_fail_closed():
    rules = [{"prefix": f"gmail/{EMAIL}", "principals": ["group:support"]}, {"prefix": "gmail/other@acme.com", "principals": ["group:other"]}]
    connector = make(five(), acl_rules=rules, default_acl=None)
    changes, _ = run(connector)
    assert all(c.file.acl == ["group:support"] for c in changes)
    assert connector.refresh_acl(persisted(changes[0].file)) == ["group:support"]
    elsewhere = make(Gmail([mail(1)], email="hr@acme.com"), acl_rules=rules, default_acl=None)
    c = run(elsewhere)[0][0]
    assert c.file.acl == [] and elsewhere.refresh_acl(persisted(c.file)) == []                       # no rule, no default: nobody
    default_only = make(Gmail([mail(1)]), default_acl=["group:ops"])
    assert run(default_only)[0][0].file.acl == ["group:ops"]


def test_friendly_paths_stay_under_the_mailbox_prefix_so_the_same_rule_applies():
    rules = [{"prefix": f"gmail/{EMAIL}", "principals": ["group:support"]}]
    connector = make(Gmail([mail(1)]), acl_rules=rules, default_acl=None, friendly_paths=True)
    assert run(connector)[0][0].file.acl == ["group:support"]


# ── incremental: users.history.list ──


def finished(site, **cfg):
    """A completed first scan: its final cursor is what the next scan starts from."""
    connector = make(site, **cfg)
    _, batches = run(connector)
    cursor = batches[-1].cursor
    assert cursor["history"] == "5000"
    return cursor


def test_incremental_scan_reads_history_after_the_saved_id_and_reports_adds_deletes_and_trash():
    site = five()
    cursor = finished(site)
    assert json.loads(json.dumps(cursor)) == cursor and TOKEN not in json.dumps(cursor)
    site.add(mail(6))
    site.delete(mid(2))
    site.label(mid(3), add=["TRASH"], remove=["INBOX"])
    site.label(mid(4), add=["STARRED"])                                    # noise: starring does not change what is indexed
    site.reset()
    changes, batches = run(make(site), cursor)
    assert kinds(changes) == sorted([("upsert", f"gmail:{mid(6)}"), ("delete", f"gmail:{mid(2)}"), ("delete", f"gmail:{mid(3)}")])
    assert site.lists() == []                                              # no listing at all
    first = site.histories()[0].url.params
    assert first["startHistoryId"] == "5000" and sorted(first.get_list("historyTypes")) == ["labelAdded", "labelRemoved", "messageAdded", "messageDeleted"]
    assert batches[-1].done and batches[-1].complete_scope is None and all(b.complete_scope is None for b in batches)
    assert batches[-1].cursor["history"] == str(site.hid) and batches[-1].cursor["last_full"] == cursor["last_full"]
    added = next(c for c in changes if c.external_id == f"gmail:{mid(6)}").file
    assert added.path == f"gmail/{EMAIL}/{mid(6)}.eml" and added.version == "1" and added.acl == ["group:all"]


def test_history_is_paged_with_page_token_and_every_page_is_applied():
    site = five()
    cursor = finished(site)
    for n in range(6, 11):
        site.add(mail(n))                                                  # five records, two per page
    site.reset()
    changes, batches = run(make(site), cursor)
    assert [r.url.params.get("pageToken") for r in site.histories()] == [None, "AEp0002Zq", "AEp0004Zq"]
    assert sorted(c.external_id for c in changes) == sorted(f"gmail:{mid(n)}" for n in range(6, 11))
    assert [len(b.changes) for b in batches] == [2, 2, 1, 0]
    assert all(r.url.params["startHistoryId"] == "5000" for r in site.histories())      # the start id stays; the token advances
    assert batches[-1].cursor["history"] == str(site.hid)


def test_an_unchanged_mailbox_yields_no_changes_and_keeps_the_history_id_current():
    site = five()
    cursor = finished(site)
    site.reset()
    changes, batches = run(make(site), cursor)                              # Gmail omits `history` when nothing happened
    assert changes == [] and batches[-1].done and batches[-1].cursor["history"] == "5000"
    assert len(site.histories()) == 1 and site.lists() == []


def test_a_message_added_and_deleted_between_scans_is_one_delete_and_is_never_fetched():
    site = five()
    cursor = finished(site)
    site.add(mail(6))
    site.delete(mid(6))
    site.reset()
    changes, _ = run(make(site), cursor)
    assert kinds(changes) == [("delete", f"gmail:{mid(6)}")]
    assert [r for r in site.requests if "/messages/" in r.url.path] == []


def test_a_message_added_and_then_purged_before_it_is_settled_becomes_a_delete():
    site = five()
    cursor = finished(site)
    site.add(mail(6))
    del site.msgs[mid(6)]                                                  # gone from the mailbox, but the history still says "added"
    site.order.remove(mid(6))
    site.reset()
    changes, _ = run(make(site), cursor)
    assert kinds(changes) == [("delete", f"gmail:{mid(6)}")]               # the 404 on messages.get is a delete, not an error


def test_label_changes_only_matter_for_spam_trash_and_configured_labels():
    site = Gmail([mail(1, labels=("INBOX", "Label_7")), mail(2, labels=("INBOX", "Label_7"))], page=10)
    cursor = finished_with(site, label_ids=["Label_7"])
    site.label(mid(1), remove=["Label_7"])                                 # leaves the indexed label: deleted
    site.add(mail(3, labels=("INBOX",)))                                   # never had it: not indexed
    site.add(mail(4, labels=("INBOX", "Label_7")))
    site.label(mid(2), add=["IMPORTANT", "CATEGORY_UPDATES"])              # irrelevant
    site.reset()
    changes, _ = run(make(site, label_ids=["Label_7"]), cursor)
    assert kinds(changes) == sorted([("delete", f"gmail:{mid(1)}"), ("delete", f"gmail:{mid(3)}"), ("upsert", f"gmail:{mid(4)}")])


def finished_with(site, **cfg):
    _, batches = run(make(site, **cfg))
    return batches[-1].cursor


def test_a_message_restored_from_trash_comes_back_as_an_upsert():
    site = five()
    cursor = finished(site)
    site.label(mid(3), add=["TRASH"])
    middle = make(site)
    changes, batches = run(middle, cursor)
    assert kinds(changes) == [("delete", f"gmail:{mid(3)}")]
    site.label(mid(3), remove=["TRASH"])
    changes, _ = run(make(site), batches[-1].cursor)
    assert kinds(changes) == [("upsert", f"gmail:{mid(3)}")]


def test_label_events_are_only_requested_when_a_label_could_matter():
    site = Gmail([mail(1)], page=10)
    cursor = finished_with(site, include_spam_trash=True)                    # spam / trash are indexed and no label filter: labels are irrelevant
    site.label(mid(1), add=["TRASH"])
    site.reset()
    changes, batches = run(make(site, include_spam_trash=True), cursor)
    assert sorted(site.histories()[0].url.params.get_list("historyTypes")) == ["messageAdded", "messageDeleted"]
    assert changes == []
    assert batches[-1].cursor["history"] == str(site.hid) != cursor["history"]       # the label record was filtered out, the mailbox's history id still moved on
    other = Gmail([mail(1)], page=10)
    cursor = finished_with(other)
    other.label(mid(1), add=["TRASH"])
    other.reset()
    changes, _ = run(make(other), cursor)                                    # default: trash is not indexed, so labels matter
    assert "labelAdded" in other.histories()[0].url.params.get_list("historyTypes")
    assert kinds(changes) == [("delete", f"gmail:{mid(1)}")]


def test_a_message_that_arrives_straight_in_spam_is_not_indexed_and_friendly_paths_apply_to_added_mail():
    site = Gmail([mail(1)], page=10)
    cursor = finished_with(site, friendly_paths=True)
    site.add(mail(2, "Weekly digest", labels=("INBOX",), when="2026-03-05T10:00:00+00:00"))
    site.add(mail(3, "Win a prize", labels=("SPAM",)))
    site.reset()
    changes, _ = run(make(site, friendly_paths=True), cursor)
    by = {c.external_id: c for c in changes}
    assert by[f"gmail:{mid(3)}"].kind == "delete"
    added = by[f"gmail:{mid(2)}"].file
    assert added.path == f"gmail/{EMAIL}/2026/2026-03-05 Weekly digest [{mid(2)}].eml" and added.size > 0
    assert added.modified_at == datetime(2026, 3, 5, 10, 0, tzinfo=timezone.utc)


def test_in_flight_changes_are_not_lost_the_history_id_is_read_before_the_listing_starts():
    site = five()

    def arrives_while_listing(s):
        s.add(mail(6))                                                     # new mail after the profile was read, before we list it

    site.on_list = arrives_while_listing
    _, batches = run(make(site))
    cursor = batches[-1].cursor
    assert cursor["history"] == "5000"                                     # the id from the profile call, not the one after the arrival
    site.reset()
    changes, _ = run(make(site), cursor)
    assert f"gmail:{mid(6)}" in {c.external_id for c in changes if c.kind == "upsert"}        # picked up by history


# ── history too old: HTTP 404 => full resync ──


def test_http_404_on_an_expired_history_id_falls_back_to_an_authoritative_full_listing():
    site = five()
    cursor = finished(site)
    site.add(mail(6))
    site.delete(mid(1))
    site.expire_history()
    site.reset()
    changes, batches = run(make(site), cursor)
    assert site.histories()[0].url.params["startHistoryId"] == "5000" and len(site.histories()) == 1
    assert sorted(c.external_id for c in changes) == sorted(f"gmail:{mid(n)}" for n in (6, 5, 4, 3, 2))        # the listing, not the history
    assert all(c.kind == "upsert" for c in changes)                          # the deleted message is simply absent...
    assert batches[-1].done and batches[-1].complete_scope == ""             # ...which, being authoritative, deletes it downstream
    assert all(b.complete_scope is None for b in batches[:-1])
    assert batches[-1].cursor["history"] == str(site.hid) and batches[-1].cursor["last_full"] == NOW
    assert [r.url.path.rsplit("/", 1)[-1] for r in site.requests][:3] == ["profile", "history", "messages"]


def test_other_history_errors_are_not_mistaken_for_an_expired_id():
    site = five()
    cursor = finished(site)
    site.reset()
    site.script_for["/history"] = [httpx.Response(400, json=google_error(400, "invalidArgument", "Invalid startHistoryId"))]
    with pytest.raises(HttpError) as err:
        run(make(site), cursor)
    assert err.value.status == 400 and site.lists() == []                 # a 400 is surfaced; only 404 means "too old"


def test_a_resumed_history_scan_whose_start_id_has_expired_also_restarts_with_a_full_listing():
    site = five()
    cursor = finished(site)
    for n in range(6, 11):
        site.add(mail(n))
    batches = list(make(site).scan(cursor))
    mid_scan = batches[0].cursor
    assert mid_scan["mode"] == "inc" and mid_scan["pos"]["phase"] == "history" and mid_scan["pos"]["start"] == "5000" and mid_scan["pos"]["token"]
    site.expire_history()
    site.reset()
    changes, resumed = run(make(site), mid_scan)
    assert len(changes) == 10 and resumed[-1].complete_scope == ""
    assert site.histories()[0].url.params["pageToken"] == mid_scan["pos"]["token"]


def test_q_cannot_be_applied_to_history_so_every_scan_is_a_full_listing():
    site = five()
    cursor = finished_with(site, q="from:ada@example.com")
    assert cursor["history"] == "5000"
    site.reset()
    _, batches = run(make(site, q="from:ada@example.com"), cursor)
    assert site.histories() == [] and len(site.lists()) == 3 and batches[-1].complete_scope == ""


def test_a_cursor_older_than_full_resync_hours_triggers_a_full_listing():
    site = five()
    cursor = finished(site)
    site.reset()
    _, batches = run(make(site, clock=lambda: NOW + 30 * 3600), cursor)
    assert site.histories() == [] and batches[-1].complete_scope == ""
    site.reset()
    _, batches = run(make(site, clock=lambda: NOW + 3600), cursor)
    assert len(site.histories()) == 1 and batches[-1].complete_scope is None


def test_a_cursor_without_a_history_id_cannot_be_incremental():
    site = five()
    _, batches = run(make(site), {"since": "2026-03-10T12:00:00Z", "last_full": NOW - 60})
    assert site.histories() == [] and len(site.lists()) == 3
    assert batches[-1].complete_scope == "" and batches[-1].cursor["history"] == "5000"


def test_a_resumed_history_scan_continues_at_its_page_token_and_finishes_with_the_original_history_id():
    site = five()
    cursor = finished(site)
    for n in range(6, 11):
        site.add(mail(n))
    first = next(iter(make(site).scan(cursor)))
    assert first.cursor["pos"]["token"] == "AEp0002Zq" and first.cursor["pos"]["start"] == "5000"
    site.reset()
    changes, resumed = run(make(site), first.cursor)
    assert [r.url.params.get("pageToken") for r in site.histories()] == ["AEp0002Zq", "AEp0004Zq"]       # page one is not read again
    assert sorted(c.external_id for c in changes) == sorted(f"gmail:{mid(n)}" for n in (8, 9, 10))        # records 3-5; 1-2 were in the first batch
    assert resumed[-1].done and resumed[-1].complete_scope is None
    assert resumed[-1].cursor["history"] == "5005"
    assert resumed[-1].cursor["since"] and resumed[-1].cursor["last_full"] == cursor["last_full"]


def test_history_responses_without_a_history_id_fall_back_to_the_newest_record_id():
    site = five()                                                          # UNSURE whether Gmail always sends `historyId` on history pages
    cursor = finished(site)
    for n in range(6, 10):
        site.add(mail(n))
    original = site._history

    def without_id(q):
        response = original(q)
        body = response.json()
        body.pop("historyId")
        return httpx.Response(200, json=body)

    site._history = without_id
    changes, batches = run(make(site), cursor)
    assert len(changes) == 4 and batches[-1].cursor["history"] == "5004"   # the id of the newest record seen, never an older one
    quiet = five()
    cursor = finished(quiet)
    quiet._history = lambda q: httpx.Response(200, json={})
    _, batches = run(make(quiet), cursor)
    assert batches[-1].cursor["history"] == "5000"                         # nothing to learn from: keep the old id (re-reading is safe)


# ── resume of an interrupted full listing ──


def test_resume_continues_at_the_saved_page_token_is_not_authoritative_and_keeps_the_original_history_id():
    site = five()
    batches = list(make(site).scan({}))
    first = batches[0]
    assert first.cursor["mode"] == "full" and first.cursor["pos"]["token"] == "AEp0002Zq" and first.cursor["pos"]["hid"] == "5000"
    assert json.loads(json.dumps(first.cursor)) == first.cursor and TOKEN not in json.dumps(first.cursor)
    site.hid += 7                                                          # the mailbox moves on (read / star events) between the crash and the resume
    site.reset()
    changes, resumed = run(make(site), first.cursor)
    assert [r.url.params.get("pageToken") for r in site.lists()] == ["AEp0002Zq", "AEp0004Zq"]       # page one is not read again
    assert [c.external_id for c in changes] == [f"gmail:{mid(n)}" for n in (3, 2, 1)] and all(c.kind == "upsert" for c in changes)
    assert resumed[-1].done and resumed[-1].complete_scope is None          # a resumed listing never deletes anything
    assert resumed[-1].cursor["history"] == "5000"                           # the id read before the interrupted listing began, not the current "5007"
    assert "last_full" in resumed[-1].cursor


def test_a_cursor_saved_after_the_last_page_goes_straight_to_the_end_without_deleting_or_listing_again():
    site = five()
    batches = list(make(site).scan({}))
    last_page = batches[-2]
    assert last_page.cursor["pos"]["phase"] == "done"
    site.reset()
    changes, resumed = run(make(site), last_page.cursor)
    assert changes == [] and site.lists() == [] and site.histories() == []
    assert resumed[-1].done and resumed[-1].complete_scope is None
    assert resumed[-1].cursor["history"] == "5000" and resumed[-1].cursor["since"]


def test_the_same_holds_for_an_incremental_scan_that_stopped_after_its_last_history_page():
    site = five()
    cursor = finished(site)
    site.add(mail(6))
    batches = list(make(site).scan(cursor))
    assert batches[-2].cursor["pos"]["phase"] == "done"
    site.reset()
    changes, resumed = run(make(site), batches[-2].cursor)
    assert changes == [] and site.lists() == [] and site.histories() == []
    assert resumed[-1].done and resumed[-1].cursor["history"] == "5001" and resumed[-1].complete_scope is None


# ── limits ──


def test_max_items_truncates_without_deleting_and_does_not_advance_the_history_id():
    site = five()
    changes, batches = run(make(site, max_items=3))
    assert len(changes) == 3 and batches[-1].done and batches[-1].complete_scope is None
    assert "history" not in batches[-1].cursor and "since" not in batches[-1].cursor                  # the next scan is a full one again
    site.reset()
    _, batches = run(make(site, max_pages=2))
    assert len(site.requests) == 2 and batches[-1].complete_scope is None and "history" not in batches[-1].cursor


def test_resuming_from_a_batch_that_was_cut_short_re_reads_that_page_instead_of_skipping_its_remainder():
    site = five()
    batches = list(make(site, max_items=3).scan({}))
    cut = batches[1]                                                       # page two held mail 3 and 2; only 3 fit in the allowance
    assert [c.external_id for c in cut.changes] == [f"gmail:{mid(3)}"]
    assert cut.cursor["pos"]["token"] == "AEp0002Zq"                         # points at the page that was cut, not at the one after it
    site.reset()
    changes, resumed = run(make(site), cut.cursor)
    assert [c.external_id for c in changes] == [f"gmail:{mid(n)}" for n in (3, 2, 1)] and resumed[-1].complete_scope is None


def test_a_cut_short_history_page_is_read_again_on_resume():
    site = five()
    cursor = finished(site)
    for n in range(6, 10):
        site.add(mail(n))
    batches = list(make(site, max_items=3).scan(cursor))
    cut = batches[1]
    assert [c.external_id for c in cut.changes] == [f"gmail:{mid(8)}"]
    site.reset()
    changes, resumed = run(make(site), cut.cursor)
    assert sorted(c.external_id for c in changes) == sorted(f"gmail:{mid(n)}" for n in (8, 9))
    assert resumed[-1].cursor["history"] == "5004"


def test_a_truncated_incremental_scan_keeps_the_old_history_id_so_nothing_is_skipped():
    site = five()
    cursor = finished(site)
    for n in range(6, 10):
        site.add(mail(n))
    changes, batches = run(make(site, max_items=2), cursor)
    assert len(changes) == 2 and batches[-1].complete_scope is None
    assert batches[-1].cursor["history"] == "5000" and batches[-1].cursor["since"] == cursor["since"]
    again, _ = run(make(site), batches[-1].cursor)
    assert len(again) == 4                                                   # the next scan re-reads from the old id and gets all four


# ── users and configuration ──


def test_a_named_mailbox_is_addressed_in_the_path_and_invalid_users_are_rejected():
    site = Gmail([mail(1)], email="shared@acme.com")
    changes, _ = run(make(site, user="shared@acme.com"))
    assert {r.url.path.split("/")[4] for r in site.requests} == {"shared@acme.com"}
    assert changes[0].file.path.startswith("gmail/shared@acme.com/")
    for bad in ("../me", "a b", "me/../x", ""):
        with pytest.raises(ValueError):
            GmailConnector({"token_env": "GMAIL_TOKEN", "user": bad})


# ── http behaviour ──


def test_429_is_retried_honouring_retry_after():
    site = five()
    site.script = [httpx.Response(429, headers={"Retry-After": "7"}, json=google_error(429, "rateLimitExceeded", "Too many requests", "RESOURCE_EXHAUSTED"))]
    connector = make(site)
    changes, _ = run(connector)
    assert len(changes) == 5 and connector.sleeps == [7.0]


@pytest.mark.parametrize("reason", ["rateLimitExceeded", "userRateLimitExceeded"])
def test_google_throttling_sent_as_403_is_waited_out_not_reported_as_bad_credentials(reason):
    site = five()                                                            # UNSURE which 403 reasons Gmail uses; these are the documented Google ones
    site.script = [httpx.Response(403, json=google_error(403, reason, "User-rate limit exceeded. Retry after 2026-03-10T12:00:05.000Z"))]
    connector = make(site)
    changes, _ = run(connector)
    assert len(changes) == 5 and connector.sleeps == [4.0]


def test_a_403_for_missing_permission_is_an_auth_error_and_is_not_retried():
    site = five()
    site.script = [httpx.Response(403, json=google_error(403, "insufficientPermissions", "Insufficient Permission", "PERMISSION_DENIED"))]
    connector = make(site)
    with pytest.raises(AuthError) as err:
        run(connector)
    assert err.value.status == 403 and connector.sleeps == [] and len(site.requests) == 1


def test_401_is_a_clear_error_that_names_the_variable_and_the_scope_but_never_the_token(monkeypatch):
    monkeypatch.setenv("GMAIL_TOKEN", "ya29.WRONG-VALUE-1234")
    site = five()
    connector = make(site)
    with pytest.raises(AuthError) as err:
        run(connector)
    message = str(err.value)
    assert "401" in message and "GMAIL_TOKEN" in message and "gmail.readonly" in message
    assert "ya29.WRONG-VALUE-1234" not in message and TOKEN not in message
    assert len(site.requests) == 1 and connector.sleeps == []


def test_a_server_that_echoes_the_credential_back_does_not_get_it_into_the_error(monkeypatch):
    monkeypatch.setenv("GMAIL_TOKEN", "ya29.ECHOED-SECRET-4321")
    site = five()
    site.script = [httpx.Response(401, json=google_error(401, "authError", "Invalid Credentials: Bearer ya29.ECHOED-SECRET-4321", "UNAUTHENTICATED"))]
    with pytest.raises(AuthError) as err:
        run(make(site))
    assert "Invalid Credentials" in str(err.value) and "ya29.ECHOED-SECRET-4321" not in str(err.value)
    site = five()
    site.script = [httpx.Response(400, json=google_error(400, "invalidArgument", "bad Bearer ya29.ECHOED-SECRET-4321"))]
    with pytest.raises(HttpError) as err:
        run(make(site))
    assert "ya29.ECHOED-SECRET-4321" not in str(err.value)


def test_the_token_is_read_at_every_request_so_a_refreshed_one_is_picked_up(monkeypatch):
    site = five()
    connector = make(site)
    batches = connector.scan({})
    next(batches)                                                            # first page done with the first token
    monkeypatch.setenv("GMAIL_TOKEN", "ya29.ROTATED-TOKEN-0000")
    with pytest.raises(AuthError):                                          # the fake only knows TOKEN: proves the new value was sent
        list(batches)
    assert site.requests[-1].headers["authorization"] == "Bearer ya29.ROTATED-TOKEN-0000"


def test_an_unset_variable_is_named_and_nothing_is_sent(monkeypatch):
    monkeypatch.delenv("GMAIL_TOKEN")
    site = five()
    with pytest.raises(HttpError, match="GMAIL_TOKEN"):
        run(make(site))
    assert site.requests == []
    with pytest.raises(HttpError, match="no environment variable name"):
        run(make(site, token_env=""))


def test_cursors_never_contain_the_secret():
    site = five()
    _, batches = run(make(site, friendly_paths=True))
    assert TOKEN not in json.dumps([b.cursor for b in batches])
