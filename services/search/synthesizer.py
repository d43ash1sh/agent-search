from __future__ import annotations

import re
import urllib.parse
from enum import StrEnum
from typing import ClassVar

from pydantic import BaseModel, Field

from services.common.models import SearchHit
from services.discovery.tier_registry import DomainTier, DomainTierRegistry
from services.search.decomposer import QueryIntent


class ConsensusLevel(StrEnum):
    HIGH_CONSENSUS = "HIGH_CONSENSUS"
    MODERATE_EVIDENCE = "MODERATE_EVIDENCE"
    CONTESTED = "CONTESTED"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"


class EvidenceSource(BaseModel):
    source_id: int
    title: str
    url: str
    canonical_url: str
    domain: str
    tier: DomainTier
    score: float


class EvidenceClaim(BaseModel):
    claim_id: str
    statement: str
    source_url: str
    source_domain: str
    source_title: str
    authority_tier: DomainTier
    relevance_score: float
    passage_snippet: str
    evidence_id: str
    corroborating_domains: list[str] = Field(default_factory=list)


class EvidenceSynthesisResult(BaseModel):
    query: str
    intent: QueryIntent
    consensus_level: ConsensusLevel
    summary: str
    key_findings: list[str] = Field(default_factory=list)
    claims: list[EvidenceClaim] = Field(default_factory=list)
    contradictions_or_caveats: list[str] = Field(default_factory=list)
    sources: list[EvidenceSource] = Field(default_factory=list)
    source_diversity_count: int = 0
    top_tier_ratio: float = 0.0
    synthesis_markdown: str = ""


class EvidenceSynthesizer:
    """Synthesizes cross-document evidence into verifiable, consensus-rated answers.

    Extracts claims from multi-source retrieval passes, maps citations, checks domain corroboration,
    and flags caveats or contested assertions without hallucinations.
    """

    CONTRADICTION_MARKERS: ClassVar[list[re.Pattern[str]]] = [
        re.compile(
            r"\b(?:however|contrary to|inconclusive|conflicting|mixed results|no direct evidence|no link|debunked|unproven|disputed|caveat)\b",
            re.IGNORECASE,
        ),
        re.compile(
            r"\b(?:fails to show|insufficient data|not statistically significant|unsupported)\b",
            re.IGNORECASE,
        ),
    ]

    def _split_into_sentences(self, text: str) -> list[str]:
        # Split text into meaningful sentences
        raw = re.split(r"(?<=[.!?])\s+", text)
        sentences = []
        for s in raw:
            s_clean = s.strip()
            # Keep reasonably sized complete statements
            if len(s_clean) >= 25 and not s_clean.startswith(("#", "-", "*", ">")):
                sentences.append(s_clean)
        return sentences

    def _find_corroborating_domains(
        self,
        sentence: str,
        current_domain: str,
        domain_to_texts: dict[str, list[str]],
    ) -> list[str]:
        """Check if content words in this sentence appear across other source domains."""
        words = set(re.findall(r"\b[a-zA-Z0-9_\u0980-\u09ff]{4,}\b", sentence.lower()))
        if len(words) < 3:
            return []

        corroborating: list[str] = []
        for dom, other_texts in domain_to_texts.items():
            if dom == current_domain:
                continue
            all_other = " ".join(other_texts).lower()
            matching_words = sum(1 for w in words if w in all_other)
            overlap_ratio = matching_words / len(words)
            if overlap_ratio >= 0.40:
                corroborating.append(dom)

        return corroborating

    def synthesize(
        self,
        query: str,
        hits: list[SearchHit],
        intent: QueryIntent = QueryIntent.GENERAL_KNOWLEDGE,
    ) -> EvidenceSynthesisResult:
        if not hits:
            return EvidenceSynthesisResult(
                query=query,
                intent=intent,
                consensus_level=ConsensusLevel.INSUFFICIENT_EVIDENCE,
                summary=f"No verified index evidence found for query: '{query}'.",
                key_findings=[],
                claims=[],
                contradictions_or_caveats=[],
                sources=[],
                source_diversity_count=0,
                top_tier_ratio=0.0,
                synthesis_markdown=f"### No Evidence Available\nNo indexed sources matched '{query}'.",
            )

        # 1. Map Sources and classify domain tiers
        sources: list[EvidenceSource] = []
        domain_to_hits: dict[str, list[SearchHit]] = {}
        domain_to_texts: dict[str, list[str]] = {}

        for idx, hit in enumerate(hits, start=1):
            dom = (urllib.parse.urlsplit(hit.url).hostname or "").lower()
            tier = DomainTierRegistry.classify_domain(dom)
            sources.append(
                EvidenceSource(
                    source_id=idx,
                    title=hit.title or dom or "Source",
                    url=hit.url,
                    canonical_url=hit.canonical_url,
                    domain=dom,
                    tier=tier,
                    score=hit.score,
                )
            )
            domain_to_hits.setdefault(dom, []).append(hit)
            domain_to_texts.setdefault(dom, []).append(hit.text)

        unique_domains = list(domain_to_hits.keys())
        top_tier_count = sum(
            1 for s in sources if s.tier in (DomainTier.TIER_1_PRIMARY, DomainTier.TIER_2_REPUTABLE_MEDIA)
        )
        top_tier_ratio = round(top_tier_count / max(len(sources), 1), 2)

        # 2. Extract Claims and Caveats
        claims: list[EvidenceClaim] = []
        caveats: list[str] = []
        seen_statements: set[str] = set()

        for idx, hit in enumerate(hits, start=1):
            dom = (urllib.parse.urlsplit(hit.url).hostname or "").lower()
            tier = DomainTierRegistry.classify_domain(dom)
            sentences = self._split_into_sentences(hit.text)

            for s_idx, sent in enumerate(sentences[:3]):
                clean_s = sent.strip()
                if clean_s in seen_statements:
                    continue
                seen_statements.add(clean_s)

                # Check for contradiction / caveat markers
                for marker in self.CONTRADICTION_MARKERS:
                    if marker.search(clean_s):
                        caveats.append(f"{clean_s} (Source [{idx}])")
                        break

                corrob = self._find_corroborating_domains(clean_s, dom, domain_to_texts)

                claims.append(
                    EvidenceClaim(
                        claim_id=f"claim_{idx}_{s_idx + 1}",
                        statement=clean_s,
                        source_url=hit.canonical_url,
                        source_domain=dom,
                        source_title=hit.title or dom,
                        authority_tier=tier,
                        relevance_score=hit.score,
                        passage_snippet=hit.text[:200],
                        evidence_id=hit.evidence_id,
                        corroborating_domains=corrob,
                    )
                )

        # 3. Determine Overall Consensus Level
        has_tier1 = any(s.tier == DomainTier.TIER_1_PRIMARY for s in sources)
        has_multiple_corroborated = any(len(c.corroborating_domains) > 0 for c in claims)

        if len(caveats) > 0 and (not has_tier1 or len(unique_domains) < 2):
            consensus_level = ConsensusLevel.CONTESTED
        elif (has_tier1 and len(unique_domains) >= 2) or (
            len(unique_domains) >= 2 and has_multiple_corroborated
        ):
            consensus_level = ConsensusLevel.HIGH_CONSENSUS
        else:
            consensus_level = ConsensusLevel.MODERATE_EVIDENCE

        # 4. Synthesize Key Findings & Summary
        key_findings: list[str] = []
        # Pick top claims with highest tier and relevance
        sorted_claims = sorted(
            claims,
            key=lambda c: (
                1.5
                if c.authority_tier == DomainTier.TIER_1_PRIMARY
                else (1.2 if c.authority_tier == DomainTier.TIER_2_REPUTABLE_MEDIA else 1.0),
                len(c.corroborating_domains),
                c.relevance_score,
            ),
            reverse=True,
        )

        for c in sorted_claims[:4]:
            # Find matching source id
            matching_source_id = next((s.source_id for s in sources if s.canonical_url == c.source_url), 1)
            key_findings.append(f"{c.statement} [^{matching_source_id}]")

        # Synthesize core summary
        top_sentences = [c.statement for c in sorted_claims[:2]]
        summary = " ".join(top_sentences)
        if not summary:
            summary = hits[0].text[:300] + "..."

        # 5. Build Formatted Synthesis Markdown
        md_lines = [
            f"## Evidence Synthesis: {query}\n",
            f"**Query Intent:** `{intent.value}` | **Consensus Level:** `{consensus_level.value}` | **Domain Diversity:** `{len(unique_domains)} distinct domains` (Top-Tier: `{int(top_tier_ratio * 100)}%`)\n",
            "### Summary",
            f"{summary}\n",
            "### Key Verified Findings",
        ]

        for finding in key_findings:
            md_lines.append(f"- {finding}")

        if caveats:
            md_lines.append("\n### Caveats & Contested Nuances")
            for cav in caveats[:3]:
                md_lines.append(f"> ⚠️ {cav}")

        md_lines.append("\n### Source Footnotes & Authority Breakdown")
        for s in sources:
            md_lines.append(
                f"[^{s.source_id}]: [{s.title}]({s.canonical_url}) — `{s.domain}` (`{s.tier.value}`, Score: {s.score:.3f})"
            )

        synthesis_markdown = "\n".join(md_lines)

        return EvidenceSynthesisResult(
            query=query,
            intent=intent,
            consensus_level=consensus_level,
            summary=summary,
            key_findings=key_findings,
            claims=claims,
            contradictions_or_caveats=caveats,
            sources=sources,
            source_diversity_count=len(unique_domains),
            top_tier_ratio=top_tier_ratio,
            synthesis_markdown=synthesis_markdown,
        )
