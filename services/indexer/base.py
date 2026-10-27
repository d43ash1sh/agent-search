from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from services.common.models import CleanDocument, SearchHit


class AbstractSearchIndex(ABC):
    """Abstract interface for storage and search backends."""

    @abstractmethod
    async def index_document(self, doc: CleanDocument) -> None:
        """Persist clean document and passages into the search index."""

    @abstractmethod
    async def search(
        self,
        query: str,
        limit: int = 10,
        max_tokens: int = 1200,
        domains: list[str] | None = None,
        hybrid: bool = False,
    ) -> tuple[list[SearchHit], int]:
        """Execute search query returning ranked hits and total token estimate."""

    @abstractmethod
    async def get_evidence(self, evidence_id: str) -> SearchHit | None:
        """Retrieve single evidence passage by ID."""

    @abstractmethod
    async def get_stats(self) -> dict[str, Any]:
        """Return index health metrics and document counts."""

    async def record_frontier_urls(self, records: list[dict[str, Any]]) -> int:
        """Record newly discovered URLs into the frontier ledger."""
        return 0

    async def update_frontier_status(self, url: str, status: str) -> None:
        """Update crawl status of a URL in the frontier ledger."""
        return None

    async def get_frontier_stats(self) -> dict[str, Any]:
        """Return summary metrics for the URL frontier ledger."""
        return {}

    async def get_frontier_urls(
        self,
        status: str | None = None,
        domain: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Retrieve recorded URLs from the frontier ledger."""
        return []

    async def get_pending_frontier_urls(
        self, limit: int = 50, max_per_domain: int = 10
    ) -> list[dict[str, Any]]:
        """Retrieve pending discovered URLs partitioned by domain."""
        return []

    async def record_link_edges(self, edges: list[dict[str, Any]]) -> int:
        """Record directed link graph edges for PageRank calculation."""
        return 0

    async def get_domain_authority(self, domain: str) -> float:
        """Get PageRank authority score for a domain (default 1.0)."""
        return 1.0

    async def update_domain_authorities(self, authorities: dict[str, float]) -> None:
        """Persist calculated PageRank authority scores."""
        return None

    async def ping(self) -> bool:
        """Check connectivity to storage backend."""
        return True

    async def close(self) -> None:
        """Close connection pools and storage resources."""
        return None
