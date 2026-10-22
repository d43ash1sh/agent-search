from __future__ import annotations

import re
from typing import Any

from parsel import Selector


class StructuredExtractor:
    """Fast, schema-driven CSS/XPath structured data extractor inspired by Crawl4AI."""

    def extract(self, html: str, schema: dict[str, Any]) -> list[dict[str, Any]]:
        """Extract structured JSON records from HTML using a declarative schema."""
        if not html or not html.strip() or not schema:
            return []

        sel = Selector(text=html)
        base_selector = (
            schema.get("baseSelector") or schema.get("items_selector") or schema.get("item_selector")
        )
        raw_fields = schema.get("fields", [])

        fields: list[dict[str, Any]] = []
        if isinstance(raw_fields, dict):
            for name, f_def in raw_fields.items():
                if isinstance(f_def, dict):
                    fields.append({"name": name, **f_def})
                elif isinstance(f_def, str):
                    fields.append({"name": name, "selector": f_def, "type": "text"})
        elif isinstance(raw_fields, list):
            fields = raw_fields
        else:
            return []

        if not fields:
            return []

        # If baseSelector is specified, extract a list of items; otherwise extract single record
        elements = sel.css(base_selector) if base_selector else [sel]
        records: list[dict[str, Any]] = []

        for elem in elements:
            record: dict[str, Any] = {}
            for field in fields:
                name = field.get("name")
                if not name:
                    continue

                selector_css = field.get("selector", "")
                field_type = field.get("type") or ("attribute" if "attribute" in field else "text")
                default_val = field.get("default", None)
                transform = field.get("transform")
                val: Any = default_val

                target_elem = elem.css(selector_css) if selector_css else elem

                if field_type == "text":
                    texts = target_elem.xpath(".//text()").getall()
                    val = " ".join(" ".join(texts).split()) if texts else default_val

                elif field_type == "attribute":
                    attr_name = field.get("attribute", "href")
                    val = target_elem.attrib.get(attr_name, default_val)

                elif field_type == "html":
                    val = target_elem.get() or default_val

                elif field_type == "regex":
                    pattern = field.get("pattern", ".*")
                    texts = target_elem.xpath(".//text()").getall()
                    joined = " ".join(texts)
                    match = re.search(pattern, joined)
                    val = (
                        match.group(1)
                        if (match and match.groups())
                        else (match.group(0) if match else default_val)
                    )

                # Optional transforms
                if isinstance(val, str) and transform:
                    if transform == "strip":
                        val = val.strip()
                    elif transform == "lowercase":
                        val = val.lower()
                    elif transform == "uppercase":
                        val = val.upper()

                record[name] = val

            records.append(record)

        return records
