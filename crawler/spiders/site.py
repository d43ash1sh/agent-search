import collections
import re
from collections.abc import AsyncIterator, Iterator
from typing import Any
from urllib.parse import urljoin, urlsplit

import scrapy
from scrapy.http import HtmlResponse, Request, Response, TextResponse
from scrapy.linkextractors import LinkExtractor

from crawler.items import DocumentItem
from crawler.urls import host_is_allowed, normalize_url


class SiteSpider(scrapy.Spider):
    name = "site"

    def __init__(
        self,
        start_urls: list[str] | None = None,
        allow_domains: list[str] | None = None,
        max_pages_per_domain: int = 0,
        follow_sitemaps: bool = False,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.allowed_domains = [domain.lower().lstrip(".") for domain in (allow_domains or [])]
        self.start_urls = [
            normalized
            for url in (start_urls or [])
            if (normalized := normalize_url(url)) and host_is_allowed(normalized, self.allowed_domains)
        ]
        self.max_pages_per_domain = max_pages_per_domain
        self.follow_sitemaps = follow_sitemaps
        self.domain_counts: dict[str, int] = collections.defaultdict(int)
        self.link_extractor = LinkExtractor(
            deny_extensions=[
                "7z",
                "avi",
                "css",
                "doc",
                "docx",
                "exe",
                "gif",
                "gz",
                "jpg",
                "jpeg",
                "js",
                "mp3",
                "mp4",
                "pdf",
                "png",
                "ppt",
                "pptx",
                "rar",
                "svg",
                "tar",
                "webm",
                "webp",
                "xls",
                "xlsx",
                "zip",
            ],
        )

    async def start(self) -> AsyncIterator[Request]:
        for url in self.start_urls:
            yield scrapy.Request(url, callback=self.parse, errback=self.on_request_error)
            if self.follow_sitemaps:
                sitemap_url = urljoin(url, "/sitemap.xml")
                yield scrapy.Request(
                    sitemap_url,
                    callback=self.parse_sitemap,
                    errback=self.ignore_errback,
                    priority=10,
                )

    def parse_sitemap(self, response: Response) -> Iterator[Request]:
        """Extract URLs from XML sitemaps and sitemap index files."""
        if not isinstance(response, TextResponse | HtmlResponse):
            return

        urls: list[str] = []
        try:
            urls = response.xpath("//*[local-name()='loc']/text()").getall()
        except Exception:
            urls = re.findall(r"<loc>\s*(https?://[^<]+)\s*</loc>", response.text, re.IGNORECASE)

        for raw_url in urls:
            target = normalize_url(raw_url.strip())
            if not target or not host_is_allowed(target, self.allowed_domains):
                continue

            target_domain = (urlsplit(target).hostname or "").lower()
            if (
                self.max_pages_per_domain > 0
                and self.domain_counts[target_domain] >= self.max_pages_per_domain
            ):
                continue

            # If sitemap index links to another sitemap, parse it too
            if target.endswith(".xml") or "sitemap" in target:
                yield scrapy.Request(
                    target,
                    callback=self.parse_sitemap,
                    errback=self.ignore_errback,
                    priority=5,
                )
            else:
                yield scrapy.Request(
                    target,
                    callback=self.parse,
                    errback=self.on_request_error,
                )

    def parse(self, response: Response) -> Iterator[Request | DocumentItem]:
        if not isinstance(response, HtmlResponse):
            return

        page_url = normalize_url(response.url)
        if page_url is None:
            return

        domain = (urlsplit(page_url).hostname or "").lower()
        if self.max_pages_per_domain > 0 and self.domain_counts[domain] >= self.max_pages_per_domain:
            return

        self.domain_counts[domain] += 1

        title = " ".join(response.css("title::text").getall()).strip()
        block_selector = (
            "//body//*[self::h1 or self::h2 or self::h3 or self::h4 or self::h5 "
            "or self::h6 or self::p or self::li or self::blockquote or self::pre "
            "or self::td or self::th or self::dt or self::dd or self::figcaption]"
            "[not(ancestor::script or ancestor::style or ancestor::noscript "
            "or ancestor::svg or ancestor::nav or ancestor::footer or ancestor::header)]"
            "[not(ancestor::*[self::h1 or self::h2 or self::h3 or self::h4 or self::h5 "
            "or self::h6 or self::p or self::li or self::blockquote or self::pre "
            "or self::td or self::th or self::dt or self::dd or self::figcaption])]"
        )
        blocks = list(
            dict.fromkeys(
                " ".join(" ".join(block.xpath(".//text()").getall()).split())
                for block in response.xpath(block_selector)
                if block.xpath(".//text()").getall()
            )
        )
        text = " ".join(blocks)
        if not text:
            text = " ".join(
                " ".join(
                    response.xpath(
                        "//body//text()[not(ancestor::script or ancestor::style "
                        "or ancestor::noscript or ancestor::svg or ancestor::nav "
                        "or ancestor::footer or ancestor::header)]"
                    ).getall()
                ).split()
            )
        canonical_href = response.css('link[rel~="canonical"]::attr(href)').get()
        canonical_url = normalize_url(urljoin(response.url, canonical_href)) if canonical_href else page_url
        if not canonical_url:
            canonical_url = page_url

        yield {
            "url": page_url,
            "canonical_url": canonical_url,
            "title": title,
            "text": text,
            "blocks": blocks,
        }

        # Follow outgoing links if domain budget has remaining quota
        if not self.max_pages_per_domain or self.domain_counts[domain] < self.max_pages_per_domain:
            for link in self.link_extractor.extract_links(response):
                target = normalize_url(link.url)
                if target and host_is_allowed(target, self.allowed_domains):
                    target_domain = (urlsplit(target).hostname or "").lower()
                    if (
                        self.max_pages_per_domain > 0
                        and self.domain_counts[target_domain] >= self.max_pages_per_domain
                    ):
                        continue
                    yield scrapy.Request(
                        target,
                        callback=self.parse,
                        errback=self.on_request_error,
                    )

    def on_request_error(self, failure: Any) -> None:
        """Log request failures politely without halting the crawl."""
        url = getattr(getattr(failure, "request", None), "url", "unknown URL")
        self.logger.warning("Failed request for %s: %s", url, failure.value)

    def ignore_errback(self, failure: Any) -> None:
        """Silently ignore missing sitemaps or optional resource failures."""
        pass
