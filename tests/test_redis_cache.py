from __future__ import annotations

import asyncio

import pytest

from services.cache.redis_cache import InMemoryUrlCache, RedisUrlCache
from services.common.models import FetchRequest, RawPageRecord
from services.fetcher.worker import FetcherWorker
from services.queue.memory_broker import InMemoryQueueBroker


@pytest.mark.anyio
async def test_in_memory_url_cache_seen_and_filter() -> None:
    cache = InMemoryUrlCache()

    # Initially unseen
    assert await cache.is_seen("https://example.com/1") is False

    # Mark seen
    added = await cache.mark_seen("https://example.com/1")
    assert added is True
    assert await cache.is_seen("https://example.com/1") is True

    # Mark seen again returns False (already present)
    added_again = await cache.mark_seen("https://example.com/1")
    assert added_again is False

    # Batch mark
    batch_added = await cache.mark_seen_batch(["https://example.com/2", "https://example.com/3"])
    assert batch_added == 2

    # Filter unseen
    urls = [
        "https://example.com/1",
        "https://example.com/2",
        "https://example.com/new-4",
        "https://example.com/new-5",
    ]
    unseen = await cache.filter_unseen_urls(urls)
    assert unseen == ["https://example.com/new-4", "https://example.com/new-5"]


@pytest.mark.anyio
async def test_in_memory_page_cache_and_locking() -> None:
    cache = InMemoryUrlCache()
    url = "https://example.com/page"

    # Initially not cached
    assert await cache.get_cached_page(url) is None

    # Cache page
    record = RawPageRecord(
        url=url,
        canonical_url=url,
        status_code=200,
        headers={"content-type": "text/html"},
        raw_html="<html><body>Cached Content</body></html>",
    )
    await cache.set_cached_page(record, ttl=10)

    # Now cached (cache hit)
    cached = await cache.get_cached_page(url)
    assert cached is not None
    assert cached.raw_html == "<html><body>Cached Content</body></html>"

    # Concurrency lock
    lock1 = await cache.acquire_crawl_lock(url, ttl=5)
    assert lock1 is True

    # Duplicate concurrent acquire fails
    lock2 = await cache.acquire_crawl_lock(url, ttl=5)
    assert lock2 is False

    # Release lock
    await cache.release_crawl_lock(url)
    lock3 = await cache.acquire_crawl_lock(url, ttl=5)
    assert lock3 is True

    # Check stats
    stats = await cache.get_cache_stats()
    assert stats["backend"] == "memory"
    assert stats["cache_hits"] >= 1


async def is_redis_available() -> bool:
    try:
        import redis.asyncio as aioredis

        client = aioredis.from_url("redis://127.0.0.1:6379/0", socket_timeout=2.0)
        res = await client.ping()
        await client.aclose()
        return bool(res)
    except Exception:
        return False


@pytest.mark.skipif(not asyncio.run(is_redis_available()), reason="Redis is not reachable on 127.0.0.1:6379")
@pytest.mark.anyio
async def test_redis_url_cache_live() -> None:
    cache = RedisUrlCache("redis://127.0.0.1:6379/0")
    try:
        assert await cache.ping() is True

        test_url = "https://redis-test.example.com/page-01"
        # Clear previous if any
        await cache.mark_seen(test_url)
        assert await cache.is_seen(test_url) is True

        # Pipeline filter
        candidates = [test_url, "https://redis-test.example.com/brand-new-999"]
        unseen = await cache.filter_unseen_urls(candidates)
        assert test_url not in unseen
        assert "https://redis-test.example.com/brand-new-999" in unseen

        # Page caching
        rec = RawPageRecord(
            url=test_url,
            canonical_url=test_url,
            status_code=200,
            headers={},
            raw_html="<html>Redis Live Cached</html>",
        )
        await cache.set_cached_page(rec, ttl=30)
        retrieved = await cache.get_cached_page(test_url)
        assert retrieved is not None
        assert "Redis Live Cached" in retrieved.raw_html

        # Lock
        lock_ok = await cache.acquire_crawl_lock(test_url, ttl=10)
        assert lock_ok is True
        await cache.release_crawl_lock(test_url)

        stats = await cache.get_cache_stats()
        assert stats["backend"] == "redis"
        assert stats["seen_urls_count"] >= 1
    finally:
        await cache.close()


@pytest.mark.anyio
async def test_fetcher_worker_serves_from_redis_cache() -> None:
    """Verify FetcherWorker returns instantly from cache without calling the underlying HTTP fetcher."""
    broker = InMemoryQueueBroker()
    cache = InMemoryUrlCache()
    worker = FetcherWorker(broker=broker, cache=cache)

    target_url = "https://cached-site.example.com/article"

    # Pre-warm cache
    record = RawPageRecord(
        url=target_url,
        canonical_url=target_url,
        status_code=200,
        headers={},
        raw_html="<h1>Ultra Fast Redis Response</h1>",
    )
    await cache.set_cached_page(record, ttl=60)

    # Process fetch request
    req = FetchRequest(url=target_url)
    success = await worker.process_request(req)
    assert success is True

    # Verify staging queue got the cached item
    raw = await broker.dequeue_raw()
    assert raw is not None
    assert raw.url == target_url
    assert "Ultra Fast Redis Response" in raw.raw_html

    await worker.close()
