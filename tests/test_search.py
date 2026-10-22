import sqlite3
import tempfile
import unittest
from pathlib import Path

from crawler.pipelines import SQLitePipeline
from crawler.search import SearchService, estimate_tokens


class SearchServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.database = str(Path(self.directory.name) / "index.sqlite3")
        pipeline = SQLitePipeline(self.database)
        pipeline.open_spider()
        pipeline.process_item(
            {
                "url": "https://docs.example.com/guide",
                "canonical_url": "https://docs.example.com/guide",
                "title": "Getting started",
                "text": "A compact guide to configuring the local crawler.",
                "blocks": ["A compact guide to configuring the local crawler."],
            }
        )
        pipeline.process_item(
            {
                "url": "https://mirror.example.com/guide",
                "canonical_url": "https://docs.example.com/guide",
                "title": "Getting started mirror",
                "text": "A compact guide to configuring the local crawler.",
                "blocks": ["A compact guide to configuring the local crawler."],
            }
        )
        pipeline.process_item(
            {
                "url": "https://other.test/setup",
                "canonical_url": "https://other.test/setup",
                "title": "Unrelated source",
                "text": "Completely different database topic.",
                "blocks": ["Completely different database topic."],
            }
        )
        pipeline.process_item(
            {
                "url": "https://as.example/assamese",
                "canonical_url": "https://as.example/assamese",
                "title": "অসমীয়া ভাষা",
                "text": "অসমীয়া ভাষাৰ উদ্ভৱ আৰু সাহিত্যৰ ইতিহাস।",
                "blocks": ["অসমীয়া ভাষাৰ উদ্ভৱ আৰু সাহিত্যৰ ইতিহাস।"],
            }
        )
        pipeline.close_spider()

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_filters_domains_and_deduplicates_canonical_sources(self) -> None:
        results, tokens = SearchService(self.database).search(
            "configuring crawler",
            domains=["example.com"],
            limit=10,
            max_tokens=100,
        )
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].canonical_url, "https://docs.example.com/guide")
        self.assertEqual(tokens, results[0].estimated_tokens)
        self.assertEqual(results[0].domain, "docs.example.com")

    def test_token_budget_is_enforced(self) -> None:
        results, tokens = SearchService(self.database).search(
            "crawler",
            limit=10,
            max_tokens=2,
        )
        self.assertEqual(results, [])
        self.assertEqual(tokens, 0)

    def test_default_requires_all_query_terms_and_any_mode_relaxes_match(self) -> None:
        strict_results, _ = SearchService(self.database).search("configuring database")
        broad_results, _ = SearchService(self.database).search(
            "configuring database",
            match="any",
        )

        self.assertEqual(strict_results, [])
        self.assertGreater(len(broad_results), 0)

    def test_token_estimate_is_explicit_and_nonzero(self) -> None:
        self.assertEqual(estimate_tokens("12345"), 2)

    def test_search_accepts_assamese_query_terms(self) -> None:
        results, _ = SearchService(self.database).search("অসমীয়া ভাষাৰ উদ্ভৱ")

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].title, "অসমীয়া ভাষা")

    def test_evidence_can_be_fetched_by_stable_id(self) -> None:
        results, _ = SearchService(self.database).search("configuring crawler")
        fetched = SearchService(self.database).get_evidence(results[0].evidence_id)

        self.assertIsNotNone(fetched)
        assert fetched is not None
        self.assertEqual(fetched.text, results[0].text)
        self.assertEqual(fetched.evidence_id, results[0].evidence_id)
        self.assertEqual(
            fetched.text,
            self._document_slice(fetched.url, fetched.start_offset, fetched.end_offset),
        )

    def test_migrates_documents_from_the_original_index_schema(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            database = str(Path(directory) / "legacy.sqlite3")
            connection = sqlite3.connect(database)
            try:
                connection.execute(
                    """
                    CREATE TABLE documents (
                        url TEXT PRIMARY KEY,
                        canonical_url TEXT NOT NULL,
                        title TEXT NOT NULL,
                        text TEXT NOT NULL,
                        fetched_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
                connection.execute(
                    """
                    INSERT INTO documents(url, canonical_url, title, text)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        "https://legacy.example/guide",
                        "https://legacy.example/guide",
                        "Legacy guide",
                        "Migrated searchable legacy content",
                    ),
                )
                connection.commit()
            finally:
                connection.close()

            pipeline = SQLitePipeline(database)
            pipeline.open_spider()
            pipeline.close_spider()
            results, _ = SearchService(database).search("legacy content")

            self.assertEqual(len(results), 1)
            self.assertEqual(results[0].title, "Legacy guide")
            self.assertEqual(
                results[0].text,
                self._document_slice(
                    results[0].url,
                    results[0].start_offset,
                    results[0].end_offset,
                    database=database,
                ),
            )

    def _document_slice(
        self,
        url: str,
        start: int,
        end: int,
        database: str | None = None,
    ) -> str:
        connection = sqlite3.connect(database or self.database)
        try:
            row = connection.execute("SELECT text FROM documents WHERE url = ?", (url,)).fetchone()
            document_text = str(row[0]) if row else ""
        finally:
            connection.close()
        return document_text[start:end]
