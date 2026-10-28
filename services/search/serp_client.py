from __future__ import annotations

import asyncio
import base64
import logging
import urllib.parse

import httpx
from parsel import Selector
from pydantic import BaseModel

from services.common.config import settings
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
    engine: str = "web"


class DuckDuckGoSerpClient:
    """Async SERP client querying DuckDuckGo endpoints with smart fallback to Bing."""

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
                        engine="duckduckgo",
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
                    engine="duckduckgo",
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

        limits = httpx.Limits(max_keepalive_connections=10, max_connections=20)
        async with httpx.AsyncClient(
            timeout=self.timeout, follow_redirects=True, headers=self.headers, limits=limits
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
            logger.debug("DuckDuckGo Lite search note for '%s': %s", clean_query, exc)

        # 2. Secondary: DuckDuckGo HTML endpoint
        try:
            html = await self._fetch_html_text(self.HTML_URL, clean_query)
            if html:
                results = self._parse_html_results(html, limit=limit)
                if results:
                    return results
        except Exception as exc:
            logger.debug("DuckDuckGo HTML search note for '%s': %s", clean_query, exc)

        # 3. Resilient Fallback: Bing Search
        try:
            bing_client = BingSearchClient(timeout=self.timeout)
            bing_results = await bing_client.search(clean_query, limit=limit)
            if bing_results:
                return bing_results
        except Exception as exc:
            logger.debug("Bing fallback search note for '%s': %s", clean_query, exc)

        return []


class BingSearchClient:
    """Async Bing search client supporting official Cognitive API and open Web HTML search."""

    WEB_URL = "https://www.bing.com/search"
    API_URL = "https://api.bing.microsoft.com/v7.0/search"

    def __init__(self, api_key: str | None = None, timeout: float = 8.0) -> None:
        self.api_key = api_key or settings.bing_api_key
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

    def _unwrap_bing_url(self, raw_url: str) -> str:
        """Decode Bing /ck/a?!...&u=a1<base64> tracking wrapper into destination URL."""
        if "u=a1" in raw_url:
            try:
                segment = raw_url.split("u=a1")[1].split("&")[0]
                padding = segment + "=" * (-len(segment) % 4)
                decoded = base64.b64decode(padding).decode("utf-8", errors="ignore")
                if decoded.startswith(("http://", "https://")):
                    return decoded
            except Exception:
                pass
        return raw_url

    async def _search_official_api(self, query: str, limit: int) -> list[SerpResultItem]:
        """Query official Bing Web Search v7 REST API."""
        if not self.api_key:
            return []

        headers = {"Ocp-Apim-Subscription-Key": self.api_key}
        params: dict[str, str | int | bool] = {
            "q": query,
            "count": limit,
            "textDecorations": False,
            "textFormat": "Raw",
        }
        limits = httpx.Limits(max_keepalive_connections=5, max_connections=10)

        async with httpx.AsyncClient(timeout=self.timeout, limits=limits) as client:
            resp = await client.get(self.API_URL, headers=headers, params=params)
            if resp.status_code == 200:
                data = resp.json()
                web_pages = data.get("webPages", {}).get("value", [])
                results: list[SerpResultItem] = []
                for p in web_pages:
                    url = p.get("url", "")
                    safe, _ = is_ssrf_safe_url(url)
                    if not safe:
                        continue
                    results.append(
                        SerpResultItem(
                            title=p.get("name", ""),
                            url=url,
                            snippet=p.get("snippet", ""),
                            domain=(urllib.parse.urlsplit(url).hostname or "").lower(),
                            engine="bing",
                        )
                    )
                return results
        return []

    async def _search_open_web(self, query: str, limit: int) -> list[SerpResultItem]:
        """Query open Bing web search and parse results with URL unwrapping."""
        html_text = ""
        if HAVE_CURL_CFFI:
            try:
                async with AsyncSession(impersonate="chrome", timeout=self.timeout) as session:
                    resp = await session.get(self.WEB_URL, params={"q": query})
                    if resp.status_code == 200 and len(resp.text) > 1000:
                        html_text = str(resp.text)
            except Exception as exc:
                logger.debug("curl_cffi Bing fetch note: %s", exc)

        if not html_text:
            limits = httpx.Limits(max_keepalive_connections=5, max_connections=10)
            async with httpx.AsyncClient(
                timeout=self.timeout, follow_redirects=True, headers=self.headers, limits=limits
            ) as client:
                resp = await client.get(self.WEB_URL, params={"q": query})
                if resp.status_code == 200:
                    html_text = str(resp.text)

        if not html_text:
            return []

        selector = Selector(html_text)
        items = selector.css("li.b_algo")
        results: list[SerpResultItem] = []

        for it in items:
            raw_href = it.css("h2 a::attr(href)").get("")
            if not raw_href:
                continue

            clean_url = self._unwrap_bing_url(raw_href)
            if not clean_url.startswith(("http://", "https://")):
                continue

            safe, _ = is_ssrf_safe_url(clean_url)
            if not safe:
                continue

            title = "".join(it.css("h2 a *::text, h2 a::text").getall()).strip()
            snippet = "".join(it.css(".b_caption p *::text, .b_caption p::text").getall()).strip()
            domain = (urllib.parse.urlsplit(clean_url).hostname or "").lower()

            results.append(
                SerpResultItem(
                    title=title or domain,
                    url=clean_url,
                    snippet=snippet,
                    domain=domain,
                    engine="bing",
                )
            )
            if len(results) >= limit:
                break

        return results

    async def search(self, query: str, limit: int = 5) -> list[SerpResultItem]:
        clean_query = query.strip()
        if not clean_query:
            return []

        if self.api_key:
            try:
                api_res = await self._search_official_api(clean_query, limit)
                if api_res:
                    return api_res
            except Exception as exc:
                logger.debug("Bing API search note: %s", exc)

        try:
            return await self._search_open_web(clean_query, limit)
        except Exception as exc:
            logger.debug("Bing Open Web search note: %s", exc)
            return []


class BraveSearchClient:
    """Async Brave Search client supporting official Brave Web Search REST API."""

    API_URL = "https://api.search.brave.com/res/v1/web/search"

    def __init__(self, api_key: str | None = None, timeout: float = 8.0) -> None:
        self.api_key = api_key or settings.brave_api_key
        self.timeout = timeout

    async def search(self, query: str, limit: int = 5) -> list[SerpResultItem]:
        clean_query = query.strip()
        if not clean_query:
            return []

        if not self.api_key:
            return []

        headers = {
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
            "X-Subscription-Token": self.api_key,
        }
        params: dict[str, str | int] = {
            "q": clean_query,
            "count": min(limit, 20),
            "extra_snippets": "true",
        }
        limits = httpx.Limits(max_keepalive_connections=5, max_connections=10)

        try:
            async with httpx.AsyncClient(timeout=self.timeout, limits=limits) as client:
                resp = await client.get(self.API_URL, headers=headers, params=params)
                if resp.status_code == 200:
                    data = resp.json()
                    results: list[SerpResultItem] = []
                    web_results = data.get("web", {}).get("results", [])
                    for item in web_results:
                        url = item.get("url", "")
                        safe, _ = is_ssrf_safe_url(url)
                        if not safe:
                            continue

                        snippet = item.get("description", "")
                        extra = item.get("extra_snippets", [])
                        if extra and isinstance(extra, list):
                            snippet = snippet + " " + " ".join(extra[:2])

                        results.append(
                            SerpResultItem(
                                title=item.get("title", ""),
                                url=url,
                                snippet=snippet.strip(),
                                domain=(urllib.parse.urlsplit(url).hostname or "").lower(),
                                engine="brave",
                            )
                        )
                        if len(results) >= limit:
                            break
                    return results
        except Exception as exc:
            logger.debug("Brave Search API error: %s", exc)

        return []


class SearXNGSearchClient:
    """Async client querying SearXNG metasearch instance."""

    def __init__(self, base_url: str | None = None, timeout: float = 8.0) -> None:
        self.base_url = (base_url or settings.searxng_url or "").rstrip("/")
        self.timeout = timeout

    async def search(self, query: str, limit: int = 5) -> list[SerpResultItem]:
        clean_query = query.strip()
        if not clean_query or not self.base_url:
            return []

        endpoint = f"{self.base_url}/search"
        params = {"q": clean_query, "format": "json"}
        limits = httpx.Limits(max_keepalive_connections=5, max_connections=10)

        try:
            async with httpx.AsyncClient(timeout=self.timeout, limits=limits) as client:
                resp = await client.get(endpoint, params=params)
                if resp.status_code == 200:
                    data = resp.json()
                    results: list[SerpResultItem] = []
                    for item in data.get("results", []):
                        url = item.get("url", "")
                        safe, _ = is_ssrf_safe_url(url)
                        if not safe:
                            continue
                        results.append(
                            SerpResultItem(
                                title=item.get("title", ""),
                                url=url,
                                snippet=item.get("content", ""),
                                domain=(urllib.parse.urlsplit(url).hostname or "").lower(),
                                engine=item.get("engine", "searxng"),
                            )
                        )
                        if len(results) >= limit:
                            break
                    return results
        except Exception as exc:
            logger.debug("SearXNG search note: %s", exc)

        return []


class FederatedSerpClient:
    """Enterprise multi-engine federated search coordinator.

    Queries DuckDuckGo, Bing, Brave, and SearXNG concurrently, merges results,
    deduplicates canonical URLs and domains, and ensures high search resilience.
    """

    def __init__(self, timeout: float = 8.0) -> None:
        self.timeout = timeout
        self.ddg = DuckDuckGoSerpClient(timeout=timeout)
        self.bing = BingSearchClient(timeout=timeout)
        self.brave = BraveSearchClient(timeout=timeout)
        self.searxng = SearXNGSearchClient(timeout=timeout)

    async def search(
        self,
        query: str,
        limit: int = 5,
        engines: list[str] | None = None,
    ) -> list[SerpResultItem]:
        """Search across all active providers in parallel and aggregate deduplicated results."""
        clean_query = query.strip()
        if not clean_query:
            return []

        tasks = []
        # Dispatch to active search providers
        if not engines or "bing" in engines:
            tasks.append(self.bing.search(clean_query, limit=limit))
        if not engines or "duckduckgo" in engines:
            tasks.append(self.ddg.search(clean_query, limit=limit))
        if (not engines or "brave" in engines) and (self.brave.api_key or engines):
            tasks.append(self.brave.search(clean_query, limit=limit))
        if (not engines or "searxng" in engines) and (self.searxng.base_url or engines):
            tasks.append(self.searxng.search(clean_query, limit=limit))

        batch_results = await asyncio.gather(*tasks, return_exceptions=True)

        merged: list[SerpResultItem] = []
        seen_urls: set[str] = set()
        domain_counts: dict[str, int] = {}

        # Round-robin or interleaved collection across engines for diversity
        engine_buckets: list[list[SerpResultItem]] = []
        for res in batch_results:
            if isinstance(res, list):
                engine_buckets.append(res)

        max_len = max((len(b) for b in engine_buckets), default=0)
        for i in range(max_len):
            for bucket in engine_buckets:
                if i < len(bucket):
                    item = bucket[i]
                    normalized_url = item.url.rstrip("/")
                    if normalized_url in seen_urls:
                        continue

                    dom = item.domain
                    count = domain_counts.get(dom, 0)
                    if count >= 2:
                        continue

                    seen_urls.add(normalized_url)
                    domain_counts[dom] = count + 1
                    merged.append(item)
                    if len(merged) >= limit:
                        return merged

        # If domain diversity cap was too strict, add remaining items up to limit
        if len(merged) < limit:
            for bucket in engine_buckets:
                for item in bucket:
                    normalized_url = item.url.rstrip("/")
                    if normalized_url not in seen_urls:
                        seen_urls.add(normalized_url)
                        merged.append(item)
                        if len(merged) >= limit:
                            break
                if len(merged) >= limit:
                    break

        return merged
