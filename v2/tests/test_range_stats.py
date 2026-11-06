"""區間統計: grid profit from intraday points, funding by settlement time, exclusions, the CLI."""
import contextlib
import io
import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from v2 import intraday, range_stats, store
from v2.store import TAIPEI
from v2.tests.test_store import FX, rec


def ts(text):
    return int(datetime.fromisoformat(text).replace(tzinfo=TAIPEI).timestamp())


def bot(oid="g1", grid=100, funding=0.0, settled="2026-09-18 00:00", **kwargs):
    raw = {"order": {"buOrderData": {"lastFundingFeeTs": ts(settled)}}}
    return rec(ApiOrderId=oid, Key=f"{oid}|2026-08-01", GridProfit=grid, FundingFee=funding, RawJson=raw, **kwargs)


def snapshot(at, records):
    running = [r for r in records if r.get("ListStatus", "running") == "running"]
    return {"CapturedAt": at.replace(" ", "T") + "+08:00", "Source": "test", "ExpectedCardCount": len(running), "Records": records}


class RangeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "ledger.sqlite"
        self.idb = intraday.default_intraday_db(self.db)
        for p in [patch("v2.store.usdt_twd", return_value=FX), patch("v2.store.BACKUP_DIR", Path(self.tmp.name) / "b")]:
            p.start()
            self.addCleanup(p.stop)

    def daily(self, at, records):
        store.ingest(snapshot(at, records), self.db)

    def point(self, at, records):
        result = intraday.record_snapshot(snapshot(at, records), "live", self.idb, self.db)
        self.assertTrue(result["ok"], result)

    def timeline(self):
        """Settlements at 08:00, 16:00, 00:00, 08:00 cost 1 each; Pionex posts the 08:00 ones a little late."""
        self.daily("2026-09-17 08:15", [bot(grid=90, funding=0, settled="2026-09-17 08:00")])
        self.point("2026-09-18 08:00:03", [bot(grid=100, funding=-0.5, settled="2026-09-18 00:00")])
        self.daily("2026-09-18 08:15", [bot(grid=101, funding=-1.5, settled="2026-09-18 08:00")])
        self.point("2026-09-18 12:00:03", [bot(grid=103, funding=-1.5, settled="2026-09-18 08:00")])
        self.point("2026-09-18 22:30:03", [bot(grid=108, funding=-2.5, settled="2026-09-18 16:00")])
        self.point("2026-09-19 04:00:03", [bot(grid=112, funding=-3.5, settled="2026-09-19 00:00")])
        self.point("2026-09-19 08:00:03", [bot(grid=115, funding=-3.5, settled="2026-09-19 00:00")])
        self.point("2026-09-19 09:00:03", [bot(grid=116, funding=-4.5, settled="2026-09-19 08:00")])

    def stats(self, start, end, exclude=()):
        return range_stats.range_stats(range_stats.parse_time(start), range_stats.parse_time(end), exclude, self.db)

    def test_old_intraday_files_gain_the_funding_columns(self):
        con = sqlite3.connect(self.idb)
        con.execute("CREATE TABLE intraday_points (run_id INTEGER, bu_order_id TEXT, captured_at TEXT, PRIMARY KEY (run_id, bu_order_id))")
        con.close()
        intraday.connect(self.idb).close()
        con = sqlite3.connect(self.idb)
        cols = {r[1] for r in con.execute("PRAGMA table_info(intraday_points)")}
        con.close()
        self.assertTrue({"funding_fee_i", "funding_ts"} <= cols)

    def test_each_point_keeps_the_funding_total_and_its_settlement_time(self):
        self.point("2026-09-18 12:00:03", [bot(grid=103, funding=-1.5, settled="2026-09-18 08:00")])
        con = sqlite3.connect(self.idb)
        row = con.execute("SELECT funding_fee_i, funding_ts FROM intraday_points").fetchone()
        con.close()
        self.assertEqual(row, (store.to_fixed(-1.5), ts("2026-09-18 08:00")))

    def test_last_settlement_follows_the_schedule(self):
        f = lambda text: datetime.fromtimestamp(range_stats.last_settlement(ts(text)), TAIPEI).strftime("%m-%d %H:%M")
        self.assertEqual([f("2026-09-18 08:00"), f("2026-09-18 07:59"), f("2026-09-18 23:59"), f("2026-09-19 00:30")],
                         ["09-18 08:00", "09-18 00:00", "09-18 16:00", "09-19 00:00"])

    def test_a_day_from_08_to_08_takes_the_three_settlements_inside_it(self):
        self.timeline()
        out = self.stats("2026-09-18 08:00", "2026-09-19 08:00")
        row = out["rows"][0]
        self.assertEqual(row["grid_i"], store.to_fixed(15))            # 115 at 08:00:03 minus 100 at 08:00:03
        self.assertEqual(row["funding_i"], store.to_fixed(-3))        # 16:00, 00:00 and the late-posted 08:00
        self.assertEqual(out["totals"]["net_i"], store.to_fixed(12))
        self.assertEqual(out["totals"]["pct_of_grid"], -20.0)

    def test_an_evening_start_reads_the_total_after_the_16_00_settlement(self):
        self.timeline()
        row = self.stats("2026-09-18 22:30", "2026-09-19 08:00")["rows"][0]
        self.assertEqual((row["grid_i"], row["funding_i"]), (store.to_fixed(7), store.to_fixed(-2)))

    def test_without_a_reading_near_the_start_funding_is_missing_not_guessed(self):
        self.daily("2026-09-17 08:15", [bot(grid=90, funding=0, settled="2026-09-17 08:00")])
        self.point("2026-09-18 08:00:03", [bot(grid=100)])
        self.daily("2026-09-18 08:15", [bot(grid=101, funding=-1.5, settled="2026-09-18 08:00")])
        self.point("2026-09-18 22:30:03", [bot(grid=108)])
        self.point("2026-09-19 08:00:03", [bot(grid=115)])
        con = sqlite3.connect(self.idb)
        con.execute("UPDATE intraday_points SET funding_fee_i=NULL, funding_ts=NULL")   # points from before 1.0.4
        con.commit()
        con.close()
        self.daily("2026-09-19 08:15", [bot(grid=116, funding=-4.5, settled="2026-09-19 08:00")])
        whole = self.stats("2026-09-18 08:00", "2026-09-19 08:00")
        self.assertEqual(whole["rows"][0]["funding_i"], store.to_fixed(-3))            # 08:15 readings stand for 08:00
        night = self.stats("2026-09-18 22:30", "2026-09-19 08:00")
        self.assertIsNone(night["rows"][0]["funding_i"])
        self.assertIn("16:00", night["rows"][0]["funding_missing"])
        self.assertEqual((night["totals"]["funding_missing"], night["totals"]["net_i"]), (1, None))
        self.assertEqual(night["totals"]["grid_i"], store.to_fixed(7))

    def test_excluded_positions_are_listed_but_left_out_of_the_totals(self):
        self.daily("2026-09-17 08:15", [bot(grid=90), bot("g2", grid=50, Symbol="SOL/USDT")])
        self.point("2026-09-18 08:00:03", [bot(grid=100), bot("g2", grid=60, Symbol="SOL/USDT")])
        self.point("2026-09-18 12:00:03", [bot(grid=104), bot("g2", grid=69, Symbol="SOL/USDT")])
        out = self.stats("2026-09-18 08:00", "2026-09-18 12:00", exclude=["SOL/USDT"])
        self.assertEqual([r["excluded"] for r in out["rows"]], [False, True])
        self.assertEqual(out["totals"]["grid_i"], store.to_fixed(4))
        self.assertEqual(out["excluded_totals"]["grid_i"], store.to_fixed(9))

    def test_a_bot_opened_inside_the_range_counts_all_its_grid_profit(self):
        self.daily("2026-09-17 08:15", [bot(grid=90)])
        self.point("2026-09-18 08:00:03", [bot(grid=100)])
        new = dict(Created="2026-09-18 09:00:00", Symbol="ADA/USDT")
        self.point("2026-09-18 12:00:03", [bot(grid=104), bot("g3", grid=3, **new)])
        self.point("2026-09-18 16:30:03", [bot(grid=106), bot("g3", grid=5, **new)])
        row = {r["id"]: r for r in self.stats("2026-09-18 08:00", "2026-09-18 16:30")["rows"]}["g3"]
        self.assertEqual((row["status"], row["grid_i"]), ("新開", store.to_fixed(5)))

    def test_bad_ranges_say_why(self):
        self.timeline()
        with self.assertRaises(range_stats.RangeError):
            self.stats("2026-09-19 08:00", "2026-09-18 08:00")
        with self.assertRaises(range_stats.RangeError):
            self.stats("2026-09-01 08:00", "2026-09-18 12:00")
        with self.assertRaises(range_stats.RangeError):
            range_stats.parse_time("yesterday")

    def test_the_api_payload_and_the_command_line(self):
        self.timeline()
        data = range_stats.payload("2026-09-18 08:00", "2026-09-19 08:00", (), self.db, self.idb)
        self.assertEqual(data["totals"]["grid"]["usdt"], "15")
        self.assertIn("twd", data["totals"]["funding"])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = range_stats.main(["--db", str(self.db), "--from", "2026-09-18 22:30", "--to", "2026-09-19 08:00"])
        self.assertEqual(code, 0)
        self.assertIn("配對 +7.00  資金費 -2.00  淨利 +5.00", out.getvalue())


if __name__ == "__main__":
    unittest.main()
