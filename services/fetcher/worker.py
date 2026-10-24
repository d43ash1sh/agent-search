from __future__ import annotations

import asyncio
import logging

from services.cache.redis_cache import AbstractUrlCache, get_url_cache
from services.common.config import settings
from services.common.models import FetchRequest
from services.fetcher.browser_client import PlaywrightBrowserFetcher
from services.fetcher.http_client import AsyncHttpFetcher
from services.fetcher.spa_detector import is_spa_shell
from services.queue.base import AbstractQueueBroker
from services.queue.redis_broker import RedisQueueBroker

logger = logging.getLogger(__name__)


class FetcherWorker:
    """I/O-bound worker that pulls URLs from fetch queue and pushes raw HTML to staging queue."""

    def __init__(
        self,
        broker: AbstractQueueBroker,
        http_fetcher: AsyncHttpFetcher | None = None,
        browser_fetcher: PlaywrightBrowserFetcher | None = None,
        cache: AbstractUrlCache | None = None,
    ) -> None:
        self.broker = broker
        self.http_fetcher = http_fetcher or AsyncHttpFetcher()
        self.browser_fetcher = browser_fetcher
        self.cache = cache or get_url_cache()
        self.is_running = False

    async def process_request(self, request: FetchRequest) -> bool:
        """Fetch a single URL and route output either to staging or DLQ."""
        # 1. Check Redis fast cache: If page is already cached and unexpired, reuse immediately
        cached_record = await self.cache.get_cached_page(request.url)
        if cached_record is not None:
            logger.info("Serving %s directly from Redis page cache (0ms fetch)", request.url)
            cached_record.metadata = request.metadata
            await self.broker.enqueue_raw(cached_record)
            return True

        # 2. In-flight Crawl Lock: Prevent duplicate concurrent fetches across workers
        lock_acquired = await self.cache.acquire_crawl_lock(
            request.url, ttl=settings.crawler_lock_ttl_seconds
        )
        if not lock_acquired:
            logger.info(
                "URL %s is currently being fetched by another worker; skipping duplicate", request.url
            )
            return False

        try:
            use_browser = request.render_js or settings.enable_playwright_fallback
            raw_record = None
            failed_record = None

            if use_browser:
                if not self.browser_fetcher:
                    self.browser_fetcher = PlaywrightBrowserFetcher()
                raw_record, failed_record = await self.browser_fetcher.fetch(request.url)
            else:
                raw_record, failed_record = await self.http_fetcher.fetch(request.url)
                # Automatic SPA detection & dynamic browser upgrade
                if raw_record and is_spa_shell(raw_record.raw_html):
                    logger.info(
                        "Detected unrendered client-side SPA shell for %s. Automatically upgrading to Playwright...",
                        request.url,
                    )
                    if not self.browser_fetcher:
                        self.browser_fetcher = PlaywrightBrowserFetcher()
                    try:
                        browser_record, _ = await self.browser_fetcher.fetch(request.url)
                        if browser_record and len(browser_record.raw_html) > len(raw_record.raw_html):
                            raw_record = browser_record
                    except Exception as browser_err:
                        logger.debug("Playwright auto-fallback skipped: %s", browser_err)

            if raw_record:
                raw_record.metadata = request.metadata
                # 3. Cache fetched page and mark URL as seen in Redis
                await self.cache.set_cached_page(raw_record)
                await self.cache.mark_seen(request.url)
                await self.broker.enqueue_raw(raw_record)
                logger.info("Successfully fetched %s (status %d)", request.url, raw_record.status_code)
                return True
            elif failed_record:
                await self.broker.route_to_dlq(failed_record)
                logger.warning(
                    "Fetch failed for %s (%s): %s",
                    request.url,
                    failed_record.error_type,
                    failed_record.error_message,
                )
                return False
            return False
        finally:
            await self.cache.release_crawl_lock(request.url)

    async def run_once(self) -> bool:
        """Process a single fetch job. Returns True if a job was processed."""
        request = await self.broker.dequeue_fetch(timeout=1.0)
        if not request:
            return False
        return await self.process_request(request)

    async def run_loop(self) -> None:
        """Continuously process fetch jobs."""
        self.is_running = True
        logger.info("Fetcher worker running...")
        while self.is_running:
            processed = await self.run_once()
            if not processed:
                await asyncio.sleep(0.1)

    async def close(self) -> None:
        self.is_running = False
        await self.http_fetcher.close()
        if self.browser_fetcher:
            await self.browser_fetcher.close()
        await self.cache.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    broker = RedisQueueBroker()
    worker = FetcherWorker(broker=broker)
    try:
        asyncio.run(worker.run_loop())
    except KeyboardInterrupt:
        logger.info("Fetcher worker stopped by user.")


if __name__ == "__main__":
    main()
