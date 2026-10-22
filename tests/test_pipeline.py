import sqlite3
import tempfile
import unittest
from pathlib import Path

from crawler.items import DocumentItem
from crawler.pipelines import SQLitePipeline


class SQLitePipelineTests(unittest.TestCase):
    def test_stores_documents_and_keeps_full_text_index_updated(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            database = str(Path(directory) / "documents.sqlite3")
            pipeline = SQLitePipeline(database)
            pipeline.open_spider()
            item: DocumentItem = {
                "url": "https://example.com/",
                "canonical_url": "https://example.com/canonical",
                "title": "Old title",
                "text": "old searchable phrase",
            }
            pipeline.process_item(item)
            updated: DocumentItem = {
                **item,
                "title": "New title",
                "text": "fresh searchable phrase",
            }
            pipeline.process_item(updated)
            pipeline.close_spider()

            connection = sqlite3.connect(database)
            try:
                document = connection.execute(
                    "SELECT canonical_url, title, text FROM documents WHERE url = ?",
                    (item["url"],),
                ).fetchone()
                fresh_results = connection.execute(
                    """
                    SELECT p.text FROM passages_fts
                    JOIN passages AS p ON p.id = passages_fts.rowid
                    WHERE passages_fts MATCH ?
                    """,
                    ("fresh",),
                ).fetchall()
                old_results = connection.execute(
                    "SELECT text FROM passages_fts WHERE passages_fts MATCH ?",
                    ("old",),
                ).fetchall()
            finally:
                connection.close()

            self.assertEqual(
                document, ("https://example.com/canonical", "New title", "fresh searchable phrase")
            )
            self.assertEqual(fresh_results, [("fresh searchable phrase",)])
            self.assertEqual(old_results, [])

    def test_removes_duplicate_and_whitespace_only_blocks(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            database = str(Path(directory) / "documents.sqlite3")
            pipeline = SQLitePipeline(database)
            pipeline.open_spider()
            pipeline.process_item(
                {
                    "url": "https://example.com/clean",
                    "canonical_url": "https://example.com/clean",
                    "title": "Clean content",
                    "text": "ignored flattened text",
                    "blocks": [" Same block  text ", "", "Same block text", "Another block"],
                }
            )
            pipeline.close_spider()

            connection = sqlite3.connect(database)
            try:
                document_text = connection.execute(
                    "SELECT text FROM documents WHERE url = ?",
                    ("https://example.com/clean",),
                ).fetchone()[0]
                passages = connection.execute(
                    "SELECT text FROM passages WHERE document_url = ? ORDER BY ordinal",
                    ("https://example.com/clean",),
                ).fetchall()
            finally:
                connection.close()

            self.assertEqual(document_text, "Same block text Another block")
            self.assertEqual(passages, [("Same block text Another block",)])

    def test_passages_have_stable_ids_and_exact_document_offsets(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            database = str(Path(directory) / "documents.sqlite3")
            pipeline = SQLitePipeline(database)
            pipeline.open_spider()
            item: DocumentItem = {
                "url": "https://example.com/citations",
                "canonical_url": "https://example.com/citations",
                "title": "Citation offsets",
                "text": "ignored",
                "blocks": ["First evidence.", "Second evidence."],
            }
            pipeline.process_item(item)

            connection = sqlite3.connect(database)
            try:
                original: list[tuple[str, str, int, int]] = connection.execute(
                    """
                    SELECT evidence_id, text, start_offset, end_offset
                    FROM passages ORDER BY ordinal
                    """
                ).fetchall()
                document_row: tuple[str] = connection.execute(
                    "SELECT text FROM documents WHERE url = ?", (item["url"],)
                ).fetchone()
                document_text = document_row[0]
            finally:
                connection.close()

            pipeline.process_item(item)
            pipeline.close_spider()

            connection = sqlite3.connect(database)
            try:
                refreshed: list[tuple[str, str, int, int]] = connection.execute(
                    """
                    SELECT evidence_id, text, start_offset, end_offset
                    FROM passages ORDER BY ordinal
                    """
                ).fetchall()
            finally:
                connection.close()

            self.assertEqual(original, refreshed)
            for evidence_id, passage, start, end in original:
                self.assertEqual(document_text[start:end], passage)
                self.assertEqual(len(evidence_id), 64)
