import unittest

from v2.server import local_post_allowed


class LocalPostCsrfTests(unittest.TestCase):
    def test_dashboard_origin_allowed(self):
        self.assertTrue(local_post_allowed("http://127.0.0.1:8787", "", 8787))
        self.assertTrue(local_post_allowed("http://localhost:8787", "", 8787))
        self.assertTrue(local_post_allowed("http://127.0.0.1:8787/", "", 8787))

    def test_other_website_blocked(self):
        self.assertFalse(local_post_allowed("https://evil.example", "", 8787))
        self.assertFalse(local_post_allowed("http://127.0.0.1:8787", "", 9999))
        self.assertFalse(local_post_allowed("", "", 8787))

    def test_referer_fallback(self):
        self.assertTrue(local_post_allowed("", "http://127.0.0.1:8787/", 8787))
        self.assertFalse(local_post_allowed("", "https://evil.example/page", 8787))
