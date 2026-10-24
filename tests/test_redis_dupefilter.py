from __future__ import annotations

import unittest

from scrapy.http import Request

from crawler.dupefilter import RedisDupeFilter


class RedisDupeFilterTests(unittest.TestCase):
    def test_dupefilter_in_memory_fallback(self) -> None:
        # Connect to invalid port to trigger memory fallback
        filter_ = RedisDupeFilter(redis_url="redis://127.0.0.1:65530/0")
        filter_.open()

        req1 = Request("https://example.com/page1")
        req2 = Request("https://example.com/page2")

        # First time seen -> returns False (not duplicate)
        self.assertFalse(filter_.request_seen(req1))
        self.assertFalse(filter_.request_seen(req2))

        # Second time seen -> returns True (duplicate)
        self.assertTrue(filter_.request_seen(req1))
        self.assertTrue(filter_.request_seen(req2))

        filter_.close()
