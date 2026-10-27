from __future__ import annotations

import asyncio
import logging
import math
import urllib.parse

from pydantic import BaseModel

from services.common.citations import CitationFormatter
from services.common.hashing import compute_simhash
from services.common.models import CleanDocument, SearchHit
from services.discovery.common_crawl import CommonCrawlClient
from services.discovery.open_search import OpenKnowledgeDiscovery
from services.extractor.chunker import chunk_document
from services.extractor.trafilatura_cleaner import TrafilaturaCleaner
from services.fetcher.http_client import AsyncHttpFetcher
from services.indexer.base import AbstractSearchIndex
from services.indexer.reranker import NeuralReranker

logger = logging.getLogger(__name__)


class AutonomousSearchResponse(BaseModel):
    query: str
    results: list[SearchHit]
    estimated_tokens: int
    max_tokens: int
    citations_markdown: str = ""
    discovered_urls: list[str] = []
    discovery_mode: str = "local_index"  # 'local_index' or 'autonomous_crawl'
    elapsed_seconds: float = 0.0


class AutonomousSearchEngine:
    """100% self-contained autonomous search engine.

    Retrieves candidates using hybrid vector + lexical search, boosts candidates
    with internal PageRank domain authority, applies cross-encoder neural reranking,
    and autonomously bootstraps cold queries via open discovery without commercial SERPs.
    """

    def __init__(
        self,
        index: AbstractSearchIndex,
        reranker: NeuralReranker | None = None,
    ) -> None:
        self.index = index
        self.reranker = reranker or NeuralReranker()
        self.cleaner = TrafilaturaCleaner()
        self.http_fetcher = AsyncHttpFetcher(timeout=10.0)
        self.open_discovery = OpenKnowledgeDiscovery()
        self.common_crawl = CommonCrawlClient()

    async def _fetch_and_index_urls(self, urls: list[str]) -> list[str]:
        """Fetch and index a list of open discovery URLs into the local search store."""
        indexed_urls: list[str] = []

        async def worker(u: str) -> bool:
            try:
                rec, _ = await self.http_fetcher.fetch(u)
                if not rec or not rec.raw_html.strip():
                    return False

                extracted = self.cleaner.extract(rec.raw_html, rec.url)
                if not extracted or not extracted["clean_text"].strip():
                    return False

                clean_text = extracted["clean_text"]
                passages = chunk_document(rec.canonical_url, clean_text)

                doc = CleanDocument(
                    url=rec.url,
                    canonical_url=rec.canonical_url,
                    title=extracted["title"] or rec.url,
                    clean_text=clean_text,
                    markdown=extracted.get("markdown", ""),
                    author=extracted.get("author"),
                    published_date=extracted.get("published_date"),
                    sitename=extracted.get("sitename"),
                    description=extracted.get("description"),
                    simhash=compute_simhash(clean_text),
                    passages=passages,
                )
                await self.index.index_document(doc)
                indexed_urls.append(u)
                return True
            except Exception as err:
                logger.debug("Autonomous fetch/index error for %s: %s", u, err)
                return False

        tasks = [worker(url) for url in urls[:5]]
        await asyncio.gather(*tasks, return_exceptions=True)
        return indexed_urls

    async def search(
        self,
        query: str,
        limit: int = 5,
        max_tokens: int = 1500,
        allow_autonomous_discovery: bool = True,
    ) -> AutonomousSearchResponse:
        """Execute autonomous search: query local hybrid index, apply PageRank authority boost,

        cross-encoder reranking, and trigger open discovery if cold.
        """
        import time

        t0 = time.perf_counter()
        clean_query = query.strip()
        if not clean_query:
            return AutonomousSearchResponse(
                query=query,
                results=[],
                estimated_tokens=0,
                max_tokens=max_tokens,
                citations_markdown="",
                elapsed_seconds=0.0,
            )

        # 1. Search local hybrid index (lexical + dense vector)
        hits, _total_tokens = await self.index.search(
            query=clean_query,
            limit=limit * 3,
            max_tokens=max_tokens * 2,
            hybrid=True,
        )

        discovery_mode = "local_index"
        discovered_urls: list[str] = []

        # 2. Cold-query autonomous discovery (if local index has 0 hits and discovery allowed)
        if not hits and allow_autonomous_discovery:
            discovery_mode = "autonomous_crawl"
            logger.info("Cold query '%s': triggering open web discovery...", clean_query)

            # Try open knowledge discovery first
            open_results = await self.open_discovery.search(clean_query, limit=3)
            urls_to_crawl = [r.url for r in open_results]

            # If still empty, try Common Crawl CDX open index
            if not urls_to_crawl:
                cc_records = await self.common_crawl.discover_urls(f"*{clean_query}*", limit=3)
                urls_to_crawl = [r.url for r in cc_records]

            if urls_to_crawl:
                discovered_urls = await self._fetch_and_index_urls(urls_to_crawl)
                # Re-query local index after indexing new pages
                hits, _total_tokens = await self.index.search(
                    query=clean_query,
                    limit=limit * 3,
                    max_tokens=max_tokens * 2,
                    hybrid=True,
                )

        if not hits:
            elapsed = time.perf_counter() - t0
            return AutonomousSearchResponse(
                query=clean_query,
                results=[],
                estimated_tokens=0,
                max_tokens=max_tokens,
                citations_markdown="",
                discovered_urls=discovered_urls,
                discovery_mode=discovery_mode,
                elapsed_seconds=round(elapsed, 2),
            )

        # 3. Apply PageRank / Domain Authority multiplier to candidate scores
        adjusted_hits: list[SearchHit] = []
        for h in hits:
            domain = (urllib.parse.urlsplit(h.url).hostname or "").lower()
            authority = await self.index.get_domain_authority(domain)
            # Logarithmic authority boost: score * (1 + 0.35 * log(1 + authority))
            authority_multiplier = 1.0 + (0.35 * math.log(1.0 + max(authority, 0.0)))
            new_score = h.score * authority_multiplier
            adjusted_hits.append(
                SearchHit(
                    evidence_id=h.evidence_id,
                    url=h.url,
                    canonical_url=h.canonical_url,
                    title=h.title,
                    text=h.text,
                    score=new_score,
                    estimated_tokens=h.estimated_tokens,
                    match_type=h.match_type,
                )
            )

        # 4. Neural Cross-Encoder Reranking (FlashRank)
        reranked_hits, _ = self.reranker.rerank(
            query=clean_query,
            hits=adjusted_hits,
            max_tokens=max_tokens,
            top_k=limit,
        )

        # 5. Enforce token budget and count
        final_hits: list[SearchHit] = []
        acc_tokens = 0
        for rh in reranked_hits[:limit]:
            if acc_tokens + rh.estimated_tokens <= max_tokens or not final_hits:
                final_hits.append(rh)
                acc_tokens += rh.estimated_tokens
            else:
                break

        # 6. Build LLM Grounding Citations Markdown
        citations_md = CitationFormatter.format_markdown(final_hits)
        elapsed = time.perf_counter() - t0

        return AutonomousSearchResponse(
            query=clean_query,
            results=final_hits,
            estimated_tokens=acc_tokens,
            max_tokens=max_tokens,
            citations_markdown=citations_md,
            discovered_urls=discovered_urls,
            discovery_mode=discovery_mode,
            elapsed_seconds=round(elapsed, 2),
        )
