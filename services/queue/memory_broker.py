from __future__ import annotations

import asyncio
from pathlib import Path

from services.common.models import CleanDocument, FailedFetchRecord, FetchRequest, RawPageRecord
from services.queue.base import AbstractQueueBroker


class InMemoryQueueBroker(AbstractQueueBroker):
    """In-memory broker using asyncio.Queue with disk DLQ support.

    Perfect for zero-dependency local runs, testing, and CI/CD pipelines.
    """

    def __init__(self, dlq_log_path: str = "data/dlq.jsonl") -> None:
        self.fetch_queue: asyncio.Queue[FetchRequest] = asyncio.Queue()
        self.staging_queue: asyncio.Queue[RawPageRecord] = asyncio.Queue()
        self.index_queue: asyncio.Queue[CleanDocument] = asyncio.Queue()
        self.dlq_records: list[FailedFetchRecord] = []
        self.dlq_log_path = Path(dlq_log_path)

    async def enqueue_fetch(self, request: FetchRequest) -> None:
        await self.fetch_queue.put(request)

    async def dequeue_fetch(self, timeout: float = 1.0) -> FetchRequest | None:
        try:
            return await asyncio.wait_for(self.fetch_queue.get(), timeout=timeout)
        except TimeoutError:
            return None

    async def enqueue_raw(self, record: RawPageRecord) -> None:
        await self.staging_queue.put(record)

    async def dequeue_raw(self, timeout: float = 1.0) -> RawPageRecord | None:
        try:
            return await asyncio.wait_for(self.staging_queue.get(), timeout=timeout)
        except TimeoutError:
            return None

    async def enqueue_document(self, doc: CleanDocument) -> None:
        await self.index_queue.put(doc)

    async def dequeue_document(self, timeout: float = 1.0) -> CleanDocument | None:
        try:
            return await asyncio.wait_for(self.index_queue.get(), timeout=timeout)
        except TimeoutError:
            return None

    async def route_to_dlq(self, failed: FailedFetchRecord) -> None:
        self.dlq_records.append(failed)
        self.dlq_log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.dlq_log_path, "a", encoding="utf-8") as f:
            f.write(failed.model_dump_json() + "\n")

    async def get_queue_depths(self) -> dict[str, int]:
        return {
            "fetch": self.fetch_queue.qsize(),
            "staging": self.staging_queue.qsize(),
            "index": self.index_queue.qsize(),
            "dlq": len(self.dlq_records),
        }

    async def clear_queue(self, queue_name: str | None = None) -> dict[str, int]:
        purged: dict[str, int] = {}
        if queue_name in (None, "fetch"):
            c = self.fetch_queue.qsize()
            while not self.fetch_queue.empty():
                self.fetch_queue.get_nowait()
            purged["fetch"] = c
        if queue_name in (None, "staging"):
            c = self.staging_queue.qsize()
            while not self.staging_queue.empty():
                self.staging_queue.get_nowait()
            purged["staging"] = c
        if queue_name in (None, "index"):
            c = self.index_queue.qsize()
            while not self.index_queue.empty():
                self.index_queue.get_nowait()
            purged["index"] = c
        if queue_name in (None, "dlq"):
            c = len(self.dlq_records)
            self.dlq_records.clear()
            purged["dlq"] = c
        return purged
