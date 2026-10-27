from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from services.api.server import app
from services.mcp.server import handle_mcp_request, mcp


@pytest.mark.anyio
async def test_mcp_list_tools() -> None:
    tools = await mcp.list_tools()
    tool_names = [t.name for t in tools]
    assert "search_passages" in tool_names
    assert "get_evidence_context" in tool_names
    assert "get_document" in tool_names
    assert "fetch_live_page" in tool_names
    assert "enqueue_crawl_url" in tool_names
    assert "crawl_frontier_batch" in tool_names
    assert "get_search_stats" in tool_names


@pytest.mark.anyio
async def test_mcp_call_stats_tool() -> None:
    res = await mcp.call_tool("get_search_stats", {})
    assert res is not None
    text = ""
    for item in res.content:
        text += getattr(item, "text", str(item))
    data = json.loads(text)
    assert "index" in data
    assert "frontier" in data


@pytest.mark.anyio
async def test_mcp_jsonrpc_request_handler() -> None:
    # 1. Initialize
    init_res = await handle_mcp_request({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    assert init_res is not None
    assert init_res["result"]["serverInfo"]["name"] == "agent-search"

    # 2. Tools list
    list_res = await handle_mcp_request({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    assert list_res is not None
    names = [t["name"] for t in list_res["result"]["tools"]]
    assert "search_passages" in names

    # 3. Tool call
    call_res = await handle_mcp_request(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "get_search_stats", "arguments": {}},
        }
    )
    assert call_res is not None
    assert "result" in call_res
    assert call_res["result"]["isError"] is False


def test_mcp_fastapi_http_endpoint() -> None:
    with TestClient(app) as client:
        # Test HTTP POST /mcp initialize
        res = client.post("/mcp", json={"jsonrpc": "2.0", "id": 10, "method": "initialize"})
        assert res.status_code == 200
        data = res.json()
        assert data["result"]["serverInfo"]["name"] == "agent-search"

        # Test HTTP POST /v1/mcp tools/list
        res_list = client.post("/v1/mcp", json={"jsonrpc": "2.0", "id": 11, "method": "tools/list"})
        assert res_list.status_code == 200
        list_data = res_list.json()
        tool_names = [t["name"] for t in list_data["result"]["tools"]]
        assert "search_passages" in tool_names
