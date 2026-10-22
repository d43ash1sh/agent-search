from __future__ import annotations

from typing import Any

from parsel import Selector


class FallbackCleaner:
    """Robust DOM-based fallback extractor for non-article HTML pages."""

    def extract(self, raw_html: str, url: str) -> dict[str, Any] | None:
        if not raw_html or not raw_html.strip():
            return None

        try:
            sel = Selector(text=raw_html)
            title = " ".join(sel.css("title::text").getall()).strip()

            # Remove scripts, styles, navigation, headers, footers
            block_xpath = (
                "//body//*[self::h1 or self::h2 or self::h3 or self::h4 or self::h5 "
                "or self::h6 or self::p or self::li or self::blockquote or self::pre "
                "or self::td or self::th or self::dt or self::dd or self::figcaption]"
                "[not(ancestor::script or ancestor::style or ancestor::noscript "
                "or ancestor::svg or ancestor::nav or ancestor::footer or ancestor::header)]"
                "[not(ancestor::*[self::h1 or self::h2 or self::h3 or self::h4 or self::h5 "
                "or self::h6 or self::p or self::li or self::blockquote or self::pre "
                "or self::td or self::th or self::dt or self::dd or self::figcaption])]"
            )
            raw_blocks = [
                " ".join(" ".join(el.xpath(".//text()").getall()).split()) for el in sel.xpath(block_xpath)
            ]
            blocks = list(dict.fromkeys(b for b in raw_blocks if b))
            clean_text = "\n\n".join(blocks)
            if not clean_text:
                body_text = " ".join(
                    " ".join(
                        sel.xpath(
                            "//body//text()[not(ancestor::script or ancestor::style "
                            "or ancestor::noscript or ancestor::svg or ancestor::nav "
                            "or ancestor::footer or ancestor::header)]"
                        ).getall()
                    ).split()
                )
                clean_text = body_text

            if not clean_text:
                return None

            return {
                "title": title,
                "author": None,
                "published_date": None,
                "sitename": None,
                "description": None,
                "clean_text": clean_text,
                "markdown": clean_text,
            }
        except Exception:
            return None
