from __future__ import annotations

import logging
from typing import Any

from services.common.config import settings
from services.common.models import CleanDocument, SearchHit
from services.indexer.base import AbstractSearchIndex

logger = logging.getLogger(__name__)


class PgVectorIndex(AbstractSearchIndex):
    """PostgreSQL + pgvector semantic vector search adapter."""

    def __init__(self, postgres_url: str | None = None, dimension: int = 384) -> None:
        self.postgres_url = postgres_url or settings.postgres_url
        self.dimension = dimension
        self._pool: Any = None

    async def _init_db(self) -> None:
        try:
            import asyncpg  # type: ignore[import-not-found]
            from pgvector.asyncpg import register_vector  # type: ignore[import-not-found]

            if self._pool is None:
                self._pool = await asyncpg.create_pool(self.postgres_url)
                async with self._pool.acquire() as conn:
                    await conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
                    await register_vector(conn)
                    await conn.execute(f"""
                        CREATE TABLE IF NOT EXISTS agent_vectors (
                            id BIGSERIAL PRIMARY KEY,
                            passage_id TEXT UNIQUE,
                            url TEXT NOT NULL,
                            canonical_url TEXT NOT NULL,
                            title TEXT NOT NULL,
                            text TEXT NOT NULL,
                            embedding VECTOR({self.dimension}),
                            created_at TIMESTAMPTZ DEFAULT NOW()
                        )
                    """)
                    await conn.execute("""
                        CREATE INDEX IF NOT EXISTS idx_agent_vectors_hnsw
                        ON agent_vectors USING hnsw (embedding vector_cosine_ops)
                    """)
        except ImportError as err:
            raise RuntimeError(
                "asyncpg or pgvector is not installed. Install with `pip install asyncpg pgvector`"
            ) from err

    async def index_document(self, doc: CleanDocument) -> None:
        # Vector embedding insertion would insert each passage embedding here
        pass

    async def search(
        self,
        query: str,
        limit: int = 10,
        max_tokens: int = 1200,
        domains: list[str] | None = None,
    ) -> tuple[list[SearchHit], int]:
        return [], 0

    async def get_evidence(self, evidence_id: str) -> SearchHit | None:
        return None

    async def get_stats(self) -> dict[str, Any]:
        return {"backend": "pgvector", "dimension": self.dimension}
