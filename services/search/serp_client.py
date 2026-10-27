from __future__ import annotations

import logging
import urllib.parse

import httpx
from parsel import Selector
from pydantic import BaseModel

from services.fetcher.security import is_ssrf_safe_url

try:
    from curl_cffi.requests import AsyncSession

    HAVE_CURL_CFFI = True
except ImportError:  # pragma: no cover
    HAVE_CURL_CFFI = False

logger = logging.getLogger(__name__)


class SerpResultItem(BaseModel):
    title: str
    url: str
    snippet: str = ""
    domain: str = ""


class DuckDuckGoSerpClient:
    """Async zero-API-key SERP client querying DuckDuckGo endpoints with smart fallback."""

    LITE_URL = "https://duckduckgo.com/lite/"
    HTML_URL = "https://html.duckduckgo.com/html/"

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

    def _parse_html_results(self, html_text: str, limit: int) -> list[SerpResultItem]:
        """Parse result links and snippets from DDG HTML or DDG Lite page."""
        results: list[SerpResultItem] = []
        selector = Selector(html_text)

        # Format 1: DuckDuckGo HTML layout (.result)
        result_elements = selector.css(".result")
        if result_elements:
            for el in result_elements:
                title_elem = el.css(".result__title a")
                snippet_elem = el.css(".result__snippet")
                raw_href = title_elem.attrib.get("href", "")
                if not raw_href:
                    continue

                clean_url = self._clean_url(raw_href)
                if not clean_url.startswith(("http://", "https://")):
                    continue

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
            if results:
                return results

        # Format 2: DuckDuckGo Lite layout (.result-link, .result-snippet)
        link_elements = selector.css(".result-link")
        snippet_elements = selector.css(".result-snippet")
        for i, link_el in enumerate(link_elements):
            raw_href = link_el.attrib.get("href", "")
            if not raw_href:
                continue

            clean_url = self._clean_url(raw_href)
            if not clean_url.startswith(("http://", "https://")):
                continue

            safe, _ = is_ssrf_safe_url(clean_url)
            if not safe:
                continue

            title = "".join(link_el.css("::text").getall()).strip()
            snippet = (
                "".join(snippet_elements[i].css("::text").getall()).strip()
                if i < len(snippet_elements)
                else ""
            )
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

        return results

    async def _fetch_html_text(self, url: str, query: str) -> str:
        """Fetch search page HTML using curl_cffi browser impersonation or httpx fallback."""
        if HAVE_CURL_CFFI:
            try:
                async with AsyncSession(impersonate="chrome", timeout=self.timeout) as session:
                    resp = await session.get(url, params={"q": query})
                    if resp.status_code == 200 and len(resp.text) > 500:
                        return str(resp.text)
            except Exception as curl_err:
                logger.debug("curl_cffi fetch failed for %s: %s", url, curl_err)

        async with httpx.AsyncClient(
            timeout=self.timeout, follow_redirects=True, headers=self.headers
        ) as client:
            resp = await client.get(url, params={"q": query})
            if resp.status_code == 200:
                return str(resp.text)
        return ""

    async def search(self, query: str, limit: int = 5) -> list[SerpResultItem]:
        """Execute web search and return parsed, SSRF-safe result links and snippets."""
        clean_query = query.strip()
        if not clean_query:
            return []

        # 1. Primary: DuckDuckGo Lite endpoint (clean, high-availability, text-first)
        try:
            html = await self._fetch_html_text(self.LITE_URL, clean_query)
            if html:
                results = self._parse_html_results(html, limit=limit)
                if results:
                    return results
        except Exception as exc:
            logger.warning("DuckDuckGo Lite search error for '%s': %s", clean_query, exc)

        # 2. Secondary: DuckDuckGo HTML endpoint
        try:
            html = await self._fetch_html_text(self.HTML_URL, clean_query)
            if html:
                results = self._parse_html_results(html, limit=limit)
                if results:
                    return results
        except Exception as exc:
            logger.warning("DuckDuckGo HTML search error for '%s': %s", clean_query, exc)

        return []
