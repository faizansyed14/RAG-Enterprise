import logging

from fastapi import APIRouter
from sqlalchemy import text

from app.core.config import get_settings
from app.core.object_store import get_object_store
from app.models.db import async_session

router = APIRouter(prefix="/api/health", tags=["health"])
log = logging.getLogger(__name__)


@router.get("")
async def health() -> dict:
    """Public liveness/readiness. Values are only "ok" / "unavailable" -- the
    real error goes to the server log, never to an unauthenticated caller."""
    checks: dict[str, str] = {}

    try:
        async with async_session() as session:
            await session.execute(text("SELECT 1"))
        checks["postgres"] = "ok"
    except Exception:
        log.exception("health: postgres check failed")
        checks["postgres"] = "unavailable"

    try:
        get_object_store().ensure_bucket()
        checks["object_store"] = "ok"
    except Exception:
        log.exception("health: object store check failed")
        checks["object_store"] = "unavailable"

    try:
        from app.retrieval.vector_store import get_qdrant_client
        await get_qdrant_client().get_collections()
        checks["qdrant"] = "ok"
    except Exception:
        log.exception("health: qdrant check failed")
        checks["qdrant"] = "unavailable"

    if not get_settings().openrouter_api_key:
        log.error("health: OPENROUTER_API_KEY is not configured")
        checks["model_gateway"] = "unavailable"
    else:
        checks["model_gateway"] = "ok"

    status = "ok" if all(v == "ok" for v in checks.values()) else "degraded"
    return {"status": status, "checks": checks}
