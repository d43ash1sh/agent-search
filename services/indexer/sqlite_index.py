from __future__ import annotations

from typing import Any

from crawler.items import DocumentItem
from crawler.pipelines import SQLitePipeline
from crawler.search import SearchService
from services.common.models import CleanDocument, SearchHit
from services.indexer.base import AbstractSearchIndex


class SQLiteSearchIndex(AbstractSearchIndex):
    """Local SQLite FTS5 search index with zero external dependencies."""

    def __init__(self, database_path: str = "data/crawler.sqlite3") -> None:
        self.database_path = database_path
        self._pipeline = SQLitePipeline(database=database_path)
        self._pipeline.open_spider()
        self._service = SearchService(database=database_path)

    async def index_document(self, doc: CleanDocument) -> None:
        # Save via pipeline logic to maintain full compatibility
        # Include title in blocks so title keywords are indexed in passage search
        blocks = ([doc.title] if doc.title else []) + (
            [p.text for p in doc.passages] if doc.passages else [doc.clean_text]
        )
        item: DocumentItem = {
            "url": doc.url,
            "canonical_url": doc.canonical_url,
            "title": doc.title,
            "text": doc.clean_text,
            "blocks": blocks,
        }
        self._pipeline.process_item(item)

    async def search(
        self,
        query: str,
        limit: int = 10,
        max_tokens: int = 1200,
        domains: list[str] | None = None,
    ) -> tuple[list[SearchHit], int]:
        results, tokens = self._service.search(
            query=query,
            limit=limit,
            max_tokens=max_tokens,
            domains=domains,
        )
        hits = [
            SearchHit(
                evidence_id=r.evidence_id,
                url=r.url,
                canonical_url=r.canonical_url,
                title=r.title or "",
                text=r.text,
                score=r.score,
                estimated_tokens=r.estimated_tokens,
                match_type="lexical",
            )
            for r in results
        ]
        return hits, tokens

    async def get_evidence(self, evidence_id: str) -> SearchHit | None:
        ev = self._service.get_evidence(evidence_id)
        if not ev:
            return None
        return SearchHit(
            evidence_id=ev.evidence_id,
            url=ev.url,
            canonical_url=ev.canonical_url,
            title=ev.title or "",
            text=ev.text,
            score=ev.score,
            estimated_tokens=ev.estimated_tokens,
            match_type="lexical",
        )

    async def get_stats(self) -> dict[str, Any]:
        return self._service.get_stats()

    def close(self) -> None:
        if self._pipeline.connection:
            self._pipeline.close_spider()
