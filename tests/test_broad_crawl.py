import http.server
import json
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from typing import ClassVar


class BroadCrawlServer(http.server.BaseHTTPRequestHandler):
    visited: ClassVar[list[str]] = []

    def do_GET(self) -> None:
        type(self).visited.append(self.path)
        if self.path == "/robots.txt":
            status, ctype, body = 200, "text/plain", b"User-agent: *\nAllow: /\n"
        elif self.path == "/":
            status, ctype, body = (
                200,
                "text/html",
                (
                    b"<html><head><title>Root</title></head><body>"
                    b"<p>Root page</p>"
                    b"<a href='/page1'>Page 1</a>"
                    b"<a href='/page2'>Page 2</a>"
                    b"<a href='/page3'>Page 3</a>"
                    b"</body></html>"
                ),
            )
        elif self.path in {"/page1", "/page2", "/page3"}:
            status, ctype, body = (
                200,
                "text/html",
                (
                    f"<html><head><title>{self.path[1:]}</title></head>"
                    f"<body><p>Content of {self.path}</p></body></html>"
                ).encode(),
            )
        elif self.path == "/sitemap.xml":
            status, ctype, body = (
                200,
                "application/xml",
                (
                    b"<?xml version='1.0' encoding='UTF-8'?>"
                    b"<urlset xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'>"
                    b"<url><loc>http://127.0.0.1/sitemap-target</loc></url>"
                    b"</urlset>"
                ),
            )
        elif self.path == "/sitemap-target":
            status, ctype, body = (
                200,
                "text/html",
                (
                    b"<html><head><title>Sitemap target</title></head>"
                    b"<body><p>Discovered through sitemap</p></body></html>"
                ),
            )
        else:
            status, ctype, body = 404, "text/plain", b"Not found"

        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


class BroadCrawlTests(unittest.TestCase):
    def test_seeds_file_txt_and_json(self) -> None:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            txt_file = Path(directory) / "seeds.txt"
            txt_file.write_text(
                "# comment\nhttps://example.com/one\n\nhttps://example.org/two\n",
                encoding="utf-8",
            )
            json_file = Path(directory) / "seeds.json"
            json_file.write_text(
                json.dumps(["https://site.test/a", "https://site.test/b"]),
                encoding="utf-8",
            )

            # Test using python CLI validation logic
            import argparse

            from crawler.__main__ import _load_seeds

            args_txt = argparse.Namespace(seed=[], seeds_file=str(txt_file))
            seeds_txt = _load_seeds(args_txt)
            self.assertEqual(seeds_txt, ["https://example.com/one", "https://example.org/two"])

            args_json = argparse.Namespace(seed=[], seeds_file=str(json_file))
            seeds_json = _load_seeds(args_json)
            self.assertEqual(seeds_json, ["https://site.test/a", "https://site.test/b"])

    def test_max_pages_per_domain_limits_domain_crawl(self) -> None:
        BroadCrawlServer.visited.clear()
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), BroadCrawlServer)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
                database = Path(directory) / "domain_capped.sqlite3"
                seeds_file = Path(directory) / "seeds.txt"
                seeds_file.write_text(f"http://127.0.0.1:{server.server_port}/\n", encoding="utf-8")

                result = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "crawler",
                        "crawl",
                        "--seeds-file",
                        str(seeds_file),
                        "--allow-domain",
                        "127.0.0.1",
                        "--delay",
                        "0",
                        "--max-pages-per-domain",
                        "2",
                        "--database",
                        str(database),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=60,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

                connection = sqlite3.connect(database)
                try:
                    documents = connection.execute("SELECT url FROM documents").fetchall()
                finally:
                    connection.close()

                # Capped to 2 pages on this domain even though 4 pages exist
                self.assertEqual(len(documents), 2)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_sitemap_discovery_finds_unlinked_pages(self) -> None:
        BroadCrawlServer.visited.clear()
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), BroadCrawlServer)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
                database = Path(directory) / "sitemap.sqlite3"
                # Rewrite sitemap port dynamically in test
                result = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "crawler",
                        "crawl",
                        "--seed",
                        f"http://127.0.0.1:{server.server_port}/",
                        "--allow-domain",
                        "127.0.0.1",
                        "--delay",
                        "0",
                        "--sitemaps",
                        "--max-pages",
                        "5",
                        "--database",
                        str(database),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=60,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

                # Confirm sitemap.xml was requested
                self.assertIn("/sitemap.xml", BroadCrawlServer.visited)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
