from __future__ import annotations

import asyncio
import contextlib
import logging

import redis.asyncio as aioredis

from services.common.config import settings
from services.common.models import CleanDocument, FailedFetchRecord, FetchRequest, RawPageRecord
from services.queue.base import AbstractQueueBroker

logger = logging.getLogger(__name__)


class RedisQueueBroker(AbstractQueueBroker):
    """Distributed Redis-backed broker using atomic list push/pop operations with auto-reconnection."""

    def __init__(self, redis_url: str | None = None) -> None:
        self.url = redis_url or settings.redis_url
        self.redis: aioredis.Redis | None = None

    async def _get_client(self) -> aioredis.Redis:
        if self.redis is None:
            self.redis = aioredis.from_url(
                self.url,
                decode_responses=True,
                socket_connect_timeout=3.0,
                socket_timeout=5.0,
                socket_keepalive=True,
                health_check_interval=15,
                retry_on_timeout=True,
            )
        return self.redis

    async def _reset_client(self) -> None:
        if self.redis is not None:
            with contextlib.suppress(Exception):
                await self.redis.aclose()
            self.redis = None

    async def _enqueue_with_retry(self, queue_name: str, payload: str, retries: int = 2) -> None:
        for attempt in range(retries):
            try:
                client = await self._get_client()
                await client.lpush(queue_name, payload)
                return
            except (aioredis.TimeoutError, aioredis.ConnectionError, Exception) as exc:
                logger.warning(
                    "Redis lpush to '%s' failed (attempt %d/%d): %s",
                    queue_name,
                    attempt + 1,
                    retries,
                    exc,
                )
                await self._reset_client()
                if attempt == retries - 1:
                    raise
                await asyncio.sleep(0.5)

    async def _dequeue_with_fallback(self, queue_name: str, timeout: float = 1.0) -> str | None:
        try:
            client = await self._get_client()
            item = await client.brpop(queue_name, timeout=int(max(1, timeout)))
            if item:
                _, payload = item
                return payload.decode("utf-8") if isinstance(payload, bytes) else str(payload)
            return None
        except (aioredis.TimeoutError, aioredis.ConnectionError, Exception) as exc:
            logger.debug("Redis brpop from '%s' timed out or disconnected: %s", queue_name, exc)
            await self._reset_client()
            return None

    async def enqueue_fetch(self, request: FetchRequest) -> None:
        await self._enqueue_with_retry(settings.fetch_queue_name, request.model_dump_json())

    async def dequeue_fetch(self, timeout: float = 1.0) -> FetchRequest | None:
        payload = await self._dequeue_with_fallback(settings.fetch_queue_name, timeout)
        return FetchRequest.model_validate_json(payload) if payload else None

    async def enqueue_raw(self, record: RawPageRecord) -> None:
        await self._enqueue_with_retry(settings.staging_queue_name, record.model_dump_json())

    async def dequeue_raw(self, timeout: float = 1.0) -> RawPageRecord | None:
        payload = await self._dequeue_with_fallback(settings.staging_queue_name, timeout)
        return RawPageRecord.model_validate_json(payload) if payload else None

    async def enqueue_document(self, doc: CleanDocument) -> None:
        await self._enqueue_with_retry(settings.index_queue_name, doc.model_dump_json())

    async def dequeue_document(self, timeout: float = 1.0) -> CleanDocument | None:
        payload = await self._dequeue_with_fallback(settings.index_queue_name, timeout)
        return CleanDocument.model_validate_json(payload) if payload else None

    async def route_to_dlq(self, failed: FailedFetchRecord) -> None:
        await self._enqueue_with_retry(settings.dlq_name, failed.model_dump_json())

    async def get_queue_depths(self) -> dict[str, int]:
        try:
            client = await self._get_client()
            fetch_len = await client.llen(settings.fetch_queue_name)
            staging_len = await client.llen(settings.staging_queue_name)
            index_len = await client.llen(settings.index_queue_name)
            dlq_len = await client.llen(settings.dlq_name)
            return {
                "fetch": fetch_len,
                "staging": staging_len,
                "index": index_len,
                "dlq": dlq_len,
            }
        except (aioredis.TimeoutError, aioredis.ConnectionError, Exception) as exc:
            logger.warning("Failed to query Redis queue depths: %s", exc)
            await self._reset_client()
            return {
                "fetch": 0,
                "staging": 0,
                "index": 0,
                "dlq": 0,
            }

    async def clear_queue(self, queue_name: str | None = None) -> dict[str, int]:
        """Purge items from Redis queue(s) and return counts cleared."""
        purged: dict[str, int] = {}
        all_queues = {
            "fetch": settings.fetch_queue_name,
            "staging": settings.staging_queue_name,
            "index": settings.index_queue_name,
            "dlq": settings.dlq_name,
        }
        targets = (
            {queue_name: all_queues[queue_name]} if queue_name and queue_name in all_queues else all_queues
        )

        try:
            client = await self._get_client()
            for name, key in targets.items():
                count = await client.llen(key)
                if count > 0:
                    await client.delete(key)
                purged[name] = count
            logger.info("Purged queues: %s", purged)
            return purged
        except Exception as exc:
            logger.warning("Failed to clear queues: %s", exc)
            return purged

    async def ping(self) -> bool:
        try:
            client = await self._get_client()
            return bool(await client.ping())
        except Exception as exc:
            logger.warning("Redis ping failed: %s", exc)
            return False

    async def close(self) -> None:
        await self._reset_client()
