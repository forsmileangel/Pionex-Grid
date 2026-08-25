import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from v2.store import CaptureError, connect, ingest, restore_latest_backup, summary_latest, to_fixed


def rec(**kwargs):
    base = {
        "ApiOrderId": "g1",
        "Key": "BTC/USDT|2026-08-01 10:00:00",
        "Symbol": "BTC/USDT",
        "Created": "2026-08-01 10:00:00",
        "Product": "contract_grid",
        "ListStatus": "running",
        "Complete": True,
        "Investment": 1000,
        "GridProfit": 10,
        "Leverage": "5x long",
        "Trend": "long",
        "GridType": "geometric",
        "Top": 120000,
        "Bottom": 100000,
        "Row": 20,
        "PerVolume": 0.01,
        "RawJson": {"safe": True, "userId": "should-already-be-stripped"},
    }
    base.update(kwargs)
    return base


def snap(date, records, expected=None):
    running = [r for r in records if r.get("ListStatus", "running") == "running"]
    return {
        "CapturedAt": f"{date}T00:00:00+08:00",
        "Source": "test",
        "ExpectedCardCount": expected if expected is not None else len(running),
        "Records": records,
    }


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "test.sqlite"

    def tearDown(self):
        self.tmp.cleanup()

    def test_baseline_daily_zero(self):
        result = ingest(snap("2026-08-26", [rec(GridProfit=15)]), self.db)
        self.assertEqual(result["daily_profit"], "0")
        con = connect(self.db)
        row = con.execute("SELECT * FROM daily_grid_profit").fetchone()
        self.assertEqual(row["status"], "baseline")
        self.assertEqual(row["daily_profit_i"], 0)
        self.assertEqual(row["grid_profit_i"], to_fixed(15))
        con.close()

    def test_continue_and_new(self):
        ingest(snap("2026-08-26", [rec()]), self.db)
        ingest(
            snap(
                "2026-08-27",
                [
                    rec(GridProfit=12),
                    rec(ApiOrderId="g2", Key="ETH/USDT|2026-08-27 09:00:00", Symbol="ETH/USDT", Created="2026-08-27 09:00:00", GridProfit=3),
                ],
            ),
            self.db,
        )
        con = connect(self.db)
        rows = {r["bu_order_id"]: r for r in con.execute("SELECT * FROM daily_grid_profit WHERE capture_date='2026-08-27'")}
        self.assertEqual(rows["g1"]["status"], "continue")
        self.assertEqual(rows["g1"]["daily_profit_i"], to_fixed(2))
        self.assertEqual(rows["g2"]["status"], "new")
        self.assertEqual(rows["g2"]["daily_profit_i"], 0)
        con.close()

    def test_closed_from_finished(self):
        ingest(snap("2026-08-26", [rec(GridProfit=10)]), self.db)
        ingest(
            snap(
                "2026-08-27",
                [rec(ApiOrderId="g1", ListStatus="finished", Complete=True, GridProfit=13, Closed="2026-08-27 08:10:00")],
                expected=0,
            ),
            self.db,
        )
        con = connect(self.db)
        row = con.execute("SELECT * FROM daily_grid_profit WHERE capture_date='2026-08-27' AND bu_order_id='g1'").fetchone()
        self.assertEqual(row["status"], "closed")
        self.assertEqual(row["daily_profit_i"], to_fixed(3))
        pos = con.execute("SELECT lifecycle FROM grid_positions WHERE bu_order_id='g1'").fetchone()
        self.assertEqual(pos["lifecycle"], "closed")
        con.close()

    def test_closed_unresolved(self):
        ingest(snap("2026-08-26", [rec()]), self.db)
        ingest(snap("2026-08-27", [], expected=0), self.db)
        con = connect(self.db)
        row = con.execute("SELECT * FROM daily_grid_profit WHERE capture_date='2026-08-27'").fetchone()
        self.assertEqual(row["status"], "closed_unresolved")
        self.assertIsNone(row["daily_profit_i"])
        con.close()

    def test_gap_days(self):
        ingest(snap("2026-08-26", [rec(GridProfit=10)]), self.db)
        ingest(snap("2026-08-29", [rec(GridProfit=16)]), self.db)
        con = connect(self.db)
        row = con.execute("SELECT * FROM daily_grid_profit WHERE capture_date='2026-08-29'").fetchone()
        self.assertEqual(row["gap_days"], 2)
        self.assertEqual(row["daily_profit_i"], to_fixed(6))
        con.close()

    def test_duplicate_date_rejected(self):
        ingest(snap("2026-08-26", [rec()]), self.db)
        with self.assertRaises(CaptureError):
            ingest(snap("2026-08-26", [rec(GridProfit=11)]), self.db)

    def test_replace_date(self):
        ingest(snap("2026-08-26", [rec(GridProfit=10)]), self.db)
        ingest(snap("2026-08-26", [rec(GridProfit=11)]), self.db, replace_date=True)
        con = connect(self.db)
        n = con.execute("SELECT COUNT(*) AS n FROM daily_summary").fetchone()["n"]
        self.assertEqual(n, 1)
        row = con.execute("SELECT grid_profit_i FROM daily_grid_profit").fetchone()
        self.assertEqual(row["grid_profit_i"], to_fixed(11))
        con.close()

    def test_incomplete_running_aborts(self):
        with self.assertRaises(CaptureError):
            ingest(snap("2026-08-26", [rec()], expected=2), self.db)
        if self.db.exists():
            con = connect(self.db)
            n = con.execute("SELECT COUNT(*) AS n FROM capture_runs").fetchone()["n"]
            con.close()
            self.assertEqual(n, 0)

    def test_transaction_rollback(self):
        ingest(snap("2026-08-26", [rec()]), self.db)
        bad = rec(GridProfit=None, ListStatus="running")
        with self.assertRaises(CaptureError):
            ingest(snap("2026-08-27", [bad]), self.db)
        con = connect(self.db)
        dates = [r["capture_date"] for r in con.execute("SELECT capture_date FROM daily_summary")]
        self.assertEqual(dates, ["2026-08-26"])
        con.close()

    def test_backup_restore(self):
        ingest(snap("2026-08-26", [rec()]), self.db)
        from v2 import store
        original_backup = store.BACKUP_DIR
        store.BACKUP_DIR = Path(self.tmp.name) / "backups"
        try:
            ingest(snap("2026-08-27", [rec(GridProfit=12)]), self.db)
            restored = restore_latest_backup(self.db)
            self.assertTrue(restored.exists())
        finally:
            store.BACKUP_DIR = original_backup

    def test_summary_latest(self):
        ingest(snap("2026-08-26", [rec()]), self.db)
        payload = summary_latest(self.db)
        self.assertTrue(payload["available"])
        self.assertEqual(payload["capture_date"], "2026-08-26")
        self.assertEqual(payload["daily_profit_usdt"], "0")

    def test_fixed_rounding(self):
        self.assertEqual(to_fixed("0.01"), 1_000_000)
        self.assertEqual(to_fixed(1.005), 100500000)


if __name__ == "__main__":
    unittest.main()
