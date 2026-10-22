import unittest

from crawler.urls import host_is_allowed, normalize_url


class UrlTests(unittest.TestCase):
    def test_normalizes_scheme_host_default_port_and_fragment(self) -> None:
        self.assertEqual(
            normalize_url("HTTP://Example.COM:80/a#section"),
            "http://example.com/a",
        )

    def test_rejects_non_http_urls_and_credentials(self) -> None:
        self.assertIsNone(normalize_url("mailto:test@example.com"))
        self.assertIsNone(normalize_url("https://user:secret@example.com/"))

    def test_allowlist_includes_subdomains_but_not_suffix_confusions(self) -> None:
        self.assertTrue(host_is_allowed("https://docs.example.com/page", ["example.com"]))
        self.assertFalse(host_is_allowed("https://notexample.com/page", ["example.com"]))
