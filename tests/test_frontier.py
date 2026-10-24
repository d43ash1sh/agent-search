import os
import unittest
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from crawler.pipelines import SQLitePipeline
from services.api.routes import create_api_router
from services.cache.redis_cache import InMemoryUrlCache
from services.common.models import RawPageRecord
from services.extractor.worker import ExtractorWorker
from services.indexer.sqlite_index import SQLiteSearchIndex
from services.queue.memory_broker import InMemoryQueueBroker


class TestUrlFrontier(unittest.TestCase):
    def setUp(self) -> None:
        self.db_path = "data/test_frontier.sqlite3"
        if Path(self.db_path).exists():
            os.remove(self.db_path)
        self.pipeline = SQLitePipeline(self.db_path)
        self.pipeline.open_spider()

    def tearDown(self) -> None:
        self.pipeline.close_spider()
        if Path(self.db_path).exists():
            os.remove(self.db_path)

    def test_frontier_records_and_stats(self) -> None:
        records = [
            {"url": "https://example.com/page1", "domain": "example.com", "source_url": "https://seed.com"},
            {"url": "https://outside.org/article", "domain": "outside.org", "source_url": "https://seed.com"},
            {"url": "https://outside.org/blog", "domain": "outside.org", "source_url": "https://seed.com"},
        ]
        inserted = self.pipeline.record_frontier_urls(records)
        self.assertEqual(inserted, 3)

        stats = self.pipeline.get_frontier_stats()
        self.assertEqual(stats["total_discovered"], 3)
        self.assertEqual(stats["total_domains"], 2)
        self.assertEqual(stats["discovered"], 3)

        # Mark one as queued and one as crawled
        self.pipeline.update_frontier_status("https://example.com/page1", "queued")
        self.pipeline.update_frontier_status("https://outside.org/article", "crawled")

        stats_updated = self.pipeline.get_frontier_stats()
        self.assertEqual(stats_updated["queued"], 1)
        self.assertEqual(stats_updated["crawled"], 1)
        self.assertEqual(stats_updated["discovered"], 1)

    def test_pending_frontier_urls_partitioning(self) -> None:
        records = [
            {"url": f"https://domain-a.com/{i}", "domain": "domain-a.com", "source_url": "https://seed.com"}
            for i in range(10)
        ] + [
            {"url": f"https://domain-b.com/{i}", "domain": "domain-b.com", "source_url": "https://seed.com"}
            for i in range(10)
        ]
        self.pipeline.record_frontier_urls(records)

        # Retrieve with max_per_domain = 2
        pending = self.pipeline.get_pending_frontier_urls(limit=10, max_per_domain=2)
        self.assertEqual(len(pending), 4)  # 2 from domain-a + 2 from domain-b


class TestCrossSiteExtraction(unittest.TestCase):
    def test_extract_all_links_separates_internal_and_external(self) -> None:
        html = """
        <html>
            <body>
                <a href="/internal/page">Internal</a>
                <a href="https://news.ycombinator.com/item?id=123">Also Internal</a>
                <a href="https://github.com/torvalds/linux">External 1</a>
                <a href="https://simonwillison.net/2026/note/">External 2</a>
                <a href="/logo.png">Image</a>
            </body>
        </html>
        """
        internal, external = ExtractorWorker.extract_all_links(html, "https://news.ycombinator.com/news")
        self.assertIn("https://news.ycombinator.com/internal/page", internal)
        self.assertIn("https://news.ycombinator.com/item?id=123", internal)
        self.assertIn("https://github.com/torvalds/linux", external)
        self.assertIn("https://simonwillison.net/2026/note/", external)
        self.assertNotIn("https://news.ycombinator.com/logo.png", internal)


@pytest.mark.anyio
async def test_extractor_worker_cross_site_crawling() -> None:
    db_path = "data/test_extractor_cross_site.sqlite3"
    if Path(db_path).exists():
        os.remove(db_path)

    broker = InMemoryQueueBroker()
    cache = InMemoryUrlCache()
    worker = ExtractorWorker(broker=broker, sqlite_path=db_path, cache=cache)

    raw_html = """
    <html>
        <head><title>Hacker News Front</title></head>
        <body>
            <p>Hacker News discussions and links to external sites.</p>
            <a href="/news?p=2">Next Page (Internal)</a>
            <a href="https://external-blog.com/post-1">External Article 1</a>
            <a href="https://external-tool.org/docs">External Tool Docs</a>
        </body>
    </html>
    """
    raw_record = RawPageRecord(
        url="https://news.ycombinator.com/",
        canonical_url="https://news.ycombinator.com/",
        status_code=200,
        headers={},
        raw_html=raw_html,
        elapsed_seconds=0.1,
        fetcher_engine="httpx",
        metadata={
            "recursive": True,
            "depth": 0,
            "max_depth": 1,
            "allowed_domain": "news.ycombinator.com",
            "follow_external": True,
            "max_external_depth": 1,
        },
    )

    await broker.enqueue_raw(raw_record)
    processed = await worker.run_once()
    assert processed is True

    # Check that both internal and external links were recorded in frontier
    stats = await worker.index.get_frontier_stats()
    assert stats["total_discovered"] >= 3
    assert stats["total_domains"] >= 2

    # Check that child links were enqueued into fetch queue
    fetch_items = []
    while True:
        item = await broker.dequeue_fetch(timeout=0.1)
        if not item:
            break
        fetch_items.append(item.url)

    assert "https://news.ycombinator.com/news?p=2" in fetch_items
    assert "https://external-blog.com/post-1" in fetch_items
    assert "https://external-tool.org/docs" in fetch_items

    worker.stop()
    await worker.index.close()
    if Path(db_path).exists():
        os.remove(db_path)


@pytest.mark.anyio
async def test_api_frontier_endpoints() -> None:
    db_path = "data/test_api_frontier.sqlite3"
    if Path(db_path).exists():
        os.remove(db_path)

    broker = InMemoryQueueBroker()
    index = SQLiteSearchIndex(db_path)
    await index.record_frontier_urls(
        [
            {"url": "https://outside.org/item1", "domain": "outside.org", "source_url": "https://seed.com"},
            {"url": "https://outside.org/item2", "domain": "outside.org", "source_url": "https://seed.com"},
        ]
    )

    app = FastAPI()
    app.include_router(create_api_router(broker, index))

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        # 1. Test stats
        res_stats = await client.get("/v1/frontier/stats")
        assert res_stats.status_code == 200
        stats = res_stats.json()
        assert stats["total_discovered"] == 2
        assert stats["discovered"] == 2

        # 2. Test urls list
        res_urls = await client.get("/v1/frontier/urls")
        assert res_urls.status_code == 200
        urls = res_urls.json()
        assert len(urls) == 2
        assert urls[0]["domain"] == "outside.org"

        # 3. Test crawl frontier
        res_crawl = await client.post("/v1/frontier/crawl", json={"limit": 10, "max_per_domain": 5})
        assert res_crawl.status_code == 200
        crawl_data = res_crawl.json()
        assert crawl_data["enqueued_count"] == 2

        # Check queue
        queued_item = await broker.dequeue_fetch()
        assert queued_item is not None
        assert "outside.org" in queued_item.url

    await index.close()
    if Path(db_path).exists():
        os.remove(db_path)
