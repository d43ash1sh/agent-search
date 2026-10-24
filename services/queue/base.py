from __future__ import annotations

from abc import ABC, abstractmethod

from services.common.models import CleanDocument, FailedFetchRecord, FetchRequest, RawPageRecord


class AbstractQueueBroker(ABC):
    """Abstract interface decoupling producers and consumers across services."""

    @abstractmethod
    async def enqueue_fetch(self, request: FetchRequest) -> None:
        """Push a URL to the fetch queue."""

    @abstractmethod
    async def dequeue_fetch(self, timeout: float = 1.0) -> FetchRequest | None:
        """Pop a URL to fetch."""

    @abstractmethod
    async def enqueue_raw(self, record: RawPageRecord) -> None:
        """Push raw fetched HTML payload to staging queue."""

    @abstractmethod
    async def dequeue_raw(self, timeout: float = 1.0) -> RawPageRecord | None:
        """Pop raw fetched HTML payload for extraction."""

    @abstractmethod
    async def enqueue_document(self, doc: CleanDocument) -> None:
        """Push clean, extracted document to the indexer queue."""

    @abstractmethod
    async def dequeue_document(self, timeout: float = 1.0) -> CleanDocument | None:
        """Pop clean document for search indexing."""

    @abstractmethod
    async def route_to_dlq(self, failed: FailedFetchRecord) -> None:
        """Route failed fetches to Dead Letter Queue (DLQ) without crashing the pipeline."""

    @abstractmethod
    async def get_queue_depths(self) -> dict[str, int]:
        """Return counts of queued items in all stages."""

    async def ping(self) -> bool:
        """Check connectivity to broker."""
        return True

    async def close(self) -> None:
        """Close connections to broker."""
        return None
