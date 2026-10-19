import os
import subprocess
import unittest

from v2.capture import _collector_run_kwargs


class CaptureRunnerTests(unittest.TestCase):
    def test_windows_collector_hides_console(self):
        if os.name != "nt":
            self.skipTest("Windows-only console flag")
        kwargs = _collector_run_kwargs()
        self.assertEqual(kwargs["creationflags"], getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))
        self.assertTrue(kwargs["startupinfo"].dwFlags & subprocess.STARTF_USESHOWWINDOW)


class CatchUpTests(unittest.TestCase):
    def test_logon_catch_up_only_when_today_is_missing_after_0805(self):
        import tempfile
        from datetime import datetime
        from pathlib import Path
        from unittest.mock import patch

        from v2 import store
        from v2.capture import TAIPEI, daily_capture_due
        from v2.tests.test_store import FX, rec, snap

        with tempfile.TemporaryDirectory() as tmp, patch("v2.store.usdt_twd", return_value=FX), \
                patch("v2.store.BACKUP_DIR", Path(tmp) / "backups"):
            db = Path(tmp) / "ledger.sqlite"
            early = datetime(2026, 10, 4, 7, 50, tzinfo=TAIPEI)
            late = datetime(2026, 10, 4, 8, 39, tzinfo=TAIPEI)
            self.assertFalse(daily_capture_due(db, early)[0])
            self.assertTrue(daily_capture_due(db, late)[0])
            store.ingest(snap("2026-10-04", [rec(GridProfit=100)]), db)
            self.assertFalse(daily_capture_due(db, late)[0])


class FinishedCacheTests(unittest.TestCase):
    """Closed bots are remembered locally so the live refresh does not fetch each one again."""

    def setUp(self):
        import tempfile
        from pathlib import Path
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.cache = self.dir / "finished-details.json"
        self.creds = self.dir / "creds.txt"
        self.creds.write_text("k\ns\n", encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    @staticmethod
    def finished(oid, ts=1, **extra):
        raw = {"list": {"buOrderId": oid, "closeTime": 1000, "buOrderData": {"lastFundingFeeTs": ts}},
               "order": {"buOrderData": {"gridProfit": "5", "lastFundingFeeTs": ts}}}
        return {"ApiOrderId": oid, "ListStatus": "finished", "GridProfit": 5.0, "RawJson": raw, **extra}

    @staticmethod
    def snapshot(at, *records):
        running = {"ApiOrderId": "run-1", "ListStatus": "running", "GridProfit": 1.0}
        return {"CapturedAt": at, "Records": [running, *records]}

    def read(self):
        import json
        return json.loads(self.cache.read_text(encoding="utf-8"))

    def test_full_capture_remembers_closed_bots_only(self):
        from v2.capture import _update_finished_cache
        result = _update_finished_cache(self.snapshot("2026-10-13T08:05:00+08:00", self.finished("a")), self.cache, full=True)
        self.assertTrue(result["ok"])
        data = self.read()
        self.assertEqual(list(data["records"]), ["a"])
        self.assertEqual(data["records"]["a"]["fetched_at"], "2026-10-13T08:05:00+08:00")
        self.assertEqual(data["last_full_check"], {"at": "2026-10-13T08:05:00+08:00", "fetched": 1, "checked": 0, "changed": 0})

    def test_reused_record_keeps_its_fetch_time(self):
        from v2.capture import _update_finished_cache
        _update_finished_cache(self.snapshot("2026-10-12T08:05:00+08:00", self.finished("a")), self.cache, full=True)
        reused = self.finished("a", DetailSource="cache", DetailFetchedAt="2026-10-12T08:05:00+08:00")
        result = _update_finished_cache(self.snapshot("2026-10-13T21:30:00+08:00", reused, self.finished("b")), self.cache, full=False)
        self.assertEqual((result["fetched"], result["reused"]), (1, 1))
        data = self.read()
        self.assertEqual(data["records"]["a"]["fetched_at"], "2026-10-12T08:05:00+08:00")
        self.assertNotIn("DetailSource", data["records"]["a"]["record"])
        self.assertEqual(data["records"]["b"]["fetched_at"], "2026-10-13T21:30:00+08:00")
        self.assertEqual(data["last_reuse"]["reused"], 1)
        self.assertEqual(data["last_full_check"]["at"], "2026-10-12T08:05:00+08:00")

    def test_full_check_counts_changes_but_ignores_funding_timestamp(self):
        from v2.capture import _update_finished_cache
        _update_finished_cache(self.snapshot("2026-10-12T08:05:00+08:00", self.finished("a"), self.finished("b")), self.cache, full=True)
        moved = self.finished("b")
        moved["RawJson"]["order"]["buOrderData"]["gridProfit"] = "6"
        result = _update_finished_cache(
            self.snapshot("2026-10-13T08:05:00+08:00", self.finished("a", ts=99), moved), self.cache, full=True)
        self.assertEqual(result["changed"], 1)
        check = self.read()["last_full_check"]
        self.assertEqual((check["checked"], check["changed"]), (2, 1))
        self.assertEqual(self.read()["records"]["b"]["record"]["RawJson"]["order"]["buOrderData"]["gridProfit"], "6")

    def test_broken_cache_never_breaks_a_capture(self):
        from unittest.mock import patch
        from v2.capture import _update_finished_cache, finished_cache_status
        self.cache.write_text("{not json", encoding="utf-8")
        self.assertEqual(finished_cache_status(self.cache), {"available": False})
        result = _update_finished_cache(self.snapshot("2026-10-13T08:05:00+08:00", self.finished("a")), self.cache, full=True)
        self.assertTrue(result["ok"])
        self.assertEqual(finished_cache_status(self.cache)["count"], 1)
        with patch("v2.capture.os.replace", side_effect=PermissionError("locked")):
            result = _update_finished_cache(self.snapshot("2026-10-13T09:00:00+08:00", self.finished("b")), self.cache, full=False)
        self.assertFalse(result["ok"])
        self.assertEqual(list(self.read()["records"]), ["a"])
        self.assertEqual([p.name for p in self.dir.iterdir() if p.suffix == ".tmp"], [])

    def _run_collect(self, reuse_finished):
        import json
        from unittest.mock import patch
        from v2.capture import collect_snapshot
        seen = {}

        def fake_run(cmd, **kwargs):
            seen["cmd"] = cmd
            out = cmd[cmd.index("--output") + 1]
            with open(out, "w", encoding="utf-8") as fh:
                json.dump(self.snapshot("2026-10-13T21:30:00+08:00", self.finished("a")), fh)
            return subprocess.CompletedProcess(cmd, 0, "", "")

        with patch("v2.capture.subprocess.run", side_effect=fake_run):
            snapshot = collect_snapshot(self.creds, reuse_finished=reuse_finished, cache_path=self.cache)
        return seen["cmd"], snapshot

    def test_only_live_refresh_passes_the_cache_to_the_collector(self):
        cmd, snapshot = self._run_collect(reuse_finished=True)
        self.assertNotIn("--finished-cache", cmd)  # no cache yet
        self.assertTrue(snapshot["FinishedCache"]["ok"])
        cmd, _ = self._run_collect(reuse_finished=True)
        self.assertEqual(cmd[cmd.index("--finished-cache") + 1], str(self.cache))
        cmd, snapshot = self._run_collect(reuse_finished=False)
        self.assertNotIn("--finished-cache", cmd)
        self.assertEqual(self.read()["last_full_check"]["checked"], 1)


class ListModeTests(FinishedCacheTests):
    """The running list can stand in for the detail only while it leaves nothing out."""

    @staticmethod
    def running(listed, detail):
        raw = {"list": {"buOrderData": listed}, "order": {"buOrderData": detail}}
        return {"Symbol": "SYM", "ApiOrderId": "r", "ListStatus": "running", "RawJson": raw}

    def check(self, listed, detail):
        from v2.capture import _list_detail_check
        return _list_detail_check({"Records": [self.running(listed, detail)]})

    def test_list_check_only_flags_values_the_list_leaves_out(self):
        self.assertEqual(self.check({"profitWithdrawn": None}, {"profitWithdrawn": "0"})["missing"], 0)
        self.assertEqual(self.check({"x": None}, {"x": []})["missing"], 0)
        self.assertEqual(self.check({"gridProfit": "5"}, {"gridProfit": "6"})["missing"], 0)
        flagged = self.check({"profitWithdrawn": None}, {"profitWithdrawn": "12.5"})
        self.assertEqual((flagged["checked"], flagged["missing"]), (1, 1))
        self.assertEqual(flagged["fields"], ["SYM:profitWithdrawn"])

    def collect_cmd(self, running_from_list, reuse=True):
        import json
        from unittest.mock import patch
        from v2.capture import collect_snapshot
        seen = {}

        def fake_run(cmd, **kwargs):
            seen["cmd"] = cmd
            snap = self.snapshot("2026-10-13T21:30:00+08:00", self.finished("a"))
            if "--running-from-list" in cmd:
                snap["RunningSource"] = "list"
            with open(cmd[cmd.index("--output") + 1], "w", encoding="utf-8") as fh:
                json.dump(snap, fh)
            return subprocess.CompletedProcess(cmd, 0, "", "")

        with patch("v2.capture.subprocess.run", side_effect=fake_run):
            snapshot = collect_snapshot(self.creds, reuse_finished=reuse, cache_path=self.cache,
                                        running_from_list=running_from_list)
        return seen["cmd"], snapshot

    def test_fast_flag_follows_the_daily_check(self):
        import json
        cmd, _ = self.collect_cmd(True)
        self.assertIn("--running-from-list", cmd)
        cmd, _ = self.collect_cmd(False)
        self.assertNotIn("--running-from-list", cmd)
        data = self.read()
        data["last_list_check"] = {"at": "x", "checked": 16, "missing": 2, "fields": ["A:b"]}
        self.cache.write_text(json.dumps(data), encoding="utf-8")
        cmd, _ = self.collect_cmd(True)
        self.assertNotIn("--running-from-list", cmd)

    def test_full_capture_records_check_and_fast_capture_keeps_it(self):
        self.collect_cmd(False, reuse=False)
        first = self.read()["last_list_check"]
        self.assertEqual((first["checked"], first["missing"]), (0, 0))
        self.collect_cmd(True)
        self.assertEqual(self.read()["last_list_check"], first)
