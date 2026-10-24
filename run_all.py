from __future__ import annotations

import asyncio
import contextlib
import logging

import redis
import uvicorn

from services.api.server import app, index
from services.common.config import settings
from services.extractor.worker import ExtractorWorker
from services.fetcher.worker import FetcherWorker
from services.indexer.worker import IndexerWorker
from services.queue.base import AbstractQueueBroker
from services.queue.memory_broker import InMemoryQueueBroker
from services.queue.redis_broker import RedisQueueBroker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("agent-search")


async def main() -> None:
    # Detect broker (Redis or in-memory fallback)
    active_broker: AbstractQueueBroker
    try:
        r = redis.from_url(settings.redis_url, socket_timeout=2.0)
        r.ping()
        logger.info("Connected to Redis at %s", settings.redis_url)
        active_broker = RedisQueueBroker(settings.redis_url)
    except Exception:
        logger.warning("Redis not reachable; using in-process InMemoryQueueBroker")
        active_broker = InMemoryQueueBroker()

    # Initialize workers
    fetcher = FetcherWorker(broker=active_broker)
    extractor = ExtractorWorker(broker=active_broker, index=index)
    indexer = IndexerWorker(broker=active_broker, index=index)

    # Configure uvicorn API server
    config = uvicorn.Config(
        app=app,
        host=settings.api_host,
        port=settings.api_port,
        log_level="info",
    )
    server = uvicorn.Server(config)

    logger.info("==================================================================")
    logger.info(" Agent Search Engine: All services starting up...")
    logger.info("   - Fetcher Service:   ACTIVE (httpx + Playwright fallback)")
    logger.info("   - Extractor Service: ACTIVE (Trafilatura + SimHash)")
    logger.info("   - Indexer Service:   ACTIVE (SQLite FTS5 + Neural Reranker)")
    logger.info("   - API Service:       ACTIVE (http://%s:%d)", settings.api_host, settings.api_port)
    logger.info("   - Interactive Docs:  http://localhost:%d/docs", settings.api_port)
    logger.info("==================================================================")

    # Run all workers and the web server concurrently
    try:
        await asyncio.gather(
            server.serve(),
            fetcher.run_loop(),
            extractor.run_loop(),
            indexer.run_loop(),
        )
    except (asyncio.CancelledError, KeyboardInterrupt):
        logger.info("Shutdown signal received. Stopping services...")
    finally:
        fetcher.is_running = False
        extractor.is_running = False
        indexer.is_running = False
        await fetcher.close()
        await extractor.cache.close()
        logger.info("All services shut down cleanly.")


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())
