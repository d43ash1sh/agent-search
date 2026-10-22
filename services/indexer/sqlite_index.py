from __future__ import annotations

from typing import Any

from crawler.items import DocumentItem
from crawler.pipelines import SQLitePipeline
from crawler.search import SearchService
from services.common.models import CleanDocument, SearchHit
from services.indexer.base import AbstractSearchIndex
from services.indexer.semantic_embedder import SemanticEmbedder


class SQLiteSearchIndex(AbstractSearchIndex):
    """Local SQLite FTS5 search index with zero external dependencies."""

    def __init__(self, database_path: str = "data/crawler.sqlite3") -> None:
        self.database_path = database_path
        self._pipeline = SQLitePipeline(database=database_path)
        self._pipeline.open_spider()
        self._service = SearchService(database=database_path)
        self._embedder = SemanticEmbedder()

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
        hybrid: bool = False,
    ) -> tuple[list[SearchHit], int]:
        cand_limit = max(limit * 2, 20) if hybrid else limit
        cand_tokens = max(max_tokens * 2, 2400) if hybrid else max_tokens

        results, tokens = self._service.search(
            query=query,
            limit=cand_limit,
            max_tokens=cand_tokens,
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

        if hybrid and hits:
            q_vec = self._embedder.embed_query(query)
            if q_vec is not None:
                doc_vecs = self._embedder.embed_passages([h.text for h in hits])
                if doc_vecs and len(doc_vecs) == len(hits):
                    semantic_scored = []
                    for h, d_vec in zip(hits, doc_vecs, strict=False):
                        sim = self._embedder.compute_similarity(q_vec, d_vec)
                        semantic_scored.append((h, sim))

                    # Sort by semantic similarity
                    semantic_scored.sort(key=lambda item: item[1], reverse=True)
                    semantic_hits = [
                        SearchHit(
                            evidence_id=h.evidence_id,
                            url=h.url,
                            canonical_url=h.canonical_url,
                            title=h.title,
                            text=h.text,
                            score=sim,
                            estimated_tokens=h.estimated_tokens,
                            match_type="vector",
                        )
                        for h, sim in semantic_scored
                    ]

                    # Combine lexical and semantic rankings with RRF
                    fused = self._embedder.reciprocal_rank_fusion(hits, semantic_hits)

                    # Enforce token budget and limit
                    budget_hits: list[SearchHit] = []
                    acc_tokens = 0
                    for fh in fused[:limit]:
                        if acc_tokens + fh.estimated_tokens <= max_tokens or not budget_hits:
                            budget_hits.append(fh)
                            acc_tokens += fh.estimated_tokens
                        else:
                            break
                    return budget_hits, acc_tokens

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
