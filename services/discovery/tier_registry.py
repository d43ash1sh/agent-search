from __future__ import annotations

import urllib.parse
from enum import StrEnum
from typing import TYPE_CHECKING, ClassVar

if TYPE_CHECKING:
    from services.search.decomposer import QueryIntent


class DomainTier(StrEnum):
    TIER_1_PRIMARY = "TIER_1_PRIMARY"
    TIER_2_REPUTABLE_MEDIA = "TIER_2_REPUTABLE_MEDIA"
    TIER_3_GENERAL = "TIER_3_GENERAL"
    TIER_4_LOW_SIGNAL = "TIER_4_LOW_SIGNAL"


class DomainTierRegistry:
    """Classifies source domains into authority tiers and provides query routing multipliers.

    Prevents SEO spam and uncorroborated assertions from diluting high-evidence facts.
    """

    # Multipliers applied to candidate scores during evidence ranking
    TIER_MULTIPLIERS: ClassVar[dict[DomainTier, float]] = {
        DomainTier.TIER_1_PRIMARY: 1.50,
        DomainTier.TIER_2_REPUTABLE_MEDIA: 1.25,
        DomainTier.TIER_3_GENERAL: 1.00,
        DomainTier.TIER_4_LOW_SIGNAL: 0.50,
    }

    # Suffixes matching Tier 1 government, academic, or multilateral organizations
    TIER_1_SUFFIXES: ClassVar[tuple[str, ...]] = (
        ".gov",
        ".gov.in",
        ".gov.uk",
        ".gov.au",
        ".europa.eu",
        ".edu",
        ".ac.in",
        ".ac.uk",
        ".int",
    )

    # Exact domains for Tier 1: Academic repositories, primary scientific journals, official docs
    TIER_1_DOMAINS: ClassVar[set[str]] = {
        # Scientific & Academic Repositories
        "arxiv.org",
        "nih.gov",
        "ncbi.nlm.nih.gov",
        "pubmed.ncbi.nlm.nih.gov",
        "nature.com",
        "science.org",
        "biorxiv.org",
        "medrxiv.org",
        "semanticscholar.org",
        "ieee.org",
        "acm.org",
        "pnas.org",
        "cell.com",
        "thelancet.com",
        "nejm.org",
        "who.int",
        "un.org",
        "w3.org",
        "ietf.org",
        "iso.org",
        "sec.gov",
        "india.gov.in",
        "assam.gov.in",
        # Official Software Specifications & Documentation
        "docs.python.org",
        "python.org",
        "golang.org",
        "go.dev",
        "pkg.go.dev",
        "rust-lang.org",
        "doc.rust-lang.org",
        "crates.io",
        "developer.mozilla.org",
        "github.com",
        "pypi.org",
        "postgresql.org",
        "redis.io",
        "fastapi.tiangolo.com",
        "pydantic.dev",
        "docker.com",
        "kubernetes.io",
    }

    # Exact domains for Tier 2: Reputable news agencies, wire services, curated encyclopedias
    TIER_2_DOMAINS: ClassVar[set[str]] = {
        # Global & Regional Press
        "reuters.com",
        "apnews.com",
        "bloomberg.com",
        "bbc.com",
        "bbc.co.uk",
        "thehindu.com",
        "indianexpress.com",
        "timesofindia.indiatimes.com",
        "hindustantimes.com",
        "asomiyapratidin.in",
        "ndtv.com",
        "economist.com",
        "wsj.com",
        "ft.com",
        "theguardian.com",
        "aljazeera.com",
        # Curated Reference & Knowledge
        "wikipedia.org",
        "en.wikipedia.org",
        "as.wikipedia.org",
        "britannica.com",
        "plato.stanford.edu",
    }

    # Low signal content farms / scrapers
    TIER_4_DOMAINS: ClassVar[set[str]] = {
        "ezinearticles.com",
        "hubpages.com",
        "answers.com",
        "quora.com",
        "ehow.com",
        "buzzfeed.com",
        "w3schools.com",  # often outdated vs MDN
    }

    @classmethod
    def clean_domain(cls, domain_or_url: str) -> str:
        domain = domain_or_url.strip().lower()
        if "://" in domain:
            domain = urllib.parse.urlsplit(domain).hostname or ""
        elif "/" in domain:
            domain = domain.split("/")[0]
        # Remove common port or userinfo
        if ":" in domain:
            domain = domain.split(":")[0]
        return domain

    @classmethod
    def classify_domain(cls, domain_or_url: str) -> DomainTier:
        domain = cls.clean_domain(domain_or_url)
        if not domain:
            return DomainTier.TIER_3_GENERAL

        # 1. Tier 4 check
        if domain in cls.TIER_4_DOMAINS:
            return DomainTier.TIER_4_LOW_SIGNAL
        for t4 in cls.TIER_4_DOMAINS:
            if domain.endswith("." + t4):
                return DomainTier.TIER_4_LOW_SIGNAL

        # 2. Tier 1 check (direct match or ends with TIER_1_DOMAINS or TIER_1_SUFFIXES)
        if domain in cls.TIER_1_DOMAINS:
            return DomainTier.TIER_1_PRIMARY
        for t1 in cls.TIER_1_DOMAINS:
            if domain.endswith("." + t1):
                return DomainTier.TIER_1_PRIMARY
        for suffix in cls.TIER_1_SUFFIXES:
            if domain.endswith(suffix):
                return DomainTier.TIER_1_PRIMARY

        # 3. Tier 2 check
        if domain in cls.TIER_2_DOMAINS:
            return DomainTier.TIER_2_REPUTABLE_MEDIA
        for t2 in cls.TIER_2_DOMAINS:
            if domain.endswith("." + t2):
                return DomainTier.TIER_2_REPUTABLE_MEDIA

        return DomainTier.TIER_3_GENERAL

    @classmethod
    def get_multiplier(cls, domain_or_url: str) -> float:
        tier = cls.classify_domain(domain_or_url)
        return cls.TIER_MULTIPLIERS.get(tier, 1.0)

    @classmethod
    def get_tier_and_multiplier(cls, domain_or_url: str) -> tuple[DomainTier, float]:
        tier = cls.classify_domain(domain_or_url)
        return tier, cls.TIER_MULTIPLIERS.get(tier, 1.0)

    @classmethod
    def get_recommended_domains_for_intent(cls, intent: QueryIntent | str) -> list[str]:
        val = getattr(intent, "value", str(intent))
        if val == "medical_health":
            return ["pubmed.ncbi.nlm.nih.gov", "nih.gov", "who.int", "thelancet.com"]
        if val == "technical_code":
            return ["docs.python.org", "developer.mozilla.org", "pkg.go.dev", "github.com"]
        if val == "legal_policy":
            return ["europa.eu", "sec.gov", "india.gov.in"]
        if val == "current_news":
            return ["reuters.com", "apnews.com", "thehindu.com", "asomiyapratidin.in"]
        return ["en.wikipedia.org", "britannica.com"]
