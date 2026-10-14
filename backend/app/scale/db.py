"""Synchronous Postgres access for the scale package.

The worker, the search hook and the agent tools all run on plain threads, so this reuses the same
small psycopg2 engine the tree store already uses (rag_core/postgres_store.py) instead of opening a
second connection pool. The FastAPI side calls these helpers through asyncio.to_thread.
"""

from __future__ import annotations

from sqlalchemy.engine import Engine


def engine() -> Engine:
    from app.rag_core.postgres_store import _engine

    return _engine()
