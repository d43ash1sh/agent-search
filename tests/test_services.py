from __future__ import annotations

import tempfile
import time

import pytest

from services.common.hashing import (
    compute_simhash,
    simhash_hamming_distance,
    to_signed_int64,
    to_unsigned_uint64,
)
from services.extractor.chunker import chunk_document
from services.extractor.dedup_filter import DeduplicationFilter
from services.extractor.trafilatura_cleaner import TrafilaturaCleaner
from services.extractor.worker import ExtractorWorker
from services.fetcher.worker import FetcherWorker
from services.indexer.sqlite_index import SQLiteSearchIndex
from services.indexer.worker import IndexerWorker
from services.queue.memory_broker import InMemoryQueueBroker


class TestDistributedServices:
    def test_simhash_and_deduplication(self) -> None:
        text1 = "Agent-native search systems prioritize high quality evidence units over document ranking."
        text2 = "Agent native search systems prioritize high quality evidence units over document ranking."
        text3 = "Completely unrelated baking recipe with butter, sugar, and flour in an oven."

        hash1 = compute_simhash(text1)
        hash2 = compute_simhash(text2)
        hash3 = compute_simhash(text3)

        assert simhash_hamming_distance(hash1, hash2) <= 3
        assert simhash_hamming_distance(hash1, hash3) > 10

        dedup = DeduplicationFilter(hamming_threshold=3)
        assert dedup.is_duplicate(text1) == (False, "")
        dedup.register(text1)

        is_dup, reason = dedup.is_duplicate(text1)
        assert is_dup and reason == "exact_duplicate"

        is_dup, reason = dedup.is_duplicate(text2)
        assert is_dup and reason == "near_duplicate"

        assert dedup.is_duplicate(text3) == (False, "")

    def test_simhash_signed_conversions(self) -> None:
        overflow_values = [
            14454562975676966686,
            12435559112285661429,
            10363923691107606715,
            10215408779451196395,
            14720958193186661231,
            (1 << 64) - 1,
            0,
            1,
            (1 << 63) - 1,
            1 << 63,
        ]
        for val in overflow_values:
            signed = to_signed_int64(val)
            assert -9223372036854775808 <= signed <= 9223372036854775807
            restored = to_unsigned_uint64(signed)
            assert restored == val

            # Verify hamming distance is identical regardless of signed/unsigned
            dist1 = simhash_hamming_distance(val, val ^ 1)
            dist2 = simhash_hamming_distance(signed, to_signed_int64(val ^ 1))
            assert dist1 == dist2 == 1

    def test_trafilatura_extraction(self) -> None:
        html = """
        <!DOCTYPE html>
        <html>
        <head><title>Agent Architecture Article</title></head>
        <body>
            <nav><a href="/">Home</a><a href="/about">About</a></nav>
            <main>
                <h1>Building Agent-Native Search</h1>
                <p>This article discusses decoupled crawler architectures with Redis and Trafilatura.</p>
                <p>Trafilatura strips boilerplate advertisements and navigation bars accurately.</p>
            </main>
            <footer><p>&copy; 2026 Corporation. All rights reserved.</p></footer>
        </body>
        </html>
        """
        cleaner = TrafilaturaCleaner()
        result = cleaner.extract(html, url="https://example.com/article")
        assert result is not None
        assert "Building Agent-Native Search" in result["clean_text"]
        assert "Trafilatura strips boilerplate" in result["clean_text"]
        assert "Home" not in result["clean_text"]  # Nav stripped
        assert "Corporation" not in result["clean_text"]  # Footer stripped
        assert result["markdown"] != ""

    def test_passage_chunking_offsets(self) -> None:
        url = "https://example.com/docs"
        text = (
            "Paragraph one is introduction to RAG.\n\n"
            "Paragraph two explains dense vector embeddings and BM25.\n\n"
            "Paragraph three concludes with evaluation benchmarks."
        )
        passages = chunk_document(url, text, max_chars=80, min_chars=10)
        assert len(passages) >= 2
        for p in passages:
            assert p.passage_id.startswith("ev_")
            assert text[p.start_char : p.end_char] == p.text
            assert p.estimated_tokens > 0

    def test_chunk_document_sentence_subdivision(self) -> None:
        """Verify that massive paragraphs exceeding max_chars are cleanly split at sentence boundaries."""
        url = "https://example.com/long-article"
        long_paragraph = (
            "First sentence introduces the primary database concept. "
            "Second sentence explains the write-ahead log mechanism. "
            "Third sentence details the distributed consensus algorithm and Raft leader elections. "
            "Fourth sentence covers vector indexing using hierarchical navigable small world graphs. "
            "Fifth sentence concludes the performance benchmark analysis across clusters."
        )
        passages = chunk_document(url, long_paragraph, max_chars=120, min_chars=20)
        assert len(passages) >= 3
        for p in passages:
            assert len(p.text) <= 160
            assert long_paragraph[p.start_char : p.end_char] == p.text
            assert p.text.endswith((".", "!", "?"))

    def test_is_listing_or_feed_url(self) -> None:
        from services.extractor.worker import is_listing_or_feed_url

        assert is_listing_or_feed_url("https://simonwillison.net/tags/parquet/") is True
        assert is_listing_or_feed_url("https://example.com/category/tech/") is True
        assert is_listing_or_feed_url("https://example.com/blog/archive/2025/") is True
        assert is_listing_or_feed_url("https://example.com/posts?page=2") is True
        assert is_listing_or_feed_url("https://example.com/feed") is True

        # Regular article URLs should NOT be flagged as listings
        assert is_listing_or_feed_url("https://simonwillison.net/2025/Feb/15/s3-tables/") is False
        assert is_listing_or_feed_url("https://example.com/docs/architecture.html") is False
        assert is_listing_or_feed_url("https://example.com/blog/how-to-build-search") is False

    @pytest.mark.anyio
    async def test_decoupled_pipeline_end_to_end(self) -> None:
        """Verify the full Producer-Consumer pipeline:
        FetchQueue -> FetchWorker -> StagingQueue -> ExtractorWorker -> IndexQueue -> IndexerWorker -> Search
        """
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = f"{tmp_dir}/test_services.sqlite3"
            dlq_path = f"{tmp_dir}/dlq.jsonl"

            broker = InMemoryQueueBroker(dlq_log_path=dlq_path)
            index = SQLiteSearchIndex(database_path=db_path)

            FetcherWorker(broker=broker)
            extractor_worker = ExtractorWorker(broker=broker)
            indexer_worker = IndexerWorker(broker=broker, index=index)

            # Step 1: Enqueue a simulated raw page directly to staging
            html_content = """
            <!DOCTYPE html>
            <html>
            <head><title>Distributed Systems Manual</title></head>
            <body>
                <h1>Decoupled Queues in Distributed Crawlers</h1>
                <p>Separating fetch workers from index workers prevents crawl job crashes.</p>
                <p>A failed page load should never corrupt the search database.</p>
            </body>
            </html>
            """
            from services.common.models import RawPageRecord

            raw_record = RawPageRecord(
                url="https://docs.example.com/queues",
                canonical_url="https://docs.example.com/queues",
                status_code=200,
                raw_html=html_content,
                fetcher_engine="httpx",
            )
            await broker.enqueue_raw(raw_record)

            # Step 2: Extractor worker processes staging queue -> pushes to index queue
            extracted_ok = await extractor_worker.run_once()
            assert extracted_ok is True

            depths = await broker.get_queue_depths()
            assert depths["index"] == 1

            # Step 3: Indexer worker processes index queue -> commits to SQLite index
            indexed_ok = await indexer_worker.run_once()
            assert indexed_ok is True

            depths = await broker.get_queue_depths()
            assert depths["index"] == 0

            try:
                # Step 4: Search query retrieves the evidence unit
                hits, tokens = await index.search(query="Separating fetch workers", limit=5, max_tokens=1000)
                assert len(hits) >= 1
                assert "Separating fetch workers" in hits[0].text
                assert tokens > 0
            finally:
                await index.close()

    @pytest.mark.anyio
    async def test_dlq_isolation_on_failure(self) -> None:
        """Verify that a failed fetch does NOT touch staging/index and safely lands in DLQ."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            dlq_path = f"{tmp_dir}/dlq.jsonl"
            broker = InMemoryQueueBroker(dlq_log_path=dlq_path)

            # Mock fetcher that fails on invalid URL
            from services.common.models import FailedFetchRecord

            failed = FailedFetchRecord(
                url="https://broken-domain-999.invalid/bad-page",
                error_type="ConnectTimeout",
                error_message="Timed out connecting to remote host",
            )
            await broker.route_to_dlq(failed)

            depths = await broker.get_queue_depths()
            assert depths["dlq"] == 1
            assert depths["staging"] == 0
            assert depths["index"] == 0
            assert len(broker.dlq_records) == 1
            assert broker.dlq_records[0].error_type == "ConnectTimeout"

    @pytest.mark.anyio
    async def test_fetcher_parallel_concurrency_and_queue_clear(self) -> None:
        """Verify that FetcherWorker runs with parallel consumers and queue clear functions."""
        from services.common.models import FetchRequest

        broker = InMemoryQueueBroker()
        for i in range(50):
            await broker.enqueue_fetch(FetchRequest(url=f"https://example.com/page-{i}"))

        depths = await broker.get_queue_depths()
        assert depths["fetch"] == 50

        # Test queue clear
        cleared = await broker.clear_queue("fetch")
        assert cleared["fetch"] == 50
        depths_after = await broker.get_queue_depths()
        assert depths_after["fetch"] == 0

    @pytest.mark.anyio
    async def test_playwright_worker_pool_concurrency(self) -> None:
        """Verify PlaywrightBrowserFetcher runs bounded by max_workers=3."""
        from services.fetcher.browser_client import PlaywrightBrowserFetcher

        fetcher = PlaywrightBrowserFetcher(max_workers=3, timeout_ms=15000)
        try:
            stats = fetcher.get_stats()
            assert stats["max_workers"] == 3
            assert stats["active_workers"] == 0

            # Test fetching via Playwright browser worker
            record, _failed = await fetcher.fetch("https://example.com")
            assert record is not None
            assert record.status_code == 200
            assert record.fetcher_engine == "playwright"
            assert "Example Domain" in record.raw_html

            stats_after = fetcher.get_stats()
            assert stats_after["total_rendered"] >= 1
            assert stats_after["active_workers"] == 0
        finally:
            await fetcher.close()

    def test_ssrf_protection_guard(self) -> None:
        """Verify that private, loopback, link-local metadata IPs are blocked by SSRF guard."""
        from services.fetcher.security import is_ssrf_safe_url

        # Private / loopback / metadata addresses -> BLOCKED
        blocked_targets = [
            "http://127.0.0.1:8000/api",
            "http://localhost:5432",
            "http://169.254.169.254/latest/meta-data/",
            "http://10.0.0.1/admin",
            "http://192.168.1.1/router",
            "http://172.16.0.5:6379",
            "ftp://example.com/file",  # invalid scheme
        ]
        for target in blocked_targets:
            safe, reason = is_ssrf_safe_url(target)
            assert safe is False, f"Expected {target} to be blocked by SSRF guard!"
            assert len(reason) > 0

        # Public domains -> ALLOWED
        safe, _ = is_ssrf_safe_url("https://example.com")
        assert safe is True

    def test_idempotent_cache_keys(self) -> None:
        """Verify deterministic hashing of URL and options produces unique, idempotent keys."""
        from services.cache.redis_cache import make_cache_key

        k1 = make_cache_key("https://example.com/article", render_js=False)
        k2 = make_cache_key("https://example.com/article", render_js=False)
        k3 = make_cache_key("https://example.com/article", render_js=True)
        k4 = make_cache_key("https://example.com/article", render_js=False, options={"depth": 1})

        assert k1 == k2  # Identical options -> identical key (idempotency)
        assert k1 != k3  # JS render vs HTTP -> distinct keys
        assert k1 != k4  # Different options -> distinct keys

    @pytest.mark.anyio
    async def test_politeness_domain_rate_limiter(self) -> None:
        """Verify PolitenessManager enforces domain delays and tracks active leases."""
        from services.fetcher.politeness import PolitenessManager

        manager = PolitenessManager(min_domain_delay_seconds=0.1, max_concurrent_per_domain=2)
        url = "https://politeness-test.com/page1"

        t0 = time.monotonic()
        await manager.acquire_domain_slot(url)
        await manager.release_domain_slot(url)

        # Second request immediately to same domain should enforce delay
        await manager.acquire_domain_slot(url)
        t1 = time.monotonic()
        await manager.release_domain_slot(url)

        assert (t1 - t0) >= 0.08

    @pytest.mark.anyio
    async def test_async_crawl_and_sync_scrape_endpoints(self) -> None:
        """Verify synchronous /v1/scrape and async /v1/crawl with job polling."""
        from fastapi.testclient import TestClient

        from services.api.server import app

        client = TestClient(app)

        # 1. Test synchronous /scrape SSRF protection
        blocked_resp = client.post("/v1/scrape", json={"url": "http://127.0.0.1:8000/internal"})
        assert blocked_resp.status_code == 200
        assert blocked_resp.json()["status"] == "blocked"

        # 2. Test async /crawl returning job_id
        crawl_resp = client.post(
            "/v1/crawl", json={"urls": ["https://example.com/item-1", "https://example.com/item-2"]}
        )
        assert crawl_resp.status_code == 200
        crawl_data = crawl_resp.json()
        assert "job_id" in crawl_data
        assert crawl_data["status"] == "queued"
        assert crawl_data["enqueued"] == 2

        job_id = crawl_data["job_id"]

        # 3. Test polling /v1/jobs/{job_id}
        job_poll = client.get(f"/v1/jobs/{job_id}")
        assert job_poll.status_code == 200
        job_info = job_poll.json()
        assert job_info["job_id"] == job_id
        assert job_info["enqueued_urls"] == 2

        # 4. Test /v1/map domain discovery
        map_resp = client.post("/v1/map", json={"url": "https://example.com", "limit": 10})
        assert map_resp.status_code == 200
        assert map_resp.json()["domain"] == "example.com"
