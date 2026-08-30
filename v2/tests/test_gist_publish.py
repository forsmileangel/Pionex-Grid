import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from v2.gist_publish import LEDGER_FILENAME, publish_ledger, read_publish_credentials


class GistPublishTests(unittest.TestCase):
    def test_missing_file_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = publish_ledger(creds_path=Path(tmp) / "missing.txt")
        self.assertFalse(result["ok"])
        self.assertTrue(result["skipped"])
        self.assertEqual(result["reason"], "missing GIST PUBLISH.txt")

    def test_two_line_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "GIST PUBLISH.txt"
            path.write_text("# comment\ngistid123\ntoken456\n", encoding="utf-8")
            self.assertEqual(read_publish_credentials(path), ("gistid123", "token456"))

    def test_patch_only_ledger_file(self):
        captured = {}

        class FakeResponse:
            def read(self):
                return json.dumps({"updated_at": "2026-08-26T00:00:00Z"}).encode("utf-8")

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        def fake_urlopen(req, timeout=0):
            captured["url"] = req.full_url
            captured["method"] = req.get_method()
            captured["body"] = json.loads(req.data.decode("utf-8"))
            captured["auth"] = req.headers.get("Authorization")
            return FakeResponse()

        with tempfile.TemporaryDirectory() as tmp:
            creds = Path(tmp) / "GIST PUBLISH.txt"
            creds.write_text("abc123\nsecret-token\n", encoding="utf-8")
            with patch("v2.gist_publish.ledger_publish_payload", return_value={
                "schema": 1,
                "capture_date": "2026-08-26",
                "days": [{"date": "2026-08-26"}],
            }), patch("v2.gist_publish.urllib.request.urlopen", side_effect=fake_urlopen):
                result = publish_ledger(creds_path=creds)
        self.assertTrue(result["ok"])
        self.assertEqual(captured["method"], "PATCH")
        self.assertEqual(list(captured["body"]["files"].keys()), [LEDGER_FILENAME])
        self.assertNotIn("portfolio-tracker-holdings.json", captured["body"]["files"])
        self.assertNotIn("secret-token", json.dumps(result))
