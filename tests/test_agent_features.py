import http.server
import json
import socket
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path
from typing import ClassVar

import uvicorn

from crawler.api import create_app
from crawler.fetcher import fetch_live_page
from crawler.mcp import AgentSearchMCPServer
from crawler.pipelines import SQLitePipeline
from crawler.search import SearchService


class DummyServer(http.server.BaseHTTPRequestHandler):
    visited: ClassVar[list[str]] = []

    def do_GET(self) -> None:
        type(self).visited.append(self.path)
        if self.path == "/":
            body = (
                b"<html><head><title>Live Test Page</title></head>"
                b"<body><h1>Main Heading</h1><p>Paragraph one with evidence.</p>"
                b"<p>Paragraph two with additional details.</p></body></html>"
            )
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        return


class AgentFeaturesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.database = str(Path(self.directory.name) / "agent_test.sqlite3")
        pipeline = SQLitePipeline(self.database)
        pipeline.open_spider()
        pipeline.process_item(
            {
                "url": "https://docs.test/intro",
                "canonical_url": "https://docs.test/intro",
                "title": "Introduction to Agents",
                "text": "Ignored flat text",
                "blocks": [
                    "First passage introducing agent search systems. " * 35,
                    "Second passage discussing retrieval benchmarks and budgets. " * 35,
                    "Third passage exploring citations and source verification. " * 35,
                ],
            }
        )
        pipeline.process_item(
            {
                "url": "https://research.test/analysis",
                "canonical_url": "https://research.test/analysis",
                "title": "Agent Research Analysis",
                "text": "Ignored flat text",
                "blocks": [
                    "Independent benchmarks for agent retrieval systems. " * 35,
                    "Comparing token efficiency across search architectures. " * 35,
                ],
            }
        )
        pipeline.close_spider()
        self.service = SearchService(self.database)

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_neighboring_passage_context_retrieval(self) -> None:
        results, _ = self.service.search("benchmarks budgets", limit=5)
        self.assertGreater(len(results), 0)
        target_ev_id = results[0].evidence_id

        context = self.service.get_evidence_context(target_ev_id, window=1)
        self.assertIsNotNone(context)
        assert context is not None
        self.assertEqual(context["evidence_id"], target_ev_id)
        self.assertGreater(context["target"]["ordinal"], 0)
        self.assertEqual(len(context["preceding"]), 1)
        self.assertEqual(len(context["succeeding"]), 1)

    def test_document_retrieval_returns_full_text_and_passages(self) -> None:
        doc = self.service.get_document("https://docs.test/intro")
        self.assertIsNotNone(doc)
        assert doc is not None
        self.assertEqual(doc["title"], "Introduction to Agents")
        self.assertGreaterEqual(len(doc["passages"]), 3)
        self.assertIn("First passage introducing agent search systems.", doc["text"])

    def test_search_modes_compact_and_research(self) -> None:
        compact_results, _ = self.service.search("agent systems", mode="compact", limit=10)
        research_results, _ = self.service.search("agent systems", mode="research", limit=10)

        self.assertGreater(len(compact_results), 0)
        self.assertGreater(len(research_results), 0)

        # In research mode, results interleave across distinct domains
        domains = [r.domain for r in research_results]
        self.assertIn("docs.test", domains)
        self.assertIn("research.test", domains)

    def test_index_stats_metrics(self) -> None:
        stats = self.service.get_stats()
        self.assertTrue(stats["exists"])
        self.assertEqual(stats["total_documents"], 2)
        self.assertEqual(stats["total_passages"], 9)
        self.assertEqual(stats["total_domains"], 2)

    def test_api_context_document_and_stats_endpoints(self) -> None:
        app = create_app(lambda: SearchService(self.database))
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]

        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="critical"))
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 5
            while not server.started and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(server.started, "HTTP server failed to start")

            # 1. Test POST /v1/search
            search_payload = json.dumps({"query": "agent", "mode": "research", "limit": 5}).encode("utf-8")
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/v1/search",
                data=search_payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=5) as res:
                data = json.load(res)
            self.assertGreater(data["result_count"], 0)
            self.assertEqual(data["mode"], "research")
            ev_id = data["results"][0]["evidence_id"]

            # 2. Test GET /v1/evidence/{evidence_id}/context
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/v1/evidence/{ev_id}/context?window=1", timeout=5
            ) as res:
                context_data = json.load(res)
            self.assertEqual(context_data["evidence_id"], ev_id)

            # 3. Test GET /v1/documents?url=...
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/v1/documents?url=https://docs.test/intro", timeout=5
            ) as res:
                doc_data = json.load(res)
            self.assertEqual(doc_data["title"], "Introduction to Agents")

            # 4. Test GET /v1/stats
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/v1/stats", timeout=5) as res:
                stats_data = json.load(res)
            self.assertEqual(stats_data["total_documents"], 2)
        finally:
            server.should_exit = True
            thread.join(timeout=5)

    def test_mcp_server_protocol_over_json_rpc(self) -> None:
        mcp = AgentSearchMCPServer(self.database)

        # 1. Test initialize
        init_req = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
        init_res = mcp.handle_request(init_req)
        self.assertIsNotNone(init_res)
        assert init_res is not None
        self.assertEqual(init_res["result"]["serverInfo"]["name"], "agent-search")

        # 2. Test tools/list
        tools_req = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
        tools_res = mcp.handle_request(tools_req)
        self.assertIsNotNone(tools_res)
        assert tools_res is not None
        tool_names = [t["name"] for t in tools_res["result"]["tools"]]
        self.assertIn("search_web_evidence", tool_names)
        self.assertIn("get_evidence", tool_names)
        self.assertIn("get_evidence_context", tool_names)
        self.assertIn("get_document", tool_names)
        self.assertIn("fetch_live_page", tool_names)
        self.assertIn("get_index_stats", tool_names)

        # 3. Test tools/call: search_web_evidence
        call_req = {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "search_web_evidence", "arguments": {"query": "citations verification"}},
        }
        call_res = mcp.handle_request(call_req)
        self.assertIsNotNone(call_res)
        assert call_res is not None
        content_text = call_res["result"]["content"][0]["text"]
        payload = json.loads(content_text)
        self.assertGreater(payload["result_count"], 0)
        self.assertIn("citations and source verification", content_text)

        # 4. Test tools/call: get_index_stats
        stats_call = {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {"name": "get_index_stats", "arguments": {}},
        }
        stats_res = mcp.handle_request(stats_call)
        self.assertIsNotNone(stats_res)
        assert stats_res is not None
        stats_payload = json.loads(stats_res["result"]["content"][0]["text"])
        self.assertEqual(stats_payload["total_documents"], 2)

    def test_live_fetcher_extracts_clean_evidence(self) -> None:
        DummyServer.visited.clear()
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), DummyServer)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_port}/"
            page = fetch_live_page(url)
            self.assertEqual(page["title"], "Live Test Page")
            self.assertIn("Paragraph one with evidence", page["text"])
            self.assertGreater(page["passage_count"], 0)
            self.assertEqual(page["status"], 200)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
