"""
Local embedding model using sentence-transformers.
No API key or network required after the first download (~430 MB).
Produces 768-dimensional vectors (BAAI/bge-base-en-v1.5).

BGE asymmetric retrieval:
  - Ingestion: encode document text as-is.
  - Query:     prepend QUERY_PREFIX before encoding so the model
               finds relevant passages rather than similar sentences.
"""

from __future__ import annotations

import asyncio
from functools import lru_cache

import structlog

logger = structlog.get_logger(__name__)

_MODEL_NAME = "BAAI/bge-base-en-v1.5"
EMBEDDING_DIM = 768
QUERY_PREFIX = "Represent this sentence for searching relevant passages: "


@lru_cache(maxsize=1)
def _get_model():
    from sentence_transformers import SentenceTransformer
    logger.info("loading_local_embedding_model", model=_MODEL_NAME)
    return SentenceTransformer(_MODEL_NAME)


class LocalEmbedder:
    """
    Async wrapper around a local SentenceTransformer model.
    The model is loaded once and cached for the process lifetime.
    encode() is CPU-bound so it runs in a thread executor.
    """

    async def embed(self, text: str) -> list[float]:
        """Encode a document chunk for ingestion (no prefix)."""
        model = _get_model()
        loop = asyncio.get_event_loop()
        vector = await loop.run_in_executor(None, model.encode, text)
        return vector.tolist()

    async def embed_query(self, text: str) -> list[float]:
        """Encode a search query with the BGE retrieval prefix."""
        return await self.embed(QUERY_PREFIX + text)

    @property
    def dimension(self) -> int:
        return EMBEDDING_DIM
