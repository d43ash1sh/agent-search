from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import urllib.parse
from typing import Any

from pydantic import BaseModel

from services.common.hashing import compute_simhash
from services.common.models import CleanDocument
from services.discovery.seeds import SeedManager
from services.extractor.chunker import chunk_document
from services.extractor.link_harvester import LinkHarvester
from services.extractor.trafilatura_cleaner import TrafilaturaCleaner
from services.fetcher.browser_client import PlaywrightBrowserFetcher
from services.fetcher.http_client import AsyncHttpFetcher
from services.indexer.base import AbstractSearchIndex
from services.indexer.pagerank import PageRankCalculator

logger = logging.getLogger(__name__)


class CrawlerStats(BaseModel):
    is_running: bool = False
    pages_crawled: int = 0
    pages_failed: int = 0
    links_discovered: int = 0
    domains_indexed: int = 0
    elapsed_seconds: float = 0.0


class AutonomousCrawlerDaemon:
    """Continuous background autonomous crawler that dequeues from the frontier,

    respects domain politeness, cleans text, harvests outlinks, updates the link graph,
    and indexes vectors into pgvector/SQLite without relying on third-party SERPs.
    """

    def __init__(
        self,
        index: AbstractSearchIndex,
        politeness_delay: float = 1.0,
        concurrency: int = 4,
        max_depth: int = 3,
    ) -> None:
        self.index = index
        self.politeness_delay = politeness_delay
        self.concurrency = concurrency
        self.max_depth = max_depth

        self.http_fetcher = AsyncHttpFetcher(timeout=10.0)
        self.browser_fetcher: PlaywrightBrowserFetcher | None = None
        self.cleaner = TrafilaturaCleaner()
        self.seed_manager = SeedManager()
        self.pagerank_calc = PageRankCalculator()

        self._domain_last_visited: dict[str, float] = {}
        self._is_running = False
        self._task: asyncio.Task[None] | None = None
        self._start_time: float = 0.0
        self._pages_crawled = 0
        self._pages_failed = 0
        self._links_discovered = 0
        self._seen_domains: set[str] = set()

    def get_stats(self) -> CrawlerStats:
        """Return live telemetry for the autonomous crawling daemon."""
        elapsed = (time.perf_counter() - self._start_time) if self._is_running else 0.0
        return CrawlerStats(
            is_running=self._is_running,
            pages_crawled=self._pages_crawled,
            pages_failed=self._pages_failed,
            links_discovered=self._links_discovered,
            domains_indexed=len(self._seen_domains),
            elapsed_seconds=round(elapsed, 2),
        )

    async def bootstrap_seeds(self) -> int:
        """Inject curated authority seeds into the frontier if ledger is empty."""
        stats = await self.index.get_frontier_stats()
        if stats.get("total_discovered", 0) > 0:
            return 0

        seeds = self.seed_manager.get_seeds()
        records = [
            {
                "url": s.url,
                "domain": s.domain,
                "source_url": "seed://curated",
                "depth": 0,
                "status": "discovered",
            }
            for s in seeds
        ]
        count = await self.index.record_frontier_urls(records)
        logger.info("Bootstrapped frontier with %d high-authority seed URLs.", count)
        return count

    async def _crawl_single_url(self, item: dict[str, Any]) -> bool:
        """Fetch, clean, chunk, link-harvest, and index a single URL."""
        url = item["url"]
        domain = item.get("domain") or (urllib.parse.urlsplit(url).hostname or "").lower()
        depth = int(item.get("depth", 0))

        # Enforce domain politeness
        now = time.perf_counter()
        last_hit = self._domain_last_visited.get(domain, 0.0)
        wait_time = self.politeness_delay - (now - last_hit)
        if wait_time > 0:
            await asyncio.sleep(wait_time)
        self._domain_last_visited[domain] = time.perf_counter()

        try:
            record, _failed = await self.http_fetcher.fetch(url)

            # Auto-fallback to Playwright for dynamic / blocked pages if pool exists
            if (not record or len(record.raw_html.strip()) < 200) and self.browser_fetcher is not None:
                try:
                    b_rec, _ = await self.browser_fetcher.fetch(url)
                    if b_rec and len(b_rec.raw_html) > 200:
                        record = b_rec
                except Exception as b_err:
                    logger.debug("Playwright fallback note for %s: %s", url, b_err)

            if not record or not record.raw_html.strip():
                await self.index.update_frontier_status(url, "failed")
                self._pages_failed += 1
                return False

            # Extract clean content
            extracted = self.cleaner.extract(record.raw_html, record.url)
            if not extracted or not extracted["clean_text"].strip():
                await self.index.update_frontier_status(url, "crawled")
                self._pages_crawled += 1
                return True

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
            await self.index.update_frontier_status(url, "crawled")

            self._pages_crawled += 1
            self._seen_domains.add(domain)

            # Harvest outlinks to grow the frontier & link graph recursively
            if depth < self.max_depth:
                harvested = LinkHarvester.harvest(record.raw_html, url, max_links=80)
                new_urls = [
                    {
                        "url": out_url,
                        "domain": (urllib.parse.urlsplit(out_url).hostname or "").lower(),
                        "source_url": url,
                        "depth": depth + 1,
                        "status": "discovered",
                    }
                    for out_url in (harvested.internal_links + harvested.external_links)
                ]
                if new_urls:
                    added = await self.index.record_frontier_urls(new_urls)
                    self._links_discovered += added

                # Record graph edges for PageRank calculation
                if harvested.edges:
                    edge_records = [
                        {
                            "source_url": e.source_url,
                            "target_url": e.target_url,
                            "source_domain": e.source_domain,
                            "target_domain": e.target_domain,
                            "is_internal": e.is_internal,
                        }
                        for e in harvested.edges
                    ]
                    await self.index.record_link_edges(edge_records)

            return True

        except Exception as exc:
            logger.warning("Error crawling %s: %s", url, exc)
            await self.index.update_frontier_status(url, "failed")
            self._pages_failed += 1
            return False

    async def crawl_batch(self, batch_size: int = 10) -> int:
        """Fetch and crawl a batch of pending frontier URLs concurrently."""
        pending = await self.index.get_pending_frontier_urls(limit=batch_size, max_per_domain=3)
        if not pending:
            return 0

        # Mark selected URLs as 'queued' to avoid double-processing
        for item in pending:
            await self.index.update_frontier_status(item["url"], "queued")

        semaphore = asyncio.Semaphore(self.concurrency)

        async def worker(u_item: dict[str, Any]) -> bool:
            async with semaphore:
                return await self._crawl_single_url(u_item)

        tasks = [worker(item) for item in pending]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        successful = sum(1 for r in results if r is True)
        return successful

    async def _daemon_loop(self) -> None:
        """Infinite loop driving continuous autonomous web exploration."""
        await self.bootstrap_seeds()
        logger.info("Autonomous crawler daemon started.")
        while self._is_running:
            try:
                processed = await self.crawl_batch(batch_size=self.concurrency * 2)
                if processed == 0:
                    # If frontier is empty, check if we should add new seeds or sleep
                    await asyncio.sleep(2.0)
                else:
                    await asyncio.sleep(0.1)
            except asyncio.CancelledError:
                break
            except Exception as loop_err:
                logger.warning("Crawler daemon loop error: %s", loop_err)
                await asyncio.sleep(1.0)
        logger.info("Autonomous crawler daemon stopped.")

    def start(self) -> None:
        """Start the autonomous crawling daemon in the background."""
        if self._is_running:
            return
        self._is_running = True
        self._start_time = time.perf_counter()
        self._task = asyncio.create_task(self._daemon_loop())

    async def stop(self) -> None:
        """Gracefully stop the background crawling daemon."""
        if not self._is_running:
            return
        self._is_running = False
        if self._task and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        self._task = None
