import httpx
import pytest

from app.scale.connectors._http import AuthError, Http, HttpError, TooLarge, env_secret, parse_retry_after, render_document, safe_name


def make(handler, **kw):
    sleeps = []
    return Http("https://api.example.com", transport=httpx.MockTransport(handler), sleep=sleeps.append, **kw), sleeps


def test_retries_429_and_honours_retry_after():
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) < 3:
            return httpx.Response(429, headers={"Retry-After": "7"})
        return httpx.Response(200, json={"ok": True})

    http, sleeps = make(handler)
    assert http.get_json("/x") == {"ok": True}
    assert sleeps == [7.0, 7.0]


def test_auth_error_is_not_retried_and_hides_tokens():
    def handler(request):
        return httpx.Response(401, text="bad token")

    http, sleeps = make(handler, headers=None)
    with pytest.raises(AuthError) as err:
        http.get("/x?access_token=SECRET")
    assert "SECRET" not in str(err.value) and sleeps == []


def test_other_errors_carry_status():
    http, _ = make(lambda r: httpx.Response(404, text="nope"))
    with pytest.raises(HttpError) as err:
        http.get("/x")
    assert err.value.status == 404


def test_gives_up_after_max_retries():
    http, sleeps = make(lambda r: httpx.Response(503), max_retries=2)
    with pytest.raises(HttpError):
        http.get("/x", ok=(200,))
    assert len(sleeps) == 2


def test_download_streams_and_enforces_cap():
    http, _ = make(lambda r: httpx.Response(200, content=b"x" * 5000))
    with http.download("/f") as fh:
        assert fh.read() == b"x" * 5000
    with pytest.raises(TooLarge):
        http.download("/f", max_bytes=1000)


def test_download_retries_rate_limit():
    state = {"n": 0}

    def handler(request):
        state["n"] += 1
        return httpx.Response(429, headers={"Retry-After": "1"}) if state["n"] == 1 else httpx.Response(200, content=b"data")

    http, sleeps = make(handler)
    assert http.download("/f").read() == b"data" and sleeps == [1.0]


def test_env_secret(monkeypatch):
    monkeypatch.setenv("T_TOKEN", "abc")
    assert env_secret("T_TOKEN") == "abc"
    with pytest.raises(HttpError):
        env_secret("MISSING_VAR_X")
    with pytest.raises(HttpError):
        env_secret(None)


def test_parse_retry_after_forms():
    assert parse_retry_after("3", 1) == 3.0
    assert parse_retry_after(None, 5) == 5
    assert parse_retry_after("garbage", 5) == 5
    assert parse_retry_after("Wed, 21 Oct 2015 07:28:00 GMT", 5) == 0.0


def test_render_document_and_safe_name():
    body = render_document("Title", {"author": "a", "empty": ""}, "Body text").decode()
    assert body.startswith("# Title") and "author: a" in body and "empty" not in body and body.rstrip().endswith("Body text")
    assert safe_name('a/b:c*"d') == "a b c d" and safe_name("   ") == "untitled"
