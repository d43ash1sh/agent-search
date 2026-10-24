from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncGenerator

import pytest

from services.common.models import CleanDocument, PassageChunk
from services.indexer.postgres_index import PostgresSearchIndex

POSTGRES_TEST_URL = "postgresql://postgres:postgres@127.0.0.1:5432/agent_search"


async def is_postgres_available() -> bool:
    try:
        import asyncpg

        conn = await asyncio.wait_for(asyncpg.connect(POSTGRES_TEST_URL), timeout=3.0)
        await conn.close()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not asyncio.run(is_postgres_available()),
    reason="PostgreSQL is not running on 127.0.0.1:5432 or credentials mismatch",
)


@pytest.fixture
async def pg_index() -> AsyncGenerator[PostgresSearchIndex, None]:
    index = PostgresSearchIndex(postgres_url=POSTGRES_TEST_URL)
    yield index
    await index.close()


@pytest.mark.anyio
async def test_postgres_ping_and_stats(pg_index: PostgresSearchIndex) -> None:
    ok = await pg_index.ping()
    assert ok is True

    stats = await pg_index.get_stats()
    assert "document_count" in stats
    assert "passage_count" in stats
    assert "backend" in stats
    assert "postgres" in stats["backend"]


@pytest.mark.anyio
async def test_postgres_index_and_search(pg_index: PostgresSearchIndex) -> None:
    unique_suffix = uuid.uuid4().hex[:8]
    doc_url = f"https://postgres-test.example.com/guide-{unique_suffix}"
    passage_id = f"ev_pg_{unique_suffix}"

    doc = CleanDocument(
        url=doc_url,
        canonical_url=doc_url,
        title=f"PostgreSQL Distributed Guide {unique_suffix}",
        clean_text="PostgreSQL with pgvector allows fast approximate nearest neighbor search and BM25-like tsvector ranking.",
        passages=[
            PassageChunk(
                passage_id=passage_id,
                document_url=doc_url,
                order_index=0,
                start_char=0,
                end_char=95,
                text="PostgreSQL with pgvector allows fast approximate nearest neighbor search and BM25-like tsvector ranking.",
                estimated_tokens=22,
                content_hash=f"hash_{unique_suffix}",
            )
        ],
    )

    # Index document
    await pg_index.index_document(doc)

    # Retrieve by evidence id
    evidence = await pg_index.get_evidence(passage_id)
    assert evidence is not None
    assert evidence.evidence_id == passage_id
    assert "pgvector" in evidence.text

    # Search with hybrid=False (lexical tsvector)
    hits_lexical, tokens = await pg_index.search(query="approximate nearest neighbor", limit=5)
    assert len(hits_lexical) >= 1
    assert any(h.evidence_id == passage_id for h in hits_lexical)
    assert tokens > 0

    # Search with hybrid=True (dense vector + lexical RRF)
    hits_hybrid, _ = await pg_index.search(query="pgvector approximate neighbor", limit=5, hybrid=True)
    assert len(hits_hybrid) >= 1
    assert any(h.evidence_id == passage_id for h in hits_hybrid)


@pytest.mark.anyio
async def test_postgres_unsigned_64bit_simhash(pg_index: PostgresSearchIndex) -> None:
    large_simhash = 14720958193186661231
    doc_url = f"https://example.com/large-simhash-{uuid.uuid4().hex[:6]}"
    doc = CleanDocument(
        url=doc_url,
        canonical_url=doc_url,
        title="Large SimHash Test",
        clean_text="SimHash values in Python can reach up to 2^64-1 without overflowing PostgreSQL numeric.",
        simhash=large_simhash,
        content_hash="hash_simhash_test",
    )
    await pg_index.index_document(doc)
    # Re-index to test ON CONFLICT UPDATE path with large simhash
    await pg_index.index_document(doc)


@pytest.mark.anyio
async def test_postgres_frontier_management(pg_index: PostgresSearchIndex) -> None:
    test_id = uuid.uuid4().hex[:6]
    records = [
        {
            "url": f"https://domain-{test_id}-a.com/page1",
            "domain": f"domain-{test_id}-a.com",
            "source_url": "https://seed.com",
            "depth": 1,
        },
        {
            "url": f"https://domain-{test_id}-a.com/page2",
            "domain": f"domain-{test_id}-a.com",
            "source_url": "https://seed.com",
            "depth": 1,
        },
        {
            "url": f"https://domain-{test_id}-b.com/item1",
            "domain": f"domain-{test_id}-b.com",
            "source_url": "https://seed.com",
            "depth": 2,
        },
    ]

    inserted = await pg_index.record_frontier_urls(records)
    assert inserted >= 3

    # Check stats
    stats = await pg_index.get_frontier_stats()
    assert stats["total_discovered"] >= 3
    assert stats["total_domains"] >= 2

    # Query frontier by domain
    domain_a_urls = await pg_index.get_frontier_urls(domain=f"domain-{test_id}-a.com")
    assert len(domain_a_urls) == 2

    # Test pending URLs partitioning
    pending = await pg_index.get_pending_frontier_urls(limit=10, max_per_domain=1)
    # With max_per_domain=1, each domain should contribute at most 1 URL
    test_domains_in_pending = [
        item["domain"]
        for item in pending
        if item["domain"] in {f"domain-{test_id}-a.com", f"domain-{test_id}-b.com"}
    ]
    assert test_domains_in_pending.count(f"domain-{test_id}-a.com") <= 1

    # Update status
    await pg_index.update_frontier_status(str(records[0]["url"]), "crawled")
    updated_records = await pg_index.get_frontier_urls(domain=f"domain-{test_id}-a.com", status="crawled")
    assert len(updated_records) >= 1
    assert updated_records[0]["url"] == records[0]["url"]
