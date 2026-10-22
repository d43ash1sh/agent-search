from collections.abc import AsyncIterator, Iterator
from urllib.parse import urljoin

import scrapy
from scrapy.http import HtmlResponse, Request, Response
from scrapy.linkextractors import LinkExtractor

from crawler.items import DocumentItem
from crawler.urls import host_is_allowed, normalize_url


class SiteSpider(scrapy.Spider):
    name = "site"

    def __init__(
        self,
        start_urls: list[str] | None = None,
        allow_domains: list[str] | None = None,
        **kwargs: str,
    ) -> None:
        super().__init__(**kwargs)
        self.allowed_domains = [domain.lower().lstrip(".") for domain in (allow_domains or [])]
        self.start_urls = [
            normalized
            for url in (start_urls or [])
            if (normalized := normalize_url(url)) and host_is_allowed(normalized, self.allowed_domains)
        ]
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
            yield scrapy.Request(url, callback=self.parse)

    def parse(self, response: Response) -> Iterator[Request | DocumentItem]:
        if not isinstance(response, HtmlResponse):
            return

        page_url = normalize_url(response.url)
        if page_url is None:
            return

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

        for link in self.link_extractor.extract_links(response):
            target = normalize_url(link.url)
            if target and host_is_allowed(target, self.allowed_domains):
                yield scrapy.Request(target, callback=self.parse)
