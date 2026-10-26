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
            early = datetime(2026, 10, 4, 8, 10, tzinfo=TAIPEI)
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


class CollectorTimeoutTests(FinishedCacheTests):
    def test_stuck_collector_is_stopped_and_reported(self):
        from unittest.mock import patch
        from v2.capture import COLLECTOR_TIMEOUT_SECONDS, collect_snapshot
        from v2.store import CaptureError
        seen = {}

        def stuck(cmd, **kwargs):
            seen.update(kwargs)
            raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])

        with patch("v2.capture.subprocess.run", side_effect=stuck), self.assertRaises(CaptureError) as caught:
            collect_snapshot(self.creds, cache_path=self.cache)
        self.assertEqual(seen["timeout"], COLLECTOR_TIMEOUT_SECONDS)
        self.assertIn("did not finish", str(caught.exception))


class DailyCaptureGuardTests(unittest.TestCase):
    """capture() must not quietly move today's cut-off or drop positions the API left out."""

    def setUp(self):
        import tempfile
        from datetime import datetime
        from pathlib import Path
        from unittest.mock import patch
        from v2.capture import TAIPEI
        from v2.tests.test_store import FX
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db = Path(self._tmp.name) / "ledger.sqlite"
        self.today = datetime.now(TAIPEI).date()
        self.collect = patch("v2.capture.collect_snapshot").start()
        self.intraday = patch("v2.capture.record_intraday", return_value={"ok": True}).start()
        patch("v2.store.usdt_twd", return_value=FX).start()
        self.addCleanup(patch.stopall)

    def snap_at(self, day, hhmm, records):
        from v2.tests.test_store import snap
        data = snap(day.isoformat(), records)
        data["CapturedAt"] = f"{day.isoformat()}T{hhmm}:00+08:00"
        return data

    def seed(self, hhmm, records, day=None):
        from v2.store import ingest
        ingest(self.snap_at(day or self.today, hhmm, records), self.db)

    def run_capture(self, **kwargs):
        from v2.capture import capture
        return capture(self._tmp.name, self.db, skip_publish=True, **kwargs)

    def rows(self, day):
        from v2.store import connect
        con = connect(self.db)
        try:
            return {r["bu_order_id"]: r["status"] for r in con.execute(
                "SELECT bu_order_id, status FROM daily_grid_profit WHERE capture_date=?", (day.isoformat(),))}
        finally:
            con.close()

    def test_second_run_after_the_cutoff_skips_without_calling_the_api(self):
        from v2.tests.test_store import rec
        self.seed("08:15", [rec(GridProfit=10)])
        result = self.run_capture()
        self.assertTrue(result["skipped"])
        self.assertIn("--replace-date", result["reason"])
        self.collect.assert_not_called()

    def test_record_taken_before_the_cutoff_is_replaced(self):
        from v2.tests.test_store import rec
        # 08:05 was the cut-off until 2026-10-26; a record from then is now before the 08:15 cut-off.
        self.seed("08:05", [rec(GridProfit=10)])
        self.collect.return_value = self.snap_at(self.today, "08:15", [rec(GridProfit=12)])
        result = self.run_capture()
        self.assertEqual(result["status"], "success")
        self.collect.assert_called_once()

    def test_position_missing_from_both_lists_stops_the_capture(self):
        from datetime import timedelta
        from v2.store import CaptureError
        from v2.tests.test_store import rec
        yesterday = self.today - timedelta(days=1)
        self.seed("08:05", [rec(ApiOrderId="g1"), rec(ApiOrderId="g2", Key="ETH|x")], day=yesterday)
        self.collect.return_value = self.snap_at(self.today, "08:05", [rec(ApiOrderId="g1")])
        with self.assertRaises(CaptureError) as caught:
            self.run_capture()
        self.assertIn("g2", str(caught.exception))
        self.assertEqual(self.rows(self.today), {})
        self.intraday.assert_not_called()

    def test_closed_position_in_the_finished_list_is_accepted(self):
        from datetime import timedelta
        from v2.tests.test_store import rec
        yesterday = self.today - timedelta(days=1)
        self.seed("08:05", [rec(ApiOrderId="g1"), rec(ApiOrderId="g2", Key="ETH|x")], day=yesterday)
        self.collect.return_value = self.snap_at(self.today, "08:05", [
            rec(ApiOrderId="g1"), rec(ApiOrderId="g2", Key="ETH|x", ListStatus="finished")])
        self.assertEqual(self.run_capture()["status"], "success")
        self.assertEqual(self.rows(self.today)["g2"], "closed")

    def test_allow_missing_marks_the_position_unresolved(self):
        from datetime import timedelta
        from v2.tests.test_store import rec
        yesterday = self.today - timedelta(days=1)
        self.seed("08:05", [rec(ApiOrderId="g1"), rec(ApiOrderId="g2", Key="ETH|x")], day=yesterday)
        self.collect.return_value = self.snap_at(self.today, "08:05", [rec(ApiOrderId="g1")])
        self.run_capture(allow_missing=True)
        self.assertEqual(self.rows(self.today)["g2"], "closed_unresolved")


class NodePathTests(unittest.TestCase):
    """PIONEX_NODE_PATH, then PATH, then the Codex cache, then the name node."""

    CODEX = r"C:\Users\Forever you\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe"
    WHICH = r"D:\Program Files\nodejs\node.exe"
    ENV = r"D:\custom\node.exe"

    def resolve(self, env, which, present):
        from unittest.mock import patch

        from v2.capture import node_path

        with patch.dict(os.environ, {}, clear=False):
            if env is None:
                os.environ.pop("PIONEX_NODE_PATH", None)
            else:
                os.environ["PIONEX_NODE_PATH"] = env
            with patch("v2.capture.shutil.which", return_value=which) as which_mock, \
                    patch("v2.capture.Path.exists", lambda self: str(self) in present), \
                    patch("v2.capture.Path.resolve", lambda self: self):
                return node_path(), which_mock

    def test_env_path_wins_when_the_file_exists(self):
        found, which_mock = self.resolve(self.ENV, self.WHICH, {self.ENV, self.CODEX})
        self.assertEqual(found, self.ENV)
        which_mock.assert_not_called()

    def test_which_wins_when_env_is_unset_or_missing(self):
        found, which_mock = self.resolve(None, self.WHICH, {self.CODEX})
        self.assertEqual(found, self.WHICH)
        which_mock.assert_called_once_with("node")
        found, which_mock = self.resolve(self.ENV, self.WHICH, {self.CODEX})
        self.assertEqual(found, self.WHICH)
        which_mock.assert_called_once_with("node")

    def test_codex_cache_wins_when_which_finds_nothing(self):
        found, which_mock = self.resolve(None, None, {self.CODEX})
        self.assertEqual(found, self.CODEX)
        which_mock.assert_called_once_with("node")

    def test_plain_node_when_nothing_is_available(self):
        found, which_mock = self.resolve(None, None, set())
        self.assertEqual(found, "node")
        which_mock.assert_called_once_with("node")
