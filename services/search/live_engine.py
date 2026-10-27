from __future__ import annotations

import asyncio
import logging

from pydantic import BaseModel

from services.common.citations import CitationFormatter
from services.common.hashing import compute_simhash
from services.common.models import CleanDocument, SearchHit
from services.extractor.chunker import chunk_document
from services.extractor.trafilatura_cleaner import TrafilaturaCleaner
from services.fetcher.browser_client import PlaywrightBrowserFetcher
from services.fetcher.http_client import AsyncHttpFetcher
from services.indexer.base import AbstractSearchIndex
from services.indexer.reranker import NeuralReranker
from services.search.serp_client import DuckDuckGoSerpClient, SerpResultItem

logger = logging.getLogger(__name__)


class LiveSearchResponse(BaseModel):
    query: str
    estimated_tokens: int
    max_tokens: int
    results: list[SearchHit]
    serp_sources: list[SerpResultItem]
    citations_markdown: str = ""
    new_pages_indexed: int = 0
    crawl_elapsed_seconds: float = 0.0


class LiveSearchEngine:
    """End-to-end autonomous metasearch: discovers live web links, fetches and cleans content,

    indexes vectors into pgvector/SQLite in real-time, and returns neural-reranked citations.
    """

    def __init__(
        self,
        index: AbstractSearchIndex,
        serp_client: DuckDuckGoSerpClient | None = None,
        reranker: NeuralReranker | None = None,
    ) -> None:
        self.index = index
        self.serp_client = serp_client or DuckDuckGoSerpClient()
        self.reranker = reranker or NeuralReranker()
        self.cleaner = TrafilaturaCleaner()

    async def _fetch_and_index_url(
        self,
        url: str,
        http_fetcher: AsyncHttpFetcher,
        browser_fetcher: PlaywrightBrowserFetcher | None = None,
    ) -> CleanDocument | None:
        """Fetch a single URL, clean HTML, chunk passages, and persist into search index."""
        try:
            record, _failed = await http_fetcher.fetch(url)

            # Auto-fallback to Playwright if HTTP failed with challenge or empty body
            if (not record or len(record.raw_html.strip()) < 200) and browser_fetcher is not None:
                try:
                    b_rec, _ = await browser_fetcher.fetch(url)
                    if b_rec and len(b_rec.raw_html) > 200:
                        record = b_rec
                except Exception as b_err:
                    logger.debug("Playwright fallback note for %s: %s", url, b_err)

            if not record or not record.raw_html.strip():
                return None

            extracted = self.cleaner.extract(record.raw_html, record.url)
            if not extracted or not extracted["clean_text"].strip():
                return None

            clean_text = extracted["clean_text"]
            passages = chunk_document(record.canonical_url, clean_text)

            doc = CleanDocument(
                url=record.url,
                canonical_url=record.canonical_url,
                title=extracted["title"] or record.url,
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
            return doc
        except Exception as exc:
            logger.warning("Error fetching/indexing live URL %s: %s", url, exc)
            return None

    async def search(
        self,
        query: str,
        limit: int = 5,
        max_tokens: int = 1200,
        hybrid: bool = True,
        rerank: bool = True,
        live_crawl: bool = True,
    ) -> LiveSearchResponse:
        """Execute autonomous live search: query SERP, crawl new pages, index, and rerank."""
        import time

        start_time = time.monotonic()
        serp_results: list[SerpResultItem] = []
        new_pages_indexed = 0

        if live_crawl:
            serp_results = await self.serp_client.search(query, limit=limit)
            if serp_results:
                http_fetcher = AsyncHttpFetcher(timeout=6.0)
                browser_fetcher = PlaywrightBrowserFetcher()
                try:
                    tasks = [
                        self._fetch_and_index_url(item.url, http_fetcher, browser_fetcher)
                        for item in serp_results
                    ]
                    docs = await asyncio.gather(*tasks, return_exceptions=True)
                    new_pages_indexed = sum(1 for d in docs if isinstance(d, CleanDocument))
                finally:
                    await http_fetcher.close()
                    await browser_fetcher.close()

        crawl_elapsed = time.monotonic() - start_time

        # Retrieve ranked evidence passages from the search index
        hits, total_tokens = await self.index.search(
            query=query,
            limit=limit,
            max_tokens=max_tokens,
            hybrid=hybrid,
        )

        if rerank and hits:
            hits, total_tokens = self.reranker.rerank(
                query=query,
                hits=hits,
                max_tokens=max_tokens,
            )

        citations_md = CitationFormatter.format_markdown(hits)

        return LiveSearchResponse(
            query=query,
            estimated_tokens=total_tokens,
            max_tokens=max_tokens,
            results=hits,
            serp_sources=serp_results,
            citations_markdown=citations_md,
            new_pages_indexed=new_pages_indexed,
            crawl_elapsed_seconds=round(crawl_elapsed, 2),
        )
