from __future__ import annotations

import json
import logging
import urllib.parse
from typing import Any

import httpx
from pydantic import BaseModel

from services.fetcher.security import is_ssrf_safe_url

logger = logging.getLogger(__name__)


class CommonCrawlRecord(BaseModel):
    url: str
    domain: str
    timestamp: str
    mime: str = "text/html"
    status: int = 200
    digest: str = ""


class CommonCrawlClient:
    """Async client querying Common Crawl's open CDX API for URL discovery.

    Allows zero-API-key discovery of billions of verified web pages across
    domains and TLDs without relying on third-party commercial search engines.
    """

    COLLINFO_URL = "https://index.commoncrawl.org/collinfo.json"

    def __init__(self, timeout: float = 12.0) -> None:
        self.timeout = timeout
        self.headers = {
            "User-Agent": "AgentSearch-OpenCrawler/1.0 (+https://github.com/siddharthaborah/agent-search)",
            "Accept": "application/json, text/plain, */*",
        }
        self._cached_latest_endpoint: str | None = None

    async def get_latest_cdx_endpoint(self) -> str:
        """Fetch the latest available Common Crawl CDX index endpoint."""
        if self._cached_latest_endpoint:
            return self._cached_latest_endpoint

        try:
            async with httpx.AsyncClient(timeout=self.timeout, headers=self.headers) as client:
                resp = await client.get(self.COLLINFO_URL)
                if resp.status_code == 200:
                    collections: list[dict[str, Any]] = resp.json()
                    if collections and "cdx-api" in collections[0]:
                        self._cached_latest_endpoint = collections[0]["cdx-api"]
                        return self._cached_latest_endpoint
        except Exception as exc:
            logger.warning("Failed to fetch Common Crawl collinfo: %s", exc)

        # Fallback to a known stable index endpoint if collinfo is unreachable
        return "https://index.commoncrawl.org/CC-MAIN-2024-51-index"

    async def discover_urls(
        self,
        domain_or_pattern: str,
        limit: int = 20,
        filter_status_200: bool = True,
    ) -> list[CommonCrawlRecord]:
        """Discover web pages matching a domain or URL pattern from Common Crawl.

        Examples of domain_or_pattern:
          - 'wikipedia.org/*'
          - '*.gov.in/*'
          - 'simonwillison.net/*'
        """
        cdx_endpoint = await self.get_latest_cdx_endpoint()
        clean_pattern = domain_or_pattern.strip()
        if not clean_pattern:
            return []

        # Ensure wildcard at end for path traversal if not present
        if "*" not in clean_pattern:
            if clean_pattern.endswith("/"):
                clean_pattern += "*"
            else:
                clean_pattern += "/*"

        params: dict[str, str] = {
            "url": clean_pattern,
            "output": "json",
            "limit": str(min(limit, 200)),
        }
        if filter_status_200:
            params["filter"] = "=status:200"

        records: list[CommonCrawlRecord] = []
        try:
            async with httpx.AsyncClient(timeout=self.timeout, headers=self.headers) as client:
                resp = await client.get(cdx_endpoint, params=params)
                if resp.status_code != 200:
                    logger.warning(
                        "Common Crawl CDX returned HTTP %d for pattern '%s'",
                        resp.status_code,
                        clean_pattern,
                    )
                    return []

                for line in resp.text.strip().splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                        raw_url = data.get("url", "")
                        if not raw_url.startswith(("http://", "https://")):
                            continue

                        # SSRF security filter
                        safe, _ = is_ssrf_safe_url(raw_url)
                        if not safe:
                            continue

                        domain = (urllib.parse.urlsplit(raw_url).hostname or "").lower()
                        status_code = int(data.get("status", 200))

                        records.append(
                            CommonCrawlRecord(
                                url=raw_url,
                                domain=domain,
                                timestamp=data.get("timestamp", ""),
                                mime=data.get("mime", "text/html"),
                                status=status_code,
                                digest=data.get("digest", ""),
                            )
                        )
                        if len(records) >= limit:
                            break
                    except Exception as parse_err:
                        logger.debug("Failed parsing CDX JSON line: %s", parse_err)

        except Exception as exc:
            logger.warning("Common Crawl discovery error for '%s': %s", clean_pattern, exc)

        return records
