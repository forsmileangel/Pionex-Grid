"""Batch 2 hardening: locks and atomic files, server Host checks, fx retry pause,
the slimmer Gist ledger, GitHub quota logging and the Excel export upkeep."""

import email.message
import io
import json
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from v2.tests.test_store import FX, rec, snap


class FileIoTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "live-snapshot.json"

    def test_atomic_write_replaces_the_file_and_leaves_no_temp_file(self):
        from v2.fileio import atomic_write_text
        atomic_write_text(self.path, "old")
        atomic_write_text(self.path, "new")
        self.assertEqual(self.path.read_text(encoding="utf-8"), "new")
        self.assertEqual([p.name for p in Path(self.tmp.name).iterdir()], ["live-snapshot.json"])

    def test_lock_keeps_out_a_second_holder_until_released(self):
        from v2.fileio import LockTimeout, file_lock
        with file_lock(self.path):
            with self.assertRaises(LockTimeout):
                with file_lock(self.path, timeout=0):
                    pass
        with file_lock(self.path, timeout=0):
            pass

    def test_lock_works_across_processes(self):
        from v2.fileio import LockTimeout, file_lock
        holder = subprocess.Popen(
            [sys.executable, "-c",
             "import sys, time; from pathlib import Path; from v2.fileio import file_lock\n"
             "with file_lock(Path(sys.argv[1])):\n    print('held', flush=True); time.sleep(3)",
             str(self.path)],
            stdout=subprocess.PIPE, text=True, cwd=str(Path(__file__).resolve().parents[2]))
        try:
            self.assertEqual(holder.stdout.readline().strip(), "held")
            with self.assertRaises(LockTimeout):
                with file_lock(self.path, timeout=0):
                    pass
        finally:
            holder.wait(timeout=10)
            holder.stdout.close()
        with file_lock(self.path, timeout=0):
            pass

    def test_live_snapshot_is_saved_whole(self):
        from v2.live import load_live_snapshot, save_live_snapshot
        save_live_snapshot({"CapturedAt": "2026-10-26T09:00:00+08:00", "Records": []}, self.path)
        self.assertEqual(load_live_snapshot(self.path)["CapturedAt"], "2026-10-26T09:00:00+08:00")
        self.assertEqual(sorted(p.name for p in Path(self.tmp.name).iterdir()),
                         ["live-snapshot.json", "live-snapshot.json.lock"])


class FxRetryPauseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cache = Path(self.tmp.name) / "usdt-twd.json"
        p = patch("v2.fx.CACHE", self.cache)
        p.start()
        self.addCleanup(p.stop)

    def test_failed_fetch_is_not_retried_on_every_request(self):
        from v2 import fx
        with patch("v2.fx._fetch_live", side_effect=OSError("bank down")) as fetch:
            self.assertIsNone(fx.usdt_twd()["usdt_twd"])
            self.assertIsNone(fx.usdt_twd()["usdt_twd"])
        self.assertEqual(fetch.call_count, 1)

    def test_failed_fetch_keeps_the_previous_rate(self):
        from v2 import fx
        self.cache.write_text(json.dumps({"usdt_twd": 32.06, "fetched_at": 0}), encoding="utf-8")
        with patch("v2.fx._fetch_live", side_effect=OSError("bank down")) as fetch:
            self.assertEqual(fx.usdt_twd()["usdt_twd"], 32.06)
            self.assertEqual(fx.usdt_twd()["usdt_twd"], 32.06)
        self.assertEqual(fetch.call_count, 1)

    def test_fetches_again_after_the_pause(self):
        from v2 import fx
        self.cache.write_text(json.dumps({"usdt_twd": 32.06, "fetched_at": 0,
                                          "failed_at": time.time() - fx.RETRY_SECONDS - 1}), encoding="utf-8")
        fresh = dict(FX, usdt_twd=32.1, fetched_at=time.time())
        with patch("v2.fx._fetch_live", return_value=fresh) as fetch:
            self.assertEqual(fx.usdt_twd()["usdt_twd"], 32.1)
        fetch.assert_called_once()
        self.assertNotIn("failed_at", json.loads(self.cache.read_text(encoding="utf-8")))


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "ledger.sqlite"

    def ingest_days(self, days):
        from v2.store import ingest
        with patch("v2.store.usdt_twd", return_value=FX):
            for i, day in enumerate(days):
                ingest(snap(day, [rec(GridProfit=10 + i)]), self.db)

    def test_rate_is_fetched_before_the_write_transaction(self):
        from v2.store import ingest
        self.ingest_days(["2026-09-01"])

        def rate_while_another_writer_can_still_begin():
            other = sqlite3.connect(self.db, timeout=0)
            try:
                other.execute("BEGIN IMMEDIATE")
                other.rollback()
            finally:
                other.close()
            return FX

        with patch("v2.store.usdt_twd", side_effect=rate_while_another_writer_can_still_begin):
            self.assertEqual(ingest(snap("2026-09-02", [rec(GridProfit=11)]), self.db)["status"], "success")

    def test_position_indexes_exist(self):
        from v2.store import connect
        self.ingest_days(["2026-09-01"])
        con = connect(self.db)
        try:
            names = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        finally:
            con.close()
        self.assertTrue({"idx_grid_snapshots_order", "idx_daily_grid_profit_order"} <= names)

    def test_gist_rows_cover_the_latest_days_and_day_totals_do_not_change(self):
        from v2.store import ledger_publish_payload
        self.ingest_days(["2026-09-01", "2026-09-02", "2026-09-03"])
        with patch("v2.store.usdt_twd", return_value=FX):
            full = ledger_publish_payload(self.db)
            with patch("v2.store.LEDGER_ROWS_DAYS", 2):
                slim = ledger_publish_payload(self.db)
        self.assertEqual([("rows" in d, d.get("rows_trimmed")) for d in slim["days"]],
                         [(True, None), (True, None), (False, True)])
        totals = lambda d: {k: v for k, v in d.items() if k not in ("rows", "rows_trimmed")}
        self.assertEqual([totals(d) for d in slim["days"]], [totals(d) for d in full["days"]])
        row = full["days"][0]["rows"][0]
        self.assertEqual((row["id"], row["investment_usdt"], row["daily_profit_usdt"]), ("g1", "1000.00", "1.0000"))
        self.assertEqual(full["days"][0]["daily_profit_usdt"], "1")


def _headers(**values):
    msg = email.message.Message()
    for key, value in values.items():
        msg[key.replace("_", "-")] = value
    return msg


class GistRateLogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.creds = Path(self.tmp.name) / "GIST PUBLISH.txt"
        self.creds.write_text("abc123\nsecret-token\n", encoding="utf-8")
        self.reset = int(time.time()) + 900

    def publish(self, urlopen):
        from v2.gist_publish import LIVE_FILENAME, patch_gist_file
        with patch("v2.gist_publish.urllib.request.urlopen", side_effect=urlopen):
            return patch_gist_file(LIVE_FILENAME, {"schema": 1}, creds_path=self.creds)

    def log(self):
        return (Path(self.tmp.name) / "gist-rate.log").read_text(encoding="utf-8")

    def http_error(self, message, **headers):
        body = io.BytesIO(json.dumps({"message": message}).encode("utf-8"))
        return urllib.error.HTTPError("https://api.github.com/gists/abc123", 403, "Forbidden", _headers(**headers), body)

    def test_success_records_the_remaining_quota(self):
        reset = self.reset

        class Response:
            status = 200
            headers = _headers(X_RateLimit_Remaining="93", X_RateLimit_Limit="100",
                               X_RateLimit_Resource="gist_write", X_RateLimit_Reset=str(reset))

            def read(self):
                return b'{"updated_at": "2026-10-26T01:00:00Z"}'

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        result = self.publish(lambda req, timeout=0: Response())
        self.assertEqual((result["ok"], result["rate_remaining"], result["rate_limit"]), (True, 93, 100))
        self.assertNotIn("rate_limited", result)
        # The limit and bucket come from GitHub's headers, not a fixed 5000.
        self.assertIn("pionex-grid-live.json ok rate 93/100 gist_write reset", self.log())
        state = json.loads((Path(self.tmp.name) / "publish-state.json").read_text(encoding="utf-8"))
        entry = state["pionex-grid-live.json"]
        self.assertEqual((entry["rate_remaining"], entry["rate_limit"], entry["rate_resource"]), (93, 100, "gist_write"))

    def test_exhausted_quota_is_flagged_as_a_rate_limit(self):
        def urlopen(req, timeout=0):
            raise self.http_error("API rate limit exceeded for user ID 1.",
                                  X_RateLimit_Remaining="0", X_RateLimit_Reset=str(self.reset))
        result = self.publish(urlopen)
        self.assertFalse(result["ok"])
        self.assertTrue(result["rate_limited"])
        self.assertIn("fail 403 rate-limited until", self.log())

    def test_permission_403_is_not_a_rate_limit(self):
        def urlopen(req, timeout=0):
            raise self.http_error("Resource not accessible by personal access token",
                                  X_RateLimit_Remaining="4990", X_RateLimit_Reset=str(self.reset))
        result = self.publish(urlopen)
        self.assertFalse(result["ok"])
        self.assertNotIn("rate_limited", result)
        self.assertIn("fail 403 rate 4990/? reset", self.log())


class ServerHardeningTests(unittest.TestCase):
    def setUp(self):
        import threading
        from http.server import ThreadingHTTPServer

        from v2 import server
        from v2.store import ingest

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        db = root / "ledger.sqlite"
        self.log = []
        for p in [patch("v2.store.usdt_twd", return_value=FX),
                  patch("v2.server._write_log", side_effect=self.log.append),
                  patch("v2.server.EXPORTS", root / "exports"),
                  patch("v2.gist_publish.DEFAULT_CREDS", root / "missing.txt"),
                  patch("v2.gist_publish.DEFAULT_DB", db)]:
            p.start()
            self.addCleanup(p.stop)
        ingest(snap("2026-09-18", [rec(GridProfit=100)]), db)
        old_db = server.Handler.db_path
        server.Handler.db_path = db
        self.addCleanup(setattr, server.Handler, "db_path", old_db)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)

    def request(self, path, host=None, method="GET", origin=None):
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        try:
            conn.putrequest(method, path, skip_host=True)
            conn.putheader("Host", host or f"127.0.0.1:{self.port}")
            if origin:
                conn.putheader("Origin", origin)
            conn.putheader("Content-Length", "0")
            conn.endheaders()
            res = conn.getresponse()
            res.read()
            return res.status
        finally:
            conn.close()

    def test_a_page_on_another_host_name_is_refused(self):
        # DNS rebinding: evil.example resolves to 127.0.0.1 but the browser still sends its host name.
        self.assertEqual(self.request("/api/v1/status", host=f"evil.example:{self.port}"), 403)
        self.assertEqual(self.request("/api/v1/status"), 200)
        self.assertEqual(self.request("/api/v1/status", host=f"localhost:{self.port}"), 200)
        own = f"http://127.0.0.1:{self.port}"
        self.assertEqual(self.request("/api/v1/publish/ledger", host=f"evil.example:{self.port}",
                                      method="POST", origin=own), 403)

    def test_export_writes_one_fixed_file_whatever_name_is_asked(self):
        self.assertEqual(self.request("/api/v1/export.xlsx?name=..%2F..%2Fevil.cmd"), 200)
        exports = Path(self.tmp.name) / "exports"
        self.assertEqual([p.name for p in exports.iterdir()], ["pionex-grid-export.xlsx"])

    def test_a_500_answer_logs_its_reason(self):
        with patch("v2.server.capture_live", side_effect=RuntimeError("collector exploded")):
            status = self.request("/api/v1/live/refresh", method="POST", origin=f"http://127.0.0.1:{self.port}")
        self.assertEqual(status, 500)
        self.assertTrue(any("/api/v1/live/refresh 500: RuntimeError: collector exploded" in line for line in self.log))

    def test_pid_file_records_this_process(self):
        import os
        from v2 import server
        target = Path(self.tmp.name) / "dashboard-server-8799.pid"
        with patch("v2.server.pid_file", return_value=target), patch("v2.server.atexit.register"):
            server._record_pid(8799)
        self.assertEqual(target.read_text(encoding="ascii"), str(os.getpid()))


class ExcelUpkeepTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.db = root / "ledger.sqlite"
        con = sqlite3.connect(self.db)
        con.execute("CREATE TABLE daily_summary (capture_date TEXT)")
        con.execute("INSERT INTO daily_summary(capture_date) VALUES ('2026-10-27')")
        con.commit()
        con.close()
        self.data = root / "legacy" / "data"
        self.data.mkdir(parents=True)
        self.state = root / "pipeline-state.json"
        self.state.write_text(json.dumps({"legacy_excel": "2026-10-26", "daily_report": "2026-10-27"}), encoding="utf-8")
        self.calls = []
        # Never the real legacy folder: pruning and exports point at the temp copy.
        for p in [patch("v2.pipeline.PRODUCTION_HISTORY", self.data / "pionex-grid-history.xlsx"),
                  patch("v2.pipeline.STATE_PATH", self.state),
                  patch("v2.pipeline.LOG_PATH", root / "pipeline.log"),
                  patch("v2.pipeline._outside_production", return_value=False),
                  patch("v2.pipeline.ledger_rows"),
                  patch("v2.pipeline.export_report"),
                  patch("v2.pipeline._automation_excel_pids", return_value=set()),
                  patch("v2.pipeline.subprocess.run", side_effect=self.fake_run)]:
            p.start()
            self.addCleanup(p.stop)
        self.excel_result = subprocess.CompletedProcess([], 0, stdout="", stderr="")

    def fake_run(self, cmd, **kwargs):
        self.calls.append(cmd)
        if cmd[0] == "taskkill":
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if isinstance(self.excel_result, Exception):
            raise self.excel_result
        return self.excel_result

    def at(self, hhmm):
        from v2.capture import TAIPEI
        h, m = map(int, hhmm.split(":"))
        return datetime(2026, 10, 27, h, m, tzinfo=TAIPEI)

    def test_catch_up_exports_the_workbook_the_daily_run_missed(self):
        from v2.pipeline import catch_up_exports
        result = catch_up_exports(self.db, self.at("09:00"))
        self.assertEqual(result, {"legacy_excel": "ok"})
        self.assertEqual(json.loads(self.state.read_text(encoding="utf-8"))["legacy_excel"], "2026-10-27")
        self.assertTrue(catch_up_exports(self.db, self.at("09:30"))["skipped"])

    def test_catch_up_waits_for_the_daily_task_and_todays_ledger(self):
        from v2.pipeline import catch_up_exports
        self.assertIn("still be running", catch_up_exports(self.db, self.at("08:40"))["reason"])
        from v2.capture import TAIPEI
        tomorrow = datetime(2026, 10, 28, 9, 0, tzinfo=TAIPEI)
        self.assertIn("not ready", catch_up_exports(self.db, tomorrow)["reason"])
        self.assertEqual(self.calls, [])

    def test_catch_up_skips_while_another_export_holds_the_lock(self):
        from v2.fileio import file_lock
        from v2.pipeline import catch_up_exports
        with file_lock(self.state.parent / "excel-export"):
            self.assertIn("another export", catch_up_exports(self.db, self.at("09:00"))["reason"])
        self.assertEqual(self.calls, [])

    def test_only_the_newest_fourteen_excel_backups_are_kept(self):
        from v2.pipeline import catch_up_exports
        names = [f"snapshot-legacy-excel-202610{d:02d}-081202-000.xlsx" for d in range(1, 21)]
        for name in names:
            (self.data / name).write_bytes(b"x")
        (self.data / "pionex-grid-history.xlsx").write_bytes(b"workbook")
        catch_up_exports(self.db, self.at("09:00"))
        left = sorted(p.name for p in self.data.glob("snapshot-legacy-excel-*.xlsx"))
        self.assertEqual(left, names[-14:])
        self.assertTrue((self.data / "pionex-grid-history.xlsx").exists())

    def test_hung_export_also_stops_the_excel_it_started(self):
        from v2.pipeline import catch_up_exports
        self.excel_result = subprocess.TimeoutExpired("powershell.exe", 600)
        with patch("v2.pipeline._automation_excel_pids", side_effect=[{111}, {111, 222}]):
            self.assertEqual(catch_up_exports(self.db, self.at("09:00")), {"legacy_excel": "failed"})
        self.assertIn(["taskkill", "/F", "/PID", "222"], self.calls)
        self.assertNotIn(["taskkill", "/F", "/PID", "111"], self.calls)
        log = (Path(self.tmp.name) / "pipeline.log").read_text(encoding="utf-8")
        self.assertIn("pid 222", log)


if __name__ == "__main__":
    unittest.main()
