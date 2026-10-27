from __future__ import annotations

import logging
import urllib.parse

from pydantic import BaseModel

from services.fetcher.security import is_ssrf_safe_url

logger = logging.getLogger(__name__)


class SeedEntry(BaseModel):
    url: str
    domain: str
    category: str
    priority: int = 1


DEFAULT_CURATED_SEEDS: list[dict[str, str | int]] = [
    # Open Knowledge & Encyclopedias
    {
        "url": "https://en.wikipedia.org/wiki/Portal:Current_events",
        "category": "encyclopedia",
        "priority": 1,
    },
    {
        "url": "https://en.wikipedia.org/wiki/Portal:Science",
        "category": "science",
        "priority": 1,
    },
    {
        "url": "https://as.wikipedia.org/wiki/%E0%A6%AA%E0%A7%8D%E0%A7%B0%E0%A6%A5%E0%A6%AE_%E0%A6%AA%E0%A7%83%E0%A6%B7%E0%A7%8D%E0%A6%A0%E0%A6%BE",
        "category": "indic_assamese",
        "priority": 1,
    },
    # Open Tech & Developer Documentation
    {
        "url": "https://simonwillison.net",
        "category": "tech_blog",
        "priority": 2,
    },
    {
        "url": "https://fastapi.tiangolo.com",
        "category": "developer_docs",
        "priority": 2,
    },
    {
        "url": "https://docs.python.org/3/",
        "category": "developer_docs",
        "priority": 2,
    },
    # Government & Institutional Open Data
    {
        "url": "https://assam.gov.in",
        "category": "government_indic",
        "priority": 2,
    },
    {
        "url": "https://data.gov.in",
        "category": "government_open_data",
        "priority": 2,
    },
]


class SeedManager:
    """Manages high-authority bootstrap seeds for continuous autonomous web discovery."""

    def __init__(self, custom_seeds: list[SeedEntry] | None = None) -> None:
        self._seeds: list[SeedEntry] = []
        if custom_seeds:
            self._seeds.extend(custom_seeds)
        else:
            self._load_default_seeds()

    def _load_default_seeds(self) -> None:
        for item in DEFAULT_CURATED_SEEDS:
            raw_url = str(item["url"])
            safe, _ = is_ssrf_safe_url(raw_url)
            if not safe:
                continue
            domain = (urllib.parse.urlsplit(raw_url).hostname or "").lower()
            self._seeds.append(
                SeedEntry(
                    url=raw_url,
                    domain=domain,
                    category=str(item.get("category", "general")),
                    priority=int(item.get("priority", 1)),
                )
            )

    def get_seeds(self, category: str | None = None) -> list[SeedEntry]:
        """Return seed URLs, optionally filtered by category."""
        if category:
            return [s for s in self._seeds if s.category == category]
        return list(self._seeds)

    def add_seed(self, url: str, category: str = "custom", priority: int = 1) -> SeedEntry | None:
        """Add a new safe seed URL to the discovery ledger."""
        safe, _ = is_ssrf_safe_url(url)
        if not safe:
            logger.warning("Rejected non-safe seed URL: %s", url)
            return None
        domain = (urllib.parse.urlsplit(url).hostname or "").lower()
        entry = SeedEntry(url=url, domain=domain, category=category, priority=priority)
        self._seeds.append(entry)
        return entry
