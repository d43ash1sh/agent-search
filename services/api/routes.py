from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from services.common.models import FetchRequest, SearchHit
from services.indexer.base import AbstractSearchIndex
from services.queue.base import AbstractQueueBroker

router = APIRouter()


class SearchRequest(BaseModel):
    query: str
    limit: int = 10
    tokens: int = 1200
    domains: list[str] = Field(default_factory=list)


class SearchResponse(BaseModel):
    query: str
    estimated_tokens: int
    max_tokens: int
    results: list[SearchHit]


class EnqueueCrawlRequest(BaseModel):
    urls: list[str]
    priority: int = 0
    render_js: bool = False


def create_api_router(broker: AbstractQueueBroker, index: AbstractSearchIndex) -> APIRouter:
    api = APIRouter()

    @api.post("/v1/search", response_model=SearchResponse)
    async def search(req: SearchRequest) -> SearchResponse:
        hits, total_tokens = await index.search(
            query=req.query,
            limit=req.limit,
            max_tokens=req.tokens,
            domains=req.domains or None,
        )
        return SearchResponse(
            query=req.query,
            estimated_tokens=total_tokens,
            max_tokens=req.tokens,
            results=hits,
        )

    @api.post("/v1/crawl")
    async def enqueue_crawl(req: EnqueueCrawlRequest) -> dict[str, Any]:
        enqueued = 0
        for url in req.urls:
            url = url.strip()
            if url:
                await broker.enqueue_fetch(
                    FetchRequest(
                        url=url,
                        priority=req.priority,
                        render_js=req.render_js,
                    )
                )
                enqueued += 1
        return {"status": "ok", "enqueued": enqueued}

    @api.get("/v1/queue/status")
    async def get_queue_status() -> dict[str, Any]:
        depths = await broker.get_queue_depths()
        return {"queues": depths}

    @api.get("/v1/stats")
    async def get_stats() -> dict[str, Any]:
        return await index.get_stats()

    return api
