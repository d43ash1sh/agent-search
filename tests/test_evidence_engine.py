from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from starlette.testclient import TestClient

from services.api.routes import create_api_router
from services.common.models import CleanDocument, PassageChunk, SearchHit
from services.discovery.tier_registry import DomainTier, DomainTierRegistry
from services.indexer.base import AbstractSearchIndex
from services.indexer.sqlite_index import SQLiteSearchIndex
from services.mcp.server import mcp
from services.queue.memory_broker import InMemoryQueueBroker
from services.search.autonomous_engine import AutonomousSearchEngine
from services.search.decomposer import QueryDecomposer, QueryIntent
from services.search.synthesizer import (
    ConsensusLevel,
    EvidenceSynthesizer,
)


class TestEvidenceEngine:
    """Comprehensive test suite for the consensus-grade Evidence Engine."""

    def test_query_decomposer_intent_classification(self) -> None:
        decomposer = QueryDecomposer()

        # Medical / Health
        res_med = decomposer.decompose("does creatine supplementation cause hair loss in athletes?")
        assert res_med.intent == QueryIntent.MEDICAL_HEALTH
        assert any(sq.angle == "clinical_evidence" for sq in res_med.sub_queries)
        assert any(sq.angle == "biological_mechanism" for sq in res_med.sub_queries)

        # Comparative
        res_comp = decomposer.decompose("FastAPI vs Gin performance benchmark")
        assert res_comp.intent == QueryIntent.COMPARATIVE_ANALYSIS
        assert any(sq.angle == "comparative_overview" for sq in res_comp.sub_queries)
        assert any(sq.angle == "subject_a_evidence" for sq in res_comp.sub_queries)

        # Technical / Code
        res_tech = decomposer.decompose("python asyncio concurrency event loop microservices")
        assert res_tech.intent == QueryIntent.TECHNICAL_CODE
        assert any(sq.angle == "specification" for sq in res_tech.sub_queries)

        # Legal / Policy
        res_legal = decomposer.decompose("EU AI Act compliance obligations and high-risk regulation")
        assert res_legal.intent == QueryIntent.LEGAL_POLICY
        assert any(sq.angle == "statutory_text" for sq in res_legal.sub_queries)

        # Current News / Regional
        res_news = decomposer.decompose("Himanta Biswa Sarma latest announcement 2026")
        assert res_news.intent == QueryIntent.CURRENT_NEWS
        assert any(sq.angle == "official_record" for sq in res_news.sub_queries)

        # General Knowledge
        res_gen = decomposer.decompose("Dr. Bhupen Hazarika biography and musical legacy")
        assert res_gen.intent == QueryIntent.GENERAL_KNOWLEDGE
        assert len(res_gen.sub_queries) >= 2

    def test_domain_tier_registry_classification_and_multipliers(self) -> None:
        # Tier 1 Primary: Government & Academic & Official Specifications
        assert (
            DomainTierRegistry.classify_domain("https://pubmed.ncbi.nlm.nih.gov/12345")
            == DomainTier.TIER_1_PRIMARY
        )
        assert DomainTierRegistry.classify_domain("arxiv.org") == DomainTier.TIER_1_PRIMARY
        assert DomainTierRegistry.classify_domain("assam.gov.in") == DomainTier.TIER_1_PRIMARY
        assert DomainTierRegistry.classify_domain("docs.python.org") == DomainTier.TIER_1_PRIMARY
        assert (
            DomainTierRegistry.classify_domain("https://oxford.ac.uk/research") == DomainTier.TIER_1_PRIMARY
        )
        assert DomainTierRegistry.get_multiplier("nih.gov") == 1.50

        # Tier 2 Reputable Media & References
        assert (
            DomainTierRegistry.classify_domain("https://www.reuters.com/world")
            == DomainTier.TIER_2_REPUTABLE_MEDIA
        )
        assert DomainTierRegistry.classify_domain("asomiyapratidin.in") == DomainTier.TIER_2_REPUTABLE_MEDIA
        assert DomainTierRegistry.classify_domain("en.wikipedia.org") == DomainTier.TIER_2_REPUTABLE_MEDIA
        assert DomainTierRegistry.classify_domain("thehindu.com") == DomainTier.TIER_2_REPUTABLE_MEDIA
        assert DomainTierRegistry.get_multiplier("reuters.com") == 1.25

        # Tier 3 General Web
        assert DomainTierRegistry.classify_domain("techblog.example.org") == DomainTier.TIER_3_GENERAL
        assert DomainTierRegistry.get_multiplier("techblog.example.org") == 1.00

        # Tier 4 Low Signal
        assert DomainTierRegistry.classify_domain("quora.com") == DomainTier.TIER_4_LOW_SIGNAL
        assert DomainTierRegistry.classify_domain("buzzfeed.com") == DomainTier.TIER_4_LOW_SIGNAL
        assert DomainTierRegistry.get_multiplier("buzzfeed.com") == 0.50

        # Intent recommendations
        rec_med = DomainTierRegistry.get_recommended_domains_for_intent(QueryIntent.MEDICAL_HEALTH)
        assert "nih.gov" in rec_med or "pubmed.ncbi.nlm.nih.gov" in rec_med

    def test_evidence_synthesizer_cross_document_corroboration(self) -> None:
        synthesizer = EvidenceSynthesizer()

        hits = [
            SearchHit(
                evidence_id="hit-nih-1",
                url="https://pubmed.ncbi.nlm.nih.gov/3001/creatine-study",
                canonical_url="https://pubmed.ncbi.nlm.nih.gov/3001/creatine-study",
                title="Creatine supplementation and dihydrotestosterone: clinical review",
                text="Creatine supplementation does not directly cause hair loss in healthy adults. Multiple randomized clinical trials show no significant increase in systemic dihydrotestosterone.",
                score=0.95,
                estimated_tokens=30,
            ),
            SearchHit(
                evidence_id="hit-nature-2",
                url="https://www.nature.com/articles/d41586-creatine-meta",
                canonical_url="https://www.nature.com/articles/d41586-creatine-meta",
                title="Systematic Meta-analysis on Creatine Safety",
                text="Extensive meta-analyses confirm creatine supplementation does not cause hair loss or accelerate male pattern baldness. Evidence shows favorable safety profiles.",
                score=0.91,
                estimated_tokens=28,
            ),
            SearchHit(
                evidence_id="hit-reuters-3",
                url="https://www.reuters.com/sports/nutrition-science-update",
                canonical_url="https://www.reuters.com/sports/nutrition-science-update",
                title="Sports Science Review on Supplements",
                text="Nutritionists emphasize that creatine supplementation does not produce baldness, contrary to anecdotal gym myths.",
                score=0.88,
                estimated_tokens=25,
            ),
        ]

        result = synthesizer.synthesize(
            query="Does creatine cause hair loss?",
            hits=hits,
            intent=QueryIntent.MEDICAL_HEALTH,
        )

        assert result.consensus_level == ConsensusLevel.HIGH_CONSENSUS
        assert result.source_diversity_count == 3
        assert result.top_tier_ratio >= 0.60
        assert len(result.claims) > 0
        assert len(result.key_findings) > 0
        assert "[^1]" in result.synthesis_markdown
        # Corroborating domains identified
        corroborated_claims = [c for c in result.claims if len(c.corroborating_domains) > 0]
        assert len(corroborated_claims) > 0

    def test_evidence_synthesizer_detects_caveats_and_contested_assertions(self) -> None:
        synthesizer = EvidenceSynthesizer()

        hits = [
            SearchHit(
                evidence_id="hit-blog-1",
                url="https://unverified-supplement-review.com/creatine",
                canonical_url="https://unverified-supplement-review.com/creatine",
                title="Gym Blog",
                text="Creatine might increase hair loss. However anecdotal reports indicate conflicting and mixed results among bodybuilders.",
                score=0.75,
                estimated_tokens=22,
            )
        ]

        result = synthesizer.synthesize(
            query="creatine hair loss",
            hits=hits,
            intent=QueryIntent.MEDICAL_HEALTH,
        )

        assert result.consensus_level == ConsensusLevel.CONTESTED
        assert len(result.contradictions_or_caveats) > 0

    def test_evidence_synthesizer_empty_hits(self) -> None:
        synthesizer = EvidenceSynthesizer()
        result = synthesizer.synthesize(query="nonexistent term", hits=[])
        assert result.consensus_level == ConsensusLevel.INSUFFICIENT_EVIDENCE
        assert result.source_diversity_count == 0

    @pytest.mark.anyio
    async def test_autonomous_engine_search_evidence_end_to_end(self, tmp_path: Path) -> None:
        db_path = tmp_path / "evidence_test.db"
        index = SQLiteSearchIndex(database_path=str(db_path))

        # Seed 2 independent high-tier sources into index
        doc_nih = CleanDocument(
            url="https://pubmed.ncbi.nlm.nih.gov/8892/fastapi-concurrency",
            canonical_url="https://pubmed.ncbi.nlm.nih.gov/8892/fastapi-concurrency",
            title="Asynchronous Web Microservices Benchmarks",
            clean_text="FastAPI leverages Starlette and Pydantic for high throughput asynchronous Python APIs. Benchmark studies demonstrate sub-millisecond latencies under concurrent loads.",
            passages=[
                PassageChunk(
                    passage_id="pass_nih_1",
                    document_url="https://pubmed.ncbi.nlm.nih.gov/8892/fastapi-concurrency",
                    order_index=0,
                    start_char=0,
                    end_char=170,
                    text="FastAPI leverages Starlette and Pydantic for high throughput asynchronous Python APIs. Benchmark studies demonstrate sub-millisecond latencies under concurrent loads.",
                    estimated_tokens=30,
                    content_hash="hash_nih_1",
                )
            ],
        )
        doc_python = CleanDocument(
            url="https://docs.python.org/3/library/asyncio.html",
            canonical_url="https://docs.python.org/3/library/asyncio.html",
            title="Asyncio Documentation",
            clean_text="Asyncio is a library to write concurrent code using the async/await syntax. It serves as the foundation for asynchronous frameworks including FastAPI.",
            passages=[
                PassageChunk(
                    passage_id="pass_py_1",
                    document_url="https://docs.python.org/3/library/asyncio.html",
                    order_index=0,
                    start_char=0,
                    end_char=165,
                    text="Asyncio is a library to write concurrent code using the async/await syntax. It serves as the foundation for asynchronous frameworks including FastAPI.",
                    estimated_tokens=28,
                    content_hash="hash_py_1",
                )
            ],
        )
        await index.index_document(doc_nih)
        await index.index_document(doc_python)

        engine = AutonomousSearchEngine(index=index)
        resp = await engine.search_evidence("FastAPI async performance", limit=5)

        assert resp.decomposition is not None
        assert resp.decomposition.intent in (QueryIntent.TECHNICAL_CODE, QueryIntent.COMPARATIVE_ANALYSIS)
        assert resp.synthesis is not None
        assert resp.synthesis.consensus_level in (
            ConsensusLevel.HIGH_CONSENSUS,
            ConsensusLevel.MODERATE_EVIDENCE,
        )
        assert len(resp.results) >= 2
        assert len(resp.synthesis.key_findings) > 0
        assert "[^1]" in resp.citations_markdown

    def test_api_search_evidence_endpoint(self, tmp_path: Path) -> None:
        db_path = tmp_path / "api_evidence.db"
        index = SQLiteSearchIndex(database_path=str(db_path))
        broker = InMemoryQueueBroker()
        router = create_api_router(broker=broker, index=index)

        from fastapi import FastAPI

        app = FastAPI()
        app.include_router(router)
        client = TestClient(app)

        # 1. Evidence endpoint call
        resp = client.post(
            "/v1/search/evidence",
            json={"query": "Does creatine cause hair loss?", "limit": 3, "allow_discovery": False},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "synthesis" in data
        assert "decomposition" in data
        assert data["query"] == "Does creatine cause hair loss?"

        # 2. Autonomous search with decompose=True and synthesize=True
        resp_auto = client.post(
            "/v1/search/autonomous",
            json={
                "query": "FastAPI vs Gin performance",
                "limit": 3,
                "allow_discovery": False,
                "decompose": True,
                "synthesize": True,
            },
        )
        assert resp_auto.status_code == 200
        data_auto = resp_auto.json()
        assert data_auto["decomposition"] is not None
        assert data_auto["synthesis"] is not None

    @pytest.mark.anyio
    async def test_mcp_synthesize_evidence_tool(self) -> None:
        # Check MCP registered tools
        tools = await mcp.list_tools()
        tool_names = [t.name for t in tools]
        assert "synthesize_evidence" in tool_names

        # Execute MCP tool
        with patch("services.mcp.server.get_search_index") as mock_get_idx:
            mock_idx = AsyncMock(spec=AbstractSearchIndex)
            mock_idx.search.return_value = ([], 0)
            mock_idx.get_domain_authority.return_value = 1.0
            mock_get_idx.return_value = mock_idx

            res = await mcp.call_tool(
                "synthesize_evidence",
                {"query": "EU AI Act compliance requirements", "limit": 3, "allow_discovery": False},
            )
            content = getattr(res, "content", [])
            assert len(content) > 0
            payload = json.loads(content[0].text)
            assert payload["query"] == "EU AI Act compliance requirements"
            assert "synthesis" in payload
            assert payload["synthesis"]["intent"] == QueryIntent.LEGAL_POLICY
