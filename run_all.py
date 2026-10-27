from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import sys

import redis
import uvicorn

from services.common.config import settings
from services.common.docker_manager import (
    ensure_infrastructure_online,
    is_docker_available,
    stop_docker_containers,
)
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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run Agent Search distributed services (API, Fetcher, Extractor, Indexer) with auto-Docker infrastructure."
    )
    parser.add_argument(
        "--no-docker",
        action="store_true",
        help="Skip automatic Docker infrastructure check/startup (for containerized or external setups).",
    )
    parser.add_argument(
        "--docker-only",
        action="store_true",
        help="Only ensure Docker infrastructure (PostgreSQL & Redis) is running and healthy, then exit.",
    )
    parser.add_argument(
        "--stop-docker",
        action="store_true",
        help="Stop running Docker compose containers and exit.",
    )
    parser.add_argument(
        "--down-on-exit",
        action="store_true",
        help="Automatically stop Docker compose containers when run_all.py is terminated.",
    )
    parser.add_argument(
        "--playwright-workers",
        type=int,
        default=settings.playwright_workers,
        help="Number of concurrent Playwright browser rendering workers (default: 3).",
    )
    return parser.parse_args()


async def main() -> None:
    args = parse_args()

    # Handle --stop-docker command
    if args.stop_docker:
        logger.info("Stopping Docker infrastructure...")
        success = stop_docker_containers()
        sys.exit(0 if success else 1)

    # 1. Automatic Docker & Infrastructure Management
    auto_start = not args.no_docker
    logger.info("Checking infrastructure dependencies (Docker available: %s)...", is_docker_available())
    infra_status = ensure_infrastructure_online(auto_start_docker=auto_start)
    logger.info("Infrastructure status: %s", infra_status)

    if args.docker_only:
        logger.info("Docker infrastructure check complete. Exiting (--docker-only requested).")
        return

    # 2. Detect broker (Redis or in-memory fallback)
    from services.api.server import app, index

    active_broker: AbstractQueueBroker
    try:
        r = redis.from_url(settings.redis_url, socket_timeout=2.0)
        r.ping()
        logger.info("Connected to Redis broker at %s", settings.redis_url)
        active_broker = RedisQueueBroker(settings.redis_url)
    except Exception as exc:
        logger.warning("Redis broker not reachable (%s); falling back to InMemoryQueueBroker", exc)
        active_broker = InMemoryQueueBroker()

    # 3. Initialize workers
    fetcher = FetcherWorker(broker=active_broker, playwright_workers=args.playwright_workers)
    extractor = ExtractorWorker(broker=active_broker, index=index)
    indexer = IndexerWorker(broker=active_broker, index=index)

    # 4. Configure uvicorn API server
    config = uvicorn.Config(
        app=app,
        host=settings.api_host,
        port=settings.api_port,
        log_level="info",
    )
    server = uvicorn.Server(config)

    effective_backend = (
        "POSTGRES" if index.__class__.__name__ == "PostgresSearchIndex" else "SQLITE (fallback)"
    )
    logger.info("==================================================================")
    logger.info(" Agent Search Engine: All services starting up...")
    logger.info("   - Storage Backend:   %s", effective_backend)
    logger.info("   - Queue Broker:      %s", active_broker.__class__.__name__)
    logger.info(
        "   - Fetcher Service:   ACTIVE (%dx HTTP Consumers + %dx Playwright Browser Workers)",
        settings.fetcher_concurrency,
        args.playwright_workers,
    )
    logger.info(
        "   - Extractor Service: ACTIVE (%dx Parallel Consumers | Trafilatura + Frontier)",
        settings.extractor_concurrency,
    )
    logger.info(
        "   - Indexer Service:   ACTIVE (%dx Parallel Consumers | Hybrid pgvector / FTS5)",
        settings.indexer_concurrency,
    )
    logger.info("   - API Service:       ACTIVE (http://%s:%d)", settings.api_host, settings.api_port)
    logger.info("   - Interactive Docs:  http://localhost:%d/docs", settings.api_port)
    logger.info("==================================================================")

    # 5. Run all workers and the web server concurrently
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

        if args.down_on_exit:
            logger.info("Stopping Docker infrastructure (--down-on-exit requested)...")
            stop_docker_containers()


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())
