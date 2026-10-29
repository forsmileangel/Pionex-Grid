import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from v2.capture import _update_finished_cache
from v2.store import connect, ingest
from v2.tests.test_store import FX, rec, snap


def _snapshot(date, stale):
    mark = {"ConversionStale": True} if stale else {}
    grid, price = (0.15, 2000) if date.endswith("25") else (0.16, 2100)
    usdt_profit = 100 if date.endswith("25") else 112
    raw = {"order": {"quote": "ETH", "buOrderData": {"gridProfit": grid, "profitWithdrawn": 0}}}
    rows = [
        rec(GridProfit=usdt_profit, **mark),
        rec(
            ApiOrderId="eth",
            Key="ETH|2026-08-01 11:00:00",
            Symbol="ETH",
            Created="2026-08-01 11:00:00",
            Product="coin_margined_contract_grid",
            Investment=1000,
            GridProfit=grid * price,
            RawGridProfit=grid,
            ConversionPrice=price,
            ConversionSymbol="ETH_USDT",
            RawJson=raw,
            **mark,
        ),
    ]
    return snap(date, rows)


def _book(db, stale):
    with patch("v2.store.usdt_twd", return_value=FX):
        for date in ("2026-08-25", "2026-08-26"):
            ingest(_snapshot(date, stale), db)


def _profits(db):
    con = connect(db)
    try:
        summary = [tuple(row) for row in con.execute(
            """SELECT capture_date, daily_profit_i, cumulative_i, true_profit_i, investment_i
               FROM daily_summary ORDER BY capture_date""")]
        rows = [tuple(row) for row in con.execute(
            """SELECT capture_date, bu_order_id, daily_profit_i, cumulative_i, grid_profit_i
               FROM daily_grid_profit ORDER BY capture_date, bu_order_id""")]
    finally:
        con.close()
    return summary, rows


class ConversionStaleTests(unittest.TestCase):
    def test_the_flag_does_not_change_daily_or_cumulative_profit(self):
        with tempfile.TemporaryDirectory() as tmp:
            plain = Path(tmp) / "plain.sqlite"
            marked = Path(tmp) / "marked.sqlite"
            _book(plain, False)
            _book(marked, True)
            self.assertEqual(_profits(plain), _profits(marked))
            summary, _rows = _profits(plain)
            self.assertEqual(summary[0][1], 0)
            self.assertNotEqual(summary[1][1], 0)

    def test_capture_treats_the_flag_as_the_same_finished_record(self):
        raw = {"list": {"buOrderId": "g1"}, "order": {"buOrderData": {"gridProfit": "5"}}}
        base = {
            "CapturedAt": "2026-10-13T08:15:00+08:00",
            "Records": [rec(ListStatus="finished", GridProfit=5, RawJson=raw)],
        }
        marked = json.loads(json.dumps(base))
        marked["Records"][0]["ConversionStale"] = True
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "finished-details.json"
            _update_finished_cache(base, cache, full=True)
            again = _update_finished_cache(marked, cache, full=True)
        self.assertEqual(again["changed"], 0)
        self.assertEqual(again["fetched"], 1)
