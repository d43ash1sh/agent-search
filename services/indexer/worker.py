from __future__ import annotations

import asyncio
import logging

from services.common.config import settings
from services.indexer.base import AbstractSearchIndex
from services.indexer.opensearch_index import OpenSearchIndex
from services.indexer.sqlite_index import SQLiteSearchIndex
from services.queue.base import AbstractQueueBroker
from services.queue.redis_broker import RedisQueueBroker

logger = logging.getLogger(__name__)


class IndexerWorker:
    """Worker that pulls CleanDocument records from the index queue and commits them."""

    def __init__(self, broker: AbstractQueueBroker, index: AbstractSearchIndex) -> None:
        self.broker = broker
        self.index = index
        self.is_running = False

    async def run_once(self) -> bool:
        doc = await self.broker.dequeue_document(timeout=1.0)
        if not doc:
            return False

        try:
            await self.index.index_document(doc)
            logger.info("Successfully indexed %s (%d passages)", doc.canonical_url, len(doc.passages))
            return True
        except Exception as error:
            logger.error("Failed to index document %s: %s", doc.canonical_url, error, exc_info=True)
            return False

    async def run_loop(self) -> None:
        self.is_running = True
        logger.info("Indexer worker started.")
        while self.is_running:
            processed = await self.run_once()
            if not processed:
                await asyncio.sleep(0.1)

    def stop(self) -> None:
        self.is_running = False


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    broker = RedisQueueBroker()
    index: AbstractSearchIndex
    if settings.index_backend == "opensearch":
        index = OpenSearchIndex()
    else:
        index = SQLiteSearchIndex(settings.sqlite_path)

    worker = IndexerWorker(broker=broker, index=index)
    try:
        asyncio.run(worker.run_loop())
    except KeyboardInterrupt:
        logger.info("Indexer worker stopped by user.")


if __name__ == "__main__":
    main()
