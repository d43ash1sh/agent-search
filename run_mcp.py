"""Agent Search - Standalone Model Context Protocol (MCP) Server.

Connects to PostgreSQL (pgvector + hybrid search), Redis queues, and neural reranker,
exposing standard MCP tools over stdio to Claude Desktop, Cursor, VS Code, and AI agents.

Usage:
    .venv\\Scripts\\python run_mcp.py
"""

from __future__ import annotations

import sys

from services.mcp.server import mcp

if __name__ == "__main__":
    try:
        mcp.run()
    except KeyboardInterrupt:
        sys.exit(0)
