from __future__ import annotations

import asyncio
import logging
import math
import urllib.parse

from pydantic import BaseModel, Field

from services.common.citations import CitationFormatter
from services.common.config import settings
from services.common.hashing import compute_simhash
from services.common.models import CleanDocument, SearchHit
from services.discovery.tier_registry import DomainTierRegistry
from services.extractor.chunker import chunk_document
from services.extractor.link_harvester import LinkHarvester
from services.extractor.trafilatura_cleaner import TrafilaturaCleaner
from services.fetcher.browser_client import PlaywrightBrowserFetcher
from services.fetcher.http_client import AsyncHttpFetcher
from services.indexer.base import AbstractSearchIndex
from services.indexer.reranker import NeuralReranker
from services.search.serp_client import DuckDuckGoSerpClient, FederatedSerpClient, SerpResultItem

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
    engines_used: list[str] = Field(default_factory=list)


class LiveSearchEngine:
    """End-to-end real-time search and live web indexing engine.

    Federates discovery across Bing, Brave, DuckDuckGo, and SearXNG; fetches and cleans content in real time;
    chunks and embeds vectors; updates directed link graph edges; and returns neural-reranked citations.
    """

    def __init__(
        self,
        index: AbstractSearchIndex,
        serp_client: FederatedSerpClient | DuckDuckGoSerpClient | None = None,
        reranker: NeuralReranker | None = None,
    ) -> None:
        self.index = index
        self.serp_client = serp_client or FederatedSerpClient()
        self.reranker = reranker or NeuralReranker()
        self.cleaner = TrafilaturaCleaner()
        self.harvester = LinkHarvester()

    async def _fetch_and_index_url(
        self,
        url: str,
        http_fetcher: AsyncHttpFetcher,
    ) -> CleanDocument | None:
        """Fetch a single URL, clean HTML, chunk passages, harvest link graph edges, and index."""
        try:
            record, _failed = await http_fetcher.fetch(url)

            # Lazy on-demand fallback to Playwright only if HTTP returned minimal payload and enabled
            if (not record or len(record.raw_html.strip()) < 200) and settings.enable_playwright_fallback:
                try:
                    browser_fetcher = PlaywrightBrowserFetcher()
                    b_rec, _ = await browser_fetcher.fetch(url)
                    await browser_fetcher.close()
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

            # Harvest outlinks and persist directed link edges into domain authority graph
            try:
                harvested = self.harvester.harvest(record.raw_html, record.url)
                if harvested.edges:
                    await self.index.record_link_edges([e.model_dump() for e in harvested.edges])
            except Exception as h_err:
                logger.debug("Link graph recording note for %s: %s", record.url, h_err)

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
        engines: list[str] | None = None,
    ) -> LiveSearchResponse:
        """Execute real-time federated search, live crawl & index new pages, and neural rerank."""
        import time

        start_time = time.monotonic()
        clean_query = query.strip()
        serp_results: list[SerpResultItem] = []
        new_pages_indexed = 0
        engines_used: list[str] = []

        if not clean_query:
            return LiveSearchResponse(
                query=query,
                estimated_tokens=0,
                max_tokens=max_tokens,
                results=[],
                serp_sources=[],
                citations_markdown="",
                new_pages_indexed=0,
                crawl_elapsed_seconds=0.0,
                engines_used=[],
            )

        if live_crawl:
            if isinstance(self.serp_client, FederatedSerpClient):
                serp_results = await self.serp_client.search(clean_query, limit=limit, engines=engines)
            else:
                serp_results = await self.serp_client.search(clean_query, limit=limit)

            engines_used = list({s.engine for s in serp_results if s.engine})

            if serp_results:
                http_fetcher = AsyncHttpFetcher(timeout=settings.live_fetch_timeout)
                try:
                    tasks = [
                        self._fetch_and_index_url(item.url, http_fetcher)
                        for item in serp_results[: settings.live_search_concurrency]
                    ]
                    docs = await asyncio.gather(*tasks, return_exceptions=True)
                    new_pages_indexed = sum(1 for d in docs if isinstance(d, CleanDocument))
                finally:
                    await http_fetcher.close()

        crawl_elapsed = time.monotonic() - start_time

        # 1. Retrieve ranked evidence passages from the search index
        hits, total_tokens = await self.index.search(
            query=clean_query,
            limit=limit * 3,
            max_tokens=max_tokens * 2,
            hybrid=hybrid,
        )

        # 2. Apply Domain Authority and Domain Tier multipliers
        adjusted_hits: list[SearchHit] = []
        for h in hits:
            domain = (urllib.parse.urlsplit(h.url).hostname or "").lower()
            authority = await self.index.get_domain_authority(domain)
            tier_mult = DomainTierRegistry.get_multiplier(domain)
            authority_multiplier = (1.0 + (0.35 * math.log(1.0 + max(authority, 0.0)))) * tier_mult
            adjusted_hits.append(
                SearchHit(
                    evidence_id=h.evidence_id,
                    url=h.url,
                    canonical_url=h.canonical_url,
                    title=h.title,
                    text=h.text,
                    score=h.score * authority_multiplier,
                    estimated_tokens=h.estimated_tokens,
                    match_type=h.match_type,
                    author=h.author,
                    published_date=h.published_date,
                )
            )

        # 3. Neural cross-encoder reranking
        if rerank and adjusted_hits:
            reranked_hits, _ = self.reranker.rerank(
                query=clean_query,
                hits=adjusted_hits,
                max_tokens=max_tokens,
                top_k=limit * 2,
            )
        else:
            reranked_hits = adjusted_hits

        # 4. Enforce domain diversity (max 2 per domain) and token budget
        final_hits: list[SearchHit] = []
        domain_hit_counts: dict[str, int] = {}
        acc_tokens = 0
        for rh in reranked_hits:
            domain = (urllib.parse.urlsplit(rh.url).hostname or "").lower()
            current_count = domain_hit_counts.get(domain, 0)
            if current_count >= 2:
                continue

            if acc_tokens + rh.estimated_tokens <= max_tokens or not final_hits:
                final_hits.append(rh)
                domain_hit_counts[domain] = current_count + 1
                acc_tokens += rh.estimated_tokens
                if len(final_hits) >= limit:
                    break

        # 5. Zero-empty resilience fallback: synthesize from SERP snippets if index had 0 passages
        if not final_hits and serp_results:
            for idx, s in enumerate(serp_results[:limit]):
                snippet_text = s.snippet.strip() or s.title.strip()
                tokens = max(len(snippet_text.split()), 10)
                final_hits.append(
                    SearchHit(
                        evidence_id=f"serp_{idx + 1}_{abs(hash(s.url)) % 1000000:06d}",
                        url=s.url,
                        canonical_url=s.url,
                        title=s.title,
                        text=snippet_text,
                        score=1.0 - (idx * 0.05),
                        estimated_tokens=tokens,
                        match_type="lexical",
                    )
                )
                acc_tokens += tokens

        citations_md = CitationFormatter.format_markdown(final_hits)

        return LiveSearchResponse(
            query=clean_query,
            estimated_tokens=acc_tokens or total_tokens,
            max_tokens=max_tokens,
            results=final_hits,
            serp_sources=serp_results,
            citations_markdown=citations_md,
            new_pages_indexed=new_pages_indexed,
            crawl_elapsed_seconds=round(crawl_elapsed, 2),
            engines_used=engines_used,
        )
