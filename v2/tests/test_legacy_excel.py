import json
import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from v2 import store
from v2.legacy_excel import ledger_rows
from v2.tests.test_store import FX, rec, snap


class LegacyExcelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / 'test.sqlite'
        for p in [patch('v2.store.usdt_twd', return_value=FX),
                  patch('v2.store.BACKUP_DIR', Path(self.temp.name) / 'backups')]:
            p.start()
            self.addCleanup(p.stop)

    def capture(self, day, records):
        store.ingest(snap(day, records), self.db)

    def test_withdraw_close_late_details_and_repeated_export(self):
        self.capture('2026-09-18', [rec(GridProfit=1500, ProfitWithdrawn=0)])
        self.capture('2026-09-19', [rec(GridProfit=1505, ProfitWithdrawn=1000)])
        self.capture('2026-09-20', [])
        pending = ledger_rows(self.db, date(2026, 9, 20))['rows'][-2]
        self.assertEqual(pending[3], 'g1')
        self.assertIsNone(pending[10])
        self.assertEqual(pending[11], 5)
        self.assertEqual(pending[7], '關倉待補')
        self.capture('2026-09-21', [rec(ListStatus='finished', GridProfit=1508,
                                     ProfitWithdrawn=1000, TotalProfit=-195)])
        payload = ledger_rows(self.db, date(2026, 9, 21))
        totals = [r for r in payload['rows'] if r[3] == '__DAILY_TOTAL__']
        self.assertEqual([r[10] for r in totals], [0, 5, 3, 0])
        self.assertEqual([r[11] for r in totals], [0, 5, 8, 8])
        self.assertEqual([r[8] for r in totals], [1500, 1505, 0, 0])
        self.assertEqual(payload, ledger_rows(self.db, date(2026, 9, 21)))
        self.assertTrue(all(len(r) == 16 for r in payload['rows']))
        self.assertNotIn('-195', json.dumps(payload))

    def test_coin_daily_uses_native_increment_not_usdt_stock_change(self):
        for day, profit, price in [('2026-09-18', .15, 2000), ('2026-09-19', .155, 2100)]:
            self.capture(day, [rec(Product='coin_margined_contract_grid', Symbol='ETH',
                GridProfit=profit*price, RawGridProfit=profit, ConversionPrice=price,
                ConversionSymbol='ETH_USDT')])
        latest = ledger_rows(self.db, date(2026, 9, 19))['rows'][-1]
        self.assertEqual(latest[10], 10.5)
        self.assertEqual(latest[11], 10.5)

    def test_stale_or_missing_database_is_not_silently_used(self):
        with self.assertRaises(sqlite3.OperationalError):
            ledger_rows(self.db, date(2026, 9, 19))
        self.assertFalse(self.db.exists())
        self.capture('2026-09-18', [rec()])
        with self.assertRaisesRegex(ValueError, '今日 v2 帳本'):
            ledger_rows(self.db, date(2026, 9, 19))
        self.assertEqual(ledger_rows(self.db, date(2026, 9, 19), True)['latest_date'], '2026-09-18')
