"""
Thin Qdrant wrapper -- the only place in the app that imports qdrant_client
directly. Collection is named after the embedding model that fills it
(diagram_pages__<model>), so switching core/config.py's model_embedding
always points at a brand-new, empty collection rather than mixing two
models' vectors in the same index.
"""

import re
import uuid
from functools import lru_cache

from qdrant_client import AsyncQdrantClient, models as qmodels

from app.core.config import get_settings
from app.retrieval.embeddings import embedding_dimension


def _sanitize(model: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", model.lower()).strip("-")


def collection_name_for_model(model: str) -> str:
    return f"diagram_pages__{_sanitize(model)}"


def diagram_collection_name() -> str:
    return collection_name_for_model(get_settings().model_embedding)


@lru_cache
def get_qdrant_client() -> AsyncQdrantClient:
    settings = get_settings()
    return AsyncQdrantClient(url=settings.qdrant_url, api_key=settings.qdrant_api_key)


async def ensure_collection(collection_name: str) -> None:
    client = get_qdrant_client()
    if not await client.collection_exists(collection_name):
        await client.create_collection(
            collection_name=collection_name,
            vectors_config=qmodels.VectorParams(size=embedding_dimension(), distance=qmodels.Distance.COSINE),
        )


async def upsert(collection_name: str, point_id: uuid.UUID, vector: list[float], payload: dict) -> None:
    await ensure_collection(collection_name)
    client = get_qdrant_client()
    full_payload = {**payload, "embedding_model": get_settings().model_embedding}
    await client.upsert(
        collection_name=collection_name,
        points=[qmodels.PointStruct(id=str(point_id), vector=vector, payload=full_payload)],
    )


async def search(
    collection_name: str,
    query_vector: list[float],
    k: int = 5,
    document_ids: list[uuid.UUID] | None = None,
) -> list[dict]:
    """document_ids restricts hits to that set (matches the tree-search
    agent's own document allowlist) -- omitted, searches the whole
    collection, which is only correct when the caller has no selection to
    honor."""
    client = get_qdrant_client()
    if not await client.collection_exists(collection_name):
        return []
    query_filter = (
        qmodels.Filter(
            must=[qmodels.FieldCondition(
                key="document_id", match=qmodels.MatchAny(any=[str(d) for d in document_ids])
            )]
        )
        if document_ids
        else None
    )
    # AsyncQdrantClient.search() was removed in qdrant-client 1.10+ in favor
    # of query_points() -- confirmed by actually running this against the
    # installed 1.19.1 client (AttributeError), not assumed from docs; the
    # diagram vision-search path had never been exercised end-to-end before.
    response = await client.query_points(
        collection_name=collection_name, query=query_vector, limit=k, query_filter=query_filter
    )
    return [{"id": hit.id, "score": hit.score, **(hit.payload or {})} for hit in response.points]


async def delete_by_document(collection_name: str, document_id: uuid.UUID) -> None:
    client = get_qdrant_client()
    if not await client.collection_exists(collection_name):
        return
    await client.delete(
        collection_name=collection_name,
        points_selector=qmodels.FilterSelector(
            filter=qmodels.Filter(
                must=[qmodels.FieldCondition(key="document_id", match=qmodels.MatchValue(value=str(document_id)))]
            )
        ),
    )
