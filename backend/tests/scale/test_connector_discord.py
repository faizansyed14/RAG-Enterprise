"""Discord connector against a fake REST API shaped like the Discord documentation: ids are snowflakes,
GET /channels/{id}/messages answers newest first and honours `before` (strictly older), rate-limit headers on the
responses, 429 bodies with `retry_after`, 401 `{"message": "401: Unauthorized"}`, 403 "Missing Access" for a channel the
bot may not read.

Not certain of (written from memory of the docs): which optional message fields a real server fills in, and the exact
wording of error bodies (the connector only looks at status codes and `retry_after`).
"""

import copy
import json
from datetime import datetime, timezone

import httpx
import pytest

from app.scale.connectors._http import AuthError, HttpError
from app.scale.connectors.base import SourceFile
from app.scale.connectors.discord import DiscordConnector, RetryAfterFromBody, snowflake, snowflake_time

TOKEN = "MTIzNDU2Nzg5.SeCrEt.discord-bot-token-value"
GUILD = "111111111111111111"
OTHER_GUILD = "111111111111111199"
NOW = datetime(2026, 3, 10, 12, 0, tzinfo=timezone.utc).timestamp()
EPOCH = 1420070400000


def at(day, hhmm="09:00"):
    hh, mm = map(int, hhmm.split(":"))
    y, m, d = map(int, day.split("-"))
    return datetime(y, m, d, hh, mm, tzinfo=timezone.utc)


def boundary(day):
    """The first snowflake of a UTC day, computed here from the documented formula."""
    return str((int(at(day, "00:00").timestamp()) * 1000 - EPOCH) << 22)


def cid(n):
    return f"2222222222222222{n:02d}"


def ext(channel, day):
    return f"discord:{channel}:{day}"


ADA = {"id": "900000000000000001", "username": "ada", "global_name": "Ada Lovelace"}
GRACE = {"id": "900000000000000002", "username": "grace", "global_name": None}
BOT = {"id": "900000000000000003", "username": "deploybot", "global_name": None, "bot": True}


def msg(day, hhmm, author, content, n=1, **extra):
    when = at(day, hhmm)
    return {"id": str(((int(when.timestamp()) * 1000 - EPOCH) << 22) | n), "type": 0, "content": content, "author": author,
            "timestamp": when.strftime("%Y-%m-%dT%H:%M:%S.000000+00:00"), "edited_timestamp": None, "mentions": [],
            "attachments": [], "embeds": [], **extra}


def chan(channel_id, name, type=0, **extra):
    return {"id": channel_id, "type": type, "guild_id": GUILD, "name": name, "position": 0, "permission_overwrites": [],
            "parent_id": None, **extra}


def thread(thread_id, parent, name, type=11):
    return {"id": thread_id, "type": type, "guild_id": GUILD, "parent_id": parent, "name": name, "thread_metadata": {"archived": False}}


class Discord:
    def __init__(self, channels, history, *, threads=(), guild_name="Acme", other_guilds=None):
        self.channels = copy.deepcopy(channels)
        self.history = copy.deepcopy(history)
        self.threads = copy.deepcopy(list(threads))
        self.guild_name = guild_name
        self.other_guilds = other_guilds or {}               # guild id -> (name, channels)
        self.blocked: dict[str, int] = {}                    # channel id -> status of its message list
        self.headers: dict[str, str] = {}                    # added to every 200 (rate-limit headers)
        self.guild_status: int | None = None                 # status of GET /guilds/{id}
        self.lookup_status: int | None = None                # status of GET /channels/{id}
        self.requests: list[httpx.Request] = []
        self.script: list[httpx.Response] = []

    def reset(self):
        self.requests.clear()

    def messages(self, channel=None):
        return [r for r in self.requests if r.url.path.endswith("/messages") and (channel is None or f"/channels/{channel}/" in r.url.path)]

    def lookups(self):
        return [r.url.path.removeprefix("/api/v10") for r in self.requests if not r.url.path.endswith("/messages")]

    def _ok(self, body):
        return httpx.Response(200, json=body, headers=self.headers)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.script:
            return self.script.pop(0)
        if request.headers.get("authorization") != f"Bot {TOKEN}":
            return httpx.Response(401, json={"message": "401: Unauthorized", "code": 0})
        q = dict(request.url.params)
        assert "token" not in q, "the token must travel in the Authorization header, never in the URL"
        parts = request.url.path.removeprefix("/api/v10").strip("/").split("/")
        if parts[0] == "guilds":
            guild = parts[1]
            known = {GUILD: (self.guild_name, self.channels, self.threads), **{g: (n, c, []) for g, (n, c) in self.other_guilds.items()}}
            if guild not in known:
                return httpx.Response(404, json={"message": "Unknown Guild", "code": 10004})
            name, channels, threads = known[guild]
            if len(parts) == 2:
                if self.guild_status:
                    return httpx.Response(self.guild_status, json={"message": "Missing Access", "code": 50001})
                return self._ok({"id": guild, "name": name, "owner_id": "1"})
            if parts[2] == "channels":
                return self._ok(channels)
            if parts[2:] == ["threads", "active"]:
                return self._ok({"threads": threads, "members": []})
        if parts[0] == "channels":
            channel = parts[1]
            if len(parts) == 2:
                if self.lookup_status:
                    return httpx.Response(self.lookup_status, json={"message": "Missing Access", "code": 50001})
                return self._ok({"id": channel, "guild_id": GUILD, "type": 0})
            if parts[2] == "messages":
                if channel in self.blocked:
                    return httpx.Response(self.blocked[channel], json={"message": "Missing Access", "code": 50001})
                if channel not in self.history:
                    return httpx.Response(404, json={"message": "Unknown Channel", "code": 10003})
                limit = int(q.get("limit", 50))
                assert 1 <= limit <= 100, "Discord accepts limits from 1 to 100"
                items = sorted(self.history[channel], key=lambda m: int(m["id"]), reverse=True)
                if "before" in q:
                    items = [m for m in items if int(m["id"]) < int(q["before"])]
                return self._ok(items[:limit])
        return httpx.Response(404, json={"message": "404: Not Found", "code": 0})


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("DISCORD_TOKEN", TOKEN)


def make(site, clock=lambda: NOW, **cfg):
    sleeps = []
    config = {"token_env": "DISCORD_TOKEN", "guilds": [GUILD], "page_size": 2, "default_acl": ["group:all"], **cfg}
    connector = DiscordConnector(config, transport=httpx.MockTransport(site.handler), sleep=sleeps.append, clock=clock)
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


def upserts(changes):
    return {c.external_id: c.file for c in changes if c.kind == "upsert"}


GENERAL_ID, OPS_ID, THREAD_ID = cid(1), cid(2), cid(21)
CATEGORY = chan(cid(9), "Text Channels", 4)
CHANNELS = [CATEGORY, chan(GENERAL_ID, "general"), chan(OPS_ID, "ops")]
GENERAL = [
    msg("2026-03-02", "09:15", ADA, f"Morning <@{GRACE['id']}>, standup is in <#{OPS_ID}> today", mentions=[GRACE]),
    msg("2026-03-02", "09:20", GRACE, "Ack"),
    msg("2026-03-02", "17:45", ADA, "See https://wiki.acme.io/deploy"),
    msg("2026-03-03", "10:00", GRACE, "Second day"),
    msg("2026-03-03", "10:05", ADA, "Second day, edited", edited_timestamp="2026-03-03T10:06:00.000000+00:00"),
    msg("2026-03-04", "08:00", ADA, "Thread starter"),
    msg("2026-03-04", "08:30", GRACE, "", type=7),                       # USER_JOIN: a system message
    msg("2026-03-05", "08:00", ADA, "Agreed", type=19, referenced_message={"author": GRACE, "content": "..."}),
]
OPS = [msg("2026-03-03", "11:00", ADA, "ops message")]


def standard(**kw):
    return Discord(CHANNELS, {GENERAL_ID: GENERAL, OPS_ID: OPS}, **kw)


GENERAL_DAYS = [ext(GENERAL_ID, d) for d in ("2026-03-05", "2026-03-04", "2026-03-03", "2026-03-02")]


# ── api helpers ──


def test_snowflakes_follow_the_documented_formula():
    documented = "175928847299117063"                                    # the example in "Reference: Snowflakes"
    assert snowflake_time(documented) == datetime(2016, 4, 30, 11, 18, 25, 796000, tzinfo=timezone.utc)
    assert snowflake(datetime(2016, 4, 30, 11, 18, 25, 796000, tzinfo=timezone.utc)) == "175928847298985984"
    assert snowflake(at("2026-03-05", "00:00")) == boundary("2026-03-05")
    assert snowflake(datetime(1999, 1, 1, tzinfo=timezone.utc)) == "0"                   # before Discord existed
    assert snowflake_time(snowflake(at("2026-03-05", "13:37"))) == at("2026-03-05", "13:37")


def test_config_needs_guild_ids():
    for bad in ({}, {"guilds": []}, {"guilds": ["my-server"]}, {"guilds": ["123"]}, {"guilds": "111111111111111111x"}):
        with pytest.raises(ValueError, match="guilds"):
            DiscordConnector({"token_env": "DISCORD_TOKEN", **bad})
    assert DiscordConnector({"token_env": "DISCORD_TOKEN", "guilds": [int(GUILD)]}).guilds == [GUILD]


# ── pagination and grouping ──


def test_history_is_paged_newest_first_with_before_and_days_are_grouped_across_page_boundaries():
    site = standard()
    changes, batches = run(make(site))
    assert [c.external_id for c in changes] == GENERAL_DAYS + [ext(OPS_ID, "2026-03-03")]           # channels by id, newest day first
    pages = site.messages(GENERAL_ID)
    newest_first = sorted(GENERAL, key=lambda m: int(m["id"]), reverse=True)
    assert [r.url.params.get("before") for r in pages] == [None] + [newest_first[i]["id"] for i in (1, 3, 5, 7)]   # the oldest id of each page
    assert all(r.url.params["limit"] == "2" for r in pages)
    assert len(pages) == 5                                   # eight messages = four full pages; an empty fifth one proves the end
    assert [r.url.path.removeprefix("/api/v10") for r in site.requests[:3]] == [f"/guilds/{GUILD}", f"/guilds/{GUILD}/channels", f"/guilds/{GUILD}/threads/active"]
    day = upserts(changes)[ext(GENERAL_ID, "2026-03-02")]
    assert day.path == "discord/Acme/general/2026-03-02.md" and day.name == "general-2026-03-02.md" and day.mime == "text/markdown"
    assert day.modified_at == at("2026-03-02", "17:45") and day.acl == ["group:all"]
    assert batches[-1].done and batches[-1].changes == [] and batches[-1].complete_scope == ""
    assert all(b.complete_scope is None and not b.done for b in batches[:-1])


def test_a_short_page_ends_the_channel_and_page_size_is_clamped_to_discords_maximum():
    site = standard()
    run(make(site, page_size=3))
    assert [r.url.params["limit"] for r in site.messages(GENERAL_ID)] == ["3", "3", "3"]      # 3 + 3 + 2: the short page is the end
    wide = standard()
    run(make(wide, page_size=500))
    assert {r.url.params["limit"] for r in wide.messages()} == {"100"} and len(wide.messages(GENERAL_ID)) == 1


def test_only_text_and_announcement_channels_are_read_and_filters_match_name_id_and_glob():
    chans = [CATEGORY, chan(cid(1), "general"), chan(cid(2), "eng-backend"), chan(cid(3), "eng-frontend"), chan(cid(4), "lounge", 2),
             chan(cid(5), "town-hall", 13), chan(cid(6), "news", 5), chan(cid(7), "help-forum", 15), chan(cid(8), "random")]
    site = Discord(chans, {c["id"]: [msg("2026-03-02", "09:00", ADA, "x")] for c in chans})
    names = lambda **cfg: sorted(c.file.path.split("/")[2] for c in run(make(site, **cfg))[0])          # noqa: E731
    assert names() == ["eng-backend", "eng-frontend", "general", "news", "random"]
    assert site.messages(cid(4)) == [] and site.messages(cid(5)) == [] and site.messages(cid(7)) == [] and site.messages(CATEGORY["id"]) == []
    assert names(channels=["#general", "eng-*"]) == ["eng-backend", "eng-frontend", "general"]
    assert names(channels=["GENERAL"]) == ["general"]
    assert names(channels=[cid(8)]) == ["random"]
    assert names(exclude_channels=["eng-*", "random"]) == ["general", "news"]
    assert names(channels=["eng-*"], exclude_channels=["#eng-backend"]) == ["eng-frontend"]


def test_a_channel_that_is_no_longer_listed_is_left_to_the_authoritative_scan_to_remove():
    site = standard()
    site.channels = [c for c in site.channels if c["id"] != OPS_ID]
    changes, batches = run(make(site))
    assert ext(OPS_ID, "2026-03-03") not in upserts(changes) and batches[-1].complete_scope == ""      # not mentioned => deleted by the sync layer


def test_a_channel_hidden_from_everyone_is_not_indexed_unless_asked():
    everyone = lambda deny, kind=0, who=GUILD: [{"id": who, "type": kind, "allow": "0", "deny": str(deny)}]      # noqa: E731
    chans = [
        chan(cid(1), "public"),
        chan(cid(2), "staff", permission_overwrites=everyone(1 << 10)),                                   # @everyone: no View Channel
        chan(cid(3), "staff-also-muted", permission_overwrites=everyone((1 << 10) | (1 << 11))),
        chan(cid(4), "other-role-denied", permission_overwrites=everyone(1 << 10, who="333333333333333333")),   # a role other than @everyone
        chan(cid(5), "read-only", permission_overwrites=everyone(1 << 11)),                              # cannot send, can view
        chan(cid(6), "member-overwrite", permission_overwrites=everyone(1 << 10, kind=1)),               # type 1 = a member, not a role
        chan(cid(7), "garbled", permission_overwrites=everyone("not-a-number")),                         # unreadable => treated as private
    ]
    site = Discord(chans, {c["id"]: [msg("2026-03-02", "09:00", ADA, "x")] for c in chans})
    names = lambda **cfg: sorted(c.file.path.split("/")[2] for c in run(make(site, **cfg))[0])          # noqa: E731
    assert names() == ["member-overwrite", "other-role-denied", "public", "read-only"]
    assert site.messages(cid(2)) == [] and site.messages(cid(7)) == []                                    # never even requested
    assert names(include_private_channels=True) == sorted(c["name"] for c in chans)


def test_active_public_threads_are_sub_folders_of_their_parent_and_private_or_orphaned_ones_are_left_out():
    threads = [thread(THREAD_ID, GENERAL_ID, "Release plan"), thread(cid(22), GENERAL_ID, "secret", type=12), thread(cid(23), cid(77), "orphan"),
               thread(cid(24), OPS_ID, "ops chatter", type=10)]
    history = {GENERAL_ID: GENERAL, OPS_ID: OPS, THREAD_ID: [msg("2026-03-02", "10:00", GRACE, "in the thread")], cid(24): [msg("2026-03-02", "10:00", GRACE, "ops thread")]}
    site = Discord(CHANNELS, history, threads=threads)
    connector = make(site)
    changes, _ = run(connector)
    files = upserts(changes)
    sub = files[ext(THREAD_ID, "2026-03-02")]
    assert sub.path == "discord/Acme/general/Release plan/2026-03-02.md" and sub.name == "general-Release plan-2026-03-02.md"
    assert site.messages(cid(22)) == [] and site.messages(cid(23)) == []                                   # a private thread, a thread of an unknown channel
    assert ext(cid(24), "2026-03-02") in files                                                           # an announcement thread (type 10) counts as public
    text = read(make(site), sub)
    assert text.startswith("# #general/Release plan 2026-03-02\n") and "[10:00] grace: in the thread" in text
    names = lambda **cfg: sorted({c.external_id.split(":")[1] for c in run(make(site, **cfg))[0]})          # noqa: E731
    assert THREAD_ID in names(channels=["general"])                                                       # a thread follows its parent's name
    assert THREAD_ID not in names(exclude_channels=["release*"]) and GENERAL_ID in names(exclude_channels=["release*"])
    assert THREAD_ID not in names(exclude_channels=["general"]) and GENERAL_ID not in names(exclude_channels=["general"])
    assert THREAD_ID not in names(channels=["ops"])
    off = Discord(CHANNELS, history, threads=threads)
    changes, _ = run(make(off, include_threads=False))
    assert off.lookups().count(f"/guilds/{GUILD}/threads/active") == 0 and not any(e.endswith(THREAD_ID) or THREAD_ID in e for e in upserts(changes))


def test_several_guilds_get_their_own_folders_and_the_guild_name_falls_back_to_the_id():
    other = [chan(cid(31), "general")]
    site = Discord(CHANNELS, {GENERAL_ID: GENERAL, OPS_ID: OPS, cid(31): [msg("2026-03-02", "09:00", GRACE, "hello from the other side")]},
                   other_guilds={OTHER_GUILD: ("Other Co: Labs", other)})
    changes, _ = run(make(site, guilds=[GUILD, OTHER_GUILD]))
    paths = sorted(f.path for f in upserts(changes).values() if "2026-03-02" in f.path)
    assert paths == ["discord/Acme/general/2026-03-02.md", "discord/Other Co Labs/general/2026-03-02.md"]     # ':' is not allowed in a folder name
    named_by_id = standard()
    named_by_id.guild_status = 403                                                                   # GET /guilds/{id} not allowed: only the name is lost
    assert upserts(run(make(named_by_id))[0])[GENERAL_DAYS[0]].path == f"discord/{GUILD}/general/2026-03-05.md"


# ── rendering ──


def test_a_day_file_has_names_mentions_replies_edits_and_leaves_out_system_messages():
    changes, _ = run(make(standard()))
    by = upserts(changes)
    fresh = make(standard())
    d2 = read(fresh, by[ext(GENERAL_ID, "2026-03-02")])
    assert d2.startswith("# #general 2026-03-02\n")
    assert "channel: #general" in d2 and "date: 2026-03-02 (UTC)" in d2 and "messages: 3" in d2
    assert "[09:15] Ada Lovelace: Morning @grace, standup is in #ops today" in d2          # <@id> from `mentions` (no global name => username), <#id> from the guild
    assert "[09:20] grace: Ack" in d2 and "[17:45] Ada Lovelace: See https://wiki.acme.io/deploy" in d2
    assert d2.index("[09:15]") < d2.index("[09:20]") < d2.index("[17:45]")                  # oldest first inside a day
    assert "Second day, edited (edited)" in read(fresh, by[ext(GENERAL_ID, "2026-03-03")])
    d4 = read(fresh, by[ext(GENERAL_ID, "2026-03-04")])
    assert "messages: 1" in d4 and "Thread starter" in d4 and "(no text)" not in d4          # the join notice is not a message
    assert "[08:00] Ada Lovelace (reply to grace): Agreed" in read(fresh, by[ext(GENERAL_ID, "2026-03-05")])


def test_embeds_attachments_emoji_timestamps_roles_multiline_and_empty_messages_are_rendered():
    day = "2026-03-06"
    history = {GENERAL_ID: [
        msg(day, "09:00", BOT, "", embeds=[{"title": "Deploy 42", "description": f"Finished in <#{OPS_ID}>", "fields": [{"name": "env", "value": "prod"}]}]),
        msg(day, "09:05", ADA, "", n=2, attachments=[{"id": "1", "filename": "report.pdf", "size": 10}, {"id": "2", "size": 3}]),
        msg(day, "09:06", ADA, f"party <:tada:123456789> and <a:wave:987654321> at <t:{int(NOW)}:R>", n=3),
        msg(day, "09:07", ADA, "line one\nline two\n\nline four", n=4, edited_timestamp="2026-03-06T09:08:00.000000+00:00"),
        msg(day, "09:08", GRACE, "", n=5),                                                           # no MESSAGE CONTENT intent
        msg(day, "09:09", ADA, f"ping <@&555666777> and <@!{ADA['id']}> and <@999>", n=6, mentions=[ADA]),
        msg(day, "09:10", ADA, "slash command", n=7, type=20),
        msg(day, "09:11", ADA, "pinned notice", n=8, type=6),                                          # CHANNEL_PINNED_MESSAGE
        msg(day, "09:12", ADA, "boosted", n=9, type=8),                                                # GUILD_BOOST
        msg(day, "09:13", ADA, "<t:notanumber> <t:99999999999999999999> <#123456789012345678>", n=10),
    ]}
    site = Discord([CATEGORY, chan(GENERAL_ID, "general"), chan(OPS_ID, "ops")], {**history, OPS_ID: []})
    connector = make(site, page_size=100)
    (change,) = run(connector)[0]
    text = read(make(site, page_size=100), change.file)
    assert "messages: 8" in text
    assert "[09:00] deploybot (bot): Deploy 42\n    Finished in #ops\n    env: prod\n" in text
    assert "[09:05] Ada Lovelace: [file: report.pdf]\n    [file: unnamed]\n" in text
    assert "[09:06] Ada Lovelace: party :tada: and :wave: at 2026-03-10 12:00 UTC" in text
    assert "[09:07] Ada Lovelace: line one\n    line two\n    \n    line four (edited)\n" in text
    assert "[09:08] grace: (no text)" in text
    assert "[09:09] Ada Lovelace: ping @role-555666777 and @Ada Lovelace and @999" in text
    assert "[09:10] Ada Lovelace: slash command" in text
    assert "pinned notice" not in text and "boosted" not in text
    assert "[09:13] Ada Lovelace: <t:notanumber> <t:99999999999999999999> #123456789012345678" in text   # unknown things stay readable


# ── versions ──


def test_version_is_stable_and_changes_with_edits_new_messages_and_deletions_only():
    def versions(site):
        return {c.external_id: c.file.version for c in run(make(site))[0]}

    base = versions(standard())
    assert versions(standard()) == base and base[GENERAL_DAYS[3]].startswith("3-")
    d5, d4, d3, d2 = GENERAL_DAYS
    edited = standard()
    next(m for m in edited.history[GENERAL_ID] if m["content"] == "Ack")["content"] = "Ack, edited"
    after = versions(edited)
    assert after[d2] != base[d2] and all(after[d] == base[d] for d in (d5, d4, d3))
    added = standard()
    added.history[GENERAL_ID].append(msg("2026-03-03", "18:00", GRACE, "one more"))
    after = versions(added)
    assert after[d3] != base[d3] and after[d3].startswith("3-") and after[d2] == base[d2]
    deleted = standard()
    deleted.history[GENERAL_ID] = [m for m in deleted.history[GENERAL_ID] if m["content"] != "Ack"]
    after = versions(deleted)
    assert after[d2] != base[d2] and after[d2].startswith("2-")
    reacted = standard()
    reacted.history[GENERAL_ID][0]["reactions"] = [{"count": 3, "emoji": {"name": "tada"}}]           # reactions are not indexed
    reacted.history[GENERAL_ID][0]["pinned"] = True
    assert versions(reacted) == base


# ── resume ──


def test_resume_in_the_middle_of_a_channel_uses_the_day_boundary_and_does_not_repeat_days():
    site = standard()
    batches = list(make(site, batch_size=1).scan({}))
    first = batches[0]
    assert [c.external_id for c in first.changes] == [GENERAL_DAYS[0]]
    assert first.cursor["pos"] == {"ch": GENERAL_ID, "latest": "2026-03-05T00:00:00+00:00"} and first.cursor["mode"] == "full"
    assert json.loads(json.dumps(first.cursor)) == first.cursor
    site.reset()
    changes, resumed = run(make(site, batch_size=1), first.cursor)
    assert [c.external_id for c in changes] == GENERAL_DAYS[1:] + [ext(OPS_ID, "2026-03-03")]
    assert site.messages(GENERAL_ID)[0].url.params["before"] == boundary("2026-03-05")
    assert resumed[-1].done and resumed[-1].complete_scope is None and all(b.complete_scope is None for b in resumed)


def test_resume_between_channels_skips_finished_ones_and_a_finished_cursor_does_nothing():
    site = standard()
    batches = list(make(site).scan({}))
    boundary_batch = next(b for b in batches if b.cursor.get("pos", {}).get("ch") == OPS_ID)
    assert [c.external_id for c in boundary_batch.changes] == GENERAL_DAYS                              # one batch per finished channel
    site.reset()
    changes, _ = run(make(site), boundary_batch.cursor)
    assert [c.external_id for c in changes] == [ext(OPS_ID, "2026-03-03")]
    assert site.messages(GENERAL_ID) == [] and {r.url.path.rsplit("/", 2)[-2] for r in site.messages()} == {OPS_ID}
    last = batches[-2].cursor
    assert last["pos"] == {"finished": True}
    site.reset()
    changes, resumed = run(make(site), last)
    assert changes == [] and site.requests == [] and resumed[-1].done


def test_a_message_exactly_on_midnight_is_not_double_counted_by_a_resume():
    history = {GENERAL_ID: [msg("2026-03-03", "00:00", ADA, "midnight sharp", 0), msg("2026-03-02", "23:59", ADA, "last minute", 1)]}
    site = Discord([chan(GENERAL_ID, "general")], history)
    batches = list(make(site, batch_size=1).scan({}))
    assert [c.external_id for c in batches[0].changes] == [ext(GENERAL_ID, "2026-03-03")]
    changes, _ = run(make(site, batch_size=1), batches[0].cursor)
    assert [c.external_id for c in changes] == [ext(GENERAL_ID, "2026-03-02")]                           # `before` is exclusive: 03-03 is not emitted again
    day = SourceFile(ext(GENERAL_ID, "2026-03-03"), "discord/Acme/general/2026-03-03.md", "n", 0, None, "v", [])
    text = read(make(site), day)
    assert "midnight sharp" in text and "last minute" not in text                                      # ... and open() still includes it


def test_resumed_incremental_scan_neither_repeats_a_day_nor_deletes_one():
    history = {GENERAL_ID: [msg("2026-03-03", "00:00", ADA, "midnight sharp", 0), msg("2026-03-02", "23:59", ADA, "last minute", 1)]}
    site = Discord([chan(GENERAL_ID, "general")], history)
    cursor = {"mode": "inc", "since": "2026-03-05T00:00:00Z", "last_full": NOW - 60, "high": None,
              "pos": {"ch": GENERAL_ID, "latest": "2026-03-03T00:00:00+00:00"}}                         # 03-03 was emitted before the crash
    changes, batches = run(make(site, rescan_days=3), cursor)
    assert [(c.kind, c.external_id) for c in changes] == [("upsert", ext(GENERAL_ID, "2026-03-02"))]       # no repeat of 03-03, no delete of any day
    assert site.messages()[0].url.params["before"] == boundary("2026-03-03")
    assert batches[-1].complete_scope is None


# ── incremental ──


def test_next_scan_rereads_a_window_and_deletes_the_days_that_are_now_empty():
    site = standard()
    _, batches = run(make(site))
    final = batches[-1].cursor
    assert final == {"since": "2026-03-05T08:00:00Z", "last_full": NOW}                                  # the newest message seen
    d5, d4, d3, d2 = GENERAL_DAYS
    site.history[GENERAL_ID] = [m for m in site.history[GENERAL_ID] if m["content"] != "Agreed"]         # every message of 03-05 is deleted
    site.history[GENERAL_ID].append(msg("2026-03-09", "14:00", GRACE, "new message after the watermark"))
    site.reset()
    changes, inc = run(make(site, rescan_days=1), final)
    by = {(c.kind, c.external_id) for c in changes}
    assert ("upsert", ext(GENERAL_ID, "2026-03-09")) in by and ("upsert", d4) in by
    assert ("delete", d5) in by                                                                          # in the window, nothing left of it
    assert {("delete", ext(GENERAL_ID, d)) for d in ("2026-03-06", "2026-03-07", "2026-03-08", "2026-03-10")} <= by      # quiet days, up to today
    assert not any(e in (d3, d2) for _, e in by)                                                         # older than the window: neither read nor deleted
    assert not any(e == ext(OPS_ID, "2026-03-03") for _, e in by)
    assert len(site.messages(GENERAL_ID)) == 2                                                            # it stopped paging once it passed the window
    assert inc[-1].complete_scope is None and inc[-1].cursor == {"since": "2026-03-09T14:00:00Z", "last_full": NOW}
    assert [c for c in changes if c.kind == "delete"] and all(c.file is None for c in changes if c.kind == "delete")


def test_incremental_window_starts_rescan_days_before_the_watermark_at_midnight():
    cursor = {"since": "2026-03-05T20:00:00Z", "last_full": NOW - 60}
    full = {c.external_id: c.file.version for c in run(make(standard()))[0]}
    changes, _ = run(make(standard(), rescan_days=2), cursor)
    # 03-05 20:00 minus two days is 03-03 20:00, but the window opens at 03-03 00:00: all of 03-03 is read, nothing of 03-02
    got = {c.external_id: c.file.version for c in changes if c.kind == "upsert"}
    assert got == {k: full[k] for k in (GENERAL_DAYS[0], GENERAL_DAYS[1], GENERAL_DAYS[2], ext(OPS_ID, "2026-03-03"))}
    deleted = {c.external_id for c in changes if c.kind == "delete"}
    days = ["2026-03-%02d" % d for d in range(3, 11)]
    assert deleted == {ext(GENERAL_ID, d) for d in days if d not in ("2026-03-03", "2026-03-04", "2026-03-05")} | {ext(OPS_ID, d) for d in days if d != "2026-03-03"}


def test_config_since_is_a_floor_for_full_and_incremental_scans():
    site = standard()
    changes, batches = run(make(site, since="2026-03-03"))
    assert sorted(upserts(changes)) == sorted(GENERAL_DAYS[:3] + [ext(OPS_ID, "2026-03-03")])
    assert all(c.kind == "upsert" for c in changes) and batches[-1].complete_scope == ""
    inc, _ = run(make(site, since="2026-03-05", rescan_days=30), {"since": "2026-03-06T00:00:00Z", "last_full": NOW - 60})
    assert ext(GENERAL_ID, "2026-03-05") in upserts(inc) and ext(GENERAL_ID, "2026-03-04") not in upserts(inc)       # 03-06 - 30 days is older than the floor


def test_full_resync_is_authoritative_and_a_resumed_scan_is_not():
    site = standard()
    changes, batches = run(make(site), {"since": "2026-03-05T00:00:00Z", "last_full": NOW - 40 * 3600})
    assert batches[-1].complete_scope == "" and len(upserts(changes)) == 5 and batches[-1].cursor["last_full"] == NOW
    again = standard()
    _, batches = run(make(again), {"since": "2026-03-05T00:00:00Z"})                                     # no last_full: never had a full pass
    assert batches[-1].complete_scope == ""
    recent = standard()
    _, batches = run(make(recent), {"since": "2026-03-05T00:00:00Z", "last_full": NOW - 3600})
    assert batches[-1].complete_scope is None


def test_max_items_and_max_pages_truncate_without_deleting_or_advancing_the_watermark():
    site = standard()
    changes, batches = run(make(site, max_items=2))
    assert [c.external_id for c in changes] == GENERAL_DAYS[:2] and batches[-1].complete_scope is None
    assert "since" not in batches[-1].cursor and batches[-1].done
    four = standard()
    changes, batches = run(make(four, max_items=4))                                                      # exactly the days of the first channel
    assert [c.external_id for c in changes] == GENERAL_DAYS and batches[-1].complete_scope is None
    assert four.messages(OPS_ID) == []                                                                    # nothing is requested for work that cannot be kept
    pages = standard()
    changes, batches = run(make(pages, max_pages=3))
    assert len(pages.messages()) == 2 and batches[-1].complete_scope is None and "since" not in batches[-1].cursor      # the page budget covers channels + history


# ── errors and rate limits ──


def test_a_bad_token_is_an_auth_error_that_does_not_show_the_token(monkeypatch):
    wrong = "wrong-token-value-1234"
    monkeypatch.setenv("DISCORD_TOKEN", wrong)
    site = standard()
    site.script = [httpx.Response(401, json={"message": f"401: Unauthorized (you sent {wrong})", "code": 0})]       # a server that echoes the credential
    connector = make(site)
    with pytest.raises(AuthError) as err:
        run(connector)
    assert "DISCORD_TOKEN" in str(err.value) and wrong not in str(err.value) and TOKEN not in str(err.value)
    assert len(site.requests) == 1 and connector.sleeps == []
    revoked = standard()
    monkeypatch.setenv("DISCORD_TOKEN", TOKEN)
    revoked.blocked[GENERAL_ID] = 401                                                                    # revoked in the middle of a scan: not swallowed as "skip channel"
    with pytest.raises(AuthError):
        run(make(revoked))


def test_a_channel_the_bot_cannot_read_is_skipped_but_other_channels_are_scanned():
    for status in (403, 404):
        site = standard()
        site.blocked[GENERAL_ID] = status
        changes, batches = run(make(site))
        assert [c.external_id for c in changes] == [ext(OPS_ID, "2026-03-03")]
        assert batches[-1].complete_scope == ""                         # general is treated as gone: its indexed days are removed (fail closed)
    site = standard()
    site.blocked[GENERAL_ID] = 403
    changes, batches = run(make(site), {"since": "2026-03-05T08:00:00Z", "last_full": NOW - 60})
    assert not any(GENERAL_ID in c.external_id for c in changes) and batches[-1].complete_scope is None      # an incremental scan deletes nothing it did not see


def test_server_errors_and_odd_bodies_are_raised_not_swallowed():
    guild = httpx.Response(200, json={"id": GUILD, "name": "Acme"})
    listing = httpx.Response(200, json=CHANNELS)
    threads = httpx.Response(200, json={"threads": [], "members": []})
    site = standard()
    site.script = [guild, httpx.Response(500, text="boom")]
    with pytest.raises(HttpError, match="500"):
        run(make(site))
    site = standard()
    site.script = [guild, listing, threads, httpx.Response(200, json={"message": "something else"})]
    with pytest.raises(HttpError, match="not a list"):
        run(make(site))
    site = standard()
    site.script = [guild, listing, threads, httpx.Response(500, text="boom")]
    with pytest.raises(HttpError, match="500"):
        run(make(site))
    site = standard()
    site.script = [guild, httpx.Response(500, text=f"upstream said {TOKEN}")]                    # a server that echoes the credential
    with pytest.raises(HttpError) as err:
        run(make(site))
    assert "500" in str(err.value) and TOKEN not in str(err.value) and "***" in str(err.value)


def test_a_before_that_never_advances_is_an_error_not_an_endless_loop():
    class Stuck(Discord):
        def handler(self, request):
            if request.url.path.endswith("/messages"):                       # a server that ignores `before`
                request = httpx.Request("GET", request.url.copy_remove_param("before"), headers=request.headers)
            return super().handler(request)

    with pytest.raises(HttpError, match="did not advance"):
        run(make(Stuck(CHANNELS, {GENERAL_ID: GENERAL, OPS_ID: OPS})))


@pytest.mark.parametrize("reset_after, expected", [("1.5", 1.5), ("99999", 120.0), ("soon", 1.0), ("-5", None), (None, None)])
def test_a_drained_bucket_makes_the_next_request_wait_for_reset_after(reset_after, expected):
    site = standard()
    site.headers = {"X-RateLimit-Remaining": "0", **({"X-RateLimit-Reset-After": reset_after} if reset_after else {})}
    connector = make(site)
    run(connector)
    assert connector.sleeps == ([expected] * (len(site.requests) - 1) if expected else [])


def test_a_bucket_with_requests_left_does_not_wait_and_the_wait_is_taken_once():
    site = standard()
    site.headers = {"X-RateLimit-Remaining": "2", "X-RateLimit-Reset-After": "9"}
    connector = make(site)
    run(connector)
    assert connector.sleeps == []
    site = standard()
    site.script = [httpx.Response(200, json={"id": GUILD, "name": "Acme"}, headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset-After": "2.5"})]
    connector = make(site)
    run(connector)
    assert connector.sleeps == [2.5]                                                                      # only before the request that follows it


def test_a_429_is_waited_out_with_retry_after_the_json_body_or_the_default_backoff():
    site = standard()
    limited = {"message": "You are being rate limited.", "retry_after": 1.25, "global": False}
    site.script = [
        httpx.Response(429, headers={"Retry-After": "3"}, json=limited),                                  # the header wins
        httpx.Response(429, json={**limited, "global": True}),                                           # body only (a global limit)
        httpx.Response(429, json={**limited, "retry_after": "soon"}),                                    # unreadable body: default backoff (2 ** attempt)
        httpx.Response(429, text="<html>Cloudflare</html>"),
    ]
    connector = make(site)
    changes, _ = run(connector)
    assert len(upserts(changes)) == 5 and connector.sleeps == [3.0, 1.25, 4, 8]


def test_retry_after_from_body_only_touches_a_429_without_the_header():
    def inner(response):
        return RetryAfterFromBody(httpx.MockTransport(lambda request: response))

    request = httpx.Request("GET", "https://discord.test/x")
    ok = httpx.Response(200, json={"retry_after": 5})
    assert inner(ok).handle_request(request) is ok
    with_header = httpx.Response(429, headers={"Retry-After": "7"}, json={"retry_after": 5})
    assert inner(with_header).handle_request(request) is with_header
    for body in ({"retry_after": "x"}, [1, 2], {"message": "no wait given"}, None):
        bad = httpx.Response(429, json=body) if body is not None else httpx.Response(429, text="<html></html>")
        assert inner(bad).handle_request(request) is bad
    fixed = inner(httpx.Response(429, json={"retry_after": 0.5, "global": True})).handle_request(request)
    assert fixed.status_code == 429 and fixed.headers["retry-after"] == "0.500" and fixed.json()["retry_after"] == 0.5
    negative = inner(httpx.Response(429, json={"retry_after": -3})).handle_request(request)
    assert negative.headers["retry-after"] == "0.000"


def test_cursors_hold_no_secret_and_a_missing_variable_is_named(monkeypatch):
    _, batches = run(make(standard(), batch_size=1))
    assert TOKEN not in json.dumps([b.cursor for b in batches])
    monkeypatch.delenv("DISCORD_TOKEN")
    with pytest.raises(HttpError, match="DISCORD_TOKEN"):
        run(make(standard()))


def test_defaults_rescan_a_week_and_run_a_full_scan_after_a_day():
    cursor = {"since": "2026-03-05T08:00:00Z", "last_full": NOW - 3600}
    changes, _ = run(make(standard()), cursor)
    deleted = {c.external_id for c in changes if c.kind == "delete"}
    assert ext(OPS_ID, "2026-02-26") in deleted and ext(OPS_ID, "2026-02-25") not in deleted         # 03-05 minus the default seven days
    for hours, scope in ((23, None), (25, "")):
        _, batches = run(make(standard()), {"since": "2026-03-05T08:00:00Z", "last_full": NOW - hours * 3600})
        assert batches[-1].complete_scope == scope


def test_channels_are_scanned_in_id_order_whatever_order_the_api_lists_them():
    site = Discord([chan(OPS_ID, "ops"), chan(GENERAL_ID, "general")], {GENERAL_ID: GENERAL, OPS_ID: OPS})
    batches = list(make(site).scan({}))
    assert [c.external_id for b in batches for c in b.changes] == GENERAL_DAYS + [ext(OPS_ID, "2026-03-03")]
    mid = next(b for b in batches if b.cursor.get("pos", {}).get("ch") == OPS_ID)
    site.reset()
    changes, _ = run(make(site), mid.cursor)
    assert [c.external_id for c in changes] == [ext(OPS_ID, "2026-03-03")]


def test_a_message_without_a_timestamp_is_dated_by_its_snowflake():
    message = msg("2026-03-06", "09:00", ADA, "no timestamp")
    del message["timestamp"]
    site = Discord([chan(GENERAL_ID, "general")], {GENERAL_ID: [message]})
    (change,) = run(make(site))[0]
    assert change.external_id == ext(GENERAL_ID, "2026-03-06") and change.file.modified_at == at("2026-03-06", "09:00")
    assert "[09:00] Ada Lovelace: no timestamp" in read(make(site), change.file)


# ── permissions and open() ──


def test_acl_rules_by_channel_prefix_and_fail_closed_default():
    chans = CHANNELS + [chan(cid(3), "general-chat")]
    history = {GENERAL_ID: GENERAL, OPS_ID: OPS, cid(3): [msg("2026-03-03", "12:00", ADA, "chat")]}
    rules = [{"prefix": "discord/Acme/general", "principals": ["group:everyone"]}, {"prefix": "discord/Acme/ops", "principals": ["group:sre"]}]
    connector = make(Discord(chans, history), acl_rules=rules, default_acl=None)
    by = upserts(run(connector)[0])
    assert by[ext(GENERAL_ID, "2026-03-02")].acl == ["group:everyone"] and by[ext(OPS_ID, "2026-03-03")].acl == ["group:sre"]
    assert by[ext(cid(3), "2026-03-03")].acl == []                           # a prefix is a folder boundary: `general` does not cover `general-chat`
    assert connector.refresh_acl(persisted(by[ext(OPS_ID, "2026-03-03")])) == ["group:sre"]
    only_general = make(Discord(chans, history), acl_rules=rules[:1], default_acl=None)
    by = upserts(run(only_general)[0])
    assert by[ext(OPS_ID, "2026-03-03")].acl == [] and by[ext(GENERAL_ID, "2026-03-03")].acl == ["group:everyone"]
    threaded = Discord(CHANNELS, {GENERAL_ID: GENERAL, OPS_ID: OPS, THREAD_ID: [msg("2026-03-02", "10:00", GRACE, "t")]}, threads=[thread(THREAD_ID, GENERAL_ID, "Release plan")])
    assert upserts(run(make(threaded, acl_rules=rules, default_acl=None))[0])[ext(THREAD_ID, "2026-03-02")].acl == ["group:everyone"]
    assert make(standard(), acl_rules=rules, default_acl=None).refresh_acl(SourceFile("x", "discord/Elsewhere/a/2026-03-02.md", "n", 0, None, "v", ["group:old"])) == []


def test_open_rebuilds_exactly_the_day_that_was_scanned_from_persisted_columns_only():
    scanned = upserts(run(make(standard()))[0])
    site = standard()
    fresh = make(site)
    for ext_id, sf in scanned.items():
        text = read(fresh, sf)
        assert text.startswith(f"# #{sf.path.split('/')[2]} {ext_id.rsplit(':', 1)[1]}\n")
        assert len(text.encode()) == sf.size                                  # the same bytes the version was computed from
    site.reset()
    read(fresh, scanned[GENERAL_DAYS[3]])
    assert site.messages(GENERAL_ID)[0].url.params["before"] == boundary("2026-03-03")      # asks for the day by its closing snowflake
    assert len(site.messages(GENERAL_ID)) == 2                                                 # ... and stops when it reaches the day before
    site.reset()
    text = read(fresh, scanned[GENERAL_DAYS[1]])
    assert "Thread starter" in text and "Second day" not in text and "Agreed" not in text


def test_open_names_mentioned_channels_with_two_lookups_and_survives_a_failed_lookup():
    scanned = upserts(run(make(standard()))[0])
    site = standard()
    fresh = make(site)
    text = read(fresh, scanned[GENERAL_DAYS[3]])
    assert "standup is in #ops today" in text
    assert site.lookups() == [f"/channels/{GENERAL_ID}", f"/guilds/{GUILD}/channels"]
    site.reset()
    read(fresh, scanned[GENERAL_DAYS[2]])                                         # no channel mention: no lookup
    assert site.lookups() == []
    failing = standard()
    failing.lookup_status = 403
    assert f"standup is in #{OPS_ID} today" in read(make(failing), scanned[GENERAL_DAYS[3]])
    revoked = standard()
    revoked.lookup_status = 401
    with pytest.raises(AuthError):
        read(make(revoked), scanned[GENERAL_DAYS[3]])


def test_open_of_a_day_that_has_vanished_is_a_header_only_document_and_bad_ids_are_rejected():
    site = standard()
    connector = make(site)
    text = read(connector, SourceFile(ext(GENERAL_ID, "2026-02-01"), "discord/Acme/general/2026-02-01.md", "n", 0, None, "v", []))
    assert text.startswith("# #general 2026-02-01") and "messages: 0" in text
    odd = read(connector, SourceFile(ext(GENERAL_ID, "2026-03-02"), "weird", "n", 0, None, "v", []))     # a path without a channel folder
    assert odd.startswith(f"# #{GENERAL_ID} 2026-03-02")
    site.reset()
    for bad in ("discord:abc:2026-03-01", "discord:../x:2026-03-01", f"discord:{GENERAL_ID}", f"slack:{GENERAL_ID}:2026-03-01",
                f"discord:{GENERAL_ID}:2026-3-1", f"discord:{GENERAL_ID}:2026-02-30", f"discord:{GENERAL_ID}:2026-03-01:x"):
        with pytest.raises(HttpError):
            connector.open(SourceFile(bad, "p", "n", 0, None, "v", []))
    assert site.requests == []
