from __future__ import annotations

import logging
import urllib.parse

import httpx
from parsel import Selector
from pydantic import BaseModel

from services.fetcher.security import is_ssrf_safe_url

logger = logging.getLogger(__name__)


class NewsArticleItem(BaseModel):
    title: str
    url: str
    source: str
    pub_date: str = ""
    domain: str = ""


class OpenNewsDiscovery:
    """Zero-API-key open news discovery querying public RSS search feeds.

    Discovers live articles from diverse regional and national media outlets
    (e.g., Assam Tribune, Pratidin, News18, India Today, NDTV, ANI) without commercial search APIs.
    """

    NEWS_RSS_URL = "https://news.google.com/rss/search"

    def __init__(self, timeout: float = 8.0) -> None:
        self.timeout = timeout
        self.headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": "application/rss+xml, application/xml, text/xml, */*",
        }

    async def search_news(
        self,
        query: str,
        limit: int = 10,
        region: str = "IN",
    ) -> list[NewsArticleItem]:
        """Discover live news articles matching the query across independent media sites."""
        clean_query = query.strip()
        if not clean_query:
            return []

        articles: list[NewsArticleItem] = []
        seen_domains: set[str] = set()

        params = {
            "q": clean_query,
            "hl": "en-IN",
            "gl": region,
            "ceid": f"{region}:en",
        }

        try:
            async with httpx.AsyncClient(
                timeout=self.timeout, follow_redirects=True, headers=self.headers
            ) as client:
                resp = await client.get(self.NEWS_RSS_URL, params=params)
                if resp.status_code != 200:
                    logger.warning(
                        "OpenNews RSS search returned HTTP %d for '%s'", resp.status_code, clean_query
                    )
                    return []

                selector = Selector(resp.text, type="xml")
                items = selector.css("item")

                for item in items:
                    title_elem = item.css("title::text").get() or ""
                    link_elem = item.css("link::text").get() or ""
                    source_elem = item.css("source::text").get() or ""
                    pub_date = item.css("pubDate::text").get() or ""

                    if not link_elem or not link_elem.startswith(("http://", "https://")):
                        continue

                    safe, _ = is_ssrf_safe_url(link_elem)
                    if not safe:
                        continue

                    domain = (urllib.parse.urlsplit(link_elem).hostname or "").lower()

                    # Domain diversity filter: avoid clustering more than 2 articles from same publisher
                    domain_count = sum(1 for a in articles if a.domain == domain)
                    if domain_count >= 2:
                        continue

                    articles.append(
                        NewsArticleItem(
                            title=title_elem,
                            url=link_elem,
                            source=source_elem,
                            pub_date=pub_date,
                            domain=domain,
                        )
                    )
                    seen_domains.add(domain)

                    if len(articles) >= limit:
                        break

        except Exception as exc:
            logger.warning("OpenNews discovery error for '%s': %s", clean_query, exc)

        return articles
