import hashlib
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlsplit

from scrapy.http import HtmlResponse

from crawler.pipelines import _clean_blocks, _passage_records
from crawler.search import estimate_tokens
from crawler.urls import normalize_url

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36 (AgentSearchBot/1.0; +https://github.com/agent-search)"
)


def fetch_live_page(url: str, timeout: float = 10.0) -> dict[str, Any]:
    """Fetch an arbitrary live web page, clean it, and extract structured evidence passages."""
    normalized = normalize_url(url)
    if not normalized:
        raise ValueError(f"Invalid URL: {url}")

    req = urllib.request.Request(
        normalized,
        headers={
            "User-Agent": DEFAULT_USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            status = response.status
            content_type = response.headers.get_content_type()
            raw_bytes = response.read(10 * 1024 * 1024)  # 10 MB limit
            encoding = response.headers.get_content_charset() or "utf-8"
            html_text = raw_bytes.decode(encoding, errors="replace")
            final_url = response.url or normalized
    except urllib.error.HTTPError as error:
        raise RuntimeError(f"HTTP {error.code}: {error.reason} for {normalized}") from error
    except urllib.error.URLError as error:
        raise RuntimeError(f"Connection error: {error.reason} for {normalized}") from error

    scrapy_response = HtmlResponse(url=final_url, body=html_text.encode("utf-8"), encoding="utf-8")

    title = " ".join(scrapy_response.css("title::text").getall()).strip()
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
    raw_blocks = list(
        dict.fromkeys(
            " ".join(" ".join(block.xpath(".//text()").getall()).split())
            for block in scrapy_response.xpath(block_selector)
            if block.xpath(".//text()").getall()
        )
    )

    blocks = _clean_blocks(raw_blocks)
    clean_text = " ".join(blocks)
    if not clean_text:
        clean_text = " ".join(
            " ".join(
                scrapy_response.xpath(
                    "//body//text()[not(ancestor::script or ancestor::style "
                    "or ancestor::noscript or ancestor::svg or ancestor::nav "
                    "or ancestor::footer or ancestor::header)]"
                ).getall()
            ).split()
        )

    canonical_href = scrapy_response.css('link[rel~="canonical"]::attr(href)').get()
    canonical_url = normalize_url(canonical_href) if canonical_href else final_url
    if not canonical_url:
        canonical_url = final_url

    domain = (urlsplit(final_url).hostname or "").lower()
    content_hash = hashlib.sha256("\0".join((canonical_url, title, clean_text)).encode("utf-8")).hexdigest()

    records = _passage_records(final_url, clean_text)
    passages = [
        {
            "ordinal": ordinal,
            "evidence_id": evidence_id,
            "text": passage_text,
            "start_offset": start_off,
            "end_offset": end_off,
            "estimated_tokens": estimate_tokens(passage_text),
        }
        for ordinal, (passage_text, evidence_id, start_off, end_off) in enumerate(records)
    ]

    return {
        "url": final_url,
        "canonical_url": canonical_url,
        "title": title,
        "domain": domain,
        "status": status,
        "content_type": content_type,
        "content_hash": content_hash,
        "estimated_tokens": estimate_tokens(clean_text),
        "text": clean_text,
        "passage_count": len(passages),
        "passages": passages,
    }
