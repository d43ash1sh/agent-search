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
    ) -> tuple[list[SearchHit], int]:
        """Execute search query returning ranked hits and total token estimate."""

    @abstractmethod
    async def get_evidence(self, evidence_id: str) -> SearchHit | None:
        """Retrieve single evidence passage by ID."""

    @abstractmethod
    async def get_stats(self) -> dict[str, Any]:
        """Return index health metrics and document counts."""
