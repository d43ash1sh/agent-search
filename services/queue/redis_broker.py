from __future__ import annotations

import redis.asyncio as aioredis

from services.common.config import settings
from services.common.models import CleanDocument, FailedFetchRecord, FetchRequest, RawPageRecord
from services.queue.base import AbstractQueueBroker


class RedisQueueBroker(AbstractQueueBroker):
    """Distributed Redis-backed broker using atomic list push/pop operations."""

    def __init__(self, redis_url: str | None = None) -> None:
        self.url = redis_url or settings.redis_url
        self.redis: aioredis.Redis | None = None

    async def _get_client(self) -> aioredis.Redis:
        if self.redis is None:
            self.redis = aioredis.from_url(self.url, decode_responses=True)
        return self.redis

    async def enqueue_fetch(self, request: FetchRequest) -> None:
        client = await self._get_client()
        await client.lpush(settings.fetch_queue_name, request.model_dump_json())

    async def dequeue_fetch(self, timeout: float = 1.0) -> FetchRequest | None:
        client = await self._get_client()
        item = await client.brpop(settings.fetch_queue_name, timeout=int(max(1, timeout)))
        if item:
            _, payload = item
            return FetchRequest.model_validate_json(payload)
        return None

    async def enqueue_raw(self, record: RawPageRecord) -> None:
        client = await self._get_client()
        await client.lpush(settings.staging_queue_name, record.model_dump_json())

    async def dequeue_raw(self, timeout: float = 1.0) -> RawPageRecord | None:
        client = await self._get_client()
        item = await client.brpop(settings.staging_queue_name, timeout=int(max(1, timeout)))
        if item:
            _, payload = item
            return RawPageRecord.model_validate_json(payload)
        return None

    async def enqueue_document(self, doc: CleanDocument) -> None:
        client = await self._get_client()
        await client.lpush(settings.index_queue_name, doc.model_dump_json())

    async def dequeue_document(self, timeout: float = 1.0) -> CleanDocument | None:
        client = await self._get_client()
        item = await client.brpop(settings.index_queue_name, timeout=int(max(1, timeout)))
        if item:
            _, payload = item
            return CleanDocument.model_validate_json(payload)
        return None

    async def route_to_dlq(self, failed: FailedFetchRecord) -> None:
        client = await self._get_client()
        await client.lpush(settings.dlq_name, failed.model_dump_json())

    async def get_queue_depths(self) -> dict[str, int]:
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

    async def close(self) -> None:
        if self.redis:
            await self.redis.aclose()
            self.redis = None
