from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from services.api.server import app
from services.common.models import CleanDocument, PassageChunk
from services.indexer.sqlite_index import SQLiteSearchIndex
from services.search.live_engine import LiveSearchEngine
from services.search.serp_client import (
    BingSearchClient,
    BraveSearchClient,
    DuckDuckGoSerpClient,
    FederatedSerpClient,
    SerpResultItem,
)


class TestLiveSearch:
    def test_serp_client_clean_url_unwraps_redirects(self) -> None:
        client = DuckDuckGoSerpClient()
        direct = "https://www.python.org/downloads/"
        assert client._clean_url(direct) == direct

        wrapped = "https://duckduckgo.com/l/?uddg=https%3A%2F%2Ffastapi.tiangolo.com%2F&rut=..."
        assert client._clean_url(wrapped) == "https://fastapi.tiangolo.com/"

        protocol_relative = "//example.com/page"
        assert client._clean_url(protocol_relative) == "https://example.com/page"

    def test_bing_client_unwraps_redirects(self) -> None:
        bing = BingSearchClient()
        # Test direct URL
        direct = "https://python.org"
        assert bing._unwrap_bing_url(direct) == direct

        # Test Bing base64 encoded URL wrapper: u=a1aHR0cHM6Ly9mYXN0YXBpLnRpYW5nb2xvLmNvbS8
        wrapped = "https://www.bing.com/ck/a?!&&p=123&u=a1aHR0cHM6Ly9mYXN0YXBpLnRpYW5nb2xvLmNvbS8&ntb=1"
        assert bing._unwrap_bing_url(wrapped) == "https://fastapi.tiangolo.com/"

    @pytest.mark.anyio
    async def test_serp_client_executes_query(self) -> None:
        client = DuckDuckGoSerpClient()
        results = await client.search("python documentation", limit=3)
        # Note: In environments without internet, results may be empty, but must not crash
        assert isinstance(results, list)
        for r in results:
            assert isinstance(r, SerpResultItem)
            assert r.url.startswith(("http://", "https://"))
            assert len(r.title) > 0

    @pytest.mark.anyio
    async def test_bing_client_executes_query(self) -> None:
        client = BingSearchClient()
        results = await client.search("python documentation", limit=3)
        assert isinstance(results, list)
        for r in results:
            assert isinstance(r, SerpResultItem)
            assert r.url.startswith(("http://", "https://"))
            assert len(r.title) > 0

    @pytest.mark.anyio
    async def test_brave_client_graceful_without_key(self) -> None:
        client = BraveSearchClient(api_key=None)
        results = await client.search("fastapi tutorial", limit=3)
        assert results == []

    @pytest.mark.anyio
    async def test_federated_client_aggregates_and_deduplicates(self) -> None:
        client = FederatedSerpClient()
        results = await client.search("python asyncio documentation", limit=4)
        assert isinstance(results, list)
        seen_domains: dict[str, int] = {}
        for r in results:
            assert isinstance(r, SerpResultItem)
            dom = r.domain
            seen_domains[dom] = seen_domains.get(dom, 0) + 1
            # Verify domain diversity: at most 2 per domain
            assert seen_domains[dom] <= 2

    @pytest.mark.anyio
    async def test_live_search_engine_fallback_and_reranking(self, tmp_path: pytest.TempPathFactory) -> None:
        db_path = str(tmp_path / "live_search_test.sqlite3")  # type: ignore[operator]
        index = SQLiteSearchIndex(database_path=db_path)

        # Pre-seed a document into index
        doc = CleanDocument(
            url="https://live-test.example.com/agent-article",
            canonical_url="https://live-test.example.com/agent-article",
            title="Building Autonomous AI Agents with Search Grounding",
            clean_text=(
                "Autonomous AI agents need grounded web evidence and citations to avoid hallucinations. "
                "Metasearch engines combine SERP discovery with dense vector indexing and neural cross-encoders."
            ),
            passages=[
                PassageChunk(
                    passage_id="ev_live_01",
                    document_url="https://live-test.example.com/agent-article",
                    order_index=0,
                    start_char=0,
                    end_char=86,
                    text="Autonomous AI agents need grounded web evidence and citations to avoid hallucinations.",
                    estimated_tokens=20,
                    content_hash="mock_hash_01",
                )
            ],
        )
        await index.index_document(doc)

        engine = LiveSearchEngine(index=index)
        response = await engine.search(
            query="grounded web evidence hallucinations",
            limit=5,
            live_crawl=False,  # Test retrieval & reranking without live network delay
        )

        assert response.query == "grounded web evidence hallucinations"
        assert len(response.results) >= 1
        assert len(response.results[0].evidence_id) > 5
        assert "Autonomous AI agents" in response.results[0].text
        assert "ID: " in response.citations_markdown
        await index.close()

    def test_api_live_search_endpoints(self) -> None:
        client = TestClient(app)

        # 1. Test POST /v1/search/live
        live_req = {
            "query": "artificial intelligence agents",
            "limit": 3,
            "live_crawl": False,
        }
        resp = client.post("/v1/search/live", json=live_req)
        assert resp.status_code == 200
        data = resp.json()
        assert "query" in data
        assert "results" in data
        assert "citations_markdown" in data

        # 2. Test POST /v1/search with live=True flag
        search_req = {
            "query": "artificial intelligence agents",
            "limit": 3,
            "live": True,
        }
        resp2 = client.post("/v1/search", json=search_req)
        assert resp2.status_code == 200
        data2 = resp2.json()
        assert "query" in data2
        assert "results" in data2
