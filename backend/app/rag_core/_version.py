"""Installed-package version, shared by every surface that reports it."""
from __future__ import annotations


def sdk_version() -> str:
    # Vendored -- no longer a separately pip-installed package, so there is
    # no importlib.metadata entry to read; this always falls through.
    return "vendored"
