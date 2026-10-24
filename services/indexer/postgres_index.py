from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any
from urllib.parse import urlsplit

import asyncpg
from pgvector import Vector
from pgvector.asyncpg import register_vector

from services.common.config import settings
from services.common.models import CleanDocument, SearchHit
from services.indexer.base import AbstractSearchIndex
from services.indexer.reranker import NeuralReranker
from services.indexer.semantic_embedder import SemanticEmbedder

logger = logging.getLogger(__name__)


class PostgresSearchIndex(AbstractSearchIndex):
    """High-performance PostgreSQL search backend with native pgvector and tsvector hybrid search."""

    def __init__(
        self,
        dsn: str | None = None,
        postgres_url: str | None = None,
        min_pool: int | None = None,
        max_pool: int | None = None,
    ) -> None:
        self.dsn = dsn or postgres_url or settings.postgres_url
        self.min_pool = min_pool or settings.postgres_pool_min_size
        self.max_pool = max_pool or settings.postgres_pool_max_size
        self.pool: asyncpg.Pool | None = None
        self._embedder = SemanticEmbedder()
        self._reranker = NeuralReranker()
        self._init_lock = asyncio.Lock()
        self._initialized = False

    async def _init_connection(self, conn: asyncpg.Connection) -> None:
        """Register the pgvector binary codec on each connection in the pool."""
        await register_vector(conn)

    async def get_pool(self) -> asyncpg.Pool:
        """Lazily initialize and return the asyncpg connection pool."""
        if self.pool is None:
            async with self._init_lock:
                if self.pool is None:
                    self.pool = await asyncpg.create_pool(
                        dsn=self.dsn,
                        min_size=self.min_pool,
                        max_size=self.max_pool,
                        init=self._init_connection,
                    )
                    await self._ensure_schema()
        return self.pool

    async def _ensure_schema(self) -> None:
        """Create extensions, tables, and vector/full-text indexes."""
        if self._initialized or self.pool is None:
            return

        async with self.pool.acquire() as conn:
            await conn.execute("CREATE EXTENSION IF NOT EXISTS vector;")
            await conn.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm;")

            # Documents Table
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS documents (
                    url TEXT PRIMARY KEY,
                    canonical_url TEXT NOT NULL,
                    title TEXT NOT NULL,
                    clean_text TEXT NOT NULL,
                    markdown TEXT NOT NULL,
                    domain TEXT NOT NULL DEFAULT '',
                    author TEXT,
                    published_date TEXT,
                    sitename TEXT,
                    description TEXT,
                    content_hash TEXT NOT NULL DEFAULT '',
                    simhash NUMERIC(20, 0) NOT NULL DEFAULT 0,
                    changed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    fetched_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                CREATE INDEX IF NOT EXISTS idx_documents_domain ON documents(domain);
                """
            )

            # Auto-migrate simhash column to NUMERIC(20, 0) if previously BIGINT
            with contextlib.suppress(Exception):
                await conn.execute("ALTER TABLE documents ALTER COLUMN simhash TYPE NUMERIC(20, 0);")

            # Passages Table with pgvector (384 dimensions) and generated tsvector
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS passages (
                    id BIGSERIAL PRIMARY KEY,
                    document_url TEXT NOT NULL REFERENCES documents(url) ON DELETE CASCADE,
                    ordinal INTEGER NOT NULL,
                    text TEXT NOT NULL,
                    evidence_id TEXT NOT NULL UNIQUE,
                    start_offset INTEGER NOT NULL DEFAULT 0,
                    end_offset INTEGER NOT NULL DEFAULT 0,
                    embedding vector(384),
                    tsv tsvector GENERATED ALWAYS AS (to_tsvector('english', text)) STORED,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                CREATE INDEX IF NOT EXISTS idx_passages_doc ON passages(document_url);
                CREATE INDEX IF NOT EXISTS idx_passages_tsv ON passages USING GIN(tsv);
                CREATE INDEX IF NOT EXISTS idx_passages_hnsw ON passages USING hnsw (embedding vector_cosine_ops);
                """
            )

            # URL Frontier Table
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS frontier_urls (
                    url TEXT PRIMARY KEY,
                    domain TEXT NOT NULL DEFAULT '',
                    source_url TEXT NOT NULL DEFAULT '',
                    depth INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'discovered',
                    discovered_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                CREATE INDEX IF NOT EXISTS idx_frontier_domain ON frontier_urls(domain);
                CREATE INDEX IF NOT EXISTS idx_frontier_status ON frontier_urls(status);
                """
            )

        self._initialized = True
        logger.info("PostgreSQL schema, pgvector HNSW index, and tsvector indexes verified.")

    async def index_document(self, doc: CleanDocument) -> None:
        """Persist document and compute/store dense passage embeddings in PostgreSQL."""
        pool = await self.get_pool()
        domain = (urlsplit(doc.url).hostname or "").lower()

        async with pool.acquire() as conn, conn.transaction():
            # 1. Upsert document
            await conn.execute(
                """
                    INSERT INTO documents (
                        url, canonical_url, title, clean_text, markdown, domain,
                        author, published_date, sitename, description, content_hash, simhash,
                        changed_at, fetched_at
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, NOW(), NOW())
                    ON CONFLICT (url) DO UPDATE SET
                        canonical_url = EXCLUDED.canonical_url,
                        title = EXCLUDED.title,
                        clean_text = EXCLUDED.clean_text,
                        markdown = EXCLUDED.markdown,
                        domain = EXCLUDED.domain,
                        author = EXCLUDED.author,
                        published_date = EXCLUDED.published_date,
                        sitename = EXCLUDED.sitename,
                        description = EXCLUDED.description,
                        changed_at = CASE
                            WHEN documents.content_hash != EXCLUDED.content_hash THEN NOW()
                            ELSE documents.changed_at
                        END,
                        content_hash = EXCLUDED.content_hash,
                        simhash = EXCLUDED.simhash,
                        fetched_at = NOW();
                    """,
                doc.url,
                doc.canonical_url,
                doc.title,
                doc.clean_text,
                doc.markdown,
                domain,
                doc.author,
                doc.published_date,
                doc.sitename,
                doc.description,
                doc.content_hash,
                int(doc.simhash or 0),
            )

            # 2. Delete existing passages for this document
            await conn.execute("DELETE FROM passages WHERE document_url = $1", doc.url)

            # 3. Embed passages and batch insert with vectors
            if doc.passages:
                passage_texts = [p.text for p in doc.passages]
                embeddings = self._embedder.embed_passages(passage_texts)

                records: list[tuple[Any, ...]] = []
                for p, emb in zip(doc.passages, embeddings, strict=False):
                    vec = Vector(emb)
                    ordinal = getattr(p, "order_index", getattr(p, "ordinal", 0))
                    evidence_id = getattr(p, "passage_id", getattr(p, "evidence_id", ""))
                    start_offset = getattr(p, "start_char", getattr(p, "start_offset", 0))
                    end_offset = getattr(p, "end_char", getattr(p, "end_offset", len(p.text)))
                    records.append(
                        (
                            doc.url,
                            ordinal,
                            p.text,
                            evidence_id,
                            start_offset,
                            end_offset,
                            vec,
                        )
                    )

                await conn.executemany(
                    """
                        INSERT INTO passages (
                            document_url, ordinal, text, evidence_id,
                            start_offset, end_offset, embedding
                        ) VALUES ($1, $2, $3, $4, $5, $6, $7)
                        """,
                    records,
                )

            # 4. Update frontier table status to crawled
            await conn.execute(
                """
                    INSERT INTO frontier_urls (url, domain, source_url, depth, status, discovered_at)
                    VALUES ($1, $2, $1, 0, 'crawled', NOW())
                    ON CONFLICT (url) DO UPDATE SET status = 'crawled';
                    """,
                doc.url,
                domain,
            )

    async def search(
        self,
        query: str,
        limit: int = 10,
        max_tokens: int = 1200,
        domains: list[str] | None = None,
        hybrid: bool = True,
    ) -> tuple[list[SearchHit], int]:
        """Execute hybrid search combining PostgreSQL tsvector and pgvector HNSW distance."""
        pool = await self.get_pool()
        query_str = query.strip()
        if not query_str:
            return [], 0

        fetch_limit = max(limit * 3, 25)

        async with pool.acquire() as conn:
            # 1. Lexical full-text search
            domain_filter = ""
            params: list[Any] = [query_str, fetch_limit]
            if domains:
                domain_filter = "AND d.domain = ANY($3)"
                params.append(domains)

            lexical_sql = f"""
                SELECT p.evidence_id, p.document_url, d.canonical_url, d.title, p.text,
                       d.author, d.published_date,
                       ts_rank_cd(p.tsv, websearch_to_tsquery('english', $1)) AS score
                FROM passages p
                JOIN documents d ON p.document_url = d.url
                WHERE (p.tsv @@ websearch_to_tsquery('english', $1) OR p.text ILIKE '%' || $1 || '%')
                {domain_filter}
                ORDER BY score DESC
                LIMIT $2;
            """
            lexical_rows = await conn.fetch(lexical_sql, *params)

            # 2. Vector semantic search via pgvector
            vector_rows = []
            if hybrid:
                query_emb = self._embedder.embed_query(query_str)
                if query_emb is not None:
                    vec_list: list[float] = (
                        query_emb.tolist() if hasattr(query_emb, "tolist") else list(query_emb)
                    )
                    query_vec = Vector(vec_list)
                    vec_params: list[Any] = [query_vec, fetch_limit]
                    vec_domain_filter = ""
                    if domains:
                        vec_domain_filter = "AND d.domain = ANY($3)"
                        vec_params.append(domains)

                    vector_sql = f"""
                        SELECT p.evidence_id, p.document_url, d.canonical_url, d.title, p.text,
                               d.author, d.published_date,
                               1 - (p.embedding <=> $1) AS similarity
                        FROM passages p
                        JOIN documents d ON p.document_url = d.url
                        WHERE p.embedding IS NOT NULL
                        {vec_domain_filter}
                        ORDER BY p.embedding <=> $1 ASC
                        LIMIT $2;
                    """
                    vector_rows = await conn.fetch(vector_sql, *vec_params)

        # 3. Reciprocal Rank Fusion (RRF, k=60)
        rrf_scores: dict[str, float] = {}
        row_map: dict[str, Any] = {}

        for rank, row in enumerate(lexical_rows):
            eid = row["evidence_id"]
            rrf_scores[eid] = rrf_scores.get(eid, 0.0) + (1.0 / (60.0 + rank + 1))
            row_map[eid] = row

        for rank, row in enumerate(vector_rows):
            eid = row["evidence_id"]
            rrf_scores[eid] = rrf_scores.get(eid, 0.0) + (1.0 / (60.0 + rank + 1))
            row_map[eid] = row

        # Sort candidates by combined RRF score
        sorted_eids = sorted(rrf_scores.keys(), key=lambda e: rrf_scores[e], reverse=True)

        hits: list[SearchHit] = []
        total_tokens = 0
        for eid in sorted_eids:
            row = row_map[eid]
            text = row["text"]
            # Estimate tokens: ~4 chars per token
            est_tokens = max(1, len(text) // 4)

            if total_tokens + est_tokens > max_tokens and hits:
                break

            hits.append(
                SearchHit(
                    evidence_id=eid,
                    url=row["document_url"],
                    canonical_url=row["canonical_url"],
                    title=row["title"] or "",
                    text=text,
                    score=float(rrf_scores[eid]),
                    estimated_tokens=est_tokens,
                    match_type="hybrid" if hybrid and vector_rows else "lexical",
                    author=row["author"],
                    published_date=row["published_date"],
                )
            )
            total_tokens += est_tokens
            if len(hits) >= limit:
                break

        return hits, total_tokens

    async def get_evidence(self, evidence_id: str) -> SearchHit | None:
        """Retrieve single evidence passage by ID."""
        pool = await self.get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT p.evidence_id, p.document_url, d.canonical_url, d.title, p.text,
                       d.author, d.published_date
                FROM passages p
                JOIN documents d ON p.document_url = d.url
                WHERE p.evidence_id = $1
                """,
                evidence_id,
            )
            if not row:
                return None
            return SearchHit(
                evidence_id=row["evidence_id"],
                url=row["document_url"],
                canonical_url=row["canonical_url"],
                title=row["title"] or "",
                text=row["text"],
                score=1.0,
                estimated_tokens=max(1, len(row["text"]) // 4),
                match_type="lexical",
                author=row["author"],
                published_date=row["published_date"],
            )

    async def get_stats(self) -> dict[str, Any]:
        """Return PostgreSQL database metrics and index status."""
        pool = await self.get_pool()
        async with pool.acquire() as conn:
            doc_count = await conn.fetchval("SELECT count(*) FROM documents")
            passage_count = await conn.fetchval("SELECT count(*) FROM passages")
            domain_count = await conn.fetchval("SELECT count(DISTINCT domain) FROM documents")
            db_size = await conn.fetchval("SELECT pg_database_size(current_database())")

            return {
                "backend": "postgresql",
                "exists": True,
                "database_name": "agent_search",
                "size_bytes": db_size or 0,
                "total_documents": doc_count or 0,
                "document_count": doc_count or 0,
                "total_passages": passage_count or 0,
                "passage_count": passage_count or 0,
                "total_domains": domain_count or 0,
                "pgvector_enabled": True,
            }

    async def record_frontier_urls(self, records: list[dict[str, Any]]) -> int:
        """Persist newly discovered URLs in the frontier ledger."""
        if not records:
            return 0
        pool = await self.get_pool()
        items: list[tuple[Any, ...]] = []
        for r in records:
            url = r["url"]
            domain = r.get("domain") or (urlsplit(url).hostname or "").lower()
            source = r.get("source_url", "")
            depth = r.get("depth", 0)
            status = r.get("status", "discovered")
            items.append((url, domain, source, depth, status))

        async with pool.acquire() as conn:
            await conn.executemany(
                """
                INSERT INTO frontier_urls (url, domain, source_url, depth, status, discovered_at)
                VALUES ($1, $2, $3, $4, $5, NOW())
                ON CONFLICT (url) DO NOTHING;
                """,
                items,
            )
        return len(items)

    async def update_frontier_status(self, url: str, status: str) -> None:
        """Update crawl status of a URL in the frontier ledger."""
        pool = await self.get_pool()
        async with pool.acquire() as conn:
            await conn.execute("UPDATE frontier_urls SET status = $1 WHERE url = $2", status, url)

    async def get_frontier_stats(self) -> dict[str, Any]:
        """Return frontier ledger summary statistics."""
        pool = await self.get_pool()
        async with pool.acquire() as conn:
            total = await conn.fetchval("SELECT count(*) FROM frontier_urls") or 0
            domains = await conn.fetchval("SELECT count(DISTINCT domain) FROM frontier_urls") or 0
            discovered = (
                await conn.fetchval("SELECT count(*) FROM frontier_urls WHERE status = 'discovered'") or 0
            )
            queued = await conn.fetchval("SELECT count(*) FROM frontier_urls WHERE status = 'queued'") or 0
            crawled = await conn.fetchval("SELECT count(*) FROM frontier_urls WHERE status = 'crawled'") or 0

            return {
                "total_discovered": total,
                "total_domains": domains,
                "discovered": discovered,
                "queued": queued,
                "crawled": crawled,
            }

    async def get_frontier_urls(
        self,
        status: str | None = None,
        domain: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Retrieve recent discovered frontier URLs."""
        pool = await self.get_pool()
        query = "SELECT url, domain, source_url, depth, status, discovered_at FROM frontier_urls WHERE 1=1"
        params: list[Any] = []
        idx = 1
        if status:
            query += f" AND status = ${idx}"
            params.append(status)
            idx += 1
        if domain:
            query += f" AND domain = ${idx}"
            params.append(domain)
            idx += 1
        query += f" ORDER BY discovered_at DESC LIMIT ${idx}"
        params.append(limit)

        async with pool.acquire() as conn:
            rows = await conn.fetch(query, *params)
            return [
                {
                    "url": r["url"],
                    "domain": r["domain"],
                    "source_url": r["source_url"],
                    "depth": r["depth"],
                    "status": r["status"],
                    "discovered_at": r["discovered_at"].isoformat() if r["discovered_at"] else "",
                }
                for r in rows
            ]

    async def get_pending_frontier_urls(
        self, limit: int = 50, max_per_domain: int = 10
    ) -> list[dict[str, Any]]:
        """Retrieve pending frontier URLs partitioned by domain."""
        pool = await self.get_pool()
        query = """
            SELECT url, domain, source_url, depth, status, discovered_at
            FROM (
                SELECT url, domain, source_url, depth, status, discovered_at,
                       ROW_NUMBER() OVER(PARTITION BY domain ORDER BY discovered_at ASC) as rn
                FROM frontier_urls
                WHERE status = 'discovered'
            ) sub
            WHERE rn <= $1
            ORDER BY discovered_at ASC
            LIMIT $2;
        """
        async with pool.acquire() as conn:
            rows = await conn.fetch(query, max_per_domain, limit)
            return [
                {
                    "url": r["url"],
                    "domain": r["domain"],
                    "source_url": r["source_url"],
                    "depth": r["depth"],
                    "status": r["status"],
                    "discovered_at": r["discovered_at"].isoformat() if r["discovered_at"] else "",
                }
                for r in rows
            ]

    async def ping(self) -> bool:
        """Check PostgreSQL connectivity."""
        try:
            pool = await self.get_pool()
            val = await pool.fetchval("SELECT 1")
            return bool(val == 1)
        except Exception as exc:
            logger.warning("Postgres health check ping failed: %s", exc)
            return False

    async def close(self) -> None:
        """Close connection pool."""
        if self.pool is not None:
            await self.pool.close()
            self.pool = None
