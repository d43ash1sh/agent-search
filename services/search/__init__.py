"""Live web search and SERP discovery subsystem."""

from services.search.autonomous_engine import AutonomousSearchEngine, AutonomousSearchResponse
from services.search.decomposer import QueryDecomposer, QueryDecomposition, QueryIntent, SubQuery
from services.search.live_engine import LiveSearchEngine, LiveSearchResponse
from services.search.serp_client import DuckDuckGoSerpClient
from services.search.synthesizer import (
    ConsensusLevel,
    EvidenceClaim,
    EvidenceSource,
    EvidenceSynthesisResult,
    EvidenceSynthesizer,
)

__all__ = [
    "AutonomousSearchEngine",
    "AutonomousSearchResponse",
    "ConsensusLevel",
    "DuckDuckGoSerpClient",
    "EvidenceClaim",
    "EvidenceSource",
    "EvidenceSynthesisResult",
    "EvidenceSynthesizer",
    "LiveSearchEngine",
    "LiveSearchResponse",
    "QueryDecomposer",
    "QueryDecomposition",
    "QueryIntent",
    "SubQuery",
]
