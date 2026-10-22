from __future__ import annotations

import logging
import re
from typing import Any

import trafilatura

logger = logging.getLogger(__name__)


def sanitize_markdown(text: str) -> str:
    """Sanitize extracted markdown by removing raw HTML artifacts and anchor noise."""
    if not text:
        return ""

    # 1. Strip raw styling HTML tags (<font>, <span>, <u>, <b>, <i>, <div>, etc.)
    text = re.sub(
        r"</?(?:font|span|u|b|i|div|em|strong|mark|small|abbr|code-annotation)[^>]*>",
        "",
        text,
        flags=re.IGNORECASE,
    )

    # 2. Strip permalink anchors like [¶](...), [#](...), [](url), and glyphs
    text = re.sub(r"\[(?:[¶#§]|\s*)\]\([^)]+\)", "", text)
    text = re.sub(r"[¶\uf0c1\u200b\ufeff]", "", text)

    # 3. Strip empty links like [](...)
    text = re.sub(r"\[\s*\]\([^)]+\)", "", text)

    # 4. Collapse 3+ newlines to 2 newlines (markdown paragraph separation)
    text = re.sub(r"\n{3,}", "\n\n", text)

    return text.strip()


def extract_clean_markdown(raw_html: str, url: str) -> dict[str, Any] | None:
    """Extract clean structured Markdown and metadata using Trafilatura."""
    if not raw_html or not raw_html.strip():
        return None

    try:
        markdown = trafilatura.extract(
            raw_html,
            url=url,
            output_format="markdown",
            include_formatting=True,
            include_tables=True,
            include_links=True,
            favor_precision=True,
        )
        if not markdown or not markdown.strip():
            return None

        clean_md = sanitize_markdown(markdown)
        if not clean_md:
            return None

        # Extract metadata
        meta = trafilatura.bare_extraction(raw_html, url=url, with_metadata=True)
        title = ""
        if meta and isinstance(meta, dict):
            title = str(meta.get("title") or "")
        elif meta:
            title = str(getattr(meta, "title", "") or "")

        # Split into logical markdown blocks (paragraphs, headers, code fences)
        blocks = [block.strip() for block in clean_md.split("\n\n") if block.strip()]

        return {
            "title": title,
            "text": clean_md,
            "markdown": clean_md,
            "blocks": blocks,
        }
    except Exception as error:
        logger.debug("Trafilatura markdown extraction skipped for %s: %s", url, error)
        return None


def clean_legacy_text(text: str) -> str:
    """Repair and clean legacy text that was flattened or corrupted by token spacing."""
    if not text:
        return ""

    # 1. Strip raw HTML tags
    text = re.sub(
        r"</?(?:font|span|u|b|i|div|em|strong|mark|small|abbr|code-annotation)[^>]*>",
        "",
        text,
        flags=re.IGNORECASE,
    )

    # 2. Strip permalink anchors and unicode noise
    text = re.sub(r"\[(?:[¶#§]|\s*)\]\([^)]+\)", "", text)
    text = re.sub(r"[¶\uf0c1\u200b\ufeff]", "", text)

    # 3. Fix artificial token spaces before punctuation: "word ." -> "word.", "word ," -> "word,"
    text = re.sub(r"\s+([.,;:?!])", r"\1", text)

    # 4. Fix artificial spaces inside brackets: "( text )" -> "(text)", "[ text ]" -> "[text]"
    text = re.sub(r"([(\[{])\s+", r"\1", text)
    text = re.sub(r"\s+([)\]}])", r"\1", text)

    # 5. Fix spaced quotes: ' " word " ' -> ' "word" '
    text = re.sub(r'"\s+([^"]+?)\s+"', r'"\1"', text)
    text = re.sub(r"'\s+([^']+?)\s+'", r"'\1'", text)

    # 6. Normalize multiple spaces
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)

    return text.strip()
