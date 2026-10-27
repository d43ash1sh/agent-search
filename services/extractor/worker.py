from __future__ import annotations

import asyncio
import logging
import re
from urllib.parse import urljoin, urlsplit

from crawler.urls import host_is_allowed, normalize_url
from services.cache.redis_cache import AbstractUrlCache, get_url_cache
from services.common.config import settings
from services.common.hashing import compute_sha256, compute_simhash
from services.common.models import CleanDocument, FetchRequest, RawPageRecord
from services.extractor.chunker import chunk_document
from services.extractor.dedup_filter import DeduplicationFilter
from services.extractor.fallback_cleaner import FallbackCleaner
from services.extractor.trafilatura_cleaner import TrafilaturaCleaner
from services.indexer.base import AbstractSearchIndex
from services.indexer.factory import create_search_index
from services.indexer.sqlite_index import SQLiteSearchIndex
from services.queue.base import AbstractQueueBroker
from services.queue.redis_broker import RedisQueueBroker

logger = logging.getLogger(__name__)


def is_listing_or_feed_url(url: str) -> bool:
    """Detect if a URL is an aggregation, category, tag, archive, or pagination listing.

    These pages are useful for discovering outlinks, but indexing them directly
    creates confusing multi-article snippets for AI agents.
    """
    parsed = urlsplit(url)
    path = parsed.path.lower()
    query = parsed.query.lower()

    listing_path_patterns = (
        "/tags/",
        "/tag/",
        "/category/",
        "/categories/",
        "/topics/",
        "/topic/",
        "/archive/",
        "/archives/",
        "/page/",
        "/pages/",
        "/author/",
        "/authors/",
        "/search",
        "/feed",
        "/rss",
    )
    if any(p in path for p in listing_path_patterns):
        return True

    if path.endswith(("/tags", "/category", "/archive", "/feed", "/rss.xml", "/atom.xml")):
        return True

    # Pagination query parameters: ?page=2, ?p=2, ?offset=
    return bool(re.search(r"(?:^|&)(?:page|p|offset|paged)=\d+", query))


class ExtractorWorker:
    """CPU-bound worker that extracts, chunks, deduplicates, and manages the URL frontier."""

    def __init__(
        self,
        broker: AbstractQueueBroker,
        index: AbstractSearchIndex | None = None,
        sqlite_path: str | None = None,
        cache: AbstractUrlCache | None = None,
        concurrency: int | None = None,
    ) -> None:
        self.broker = broker
        self.trafilatura = TrafilaturaCleaner()
        self.fallback = FallbackCleaner()
        self.dedup = DeduplicationFilter()
        self.cache = cache or get_url_cache()
        self.concurrency = concurrency or settings.extractor_concurrency
        if index is not None:
            self.index: AbstractSearchIndex = index
        elif sqlite_path:
            self.index = SQLiteSearchIndex(sqlite_path)
        else:
            self.index = create_search_index()
        self.is_running = False

    @staticmethod
    def extract_all_links(html: str, base_url: str) -> tuple[list[str], list[str]]:
        """Extract both internal links and external outlinks from HTML."""
        import re

        base_host = (urlsplit(base_url).hostname or "").lower()
        internal: set[str] = set()
        external: set[str] = set()

        hrefs = re.findall(r"""<a\b[^>]*href=["']([^"'#]+)["']""", html, flags=re.IGNORECASE)
        for raw_href in hrefs:
            full = urljoin(base_url, raw_href)
            normalized = normalize_url(full)
            if not normalized:
                continue

            ext = normalized.split("?")[0].split(".")[-1].lower()
            if ext in {
                "png",
                "jpg",
                "jpeg",
                "gif",
                "svg",
                "css",
                "js",
                "pdf",
                "zip",
                "exe",
                "tar",
                "gz",
                "mp4",
                "mp3",
                "webp",
                "ico",
            }:
                continue

            host = (urlsplit(normalized).hostname or "").lower()
            if not host:
                continue

            if host == base_host or host.endswith("." + base_host):
                internal.add(normalized)
            else:
                external.add(normalized)

        return list(internal), list(external)

    @classmethod
    def extract_internal_links(cls, html: str, base_url: str, allowed_domain: str) -> list[str]:
        internal, _ = cls.extract_all_links(html, base_url)
        if allowed_domain:
            return [url for url in internal if host_is_allowed(url, [allowed_domain])]
        return internal

    def process_record(self, raw: RawPageRecord) -> CleanDocument | None:
        """Process a single raw HTML payload into a CleanDocument."""
        # Skip indexing listing / tag / archive pages to prevent multi-article aggregation noise
        force_index = bool(raw.metadata and raw.metadata.get("force_index"))
        if not force_index and is_listing_or_feed_url(raw.url):
            logger.info(
                "Skipping document indexing for listing/feed page: %s (used for link discovery only)", raw.url
            )
            return None

        extracted = self.trafilatura.extract(raw.raw_html, raw.url)
        if not extracted:
            extracted = self.fallback.extract(raw.raw_html, raw.url)

        if not extracted or not extracted["clean_text"]:
            logger.info("No extractable text found in %s", raw.url)
            return None

        clean_text = extracted["clean_text"]
        is_dup, reason = self.dedup.is_duplicate(clean_text)
        if is_dup:
            logger.info("Dropping %s: %s", raw.url, reason)
            return None

        self.dedup.register(clean_text)
        passages = chunk_document(raw.canonical_url, clean_text)

        return CleanDocument(
            url=raw.url,
            canonical_url=raw.canonical_url,
            title=extracted["title"],
            author=extracted.get("author"),
            published_date=extracted.get("published_date"),
            sitename=extracted.get("sitename"),
            description=extracted.get("description"),
            clean_text=clean_text,
            markdown=extracted.get("markdown") or clean_text,
            passages=passages,
            content_hash=compute_sha256(clean_text),
            simhash=compute_simhash(clean_text),
        )

    async def run_once(self) -> bool:
        """Process one item from the staging queue. Returns True if an item was processed."""
        raw = await self.broker.dequeue_raw(timeout=1.0)
        if not raw:
            return False

        try:
            doc = self.process_record(raw)
            if doc:
                await self.broker.enqueue_document(doc)

            # Mark current page as seen in Redis cache
            await self.cache.mark_seen(raw.url)

            # Discover all links (both internal within the site and external outlinks)
            internal_links, external_links = self.extract_all_links(raw.raw_html, raw.url)
            current_depth = int(raw.metadata.get("depth", 0)) if raw.metadata else 0

            # Fast Redis check: Filter out already seen URLs before queuing
            all_links = internal_links + external_links
            unseen_links = set(await self.cache.filter_unseen_urls(all_links))

            # Store discovered URLs in the persistent frontier ledger
            frontier_records = [
                {
                    "url": link,
                    "domain": (urlsplit(link).hostname or "").lower(),
                    "source_url": raw.url,
                    "depth": current_depth + 1,
                    "status": "discovered",
                }
                for link in (internal_links + external_links)
            ]
            if frontier_records:
                await self.index.record_frontier_urls(frontier_records)

            # Recursive crawling link extraction & queueing
            if raw.metadata and raw.metadata.get("recursive"):
                max_depth = int(raw.metadata.get("max_depth", 1))
                allowed_domain = str(raw.metadata.get("allowed_domain", ""))
                render_js = bool(raw.metadata.get("render_js", False))
                follow_external = bool(raw.metadata.get("follow_external", False))
                max_external_depth = int(raw.metadata.get("max_external_depth", 1))

                # 1. Enqueue internal child links within allowed depth
                if current_depth < max_depth:
                    for link in internal_links:
                        if link not in unseen_links:
                            continue
                        if allowed_domain and not host_is_allowed(link, [allowed_domain]):
                            continue
                        child_meta = dict(raw.metadata)
                        child_meta["depth"] = current_depth + 1
                        await self.broker.enqueue_fetch(
                            FetchRequest(
                                url=link,
                                depth=current_depth + 1,
                                render_js=render_js,
                                metadata=child_meta,
                            )
                        )
                        await self.cache.mark_seen(link)
                        await self.index.update_frontier_status(link, "queued")

                # 2. Enqueue external outlinks if cross-site broad crawling is requested
                if follow_external and current_depth < max_external_depth:
                    domain_counts: dict[str, int] = {}
                    for link in external_links:
                        if link not in unseen_links:
                            continue
                        target_domain = (urlsplit(link).hostname or "").lower()
                        # Limit to max 5 external links per domain per page to maintain broad diversity
                        if domain_counts.get(target_domain, 0) >= 5:
                            continue
                        domain_counts[target_domain] = domain_counts.get(target_domain, 0) + 1

                        ext_meta = dict(raw.metadata)
                        ext_meta["depth"] = current_depth + 1
                        ext_meta["allowed_domain"] = target_domain
                        await self.broker.enqueue_fetch(
                            FetchRequest(
                                url=link,
                                depth=current_depth + 1,
                                render_js=render_js,
                                metadata=ext_meta,
                            )
                        )
                        await self.cache.mark_seen(link)
                        await self.index.update_frontier_status(link, "queued")

            return True
        except Exception as error:
            logger.error("Extraction error for %s: %s", raw.url, error, exc_info=True)
            return False

    async def _consumer_loop(self, worker_id: int) -> None:
        """Parallel consumer routine extracting from staging queue."""
        while self.is_running:
            try:
                processed = await self.run_once()
                if not processed:
                    await asyncio.sleep(0.2)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("Extractor consumer %d error: %s", worker_id, exc)
                await asyncio.sleep(0.5)

    async def run_loop(self, concurrency: int | None = None) -> None:
        """Continuously process items from the staging queue with parallel consumers."""
        num_workers = concurrency or self.concurrency
        self.is_running = True
        logger.info("Extractor worker started with %d parallel consumers.", num_workers)
        if num_workers <= 1:
            while self.is_running:
                processed = await self.run_once()
                if not processed:
                    await asyncio.sleep(0.1)
        else:
            consumers = [asyncio.create_task(self._consumer_loop(i)) for i in range(num_workers)]
            try:
                await asyncio.gather(*consumers)
            except asyncio.CancelledError:
                for c in consumers:
                    c.cancel()
                await asyncio.gather(*consumers, return_exceptions=True)

    async def close(self) -> None:
        self.is_running = False
        await self.index.close()

    def stop(self) -> None:
        self.is_running = False


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    broker = RedisQueueBroker()
    worker = ExtractorWorker(broker=broker)
    try:
        asyncio.run(worker.run_loop())
    except KeyboardInterrupt:
        logger.info("Extractor worker stopped by user.")


if __name__ == "__main__":
    main()
