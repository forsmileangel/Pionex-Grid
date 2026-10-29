import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from v2 import store
from v2.tests.test_store import FX, rec, snap

TABLES = ("daily_grid_profit", "daily_summary")


def g2(**kwargs):
    return rec(ApiOrderId="g2", Key="ETH/USDT|2026-08-01 11:00:00", Symbol="ETH/USDT",
               Created="2026-08-01 11:00:00", **kwargs)


def dump(db):
    con = sqlite3.connect(db)
    try:
        return {t: con.execute(f"SELECT * FROM {t} ORDER BY id" if t == "daily_grid_profit"
                               else f"SELECT * FROM {t} ORDER BY capture_date").fetchall() for t in TABLES}
    finally:
        con.close()


class IncrementalRebuildTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.db = self.dir / "ledger.sqlite"
        for p in [patch("v2.store.usdt_twd", return_value=FX), patch("v2.store.BACKUP_DIR", self.dir / "backups")]:
            p.start()
            self.addCleanup(p.stop)

    def capture(self, day, rows, db=None, **kwargs):
        return store.ingest(snap(day, rows), db or self.db, **kwargs)

    def ledger(self):
        """A reinvest, a vanished bot settled a day later, and a new bot's first day."""
        self.capture("2026-08-25", [rec(GridProfit=10), g2(GridProfit=5)])
        self.capture("2026-08-26", [rec(GridProfit=16), g2(GridProfit=7)])
        self.capture("2026-08-27", [rec(GridProfit=2, ProfitReinvest=20), g2(GridProfit=9)])
        self.capture("2026-08-28", [rec(GridProfit=6)])
        self.capture("2026-08-29", [rec(GridProfit=9),
                                    g2(GridProfit=11, ListStatus="finished", Closed="2026-08-28 18:00:00")])
        self.capture("2026-08-30", [rec(GridProfit=12),
                                    rec(ApiOrderId="g3", Key="SOL/USDT|2026-08-29 21:00:00", Symbol="SOL/USDT",
                                        Created="2026-08-29 21:00:00", GridProfit=1)])

    def rebuild(self, db, since=None):
        con = store.connect(db)
        try:
            store.rebuild_daily_profits(con, since=since)
            con.commit()
        finally:
            con.close()

    def test_rebuilding_from_any_date_matches_a_full_rebuild(self):
        self.ledger()
        self.rebuild(self.db)
        full = dump(self.db)
        days = [row[0] for row in full["daily_summary"]]
        self.assertEqual(len(days), 6)
        for day in days:
            with self.subTest(since=day):
                copy = self.dir / f"copy-{day}.sqlite"
                shutil.copy2(self.db, copy)
                self.rebuild(copy, since=day)
                self.assertEqual(dump(copy), full)

    def spy(self):
        calls = []
        real = store.rebuild_daily_profits

        def record(con, since=None):
            calls.append(since)
            return real(con, since)
        p = patch("v2.store.rebuild_daily_profits", side_effect=record)
        p.start()
        self.addCleanup(p.stop)
        return calls

    def test_a_new_day_with_settled_closures_recomputes_only_that_day(self):
        self.ledger()
        calls = self.spy()
        finished = g2(GridProfit=11, ListStatus="finished", Closed="2026-08-28 18:00:00")
        self.capture("2026-08-31", [rec(GridProfit=14), finished], full_rebuild=False)
        self.assertEqual(calls, ["2026-08-31"])

    def test_a_late_settlement_recomputes_from_its_closing_day_like_a_full_pass(self):
        rows = [
            ("2026-08-25", [rec(GridProfit=10), g2(GridProfit=5)]),
            ("2026-08-26", [rec(GridProfit=16), g2(GridProfit=7)]),
            ("2026-08-27", [rec(GridProfit=18)]),                 # g2 gone: closing day, not yet settled
            ("2026-08-28", [rec(GridProfit=21)]),
        ]
        late = ("2026-08-29", [rec(GridProfit=25), g2(GridProfit=8, ListStatus="finished", Closed="2026-08-26 22:00:00")])
        other = self.dir / "full.sqlite"
        for day, recs in rows:
            self.capture(day, recs)
            self.capture(day, recs, db=other)
        self.capture(*late, db=other)
        calls = self.spy()
        self.capture(*late, full_rebuild=False)
        self.assertEqual(calls, ["2026-08-27"])
        self.assertEqual(dump(self.db), dump(other))
        con = store.connect(self.db)
        try:
            closing = con.execute("SELECT daily_profit_i FROM daily_summary WHERE capture_date='2026-08-27'").fetchone()[0]
        finally:
            con.close()
        self.assertEqual(closing, store.to_fixed(2 + 1))       # g1 +2, g2's last +1 lands on its closing day

    def test_a_recreated_ledger_file_gets_its_schema_again(self):
        self.capture("2026-08-25", [rec(GridProfit=10)])
        self.db.unlink()
        for side in self.dir.glob("ledger.sqlite-*"):
            side.unlink()
        con = store.connect(self.db)
        try:
            self.assertEqual(con.execute("SELECT count(*) FROM daily_summary").fetchone()[0], 0)
        finally:
            con.close()


class DayRowsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "ledger.sqlite"
        for p in [patch("v2.store.usdt_twd", return_value=FX), patch("v2.store.BACKUP_DIR", Path(self.tmp.name) / "b")]:
            p.start()
            self.addCleanup(p.stop)

    def test_days_carry_totals_and_rows_come_one_day_at_a_time(self):
        store.ingest(snap("2026-08-25", [rec(GridProfit=10), g2(GridProfit=5)]), self.db)
        store.ingest(snap("2026-08-26", [rec(GridProfit=16), g2(GridProfit=7)]), self.db)
        days = store.days_payload(self.db)["days"]
        self.assertEqual([d["capture_date"] for d in days], ["2026-08-26", "2026-08-25"])
        self.assertTrue(all("rows" not in d for d in days))
        rows = store.day_rows_payload("2026-08-26", self.db)["rows"]
        self.assertEqual(sorted(r["bu_order_id"] for r in rows), ["g1", "g2"])
        self.assertEqual(sorted(float(r["daily_profit"]["usdt"]) for r in rows), [2.0, 6.0])
        self.assertIn("twd", rows[0]["daily_profit"])
        self.assertEqual(store.day_rows_payload("2026-01-01", self.db)["rows"], [])


if __name__ == "__main__":
    unittest.main()
