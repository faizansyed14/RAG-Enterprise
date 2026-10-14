"""
The one pinned embedding model (core/config.py: model_embedding), used
everywhere something needs a vector for Qdrant. OpenRouter's embeddings
endpoint is text-only, so diagram vectors embed the vision model's caption
+ structured description + OCR text (see ingestion/diagram_pipeline.py),
never raw image bytes.
"""

from functools import lru_cache

from openai import AsyncOpenAI

from app.core.config import get_settings


@lru_cache
def _client() -> AsyncOpenAI:
    settings = get_settings()
    return AsyncOpenAI(base_url=settings.openrouter_base_url, api_key=settings.openrouter_api_key)


def embedding_dimension() -> int:
    return get_settings().model_embedding_dimension


async def embed_text(text: str) -> list[float]:
    settings = get_settings()
    resp = await _client().embeddings.create(model=settings.model_embedding, input=text or " ")
    return resp.data[0].embedding
