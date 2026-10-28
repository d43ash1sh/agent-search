"""Live web search and SERP discovery subsystem."""

from services.search.autonomous_engine import AutonomousSearchEngine, AutonomousSearchResponse
from services.search.decomposer import QueryDecomposer, QueryDecomposition, QueryIntent, SubQuery
from services.search.live_engine import LiveSearchEngine, LiveSearchResponse
from services.search.serp_client import (
    BingSearchClient,
    BraveSearchClient,
    DuckDuckGoSerpClient,
    FederatedSerpClient,
    SearXNGSearchClient,
    SerpResultItem,
)
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
    "BingSearchClient",
    "BraveSearchClient",
    "ConsensusLevel",
    "DuckDuckGoSerpClient",
    "EvidenceClaim",
    "EvidenceSource",
    "EvidenceSynthesisResult",
    "EvidenceSynthesizer",
    "FederatedSerpClient",
    "LiveSearchEngine",
    "LiveSearchResponse",
    "QueryDecomposer",
    "QueryDecomposition",
    "QueryIntent",
    "SearXNGSearchClient",
    "SerpResultItem",
    "SubQuery",
]
