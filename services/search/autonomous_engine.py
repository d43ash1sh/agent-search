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
from services.discovery.news_discovery import OpenNewsDiscovery
from services.discovery.open_search import OpenKnowledgeDiscovery
from services.discovery.tier_registry import DomainTierRegistry
from services.extractor.chunker import chunk_document
from services.extractor.trafilatura_cleaner import TrafilaturaCleaner
from services.fetcher.http_client import AsyncHttpFetcher
from services.indexer.base import AbstractSearchIndex
from services.indexer.reranker import NeuralReranker
from services.search.decomposer import QueryDecomposer, QueryDecomposition
from services.search.serp_client import DuckDuckGoSerpClient
from services.search.synthesizer import EvidenceSynthesisResult, EvidenceSynthesizer

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
    decomposition: QueryDecomposition | None = None
    synthesis: EvidenceSynthesisResult | None = None


class AutonomousSearchEngine:
    """100% self-contained autonomous search engine.

    Retrieves candidates using hybrid vector + lexical search, boosts candidates
    with internal PageRank domain authority and domain tier weighting, applies cross-encoder neural reranking,
    and autonomously bootstraps cold queries via open discovery without commercial SERPs.
    """

    def __init__(
        self,
        index: AbstractSearchIndex,
        reranker: NeuralReranker | None = None,
        decomposer: QueryDecomposer | None = None,
        synthesizer: EvidenceSynthesizer | None = None,
    ) -> None:
        self.index = index
        self.reranker = reranker or NeuralReranker()
        self.decomposer = decomposer or QueryDecomposer()
        self.synthesizer = synthesizer or EvidenceSynthesizer()
        self.cleaner = TrafilaturaCleaner()
        self.http_fetcher = AsyncHttpFetcher(timeout=10.0)
        self.open_discovery = OpenKnowledgeDiscovery()
        self.common_crawl = CommonCrawlClient()
        self.news_discovery = OpenNewsDiscovery()
        self.serp_client = DuckDuckGoSerpClient()

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
        decompose: bool = False,
        synthesize: bool = False,
    ) -> AutonomousSearchResponse:
        """Execute autonomous search: query local hybrid index, apply PageRank authority boost,

        domain tier weighting, cross-encoder reranking, and trigger open discovery if cold.
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

        decomposition: QueryDecomposition | None = None
        if decompose or synthesize:
            decomposition = self.decomposer.decompose(clean_query)

        # 1. Search local hybrid index (lexical + dense vector)
        if decomposition and decomposition.sub_queries:
            sub_queries_to_run = [clean_query] + [sq.query for sq in decomposition.sub_queries]
            search_tasks = [
                self.index.search(
                    query=sq,
                    limit=limit * 2,
                    max_tokens=max_tokens * 2,
                    hybrid=True,
                )
                for sq in sub_queries_to_run
            ]
            all_results = await asyncio.gather(*search_tasks, return_exceptions=True)
            candidate_dict: dict[str, SearchHit] = {}
            for res in all_results:
                if isinstance(res, tuple):
                    sub_hits, _ = res
                    for sh in sub_hits:
                        if (
                            sh.evidence_id not in candidate_dict
                            or sh.score > candidate_dict[sh.evidence_id].score
                        ):
                            candidate_dict[sh.evidence_id] = sh
            hits = list(candidate_dict.values())
        else:
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
            logger.info("Cold query '%s': triggering multi-channel open web discovery...", clean_query)

            candidate_urls: list[str] = []
            seen_discovery_domains: set[str] = set()

            # A. Broad SERP discovery (captures news dailies, blogs, official portals)
            try:
                serp_results = await self.serp_client.search(clean_query, limit=5)
                for item in serp_results:
                    if item.domain and item.domain not in seen_discovery_domains:
                        seen_discovery_domains.add(item.domain)
                        candidate_urls.append(item.url)
            except Exception as exc:
                logger.debug("SERP discovery note: %s", exc)

            # B. Open News RSS discovery (captures live regional & national reporting)
            try:
                news_items = await self.news_discovery.search_news(clean_query, limit=5)
                for n_item in news_items:
                    if n_item.domain and n_item.domain not in seen_discovery_domains:
                        seen_discovery_domains.add(n_item.domain)
                        candidate_urls.append(n_item.url)
            except Exception as exc:
                logger.debug("News discovery note: %s", exc)

            # C. Open Knowledge / Wikimedia discovery (captures encyclopedic entries)
            try:
                open_results = await self.open_discovery.search(clean_query, limit=3)
                for k_item in open_results:
                    if k_item.domain and k_item.domain not in seen_discovery_domains:
                        seen_discovery_domains.add(k_item.domain)
                        candidate_urls.append(k_item.url)
            except Exception as exc:
                logger.debug("Knowledge discovery note: %s", exc)

            # D. Common Crawl open index fallback
            if not candidate_urls:
                cc_records = await self.common_crawl.discover_urls(f"*{clean_query}*", limit=3)
                candidate_urls = [r.url for r in cc_records]

            if candidate_urls:
                discovered_urls = await self._fetch_and_index_urls(candidate_urls)
                # Re-query local index after indexing new pages
                hits, _total_tokens = await self.index.search(
                    query=clean_query,
                    limit=limit * 3,
                    max_tokens=max_tokens * 2,
                    hybrid=True,
                )

        if not hits:
            elapsed = time.perf_counter() - t0
            synthesis_res: EvidenceSynthesisResult | None = None
            if synthesize:
                intent = decomposition.intent if decomposition else self.decomposer.detect_intent(clean_query)
                synthesis_res = self.synthesizer.synthesize(query=clean_query, hits=[], intent=intent)

            return AutonomousSearchResponse(
                query=clean_query,
                results=[],
                estimated_tokens=0,
                max_tokens=max_tokens,
                citations_markdown="",
                discovered_urls=discovered_urls,
                discovery_mode=discovery_mode,
                elapsed_seconds=round(elapsed, 2),
                decomposition=decomposition,
                synthesis=synthesis_res,
            )

        # 3. Apply PageRank / Domain Authority and Domain Tier multiplier to candidate scores
        adjusted_hits: list[SearchHit] = []
        for h in hits:
            domain = (urllib.parse.urlsplit(h.url).hostname or "").lower()
            authority = await self.index.get_domain_authority(domain)
            tier_mult = DomainTierRegistry.get_multiplier(domain)
            # Logarithmic authority boost multiplied by domain tier multiplier
            authority_multiplier = (1.0 + (0.35 * math.log(1.0 + max(authority, 0.0)))) * tier_mult
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
            top_k=limit * 2,
        )

        # 5. Enforce token budget, count, and domain diversity (max 2 per domain)
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

        # 6. Build LLM Grounding Citations Markdown & Optional Evidence Synthesis
        citations_md = CitationFormatter.format_markdown(final_hits)
        elapsed = time.perf_counter() - t0

        synthesis_result: EvidenceSynthesisResult | None = None
        if synthesize:
            intent = decomposition.intent if decomposition else self.decomposer.detect_intent(clean_query)
            synthesis_result = self.synthesizer.synthesize(
                query=clean_query,
                hits=final_hits,
                intent=intent,
            )

        return AutonomousSearchResponse(
            query=clean_query,
            results=final_hits,
            estimated_tokens=acc_tokens,
            max_tokens=max_tokens,
            citations_markdown=citations_md,
            discovered_urls=discovered_urls,
            discovery_mode=discovery_mode,
            elapsed_seconds=round(elapsed, 2),
            decomposition=decomposition,
            synthesis=synthesis_result,
        )

    async def search_evidence(
        self,
        query: str,
        limit: int = 5,
        max_tokens: int = 1500,
        allow_autonomous_discovery: bool = True,
    ) -> AutonomousSearchResponse:
        """Convenience method executing full multi-angle query decomposition and cross-document evidence synthesis."""
        return await self.search(
            query=query,
            limit=limit,
            max_tokens=max_tokens,
            allow_autonomous_discovery=allow_autonomous_discovery,
            decompose=True,
            synthesize=True,
        )
