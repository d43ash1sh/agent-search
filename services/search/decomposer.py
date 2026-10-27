from __future__ import annotations

import re
from enum import StrEnum
from typing import ClassVar

from pydantic import BaseModel, Field


class QueryIntent(StrEnum):
    MEDICAL_HEALTH = "medical_health"
    TECHNICAL_CODE = "technical_code"
    LEGAL_POLICY = "legal_policy"
    CURRENT_NEWS = "current_news"
    COMPARATIVE_ANALYSIS = "comparative_analysis"
    GENERAL_KNOWLEDGE = "general_knowledge"


class SubQuery(BaseModel):
    query: str
    angle: str
    target_tiers: list[str] = Field(default_factory=list)


class QueryDecomposition(BaseModel):
    original_query: str
    intent: QueryIntent
    confidence: float = 1.0
    sub_queries: list[SubQuery] = Field(default_factory=list)
    primary_domain_types: list[str] = Field(default_factory=list)
    requires_consensus_check: bool = True


class QueryDecomposer:
    """Decomposes diverse complex user queries (medical, technical, legal, news, comparative)

    into orthogonal, high-signal retrieval angles with intent classification and tier routing.
    """

    COMPARATIVE_PATTERNS: ClassVar[list[re.Pattern[str]]] = [
        re.compile(
            r"\b(?:vs\.?|versus|compare|difference\s+between|which\s+is\s+better|pros\s+and\s+cons)\b",
            re.IGNORECASE,
        ),
        re.compile(r"^(.+?)\s+vs\.?\s+(.+?)$", re.IGNORECASE),
    ]

    MEDICAL_PATTERNS: ClassVar[list[re.Pattern[str]]] = [
        re.compile(
            r"\b(?:health|medical|disease|symptom|symptoms|drug|dosage|treatment|clinical|trial|trials|"
            r"hair\s+loss|creatine|vaccine|efficacy|medicine|cancer|supplement|protein|therapy|syndrome|"
            r"blood\s+pressure|diabetes|cardiovascular|side\s+effects?|toxicity|pharmacology|pathology)\b",
            re.IGNORECASE,
        ),
    ]

    TECHNICAL_PATTERNS: ClassVar[list[re.Pattern[str]]] = [
        re.compile(
            r"\b(?:python|rust|golang|fastapi|docker|kubernetes|postgres|pgvector|redis|api|sdk|cli|"
            r"asyncio|threading|benchmark|latency|throughput|concurrency|memory\s+safety|compile|"
            r"compiler|error|exception|debug|algorithm|function|microservices|architecture|git|orm)\b",
            re.IGNORECASE,
        ),
    ]

    LEGAL_PATTERNS: ClassVar[list[re.Pattern[str]]] = [
        re.compile(
            r"\b(?:law|act|statute|statutory|regulation|directive|gdpr|compliance|legal|court|ruling|"
            r"treaty|policy|bill|section|article|liability|copyright|patent|intellectual\s+property|"
            r"jurisdiction|enforcement|sanction)\b",
            re.IGNORECASE,
        ),
    ]

    NEWS_PATTERNS: ClassVar[list[re.Pattern[str]]] = [
        re.compile(
            r"\b(?:latest|breaking|news|today|yesterday|minister|chief\s+minister|election|elections|"
            r"announced|announcement|cabinet|parliament|assembly|press\s+release|scandal|resigned|2026|"
            r"himanta|assam|bjp|congress|government\s+decision)\b",
            re.IGNORECASE,
        ),
    ]

    def detect_intent(self, query: str) -> QueryIntent:
        clean = query.strip()
        # 1. Comparative check first (e.g., "FastAPI vs Gin", "Creatine vs Protein")
        for pat in self.COMPARATIVE_PATTERNS:
            if pat.search(clean):
                return QueryIntent.COMPARATIVE_ANALYSIS

        # 2. Medical / Health
        for pat in self.MEDICAL_PATTERNS:
            if pat.search(clean):
                return QueryIntent.MEDICAL_HEALTH

        # 3. Technical / Code
        for pat in self.TECHNICAL_PATTERNS:
            if pat.search(clean):
                return QueryIntent.TECHNICAL_CODE

        # 4. Legal / Policy
        for pat in self.LEGAL_PATTERNS:
            if pat.search(clean):
                return QueryIntent.LEGAL_POLICY

        # 5. Current News / Regional
        for pat in self.NEWS_PATTERNS:
            if pat.search(clean):
                return QueryIntent.CURRENT_NEWS

        return QueryIntent.GENERAL_KNOWLEDGE

    def decompose(self, query: str) -> QueryDecomposition:
        clean = query.strip()
        intent = self.detect_intent(clean)
        sub_queries: list[SubQuery] = []
        domain_types: list[str] = []

        if intent == QueryIntent.COMPARATIVE_ANALYSIS:
            # Extract comparative entities if possible
            match = re.search(r"^(.*?)\s+(?:vs\.?|versus)\s+(.*?)$", clean, re.IGNORECASE)
            entity_a = match.group(1).strip() if match else "Subject A"
            entity_b = match.group(2).strip() if match else "Subject B"

            sub_queries = [
                SubQuery(
                    query=f"{clean} architectural comparison trade-offs",
                    angle="comparative_overview",
                    target_tiers=["TIER_1_PRIMARY", "TIER_2_REPUTABLE_MEDIA"],
                ),
                SubQuery(
                    query=f"{entity_a} empirical benchmarks performance and use cases",
                    angle="subject_a_evidence",
                    target_tiers=["TIER_1_PRIMARY", "TIER_2_REPUTABLE_MEDIA"],
                ),
                SubQuery(
                    query=f"{entity_b} empirical benchmarks performance and use cases",
                    angle="subject_b_evidence",
                    target_tiers=["TIER_1_PRIMARY", "TIER_2_REPUTABLE_MEDIA"],
                ),
            ]
            domain_types = ["academic_benchmarks", "official_docs", "curated_analysis"]

        elif intent == QueryIntent.MEDICAL_HEALTH:
            sub_queries = [
                SubQuery(
                    query=f"{clean} clinical trials and empirical evidence",
                    angle="clinical_evidence",
                    target_tiers=["TIER_1_PRIMARY"],
                ),
                SubQuery(
                    query=f"{clean} biological mechanism and side effects",
                    angle="biological_mechanism",
                    target_tiers=["TIER_1_PRIMARY"],
                ),
                SubQuery(
                    query=f"{clean} systematic review meta-analysis consensus",
                    angle="scientific_consensus",
                    target_tiers=["TIER_1_PRIMARY", "TIER_2_REPUTABLE_MEDIA"],
                ),
            ]
            domain_types = ["pubmed", "nih_gov", "clinical_trials", "academic_journals"]

        elif intent == QueryIntent.TECHNICAL_CODE:
            sub_queries = [
                SubQuery(
                    query=f"{clean} official documentation and specification",
                    angle="specification",
                    target_tiers=["TIER_1_PRIMARY"],
                ),
                SubQuery(
                    query=f"{clean} performance benchmarks architecture and best practices",
                    angle="benchmarks_and_design",
                    target_tiers=["TIER_1_PRIMARY", "TIER_2_REPUTABLE_MEDIA"],
                ),
                SubQuery(
                    query=f"{clean} trade-offs limitations and edge cases",
                    angle="limitations",
                    target_tiers=["TIER_1_PRIMARY", "TIER_2_REPUTABLE_MEDIA"],
                ),
            ]
            domain_types = ["official_documentation", "github_repositories", "engineering_blogs"]

        elif intent == QueryIntent.LEGAL_POLICY:
            sub_queries = [
                SubQuery(
                    query=f"{clean} statutory text official provisions and scope",
                    angle="statutory_text",
                    target_tiers=["TIER_1_PRIMARY"],
                ),
                SubQuery(
                    query=f"{clean} regulatory guidelines compliance and enforcement",
                    angle="compliance_framework",
                    target_tiers=["TIER_1_PRIMARY", "TIER_2_REPUTABLE_MEDIA"],
                ),
                SubQuery(
                    query=f"{clean} legal analysis judicial precedent and implications",
                    angle="legal_implications",
                    target_tiers=["TIER_1_PRIMARY", "TIER_2_REPUTABLE_MEDIA"],
                ),
            ]
            domain_types = ["government_portals", "statutory_registers", "legal_reviews"]

        elif intent == QueryIntent.CURRENT_NEWS:
            sub_queries = [
                SubQuery(
                    query=f"{clean} official announcement press release government record",
                    angle="official_record",
                    target_tiers=["TIER_1_PRIMARY", "TIER_2_REPUTABLE_MEDIA"],
                ),
                SubQuery(
                    query=f"{clean} latest news reporting and coverage",
                    angle="media_coverage",
                    target_tiers=["TIER_2_REPUTABLE_MEDIA"],
                ),
                SubQuery(
                    query=f"{clean} background context and analysis",
                    angle="contextual_analysis",
                    target_tiers=["TIER_2_REPUTABLE_MEDIA", "TIER_3_GENERAL"],
                ),
            ]
            domain_types = ["government_press", "reputable_wire_services", "regional_dailies"]

        else:  # GENERAL_KNOWLEDGE
            sub_queries = [
                SubQuery(
                    query=f"{clean} overview biography facts",
                    angle="overview",
                    target_tiers=["TIER_1_PRIMARY", "TIER_2_REPUTABLE_MEDIA"],
                ),
                SubQuery(
                    query=f"{clean} key works contributions history",
                    angle="contributions",
                    target_tiers=["TIER_2_REPUTABLE_MEDIA"],
                ),
                SubQuery(
                    query=f"{clean} verified records and references",
                    angle="verification",
                    target_tiers=["TIER_1_PRIMARY", "TIER_2_REPUTABLE_MEDIA"],
                ),
            ]
            domain_types = ["encyclopedic_records", "peer_reviewed_references", "verified_archives"]

        return QueryDecomposition(
            original_query=clean,
            intent=intent,
            confidence=0.95,
            sub_queries=sub_queries,
            primary_domain_types=domain_types,
            requires_consensus_check=True,
        )
