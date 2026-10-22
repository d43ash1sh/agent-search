from __future__ import annotations

import logging

import redis
from fastapi import FastAPI

from services.api.routes import create_api_router
from services.common.config import settings
from services.indexer.sqlite_index import SQLiteSearchIndex
from services.queue.base import AbstractQueueBroker
from services.queue.memory_broker import InMemoryQueueBroker
from services.queue.redis_broker import RedisQueueBroker

logger = logging.getLogger(__name__)

app = FastAPI(
    title="Agent Search Distributed API",
    description="Decoupled search, crawl staging, and retrieval service.",
    version="2.0.0",
)


def _init_broker() -> AbstractQueueBroker:
    try:
        r = redis.from_url(settings.redis_url, socket_timeout=2.0)
        r.ping()
        logger.info("API connected to Redis broker at %s", settings.redis_url)
        return RedisQueueBroker(settings.redis_url)
    except Exception:
        logger.info("API falling back to InMemoryQueueBroker")
        return InMemoryQueueBroker()


broker = _init_broker()
index = SQLiteSearchIndex(settings.sqlite_path)

api_router = create_api_router(broker, index)
app.include_router(api_router)
