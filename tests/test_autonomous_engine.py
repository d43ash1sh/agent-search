from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from services.api.server import app
from services.common.models import CleanDocument, PassageChunk
from services.discovery.common_crawl import CommonCrawlClient, CommonCrawlRecord
from services.discovery.open_search import OpenKnowledgeDiscovery
from services.discovery.seeds import SeedManager
from services.extractor.link_harvester import LinkHarvester
from services.frontier.crawler_daemon import AutonomousCrawlerDaemon
from services.indexer.pagerank import PageRankCalculator
from services.indexer.sqlite_index import SQLiteSearchIndex
from services.mcp.server import mcp
from services.search.autonomous_engine import AutonomousSearchEngine


class TestAutonomousSearchSystem:
    def test_link_harvester_extracts_and_normalizes(self) -> None:
        html = """
        <html>
            <body>
                <a href="/about?utm_source=twitter&utm_medium=social#team">About Us</a>
                <a href="https://example.com/docs/api?ref=hackernews">API Docs</a>
                <a href="https://python.org/community?id=123">Python Community</a>
                <a href="mailto:contact@example.com">Email Us</a>
                <a href="javascript:void(0)">Click</a>
            </body>
        </html>
        """
        source_url = "https://example.com/home"
        with patch("services.extractor.link_harvester.is_ssrf_safe_url", return_value=(True, "")):
            harvested = LinkHarvester.harvest(html, source_url)

        assert harvested.source_domain == "example.com"
        assert len(harvested.internal_links) == 2
        assert "https://example.com/about" in harvested.internal_links
        assert "https://example.com/docs/api" in harvested.internal_links
        assert len(harvested.external_links) == 1
        assert "https://python.org/community?id=123" in harvested.external_links
        assert len(harvested.edges) == 3

    def test_pagerank_calculator_converges_and_ranks(self) -> None:
        calc = PageRankCalculator(damping=0.85, max_iterations=20)
        # B is heavily referenced by A, C, D -> B should have the highest PageRank
        edges = [
            ("siteA.com", "siteB.com"),
            ("siteC.com", "siteB.com"),
            ("siteD.com", "siteB.com"),
            ("siteB.com", "siteA.com"),
        ]
        ranks = calc.compute(edges)
        assert len(ranks) == 4
        assert ranks["siteB.com"] == pytest.approx(1.0, rel=1e-2)
        assert ranks["siteB.com"] > ranks["siteA.com"]
        assert ranks["siteB.com"] > ranks["siteC.com"]
        assert ranks["siteB.com"] > ranks["siteD.com"]

    def test_seed_manager_provides_curated_seeds(self) -> None:
        mgr = SeedManager()
        seeds = mgr.get_seeds()
        assert len(seeds) >= 5
        domains = {s.domain for s in seeds}
        assert "en.wikipedia.org" in domains
        assert "as.wikipedia.org" in domains

        with patch("services.discovery.seeds.is_ssrf_safe_url", return_value=(True, "")):
            custom = mgr.add_seed("https://custom-repo.org/docs", category="custom", priority=1)
        assert custom is not None
        assert custom.domain == "custom-repo.org"

    @pytest.mark.anyio
    async def test_common_crawl_discovery_mock(self) -> None:
        client = CommonCrawlClient()
        client._cached_latest_endpoint = "https://index.commoncrawl.org/test-index"
        mock_cdx_lines = (
            '{"url": "https://example.org/article-1", "timestamp": "20261001", "status": "200"}\n'
            '{"url": "https://example.org/article-2", "timestamp": "20261002", "status": "200"}\n'
        )
        with (
            patch("services.discovery.common_crawl.is_ssrf_safe_url", return_value=(True, "")),
            patch.object(
                client, "get_latest_cdx_endpoint", return_value="https://index.commoncrawl.org/test"
            ),
            patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get,
        ):
            mock_resp = AsyncMock()
            mock_resp.status_code = 200
            mock_resp.text = mock_cdx_lines
            mock_get.return_value = mock_resp

            records = await client.discover_urls("example.org/*", limit=5)
            assert len(records) == 2
            assert isinstance(records[0], CommonCrawlRecord)
            assert records[0].url == "https://example.org/article-1"
            assert records[0].domain == "example.org"

    def test_open_knowledge_discovery_script_detection(self) -> None:
        disc = OpenKnowledgeDiscovery()
        indic_endpoints = disc._detect_endpoint("ভূপেন হাজৰিকা")
        assert "https://as.wikipedia.org/w/api.php" in indic_endpoints

        latin_endpoints = disc._detect_endpoint("vector database indexing")
        assert "https://en.wikipedia.org/w/api.php" in latin_endpoints

    @pytest.mark.anyio
    async def test_autonomous_crawler_daemon_seed_and_stats(self, tmp_path: pytest.TempPathFactory) -> None:
        db_path = str(tmp_path / "crawler_test.sqlite3")  # type: ignore[operator]
        index = SQLiteSearchIndex(database_path=db_path)

        daemon = AutonomousCrawlerDaemon(index=index, concurrency=2)
        count = await daemon.bootstrap_seeds()
        assert count > 0

        stats = daemon.get_stats()
        assert stats.is_running is False
        assert stats.pages_crawled == 0

        # Test recording link graph edges
        edge_records = [
            {
                "source_url": "https://a.org/page1",
                "target_url": "https://b.org/page2",
                "source_domain": "a.org",
                "target_domain": "b.org",
                "is_internal": False,
            }
        ]
        inserted = await index.record_link_edges(edge_records)
        assert inserted == 1

        # Test updating and retrieving domain authority
        await index.update_domain_authorities({"b.org": 2.5})
        authority = await index.get_domain_authority("b.org")
        assert authority == pytest.approx(2.5)

        await index.close()

    @pytest.mark.anyio
    async def test_autonomous_search_engine_local_retrieval(self, tmp_path: pytest.TempPathFactory) -> None:
        db_path = str(tmp_path / "auto_search.sqlite3")  # type: ignore[operator]
        index = SQLiteSearchIndex(database_path=db_path)

        # Seed authoritative document
        doc = CleanDocument(
            url="https://authoritative.org/deep-learning",
            canonical_url="https://authoritative.org/deep-learning",
            title="Modern Deep Learning for Autonomous Agents",
            clean_text="Autonomous agents utilize dense neural representations, vector indexes, and link graph PageRank.",
            passages=[
                PassageChunk(
                    passage_id="ev_deep_01",
                    document_url="https://authoritative.org/deep-learning",
                    order_index=0,
                    start_char=0,
                    end_char=98,
                    text="Autonomous agents utilize dense neural representations, vector indexes, and link graph PageRank.",
                    estimated_tokens=22,
                    content_hash="mock_hash_deep",
                )
            ],
        )
        await index.index_document(doc)
        await index.update_domain_authorities({"authoritative.org": 3.0})

        engine = AutonomousSearchEngine(index=index)
        resp = await engine.search(
            query="neural representations vector indexes PageRank",
            limit=3,
            allow_autonomous_discovery=False,
        )

        assert resp.query == "neural representations vector indexes PageRank"
        assert len(resp.results) >= 1
        assert "Autonomous agents utilize" in resp.results[0].text
        assert resp.discovery_mode == "local_index"
        assert "ID: " in resp.citations_markdown
        assert resp.results[0].evidence_id in resp.citations_markdown
        await index.close()

    def test_api_autonomous_endpoints(self) -> None:
        client = TestClient(app)

        # 1. Test POST /v1/search/autonomous
        resp = client.post(
            "/v1/search/autonomous",
            json={
                "query": "autonomous neural search",
                "limit": 3,
                "tokens": 1200,
                "allow_discovery": False,
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "query" in data
        assert "results" in data
        assert "discovery_mode" in data

        # 2. Test GET /v1/crawler/daemon/status
        status_resp = client.get("/v1/crawler/daemon/status")
        assert status_resp.status_code == 200
        stats_data = status_resp.json()
        assert "is_running" in stats_data
        assert "pages_crawled" in stats_data

        # 3. Test POST /v1/crawler/daemon/start and /stop
        start_resp = client.post("/v1/crawler/daemon/start")
        assert start_resp.status_code == 200
        stop_resp = client.post("/v1/crawler/daemon/stop")
        assert stop_resp.status_code == 200

    @pytest.mark.anyio
    async def test_mcp_autonomous_tools_registered(self) -> None:
        tools = await mcp.list_tools()
        tool_names = [t.name for t in tools]

        assert "autonomous_search" in tool_names
        assert "query_common_crawl" in tool_names
        assert "get_autonomous_crawler_status" in tool_names

        # Call get_autonomous_crawler_status
        result = await mcp.call_tool("get_autonomous_crawler_status", {})
        assert result is not None
        assert not getattr(result, "is_error", False)
        content = getattr(result, "content", [])
        text = "".join(getattr(item, "text", str(item)) for item in content)
        data = json.loads(text)
        assert "is_running" in data
        assert "pages_crawled" in data
