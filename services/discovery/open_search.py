from __future__ import annotations

import logging
import urllib.parse
from typing import Any

import httpx
from pydantic import BaseModel

from services.fetcher.security import is_ssrf_safe_url

logger = logging.getLogger(__name__)


class OpenSearchResult(BaseModel):
    title: str
    url: str
    snippet: str = ""
    domain: str = ""


class OpenKnowledgeDiscovery:
    """Zero-API-key open discovery querying Wikimedia & Wikipedia standard APIs.

    Enables instant multilingual knowledge entity discovery for cultural,
    historical, regional, and scientific queries without commercial SERP APIs.
    """

    def __init__(self, timeout: float = 10.0) -> None:
        self.timeout = timeout
        self.headers = {
            "User-Agent": "AgentSearch-OpenDiscovery/1.0 (+https://github.com/siddharthaborah/agent-search)",
            "Accept": "application/json",
        }

    def _detect_endpoint(self, query: str) -> list[str]:
        """Detect relevant Wikipedia language endpoints based on script character ranges."""
        endpoints = []
        # Check for Bengali/Assamese Unicode range (U+0980 to U+09FF)
        has_assamese = any("\u0980" <= ch <= "\u09ff" for ch in query)
        if has_assamese:
            endpoints.append("https://as.wikipedia.org/w/api.php")
            endpoints.append("https://bn.wikipedia.org/w/api.php")

        # Always include English Wikipedia
        endpoints.append("https://en.wikipedia.org/w/api.php")
        return endpoints

    async def search(self, query: str, limit: int = 5) -> list[OpenSearchResult]:
        """Execute open entity search and return safe article URLs."""
        clean_query = query.strip()
        if not clean_query:
            return []

        endpoints = self._detect_endpoint(clean_query)
        results: list[OpenSearchResult] = []
        seen_urls: set[str] = set()

        async with httpx.AsyncClient(timeout=self.timeout, headers=self.headers) as client:
            for api_url in endpoints:
                try:
                    # 1. Full text & title search via MediaWiki API
                    resp = await client.get(
                        api_url,
                        params={
                            "action": "query",
                            "list": "search",
                            "srsearch": clean_query,
                            "srlimit": str(min(limit, 10)),
                            "format": "json",
                            "utf8": "1",
                        },
                    )
                    if resp.status_code != 200:
                        continue

                    data: dict[str, Any] = resp.json()
                    search_items = data.get("query", {}).get("search", [])

                    # Determine base wiki domain
                    parsed_api = urllib.parse.urlsplit(api_url)
                    base_domain = (parsed_api.hostname or "en.wikipedia.org").lower()
                    base_prefix = f"https://{base_domain}/wiki/"

                    for item in search_items:
                        title = item.get("title", "")
                        if not title:
                            continue

                        # Clean snippet (remove <span class="searchmatch"> tags)
                        raw_snippet = item.get("snippet", "")
                        clean_snippet = (
                            raw_snippet.replace('<span class="searchmatch">', "")
                            .replace("</span>", "")
                            .strip()
                        )

                        article_url = base_prefix + urllib.parse.quote(title.replace(" ", "_"))
                        if article_url in seen_urls:
                            continue

                        safe, _ = is_ssrf_safe_url(article_url)
                        if not safe:
                            continue

                        seen_urls.add(article_url)
                        results.append(
                            OpenSearchResult(
                                title=title,
                                url=article_url,
                                snippet=clean_snippet,
                                domain=base_domain,
                            )
                        )
                        if len(results) >= limit:
                            break

                except Exception as exc:
                    logger.debug("Open knowledge query failed for %s (%s): %s", clean_query, api_url, exc)

                if len(results) >= limit:
                    break

        return results
