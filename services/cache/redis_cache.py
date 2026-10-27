from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from abc import ABC, abstractmethod
from typing import Any

import redis.asyncio as aioredis

from services.common.config import settings
from services.common.hashing import compute_sha256
from services.common.models import RawPageRecord

logger = logging.getLogger(__name__)


def make_cache_key(url: str, render_js: bool = False, options: dict[str, Any] | None = None) -> str:
    """Compute deterministic hash of URL + options for idempotent job deduplication."""
    parts = [url, f"js={int(render_js)}"]
    if options:
        parts.append(json.dumps(options, sort_keys=True))
    return compute_sha256(":".join(parts))


class AbstractUrlCache(ABC):
    """Abstract interface for crawler URL caching, deduplication, and concurrency control."""

    @abstractmethod
    async def is_seen(self, url: str) -> bool:
        """Check if a URL has already been crawled or scheduled."""

    @abstractmethod
    async def mark_seen(self, url: str) -> bool:
        """Mark a URL as seen. Returns True if newly added, False if already present."""

    @abstractmethod
    async def mark_seen_batch(self, urls: list[str]) -> int:
        """Mark multiple URLs as seen in a single batch. Returns count of newly added URLs."""

    @abstractmethod
    async def filter_unseen_urls(self, urls: list[str]) -> list[str]:
        """Filter a list of URLs returning only those that have NOT been seen yet."""

    @abstractmethod
    async def get_cached_page(self, url: str, cache_key: str | None = None) -> RawPageRecord | None:
        """Retrieve cached HTTP response record for a URL if present and unexpired."""

    @abstractmethod
    async def set_cached_page(
        self, record: RawPageRecord, ttl: int | None = None, cache_key: str | None = None
    ) -> None:
        """Store raw fetched page in cache with a TTL."""

    @abstractmethod
    async def acquire_crawl_lock(self, url: str, ttl: int = 60, lock_key: str | None = None) -> bool:
        """Acquire an in-flight crawl lock to prevent multiple workers fetching the same URL concurrently."""

    @abstractmethod
    async def release_crawl_lock(self, url: str, lock_key: str | None = None) -> None:
        """Release an in-flight crawl lock for a URL."""

    @abstractmethod
    async def get_cache_stats(self) -> dict[str, Any]:
        """Return cache health, hit/miss metrics, and stored URL counts."""

    @abstractmethod
    async def clear_cache(self) -> None:
        """Clear all cached URLs, page bodies, and locks."""

    async def ping(self) -> bool:
        """Check cache backend connectivity."""
        return True

    async def close(self) -> None:
        """Clean up connection resources."""
        return None


class InMemoryUrlCache(AbstractUrlCache):
    """In-memory cache implementation for testing, offline execution, and local development."""

    def __init__(self) -> None:
        self._seen_urls: set[str] = set()
        self._page_cache: dict[str, tuple[RawPageRecord, float]] = {}  # key -> (record, expire_timestamp)
        self._locks: dict[str, float] = {}  # key -> expire_timestamp
        self._hits: int = 0
        self._misses: int = 0
        self._lock = asyncio.Lock()

    async def is_seen(self, url: str) -> bool:
        return url in self._seen_urls

    async def mark_seen(self, url: str) -> bool:
        async with self._lock:
            if url in self._seen_urls:
                return False
            self._seen_urls.add(url)
            return True

    async def mark_seen_batch(self, urls: list[str]) -> int:
        async with self._lock:
            initial_count = len(self._seen_urls)
            self._seen_urls.update(urls)
            return len(self._seen_urls) - initial_count

    async def filter_unseen_urls(self, urls: list[str]) -> list[str]:
        unseen: list[str] = []
        for u in urls:
            if u not in self._seen_urls:
                unseen.append(u)
        return unseen

    async def get_cached_page(self, url: str, cache_key: str | None = None) -> RawPageRecord | None:
        key = cache_key or compute_sha256(url)
        now = time.monotonic()
        item = self._page_cache.get(key)
        if item is not None:
            record, expire_at = item
            if now < expire_at:
                self._hits += 1
                return record
            del self._page_cache[key]
        self._misses += 1
        return None

    async def set_cached_page(
        self, record: RawPageRecord, ttl: int | None = None, cache_key: str | None = None
    ) -> None:
        key = cache_key or compute_sha256(record.url)
        lifetime = ttl or settings.page_cache_ttl_seconds
        expire_at = time.monotonic() + lifetime
        self._page_cache[key] = (record, expire_at)

    async def acquire_crawl_lock(self, url: str, ttl: int = 60, lock_key: str | None = None) -> bool:
        key = lock_key or compute_sha256(url)
        now = time.monotonic()
        async with self._lock:
            if key in self._locks and now < self._locks[key]:
                return False
            self._locks[key] = now + ttl
            return True

    async def release_crawl_lock(self, url: str, lock_key: str | None = None) -> None:
        key = lock_key or compute_sha256(url)
        async with self._lock:
            self._locks.pop(key, None)

    async def get_cache_stats(self) -> dict[str, Any]:
        return {
            "backend": "memory",
            "seen_urls_count": len(self._seen_urls),
            "cached_pages_count": len(self._page_cache),
            "cache_hits": self._hits,
            "cache_misses": self._misses,
            "hit_ratio": round(self._hits / max(1, self._hits + self._misses), 3),
        }

    async def clear_cache(self) -> None:
        async with self._lock:
            self._seen_urls.clear()
            self._page_cache.clear()
            self._locks.clear()
            self._hits = 0
            self._misses = 0


class RedisUrlCache(AbstractUrlCache):
    """High-speed Redis cache using atomic sets, pipelined membership checks, and key expiration."""

    SEEN_URLS_KEY = "crawler:seen_urls"
    PAGE_KEY_PREFIX = "crawler:page:"
    LOCK_KEY_PREFIX = "crawler:lock:"
    STATS_HITS_KEY = "crawler:stats:hits"
    STATS_MISSES_KEY = "crawler:stats:misses"

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

    async def is_seen(self, url: str) -> bool:
        try:
            client = await self._get_client()
            return bool(await client.sismember(self.SEEN_URLS_KEY, url))
        except Exception as exc:
            logger.warning("Redis is_seen failed for %s: %s", url, exc)
            return False

    async def mark_seen(self, url: str) -> bool:
        try:
            client = await self._get_client()
            added = await client.sadd(self.SEEN_URLS_KEY, url)
            return bool(added == 1)
        except Exception as exc:
            logger.warning("Redis mark_seen failed for %s: %s", url, exc)
            return False

    async def mark_seen_batch(self, urls: list[str]) -> int:
        if not urls:
            return 0
        try:
            client = await self._get_client()
            added = await client.sadd(self.SEEN_URLS_KEY, *urls)
            return int(added)
        except Exception as exc:
            logger.warning("Redis mark_seen_batch failed for %d URLs: %s", len(urls), exc)
            return 0

    async def filter_unseen_urls(self, urls: list[str]) -> list[str]:
        """Pipelined O(1) membership check returning only unseen URLs in a single round-trip."""
        if not urls:
            return []
        try:
            client = await self._get_client()
            pipe = client.pipeline(transaction=False)
            for u in urls:
                pipe.sismember(self.SEEN_URLS_KEY, u)
            results = await pipe.execute()

            unseen: list[str] = []
            for u, is_member in zip(urls, results, strict=False):
                if not is_member:
                    unseen.append(u)
            return unseen
        except Exception as exc:
            logger.warning("Redis filter_unseen_urls failed: %s", exc)
            return urls

    async def get_cached_page(self, url: str, cache_key: str | None = None) -> RawPageRecord | None:
        raw_key = cache_key or compute_sha256(url)
        key = f"{self.PAGE_KEY_PREFIX}{raw_key}"
        try:
            client = await self._get_client()
            payload = await client.get(key)
            if payload:
                await client.incr(self.STATS_HITS_KEY)
                return RawPageRecord.model_validate_json(payload)
            await client.incr(self.STATS_MISSES_KEY)
            return None
        except Exception as exc:
            logger.debug("Redis get_cached_page miss or error for %s: %s", url, exc)
            return None

    async def set_cached_page(
        self, record: RawPageRecord, ttl: int | None = None, cache_key: str | None = None
    ) -> None:
        raw_key = cache_key or compute_sha256(record.url)
        key = f"{self.PAGE_KEY_PREFIX}{raw_key}"
        lifetime = ttl or settings.page_cache_ttl_seconds
        try:
            client = await self._get_client()
            await client.set(key, record.model_dump_json(), ex=lifetime)
        except Exception as exc:
            logger.warning("Redis set_cached_page failed for %s: %s", record.url, exc)

    async def acquire_crawl_lock(self, url: str, ttl: int = 60, lock_key: str | None = None) -> bool:
        """Atomic lock using SET with NX and EX to prevent duplicate concurrent crawls."""
        raw_key = lock_key or compute_sha256(url)
        key = f"{self.LOCK_KEY_PREFIX}{raw_key}"
        try:
            client = await self._get_client()
            acquired = await client.set(key, "1", nx=True, ex=ttl)
            return bool(acquired)
        except Exception as exc:
            logger.warning("Redis acquire_crawl_lock error for %s: %s", url, exc)
            return True  # Proceed if lock check fails

    async def release_crawl_lock(self, url: str, lock_key: str | None = None) -> None:
        raw_key = lock_key or compute_sha256(url)
        key = f"{self.LOCK_KEY_PREFIX}{raw_key}"
        try:
            client = await self._get_client()
            await client.delete(key)
        except Exception as exc:
            logger.debug("Redis release_crawl_lock error for %s: %s", url, exc)

    async def get_cache_stats(self) -> dict[str, Any]:
        try:
            client = await self._get_client()
            seen_count = await client.scard(self.SEEN_URLS_KEY)
            hits_str = await client.get(self.STATS_HITS_KEY) or "0"
            misses_str = await client.get(self.STATS_MISSES_KEY) or "0"
            hits = int(hits_str)
            misses = int(misses_str)
            total = hits + misses
            return {
                "backend": "redis",
                "redis_url": self.url,
                "seen_urls_count": int(seen_count),
                "cache_hits": hits,
                "cache_misses": misses,
                "hit_ratio": round(hits / max(1, total), 3),
            }
        except Exception as exc:
            logger.warning("Redis get_cache_stats failed: %s", exc)
            return {
                "backend": "redis",
                "error": str(exc),
                "seen_urls_count": 0,
                "cache_hits": 0,
                "cache_misses": 0,
            }

    async def clear_cache(self) -> None:
        try:
            client = await self._get_client()
            # Delete seen set and stats
            await client.delete(self.SEEN_URLS_KEY, self.STATS_HITS_KEY, self.STATS_MISSES_KEY)

            # Scan and delete page cache and lock keys in batches
            for prefix in (self.PAGE_KEY_PREFIX, self.LOCK_KEY_PREFIX):
                cursor = 0
                while True:
                    cursor, keys = await client.scan(cursor=cursor, match=f"{prefix}*", count=200)
                    if keys:
                        await client.delete(*keys)
                    if cursor == 0:
                        break
            logger.info("Cleared Redis crawler cache successfully.")
        except Exception as exc:
            logger.warning("Redis clear_cache failed: %s", exc)

    async def ping(self) -> bool:
        try:
            client = await self._get_client()
            return bool(await client.ping())
        except Exception as exc:
            logger.warning("Redis ping failed: %s", exc)
            return False

    async def close(self) -> None:
        await self._reset_client()


def get_url_cache(redis_url: str | None = None) -> AbstractUrlCache:
    """Factory creating RedisUrlCache when enabled, falling back cleanly to InMemoryUrlCache."""
    if not settings.enable_redis_cache:
        logger.info("Redis cache disabled by configuration; using InMemoryUrlCache")
        return InMemoryUrlCache()

    target_url = redis_url or settings.redis_url
    try:
        import redis

        r = redis.from_url(target_url, socket_timeout=2.0)
        r.ping()
        logger.info("Crawler connected to Redis cache at %s", target_url)
        return RedisUrlCache(target_url)
    except Exception as exc:
        logger.info("Redis cache unavailable (%s), falling back to InMemoryUrlCache", exc)
        return InMemoryUrlCache()
