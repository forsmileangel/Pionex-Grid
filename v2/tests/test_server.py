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


class DashboardApiTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        import threading
        from pathlib import Path
        from unittest.mock import patch
        from http.server import ThreadingHTTPServer

        from v2 import server, store
        from v2.tests.test_store import FX, rec, snap

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        db = Path(self.tmp.name) / "ledger.sqlite"
        for p in [patch("v2.store.usdt_twd", return_value=FX),
                  patch("v2.store.BACKUP_DIR", Path(self.tmp.name) / "backups"),
                  patch("v2.server._write_log"),
                  patch("v2.gist_publish.DEFAULT_CREDS", Path(self.tmp.name) / "missing.txt"),
                  patch("v2.gist_publish.DEFAULT_DB", db)]:
            p.start()
            self.addCleanup(p.stop)
        store.ingest(snap("2026-09-18", [rec(GridProfit=100)]), db)
        store.ingest(snap("2026-09-19", [rec(ListStatus="finished", GridProfit=105)]), db)
        old_db = server.Handler.db_path
        server.Handler.db_path = db
        self.addCleanup(setattr, server.Handler, "db_path", old_db)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)

    def call(self, path, method="GET", body=None):
        import json
        import urllib.error
        import urllib.request
        url = f"http://127.0.0.1:{self.port}{path}"
        data = None if body is None else json.dumps(body).encode("utf-8")
        req = urllib.request.Request(url, data=data, method=method,
                                     headers={"Origin": f"http://127.0.0.1:{self.port}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as res:
                return res.status, json.loads(res.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            with exc:
                return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_status_reports_publish_state_and_settlement_counts(self):
        code, status = self.call("/api/v1/status")
        self.assertEqual(code, 200)
        self.assertFalse(status["publish"]["configured"])
        self.assertEqual(status["settlement_unconfirmed"], 1)
        self.assertIn("corrections", status)

    def test_efficiency_endpoints_and_saved_views(self):
        code, meta = self.call("/api/v1/efficiency/meta")
        self.assertEqual(code, 200)
        self.assertIn("g1", [p["id"] for p in meta["positions"]])
        code, data = self.call("/api/v1/efficiency?g=1d&from=2026-09-17T00:00:00%2B08:00&to=2026-09-20T00:00:00%2B08:00")
        self.assertEqual(code, 200)
        self.assertEqual(len(data["buckets"]), 3)
        self.assertIn("summary", data)
        code, err = self.call("/api/v1/efficiency?g=30m&from=2020-01-01T00:00:00%2B08:00")
        self.assertEqual(code, 400)
        self.assertTrue(err["suggest"])
        code, _ = self.call("/api/v1/efficiency/views", "POST",
                            {"views": [{"name": "主力", "settings": {"range": "7d", "granularity": "1h"}}], "default": "主力"})
        self.assertEqual(code, 200)
        code, views = self.call("/api/v1/efficiency/views")
        self.assertEqual((code, views["default"]), (200, "主力"))
        code, _ = self.call("/api/v1/efficiency/views", "POST", {"views": [{"name": "x" * 41}]})
        self.assertEqual(code, 400)
        code, spark = self.call("/api/v1/efficiency/sparklines?days=5")
        self.assertEqual(code, 200)
        self.assertIn("rows", spark)
        code, status = self.call("/api/v1/status")
        self.assertEqual(status["intraday"], {"available": False})

    def test_saved_views_post_requires_dashboard_origin(self):
        import urllib.error
        import urllib.request
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/v1/efficiency/views", data=b"{}", method="POST",
                                     headers={"Content-Type": "application/json"})
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=10)
        self.assertEqual(ctx.exception.code, 403)
        ctx.exception.close()

    def test_chart_script_is_served(self):
        import urllib.request
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/chart.js", timeout=10) as res:
            self.assertIn("javascript", res.headers["Content-Type"])
            self.assertIn(b"global.Charts", res.read())

    def test_module_assets_are_served_and_traversal_is_blocked(self):
        import http.client

        from v2.server import resolve_static

        def raw(path):
            conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
            conn.request("GET", path)
            res = conn.getresponse()
            body = res.read()
            conn.close()
            return res.status, res.headers, body

        status, headers, body = raw("/js/main.js")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "text/javascript; charset=utf-8")
        self.assertEqual(headers["Cache-Control"], "no-cache")
        self.assertIn(b"setRenderers", body)
        status, headers, body = raw("/js/pages/live.js")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "text/javascript; charset=utf-8")
        status, headers, body = raw("/css/app.css")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "text/css; charset=utf-8")
        self.assertEqual(headers["Cache-Control"], "no-cache")
        self.assertIn(b"--ink:", body)
        for path in ("/js/../server.py", "/js/..%2F..%2Fserver.py", "/css/x.py"):
            status, _, _ = raw(path)
            self.assertEqual(status, 404, path)
            self.assertIsNone(resolve_static(path))

    def test_manual_save_is_audited_and_republish_skips_without_credentials(self):
        code, _ = self.call("/api/v1/settlements/g1", "POST", {"own_cash_in": "1000", "note": "check"})
        self.assertEqual(code, 200)
        code, audit = self.call("/api/v1/settlements/g1/audit")
        self.assertEqual(code, 200)
        self.assertEqual(audit["rows"][0]["after"]["own_cash_in"], "1000")
        code, result = self.call("/api/v1/publish/ledger", "POST", {})
        self.assertEqual(code, 200)
        self.assertTrue(result["skipped"])


class PublishLiveLaterTests(unittest.TestCase):
    def test_older_board_finishing_late_is_skipped(self):
        from unittest.mock import patch
        import v2.server as server
        server._live_published["as_of"] = ""
        sent = []
        with patch("v2.gist_publish.publish_live", side_effect=lambda board: sent.append(board["as_of"])):
            server.publish_live_later({"as_of": "2026-10-13T22:00:00+08:00"}).join()
            server.publish_live_later({"as_of": "2026-10-13T21:30:00+08:00"}).join()
        self.assertEqual(sent, ["2026-10-13T22:00:00+08:00"])
        server._live_published["as_of"] = ""


class ClockStatusTests(unittest.TestCase):
    def test_reads_offset_from_live_snapshot(self):
        import json
        import tempfile
        from pathlib import Path
        from v2.server import clock_status
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "live-snapshot.json"
            path.write_text(json.dumps({"CapturedAt": "2026-10-15T15:00:00+08:00", "ClockOffsetMs": 9910}), encoding="utf-8")
            self.assertEqual(clock_status(path), {"offset_ms": 9910, "at": "2026-10-15T15:00:00+08:00"})

    def test_missing_snapshot_has_no_offset(self):
        import tempfile
        from pathlib import Path
        from v2.server import clock_status
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(clock_status(Path(tmp) / "none.json"), {"offset_ms": None, "at": None})

