import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from v2 import intraday
from v2.intraday import EfficiencyError, bucket_bounds, efficiency_series, intraday_status, load_views, record_snapshot, save_views
from v2.store import TAIPEI, ingest, sparkline_payload, to_fixed
from v2.tests.test_store import FX, rec, snap


def at(ts, records, expected=None):
    data = snap("2026-10-01", records, expected)
    data["CapturedAt"] = ts
    return data


def ms(text):
    return int(datetime.fromisoformat(text).timestamp() * 1000)


class IntradayTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ledger = Path(self.tmp.name) / "ledger.sqlite"
        self.db = Path(self.tmp.name) / "intraday.sqlite"
        for p in [patch("v2.store.usdt_twd", return_value=FX),
                  patch("v2.store.BACKUP_DIR", Path(self.tmp.name) / "ledger-backups")]:
            p.start()
            self.addCleanup(p.stop)

    def record(self, ts, records, source="live", expected=None):
        return record_snapshot(at(ts, records, expected), source, db_path=self.db, ledger_db=self.ledger)

    def points(self, oid="g1"):
        con = intraday.connect(self.db)
        try:
            return [dict(r) for r in con.execute(
                "SELECT * FROM intraday_points WHERE bu_order_id=? ORDER BY captured_at", (oid,))]
        finally:
            con.close()

    def series(self, start, end, g, **kwargs):
        return efficiency_series(start, end, g, ledger_db=self.ledger, intraday_db=self.db, **kwargs)


class RecordTests(IntradayTestCase):
    def test_delta_between_points(self):
        self.record("2026-10-01T10:00:00+08:00", [rec(GridProfit=10)])
        self.record("2026-10-01T10:30:00+08:00", [rec(GridProfit=10.5)])
        self.record("2026-10-01T11:00:00+08:00", [rec(GridProfit=11.25)])
        rows = self.points()
        self.assertEqual([r["delta_i"] for r in rows], [None, to_fixed(0.5), to_fixed(0.75)])
        self.assertEqual([r["event"] for r in rows], ["start", "continue", "continue"])
        self.assertEqual(rows[0]["lev_x"], 5.0)

    def test_reinvest_does_not_show_as_loss(self):
        self.record("2026-10-01T10:00:00+08:00", [rec(GridProfit=100, Investment=1000)])
        result = self.record("2026-10-01T10:30:00+08:00", [rec(GridProfit=0.4, Investment=1100)])
        row = self.points()[-1]
        self.assertEqual(row["event"], "reinvest")
        self.assertEqual(row["delta_i"], to_fixed(0.4))
        self.assertEqual(result["events"], [{"id": "g1", "event": "reinvest"}])

    def test_add_keeps_grid_delta(self):
        self.record("2026-10-01T10:00:00+08:00", [rec(GridProfit=10, Investment=1000)])
        self.record("2026-10-01T10:30:00+08:00", [rec(GridProfit=10.3, Investment=1500)])
        row = self.points()[-1]
        self.assertEqual(row["event"], "add")
        self.assertEqual(row["delta_i"], to_fixed(0.3))

    def test_unclear_event_keeps_chain_flat(self):
        self.record("2026-10-01T10:00:00+08:00", [rec(GridProfit=500, Investment=1000)])
        self.record("2026-10-01T10:30:00+08:00", [rec(GridProfit=100, Investment=1150)])
        row = self.points()[-1]
        self.assertEqual(row["event"], "review")
        self.assertEqual(row["delta_i"], 0)

    def test_coin_margined_uses_coin_increment(self):
        base = dict(Product="coin_margined_contract_grid", Symbol="ETH", Investment=5000, ConversionSymbol="ETH_USDT")
        self.record("2026-10-01T10:00:00+08:00", [rec(**base, GridProfit=425.0, RawGridProfit=0.17,
                                                      ConversionPrice=2500,
                                                      RawJson={"order": {"quote": "ETH", "buOrderData": {"gridProfit": 0.17}}})])
        self.record("2026-10-01T10:30:00+08:00", [rec(**base, GridProfit=411.6, RawGridProfit=0.1715,
                                                      ConversionPrice=2400,
                                                      RawJson={"order": {"quote": "ETH", "buOrderData": {"gridProfit": 0.1715}}})])
        row = self.points()[-1]
        self.assertAlmostEqual(row["delta_i"] / 1e8, 3.6, delta=0.01)

    def test_first_point_links_to_ledger(self):
        ingest(snap("2026-10-01", [rec(GridProfit=10)]), self.ledger)
        self.record("2026-10-01T09:00:00+08:00", [rec(GridProfit=12)])
        row = self.points()[0]
        self.assertEqual(row["ref_kind"], "ledger")
        self.assertEqual(row["delta_i"], to_fixed(2))

    def test_incomplete_duplicate_and_older_are_skipped(self):
        self.assertTrue(self.record("2026-10-01T10:00:00+08:00", [rec()], expected=2)["skipped"])
        self.assertTrue(self.record("2026-10-01T10:30:00+08:00", [rec()])["ok"])
        self.assertTrue(self.record("2026-10-01T10:30:00+08:00", [rec()])["skipped"])
        self.assertTrue(self.record("2026-10-01T10:00:00+08:00", [rec()])["skipped"])
        self.assertEqual(len(self.points()), 1)

    def test_record_never_raises(self):
        result = record_snapshot({"CapturedAt": "2026-10-01T10:00:00+08:00", "Records": [None]}, "live",
                                 db_path=self.db, ledger_db=self.ledger)
        self.assertFalse(result["ok"])

    def test_daily_source_backs_up_intraday_file(self):
        self.record("2026-10-01T08:05:00+08:00", [rec()], source="daily")
        self.assertEqual(len(list((self.db.parent / "backups").glob("intraday-*.sqlite"))), 1)

    def test_capture_live_records_and_survives_recording_failure(self):
        from v2 import capture
        data = at("2026-10-01T10:00:00+08:00", [rec()])
        with patch("v2.capture.collect_snapshot", return_value=data), patch("v2.live.usdt_twd", return_value=FX):
            result = capture.capture_live(Path("unused"), dest=Path(self.tmp.name) / "live.json", skip_publish=True,
                                          db_path=self.ledger)
            self.assertTrue(result["intraday"]["ok"])
            self.assertEqual(len(self.points()), 1)
            with patch("v2.intraday.record_snapshot", side_effect=RuntimeError("disk full")):
                result = capture.capture_live(Path("unused"), dest=Path(self.tmp.name) / "live.json", skip_publish=True,
                                              db_path=self.ledger)
        self.assertTrue(result["ok"])
        self.assertEqual(result["intraday"], {"ok": False, "error": "disk full"})


class SeriesTests(IntradayTestCase):
    def test_hourly_buckets_equal_deltas_when_aligned(self):
        for ts, gp in (("10:00", 0), ("10:30", 1), ("11:00", 2), ("11:30", 4), ("12:00", 5)):
            self.record(f"2026-10-01T{ts}:00+08:00", [rec(GridProfit=gp)])
        hourly = self.series("2026-10-01T10:00:00+08:00", "2026-10-01T12:00:00+08:00", "1h")
        self.assertEqual(hourly["buckets"], [ms("2026-10-01T10:00:00+08:00"), ms("2026-10-01T11:00:00+08:00")])
        self.assertEqual(hourly["series"]["g1"]["profit"], [2.0, 3.0])
        self.assertEqual(hourly["series"]["g1"]["flag"], [0, 0])
        # 2 USDT on 1000 at 5x over 1 hour -> per day, normalized to 3x.
        self.assertAlmostEqual(hourly["series"]["g1"]["eff"][0], 2 / 1000 * 3 / 5 * 100 * 24, places=6)
        half = self.series("2026-10-01T10:00:00+08:00", "2026-10-01T12:00:00+08:00", "30m")
        self.assertEqual(half["series"]["g1"]["profit"], [1.0, 1.0, 2.0, 1.0])

    def test_gap_is_spread_and_flagged(self):
        self.record("2026-10-01T10:00:00+08:00", [rec(GridProfit=0)])
        self.record("2026-10-01T13:00:00+08:00", [rec(GridProfit=3)])
        out = self.series("2026-10-01T10:00:00+08:00", "2026-10-01T13:00:00+08:00", "1h")
        self.assertEqual(out["series"]["g1"]["profit"], [1.0, 1.0, 1.0])
        self.assertEqual(out["series"]["g1"]["flag"], [2, 2, 2])

    def test_partial_bucket_is_flagged_and_normalized_by_covered_hours(self):
        self.record("2026-10-01T10:00:00+08:00", [rec(GridProfit=0)])
        self.record("2026-10-01T10:30:00+08:00", [rec(GridProfit=1)])
        out = self.series("2026-10-01T10:00:00+08:00", "2026-10-01T11:00:00+08:00", "1h")
        self.assertEqual(out["series"]["g1"]["flag"], [1])
        self.assertAlmostEqual(out["series"]["g1"]["eff"][0], 1 / 1000 * 3 / 5 * 100 * 48, places=6)
        self.assertEqual(out["total"]["flag"], [1])

    def test_day_boundary_is_taipei_midnight(self):
        self.record("2026-10-01T23:00:00+08:00", [rec(GridProfit=0)])
        self.record("2026-10-02T01:00:00+08:00", [rec(GridProfit=2)])
        out = self.series("2026-10-01T12:00:00+08:00", "2026-10-02T12:00:00+08:00", "1d")
        self.assertEqual(out["buckets"][0], ms("2026-10-01T00:00:00+08:00"))
        self.assertEqual(out["series"]["g1"]["profit"], [1.0, 1.0])

    def test_daily_granularity_fills_from_ledger_but_hourly_does_not(self):
        for day, gp in (("2026-09-29", 10), ("2026-09-30", 14), ("2026-10-01", 20)):
            ingest(snap(day, [rec(GridProfit=gp)]), self.ledger)
        self.record("2026-10-01T06:00:00+08:00", [rec(GridProfit=21)])
        self.record("2026-10-01T06:30:00+08:00", [rec(GridProfit=21.5)])
        daily = self.series("2026-09-29T00:00:00+08:00", "2026-10-02T00:00:00+08:00", "1d")
        self.assertEqual(daily["series"]["g1"]["profit"], [4.0, 6.0, 1.5])
        self.assertEqual(daily["series"]["g1"]["flag"], [3, 3, 3])
        hourly = self.series("2026-10-01T05:00:00+08:00", "2026-10-01T07:00:00+08:00", "1h")
        self.assertEqual(hourly["series"]["g1"]["profit"], [None, 0.5])
        self.assertEqual(hourly["series"]["g1"]["flag"], [None, 1])

    def test_total_efficiency_is_weighted_by_3x_capital_hours(self):
        other = dict(ApiOrderId="g2", Symbol="ETH/USDT", Leverage="3x long", Investment=3000)
        self.record("2026-10-01T10:00:00+08:00", [rec(GridProfit=0), rec(**other, GridProfit=0)])
        self.record("2026-10-01T11:00:00+08:00", [rec(GridProfit=1), rec(**other, GridProfit=3)])
        out = self.series("2026-10-01T10:00:00+08:00", "2026-10-01T11:00:00+08:00", "1h")
        self.assertEqual(out["total"]["profit"], [4.0])
        # 3x capital: 1000 at 5x counts as 1000 * 5 / 3; 3000 at 3x counts as 3000.
        self.assertAlmostEqual(out["total"]["eff"][0], 4 / (1000 * 5 / 3 + 3000) * 2400, places=6)
        self.assertEqual(out["summary"]["profit"], 4.0)
        self.assertAlmostEqual(out["summary"]["eff"], out["total"]["eff"][0], places=6)
        self.assertEqual(out["summary"]["coverage"], 1.0)
        # g2: 3 USDT on 3000 at 3x = 2.4 %/day; g1: 1 USDT on 1000 at 5x = 1.44 %/day.
        self.assertEqual([r["id"] for r in out["ranking"]], ["g2", "g1"])
        self.assertAlmostEqual(out["ranking"][0]["eff"], 2.4, places=6)
        only = self.series("2026-10-01T10:00:00+08:00", "2026-10-01T11:00:00+08:00", "1h", ids=["g2"])
        self.assertEqual(list(only["series"]), ["g2"])

    def test_leverage_move_keeps_margin_and_efficiency(self):
        """Raising leverage while keeping margin in the bot: investment shrinks, efficiency must not jump."""
        other = dict(ApiOrderId="g2", Symbol="ETH/USDT", Leverage="3x long", Investment=3000)
        moved = dict(Leverage="10x long", Investment=500, ExtraMargin=500)
        self.record("2026-10-01T10:00:00+08:00", [rec(GridProfit=0), rec(**other, GridProfit=0)])
        self.record("2026-10-01T11:00:00+08:00", [rec(GridProfit=1), rec(**other, GridProfit=3)])
        self.record("2026-10-01T11:30:00+08:00", [rec(GridProfit=1.5, **moved), rec(**other, GridProfit=4.5)])
        self.record("2026-10-01T12:00:00+08:00", [rec(GridProfit=2, **moved), rec(**other, GridProfit=6)])
        out = self.series("2026-10-01T10:00:00+08:00", "2026-10-01T12:00:00+08:00", "1h")
        self.assertEqual(out["series"]["g1"]["profit"], [1.0, 1.0])
        self.assertEqual(out["events"], [])
        self.assertAlmostEqual(out["series"]["g1"]["eff"][1], out["series"]["g1"]["eff"][0], places=6)
        self.assertAlmostEqual(out["total"]["eff"][1], out["total"]["eff"][0], places=6)
        self.assertAlmostEqual(out["summary"]["eff"], out["total"]["eff"][0], places=6)

    def test_heatmap_and_events(self):
        self.record("2026-10-01T10:00:00+08:00", [rec(GridProfit=10, Investment=1000)])
        self.record("2026-10-01T10:30:00+08:00", [rec(GridProfit=11, Investment=1000)])
        self.record("2026-10-01T11:00:00+08:00", [rec(GridProfit=11.5, Investment=1600)])
        out = self.series("2026-10-01T00:00:00+08:00", "2026-10-02T00:00:00+08:00", "1h")
        self.assertEqual(out["heatmap"]["rows"]["g1"]["profit"][10], 1.5)
        self.assertIsNone(out["heatmap"]["rows"]["g1"]["profit"][9])
        self.assertEqual([(e["event"], e["t"]) for e in out["events"]], [("add", ms("2026-10-01T11:00:00+08:00"))])

    def test_too_many_buckets_suggests_coarser(self):
        with self.assertRaises(EfficiencyError) as ctx:
            self.series("2026-08-01T00:00:00+08:00", "2026-10-01T00:00:00+08:00", "30m")
        self.assertEqual(ctx.exception.suggest, "1h")

    def test_week_and_month_bounds(self):
        weeks = bucket_bounds(datetime(2026, 10, 3, 15, tzinfo=TAIPEI), datetime(2026, 10, 20, tzinfo=TAIPEI), "1w")
        self.assertEqual(weeks[0], datetime(2026, 9, 28, tzinfo=TAIPEI))
        months = bucket_bounds(datetime(2026, 9, 15, tzinfo=TAIPEI), datetime(2026, 11, 2, tzinfo=TAIPEI), "1M")
        self.assertEqual([m.month for m in months], [9, 10, 11, 12])

    def test_status_lists_missing_half_hours(self):
        for ts in ("10:00", "10:30", "11:30"):
            self.record(f"2026-10-01T{ts}:05+08:00", [rec()])
        status = intraday_status(self.db, now=datetime(2026, 10, 1, 12, 20, tzinfo=TAIPEI))
        self.assertEqual(status["missing_24h"], ["10-01 11:00", "10-01 12:00"])
        self.assertEqual(status["runs"], 3)


class ViewsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "views.json"

    def test_round_trip_and_default(self):
        doc = {"views": [{"name": " 主力 ", "settings": {"positions": ["g1"], "range": "7d", "granularity": "1h",
                                                        "metric": "usdt", "junk": 1}}], "default": "主力"}
        saved = save_views(doc, self.path)
        self.assertEqual(saved["views"][0]["name"], "主力")
        self.assertNotIn("junk", saved["views"][0]["settings"])
        self.assertEqual(load_views(self.path), saved)

    def test_invalid_views_rejected(self):
        for bad in ({"views": [{"name": "x" * 41}]},
                    {"views": [{"name": "a"}, {"name": "a"}]},
                    {"views": [{"name": str(i)} for i in range(31)]},
                    {"views": [{"name": "a", "settings": {"positions": "some"}}]},
                    ["not", "a", "dict"]):
            with self.assertRaises(ValueError):
                save_views(bad, self.path)

    def test_corrupt_file_loads_empty(self):
        self.path.write_text("{oops", encoding="utf-8")
        self.assertEqual(load_views(self.path), {"views": [], "default": None})


class SparklineTests(IntradayTestCase):
    def test_daily_efficiency_normalized_to_3x(self):
        for day, gp in (("2026-09-29", 10), ("2026-09-30", 12), ("2026-10-01", 15)):
            ingest(snap(day, [rec(GridProfit=gp)]), self.ledger)
        data = sparkline_payload(14, self.ledger)
        self.assertEqual(data["dates"], ["2026-09-29", "2026-09-30", "2026-10-01"])
        values = data["rows"]["g1"]
        self.assertIsNone(values[0])
        self.assertAlmostEqual(values[1], 2 / 1000 * 100 * 3 / 5, places=6)
        self.assertAlmostEqual(values[2], 3 / 1000 * 100 * 3 / 5, places=6)


if __name__ == "__main__":
    unittest.main()
