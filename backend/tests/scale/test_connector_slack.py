"""Slack connector against a fake Web API shaped like the Slack documentation: HTTP 200 with {"ok": ...},
cursor pagination in response_metadata.next_cursor, conversations.history newest-first with has_more.

The fake reproduces one Slack behaviour that matters: `inclusive=true` applies to BOTH `oldest` and `latest`.
Not certain of (written from memory of the docs): exact `bot_profile` / `files` fields beyond the ones used.
"""

import calendar
import copy
import json
from datetime import datetime, timezone

import httpx
import pytest

from app.scale.connectors._http import AuthError, HttpError
from app.scale.connectors.base import SourceFile
from app.scale.connectors.slack import SlackConnector, slack_text

TOKEN = "xoxb-1234-SECRET-5678"
NOW = datetime(2026, 3, 10, 12, 0, tzinfo=timezone.utc).timestamp()


def ts(day, hhmm="09:00", n=1):
    y, m, d = map(int, day.split("-"))
    hh, mm = map(int, hhmm.split(":"))
    return f"{calendar.timegm((y, m, d, hh, mm, 0))}.{n:06d}"


def msg(day, hhmm, user, text, n=1, **extra):
    return {"type": "message", "user": user, "text": text, "ts": ts(day, hhmm, n), "team": "T1", **extra}


def chan(cid, name, **extra):
    return {"id": cid, "name": name, "is_channel": True, "is_group": False, "is_im": False, "is_archived": False,
            "is_private": False, "is_member": True, "num_members": 5, **extra}


USERS = [
    {"id": "U1", "name": "ada", "real_name": "Ada Lovelace", "deleted": False, "is_bot": False,
     "profile": {"display_name": "ada", "real_name": "Ada Lovelace"}},
    {"id": "U2", "name": "grace", "real_name": "Grace Hopper", "deleted": False, "is_bot": False,
     "profile": {"display_name": "", "real_name": "Grace Hopper"}},
]


class Slack:
    def __init__(self, channels, history, *, replies=None, users=USERS, page=2, users_error=None, honor_archived_filter=True):
        self.channels = copy.deepcopy(channels)
        self.history = copy.deepcopy(history)
        self.replies = copy.deepcopy(replies or {})
        self.users = users
        self.page = page
        self.users_error = users_error
        self.honor_archived_filter = honor_archived_filter
        self.not_member: set[str] = set()
        self.requests: list[httpx.Request] = []
        self.script: list[httpx.Response] = []

    def reset(self):
        self.requests.clear()

    def calls(self, method):
        return [r for r in self.requests if r.url.path == f"/api/{method}"]

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.script:
            return self.script.pop(0)
        if request.headers.get("authorization") != f"Bearer {TOKEN}":
            return httpx.Response(200, json={"ok": False, "error": "invalid_auth"})
        method = request.url.path.rsplit("/", 1)[-1]
        q = dict(request.url.params)
        assert "token" not in q, "the token must travel in the Authorization header, never in the URL"
        if method == "conversations.list":
            items = [c for c in self.channels if not (self.honor_archived_filter and q.get("exclude_archived") == "true" and c["is_archived"])]
            return self._page(items, q, "channels", size=int(q["limit"]) if self.page is None else self.page)
        if method == "users.list":
            if self.users_error:
                return httpx.Response(200, json={"ok": False, "error": self.users_error, "needed": "users:read", "provided": "channels:history"})
            return self._page(self.users, q, "members", size=1)
        cid = q.get("channel")
        if cid not in self.history:
            return httpx.Response(200, json={"ok": False, "error": "channel_not_found"})
        if cid in self.not_member:
            return httpx.Response(200, json={"ok": False, "error": "not_in_channel"})
        if method == "conversations.history":
            inclusive = q.get("inclusive") == "true"
            items = sorted(self.history[cid], key=lambda m: float(m["ts"]), reverse=True)
            if "oldest" in q:
                items = [m for m in items if float(m["ts"]) > float(q["oldest"]) or (inclusive and float(m["ts"]) == float(q["oldest"]))]
            if "latest" in q:
                items = [m for m in items if float(m["ts"]) < float(q["latest"]) or (inclusive and float(m["ts"]) == float(q["latest"]))]
            return self._page(items, q, "messages", size=min(int(q["limit"]), self.page or 999), history=True)
        if method == "conversations.replies":
            thread = self.replies.get((cid, q["ts"]))
            if thread is None:
                return httpx.Response(200, json={"ok": False, "error": "thread_not_found"})
            parent = next(m for m in self.history[cid] if m["ts"] == q["ts"])
            return self._page([parent] + thread, q, "messages", size=2, history=True)
        return httpx.Response(404)

    @staticmethod
    def _page(items, q, key, size, history=False):
        offset = int(q["cursor"][3:]) if q.get("cursor", "").startswith("cur") else 0
        part = items[offset:offset + size]
        more = offset + size < len(items)
        body = {"ok": True, key: part}
        if history:
            body.update(has_more=more, pin_count=0)
        if more:
            body["response_metadata"] = {"next_cursor": f"cur{offset + size}"}
        elif not history:
            body["response_metadata"] = {"next_cursor": ""}
        return httpx.Response(200, json=body)


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("SLACK_TOKEN", TOKEN)


def make(site, clock=lambda: NOW, **cfg):
    sleeps = []
    config = {"token_env": "SLACK_TOKEN", "page_size": 2, "default_acl": ["group:all"], **cfg}
    connector = SlackConnector(config, transport=httpx.MockTransport(site.handler), sleep=sleeps.append, clock=clock)
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


GENERAL = [
    msg("2026-03-02", "09:15", "U1", "Morning <@U2>, deploy is at <#C02|ops> today", 1),
    msg("2026-03-02", "09:20", "U2", "Ack &amp; thanks", 1),
    msg("2026-03-02", "17:45", "U1", "See <https://wiki.acme.io/deploy|the runbook>", 1),
    msg("2026-03-03", "10:00", "U2", "Second day, first message", 1),
    msg("2026-03-03", "10:05", "U1", "Second day, edited", 1, edited={"user": "U1", "ts": ts("2026-03-03", "10:06")}),
    msg("2026-03-04", "08:00", "U1", "Thread starter", 1, thread_ts=ts("2026-03-04", "08:00", 1), reply_count=2, reply_users_count=2),
    msg("2026-03-04", "08:30", "U2", "joined", 1, subtype="channel_join"),
    # "also send to #general": Slack lists this reply in the channel history too; it must not become a day of its own
    msg("2026-03-05", "08:00", "U1", "late reply next day", 1, subtype="thread_broadcast", thread_ts=ts("2026-03-04", "08:00", 1)),
]
THREAD = {("C01", ts("2026-03-04", "08:00", 1)): [
    msg("2026-03-04", "08:10", "U2", "first reply", 1, thread_ts=ts("2026-03-04", "08:00", 1)),
    msg("2026-03-05", "08:00", "U1", "late reply next day", 1, thread_ts=ts("2026-03-04", "08:00", 1)),
]}
CHANNELS = [chan("C01", "general"), chan("C02", "ops")]


def standard(**kw):
    return Slack(CHANNELS, {"C01": GENERAL, "C02": [msg("2026-03-03", "11:00", "U1", "ops message")]}, replies=THREAD, **kw)


# ── pagination and grouping ──


def test_channels_and_history_are_paged_by_cursor_and_days_are_grouped_across_page_boundaries():
    site = standard()
    changes, batches = run(make(site))
    ids = [c.external_id for c in changes]
    assert ids == ["slack:C01:2026-03-04", "slack:C01:2026-03-03", "slack:C01:2026-03-02", "slack:C02:2026-03-03"]   # newest first per channel
    assert len(site.calls("conversations.list")) == 1
    history = [r for r in site.calls("conversations.history") if r.url.params["channel"] == "C01"]
    assert [r.url.params.get("cursor") for r in history] == [None, "cur2", "cur4", "cur6"]   # 7 messages, 2 per page: the server cursors were followed
    assert all(r.url.params["limit"] == "2" for r in history)
    day2 = next(c for c in changes if c.external_id == "slack:C01:2026-03-02").file
    assert day2.path == "slack/general/2026-03-02.md" and day2.name == "general-2026-03-02.md" and day2.mime == "text/markdown"
    assert batches[-1].done and batches[-1].changes == [] and batches[-1].complete_scope == ""
    assert all(b.complete_scope is None and not b.done for b in batches[:-1])


def test_conversations_list_is_followed_through_its_cursor():
    many = [chan(f"C{n:02d}", f"chan{n}") for n in range(1, 6)]
    site = Slack(many, {c["id"]: [msg("2026-03-02", "09:00", "U1", f"hello {c['name']}")] for c in many}, page=2)
    changes, _ = run(make(site))
    assert [r.url.params.get("cursor") for r in site.calls("conversations.list")] == [None, "cur2", "cur4"]
    assert len(changes) == 5
    first = site.calls("conversations.list")[0].url.params
    assert first["types"] == "public_channel,private_channel" and first["exclude_archived"] == "true"


def test_channel_filters_by_name_id_and_glob_and_archived_channels_are_ignored():
    chans = [chan("C01", "general"), chan("C02", "eng-backend"), chan("C03", "eng-frontend"), chan("C04", "old", is_archived=True), chan("C05", "random")]
    site = Slack(chans, {c["id"]: [msg("2026-03-02", "09:00", "U1", "x")] for c in chans})
    names = lambda **cfg: sorted(c.file.path.split("/")[1] for c in run(make(site, **cfg))[0])          # noqa: E731
    assert names() == ["eng-backend", "eng-frontend", "general", "random"]
    assert names(channels=["#general", "eng-*"]) == ["eng-backend", "eng-frontend", "general"]
    assert names(channels=["C05"]) == ["random"]
    assert names(exclude_channels=["eng-*", "random"]) == ["general"]
    lax = Slack(chans, site.history, honor_archived_filter=False)                       # a server that ignores exclude_archived
    assert "old" not in [c.file.path.split("/")[1] for c in run(make(lax))[0]]


# ── rendering ──


def test_a_day_file_has_names_mentions_links_entities_threads_and_skips_joins():
    site = standard()
    connector = make(site)
    changes, _ = run(connector)
    by = {c.external_id: c.file for c in changes}
    text = read(make(Slack(CHANNELS, site.history, replies=THREAD)), by["slack:C01:2026-03-02"])
    assert text.startswith("# #general 2026-03-02\n")
    assert "channel: #general" in text and "date: 2026-03-02 (UTC)" in text and "messages: 3" in text
    assert "[09:15] Ada Lovelace: Morning @Grace Hopper, deploy is at #ops today" in text       # <@U2> and <#C02|ops> resolved
    assert "[09:20] Grace Hopper: Ack & thanks" in text
    assert "[17:45] Ada Lovelace: See [the runbook](https://wiki.acme.io/deploy)" in text
    thread_day = read(make(Slack(CHANNELS, site.history, replies=THREAD)), by["slack:C01:2026-03-04"])
    assert "[08:00] Ada Lovelace: Thread starter" in thread_day
    assert "    ↳ [08:10] Grace Hopper: first reply" in thread_day
    assert "    ↳ [08:00] Ada Lovelace: late reply next day" in thread_day                       # a reply posted on 03-05 stays with its parent's day
    assert "joined" not in thread_day and "messages: 3" in thread_day
    edited = read(make(Slack(CHANNELS, site.history, replies=THREAD)), by["slack:C01:2026-03-03"])
    assert "Second day, edited (edited)" in edited
    assert "slack:C01:2026-03-05" not in by                                                      # the reply did not become a day of its own


def test_users_are_listed_once_with_cursor_paging_and_cached():
    site = standard()
    connector = make(site)
    run(connector)
    assert [r.url.params.get("cursor") for r in site.calls("users.list")] == [None, "cur1"]       # two users, one per page
    run(connector)
    assert len(site.calls("users.list")) == 2                                                    # cached for the life of the connector


def test_without_the_users_read_scope_names_stay_as_ids_and_the_scan_still_works():
    site = standard(users_error="missing_scope")
    connector = make(site)
    changes, _ = run(connector)
    assert len(changes) == 4
    assert "[09:15] U1: Morning @U2" in read(connector, next(c.file for c in changes if c.external_id == "slack:C01:2026-03-02"))
    off = standard()
    run(make(off, resolve_users=False))
    assert off.calls("users.list") == []


def test_bots_files_and_legacy_attachments_are_rendered():
    history = {"C01": [
        msg("2026-03-02", "09:00", None, "build finished", 1, subtype="bot_message", username="ci-bot", bot_id="B1"),
        msg("2026-03-02", "09:05", "U1", "", 1, files=[{"id": "F1", "name": "report.pdf", "title": "Report"}]),
        msg("2026-03-02", "09:06", "U1", "look", 2, attachments=[{"fallback": "fb", "text": "attached text <https://x.io|link>"}]),
    ]}
    site = Slack([chan("C01", "general")], history)
    connector = make(site)
    text = read(connector, run(connector)[0][0].file)
    assert "[09:00] ci-bot: build finished" in text
    assert "[09:05] Ada Lovelace: [file: report.pdf]" in text
    assert "[09:06] Ada Lovelace: look\n" in text and "attached text [link](https://x.io)" in text


def test_slack_markup_conversion():
    users, channels = {"U1": "Ada"}, {"C1": "general"}
    assert slack_text("hi <@U1> <@U9|bob> in <#C1> and <#C2|ops>", users, channels) == "hi @Ada @bob in #general and #ops"
    assert slack_text("<!here> <!channel|channel> <!subteam^S1|@oncall> <!date^1392734382^{date_short}|Feb 18>", users, channels) == "@here @channel @oncall Feb 18"
    assert slack_text("<https://x.io|site> <https://y.io> <mailto:a@b.c|a@b.c> a &amp; b &lt;c&gt;", users, channels) == "[site](https://x.io) https://y.io a@b.c a & b <c>"
    assert slack_text("", users, channels) == "" and slack_text("<not a token", users, channels) == "<not a token"


# ── versions ──


def test_version_is_stable_and_changes_with_edits_new_replies_and_deletions():
    def versions(site):
        connector = make(site)
        return {c.external_id: c.file.version for c in run(connector)[0]}

    base = versions(standard())
    assert versions(standard()) == base
    edited = standard()
    edited.history["C01"][1]["text"] = "Ack &amp; thanks, edited"
    after = versions(edited)
    assert after["slack:C01:2026-03-02"] != base["slack:C01:2026-03-02"] and after["slack:C01:2026-03-03"] == base["slack:C01:2026-03-03"]
    replied = standard()
    replied.replies[("C01", ts("2026-03-04", "08:00", 1))].append(msg("2026-03-06", "10:00", "U2", "one more", 1, thread_ts=ts("2026-03-04", "08:00", 1)))
    assert versions(replied)["slack:C01:2026-03-04"] != base["slack:C01:2026-03-04"]
    deleted = standard()
    deleted.history["C01"] = [m for m in deleted.history["C01"] if m["text"] != "Ack &amp; thanks"]
    assert versions(deleted)["slack:C01:2026-03-02"] != base["slack:C01:2026-03-02"]


# ── resume ──


def test_resume_in_the_middle_of_a_channel_uses_the_day_bound_and_does_not_repeat_days():
    site = standard()
    batches = list(make(site, batch_size=1).scan({}))
    first = batches[0]
    assert [c.external_id for c in first.changes] == ["slack:C01:2026-03-04"]
    assert first.cursor["pos"]["ch"] == "C01" and float(first.cursor["pos"]["latest"]) == float(calendar.timegm((2026, 3, 4, 0, 0, 0)))
    assert json.loads(json.dumps(first.cursor)) == first.cursor
    site.reset()
    changes, resumed = run(make(site, batch_size=1), first.cursor)
    assert [c.external_id for c in changes] == ["slack:C01:2026-03-03", "slack:C01:2026-03-02", "slack:C02:2026-03-03"]
    assert site.calls("conversations.history")[0].url.params["latest"] == first.cursor["pos"]["latest"]
    assert resumed[-1].done and resumed[-1].complete_scope is None


def test_resume_between_channels_skips_finished_ones():
    site = standard()
    batches = list(make(site).scan({}))
    boundary = next(b for b in batches if b.cursor.get("pos", {}).get("ch") == "C02")
    site.reset()
    changes, _ = run(make(site), boundary.cursor)
    assert [c.external_id for c in changes] == ["slack:C02:2026-03-03"]
    assert {r.url.params["channel"] for r in site.calls("conversations.history")} == {"C02"}


def test_a_message_exactly_on_midnight_is_not_double_counted_by_an_inclusive_resume():
    history = {"C01": [msg("2026-03-03", "00:00", "U1", "midnight sharp", 0), msg("2026-03-02", "23:59", "U1", "last minute", 1)]}
    site = Slack([chan("C01", "general")], history, page=10)
    batches = list(make(site, batch_size=1).scan({}))
    assert [c.external_id for c in batches[0].changes] == ["slack:C01:2026-03-03"]
    changes, _ = run(make(site, batch_size=1), batches[0].cursor)
    assert [c.external_id for c in changes] == ["slack:C01:2026-03-02"]          # the 00:00:00 message still belongs to 03-03 and is not re-emitted
    with make(site).open(SourceFile("slack:C01:2026-03-03", "slack/general/2026-03-03.md", "n", 0, None, "v", [])) as fh:
        day = fh.read().decode()
    assert "midnight sharp" in day and "last minute" not in day


def test_resumed_incremental_scan_with_both_bounds_neither_repeats_a_day_nor_deletes_one():
    history = {"C01": [msg("2026-03-03", "00:00", "U1", "midnight sharp", 0), msg("2026-03-02", "23:59", "U1", "last minute", 1)]}
    site = Slack([chan("C01", "general")], history, page=10)
    cursor = {"mode": "inc", "since": "2026-03-05T00:00:00Z", "last_full": NOW - 60, "high": None,
              "pos": {"ch": "C01", "latest": f"{calendar.timegm((2026, 3, 3, 0, 0, 0)):.6f}"}}      # 03-03 was emitted before the crash
    changes, batches = run(make(site), cursor)
    params = site.calls("conversations.history")[0].url.params
    assert params["inclusive"] == "true" and "oldest" in params and "latest" in params       # Slack applies `inclusive` to both
    assert [(c.kind, c.external_id) for c in changes] == [("upsert", "slack:C01:2026-03-02")]  # no repeat of 03-03, and no delete of it
    assert batches[-1].complete_scope is None


def test_a_cursor_saved_after_the_last_channel_goes_straight_to_the_end():
    site = standard()
    batches = list(make(site).scan({}))
    site.reset()
    changes, resumed = run(make(site), batches[-2].cursor)
    assert changes == [] and site.calls("conversations.history") == [] and resumed[-1].done


# ── incremental ──


def test_next_scan_rereads_a_window_and_deletes_days_that_are_empty():
    site = standard()
    _, batches = run(make(site))
    final = batches[-1].cursor
    assert final["since"] == "2026-03-04T08:00:00Z"                  # newest top-level message seen
    json.dumps(final)

    del site.history["C01"][3:5]                                   # every message of 03-03 is deleted in Slack
    site.history["C01"].append(msg("2026-03-09", "14:00", "U2", "new message after the watermark"))
    site.reset()
    changes, inc = run(make(site, rescan_days=1), final)
    by = {(c.kind, c.external_id) for c in changes}
    assert ("upsert", "slack:C01:2026-03-09") in by and ("upsert", "slack:C01:2026-03-04") in by and ("upsert", "slack:C02:2026-03-03") in by
    assert ("delete", "slack:C01:2026-03-03") in by                # the window covers 03-03 and nothing is left of it
    assert ("delete", "slack:C01:2026-03-08") in by                # quiet days in the window are reported (idempotent)
    assert ("delete", "slack:C01:2026-03-10") in by                # ... up to and including today
    assert not any(e == "slack:C01:2026-03-02" for _, e in by)     # 03-02 is older than the window: neither re-read nor deleted
    oldest = float(site.calls("conversations.history")[0].url.params["oldest"])
    assert oldest == calendar.timegm((2026, 3, 3, 0, 0, 0))         # newest message seen (03-04) minus 1 day, at midnight
    assert site.calls("conversations.history")[0].url.params["inclusive"] == "true"
    assert inc[-1].complete_scope is None and inc[-1].cursor["last_full"] == final["last_full"]


def test_incremental_window_start_is_exactly_rescan_days_before_the_watermark_at_midnight():
    site = standard()
    state = {"since": "2026-03-05T08:00:00Z", "last_full": NOW - 60}
    run(make(site, rescan_days=3), state)
    assert float(site.calls("conversations.history")[0].url.params["oldest"]) == calendar.timegm((2026, 3, 2, 0, 0, 0))


def test_config_since_limits_the_history_that_is_read():
    site = standard()
    changes, batches = run(make(site, since="2026-03-03"))
    assert float(site.calls("conversations.history")[0].url.params["oldest"]) == calendar.timegm((2026, 3, 3, 0, 0, 0))
    assert sorted(c.external_id for c in changes if c.kind == "upsert") == ["slack:C01:2026-03-03", "slack:C01:2026-03-04", "slack:C02:2026-03-03"]
    assert all(c.kind == "upsert" for c in changes) and batches[-1].complete_scope == ""


def test_full_resync_is_authoritative_and_resumed_partial_scans_are_not():
    site = standard()
    _, batches = run(make(site), {"since": "2026-03-05T00:00:00Z", "last_full": NOW - 40 * 3600})
    assert batches[-1].complete_scope == "" and "oldest" not in site.calls("conversations.history")[0].url.params


def test_max_items_and_max_pages_truncate_without_deleting():
    site = standard()
    changes, batches = run(make(site, max_items=2))
    assert len([c for c in changes if c.kind == "upsert"]) == 2 and batches[-1].complete_scope is None and "since" not in batches[-1].cursor
    site.reset()
    _, batches = run(make(site, max_pages=2))
    assert len(site.calls("conversations.list")) == 1 and len(site.calls("conversations.history")) == 1   # the page budget covers list + history
    assert batches[-1].complete_scope is None


# ── errors and rate limits ──


def test_invalid_auth_is_an_auth_error_that_does_not_show_the_token(monkeypatch):
    monkeypatch.setenv("SLACK_TOKEN", "xoxb-wrong-token-value")
    site = standard()
    connector = make(site)
    with pytest.raises(AuthError) as err:
        run(connector)
    assert "invalid_auth" in str(err.value) and "SLACK_TOKEN" in str(err.value)
    assert "xoxb-wrong-token-value" not in str(err.value) and TOKEN not in str(err.value)
    assert len(site.requests) == 1 and connector.sleeps == []


def test_missing_scope_names_the_scope_that_is_needed():
    site = standard()
    site.script = [httpx.Response(200, json={"ok": False, "error": "missing_scope", "needed": "groups:history", "provided": "channels:history"})]
    with pytest.raises(AuthError, match="groups:history"):
        run(make(site))


def test_rate_limit_as_http_429_and_as_ok_false_are_both_waited_out():
    site = standard()
    site.script = [httpx.Response(429, headers={"Retry-After": "4"}, json={"ok": False, "error": "ratelimited"}),
                   httpx.Response(200, headers={"Retry-After": "9"}, json={"ok": False, "error": "ratelimited"})]
    connector = make(site)
    changes, _ = run(connector)
    assert len(changes) == 4 and connector.sleeps == [4.0, 9.0]


def test_a_channel_the_bot_is_not_in_is_skipped_but_other_channels_are_scanned():
    site = standard()
    site.not_member.add("C01")
    changes, batches = run(make(site))
    assert [c.external_id for c in changes] == ["slack:C02:2026-03-03"]
    assert batches[-1].complete_scope == ""              # C01 is treated as gone: its indexed days are deleted by the sync layer


def test_a_cursor_that_never_advances_is_an_error_not_an_endless_loop():
    class Stuck(Slack):
        def __init__(self, method, *a, **kw):
            super().__init__(*a, **kw)
            self.method = method

        def handler(self, request):
            response = super().handler(request)
            if request.url.path.endswith("/" + self.method):
                body = response.json()
                body["response_metadata"] = {"next_cursor": "same"}
                body["has_more"] = True
                return httpx.Response(200, json=body)
            return response

    for method in ("users.list", "conversations.history", "conversations.list", "conversations.replies"):
        site = Stuck(method, CHANNELS, {"C01": GENERAL, "C02": []}, replies=THREAD, page=10)
        with pytest.raises(HttpError, match="did not advance"):
            run(make(site))


def test_unexpected_api_errors_are_raised_not_swallowed():
    site = standard()
    site.script = [httpx.Response(200, json={"ok": False, "error": "internal_error"})]
    with pytest.raises(HttpError, match="internal_error"):
        run(make(site))
    site2 = standard()
    site2.script = [httpx.Response(200, text="<html>gateway</html>")]
    with pytest.raises(HttpError, match="not JSON"):
        run(make(site2))


def test_cursors_hold_no_secret_and_a_missing_variable_is_named(monkeypatch):
    _, batches = run(make(standard()))
    assert TOKEN not in json.dumps([b.cursor for b in batches])
    monkeypatch.delenv("SLACK_TOKEN")
    with pytest.raises(HttpError, match="SLACK_TOKEN"):
        run(make(standard()))


# ── permissions and open() ──


def test_acl_rules_by_channel_prefix_and_fail_closed_default():
    rules = [{"prefix": "slack/general", "principals": ["group:everyone"]}, {"prefix": "slack/ops", "principals": ["group:sre"]}]
    connector = make(standard(), acl_rules=rules, default_acl=None)
    by = {c.external_id: c.file for c in run(connector)[0]}
    assert by["slack:C01:2026-03-02"].acl == ["group:everyone"] and by["slack:C02:2026-03-03"].acl == ["group:sre"]
    assert connector.refresh_acl(persisted(by["slack:C02:2026-03-03"])) == ["group:sre"]
    only_general = make(standard(), acl_rules=rules[:1], default_acl=None)
    by = {c.external_id: c.file for c in run(only_general)[0]}
    assert by["slack:C02:2026-03-03"].acl == [] and by["slack:C01:2026-03-03"].acl == ["group:everyone"]


def test_open_rebuilds_exactly_the_day_that_was_scanned_from_persisted_columns_only():
    site = standard()
    scanned = {c.external_id: c.file for c in run(make(site))[0]}
    fresh_site = standard()
    fresh = make(fresh_site)
    for ext, sf in scanned.items():
        text = read(fresh, sf)
        assert text.startswith(f"# #{sf.path.split('/')[1]} {ext.rsplit(':', 1)[1]}\n")
        assert len(text.encode()) == sf.size                     # the same bytes the version was computed from
    history = fresh_site.calls("conversations.history")
    day_calls = [r for r in history if r.url.params["channel"] == "C01"][-1].url.params
    assert float(day_calls["latest"]) - float(day_calls["oldest"]) == 86400


def test_open_of_a_day_that_has_vanished_is_a_header_only_document_and_bad_ids_are_rejected():
    site = standard()
    connector = make(site)
    text = read(connector, SourceFile("slack:C01:2026-02-01", "slack/general/2026-02-01.md", "n", 0, None, "v", []))
    assert text.startswith("# #general 2026-02-01") and "messages: 0" in text
    site.reset()
    for bad in ("slack:C01:2026-02-30x", "slack:../x:2026-03-01", "slack:C01", "other:C01:2026-03-01"):
        with pytest.raises(HttpError):
            connector.open(SourceFile(bad, "p", "n", 0, None, "v", []))
    assert site.requests == []
