"""Connectors configured from the server's environment: the screen only connects and disconnects."""

import pytest
from fastapi import HTTPException
from sqlalchemy import text

from app.scale import managed
from app.scale import sources_api as api
from app.scale.connectors import build_connector
from app.scale.db import engine

TENANT = "11111111-2222-3333-4444-555555555555"
CLIENT = "66666666-7777-8888-9999-000000000000"
SECRET_VALUE = "super-secret-value-that-must-never-appear"


@pytest.fixture
def sharepoint_env(monkeypatch):
    for name in managed.ALL_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SHAREPOINT_TENANT_ID", TENANT)
    monkeypatch.setenv("SHAREPOINT_CLIENT_ID", CLIENT)
    monkeypatch.setenv("SHAREPOINT_CLIENT_SECRET", SECRET_VALUE)
    monkeypatch.setenv("SHAREPOINT_SITES", "https://contoso.sharepoint.com/sites/Finance, https://contoso.sharepoint.com/sites/Legal")


class _FakeConnector:
    def __init__(self, ok=True):
        self.ok = ok

    def test(self):
        return {"ok": self.ok, "error": None if self.ok else "AADSTS7000215: invalid client secret", "notes": [], "drives": []}


def test_the_stored_marker_is_resolved_from_the_environment_and_never_carries_the_secret(sharepoint_env):
    cfg = managed.resolve("sharepoint", {"from_env": True})
    assert cfg["tenant_id"] == TENANT and cfg["client_id"] == CLIENT
    assert cfg["client_secret_env"] == "SHAREPOINT_CLIENT_SECRET"          # the NAME; the connector reads the value itself
    assert SECRET_VALUE not in repr(cfg)
    assert cfg["sites"] == ["https://contoso.sharepoint.com/sites/Finance", "https://contoso.sharepoint.com/sites/Legal"]
    assert cfg["access"] == {"mode": "source"}


def test_a_config_without_the_marker_is_left_alone():
    plain = {"tenant_id": "x", "client_id": "y", "client_secret_env": "Z"}
    assert managed.resolve("sharepoint", plain) is plain


def test_a_missing_variable_is_named_but_no_value_is_ever_shown(sharepoint_env, monkeypatch):
    monkeypatch.delenv("SHAREPOINT_CLIENT_ID")
    assert managed.problems("sharepoint") == ["SHAREPOINT_CLIENT_ID is not set"]
    with pytest.raises(ValueError, match="SHAREPOINT_CLIENT_ID"):
        managed.resolve("sharepoint", {"from_env": True})
    shown = repr(managed.status("sharepoint"))
    assert SECRET_VALUE not in shown and TENANT not in shown


def test_onedrive_needs_people_or_everyone(monkeypatch):
    for name in managed.ALL_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    for name, value in (("ONEDRIVE_TENANT_ID", TENANT), ("ONEDRIVE_CLIENT_ID", CLIENT), ("ONEDRIVE_CLIENT_SECRET", "s")):
        monkeypatch.setenv(name, value)
    assert any("ONEDRIVE_USERS" in p for p in managed.problems("onedrive"))
    monkeypatch.setenv("ONEDRIVE_USERS", "ana@contoso.com; bo@contoso.com")
    assert managed.resolve("onedrive", {"from_env": True})["users"] == ["ana@contoso.com", "bo@contoso.com"]
    monkeypatch.setenv("ONEDRIVE_ALL_USERS", "true")
    cfg = managed.resolve("onedrive", {"from_env": True})
    assert cfg["all_users"] is True and "users" not in cfg


def test_a_note_glued_onto_a_value_is_named_not_sent_to_microsoft(sharepoint_env, monkeypatch):
    monkeypatch.setenv("SHAREPOINT_SITES", "https://contoso.sharepoint.com/sites/Finance     (empty = every site)")
    assert managed.problems("sharepoint") == ["SHAREPOINT_SITES has text after its value; put notes on their own line starting with #"]


def test_an_unknown_access_mode_is_reported(sharepoint_env, monkeypatch):
    monkeypatch.setenv("SHAREPOINT_ACCESS", "whoever")
    assert any("SHAREPOINT_ACCESS" in p for p in managed.problems("sharepoint"))


def test_the_real_connector_is_built_from_the_marker_without_any_network(sharepoint_env):
    connector = build_connector("sharepoint", {"from_env": True})
    assert connector.kind == "sharepoint" and len(connector.site_targets) == 2


async def test_the_screen_lists_names_only(sharepoint_env):
    out = await api.managed_connectors(_admin=None)
    sp = next(c for c in out["connectors"] if c["kind"] == "sharepoint")
    assert sp["ready"] is True and sp["connection"] is None
    assert {"name": "SHAREPOINT_CLIENT_SECRET", "set": True, "required": True} in sp["env"]
    assert SECRET_VALUE not in repr(out) and TENANT not in repr(out)


async def test_connect_refuses_while_the_environment_is_incomplete(sharepoint_env, monkeypatch):
    monkeypatch.delenv("SHAREPOINT_CLIENT_SECRET")
    with pytest.raises(HTTPException) as err:
        await api.managed_connect("sharepoint", _admin=None)
    assert err.value.status_code == 422 and "SHAREPOINT_CLIENT_SECRET" in err.value.detail


async def test_connect_does_not_create_anything_when_microsoft_refuses_the_credentials(sharepoint_env, monkeypatch):
    monkeypatch.setattr(api, "build_connector", lambda kind, cfg: _FakeConnector(ok=False))
    with pytest.raises(HTTPException) as err:
        await api.managed_connect("sharepoint", _admin=None)
    assert err.value.status_code == 422 and "invalid client secret" in err.value.detail
    assert api._managed_row("sharepoint") is None


async def test_connect_disconnect_and_reconnect_use_one_source(sharepoint_env, monkeypatch):
    monkeypatch.setattr(api, "build_connector", lambda kind, cfg: _FakeConnector())
    first = await api.managed_connect("sharepoint", _admin=None)
    row = api._managed_row("sharepoint")
    assert row["enabled"] is True and row["connection_id"] == first["connection_id"] and first["scan_job"]
    with engine().connect() as conn:
        stored = conn.execute(text("SELECT config FROM source_connections WHERE connection_id = :c"), {"c": row["connection_id"]}).scalar_one()
    assert stored == {"from_env": True}                                    # nothing but the marker is stored

    await api.managed_disconnect("sharepoint", _admin=None)
    assert api._managed_row("sharepoint")["enabled"] is False

    again = await api.managed_connect("sharepoint", _admin=None)
    assert again["connection_id"] == first["connection_id"] and api._managed_row("sharepoint")["enabled"] is True
    with engine().connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM source_connections WHERE kind = 'sharepoint'")).scalar_one() == 1


async def test_disconnecting_something_never_connected_is_a_clear_404(sharepoint_env):
    with pytest.raises(HTTPException) as err:
        await api.managed_disconnect("sharepoint", _admin=None)
    assert err.value.status_code == 404
