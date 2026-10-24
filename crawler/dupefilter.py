from __future__ import annotations

import contextlib
import logging
from typing import Any

from scrapy.crawler import Crawler
from scrapy.dupefilters import BaseDupeFilter
from scrapy.http import Request
from scrapy.spiders import Spider

logger = logging.getLogger(__name__)


class RedisDupeFilter(BaseDupeFilter):
    """Distributed Redis-backed duplicate request filter for Scrapy.

    Ensures that spiders across multiple processes and machines share a deduplication
    ledger in Redis and never re-crawl already visited URLs.
    """

    def __init__(
        self, redis_url: str = "redis://127.0.0.1:6379/0", key: str = "crawler:seen_fingerprints"
    ) -> None:
        self.redis_url = redis_url
        self.key = key
        self.client: Any = None
        self._memory_fallback: set[str] = set()
        self.crawler: Crawler | None = None

    @classmethod
    def from_crawler(cls, crawler: Crawler) -> RedisDupeFilter:
        redis_url = crawler.settings.get("REDIS_URL", "redis://127.0.0.1:6379/0")
        key = crawler.settings.get("REDIS_DUPEFILTER_KEY", "crawler:seen_fingerprints")
        instance = cls(redis_url=redis_url, key=key)
        instance.crawler = crawler
        return instance

    def open(self) -> None:
        try:
            import redis

            self.client = redis.from_url(self.redis_url, socket_timeout=3.0)
            self.client.ping()
            logger.info("RedisDupeFilter connected to Redis at %s", self.redis_url)
        except Exception as exc:
            logger.warning(
                "RedisDupeFilter could not connect to Redis (%s); falling back to in-memory set", exc
            )
            self.client = None

    def close(self, reason: str = "") -> None:
        if self.client is not None:
            with contextlib.suppress(Exception):
                self.client.close()
            self.client = None

    def request_fingerprint(self, request: Request) -> str:
        if self.crawler and hasattr(self.crawler, "request_fingerprinter"):
            fp = self.crawler.request_fingerprinter.fingerprint(request)
            return fp.hex() if isinstance(fp, bytes) else str(fp)
        import hashlib

        return hashlib.sha256(request.url.encode("utf-8")).hexdigest()

    def request_seen(self, request: Request) -> bool:
        fp = self.request_fingerprint(request)
        if self.client is not None:
            try:
                added = self.client.sadd(self.key, fp)
                self.client.sadd("crawler:seen_urls", request.url)
                return bool(added == 0)
            except Exception as exc:
                logger.warning("RedisDupeFilter error; falling back to memory: %s", exc)
                self.client = None

        if fp in self._memory_fallback:
            return True
        self._memory_fallback.add(fp)
        return False

    def log(self, request: Request, spider: Spider) -> None:
        logger.debug(
            "Filtered duplicate request: %(request)s", {"request": request}, extra={"spider": spider}
        )
