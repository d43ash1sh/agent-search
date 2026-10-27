from __future__ import annotations

import asyncio
import logging

from services.cache.redis_cache import AbstractUrlCache, get_url_cache, make_cache_key
from services.common.config import settings
from services.common.models import FailedFetchRecord, FetchRequest
from services.fetcher.browser_client import PlaywrightBrowserFetcher
from services.fetcher.http_client import AsyncHttpFetcher
from services.fetcher.politeness import PolitenessManager
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
        concurrency: int | None = None,
        playwright_workers: int | None = None,
    ) -> None:
        self.broker = broker
        self.http_fetcher = http_fetcher or AsyncHttpFetcher()
        self.browser_fetcher = browser_fetcher or PlaywrightBrowserFetcher(
            max_workers=playwright_workers or settings.playwright_workers
        )
        self.cache = cache or get_url_cache()
        self.concurrency = concurrency or settings.fetcher_concurrency
        self.politeness = PolitenessManager()
        self.is_running = False

    async def process_request(self, request: FetchRequest) -> bool:
        """Fetch a single URL and route output either to staging or DLQ with politeness and idempotency."""
        job_key = make_cache_key(request.url, render_js=request.render_js, options=request.metadata)

        # 1. Check Redis fast cache: If identical job (URL + options) was already cached, reuse immediately
        cached_record = await self.cache.get_cached_page(request.url, cache_key=job_key)
        if cached_record is not None:
            logger.info("Serving %s directly from cache (0ms idempotent fetch)", request.url)
            cached_record.metadata = request.metadata
            await self.broker.enqueue_raw(cached_record)
            return True

        # 2. In-flight Crawl Lock: Prevent duplicate concurrent fetches across workers
        lock_acquired = await self.cache.acquire_crawl_lock(
            request.url, ttl=settings.crawler_lock_ttl_seconds, lock_key=job_key
        )
        if not lock_acquired:
            logger.info(
                "URL %s is currently being fetched by another worker; skipping duplicate", request.url
            )
            return False

        # 3. robots.txt Politeness Check
        if settings.enable_robots_txt:
            try:
                allowed = await self.politeness.is_url_allowed(request.url)
                if not allowed:
                    logger.info("robots.txt disallowed crawl for %s; routing to DLQ", request.url)
                    await self.broker.route_to_dlq(
                        FailedFetchRecord(
                            url=request.url,
                            error_type="robots_disallowed",
                            error_message="Access denied by site robots.txt policy",
                        )
                    )
                    await self.cache.release_crawl_lock(request.url, lock_key=job_key)
                    return False
            except Exception as robot_err:
                logger.debug("robots.txt check exception for %s: %s", request.url, robot_err)

        # 4. Acquire per-domain rate limit slot
        await self.politeness.acquire_domain_slot(request.url)

        try:
            raw_record = None
            failed_record = None

            if request.render_js:
                # Explicit headless browser rendering requested
                raw_record, failed_record = await self.browser_fetcher.fetch(
                    request.url,
                    screenshot=request.screenshot,
                    actions=request.actions,
                )
            else:
                # 1. Primary fast HTTP fetch
                raw_record, failed_record = await self.http_fetcher.fetch(request.url)

                # 2. Dynamic Playwright fallback for SPAs or anti-bot/access blocks
                if settings.enable_playwright_fallback:
                    should_fallback = False
                    reason = ""

                    if raw_record and is_spa_shell(raw_record.raw_html):
                        should_fallback = True
                        reason = "unrendered client-side SPA shell"
                    elif failed_record and (
                        failed_record.error_type
                        in {
                            "http_403",
                            "http_401",
                            "http_429",
                            "http_503",
                            "ssl_error",
                            "connect_timeout",
                            "connection_error",
                        }
                    ):
                        should_fallback = True
                        reason = f"HTTP access failure ({failed_record.error_type})"
                    elif raw_record and len(raw_record.raw_html.strip()) < 200:
                        should_fallback = True
                        reason = "thin/empty response body (<200 bytes)"

                    if should_fallback:
                        logger.info(
                            "Routing %s to Playwright worker pool (reason: %s)...",
                            request.url,
                            reason,
                        )
                        try:
                            browser_record, browser_failed = await self.browser_fetcher.fetch(request.url)
                            if browser_record and (
                                not raw_record or len(browser_record.raw_html) > len(raw_record.raw_html)
                            ):
                                raw_record = browser_record
                                failed_record = None
                            elif browser_failed and not raw_record:
                                failed_record = browser_failed
                        except Exception as browser_err:
                            logger.debug("Playwright auto-fallback error: %s", browser_err)

            if raw_record:
                raw_record.metadata = request.metadata
                # 5. Cache fetched page and mark URL as seen in Redis
                await self.cache.set_cached_page(raw_record, cache_key=job_key)
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
            await self.politeness.release_domain_slot(request.url)
            await self.cache.release_crawl_lock(request.url, lock_key=job_key)

    async def run_once(self) -> bool:
        """Process a single fetch job. Returns True if a job was processed."""
        request = await self.broker.dequeue_fetch(timeout=1.0)
        if not request:
            return False
        return await self.process_request(request)

    async def _consumer_loop(self, worker_id: int) -> None:
        """Parallel consumer coroutine popping and processing URLs."""
        while self.is_running:
            try:
                processed = await self.run_once()
                if not processed:
                    await asyncio.sleep(0.2)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("Fetcher consumer %d encountered unexpected error: %s", worker_id, exc)
                await asyncio.sleep(0.5)

    async def run_loop(self, concurrency: int | None = None) -> None:
        """Continuously process fetch jobs using multiple parallel async consumers."""
        num_workers = concurrency or self.concurrency
        self.is_running = True
        logger.info(
            "Fetcher worker running with %d parallel consumers (max_connections=%d)...",
            num_workers,
            settings.max_connections,
        )
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
