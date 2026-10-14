"""The helpers every object connector shares: HTML conversion, the cursor protocol, bounded work, 403-as-throttle."""

from datetime import datetime, timezone

import httpx
import pytest

from app.scale.connectors._http import Http, HttpError
from app.scale.connectors._objects import (
    Budget, ScanState, ThrottleAs429, basic_auth, html_to_text, iso_z, parse_dt, parse_link_next, redacted, short_hash,
)

NOW = datetime(2026, 3, 10, 12, 0, tzinfo=timezone.utc).timestamp()


# ── html_to_text ──


def test_blocks_lists_links_emphasis_and_entities():
    html = ("<html><head><title>ignored</title><style>p{color:red}</style></head><body>"
            "<h1>Top</h1><p>Hello <b>big</b> <em>wide</em> <a href='https://x.io/a'>world</a> &amp; &lt;more&gt; caf&eacute;</p>"
            "<ul><li>one</li><li>two<ul><li>nested</li></ul></li></ul><ol><li>a</li><li>b</li></ol>"
            "<script>alert(1)</script></body></html>")
    assert html_to_text(html) == (
        "# Top\n\nHello **big** *wide* [world](https://x.io/a) & <more> café\n\n"
        "- one\n- two\n  - nested\n\n1. a\n2. b")


def test_tables_become_pipe_rows_and_escape_pipes():
    text = html_to_text("<table><tr><th>A</th><th>B</th></tr><tr><td>1</td><td>x | y<br>z</td></tr></table><p>after</p>")
    assert text == "| A | B |\n| --- | --- |\n| 1 | x \\| y z |\n\nafter"


def test_quotes_pre_code_hr_and_line_breaks():
    text = html_to_text("<blockquote><p>quoted</p></blockquote><pre>a\n  b</pre><p>use <code>x = 1</code></p>text<br>next<hr>end")
    assert "> quoted" in text and "```\na\n  b\n```" in text and "use `x = 1`" in text
    assert "text\nnext" in text and "---" in text and text.rstrip().endswith("end")


def test_links_without_text_or_with_unsafe_schemes_do_not_produce_empty_markdown():
    text = html_to_text("<a href='https://x.io'></a>[<a href='javascript:alert(1)'>click</a>] <a href='https://x.io'>https://x.io</a>")
    assert "[]" not in text.replace("[click]", "") and "javascript" not in text
    assert "click" in text and text.count("https://x.io") == 1


def test_outlook_style_html_with_styles_and_comments_is_readable():
    html = ("<html><head><style>.x{}</style></head><body><!-- generated --><div class=WordSection1>"
            "<p class=MsoNormal>Hi team,<o:p></o:p></p><p class=MsoNormal>Lunch at <b>noon</b>.</p>"
            "<div><br></div><div>Thanks</div></div></body></html>")
    assert html_to_text(html) == "Hi team,\n\nLunch at **noon**.\n\nThanks"


def test_malformed_markup_never_raises():
    assert "text" in html_to_text("<p>unclosed <b>text <ul><li>item")
    assert html_to_text("") == "" and html_to_text("   ") == ""
    assert html_to_text("<<<>>> a < b") != ""


def test_confluence_storage_format_elements():
    body = ('<ac:structured-macro ac:name="code"><ac:parameter ac:name="language">sql</ac:parameter>'
            "<ac:plain-text-body><![CDATA[select * from t where a < 1;]]></ac:plain-text-body></ac:structured-macro>"
            '<ac:structured-macro ac:name="info"><ac:rich-text-body><p>FYI</p></ac:rich-text-body></ac:structured-macro>'
            '<p><ac:link><ri:page ri:content-title="Other"/></ac:link> <ac:link><ri:page ri:content-title="P"/>'
            "<ac:plain-text-link-body><![CDATA[label]]></ac:plain-text-link-body></ac:link> "
            '<ac:emoticon ac:name="smile" ac:emoji-fallback=":)"/> <ac:image ac:alt="diagram"><ri:attachment ri:filename="d.png"/></ac:image> '
            '<time datetime="2026-01-02"/></p>')
    text = html_to_text(body)
    assert "```\nselect * from t where a < 1;\n```" in text
    assert "**Info:**" in text and "FYI" in text
    assert "Other label :) [image: diagram] 2026-01-02" in text


# ── parsers ──


def test_parse_dt_accepts_the_formats_the_vendors_use():
    expected = datetime(2026, 3, 4, 10, 11, 12, tzinfo=timezone.utc)
    assert parse_dt("2026-03-04T10:11:12Z") == expected
    assert parse_dt("2026-03-04T10:11:12.000+0000") == expected                 # Jira
    assert parse_dt("2026-03-04T11:11:12+01:00") == expected
    assert parse_dt("2026-03-04T10:11:12.1234567Z").microsecond == 123456        # more digits than fromisoformat likes
    assert parse_dt(expected.timestamp()) == expected and parse_dt(str(int(expected.timestamp()))) == expected
    assert parse_dt("2026-03-04T10:11:12") == expected                          # naive => UTC
    assert parse_dt(None) is None and parse_dt("") is None and parse_dt("yesterday") is None
    assert iso_z(parse_dt("2026-03-04T11:11:12+01:00")) == "2026-03-04T10:11:12Z"


def test_link_header_next_is_found_among_other_relations():
    header = '<https://api.example/x?page=1>; rel="prev", <https://api.example/x?page=3>; rel="next", <https://api.example/x?page=9>; rel="last"'
    assert parse_link_next(header) == "https://api.example/x?page=3"
    assert parse_link_next('<https://a/1>; rel="first", <https://a/9>; rel="last"') is None
    assert parse_link_next("") is None and parse_link_next(None) is None
    assert parse_link_next("<https://a/2>; rel=next") == "https://a/2"


def test_basic_auth_short_hash_and_redaction():
    assert basic_auth("a@b.c", "tok") == "Basic YUBiLmM6dG9r"
    assert short_hash("a", "b") == short_hash("a", "b") != short_hash("ab", "") and len(short_hash("x", size=8)) == 8
    err = redacted(HttpError("GET /x -> 500: token=SECRET123 failed", 500), "SECRET123", "", None, hint="look here")
    assert "SECRET123" not in str(err) and "token=*** failed (look here)" in str(err) and err.status == 500


# ── cursor protocol ──


def state(cursor=None, hours=24.0, clock=lambda: NOW):
    return ScanState(cursor or {}, full_resync_hours=hours, clock=clock)


def test_empty_cursor_is_a_full_authoritative_scan():
    s = state()
    assert s.mode == "full" and s.authoritative and not s.incremental and s.since is None
    assert s.complete_scope("", truncated=False) == "" and s.complete_scope("x:", truncated=False) == "x:"
    assert s.complete_scope("", truncated=True) is None


def test_final_cursor_of_a_full_scan_records_newest_timestamp_and_last_full():
    s = state()
    s.observe("2026-03-01T10:00:00Z")
    s.observe("2026-02-01T10:00:00Z")
    s.observe(None)
    assert s.final() == {"since": "2026-03-01T10:00:00Z", "last_full": NOW}
    assert state().final() == {"since": iso_z(datetime.fromtimestamp(NOW, tz=timezone.utc)), "last_full": NOW}   # empty source


def test_finished_cursor_makes_the_next_scan_incremental_until_the_full_resync_is_due():
    done = {"since": "2026-03-01T10:00:00Z", "last_full": NOW - 3600}
    s = state(done)
    assert s.incremental and s.since == "2026-03-01T10:00:00Z" and not s.authoritative
    assert s.complete_scope("", truncated=False) is None
    assert s.final()["last_full"] == NOW - 3600                                 # an incremental scan is not a full one
    assert state(done, hours=0.5).mode == "full"                                # due: last full was an hour ago
    assert state(done, hours=0).mode == "full"                                  # 0 => always full
    assert state({"since": "2026-03-01T10:00:00Z"}).mode == "full"             # never had a full scan
    assert state({"last_full": NOW}).mode == "full"                             # no watermark


def test_mid_scan_cursor_round_trips_and_resumes_in_the_same_mode():
    s = state()
    s.observe("2026-03-02T00:00:00Z")
    cursor = s.cursor(phase=2, next="https://x/next")
    assert cursor["pos"] == {"phase": 2, "next": "https://x/next"} and cursor["mode"] == "full"
    r = state(cursor)
    assert r.resumed and r.mode == "full" and r.pos == cursor["pos"] and not r.authoritative
    assert r.high == parse_dt("2026-03-02T00:00:00Z")                           # the watermark survives the crash
    assert r.final()["last_full"] is None                                      # a resumed pass is not credited as a full scan
    inc = state({"since": "2026-03-01T00:00:00Z", "last_full": NOW}).cursor(page=3)
    assert state(inc).mode == "inc" and state(inc).since == "2026-03-01T00:00:00Z"


def test_watermark_never_goes_backwards_and_truncation_does_not_advance_it():
    s = state({"since": "2026-03-05T00:00:00Z", "last_full": NOW})
    s.observe("2026-03-01T00:00:00Z")                                           # the overlap window returned old objects
    assert s.final()["since"] == "2026-03-05T00:00:00Z"
    s.observe("2026-03-08T00:00:00Z")
    assert s.final()["since"] == "2026-03-08T00:00:00Z"
    assert s.final(truncated=True) == {"since": "2026-03-05T00:00:00Z", "last_full": NOW}
    assert state().final(truncated=True) == {"last_full": None}


# ── bounded work ──


def test_budget_clip_and_pages():
    b = Budget(max_items=5)
    assert b.clip([1, 2, 3]) == [1, 2, 3] and not b.truncated
    assert b.clip([4, 5, 6]) == [4, 5] and b.truncated
    assert not b.allow_page()
    exact = Budget(max_items=3)
    assert exact.clip([1, 2, 3]) == [1, 2, 3] and not exact.truncated           # filled exactly and nothing more existed
    assert not exact.allow_page() and exact.truncated                            # ... but a further page does mean truncation
    p = Budget(max_pages=2)
    assert p.allow_page() and p.allow_page() and not p.allow_page() and p.truncated
    free = Budget()
    assert all(free.allow_page(i) for i in range(100)) and free.clip(list(range(1000))) and not free.truncated
    assert Budget.from_config({"max_items": 7, "max_pages": 3}).max_items == 7


def test_budget_stop_if_exhausted_marks_the_scan_truncated_only_when_work_remains():
    fresh = Budget(max_items=2, max_pages=2)
    assert not fresh.stop_if_exhausted() and not fresh.truncated
    items = Budget(max_items=2)
    items.clip([1, 2])
    assert not items.truncated and items.stop_if_exhausted() and items.truncated          # asked about further work: it is truncated
    pages = Budget(max_pages=1)
    assert pages.allow_page("a") and not pages.truncated
    assert pages.stop_if_exhausted() and pages.truncated
    assert not Budget().stop_if_exhausted()
    already = Budget(max_pages=1)
    already.allow_page("a")
    already.allow_page("b")
    assert already.truncated and already.stop_if_exhausted()


def test_budget_detects_a_page_token_that_repeats():
    b = Budget()
    assert b.allow_page("tok1") and b.allow_page("tok2")
    with pytest.raises(HttpError, match="did not advance"):
        b.allow_page("tok1")


# ── 403 as throttle ──


def test_throttle_wrapper_turns_rate_limit_403_into_a_retried_429_and_leaves_real_403_alone():
    calls = []

    def handler(request):
        calls.append(request)
        if request.url.path == "/denied":
            return httpx.Response(403, json={"message": "Resource not accessible"})
        return httpx.Response(403, json={"message": "API rate limit exceeded"}) if len(calls) < 3 else httpx.Response(200, json={"ok": 1})

    def classify(response):
        return 7.0 if "rate limit" in response.text else None

    sleeps = []
    http = Http("https://api.example", transport=ThrottleAs429(httpx.MockTransport(handler), classify), sleep=sleeps.append)
    assert http.get_json("/thing") == {"ok": 1} and sleeps == [7.0, 7.0]
    from app.scale.connectors._http import AuthError
    with pytest.raises(AuthError):
        http.get_json("/denied")
    assert sleeps == [7.0, 7.0]
