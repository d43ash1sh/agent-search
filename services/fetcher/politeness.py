from __future__ import annotations

import asyncio
import logging
import time
from urllib.parse import urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

import httpx

logger = logging.getLogger(__name__)


class PolitenessManager:
    """Manages per-domain rate limiting, concurrency caps, and cached robots.txt compliance."""

    def __init__(
        self,
        default_user_agent: str = "AgentSearchBot",
        min_domain_delay_seconds: float = 0.2,
        max_concurrent_per_domain: int = 3,
        robots_cache_ttl_seconds: int = 86400,
    ) -> None:
        self.default_user_agent = default_user_agent
        self.min_domain_delay_seconds = min_domain_delay_seconds
        self.max_concurrent_per_domain = max_concurrent_per_domain
        self.robots_cache_ttl = robots_cache_ttl_seconds

        # In-memory caches: domain -> (RobotFileParser, timestamp)
        self._robots_cache: dict[str, tuple[RobotFileParser, float]] = {}
        # domain -> last_request_time
        self._domain_last_request: dict[str, float] = {}
        # domain -> active_concurrency_count
        self._domain_active_counts: dict[str, int] = {}
        # domain -> asyncio.Lock
        self._domain_locks: dict[str, asyncio.Lock] = {}
        self._global_lock = asyncio.Lock()

    def _get_domain(self, url: str) -> str:
        return (urlsplit(url).hostname or "").lower()

    async def _get_domain_lock(self, domain: str) -> asyncio.Lock:
        async with self._global_lock:
            if domain not in self._domain_locks:
                self._domain_locks[domain] = asyncio.Lock()
            return self._domain_locks[domain]

    async def get_robots_parser(self, url: str) -> RobotFileParser | None:
        """Fetch and parse robots.txt for the given domain with 24-hour in-memory cache."""
        domain = self._get_domain(url)
        if not domain:
            return None

        now = time.monotonic()
        if domain in self._robots_cache:
            parser, cached_at = self._robots_cache[domain]
            if now - cached_at < self.robots_cache_ttl:
                return parser

        # Fetch robots.txt
        parsed = urlsplit(url)
        robots_url = urlunsplit((parsed.scheme or "https", parsed.netloc, "/robots.txt", "", ""))

        parser = RobotFileParser()
        try:
            async with httpx.AsyncClient(timeout=5.0, follow_redirects=True) as client:
                resp = await client.get(robots_url)
                if resp.status_code == 200:
                    parser.parse(resp.text.splitlines())
                else:
                    # 404 or other errors mean everything is allowed
                    parser.allow_all = True
        except Exception as exc:
            logger.debug("Could not fetch robots.txt for %s: %s (assuming allowed)", domain, exc)
            parser.allow_all = True

        self._robots_cache[domain] = (parser, now)
        return parser

    async def is_url_allowed(self, url: str, user_agent: str | None = None) -> bool:
        """Check if robots.txt permits crawling this URL for the user agent."""
        parser = await self.get_robots_parser(url)
        if parser is None:
            return True
        ua = user_agent or self.default_user_agent
        try:
            # First check specific agent, then fallback to wildcard '*'
            return parser.can_fetch(ua, url) or parser.can_fetch("*", url)
        except Exception:
            return True

    async def acquire_domain_slot(self, url: str) -> None:
        """Enforce per-domain politeness delay and concurrency cap before fetching."""
        domain = self._get_domain(url)
        if not domain:
            return

        lock = await self._get_domain_lock(domain)
        async with lock:
            # 1. Enforce min inter-request delay to avoid hammering single host
            now = time.monotonic()
            last_time = self._domain_last_request.get(domain, 0.0)
            elapsed = now - last_time
            if elapsed < self.min_domain_delay_seconds:
                sleep_needed = self.min_domain_delay_seconds - elapsed
                await asyncio.sleep(sleep_needed)

            # 2. Track active concurrency
            while self._domain_active_counts.get(domain, 0) >= self.max_concurrent_per_domain:
                await asyncio.sleep(0.1)

            self._domain_active_counts[domain] = self._domain_active_counts.get(domain, 0) + 1
            self._domain_last_request[domain] = time.monotonic()

    async def release_domain_slot(self, url: str) -> None:
        """Release concurrency lease for the domain after fetch completes."""
        domain = self._get_domain(url)
        if not domain:
            return

        lock = await self._get_domain_lock(domain)
        async with lock:
            current = self._domain_active_counts.get(domain, 1)
            self._domain_active_counts[domain] = max(0, current - 1)
            self._domain_last_request[domain] = time.monotonic()
