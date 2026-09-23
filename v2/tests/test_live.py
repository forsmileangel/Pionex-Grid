import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from v2.live import commit_live_to_daily, live_payload, save_live_snapshot, snapshot_to_board
from v2.store import CaptureError, connect, ingest, to_fixed
from v2.tests.test_store import FX, rec, snap

TAIPEI = ZoneInfo("Asia/Taipei")


def live_snap(records):
    running = [r for r in records if r.get("ListStatus", "running") == "running"]
    return {
        "CapturedAt": "2026-08-26T12:00:00+08:00",
        "Source": "live-test",
        "ExpectedCardCount": len(running),
        "Wallet": {"totalInUsdt": "1000"},
        "Records": records,
    }


class LiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.live = self.dir / "live-snapshot.json"
        self.db = self.dir / "test.sqlite"
        self.fx = patch("v2.live.usdt_twd", return_value=FX)
        self.fx.start()
        self.store_fx = patch("v2.store.usdt_twd", return_value=FX)
        self.store_fx.start()

    def tearDown(self):
        self.fx.stop()
        self.store_fx.stop()
        self.tmp.cleanup()

    def test_missing_live_file(self):
        payload = live_payload(self.live)
        self.assertFalse(payload["available"])
        self.assertEqual(payload["position_count"], 0)

    def test_live_board_from_json_does_not_need_sqlite(self):
        snapshot = live_snap([rec(GridProfit=12.5, Investment=1000, GridProfit24h=1.5)])
        board = snapshot_to_board(snapshot)
        self.assertTrue(board["available"])
        self.assertEqual(board["kind"], "live")
        self.assertEqual(board["position_count"], 1)
        self.assertEqual(board["rows"][0]["grid_profit"]["usdt"], "12.5")
        self.assertEqual(board["true_grid_profit"]["usdt"], "12.5")
        self.assertEqual(board["total_grid_profit"]["usdt"], "12.5")

    def test_live_true_profit_ignores_api_reinvest_stock(self):
        board = snapshot_to_board(live_snap([rec(GridProfit=12.5, Investment=1000, ProfitReinvest=40)]))
        self.assertEqual(board["total_grid_profit"]["usdt"], "12.5")
        self.assertEqual(board["true_grid_profit"]["usdt"], "12.5")

    def test_save_live_does_not_ingest(self):
        snapshot = live_snap([rec(GridProfit=15)])
        save_live_snapshot(snapshot, self.live)
        ingest(snap("2026-08-26", [rec(GridProfit=10)]), self.db)
        con = connect(self.db)
        n = con.execute("SELECT COUNT(*) AS n FROM daily_summary").fetchone()["n"]
        con.close()
        self.assertEqual(n, 1)
        self.assertTrue(self.live.exists())

    def test_commit_rejects_wrong_date(self):
        save_live_snapshot(live_snap([rec(GridProfit=15)]), self.live)
        ingest(snap("2026-08-26", [rec(GridProfit=10)]), self.db)
        with self.assertRaises(CaptureError):
            commit_live_to_daily("1999-01-01", self.db, self.live)
        con = connect(self.db)
        row = con.execute("SELECT grid_profit_i FROM daily_grid_profit").fetchone()
        con.close()
        self.assertEqual(row["grid_profit_i"], to_fixed(10))

    def test_commit_preserves_snapshot_time_and_skips_gist_without_creds(self):
        today = datetime.now(TAIPEI).date().isoformat()
        snapshot = live_snap([rec(GridProfit=22, Investment=1000)])
        snapshot['CapturedAt'] = today + 'T00:00:00+08:00'
        save_live_snapshot(snapshot, self.live)
        with patch("v2.gist_publish.publish_ledger", return_value={"ok": False, "skipped": True, "reason": "test"}):
            result = commit_live_to_daily(today, self.db, self.live)
        self.assertEqual(result["capture_date"], today)
        con = connect(self.db)
        row = con.execute("SELECT grid_profit_i FROM daily_grid_profit WHERE capture_date=?", (today,)).fetchone()
        captured = con.execute("SELECT captured_at FROM capture_runs").fetchone()[0]
        con.close()
        self.assertEqual(row["grid_profit_i"], to_fixed(22))
        self.assertEqual(captured, snapshot['CapturedAt'])

    def test_commit_rejects_stale_or_missing_snapshot_time_without_publishing(self):
        today = datetime.now(TAIPEI).date().isoformat()
        for captured in ['1999-01-01T12:00:00+08:00', None, 'invalid']:
            with self.subTest(captured=captured):
                snapshot = live_snap([rec(GridProfit=22)])
                snapshot['CapturedAt'] = captured
                save_live_snapshot(snapshot, self.live)
                with patch('v2.gist_publish.publish_ledger') as publish:
                    with self.assertRaises(CaptureError):
                        commit_live_to_daily(today, self.db, self.live)
                    publish.assert_not_called()
                self.assertFalse(self.db.exists())


if __name__ == "__main__":
    unittest.main()
