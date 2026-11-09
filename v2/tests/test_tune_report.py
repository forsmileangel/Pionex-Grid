import io
import json
import math
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from v2 import healthcheck, store, tune_report as tune
from v2.tests.test_store import FX, rec, snap


class GridMathTests(unittest.TestCase):
    def test_geometric_spacing(self):
        lv = tune.levels(10, 40, 5)
        expected = 4 ** 0.25 - 1
        for a, b in zip(lv, lv[1:]):
            self.assertAlmostEqual(b / a - 1, expected)
        self.assertAlmostEqual(lv[-1], 40)

    def test_max_levels(self):
        self.assertEqual(tune.max_levels(2.5, 7, 0.01), 258)
        self.assertEqual(tune.max_levels(2.5, 7, 0.01, geometric=False), 451)
        self.assertEqual(tune.max_levels(3.8, 11, 0.01), 405)
        for bottom, top, tick in ((2.5, 7, 0.01), (3.8, 11, 0.01)):
            n = tune.max_levels(bottom, top, tick)
            self.assertGreaterEqual(bottom * math.expm1(math.log(top / bottom) / (n - 1)), tick)
            self.assertLess(bottom * math.expm1(math.log(top / bottom) / n), tick)

    def test_max_levels_boundaries(self):
        self.assertEqual(tune.max_levels(1, 4, 1), 3)
        self.assertEqual(tune.max_levels(1, 1.1, 0.2), 1)
        self.assertEqual(tune.max_levels(1, 1.3, 0.1, False), 4)
        for args in ((0, 2, 0.01), (2, 1, 0.01), (1, 2, 0), (1, float('inf'), 1)):
            with self.assertRaises(ValueError):
                tune.max_levels(*args)

    def test_position_is_quantity_times_lines_strictly_above_price(self):
        lv = tune.levels(10, 40, 3)
        self.assertEqual(tune.position_at(lv, 2, 20), 2)
        self.assertEqual(tune.position_at(lv, 2, 15), 4)
        self.assertEqual(tune.position_at(lv, 2, 50), 0)
        self.assertEqual(tune.position_at(lv, 2, 5), 6)

    def test_loss_buys_each_crossed_level(self):
        self.assertEqual(tune.loss_and_qty([10, 20, 30, 40], 2, 4, 30, 15), (70, 6))

    def test_liquidation_monotonic_in_equity(self):
        lv = tune.levels(10, 40, 31)
        prices = [tune.liq_price(lv, 0.5, 5, 25, equity) for equity in (20, 40, 60, 80)]
        self.assertTrue(all(a > b for a, b in zip(prices, prices[1:])))

    def test_equity_and_liquidation_are_inverse(self):
        lv = tune.levels(10, 40, 31)
        for price in (4, 9, 13, 17, 23):
            equity = tune.equity_for(lv, 0.5, 5, 25, price)
            result = tune.liq_price(lv, 0.5, 5, 25, equity)
            self.assertLess(abs(result - price) / price, 1e-6)
            self.assertLess(abs(tune.equity_for(lv, 0.5, 5, 25, result) - equity) / equity, 1e-6)

    def test_liquidation_limits(self):
        lv = tune.levels(10, 40, 31)
        self.assertEqual(tune.liq_price(lv, 0.5, 5, 25, 0), 25)
        self.assertEqual(tune.liq_price(lv, 0.5, 5, 25, 10000), 0)


class TuneReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = self.root / 'ledger.sqlite'
        self.live = self.root / 'live.json'
        self.out = self.root / 'reports'
        self.records = [rec(
            ApiOrderId=oid, Key=f'{symbol}|2026-08-01 10:00:00', Symbol=symbol,
            Bottom=10, Top=80, Row=61, PerVolume=0.5, Position=20, MarkPrice=25,
            EstimateLiqDown=15, Investment=200, ExtraMargin=50, FundingFee=-2,
            RawJson={'order': {'buOrderData': {'exchangeOrderPairedCount': 100}},
                     'token': 'ghp_fake_test_only'},
        ) for oid, symbol in (('fake-a', 'ALPHA/USDT'), ('fake-b', 'BETA/USDT'))]
        with patch('v2.store.usdt_twd', return_value=FX), patch('v2.store.BACKUP_DIR', self.root / 'backups'):
            store.ingest(snap('2026-08-26', self.records), self.db)
            self.records = [{**r, 'GridProfit': 16, 'FundingFee': -4} for r in self.records]
            store.ingest(snap('2026-08-28', self.records), self.db)
        self.live.write_text(json.dumps(snap('2026-08-29', [
            {**r, 'MarkPrice': 30} for r in self.records])), encoding='utf-8')

    def test_collect_averages_marks_and_rounding(self):
        data = tune.collect(self.db, self.live)
        self.assertEqual(len(data['rows']), 2)
        for row in data['rows']:
            self.assertEqual(row['price'], 30)
            self.assertEqual(row['days30'], 2)
            self.assertEqual(row['grid_day'], 3)
            self.assertEqual(row['fund_day'], -1)
            step = (8 ** (1 / 60) - 1) * 100
            self.assertEqual(row['step_pct'], round(step, 4))
            self.assertEqual(row['net_pct'], round(step - 0.04, 4))
            self.assertEqual(row['pos_in_range'], round(math.log(3) / math.log(8), 3))
            self.assertEqual(row['liq_dist_pct'], 50)
            self.assertEqual(row['paired'], 100)

    def test_model_scenarios_and_does_not_mutate_collection(self):
        data = tune.collect(self.db, self.live)
        result = tune.model(data, self.db)
        self.assertNotIn('model', data['rows'][0])
        for row in result['rows']:
            m = row['model']
            self.assertAlmostEqual(m['model_liq_check'], row['liq'])
            self.assertEqual(m['high60'], 25)
            tr = m['trim']
            self.assertEqual(tr['top'], 37.5)
            self.assertEqual(tr['position'], round(tune.position_at(
                tune.levels(10, 37.5, tr['rows']), 0.5, 30), 4))
            self.assertGreater(tr['liq_dist_pct'], row['liq_dist_pct'])
            self.assertEqual(tr['rebuild_fee'], round((20 + tr['position']) * 30 * tune.FEE_CLOSE, 2))

    def test_write_is_read_only_and_keeps_raw_data_out_of_html(self):
        before = self.db.read_bytes()
        with patch('v2.store.connect', side_effect=AssertionError('DDL connection forbidden')), \
                patch('urllib.request.urlopen', side_effect=AssertionError('network forbidden')):
            path = tune.write('2026-09-01', self.db, self.live, self.out)
        self.assertEqual(path, self.out / '2026-09-01-倉位參數調整.html')
        page = path.read_text(encoding='utf-8')
        for r in self.records:
            self.assertIn(r['Symbol'], page)
        for forbidden in ('ghp_', 'token', '<script', 'https://', 'http://'):
            self.assertNotIn(forbidden, page)
        self.assertEqual(self.db.read_bytes(), before)
        self.assertFalse(Path(str(self.db) + '-wal').exists())
        self.assertFalse(Path(str(self.db) + '-shm').exists())

    def test_missing_liquidation_and_arithmetic_grid(self):
        data = tune.collect(self.db, self.live)
        data['rows'][0].update(liq=0, liq_dist_pct=None, type='arithmetic')
        result = tune.model(data, self.db)
        self.assertIsNone(result['rows'][0]['model']['e0'])
        self.assertIsNone(result['rows'][0]['model']['k_at_target'])
        self.assertIn('資料不足', tune.render(result))

    def test_empty_positions_render_and_escaped_labels(self):
        data = tune.model(tune.collect(self.db, self.live), self.db)
        data['rows'][0]['symbol'] = '<ALPHA>&/USDT'
        page = tune.render(data)
        self.assertIn('&lt;ALPHA&gt;&amp;/USDT', page)
        data['rows'] = []
        self.assertIn('沒有運行中的倉位', tune.render(data))

    def test_missing_database_does_not_create_file(self):
        missing = self.root / 'missing.sqlite'
        with self.assertRaises(FileNotFoundError):
            tune.collect(missing, self.live)
        self.assertFalse(missing.exists())

    def test_highs_use_latest_capture_day_not_report_day(self):
        con = sqlite3.connect(self.db)
        try:
            con.execute("UPDATE capture_runs SET capture_date='2026-06-28', captured_at='2026-06-28T00:00:00+08:00' WHERE id=1")
            con.execute("UPDATE grid_snapshots SET mark_price_s='200' WHERE run_id=1")
            con.commit()
        finally:
            con.close()
        data = tune.collect(self.db, self.live)
        data['date'] = '2026-12-01'
        result = tune.model(data, self.db)
        self.assertTrue(all(r['model']['high60'] == 25 for r in result['rows']))

    def test_missing_live_mark_falls_back_to_ledger(self):
        self.live.write_text(json.dumps({'Records': []}), encoding='utf-8')
        data = tune.collect(self.db, self.live)
        self.assertTrue(all(r['price'] == 25 for r in data['rows']))
        self.assertIsNone(data['live_at'])

    def test_write_defaults_and_cli_html(self):
        with patch('v2.store.DEFAULT_DB', self.db), patch('v2.live.LIVE_PATH', self.live), \
                patch('v2.healthcheck.OUT_DIR', self.out), redirect_stdout(io.StringIO()) as stdout:
            self.assertEqual(tune.main(['--date', '2026-09-01']), 0)
            self.assertEqual(tune.write('2026-09-01'), self.out / '2026-09-01-倉位參數調整.html')
        self.assertIn('倉位參數調整.html', stdout.getvalue())

    def test_cli_json_matches_model_and_does_not_write_html(self):
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            result = tune.main(['--db', str(self.db), '--live', str(self.live),
                                '--date', '2026-09-01', '--out', str(self.out), '--json'])
        self.assertEqual(result, 0)
        self.assertEqual(json.loads(stdout.getvalue()), tune.model(tune.collect(self.db, self.live), self.db))
        self.assertFalse(self.out.exists())

    def test_healthcheck_tune_bypasses_market_build(self):
        with patch('sys.argv', ['healthcheck', '--tune', '--date', '2026-09-01', '--db', str(self.db)]), \
                patch('v2.tune_report.write', return_value=self.out / 'report.html') as write, \
                patch('v2.healthcheck.build', side_effect=AssertionError('market build forbidden')), \
                redirect_stdout(io.StringIO()) as stdout:
            self.assertEqual(healthcheck.main(), 0)
        write.assert_called_once_with('2026-09-01', self.db)
        self.assertIn('report.html', stdout.getvalue())


if __name__ == '__main__':
    unittest.main()
