import unittest

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from services.api.routes import create_api_router
from services.extractor.structured_extractor import StructuredExtractor
from services.indexer.sqlite_index import SQLiteSearchIndex
from services.queue.memory_broker import InMemoryQueueBroker


class TestStructuredExtractor(unittest.TestCase):
    def setUp(self) -> None:
        self.extractor = StructuredExtractor()
        self.sample_html = """
        <html>
            <body>
                <div class="product-card">
                    <h2 class="title">Playwright Pro</h2>
                    <span class="price">$99/mo</span>
                    <a class="buy-btn" href="/checkout?plan=pro">Buy Now</a>
                    <span class="version">Version: v2.5.0</span>
                </div>
                <div class="product-card">
                    <h2 class="title">Playwright Enterprise</h2>
                    <span class="price">$499/mo</span>
                    <a class="buy-btn" href="/checkout?plan=ent">Contact Us</a>
                    <span class="version">Version: v2.6.1</span>
                </div>
            </body>
        </html>
        """

    def test_extract_items_with_css_and_attributes(self) -> None:
        schema = {
            "name": "Products",
            "baseSelector": ".product-card",
            "fields": [
                {"name": "name", "selector": ".title", "type": "text"},
                {"name": "price", "selector": ".price", "type": "text"},
                {"name": "checkout_url", "selector": ".buy-btn", "type": "attribute", "attribute": "href"},
                {"name": "version", "selector": ".version", "type": "regex", "pattern": r"v\d+\.\d+\.\d+"},
            ],
        }

        results = self.extractor.extract(self.sample_html, schema)
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]["name"], "Playwright Pro")
        self.assertEqual(results[0]["price"], "$99/mo")
        self.assertEqual(results[0]["checkout_url"], "/checkout?plan=pro")
        self.assertEqual(results[0]["version"], "v2.5.0")

        self.assertEqual(results[1]["name"], "Playwright Enterprise")
        self.assertEqual(results[1]["checkout_url"], "/checkout?plan=ent")
        self.assertEqual(results[1]["version"], "v2.6.1")


@pytest.mark.anyio
async def test_api_extract_endpoint() -> None:
    broker = InMemoryQueueBroker()
    index = SQLiteSearchIndex("data/test_extract.sqlite3")
    app = FastAPI()
    app.include_router(create_api_router(broker, index))

    html = """
    <div class="book">
        <h3 class="name">Fluent Python</h3>
        <span class="author">Luciano Ramalho</span>
    </div>
    """

    schema = {
        "name": "Books",
        "baseSelector": ".book",
        "fields": [
            {"name": "book_title", "selector": ".name", "type": "text"},
            {"name": "author", "selector": ".author", "type": "text"},
        ],
    }

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        resp = await client.post("/v1/extract", json={"html": html, "schema": schema})
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["items_count"] == 1
        assert data["data"][0]["book_title"] == "Fluent Python"
        assert data["data"][0]["author"] == "Luciano Ramalho"
