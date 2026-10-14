"""Exercises the app through ASGI directly -- /api/health reports per-dependency
failures rather than crash, so this verifies that without needing the dev stack up."""

import httpx
import pytest

from app.main import app


@pytest.mark.asyncio
async def test_health_reports_status_even_with_no_backing_services():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/health")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] in ("ok", "degraded")
    assert set(body["checks"].keys()) >= {"postgres", "object_store", "qdrant"}


@pytest.mark.asyncio
async def test_documents_list_requires_auth():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/documents")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_login_wrong_credentials_rejected():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/auth/login", json={"username": "admin", "password": "wrong"})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_login_correct_credentials_returns_token(make_user):
    user, _, password = await make_user(role="admin")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/auth/login", json={"username": user.username, "password": password})
    assert resp.status_code == 200
    assert "token" in resp.json()
