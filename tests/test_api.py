import json
import socket
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path

import uvicorn
from fastapi import HTTPException
from fastapi.routing import APIRoute

from crawler.api import create_app
from crawler.pipelines import SQLitePipeline
from crawler.search import SearchService


class SearchApiTests(unittest.TestCase):
    def test_missing_index_response_explains_how_to_create_it(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            database = str(Path(directory) / "missing" / "index.sqlite3")
            app = create_app(lambda: SearchService(database))
            route = next(
                route for route in app.routes if isinstance(route, APIRoute) and route.path == "/v1/search"
            )

            with self.assertRaises(HTTPException) as raised:
                route.endpoint(
                    q="evidence",
                    limit=5,
                    max_tokens=100,
                    domain=None,
                    after=None,
                    before=None,
                )

            self.assertEqual(raised.exception.status_code, 503)
            self.assertIn(str(Path(database).resolve()), raised.exception.detail)
            self.assertIn("python -m crawler crawl", raised.exception.detail)

    def test_search_route_returns_citations_and_budget_metadata(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            database = str(Path(directory) / "index.sqlite3")
            pipeline = SQLitePipeline(database)
            pipeline.open_spider()
            pipeline.process_item(
                {
                    "url": "https://example.com/page",
                    "canonical_url": "https://example.com/canonical",
                    "title": "Agent retrieval",
                    "text": "Useful evidence for an agent.",
                    "blocks": ["Useful evidence for an agent."],
                }
            )
            pipeline.close_spider()
            app = create_app(lambda: SearchService(database))
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
                self.assertTrue(server.started, "HTTP API did not start")

                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/v1/search?q=evidence&limit=5&max_tokens=100",
                    timeout=5,
                ) as response:
                    payload = json.load(response)

                self.assertEqual(payload["result_count"], 1)
                self.assertLessEqual(payload["estimated_tokens"], payload["token_budget"])
                self.assertEqual(payload["retrieval_mode"], "lexical")
                self.assertEqual(payload["match_mode"], "all")
                self.assertEqual(
                    payload["results"][0]["citation"]["canonical_url"],
                    "https://example.com/canonical",
                )
                self.assertEqual(payload["results"][0]["citation"]["domain"], "example.com")
                evidence_id = payload["results"][0]["evidence_id"]
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/v1/evidence/{evidence_id}",
                    timeout=5,
                ) as response:
                    evidence = json.load(response)

                self.assertEqual(evidence["evidence_id"], evidence_id)
                self.assertEqual(
                    evidence["citation"]["end_offset"] - evidence["citation"]["start_offset"],
                    len(evidence["text"]),
                )
            finally:
                server.should_exit = True
                thread.join(timeout=5)
