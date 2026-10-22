from __future__ import annotations

import tempfile

import pytest
from httpx import ASGITransport, AsyncClient

from services.api.routes import create_api_router
from services.common.citations import CitationFormatter
from services.common.models import CleanDocument, PassageChunk, SearchHit
from services.fetcher.tls_client import TlsImpersonationFetcher
from services.indexer.reranker import NeuralReranker
from services.indexer.sqlite_index import SQLiteSearchIndex
from services.queue.memory_broker import InMemoryQueueBroker


class TestAdvancedAgentFeatures:
    def test_citation_formatter_formats_markdown_and_xml(self) -> None:
        hits = [
            SearchHit(
                evidence_id="ev_001",
                url="https://example.com/docs",
                canonical_url="https://example.com/docs",
                title="Distributed Systems Guide",
                text="Redis Streams support consumer groups for distributed message delivery.",
                score=0.985,
                estimated_tokens=15,
            ),
            SearchHit(
                evidence_id="ev_002",
                url="https://example.com/postgres",
                canonical_url="https://example.com/postgres",
                title="PostgreSQL pgvector",
                text="HNSW indexes enable approximate nearest neighbor search over dense embeddings.",
                score=0.875,
                estimated_tokens=16,
            ),
        ]

        # Markdown footnotes
        md = CitationFormatter.format_markdown(hits)
        assert "[^1]: [Distributed Systems Guide](https://example.com/docs)" in md
        assert "[^2]: [PostgreSQL pgvector](https://example.com/postgres)" in md
        assert "ev_001" in md

        # XML Grounding
        xml = CitationFormatter.format_xml(hits)
        assert "<search_grounding>" in xml
        assert '<evidence id="ev_001"' in xml
        assert "CDATA[Redis Streams support" in xml

        # Agent prompt context
        prompt = CitationFormatter.format_agent_prompt_context(hits, "How do streams work?")
        assert "User Question: How do streams work?" in prompt
        assert "Strict Instruction: Always cite your claims" in prompt

    def test_neural_reranker_reorders_by_cross_encoder(self) -> None:
        reranker = NeuralReranker()
        query = "How does Redis handle persistent snapshots?"

        passage_irrelevant = SearchHit(
            evidence_id="ev_irr",
            url="https://example.com/overview",
            canonical_url="https://example.com/overview",
            title="Database Comparison",
            text="Relational databases use B-Trees while document stores use JSON.",
            score=0.5,
            estimated_tokens=20,
        )
        passage_relevant = SearchHit(
            evidence_id="ev_rel",
            url="https://example.com/redis-rdb",
            canonical_url="https://example.com/redis-rdb",
            title="Redis Persistence Details",
            text="Redis provides RDB snapshots which write point-in-time datasets to disk via fork.",
            score=0.4,  # Intentionally lower initial lexical score
            estimated_tokens=22,
        )

        hits = [passage_irrelevant, passage_relevant]
        reranked, tokens = reranker.rerank(query, hits, max_tokens=500)

        assert len(reranked) == 2
        # Neural cross-encoder must score the relevant snapshot passage higher than the generic comparison
        assert reranked[0].evidence_id == "ev_rel"
        assert reranked[0].score > reranked[1].score
        assert tokens == 42

    @pytest.mark.anyio
    async def test_tls_impersonation_fetcher_initialization(self) -> None:
        fetcher = TlsImpersonationFetcher(browser="chrome124", timeout=5.0)
        session = await fetcher.get_session()
        assert session is not None
        await fetcher.close()

    @pytest.mark.anyio
    async def test_grounded_and_streaming_api_endpoints(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = f"{tmp_dir}/test_api_endpoints.sqlite3"
            broker = InMemoryQueueBroker()
            index = SQLiteSearchIndex(database_path=db_path)

            try:
                # Seed a test document
                doc = CleanDocument(
                    url="https://docs.example.com/streams",
                    canonical_url="https://docs.example.com/streams",
                    title="Redis Streams Manual",
                    clean_text="Redis streams provide append-only logs for distributed agents.",
                    passages=[
                        PassageChunk(
                            passage_id="ev_streams_01",
                            document_url="https://docs.example.com/streams",
                            order_index=0,
                            start_char=0,
                            end_char=60,
                            text="Redis streams provide append-only logs for distributed agents.",
                            estimated_tokens=15,
                            content_hash="h123",
                        )
                    ],
                )
                await index.index_document(doc)

                from fastapi import FastAPI

                app = FastAPI()
                app.include_router(create_api_router(broker, index))

                async with AsyncClient(
                    transport=ASGITransport(app=app), base_url="http://testserver"
                ) as client:
                    # Test /v1/search/grounded
                    grounded_resp = await client.post(
                        "/v1/search/grounded",
                        json={"query": "Redis streams", "rerank": True},
                    )
                    assert grounded_resp.status_code == 200
                    data = grounded_resp.json()
                    assert len(data["results"]) >= 1
                    assert "Redis Streams Manual" in data["markdown_citations"]
                    assert "<search_grounding>" in data["xml_grounding"]

                    # Test /v1/search/stream (SSE)
                    stream_resp = await client.get("/v1/search/stream?query=Redis+streams")
                    assert stream_resp.status_code == 200
                    assert "text/event-stream" in stream_resp.headers["content-type"]
                    body = stream_resp.text
                    assert "event: start" in body
                    assert "event: evidence" in body
                    assert "event: done" in body
            finally:
                index.close()
