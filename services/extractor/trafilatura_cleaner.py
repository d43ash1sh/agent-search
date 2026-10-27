from __future__ import annotations

import logging
from typing import Any

import trafilatura

from crawler.cleaning import DEFAULT_PRUNE_XPATH, sanitize_markdown

logging.getLogger("trafilatura").setLevel(logging.ERROR)
logging.getLogger("trafilatura.xml").setLevel(logging.ERROR)
logging.getLogger("trafilatura.core").setLevel(logging.ERROR)
logging.getLogger("trafilatura.htmlprocessing").setLevel(logging.ERROR)

logger = logging.getLogger(__name__)


class TrafilaturaCleaner:
    """State-of-the-art content extraction using Trafilatura in precision mode."""

    def __init__(self, prune_xpath: list[str] | None = None) -> None:
        self.prune_xpath = prune_xpath or DEFAULT_PRUNE_XPATH

    def extract(self, raw_html: str, url: str) -> dict[str, Any] | None:
        """Extract clean body text, metadata, and structured markdown from raw HTML.

        Uses favor_precision=True and include_links=False to prevent noisy link dumps
        and web boilerplate from polluting AI agent context.
        """
        if not raw_html or not raw_html.strip():
            return None

        try:
            # 1. Extract clean markdown with precision mode and boilerplate pruning
            raw_markdown = trafilatura.extract(
                raw_html,
                url=url,
                output_format="markdown",
                favor_precision=True,
                include_links=False,
                include_formatting=True,
                include_tables=True,
                include_comments=False,
                prune_xpath=self.prune_xpath,
            )

            # 2. Extract structured metadata (title, author, published date, sitename)
            doc_meta = trafilatura.bare_extraction(
                raw_html,
                url=url,
                with_metadata=True,
                include_comments=False,
            )

            title = ""
            author = None
            date = None
            sitename = None
            description = None

            if doc_meta:
                if isinstance(doc_meta, dict):
                    title = str(doc_meta.get("title") or "")
                    author = doc_meta.get("author")
                    date = doc_meta.get("date")
                    sitename = doc_meta.get("sitename")
                    description = doc_meta.get("description")
                else:
                    title = str(getattr(doc_meta, "title", "") or "")
                    author = getattr(doc_meta, "author", None)
                    date = getattr(doc_meta, "date", None)
                    sitename = getattr(doc_meta, "sitename", None)
                    description = getattr(doc_meta, "description", None)

            # Fallback to plain text if markdown extraction produced nothing
            if not raw_markdown:
                plain = trafilatura.extract(
                    raw_html,
                    url=url,
                    output_format="txt",
                    favor_precision=True,
                    include_links=False,
                    include_comments=False,
                    prune_xpath=self.prune_xpath,
                )
                if not plain:
                    return None
                raw_markdown = plain

            markdown = sanitize_markdown(raw_markdown)
            if not markdown or len(markdown.strip()) < 30:
                return None

            return {
                "title": title,
                "author": author,
                "published_date": date,
                "sitename": sitename,
                "description": description,
                "clean_text": markdown,
                "markdown": markdown,
            }
        except Exception as error:
            logger.warning("Trafilatura extraction failed for %s: %s", url, error)
            return None
