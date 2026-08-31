import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from v2.gist_publish import (
    HOLDINGS_FILENAME,
    LEDGER_FILENAME,
    LIVE_FILENAME,
    live_publish_payload,
    publish_ledger,
    publish_live,
    read_publish_credentials,
)


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
        self.assertNotIn(HOLDINGS_FILENAME, captured["body"]["files"])
        self.assertNotIn("secret-token", json.dumps(result))

    def test_live_payload_is_compact(self):
        board = {
            "available": True,
            "as_of": "2026-08-31T12:00:00+08:00",
            "position_count": 1,
            "wallet_total": {"usdt": "1000", "twd": 31000},
            "total_profit_24h": {"usdt": "1.5"},
            "total_investment": {"usdt": "5000"},
            "total_grid_profit": {"usdt": "12.5"},
            "profit_24h_pct": 0.03,
            "rows": [{
                "symbol": "ETH",
                "leverage": "4x short",
                "trend": "short",
                "investment": {"usdt": "5000"},
                "grid_profit": {"usdt": "12.5"},
                "profit_24h": {"usdt": "1.5"},
                "mark_price": "2500",
                "liq_price": "1300",
                "raw_json": "secret",
            }],
        }
        payload = live_publish_payload(board)
        self.assertEqual(payload["schema"], 1)
        self.assertEqual(payload["source"], "pionex-grid-v2-live")
        dumped = json.dumps(payload)
        self.assertNotIn("raw_json", dumped)
        self.assertNotIn("secret", dumped)
        self.assertEqual(payload["rows"][0]["symbol"], "ETH")
        self.assertEqual(payload["wallet"]["twd"], 31000)

    def test_patch_only_live_file(self):
        captured = {}

        class FakeResponse:
            def read(self):
                return json.dumps({"updated_at": "2026-08-31T00:00:00Z"}).encode("utf-8")

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        def fake_urlopen(req, timeout=0):
            captured["body"] = json.loads(req.data.decode("utf-8"))
            return FakeResponse()

        board = {
            "available": True,
            "as_of": "2026-08-31T12:00:00+08:00",
            "position_count": 1,
            "rows": [{"symbol": "ETH", "investment": {"usdt": "1"}}],
        }
        with tempfile.TemporaryDirectory() as tmp:
            creds = Path(tmp) / "GIST PUBLISH.txt"
            creds.write_text("abc123\nsecret-token\n", encoding="utf-8")
            with patch("v2.gist_publish.urllib.request.urlopen", side_effect=fake_urlopen):
                result = publish_live(board, creds_path=creds)
        self.assertTrue(result["ok"])
        self.assertEqual(list(captured["body"]["files"].keys()), [LIVE_FILENAME])
        self.assertNotIn(LEDGER_FILENAME, captured["body"]["files"])
        self.assertNotIn(HOLDINGS_FILENAME, captured["body"]["files"])
