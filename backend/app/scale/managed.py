"""Connectors whose credentials come from the server's environment (.env), not from the screen.

A stored connection of one of these kinds holds only {"from_env": true}. Every time a connector is built (scan, worker, test) the real
settings are read from the environment, so changing .env and restarting the backend and worker is the whole procedure; nothing is copied
into the database and nothing secret passes through the browser.

  SHAREPOINT_TENANT_ID  SHAREPOINT_CLIENT_ID  SHAREPOINT_CLIENT_SECRET            required
  SHAREPOINT_SITES      comma-separated site addresses; empty = every site the app can see
  SHAREPOINT_LIBRARIES  comma-separated library names; empty = all
  SHAREPOINT_ACCESS     source (default: each file keeps its Microsoft 365 sharing) | everyone

  ONEDRIVE_TENANT_ID    ONEDRIVE_CLIENT_ID    ONEDRIVE_CLIENT_SECRET              required
  ONEDRIVE_USERS        comma-separated e-mail addresses, or
  ONEDRIVE_ALL_USERS    true = every enabled member account                        (one of the two is required)
  ONEDRIVE_ACCESS       source (default) | everyone
"""

from __future__ import annotations

import os
import re

LABELS = {"sharepoint": "SharePoint", "onedrive": "OneDrive"}
_PREFIX = {"sharepoint": "SHAREPOINT", "onedrive": "ONEDRIVE"}
_TRUE = {"1", "true", "yes", "on"}

# Names read from the environment for each managed kind: (required, optional)
_REQUIRED = ("TENANT_ID", "CLIENT_ID", "CLIENT_SECRET")
_OPTIONAL = {"sharepoint": ("SITES", "LIBRARIES", "ACCESS"), "onedrive": ("USERS", "ALL_USERS", "ACCESS")}


def is_managed(kind: str) -> bool:
    return kind in LABELS


def env_names(kind: str) -> tuple[list[str], list[str]]:
    """(required, optional) environment variable names for a managed kind."""
    p = _PREFIX[kind]
    return [f"{p}_{n}" for n in _REQUIRED], [f"{p}_{n}" for n in _OPTIONAL[kind]]


ALL_ENV_NAMES = frozenset(n for k in LABELS for part in env_names(k) for n in part)


def _list(value: str) -> list[str]:
    return [x.strip() for x in re.split(r"[\n,;]", value or "") if x.strip()]


def problems(kind: str) -> list[str]:
    """What is missing from the environment, as plain sentences. Empty = ready to connect. Names only, never values."""
    required, _ = env_names(kind)
    out = [f"{n} is not set" for n in required if not os.environ.get(n, "").strip()]
    if kind == "onedrive":
        users, all_users = (os.environ.get("ONEDRIVE_USERS", "").strip(), os.environ.get("ONEDRIVE_ALL_USERS", "").strip().lower())
        if not _list(users) and all_users not in _TRUE:
            out.append("set ONEDRIVE_USERS (e-mail addresses) or ONEDRIVE_ALL_USERS=true")
    required, optional = env_names(kind)
    for name in [*required, *optional]:
        # A .env file has no inline comments: "NAME=value   (a note)" makes the note part of the value.
        if re.search(r"\s{2,}\(|\s#", os.environ.get(name, "").strip()):
            out.append(f"{name} has text after its value; put notes on their own line starting with #")
    access = os.environ.get(f"{_PREFIX[kind]}_ACCESS", "").strip().lower()
    if access and access not in {"source", "everyone"}:
        out.append(f"{_PREFIX[kind]}_ACCESS must be 'source' or 'everyone'")
    return out


def status(kind: str) -> list[dict]:
    """For the screen: each variable's NAME and whether it is set. Values never leave the server."""
    required, optional = env_names(kind)
    return [{"name": n, "set": bool(os.environ.get(n, "").strip()), "required": True} for n in required] + [
        {"name": n, "set": bool(os.environ.get(n, "").strip()), "required": False} for n in optional
    ]


def resolve(kind: str, config: dict) -> dict:
    """The full connector configuration for a stored one. Anything without "from_env" is returned unchanged."""
    if not (config or {}).get("from_env"):
        return config
    if not is_managed(kind):
        raise ValueError(f"{kind!r} cannot be configured from the environment")
    issues = problems(kind)
    if issues:
        raise ValueError("the server's environment is incomplete: " + "; ".join(issues))
    p = _PREFIX[kind]
    env = os.environ
    cfg: dict = {
        "tenant_id": env[f"{p}_TENANT_ID"].strip(),
        "client_id": env[f"{p}_CLIENT_ID"].strip(),
        "client_secret_env": f"{p}_CLIENT_SECRET",            # the connector reads the value itself, when it needs it
        "access": {"mode": (env.get(f"{p}_ACCESS", "").strip().lower() or "source")},
    }
    if kind == "sharepoint":
        cfg["sites"] = _list(env.get("SHAREPOINT_SITES", ""))
        libraries = _list(env.get("SHAREPOINT_LIBRARIES", ""))
        if libraries:
            cfg["libraries"] = libraries
    elif env.get("ONEDRIVE_ALL_USERS", "").strip().lower() in _TRUE:
        cfg["all_users"] = True
    else:
        cfg["users"] = _list(env.get("ONEDRIVE_USERS", ""))
    return cfg
