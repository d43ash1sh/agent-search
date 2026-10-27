from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from services.api.server import app
from services.fetcher.browser_client import PlaywrightBrowserFetcher
from services.mcp.server import mcp


class TestBrowserActionsAndVision:
    @pytest.mark.anyio
    async def test_playwright_screenshot_and_actions(self) -> None:
        fetcher = PlaywrightBrowserFetcher()
        try:
            target_url = "https://example.com"
            actions = [
                {"type": "scroll", "direction": "down", "amount": 300},
                {"type": "wait", "timeout": 200},
            ]
            record, failed = await fetcher.fetch(
                target_url,
                screenshot=True,
                actions=actions,
            )
            assert failed is None
            assert record is not None
            assert record.screenshot_base64 is not None
            assert len(record.screenshot_base64) > 100  # valid base64 PNG
            assert "Example Domain" in record.raw_html
        finally:
            await fetcher.close()

    def test_api_scrape_with_screenshot_and_actions(self) -> None:
        client = TestClient(app)
        scrape_req = {
            "url": "https://example.com",
            "render_js": True,
            "screenshot": True,
            "actions": [
                {"type": "scroll", "direction": "down", "amount": 200},
            ],
        }
        resp = client.post("/v1/scrape", json=scrape_req)
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["title"] == "Example Domain" or "documentation examples" in data["markdown"]
        assert data["screenshot_base64"] is not None
        assert len(data["screenshot_base64"]) > 100

    @pytest.mark.anyio
    async def test_mcp_fetch_live_page_with_screenshot_and_actions(self) -> None:
        raw_result = await mcp.call_tool(
            "fetch_live_page",
            {
                "url": "https://example.com",
                "render_js": True,
                "screenshot": True,
                "actions": [{"type": "wait", "timeout": 100}],
            },
        )
        assert raw_result is not None
        assert hasattr(raw_result, "content")
        content = getattr(raw_result, "content", [])
        text = "".join(getattr(item, "text", str(item)) for item in content)
        data = json.loads(text)
        assert "url" in data
        assert "screenshot_base64" in data
        assert len(data["screenshot_base64"]) > 100

    @pytest.mark.anyio
    async def test_mcp_live_web_search_tool_registered(self) -> None:
        tools = await mcp.list_tools()
        tool_names = [t.name for t in tools]
        assert "live_web_search" in tool_names

        # Call live_web_search with live_crawl=False
        raw_res = await mcp.call_tool(
            "live_web_search",
            {"query": "distributed crawling and search", "limit": 2, "live_crawl": False},
        )
        assert raw_res is not None
        content = getattr(raw_res, "content", [])
        text = "".join(getattr(item, "text", str(item)) for item in content)
        data = json.loads(text)
        assert "query" in data
        assert "results" in data
