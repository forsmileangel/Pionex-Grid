import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from v2.store import CaptureError, apply_event_decision, connect, export_sheets, from_fixed, ingest, ledger_publish_payload, rebuild_daily_profits, restore_latest_backup, summary_latest, to_fixed

FX = {
    "usdt_twd": 31.49,
    "usd_twd": 31.49,
    "usdt_usd": 0.9998,
    "usd_cash_buy": 31.495,
    "as_of": "test",
    "source": "bot-cash-usdt",
    "fetched_at": 0,
}


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
        self.fx_patch = patch("v2.store.usdt_twd", return_value=FX)
        self.fx_patch.start()

    def tearDown(self):
        self.fx_patch.stop()
        self.tmp.cleanup()

    def test_baseline_daily_zero(self):
        result = ingest(snap("2026-08-26", [rec(GridProfit=15)]), self.db)
        self.assertEqual(result["daily_profit"], "0")
        con = connect(self.db)
        row = con.execute("SELECT * FROM daily_grid_profit").fetchone()
        self.assertEqual(row["status"], "baseline")
        self.assertEqual(row["daily_profit_i"], 0)
        self.assertEqual(row["grid_profit_i"], to_fixed(15))
        self.assertEqual(row["cumulative_i"], to_fixed(15))
        summary = con.execute("SELECT * FROM daily_summary").fetchone()
        self.assertEqual(summary["daily_profit_i"], 0)
        self.assertEqual(summary["cumulative_i"], to_fixed(15))
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
        self.assertEqual(rows["g1"]["cumulative_i"], to_fixed(12))
        self.assertEqual(rows["g2"]["status"], "new")
        self.assertEqual(rows["g2"]["daily_profit_i"], 0)
        self.assertEqual(rows["g2"]["cumulative_i"], to_fixed(3))
        summary = con.execute("SELECT * FROM daily_summary WHERE capture_date='2026-08-27'").fetchone()
        self.assertEqual(summary["daily_profit_i"], to_fixed(2))
        self.assertEqual(summary["cumulative_i"], to_fixed(15))
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
        summary = con.execute("SELECT * FROM daily_summary WHERE capture_date='2026-08-27'").fetchone()
        self.assertEqual(summary["true_profit_i"], to_fixed(13))
        con.close()

    def test_reinvest_does_not_create_fake_loss(self):
        ingest(snap("2026-08-26", [rec(GridProfit=100, Investment=1000)]), self.db)
        ingest(snap("2026-08-27", [rec(GridProfit=5, Investment=1100, ProfitReinvest=100)]), self.db)
        con = connect(self.db)
        row = con.execute("SELECT * FROM daily_grid_profit WHERE capture_date='2026-08-27'").fetchone()
        self.assertIn("reinvest", row["event"])
        self.assertGreaterEqual(row["daily_profit_i"], to_fixed(4))
        self.assertEqual(row["lifetime_i"], to_fixed(105))
        summary = con.execute("SELECT * FROM daily_summary WHERE capture_date='2026-08-27'").fetchone()
        self.assertEqual(summary["true_profit_i"], to_fixed(105))
        self.assertEqual(summary["cumulative_i"], to_fixed(5))
        self.assertEqual(summary["daily_profit_i"], to_fixed(5))
        con.close()

    def test_catchup_reinvest_stock_is_not_overnight_daily(self):
        ingest(snap("2026-08-25", [rec(GridProfit=100, Investment=1000)]), self.db)
        ingest(snap("2026-08-26", [rec(GridProfit=102, Investment=1000, ProfitReinvest=40)]), self.db)
        con = connect(self.db)
        row = con.execute("SELECT * FROM daily_grid_profit WHERE capture_date='2026-08-26'").fetchone()
        self.assertEqual(row["event"], "continue")
        self.assertEqual(row["daily_profit_i"], to_fixed(2))
        self.assertEqual(row["lifetime_i"], to_fixed(102))
        summary = con.execute("SELECT * FROM daily_summary WHERE capture_date='2026-08-26'").fetchone()
        self.assertEqual(summary["daily_profit_i"], to_fixed(2))
        self.assertEqual(summary["cumulative_i"], to_fixed(102))
        self.assertEqual(summary["true_profit_i"], to_fixed(102))
        con.close()

    def test_baseline_api_reinvest_stock_not_in_true(self):
        ingest(snap("2026-08-25", [rec(GridProfit=100, ProfitReinvest=40)]), self.db)
        con = connect(self.db)
        summary = con.execute("SELECT * FROM daily_summary").fetchone()
        self.assertEqual(summary["daily_profit_i"], 0)
        self.assertEqual(summary["cumulative_i"], to_fixed(100))
        self.assertEqual(summary["true_profit_i"], to_fixed(100))
        con.close()

    def test_inferred_reinvest_carries_lifetime(self):
        ingest(snap("2026-08-25", [rec(GridProfit=80, Investment=1000)]), self.db)
        ingest(snap("2026-08-26", [rec(GridProfit=3, Investment=1080)]), self.db)
        ingest(snap("2026-08-27", [rec(GridProfit=5, Investment=1080)]), self.db)
        con = connect(self.db)
        day3 = con.execute("SELECT * FROM daily_grid_profit WHERE capture_date='2026-08-27'").fetchone()
        self.assertEqual(day3["daily_profit_i"], to_fixed(2))
        self.assertGreaterEqual(day3["lifetime_i"], to_fixed(80))
        con.close()

    def test_rebuild_backfills_raw_reinvest_stock(self):
        raw25 = {"order": {"buOrderData": {"profitReinvest": 40, "gridProfit": 100}}}
        ingest(snap("2026-08-25", [rec(GridProfit=100, RawJson=raw25)]), self.db)
        con = connect(self.db)
        con.execute("UPDATE grid_snapshots SET profit_reinvest_i=0")
        con.execute("UPDATE daily_grid_profit SET reinvest_i=0, lifetime_i=grid_profit_i")
        con.execute("UPDATE daily_summary SET true_profit_i=cumulative_i")
        con.commit()
        con.close()
        raw26 = {"order": {"buOrderData": {"profitReinvest": 40, "gridProfit": 102}}}
        ingest(snap("2026-08-26", [rec(GridProfit=102, ProfitReinvest=40, RawJson=raw26)]), self.db)
        con = connect(self.db)
        rebuild_daily_profits(con)
        con.commit()
        d25 = con.execute("SELECT * FROM daily_summary WHERE capture_date='2026-08-25'").fetchone()
        d26 = con.execute("SELECT * FROM daily_summary WHERE capture_date='2026-08-26'").fetchone()
        self.assertEqual(d25["daily_profit_i"], 0)
        self.assertEqual(d25["cumulative_i"], to_fixed(100))
        self.assertEqual(d25["true_profit_i"], to_fixed(100))
        self.assertEqual(d26["daily_profit_i"], to_fixed(2))
        self.assertEqual(d26["cumulative_i"], to_fixed(102))
        self.assertEqual(d26["true_profit_i"], to_fixed(102))
        snap25 = con.execute(
            "SELECT profit_reinvest_i FROM grid_snapshots s JOIN capture_runs c ON c.id=s.run_id WHERE c.capture_date='2026-08-25'"
        ).fetchone()
        self.assertEqual(snap25["profit_reinvest_i"], to_fixed(40))
        con.close()

    def test_coin_margined_daily_uses_coin_increment_not_mtm(self):
        raw25 = {"order": {"quote": "ETH", "buOrderData": {"gridProfit": 0.1734900688396}}}
        raw26 = {"order": {"quote": "ETH", "buOrderData": {"gridProfit": 0.1750308839308}}}
        ingest(snap("2026-08-25", [rec(
            Product="coin_margined_contract_grid",
            Symbol="ETH",
            Investment=5099.78,
            GridProfit=435.32995524,
            RawGridProfit=0.1734900688396,
            ConversionPrice=2509.25,
            ConversionSymbol="ETH_USDT",
            RawJson=raw25,
        )]), self.db)
        ingest(snap("2026-08-26", [rec(
            Product="coin_margined_contract_grid",
            Symbol="ETH",
            Investment=5099.78,
            GridProfit=427.84549268,
            RawGridProfit=0.1750308839308,
            ConversionPrice=2443.64,
            ConversionSymbol="ETH_USDT",
            RawJson=raw26,
        )]), self.db)
        con = connect(self.db)
        row = con.execute("SELECT * FROM daily_grid_profit WHERE capture_date='2026-08-26'").fetchone()
        self.assertGreater(row["daily_profit_i"], 0)
        self.assertAlmostEqual(float(from_fixed(row["daily_profit_i"])), 3.76, delta=0.05)
        self.assertLess(row["fx_gap_i"], to_fixed(-10))
        summary = con.execute("SELECT * FROM daily_summary WHERE capture_date='2026-08-26'").fetchone()
        self.assertGreater(summary["daily_profit_i"], 0)
        self.assertEqual(summary["cumulative_i"], to_fixed(427.84549268))
        con.close()

    def test_no_inferred_withdraw_on_grid_drop(self):
        ingest(snap("2026-08-25", [rec(GridProfit=435, Investment=5000)]), self.db)
        ingest(snap("2026-08-26", [rec(GridProfit=428, Investment=5000)]), self.db)
        con = connect(self.db)
        row = con.execute("SELECT * FROM daily_grid_profit WHERE capture_date='2026-08-26'").fetchone()
        self.assertEqual(row["event"], "continue")
        self.assertEqual(row["daily_profit_i"], to_fixed(-7))
        self.assertNotIn("withdraw", row["event"] or "")
        con.close()

    def test_withdraw_adds_back_to_true_profit(self):
        ingest(snap("2026-08-26", [rec(GridProfit=100, Investment=1000)]), self.db)
        ingest(snap("2026-08-27", [rec(GridProfit=55, Investment=1000, ProfitWithdrawn=50)]), self.db)
        con = connect(self.db)
        row = con.execute("SELECT * FROM daily_grid_profit WHERE capture_date='2026-08-27'").fetchone()
        self.assertIn("withdraw", row["event"])
        self.assertEqual(row["lifetime_i"], to_fixed(105))
        summary = con.execute("SELECT * FROM daily_summary WHERE capture_date='2026-08-27'").fetchone()
        self.assertEqual(summary["true_profit_i"], to_fixed(105))
        con.close()

    def test_withdraw_uses_cumulative_delta_even_when_grid_profit_grows(self):
        samples = [
            ("2026-08-25", 1500, 0, 0, 1500),
            ("2026-08-26", 505, 1000, 5, 1505),
            ("2026-08-27", 510, 1000, 5, 1510),
            ("2026-08-28", 610, 2000, 1100, 2610),
            ("2026-08-29", 610, 2100, 100, 2710),
            ("2026-08-30", 620, 2100, 10, 2720),
        ]
        for date, grid, withdrawn, daily, lifetime in samples:
            ingest(snap(date, [rec(GridProfit=grid, ProfitWithdrawn=withdrawn)]), self.db)
        last = snap(samples[-1][0], [rec(GridProfit=620, ProfitWithdrawn=2100)])
        ingest(last, self.db, replace_date=True)
        con = connect(self.db)
        try:
            for _ in range(2):
                rebuild_daily_profits(con)
            con.commit()
        finally:
            con.close()
        con = connect(self.db)
        try:
            rows = list(con.execute("SELECT * FROM daily_grid_profit ORDER BY capture_date"))
            self.assertEqual(len(rows), len(samples))
            for row, (date, grid, withdrawn, daily, lifetime) in zip(rows, samples):
                with self.subTest(date=date):
                    self.assertEqual(row["daily_profit_i"], to_fixed(daily))
                    self.assertEqual(row["lifetime_i"], to_fixed(lifetime))
            payload = ledger_publish_payload(self.db)
            self.assertEqual({d["date"]: d["daily_profit_usdt"] for d in payload["days"]},
                             {s[0]: str(s[3]) for s in samples})
        finally:
            con.close()

    def test_coin_withdraw_counter_ignores_exchange_rate_changes(self):
        for date, grid, price in [("2026-08-25", 0.15, 2000),
                                  ("2026-08-26", 0.155, 2100),
                                  ("2026-08-27", 0.16, 1900)]:
            raw = {"order": {"quote": "ETH", "buOrderData": {
                "gridProfit": grid, "profitWithdrawn": 0.05}}}
            ingest(snap(date, [rec(Product="coin_margined_contract_grid", Symbol="ETH",
                GridProfit=grid * price, RawGridProfit=grid, ConversionPrice=price,
                ConversionSymbol="ETH_USDT", RawJson=raw)]), self.db)
        con = connect(self.db)
        try:
            rows = list(con.execute("SELECT * FROM daily_grid_profit ORDER BY capture_date"))
            self.assertEqual([r["daily_profit_i"] for r in rows],
                             [0, to_fixed(10.5), to_fixed(9.5)])
            self.assertTrue(all("withdraw" not in r["event"] for r in rows))
        finally:
            con.close()

    def test_inferred_reinvest_from_investment_jump(self):
        ingest(snap("2026-08-26", [rec(GridProfit=80, Investment=1000)]), self.db)
        ingest(snap("2026-08-27", [rec(GridProfit=3, Investment=1080)]), self.db)
        con = connect(self.db)
        row = con.execute("SELECT * FROM daily_grid_profit WHERE capture_date='2026-08-27'").fetchone()
        self.assertIn("reinvest", row["event"])
        self.assertGreater(row["lifetime_i"], to_fixed(70))
        con.close()

    def test_add_capital_is_not_reinvest(self):
        ingest(snap("2026-08-25", [rec(GridProfit=182.56, Investment=1668.84, ProfitReinvest=148.25)]), self.db)
        ingest(snap("2026-08-26", [rec(GridProfit=187.96, Investment=2086.04, ProfitReinvest=148.25)]), self.db)
        con = connect(self.db)
        row = con.execute("SELECT * FROM daily_grid_profit WHERE capture_date='2026-08-26'").fetchone()
        self.assertEqual(row["event"], "add")
        self.assertAlmostEqual(float(from_fixed(row["daily_profit_i"])), 5.4, places=2)
        summary = con.execute("SELECT * FROM daily_summary WHERE capture_date='2026-08-26'").fetchone()
        self.assertEqual(summary["cumulative_i"], row["grid_profit_i"])
        self.assertEqual(summary["true_profit_i"], row["grid_profit_i"])
        con.close()

    def test_reinvest_resets_grid_into_position(self):
        ingest(snap("2026-08-25", [rec(GridProfit=1418.33, Investment=3500)]), self.db)
        ingest(snap("2026-08-26", [rec(GridProfit=5, Investment=4918.33, ProfitReinvest=1418.33)]), self.db)
        con = connect(self.db)
        row = con.execute("SELECT * FROM daily_grid_profit WHERE capture_date='2026-08-26'").fetchone()
        self.assertIn("reinvest", row["event"])
        self.assertGreaterEqual(row["daily_profit_i"], to_fixed(4))
        self.assertLess(row["daily_profit_i"], to_fixed(10))
        summary = con.execute("SELECT * FROM daily_summary WHERE capture_date='2026-08-26'").fetchone()
        self.assertEqual(summary["cumulative_i"], to_fixed(5))
        self.assertGreater(summary["true_profit_i"], to_fixed(1420))
        con.close()

    def test_add_with_tiny_grid_drop_is_add(self):
        ingest(snap("2026-08-25", [rec(GridProfit=200, Investment=2000)]), self.db)
        ingest(snap("2026-08-26", [rec(GridProfit=197, Investment=2417)]), self.db)
        con = connect(self.db)
        row = con.execute("SELECT * FROM daily_grid_profit WHERE capture_date='2026-08-26'").fetchone()
        self.assertEqual(row["event"], "add")
        self.assertEqual(row["daily_profit_i"], to_fixed(-3))
        con.close()

    def test_small_position_bump_with_grid_drop_is_reinvest(self):
        ingest(snap("2026-08-25", [rec(GridProfit=90, Investment=2000)]), self.db)
        ingest(snap("2026-08-26", [rec(GridProfit=65, Investment=2090)]), self.db)
        con = connect(self.db)
        row = con.execute("SELECT * FROM daily_grid_profit WHERE capture_date='2026-08-26'").fetchone()
        self.assertIn("reinvest", row["event"])
        self.assertGreater(row["lifetime_i"], to_fixed(85))
        con.close()

    def test_mid_add_with_grid_drop_is_review(self):
        ingest(snap("2026-08-25", [rec(GridProfit=200, Investment=2000)]), self.db)
        ingest(snap("2026-08-26", [rec(GridProfit=160, Investment=2150)]), self.db)
        con = connect(self.db)
        row = con.execute("SELECT * FROM daily_grid_profit WHERE capture_date='2026-08-26'").fetchone()
        self.assertIn("review", row["event"])
        self.assertEqual(row["daily_profit_i"], to_fixed(-40))
        con.close()

    def test_round_number_add_without_grid_reset_is_add(self):
        ingest(snap("2026-08-25", [rec(GridProfit=200, Investment=2000)]), self.db)
        ingest(snap("2026-08-26", [rec(GridProfit=202, Investment=2150)]), self.db)
        con = connect(self.db)
        row = con.execute("SELECT * FROM daily_grid_profit WHERE capture_date='2026-08-26'").fetchone()
        self.assertEqual(row["event"], "add")
        self.assertEqual(row["daily_profit_i"], to_fixed(2))
        con.close()

    def test_ambiguous_add_or_reinvest_is_review(self):
        ingest(snap("2026-08-25", [rec(GridProfit=400, Investment=2000)]), self.db)
        ingest(snap("2026-08-26", [rec(GridProfit=250, Investment=2417)]), self.db)
        con = connect(self.db)
        row = con.execute("SELECT * FROM daily_grid_profit WHERE capture_date='2026-08-26'").fetchone()
        self.assertIn("review", row["event"])
        self.assertEqual(row["daily_profit_i"], to_fixed(-150))
        con.close()
        result = apply_event_decision("2026-08-26", "g1", "add", self.db)
        self.assertEqual(result["event"], "add")
        con = connect(self.db)
        row = con.execute("SELECT * FROM daily_grid_profit WHERE capture_date='2026-08-26'").fetchone()
        self.assertEqual(row["event"], "add")
        self.assertEqual(row["daily_profit_i"], to_fixed(-150))
        con.close()
        result = apply_event_decision("2026-08-26", "g1", "reinvest", self.db)
        self.assertIn("reinvest", result["event"])
        con = connect(self.db)
        row = con.execute("SELECT * FROM daily_grid_profit WHERE capture_date='2026-08-26'").fetchone()
        self.assertIn("reinvest", row["event"])
        self.assertGreater(row["lifetime_i"], to_fixed(390))
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

    def test_ledger_publish_payload_compact(self):
        first = snap("2026-08-25", [rec(GridProfit=15)])
        first["Wallet"] = {"totalInUsdt": "1000"}
        first["Fx"] = FX
        ingest(first, self.db)
        second = snap("2026-08-26", [rec(GridProfit=17)])
        second["Wallet"] = {"totalInUsdt": "1000"}
        second["Fx"] = FX
        ingest(second, self.db)
        payload = ledger_publish_payload(self.db)
        self.assertEqual(payload["schema"], 1)
        self.assertEqual(payload["source"], "pionex-grid-v2")
        self.assertEqual(payload["baseline_date"], "2026-08-25")
        self.assertTrue(payload["available"])
        self.assertEqual(payload["capture_date"], "2026-08-26")
        self.assertEqual(payload["days"][0]["date"], "2026-08-26")
        self.assertEqual(payload["days"][0]["daily_profit_usdt"], "2")
        self.assertEqual(payload["days"][0]["cumulative_usdt"], "17")
        self.assertEqual(payload["days"][0]["true_profit_usdt"], "17")
        self.assertEqual(payload["days"][-1]["date"], "2026-08-25")
        self.assertEqual(payload["days"][-1]["daily_profit_usdt"], "0")
        self.assertEqual(payload["days"][-1]["cumulative_usdt"], "15")
        self.assertEqual(payload["latest"]["cumulative_usdt"], "17")
        self.assertEqual(payload["wallet"]["usdt"], "1000")
        self.assertEqual(payload["wallet"]["twd"], 31490)
        blob = json.dumps(payload)
        self.assertNotIn("raw_json", blob)
        self.assertNotIn("userId", blob)

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

    def test_board_uses_24h_profit(self):
        ingest(snap("2026-08-26", [rec(GridProfit=10, GridProfit24h=1.5, Investment=3500, Position=15.2, MarkPrice=457, LiqPrice=286, Trend="long")]), self.db)
        from v2.store import board_payload
        board = board_payload(self.db)
        self.assertEqual(board["position_count"], 1)
        self.assertEqual(board["total_profit_24h"]["usdt"], "1.5")
        self.assertEqual(board["rows"][0]["profit_24h"]["usdt"], "1.5")
        self.assertEqual(board["rows"][0]["size"]["usdt"], "3500")
        self.assertEqual(board["rows"][0]["investment"]["usdt"], "3500")
        self.assertAlmostEqual(board["profit_24h_pct"], 1.5 / 3500 * 100, places=4)
        self.assertEqual(board["true_grid_profit"]["usdt"], "10")
        self.assertEqual(board["fx"]["usdt_twd"], 31.49)

    def test_export_long_horizon_pct(self):
        ingest(snap("2026-08-26", [rec(GridProfit=10, Investment=1000)]), self.db)
        ingest(snap("2026-08-27", [rec(GridProfit=12, Investment=1000)]), self.db)
        sheets = export_sheets(self.db)
        self.assertEqual(list(sheets.keys())[:3], ["每日總覽", "每日倉位明細", "倉位×日期"])
        overview = sheets["每日總覽"]
        self.assertEqual(overview[0][4], "單日%")
        self.assertEqual(overview[1][0], "2026-08-26")
        self.assertEqual(overview[1][1], 0.0)
        self.assertEqual(overview[1][4], 0.0)
        self.assertEqual(overview[2][0], "2026-08-27")
        self.assertEqual(overview[2][1], 2.0)
        self.assertEqual(overview[2][4], 0.2)
        detail = sheets["每日倉位明細"]
        self.assertEqual(detail[2][7], 0.2)
        matrix = sheets["倉位×日期"]
        self.assertEqual(matrix[0][3:], ["2026-08-26", "2026-08-27"])
        self.assertEqual(matrix[1][3:], [0.0, 2.0])

    def test_coin_margined_liq_is_inverted_to_usdt(self):
        ingest(snap("2026-08-26", [rec(
            Product="coin_margined_contract_grid",
            Symbol="ETH",
            Trend="short",
            Leverage="4x short",
            Investment=5099.78,
            EstimateLiqUp=0.0007690328458623,
            EstimateLiqDown=0,
            LiquidationPrice=0,
            MarkPrice=2519.86,
            GridProfit24h=1,
        )]), self.db)
        from v2.store import board_payload
        row = board_payload(self.db)["rows"][0]
        self.assertAlmostEqual(float(row["liq_price"]), 1300.34, delta=0.02)
        self.assertAlmostEqual(float(row["liq_distance_pct"]), 48.4, delta=0.2)

    def test_summary_latest(self):
        ingest(snap("2026-08-26", [rec()]), self.db)
        payload = summary_latest(self.db)
        self.assertTrue(payload["available"])
        self.assertEqual(payload["capture_date"], "2026-08-26")
        self.assertEqual(payload["daily_profit_usdt"], "0")

    def test_amdx_style_total_pnl(self):
        ingest(snap("2026-08-25", [rec(
            Symbol="AMDX/USDT",
            Created="2026-01-21 00:38:05",
            Leverage="6x long",
            Trend="long",
            Investment=3500,
            GridProfit=1410.84272059,
            FundingFee=-152.59678158,
            Position=15.3,
            PositionOpenPrice=468.3067180758142,
            MarkPrice=456.97,
        )]), self.db)
        from v2.store import board_payload
        row = board_payload(self.db)["rows"][0]
        self.assertAlmostEqual(float(row["trend_profit"]["usdt"]), -508.7, delta=8)
        self.assertAlmostEqual(float(row["total_pnl"]["usdt"]), 902, delta=8)
        self.assertAlmostEqual(float(row["funding"]["usdt"]), -152.6, delta=0.2)
        self.assertGreater(float(row["grid_annualized_pct"]), 60)
        self.assertLess(float(row["annualized_pct"]), float(row["grid_annualized_pct"]))

    def test_fixed_rounding(self):
        self.assertEqual(to_fixed("0.01"), 1_000_000)
        self.assertEqual(to_fixed(1.005), 100500000)


if __name__ == "__main__":
    unittest.main()
