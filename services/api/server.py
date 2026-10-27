from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import redis
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from services.api.routes import create_api_router
from services.cache.redis_cache import get_url_cache
from services.common.config import settings
from services.indexer.factory import create_search_index
from services.queue.base import AbstractQueueBroker
from services.queue.memory_broker import InMemoryQueueBroker
from services.queue.redis_broker import RedisQueueBroker

logger = logging.getLogger(__name__)


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
index = create_search_index()
cache = get_url_cache()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    logger.info("Starting Agent Search Distributed API (backend=%s)", settings.index_backend)
    yield
    logger.info("Shutting down Agent Search Distributed API...")
    try:
        await index.close()
    except Exception as exc:
        logger.warning("Error closing search index during shutdown: %s", exc)
    try:
        await broker.close()
    except Exception as exc:
        logger.warning("Error closing broker during shutdown: %s", exc)
    try:
        await cache.close()
    except Exception as exc:
        logger.warning("Error closing URL cache during shutdown: %s", exc)


app = FastAPI(
    title="Agent Search Distributed API",
    description="Decoupled search, crawl staging, and retrieval service.",
    version="2.0.0",
    lifespan=lifespan,
)


@app.exception_handler(ConnectionRefusedError)
async def connection_refused_handler(request: Request, exc: ConnectionRefusedError) -> JSONResponse:
    logger.warning("Downstream connection refused on %s: %s", request.url.path, exc)
    return JSONResponse(
        status_code=503,
        content={
            "status": "service_unavailable",
            "error": "Downstream database or broker service refused connection",
            "detail": str(exc),
            "suggestion": "Ensure Docker Desktop is running and PostgreSQL/Redis containers are active.",
        },
    )


api_router = create_api_router(broker, index, cache=cache)
app.include_router(api_router)
