from __future__ import annotations

import json
import logging
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from services.common.config import settings
from services.common.models import FetchRequest
from services.extractor.chunker import chunk_document
from services.extractor.trafilatura_cleaner import TrafilaturaCleaner
from services.fetcher.http_client import AsyncHttpFetcher
from services.indexer.base import AbstractSearchIndex
from services.indexer.factory import create_search_index
from services.indexer.reranker import NeuralReranker
from services.queue.base import AbstractQueueBroker
from services.queue.memory_broker import InMemoryQueueBroker
from services.queue.redis_broker import RedisQueueBroker

logger = logging.getLogger("agent-search.mcp")

# Initialize shared components
_index: AbstractSearchIndex | None = None
_reranker: NeuralReranker | None = None
_broker: AbstractQueueBroker | None = None


def get_search_index() -> AbstractSearchIndex:
    global _index
    if _index is None:
        _index = create_search_index()
    return _index


def get_reranker() -> NeuralReranker:
    global _reranker
    if _reranker is None:
        _reranker = NeuralReranker()
    return _reranker


def get_broker() -> AbstractQueueBroker:
    global _broker
    if _broker is None:
        try:
            import redis

            r = redis.from_url(settings.redis_url, socket_timeout=1.5)
            r.ping()
            _broker = RedisQueueBroker(settings.redis_url)
        except Exception:
            _broker = InMemoryQueueBroker()
    return _broker


# Create MCP Server instance
mcp = MCPServer(
    name="AgentSearch",
    version="2.0.0",
)


@mcp.tool(
    name="search_passages",
    description=(
        "Search the agent-native web evidence index using hybrid semantic vector (pgvector) "
        "and lexical ranking with neural cross-encoder reranking. "
        "Returns atomic, cited passages with character offsets and token estimates."
    ),
    annotations=ToolAnnotations(read_only_hint=True),
)
async def search_passages(
    query: str,
    limit: int = 5,
    max_tokens: int = 1200,
    domains: list[str] | None = None,
    hybrid: bool = True,
    rerank: bool = True,
) -> str:
    """Search for relevant passages with optional neural reranking."""
    index = get_search_index()
    hits, estimated_tokens = await index.search(
        query=query,
        limit=limit,
        max_tokens=max_tokens,
        domains=domains,
        hybrid=hybrid,
    )

    if rerank and hits:
        reranker = get_reranker()
        hits, _ = reranker.rerank(
            query=query,
            hits=hits,
            max_tokens=max_tokens,
            top_k=limit,
        )

    results_payload = [
        {
            "evidence_id": h.evidence_id,
            "url": h.url,
            "canonical_url": h.canonical_url,
            "title": h.title,
            "snippet": h.text,
            "score": round(float(h.score), 4),
            "match_type": h.match_type,
            "estimated_tokens": h.estimated_tokens,
            "author": h.author,
            "published_date": h.published_date,
        }
        for h in hits[:limit]
    ]

    return json.dumps(
        {
            "query": query,
            "count": len(results_payload),
            "estimated_tokens": estimated_tokens,
            "token_budget": max_tokens,
            "hybrid_enabled": hybrid,
            "rerank_applied": rerank,
            "results": results_payload,
        },
        indent=2,
    )


@mcp.tool(
    name="get_evidence_context",
    description=(
        "Retrieve the exact passage and its surrounding context by evidence_id. "
        "Enables verification of claims in the original source without hallucination."
    ),
    annotations=ToolAnnotations(read_only_hint=True),
)
async def get_evidence_context(evidence_id: str) -> str:
    """Retrieve evidence passage details."""
    index = get_search_index()
    evidence = await index.get_evidence(evidence_id)
    if evidence is None:
        return json.dumps({"error": f"Evidence not found: {evidence_id}", "found": False})

    return json.dumps(
        {
            "found": True,
            "evidence_id": evidence.evidence_id,
            "url": evidence.url,
            "title": evidence.title,
            "text": evidence.text,
            "author": evidence.author,
            "published_date": evidence.published_date,
        },
        indent=2,
    )


@mcp.tool(
    name="get_document",
    description="Retrieve the full cleaned document text and metadata for a given URL.",
    annotations=ToolAnnotations(read_only_hint=True),
)
async def get_document(url: str) -> str:
    """Retrieve full indexed document by URL."""
    index = get_search_index()
    # Check if index provides direct document lookup or search fallback
    hits, _ = await index.search(query=url, limit=1, hybrid=False)
    if not hits:
        return json.dumps({"error": f"Document not found in index: {url}", "found": False})

    hit = hits[0]
    return json.dumps(
        {
            "found": True,
            "url": hit.url,
            "canonical_url": hit.canonical_url,
            "title": hit.title,
            "author": hit.author,
            "published_date": hit.published_date,
            "text": hit.text,
        },
        indent=2,
    )


@mcp.tool(
    name="fetch_live_page",
    description=(
        "Fetch an arbitrary live web URL on-demand using httpx or Playwright and extract clean text, "
        "markdown, passage chunks, and optional multi-modal base64 screenshots with browser action automation."
    ),
    annotations=ToolAnnotations(read_only_hint=True),
)
async def fetch_live_page(
    url: str,
    render_js: bool = False,
    screenshot: bool = False,
    actions: list[dict[str, Any]] | None = None,
) -> str:
    """Live-fetch and parse a web page on-demand with optional Playwright rendering, screenshots, and actions."""
    cleaner = TrafilaturaCleaner()
    record = None
    failed = None

    if render_js or screenshot or actions:
        from services.fetcher.browser_client import PlaywrightBrowserFetcher

        browser_fetcher = PlaywrightBrowserFetcher()
        try:
            record, failed = await browser_fetcher.fetch(
                url,
                screenshot=screenshot,
                actions=actions,
            )
        finally:
            await browser_fetcher.close()
    else:
        fetcher = AsyncHttpFetcher()
        try:
            record, failed = await fetcher.fetch(url)
        finally:
            await fetcher.close()

    if failed:
        return json.dumps(
            {
                "error": f"Fetch failed: {failed.error_message}",
                "error_type": failed.error_type,
                "url": url,
            }
        )
    if not record:
        return json.dumps({"error": "Empty response received", "url": url})

    extracted = cleaner.extract(record.raw_html, url=url)
    if not extracted:
        return json.dumps({"error": "No extractable text found", "url": url})

    title = extracted.get("title", "")
    author = extracted.get("author")
    published_date = extracted.get("published_date")
    clean_text = extracted.get("clean_text", "")
    passages = chunk_document(url=url, text=clean_text)

    response_dict: dict[str, Any] = {
        "url": url,
        "status_code": record.status_code,
        "title": title,
        "author": author,
        "published_date": published_date,
        "clean_text_preview": clean_text[:500],
        "total_passages": len(passages),
        "passages": [
            {
                "evidence_id": p.passage_id,
                "text": p.text,
                "tokens": p.estimated_tokens,
            }
            for p in passages[:5]
        ],
    }
    if record.screenshot_base64:
        response_dict["screenshot_base64"] = record.screenshot_base64

    return json.dumps(response_dict, indent=2)


@mcp.tool(
    name="live_web_search",
    description=(
        "Autonomous live web metasearch: searches the live web in real time via DuckDuckGo SERP, "
        "crawls and cleans top source pages on the fly, computes dense vector embeddings, "
        "indexes them into the database, and returns neural-reranked cited evidence passages."
    ),
    annotations=ToolAnnotations(read_only_hint=False),
)
async def live_web_search(
    query: str,
    limit: int = 5,
    max_tokens: int = 1200,
    hybrid: bool = True,
    live_crawl: bool = True,
) -> str:
    """Execute live internet metasearch with on-the-fly crawling and neural reranking."""
    from services.search.live_engine import LiveSearchEngine

    index = get_search_index()
    reranker = get_reranker()
    engine = LiveSearchEngine(index=index, reranker=reranker)

    res = await engine.search(
        query=query,
        limit=limit,
        max_tokens=max_tokens,
        hybrid=hybrid,
        rerank=True,
        live_crawl=live_crawl,
    )
    return json.dumps(res.model_dump(), indent=2)


@mcp.tool(
    name="enqueue_crawl_url",
    description=(
        "Enqueue a URL to the distributed crawler fetch queue for asynchronous fetching, "
        "hyperlink discovery, and vector indexing."
    ),
)
async def enqueue_crawl_url(url: str, depth: int = 1, render_js: bool = False) -> str:
    """Enqueue a seed URL to be fetched and indexed."""
    broker = get_broker()
    req = FetchRequest(
        url=url,
        depth=depth,
        render_js=render_js,
        metadata={"from_mcp": True},
    )
    await broker.enqueue_fetch(req)
    return json.dumps(
        {
            "status": "enqueued",
            "url": url,
            "depth": depth,
            "render_js": render_js,
            "message": "URL enqueued to distributed crawler fetch queue.",
        },
        indent=2,
    )


@mcp.tool(
    name="crawl_frontier_batch",
    description=(
        "Dispatch an on-demand crawl batch from discovered cross-site frontier outlinks "
        "into the active fetch queue."
    ),
)
async def crawl_frontier_batch(limit: int = 25, max_per_domain: int = 5) -> str:
    """Dispatch discovered frontier links to the fetch queue."""
    index = get_search_index()
    broker = get_broker()
    pending = await index.get_pending_frontier_urls(limit=limit, max_per_domain=max_per_domain)
    enqueued: list[str] = []
    for item in pending:
        u = item["url"]
        await broker.enqueue_fetch(
            FetchRequest(
                url=u,
                depth=item.get("depth", 1),
                metadata={"from_frontier": True, "source_url": item.get("source_url", "")},
            )
        )
        await index.update_frontier_status(u, "queued")
        enqueued.append(u)

    return json.dumps(
        {
            "status": "ok",
            "enqueued_count": len(enqueued),
            "urls": enqueued,
        },
        indent=2,
    )


@mcp.tool(
    name="get_search_stats",
    description="Retrieve live search engine metrics, pgvector status, document counts, and frontier discovery counts.",
    annotations=ToolAnnotations(read_only_hint=True),
)
async def get_search_stats() -> str:
    """Return database and index metrics."""
    index = get_search_index()
    stats = await index.get_stats()
    frontier_stats = await index.get_frontier_stats()
    return json.dumps(
        {
            "index": stats,
            "frontier": frontier_stats,
        },
        indent=2,
    )


def create_mcp_server() -> MCPServer:
    return mcp


async def handle_mcp_request(request: dict[str, Any]) -> dict[str, Any] | None:
    """Handle standard JSON-RPC 2.0 MCP requests for HTTP or stdio bridging."""
    req_id = request.get("id")
    method = request.get("method")

    if req_id is None:
        return None

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {
                    "name": "agent-search",
                    "title": "Agent-Native Search & Evidence Index",
                    "version": "2.0.0",
                },
            },
        }

    elif method == "tools/list":
        tools = await mcp.list_tools()
        tools_data = [
            {
                "name": t.name,
                "description": t.description or "",
                "inputSchema": t.input_schema if hasattr(t, "input_schema") else {},
            }
            for t in tools
        ]
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"tools": tools_data},
        }

    elif method == "tools/call":
        params = request.get("params", {})
        name = params.get("name", "")
        arguments = params.get("arguments", {})
        try:
            result = await mcp.call_tool(name, arguments)
            text_content = ""
            for item in getattr(result, "content", []):
                text_content += getattr(item, "text", str(item))
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": text_content}],
                    "isError": bool(getattr(result, "is_error", False)),
                },
            }
        except Exception as exc:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": f"Tool execution failed: {exc}"}],
                    "isError": True,
                },
            }

    elif method == "ping":
        return {"jsonrpc": "2.0", "id": req_id, "result": {}}

    else:
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32601, "message": f"Method not found: {method}"},
        }


if __name__ == "__main__":
    # Run standalone stdio server for Claude Desktop, Cursor, and IDEs
    mcp.run()
