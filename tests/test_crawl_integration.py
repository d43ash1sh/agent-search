import http.server
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from typing import ClassVar


class Handler(http.server.BaseHTTPRequestHandler):
    paths: ClassVar[list[str]] = []

    def do_GET(self) -> None:
        type(self).paths.append(self.path)
        if self.path == "/robots.txt":
            status, content_type, body = 200, "text/plain", b"User-agent: *\nAllow: /\n"
        elif self.path == "/":
            status, content_type, body = (
                200,
                "text/html",
                (
                    b"<html><head><title>Home</title>"
                    b"<link rel='canonical' href='/'></head><body>"
                    b"<p>Start here</p><a href='/page#section'>page</a></body></html>"
                ),
            )
        elif self.path == "/page":
            status, content_type, body = (
                200,
                "text/html",
                (
                    b"<html><head><title>Indexed page</title></head>"
                    b"<body><p>unique crawlable phrase</p></body></html>"
                ),
            )
        else:
            status, content_type, body = 404, "text/plain", b"Not found"

        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


class CrawlIntegrationTests(unittest.TestCase):
    def test_cli_crawls_and_indexes_successful_pages(self) -> None:
        Handler.paths.clear()
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
                database = Path(directory) / "index.sqlite3"
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
                        "--database",
                        str(database),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=45,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

                connection = sqlite3.connect(database)
                try:
                    documents = connection.execute("SELECT url, title FROM documents ORDER BY url").fetchall()
                    search_results = connection.execute(
                        """
                        SELECT d.title FROM passages_fts
                        JOIN passages AS p ON p.id = passages_fts.rowid
                        JOIN documents AS d ON d.url = p.document_url
                        WHERE passages_fts MATCH ?
                        """,
                        ("crawlable",),
                    ).fetchall()
                finally:
                    connection.close()

            self.assertEqual(
                documents,
                [
                    (f"http://127.0.0.1:{server.server_port}/", "Home"),
                    (f"http://127.0.0.1:{server.server_port}/page", "Indexed page"),
                ],
                f"{result.stdout}{result.stderr}\nserver paths: {Handler.paths}",
            )
            self.assertEqual(search_results, [("Indexed page",)])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_max_pages_stops_crawl_and_keeps_seed_document(self) -> None:
        Handler.paths.clear()
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
                database = Path(directory) / "limited.sqlite3"
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
                        "--max-pages",
                        "1",
                        "--database",
                        str(database),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=45,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                connection = sqlite3.connect(database)
                try:
                    documents = connection.execute("SELECT url FROM documents").fetchall()
                finally:
                    connection.close()

            self.assertEqual(
                documents,
                [(f"http://127.0.0.1:{server.server_port}/",)],
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
