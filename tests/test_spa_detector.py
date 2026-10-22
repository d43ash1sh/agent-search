import unittest

from services.fetcher.spa_detector import extract_visible_text_length, is_spa_shell


class TestSPADetector(unittest.TestCase):
    def test_detects_empty_react_root(self) -> None:
        html = """
        <!DOCTYPE html>
        <html>
            <head><title>React App</title></head>
            <body>
                <noscript>You need to enable JavaScript to run this app.</noscript>
                <div id="root"></div>
                <script src="/static/js/bundle.js"></script>
            </body>
        </html>
        """
        self.assertTrue(is_spa_shell(html))

    def test_detects_empty_vue_mount(self) -> None:
        html = """
        <html>
            <head><title>Vue App</title></head>
            <body>
                <div id="app"></div>
                <script type="module" src="/src/main.js"></script>
            </body>
        </html>
        """
        self.assertTrue(is_spa_shell(html))

    def test_detects_empty_nextjs_mount(self) -> None:
        html = """
        <html>
            <body>
                <div id="__next"></div>
                <script src="/_next/static/chunks/main.js"></script>
            </body>
        </html>
        """
        self.assertTrue(is_spa_shell(html))

    def test_does_not_flag_server_rendered_documentation(self) -> None:
        html = """
        <!DOCTYPE html>
        <html>
            <head><title>FastAPI Documentation</title></head>
            <body>
                <nav><a href="/">Home</a></nav>
                <main>
                    <h1>First Steps with FastAPI</h1>
                    <p>FastAPI is a modern, fast web framework for building APIs with Python.</p>
                    <pre><code>from fastapi import FastAPI\napp = FastAPI()</code></pre>
                    <p>Here are multiple descriptive paragraphs explaining how path operations work in detail.</p>
                    <p>Parameters can be declared as query or path parameters with type hints.</p>
                </main>
                <script src="/search.js"></script>
            </body>
        </html>
        """
        self.assertFalse(is_spa_shell(html))
        self.assertGreater(extract_visible_text_length(html), 200)
