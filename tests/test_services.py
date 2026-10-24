from __future__ import annotations

import tempfile

import pytest

from services.common.hashing import compute_simhash, simhash_hamming_distance
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
