from __future__ import annotations

import logging
import urllib.parse

from parsel import Selector
from pydantic import BaseModel

from services.fetcher.security import is_ssrf_safe_url

logger = logging.getLogger(__name__)

TRACKING_PARAMS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "fbclid",
    "gclid",
    "ref",
    "source",
}


class LinkEdge(BaseModel):
    source_url: str
    target_url: str
    source_domain: str
    target_domain: str
    is_internal: bool


class HarvestedLinks(BaseModel):
    source_url: str
    source_domain: str
    internal_links: list[str]
    external_links: list[str]
    edges: list[LinkEdge]


def normalize_clean_url(url: str) -> str:
    """Normalize URL by stripping fragments, trailing slashes, and analytics query params."""
    parsed = urllib.parse.urlsplit(url)
    # Strip tracking query params
    if parsed.query:
        qs = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
        filtered_qs = [(k, v) for k, v in qs if k.lower() not in TRACKING_PARAMS]
        clean_query = urllib.parse.urlencode(filtered_qs)
    else:
        clean_query = ""

    path = parsed.path
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")

    return urllib.parse.urlunsplit(
        (
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            path or "/",
            clean_query,
            "",  # Remove fragment
        )
    )


class LinkHarvester:
    """Extracts and normalizes all outlinks from an HTML document to construct the web link graph."""

    @staticmethod
    def harvest(raw_html: str, source_url: str, max_links: int = 150) -> HarvestedLinks:
        """Parse HTML, extract all valid outlinks, and generate directed graph edges."""
        parsed_source = urllib.parse.urlsplit(source_url)
        source_domain = (parsed_source.hostname or "").lower()

        internal_links: list[str] = []
        external_links: list[str] = []
        edges: list[LinkEdge] = []
        seen_targets: set[str] = {normalize_clean_url(source_url)}

        try:
            selector = Selector(raw_html)
            raw_hrefs = selector.css("a::attr(href)").getall()

            for href in raw_hrefs:
                href = href.strip()
                if not href or href.startswith(("#", "javascript:", "mailto:", "tel:", "data:")):
                    continue

                # Resolve relative links
                absolute_url = urllib.parse.urljoin(source_url, href)
                clean_url = normalize_clean_url(absolute_url)

                if clean_url in seen_targets:
                    continue

                if not clean_url.startswith(("http://", "https://")):
                    continue

                # SSRF safety filter
                safe, _ = is_ssrf_safe_url(clean_url)
                if not safe:
                    continue

                seen_targets.add(clean_url)
                target_parsed = urllib.parse.urlsplit(clean_url)
                target_domain = (target_parsed.hostname or "").lower()

                if not target_domain:
                    continue

                is_internal = (target_domain == source_domain) or target_domain.endswith("." + source_domain)

                if is_internal:
                    internal_links.append(clean_url)
                else:
                    external_links.append(clean_url)

                edges.append(
                    LinkEdge(
                        source_url=source_url,
                        target_url=clean_url,
                        source_domain=source_domain,
                        target_domain=target_domain,
                        is_internal=is_internal,
                    )
                )

                if len(seen_targets) >= max_links:
                    break

        except Exception as exc:
            logger.debug("Failed harvesting outlinks for %s: %s", source_url, exc)

        return HarvestedLinks(
            source_url=source_url,
            source_domain=source_domain,
            internal_links=internal_links,
            external_links=external_links,
            edges=edges,
        )
