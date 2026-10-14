"""Roles, the admin Users API, and the credit/limit engine -- real Postgres, no
mocking except the LLM stream (chat tests swap in a fake `stream_chat`).

Quota unit tests inject `now` so a 1-hour block can be verified without waiting.
"""

import asyncio
import hashlib
import uuid
from datetime import timedelta

import httpx
import pytest

from app.core.config import get_settings
from app.core.quota import QuotaExceeded, refund, reserve, utcnow
from app.main import app
from app.models.db import Document, User, async_session
from app.scale.acl import grant_everyone


def _client(headers: dict | None = None) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", headers=headers or {})


async def _reserve(user_id, now=None):
    async with async_session() as session:
        return await reserve(session, user_id, now=now)


async def _get_user(user_id) -> User:
    async with async_session() as session:
        return await session.get(User, user_id)


# --------------------------------------------------------------------------- quota engine


async def test_ten_messages_then_one_hour_block(make_user):
    user, _, _ = await make_user(credit_limit=100)
    cost = get_settings().chat_credit_cost
    now = utcnow()

    for n in range(1, 11):
        quota = await _reserve(user.user_id, now=now)
        assert quota.used == n * cost
        assert quota.messages_left == 10 - n

    # The 10th message emptied the allowance -> blocked immediately, for the full window.
    assert quota.blocked_until is not None
    assert quota.retry_after_seconds == get_settings().chat_block_seconds

    with pytest.raises(QuotaExceeded) as exc:
        await _reserve(user.user_id, now=now + timedelta(seconds=30))
    assert 3500 < exc.value.quota.retry_after_seconds <= 3570


async def test_allowance_refills_after_the_block(make_user):
    user, _, _ = await make_user(credit_limit=20)
    now = utcnow()
    await _reserve(user.user_id, now=now)
    await _reserve(user.user_id, now=now)
    with pytest.raises(QuotaExceeded):
        await _reserve(user.user_id, now=now)

    later = now + timedelta(seconds=get_settings().chat_block_seconds + 1)
    quota = await _reserve(user.user_id, now=later)
    assert quota.used == get_settings().chat_credit_cost  # fresh cycle, one message spent
    assert quota.blocked_until is None


async def test_concurrent_requests_cannot_overspend(make_user):
    user, _, _ = await make_user(credit_limit=100)

    async def attempt():
        try:
            await _reserve(user.user_id)
            return True
        except QuotaExceeded:
            return False

    results = await asyncio.gather(*[attempt() for _ in range(20)])
    assert sum(results) == 10
    assert (await _get_user(user.user_id)).credits_used == 100


async def test_refund_gives_the_message_back_and_unblocks(make_user):
    user, _, _ = await make_user(credit_limit=20)
    await _reserve(user.user_id)
    quota = await _reserve(user.user_id)
    assert quota.blocked_until is not None

    async with async_session() as session:
        quota = await refund(session, user.user_id)
    assert quota.messages_left == 1
    assert quota.blocked_until is None


# --------------------------------------------------------------------------- auth + roles


async def test_login_me_and_role_gating(make_user):
    user, _, password = await make_user(role="user")
    async with _client() as client:
        bad = await client.post("/api/auth/login", json={"username": user.username, "password": "nope"})
        assert bad.status_code == 401
        good = await client.post("/api/auth/login", json={"username": user.username.upper(), "password": password})
        assert good.status_code == 200
        headers = {"Authorization": f"Bearer {good.json()['token']}"}

        me = (await client.get("/api/auth/me", headers=headers)).json()
        assert me["role"] == "user"
        assert me["quota"]["messages_left"] == 10

        # Read-only endpoints the chat page needs: allowed.
        assert (await client.get("/api/documents", headers=headers)).status_code == 200
        assert (await client.get("/api/folders", headers=headers)).status_code == 200
        # Everything that manages things: admin only.
        assert (await client.post("/api/folders", json={"name": "x"}, headers=headers)).status_code == 403
        assert (await client.get("/api/users", headers=headers)).status_code == 403
        assert (await client.delete(f"/api/documents/{uuid.uuid4()}", headers=headers)).status_code == 403
        assert (await client.get("/api/documents")).status_code == 401


async def test_admin_can_manage_users_and_changes_revoke_sessions(make_user):
    _, admin_headers, _ = await make_user(role="admin")
    username = f"t_{uuid.uuid4().hex[:10]}"
    async with _client(admin_headers) as client:
        created = await client.post("/api/users", json={"username": username, "password": "first-pass-1"})
        assert created.status_code == 201
        body = created.json()
        assert body["role"] == "user" and body["credit_limit"] == 100
        assert "password" not in body and "password_hash" not in body
        user_id = body["user_id"]

        try:
            dup = await client.post("/api/users", json={"username": username.upper(), "password": "first-pass-1"})
            assert dup.status_code == 409
            weak = await client.post("/api/users", json={"username": "ab", "password": "short"})
            assert weak.status_code == 422
            too_low = await client.patch(f"/api/users/{user_id}", json={"credit_limit": 5})
            assert too_low.status_code == 400

            login = await client.post("/api/auth/login", json={"username": username, "password": "first-pass-1"})
            old_token = {"Authorization": f"Bearer {login.json()['token']}"}
            assert (await client.get("/api/auth/me", headers=old_token)).status_code == 200

            # New password: old password + already-issued tokens stop working.
            assert (await client.patch(f"/api/users/{user_id}", json={"password": "second-pass-2"})).status_code == 200
            assert (await client.get("/api/auth/me", headers=old_token)).status_code == 401
            assert (await client.post("/api/auth/login", json={"username": username, "password": "first-pass-1"})).status_code == 401
            assert (await client.post("/api/auth/login", json={"username": username, "password": "second-pass-2"})).status_code == 200

            # Disabled users cannot log in.
            assert (await client.patch(f"/api/users/{user_id}", json={"is_active": False})).status_code == 200
            assert (await client.post("/api/auth/login", json={"username": username, "password": "second-pass-2"})).status_code == 401
        finally:
            assert (await client.delete(f"/api/users/{user_id}")).status_code == 204


async def test_admin_allowance_change_blocks_and_unblocks(make_user):
    user, _, _ = await make_user(credit_limit=20)
    _, admin_headers, _ = await make_user(role="admin")
    await _reserve(user.user_id)
    await _reserve(user.user_id)  # allowance spent -> blocked

    async with _client(admin_headers) as client:
        raised = (await client.patch(f"/api/users/{user.user_id}", json={"credit_limit": 50})).json()
        assert raised["quota"]["blocked_until"] is None and raised["quota"]["messages_left"] == 3

        lowered = (await client.patch(f"/api/users/{user.user_id}", json={"credit_limit": 20})).json()
        assert lowered["quota"]["blocked_until"] is not None and lowered["quota"]["messages_left"] == 0

        reset = (await client.post(f"/api/users/{user.user_id}/reset-usage")).json()
        assert reset["quota"]["blocked_until"] is None and reset["quota"]["messages_left"] == 2


async def test_admin_cannot_delete_or_demote_self(make_user):
    admin, admin_headers, _ = await make_user(role="admin")
    async with _client(admin_headers) as client:
        assert (await client.delete(f"/api/users/{admin.user_id}")).status_code == 400
        assert (await client.patch(f"/api/users/{admin.user_id}", json={"role": "user"})).status_code == 400
        assert (await client.patch(f"/api/users/{admin.user_id}", json={"is_active": False})).status_code == 400


# --------------------------------------------------------------------------- chat metering


@pytest.fixture
async def indexed_document():
    document_id = uuid.uuid4()
    async with async_session() as session:
        session.add(Document(
            document_id=document_id, filename="quota-test.txt", doc_type="txt",
            content_hash=hashlib.sha256(str(document_id).encode()).hexdigest(),
            storage_key=f"originals/{document_id}/quota-test.txt",
            status="indexed", rag_doc_id=f"pi-{document_id}",
        ))
        await session.commit()
    grant_everyone(document_id)
    yield document_id
    async with async_session() as session:
        doc = await session.get(Document, document_id)
        if doc is not None:
            await session.delete(doc)
            await session.commit()


def _fake_stream(events):
    async def fake(query, rag_doc_ids, document_ids, history):
        for event in events:
            yield event
    return fake


async def test_chat_charges_reports_usage_and_blocks(make_user, indexed_document, monkeypatch):
    user, headers, _ = await make_user(credit_limit=20)
    monkeypatch.setattr("app.api.chat.stream_chat", _fake_stream([
        {"type": "answer", "delta": "hello"},
        {"type": "citations", "citations": [], "diagrams": [], "resolved_answer": "hello"},
        {"type": "done"},
    ]))
    body = {"query": "what does the report say?", "document_ids": [str(indexed_document)]}
    async with _client(headers) as client:
        first = await client.post("/api/chat", json=body)
        assert first.status_code == 200
        assert first.text.index("event: usage") < first.text.index("event: done")
        assert '"messages_left": 1' in first.text

        second = await client.post("/api/chat", json=body)
        assert '"messages_left": 0' in second.text and '"retry_after_seconds": 3600' in second.text

        third = await client.post("/api/chat", json=body)
        assert third.status_code == 429
        assert third.headers["retry-after"]
        detail = third.json()["detail"]
        assert detail["code"] == "limit_reached" and detail["retry_after_seconds"] > 3500


async def test_failed_chat_is_refunded(make_user, indexed_document, monkeypatch):
    user, headers, _ = await make_user(credit_limit=100)
    monkeypatch.setattr("app.api.chat.stream_chat", _fake_stream([
        {"type": "error", "message": "model exploded"},
        {"type": "done"},
    ]))
    async with _client(headers) as client:
        resp = await client.post("/api/chat", json={"query": "what does the report say?", "document_ids": [str(indexed_document)]})
    assert resp.status_code == 200
    assert '"messages_left": 10' in resp.text
    assert (await _get_user(user.user_id)).credits_used == 0


async def test_admin_chat_is_not_metered(make_user, indexed_document, monkeypatch):
    admin, headers, _ = await make_user(role="admin", credit_limit=10)
    monkeypatch.setattr("app.api.chat.stream_chat", _fake_stream([{"type": "answer", "delta": "x"}, {"type": "done"}]))
    async with _client(headers) as client:
        for _ in range(3):
            resp = await client.post("/api/chat", json={"query": "what does the report say?", "document_ids": [str(indexed_document)]})
            assert resp.status_code == 200 and "event: usage" not in resp.text
    assert (await _get_user(admin.user_id)).credits_used == 0


# --------------------------------------------------------------------------- the .env admin


async def _by_name(name: str) -> User | None:
    from sqlalchemy import func, select

    async with async_session() as session:
        return (await session.execute(select(User).where(func.lower(User.username) == name.lower()))).scalar_one_or_none()


async def test_env_admin_password_always_comes_from_env(monkeypatch):
    from app.core.bootstrap import ensure_bootstrap_admin
    from app.core.security import hash_password, verify_password

    name = f"envadmin_{uuid.uuid4().hex[:8]}"
    settings = get_settings()
    monkeypatch.setattr(settings, "admin_username", name)
    monkeypatch.setattr(settings, "admin_password", "first-env-password")
    try:
        await ensure_bootstrap_admin()
        user = await _by_name(name)
        assert user.role == "admin" and verify_password(user.password_hash, "first-env-password")
        version = user.token_version

        await ensure_bootstrap_admin()  # nothing changed -> nothing touched, sessions stay valid
        assert (await _by_name(name)).token_version == version

        # Changed behind .env's back (e.g. direct DB edit): the next start puts .env's password back.
        async with async_session() as session:
            row = await session.get(User, user.user_id)
            row.password_hash = hash_password("changed-elsewhere-1")
            row.role = "user"
            await session.commit()
        await ensure_bootstrap_admin()
        user = await _by_name(name)
        assert verify_password(user.password_hash, "first-env-password") and user.role == "admin"
        assert user.token_version == version + 1

        # Editing .env and restarting changes the password (and signs out old sessions).
        monkeypatch.setattr(settings, "admin_password", "second-env-password")
        await ensure_bootstrap_admin()
        user = await _by_name(name)
        assert verify_password(user.password_hash, "second-env-password")
        assert user.token_version == version + 2
    finally:
        async with async_session() as session:
            row = await _by_name(name)
            if row is not None:
                await session.delete(await session.get(User, row.user_id))
                await session.commit()


async def test_env_admin_cannot_be_edited_from_the_users_page(make_user, monkeypatch):
    _, admin_headers, _ = await make_user(role="admin")
    target, _, _ = await make_user(role="admin")
    monkeypatch.setattr(get_settings(), "admin_username", target.username)

    async with _client(admin_headers) as client:
        listed = {u["username"]: u for u in (await client.get("/api/users")).json()}
        assert listed[target.username]["managed_by_env"] is True

        for payload in (
            {"password": "a-brand-new-pass-1"},
            {"username": "somebody_else"},
            {"role": "user"},
            {"is_active": False},
        ):
            resp = await client.patch(f"/api/users/{target.user_id}", json=payload)
            assert resp.status_code == 400 and ".env" in resp.json()["detail"], payload
        assert (await client.delete(f"/api/users/{target.user_id}")).status_code == 400

        # The credit allowance is not identity/credentials, so it stays editable.
        assert (await client.patch(f"/api/users/{target.user_id}", json={"credit_limit": 50})).status_code == 200

        # Nobody else can take (or be renamed to) the reserved name.
        taken = await client.post("/api/users", json={"username": target.username.upper(), "password": "some-long-password"})
        assert taken.status_code == 409


# --------------------------------------------------------------------------- selection scope


async def test_chat_with_nothing_selected_is_rejected_not_widened_to_every_document(
    make_user, indexed_document, monkeypatch
):
    """"Deselect all" sends document_ids=[]. An empty list used to be falsy server-side, so the
    filter was skipped and the chat searched the WHOLE library. It must be refused instead --
    before the engine runs and before any credit is spent."""
    user, headers, _ = await make_user(credit_limit=100)

    def must_not_run(*args, **kwargs):
        raise AssertionError("the engine must not run when no document is selected")

    monkeypatch.setattr("app.api.chat.stream_chat", must_not_run)
    async with _client(headers) as client:
        resp = await client.post("/api/chat", json={"query": "what does the report say?", "document_ids": []})

    assert resp.status_code == 400
    assert "Select at least one document" in resp.json()["detail"]
    assert (await _get_user(user.user_id)).credits_used == 0


async def test_chat_with_no_selection_field_retrieves_across_everything_the_user_may_read(
    make_user, indexed_document, monkeypatch
):
    """Omitting document_ids (null) is "no selection made": the scope is not a reading list but the whole estate
    the user may read, so retrieval picks the candidates (selected_ids reaches resolve_scope as None)."""
    from app.scale.scope import ScopeRow

    _, headers, _ = await make_user(credit_limit=100)
    seen: dict = {}

    async def fake_scope(query, selected_ids, user, history=None):
        seen["selected"] = selected_ids
        return [ScopeRow(indexed_document, f"pi-{indexed_document}", "quota-test.txt")]

    async def fake(query, rag_doc_ids, document_ids, history):
        seen["ids"] = list(document_ids)
        yield {"type": "answer", "delta": "ok"}
        yield {"type": "done"}

    monkeypatch.setattr("app.api.chat.resolve_scope", fake_scope)
    monkeypatch.setattr("app.api.chat.stream_chat", fake)
    async with _client(headers) as client:
        resp = await client.post("/api/chat", json={"query": "what does the report say?"})

    assert resp.status_code == 200
    assert seen["selected"] is None
    assert indexed_document in seen["ids"]
