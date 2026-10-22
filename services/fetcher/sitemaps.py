from __future__ import annotations

import logging
import re
from urllib.parse import urljoin

import httpx

from crawler.urls import normalize_url

logger = logging.getLogger(__name__)


class SitemapDiscoverer:
    """Async sitemap and sitemap index parser."""

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self.client = client

    async def discover_urls(self, base_url: str, max_urls: int = 500) -> list[str]:
        """Fetch /sitemap.xml and extract canonical page URLs."""
        sitemap_url = urljoin(base_url, "/sitemap.xml")
        client = self.client or httpx.AsyncClient(timeout=10.0, follow_redirects=True)
        should_close = self.client is None

        found_urls: list[str] = []
        try:
            resp = await client.get(sitemap_url)
            if resp.status_code != 200:
                return []

            content = resp.text
            # Extract all <loc>...</loc> tags
            extracted = re.findall(r"<loc>\s*(https?://[^<]+)\s*</loc>", content, re.IGNORECASE)
            for raw in extracted:
                norm = normalize_url(raw.strip())
                if norm and not norm.endswith(".xml") and norm not in found_urls:
                    found_urls.append(norm)
                    if len(found_urls) >= max_urls:
                        break
        except Exception as error:
            logger.debug("Sitemap discovery failed for %s: %s", sitemap_url, error)
        finally:
            if should_close:
                await client.aclose()

        return found_urls
