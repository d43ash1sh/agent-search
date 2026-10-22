import unittest

from scrapy.http import HtmlResponse, Request

from crawler.spiders.site import SiteSpider


class SiteSpiderTests(unittest.TestCase):
    def test_extracts_document_and_only_follows_allowed_links(self) -> None:
        spider = SiteSpider(
            start_urls=["https://example.com/"],
            allow_domains=["example.com"],
        )
        response = HtmlResponse(
            url="https://example.com/",
            body=(
                b"<html><head><title> Example page </title>"
                b'<link rel="canonical" href="/canonical"></head><body>'
                b"<p>Useful page text</p><script>ignore this</script>"
                b"<ul><li><p>Nested list item text</p></li>"
                b"<li><p>Nested list item text</p></li></ul>"
                b'<a href="/next#part">next</a>'
                b'<a href="https://outside.test/">outside</a>'
                b"</body></html>"
            ),
            encoding="utf-8",
        )

        results = list(spider.parse(response))
        documents = [result for result in results if isinstance(result, dict)]
        requests = [result for result in results if isinstance(result, Request)]
        document = documents[0]

        self.assertEqual(document["title"], "Example page")
        self.assertEqual(document["text"], "Useful page text\n\nNested list item text")
        self.assertEqual(document.get("blocks"), ["Useful page text", "Nested list item text"])
        self.assertEqual(document["canonical_url"], "https://example.com/canonical")
        self.assertEqual([request.url for request in requests], ["https://example.com/next"])
