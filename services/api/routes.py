from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel, Field

from services.api.dashboard import DASHBOARD_HTML
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
    hybrid: bool = False


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


class RecursiveCrawlRequest(BaseModel):
    url: str
    max_depth: int = 1
    max_pages: int = 50
    render_js: bool = False


class RecursiveCrawlResponse(BaseModel):
    status: str
    crawl_id: str
    seed_url: str
    allowed_domain: str
    max_depth: int
    message: str


class ExtractRequest(BaseModel):
    url: str | None = None
    html: str | None = None
    schema_: dict[str, Any] = Field(alias="schema")


class ExtractResponse(BaseModel):
    status: str
    items_count: int
    data: list[dict[str, Any]]


def create_api_router(broker: AbstractQueueBroker, index: AbstractSearchIndex) -> APIRouter:
    api = APIRouter()
    reranker = NeuralReranker()

    @api.get("/", response_class=HTMLResponse)
    @api.get("/dashboard", response_class=HTMLResponse)
    async def dashboard_page() -> str:
        return DASHBOARD_HTML

    @api.post("/v1/search", response_model=SearchResponse)
    async def search(req: SearchRequest) -> SearchResponse:
        hits, total_tokens = await index.search(
            query=req.query,
            limit=req.limit,
            max_tokens=req.tokens,
            domains=req.domains or None,
            hybrid=req.hybrid,
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
            hybrid=req.hybrid,
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

    @api.post("/v1/crawl/site", response_model=RecursiveCrawlResponse)
    async def enqueue_recursive_crawl(req: RecursiveCrawlRequest) -> RecursiveCrawlResponse:
        import uuid
        from urllib.parse import urlsplit

        domain = (urlsplit(req.url).hostname or "").lower()
        crawl_id = f"crawl_{uuid.uuid4().hex[:12]}"

        await broker.enqueue_fetch(
            FetchRequest(
                url=req.url,
                depth=0,
                render_js=req.render_js,
                metadata={
                    "recursive": True,
                    "crawl_id": crawl_id,
                    "depth": 0,
                    "max_depth": req.max_depth,
                    "max_pages": req.max_pages,
                    "allowed_domain": domain,
                    "render_js": req.render_js,
                },
            )
        )

        return RecursiveCrawlResponse(
            status="ok",
            crawl_id=crawl_id,
            seed_url=req.url,
            allowed_domain=domain,
            max_depth=req.max_depth,
            message=f"Recursive crawl initiated for {domain} (max_depth={req.max_depth})",
        )

    @api.post("/v1/extract", response_model=ExtractResponse)
    async def extract_structured(req: ExtractRequest) -> ExtractResponse:
        from services.extractor.structured_extractor import StructuredExtractor

        extractor = StructuredExtractor()
        html = req.html
        if not html and req.url:
            from services.fetcher.http_client import AsyncHttpFetcher

            fetcher = AsyncHttpFetcher()
            try:
                record, _ = await fetcher.fetch(req.url)
                if record:
                    html = record.raw_html
            finally:
                await fetcher.close()

        if not html:
            return ExtractResponse(status="error", items_count=0, data=[])

        extracted = extractor.extract(html, req.schema_)
        return ExtractResponse(status="ok", items_count=len(extracted), data=extracted)

    @api.get("/v1/queue/status")
    async def get_queue_status() -> dict[str, Any]:
        depths = await broker.get_queue_depths()
        return {"queues": depths}

    @api.get("/v1/stats")
    async def get_stats() -> dict[str, Any]:
        return await index.get_stats()

    return api
