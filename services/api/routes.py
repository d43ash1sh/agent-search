from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from services.common.citations import CitationFormatter
from services.common.models import FetchRequest, SearchHit
from services.indexer.base import AbstractSearchIndex
from services.indexer.reranker import NeuralReranker
from services.queue.base import AbstractQueueBroker

router = APIRouter()


class SearchRequest(BaseModel):
    query: str
    limit: int = 10
    tokens: int = 1200
    domains: list[str] = Field(default_factory=list)
    rerank: bool = False


class SearchResponse(BaseModel):
    query: str
    estimated_tokens: int
    max_tokens: int
    results: list[SearchHit]
    citations_markdown: str = ""


class GroundedSearchResponse(BaseModel):
    query: str
    estimated_tokens: int
    results: list[SearchHit]
    markdown_citations: str
    xml_grounding: str
    prompt_context: str


class EnqueueCrawlRequest(BaseModel):
    urls: list[str]
    priority: int = 0
    render_js: bool = False


def create_api_router(broker: AbstractQueueBroker, index: AbstractSearchIndex) -> APIRouter:
    api = APIRouter()
    reranker = NeuralReranker()

    @api.post("/v1/search", response_model=SearchResponse)
    async def search(req: SearchRequest) -> SearchResponse:
        hits, total_tokens = await index.search(
            query=req.query,
            limit=req.limit,
            max_tokens=req.tokens,
            domains=req.domains or None,
        )

        if req.rerank and hits:
            hits, total_tokens = reranker.rerank(
                query=req.query,
                hits=hits,
                max_tokens=req.tokens,
            )

        citations = CitationFormatter.format_markdown(hits)
        return SearchResponse(
            query=req.query,
            estimated_tokens=total_tokens,
            max_tokens=req.tokens,
            results=hits,
            citations_markdown=citations,
        )

    @api.post("/v1/search/grounded", response_model=GroundedSearchResponse)
    async def search_grounded(req: SearchRequest) -> GroundedSearchResponse:
        hits, total_tokens = await index.search(
            query=req.query,
            limit=req.limit,
            max_tokens=req.tokens,
            domains=req.domains or None,
        )

        if req.rerank and hits:
            hits, total_tokens = reranker.rerank(
                query=req.query,
                hits=hits,
                max_tokens=req.tokens,
            )

        md = CitationFormatter.format_markdown(hits)
        xml = CitationFormatter.format_xml(hits)
        prompt_ctx = CitationFormatter.format_agent_prompt_context(hits, req.query)

        return GroundedSearchResponse(
            query=req.query,
            estimated_tokens=total_tokens,
            results=hits,
            markdown_citations=md,
            xml_grounding=xml,
            prompt_context=prompt_ctx,
        )

    @api.get("/v1/search/stream")
    async def search_stream(
        query: str,
        limit: int = 10,
        tokens: int = 1200,
        rerank: bool = False,
    ) -> StreamingResponse:
        """Stream search results progressively via Server-Sent Events (SSE)."""

        async def event_generator() -> AsyncIterator[str]:
            hits, _ = await index.search(
                query=query,
                limit=limit,
                max_tokens=tokens,
            )
            if rerank and hits:
                hits, _ = reranker.rerank(query=query, hits=hits, max_tokens=tokens)

            yield f"event: start\ndata: {json.dumps({'query': query, 'total_candidates': len(hits)})}\n\n"
            for hit in hits:
                await asyncio.sleep(0.01)
                yield f"event: evidence\ndata: {hit.model_dump_json()}\n\n"
            yield f"event: done\ndata: {json.dumps({'status': 'completed'})}\n\n"

        return StreamingResponse(event_generator(), media_type="text/event-stream")

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
