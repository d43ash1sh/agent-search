from __future__ import annotations

import logging
import urllib.parse

import httpx
from parsel import Selector
from pydantic import BaseModel

from services.fetcher.security import is_ssrf_safe_url

logger = logging.getLogger(__name__)


class SerpResultItem(BaseModel):
    title: str
    url: str
    snippet: str = ""
    domain: str = ""


class DuckDuckGoSerpClient:
    """Async zero-API-key SERP client querying DuckDuckGo HTML endpoint."""

    SEARCH_URL = "https://html.duckduckgo.com/html/"

    def __init__(self, timeout: float = 8.0) -> None:
        self.timeout = timeout
        self.headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }

    def _clean_url(self, raw_url: str) -> str:
        """Unwrap DuckDuckGo redirect wrapper /l/?uddg=<actual_url> if present."""
        if "uddg=" in raw_url:
            parsed = urllib.parse.urlsplit(raw_url)
            qs = urllib.parse.parse_qs(parsed.query)
            if qs.get("uddg"):
                return qs["uddg"][0]
        if raw_url.startswith("//"):
            return "https:" + raw_url
        return raw_url

    async def search(self, query: str, limit: int = 5) -> list[SerpResultItem]:
        """Execute web search and return parsed, SSRF-safe result links and snippets."""
        if not query.strip():
            return []

        results: list[SerpResultItem] = []
        try:
            async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
                resp = await client.post(
                    self.SEARCH_URL,
                    data={"q": query},
                    headers=self.headers,
                )
                if resp.status_code != 200:
                    logger.warning(
                        "DuckDuckGo SERP search returned HTTP %d for '%s'", resp.status_code, query
                    )
                    return []

                selector = Selector(resp.text)
                result_elements = selector.css(".result")

                for el in result_elements:
                    title_elem = el.css(".result__title a")
                    snippet_elem = el.css(".result__snippet")

                    raw_href = title_elem.attrib.get("href", "")
                    if not raw_href:
                        continue

                    clean_url = self._clean_url(raw_href)
                    if not clean_url.startswith(("http://", "https://")):
                        continue

                    # SSRF safety filter: prevent internal or private URLs from SERP poisoning
                    safe, _ = is_ssrf_safe_url(clean_url)
                    if not safe:
                        continue

                    title = "".join(title_elem.css("::text").getall()).strip()
                    snippet = "".join(snippet_elem.css("::text").getall()).strip()
                    domain = (urllib.parse.urlsplit(clean_url).hostname or "").lower()

                    results.append(
                        SerpResultItem(
                            title=title,
                            url=clean_url,
                            snippet=snippet,
                            domain=domain,
                        )
                    )
                    if len(results) >= limit:
                        break

        except Exception as exc:
            logger.warning("DuckDuckGo SERP lookup error for '%s': %s", query, exc)

        return results
