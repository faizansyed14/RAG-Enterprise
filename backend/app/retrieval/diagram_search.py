"""Diagram/drawing vector search -- the query-time half of the vision path."""

import uuid

from app.retrieval.vector_store import diagram_collection_name, search as qdrant_search


async def diagram_search(
    query_embedding: list[float], k: int = 5, document_ids: list[uuid.UUID] | None = None
) -> list[dict]:
    return await qdrant_search(diagram_collection_name(), query_embedding, k=k, document_ids=document_ids)
