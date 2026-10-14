import uuid

import pytest

from app.retrieval.vector_store import delete_by_document, get_qdrant_client, search, upsert

# Real Qdrant integration tests (network access to the qdrant service,
# same as production) -- not covered before: query_points() (the
# AsyncQdrantClient 1.19.1 replacement for the removed .search()) and the
# document_ids scoping filter were both previously unverified end-to-end.
_COLLECTION = "test_vector_store_scoping"


@pytest.mark.asyncio
async def test_search_document_ids_filter_scopes_results():
    # get_qdrant_client() is @lru_cache'd -- fine in the real app (one
    # event loop for the process's lifetime), but pytest-asyncio gives
    # each test function its own event loop, so a client cached by an
    # earlier test binds to an already-closed loop here. Clearing it
    # forces a fresh client on this loop.
    get_qdrant_client.cache_clear()
    doc_a, doc_b = uuid.uuid4(), uuid.uuid4()
    point_a, point_b = uuid.uuid4(), uuid.uuid4()
    vec = [0.1] * 1536

    await upsert(_COLLECTION, point_a, vec, {"document_id": str(doc_a), "caption": "from A"})
    await upsert(_COLLECTION, point_b, vec, {"document_id": str(doc_b), "caption": "from B"})

    try:
        unfiltered = await search(_COLLECTION, vec, k=10)
        captions = {h["caption"] for h in unfiltered if h["document_id"] in (str(doc_a), str(doc_b))}
        assert captions == {"from A", "from B"}

        filtered = await search(_COLLECTION, vec, k=10, document_ids=[doc_a])
        assert {h["caption"] for h in filtered} == {"from A"}
    finally:
        await delete_by_document(_COLLECTION, doc_a)
        await delete_by_document(_COLLECTION, doc_b)
