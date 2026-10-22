from __future__ import annotations

import logging
from typing import Any

import trafilatura

from crawler.cleaning import sanitize_markdown

logger = logging.getLogger(__name__)


class TrafilaturaCleaner:
    """State-of-the-art content extraction using Trafilatura."""

    def extract(self, raw_html: str, url: str) -> dict[str, Any] | None:
        """Extract clean body text, metadata, and markdown from raw HTML."""
        if not raw_html or not raw_html.strip():
            return None

        try:
            # Extract structured document object with metadata
            doc = trafilatura.bare_extraction(
                raw_html,
                url=url,
                with_metadata=True,
                include_comments=False,
                include_tables=True,
                include_links=True,
            )
            if not doc:
                return None

            if isinstance(doc, dict):
                text = str(doc.get("text") or "").strip()
                title = str(doc.get("title") or "")
                author = doc.get("author")
                date = doc.get("date")
                sitename = doc.get("sitename")
                description = doc.get("description")
            else:
                text = str(getattr(doc, "text", "") or "").strip()
                title = str(getattr(doc, "title", "") or "")
                author = getattr(doc, "author", None)
                date = getattr(doc, "date", None)
                sitename = getattr(doc, "sitename", None)
                description = getattr(doc, "description", None)

            if not text:
                return None

            # Extract markdown formatted text for LLM readability
            raw_markdown = (
                trafilatura.extract(
                    raw_html,
                    url=url,
                    output_format="markdown",
                    with_metadata=False,
                    include_comments=False,
                )
                or text
            )
            markdown = sanitize_markdown(raw_markdown)

            return {
                "title": title,
                "author": author,
                "published_date": date,
                "sitename": sitename,
                "description": description,
                "clean_text": text,
                "markdown": markdown,
            }
        except Exception as error:
            logger.warning("Trafilatura extraction failed for %s: %s", url, error)
            return None
