from __future__ import annotations

import asyncio
import logging

from services.common.config import settings
from services.common.models import FetchRequest
from services.fetcher.browser_client import PlaywrightBrowserFetcher
from services.fetcher.http_client import AsyncHttpFetcher
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
    ) -> None:
        self.broker = broker
        self.http_fetcher = http_fetcher or AsyncHttpFetcher()
        self.browser_fetcher = browser_fetcher
        self.is_running = False

    async def process_request(self, request: FetchRequest) -> bool:
        """Fetch a single URL and route output either to staging or DLQ."""
        use_browser = request.render_js or settings.enable_playwright_fallback
        raw_record = None
        failed_record = None

        if use_browser:
            if not self.browser_fetcher:
                self.browser_fetcher = PlaywrightBrowserFetcher()
            raw_record, failed_record = await self.browser_fetcher.fetch(request.url)
        else:
            raw_record, failed_record = await self.http_fetcher.fetch(request.url)

        if raw_record:
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
