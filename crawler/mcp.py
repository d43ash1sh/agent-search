import json
import logging
import sys
from typing import Any

from crawler.fetcher import fetch_live_page
from crawler.search import DEFAULT_DATABASE, SearchService

logger = logging.getLogger("agent_search_mcp")


class AgentSearchMCPServer:
    """Model Context Protocol (MCP) server for Agent-Native Web Search over stdio."""

    def __init__(self, database: str = DEFAULT_DATABASE) -> None:
        self.service = SearchService(database)

    def get_tool_definitions(self) -> list[dict[str, Any]]:
        return [
            {
                "name": "search_web_evidence",
                "description": (
                    "Search the local agent-native web index for factual evidence. "
                    "Returns compact, cited passages with token budgets and relevance scores."
                ),
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "FTS5 query to search across indexed passages.",
                        },
                        "limit": {
                            "type": "integer",
                            "default": 8,
                            "description": "Maximum number of evidence units to return (1-50).",
                        },
                        "max_tokens": {
                            "type": "integer",
                            "default": 1200,
                            "description": "Maximum estimated token budget for the response payload.",
                        },
                        "domains": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Optional domain filter (e.g. ['docs.python.org']).",
                        },
                        "mode": {
                            "type": "string",
                            "enum": ["compact", "balanced", "research"],
                            "default": "compact",
                            "description": (
                                "'compact' for minimal tokens & strict deduplication; "
                                "'balanced' for up to 2 passages per source; "
                                "'research' for maximum source diversity across domains."
                            ),
                        },
                        "match": {
                            "type": "string",
                            "enum": ["all", "any"],
                            "default": "all",
                            "description": "'all' requires all query terms (high precision); 'any' broadens recall.",
                        },
                    },
                    "required": ["query"],
                },
            },
            {
                "name": "get_evidence",
                "description": "Retrieve a specific passage by its stable 64-character evidence_id.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "evidence_id": {
                            "type": "string",
                            "description": "The unique evidence_id returned in search results.",
                        }
                    },
                    "required": ["evidence_id"],
                },
            },
            {
                "name": "get_evidence_context",
                "description": "Retrieve neighboring passages before and after an evidence passage to verify full context.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "evidence_id": {
                            "type": "string",
                            "description": "The evidence_id to inspect context around.",
                        },
                        "window": {
                            "type": "integer",
                            "default": 1,
                            "description": "Number of preceding and succeeding passages to include (1-5).",
                        },
                    },
                    "required": ["evidence_id"],
                },
            },
            {
                "name": "get_document",
                "description": "Retrieve the full cleaned document text and all its indexed passage offsets by URL.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "url": {
                            "type": "string",
                            "description": "The original or canonical URL of the indexed document.",
                        }
                    },
                    "required": ["url"],
                },
            },
            {
                "name": "fetch_live_page",
                "description": "Fetch an arbitrary live web page on-demand and extract structured evidence passages.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "url": {
                            "type": "string",
                            "description": "The HTTP(S) URL to fetch live.",
                        }
                    },
                    "required": ["url"],
                },
            },
            {
                "name": "get_index_stats",
                "description": "Get index metrics: total documents, total passages, unique domains, and storage size.",
                "inputSchema": {
                    "type": "object",
                    "properties": {},
                },
            },
        ]

    def handle_tool_call(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        if name == "search_web_evidence":
            query = str(args["query"])
            limit = int(args.get("limit", 8))
            max_tokens = int(args.get("max_tokens", 1200))
            domains = args.get("domains")
            mode = args.get("mode", "compact")
            match = args.get("match", "all")

            results, tokens = self.service.search(
                query,
                limit=limit,
                max_tokens=max_tokens,
                domains=domains,
                match=match,
                mode=mode,
            )
            payload = {
                "query": query,
                "result_count": len(results),
                "estimated_tokens": tokens,
                "token_budget": max_tokens,
                "mode": mode,
                "match_mode": match,
                "results": [r.to_dict() for r in results],
            }
            return {"content": [{"type": "text", "text": json.dumps(payload, indent=2)}]}

        elif name == "get_evidence":
            evidence_id = str(args["evidence_id"])
            item = self.service.get_evidence(evidence_id)
            if item is None:
                return {
                    "isError": True,
                    "content": [{"type": "text", "text": f"Evidence not found: {evidence_id}"}],
                }
            return {"content": [{"type": "text", "text": json.dumps(item.to_dict(), indent=2)}]}

        elif name == "get_evidence_context":
            evidence_id = str(args["evidence_id"])
            window = int(args.get("window", 1))
            context = self.service.get_evidence_context(evidence_id, window=window)
            if context is None:
                return {
                    "isError": True,
                    "content": [{"type": "text", "text": f"Evidence not found: {evidence_id}"}],
                }
            return {"content": [{"type": "text", "text": json.dumps(context, indent=2)}]}

        elif name == "get_document":
            url = str(args["url"])
            doc = self.service.get_document(url)
            if doc is None:
                return {
                    "isError": True,
                    "content": [{"type": "text", "text": f"Document not found for URL: {url}"}],
                }
            return {"content": [{"type": "text", "text": json.dumps(doc, indent=2)}]}

        elif name == "fetch_live_page":
            url = str(args["url"])
            try:
                page = fetch_live_page(url)
                return {"content": [{"type": "text", "text": json.dumps(page, indent=2)}]}
            except Exception as error:
                return {"isError": True, "content": [{"type": "text", "text": str(error)}]}

        elif name == "get_index_stats":
            stats = self.service.get_stats()
            return {"content": [{"type": "text", "text": json.dumps(stats, indent=2)}]}

        else:
            return {"isError": True, "content": [{"type": "text", "text": f"Unknown tool: {name}"}]}

    def handle_request(self, request: dict[str, Any]) -> dict[str, Any] | None:
        req_id = request.get("id")
        method = request.get("method")

        # Notifications without id
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
                        "title": "Agent-Native Search Index",
                        "version": "0.2.0",
                    },
                },
            }

        elif method == "tools/list":
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"tools": self.get_tool_definitions()},
            }

        elif method == "tools/call":
            params = request.get("params", {})
            name = params.get("name", "")
            arguments = params.get("arguments", {})
            result = self.handle_tool_call(name, arguments)
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": result,
            }

        elif method == "ping":
            return {"jsonrpc": "2.0", "id": req_id, "result": {}}

        else:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32601, "message": f"Method not found: {method}"},
            }

    def run_stdio(self) -> None:
        """Run standard I/O loop for MCP communication."""
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                request = json.loads(line)
                response = self.handle_request(request)
                if response is not None:
                    sys.stdout.write(json.dumps(response) + "\n")
                    sys.stdout.flush()
            except Exception as error:
                logger.error("Error processing MCP message: %s", error)
                err_response = {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32700, "message": f"Parse error: {error}"},
                }
                sys.stdout.write(json.dumps(err_response) + "\n")
                sys.stdout.flush()
