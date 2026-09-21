import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from v2 import store, settlements
from v2.tests.test_store import rec, snap, FX


class SettlementTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / 'ledger.sqlite'
        for p in [patch('v2.store.usdt_twd', return_value=FX), patch('v2.store.BACKUP_DIR', Path(self.tmp.name)/'backups')]:
            p.start()
            self.addCleanup(p.stop)

    def capture(self, date, rows, **kwargs):
        return store.ingest(snap(date, rows), self.db, **kwargs)

    def day(self, date):
        con = store.connect(self.db)
        try:
            return dict(con.execute('SELECT * FROM daily_summary WHERE capture_date=?', (date,)).fetchone())
        finally:
            con.close()

    def row(self):
        return settlements.get_settlements(self.db)['rows'][0]

    def test_close_loss_separate_from_grid_and_wallet(self):
        self.capture('2026-09-18', [rec(GridProfit=100)])
        self.capture('2026-09-19', [rec(ListStatus='finished', GridProfit=105, TotalProfit=-195, Closed='2026-09-19 18:00:00')])
        self.assertEqual(self.day('2026-09-19')['daily_profit_i'],store.to_fixed(5))
        self.assertEqual(self.row()['reported_total_profit'],-195)
        self.assertIsNone(self.row()['net_profit'])
        settlements.save_manual('g1', {'net_profit': '-195'}, self.db)
        self.assertEqual(self.row()['non_grid_profit'], '-300')
        self.capture('2026-09-20', [])
        self.assertEqual(self.day('2026-09-20')['daily_profit_i'],0)
        self.assertEqual(self.day('2026-09-20')['true_profit_i'],store.to_fixed(105))
        self.assertEqual(self.day('2026-09-20')['cumulative_i'],0)
        ledger=store.ledger_publish_payload(self.db)
        self.assertEqual(ledger['schema'],1)
        self.assertEqual(ledger['settlements'][0]['net_profit'],'-195')
        self.assertIsNone(ledger['wallet']['usdt'])
        self.assertNotIn('RawJson',json.dumps(ledger))

    def test_late_details_backfill_original_interval_and_repeat_replace(self):
        self.capture('2026-09-18',[rec(GridProfit=100)])
        self.capture('2026-09-19',[])
        closed=rec(ListStatus='finished',GridProfit=105,Closed='2026-09-19 18:00:00')
        self.capture('2026-09-20',[closed])
        self.assertEqual(self.day('2026-09-19')['daily_profit_i'],store.to_fixed(5))
        self.assertEqual(self.day('2026-09-19')['unresolved_count'],0)
        self.assertEqual(self.day('2026-09-20')['daily_profit_i'],0)
        self.capture('2026-09-20',[closed],replace_date=True)
        settlements.refresh(snap('2026-09-20',[closed]),self.db)
        self.assertEqual(self.day('2026-09-20')['true_profit_i'],store.to_fixed(105))
        self.assertEqual(len(settlements.get_settlements(self.db)['rows']),1)

    def test_replace_close_day_keeps_final_increment(self):
        self.capture('2026-09-18',[rec(GridProfit=100)])
        closed=rec(ListStatus='finished',GridProfit=105)
        self.capture('2026-09-19',[closed])
        self.capture('2026-09-19',[closed],replace_date=True)
        self.assertEqual(self.day('2026-09-19')['daily_profit_i'],store.to_fixed(5))
        self.assertEqual(self.day('2026-09-19')['closed_count'],1)

    def test_manual_audit_persists_and_api_does_not_overwrite(self):
        self.capture('2026-09-18',[rec(GridProfit=100)])
        self.capture('2026-09-19',[])
        manual={'grid_profit':'105','net_profit':'-195','closed_at':'2026-09-19T18:00:00+08:00','note':'結算明細'}
        settlements.save_manual('g1',manual,self.db)
        settlements.save_manual('g1',manual,self.db)
        self.assertEqual(self.row()['audit_count'],1)
        settlements.refresh(snap('2026-09-20',[rec(ListStatus='finished',GridProfit=106)]),self.db)
        self.assertEqual(self.row()['grid_profit'],'105')
        self.assertEqual(self.day('2026-09-19')['daily_profit_i'],store.to_fixed(5))
        manual['grid_profit']='104'
        settlements.save_manual('g1',manual,self.db)
        self.assertEqual(self.row()['audit_count'],2)
        self.assertEqual(self.day('2026-09-19')['daily_profit_i'],store.to_fixed(4))

    def test_clear_manual_returns_to_pending(self):
        self.capture('2026-09-18',[rec(GridProfit=100)])
        self.capture('2026-09-19',[])
        settlements.save_manual('g1',{'grid_profit':'105'},self.db)
        settlements.save_manual('g1',{},self.db)
        self.assertEqual(self.row()['status'],'pending')
        self.assertEqual(self.day('2026-09-19')['unresolved_count'],1)
        self.assertEqual(self.day('2026-09-19')['true_profit_i'],store.to_fixed(100))

    def test_withdrawal_not_double_counted_and_full_scope_required(self):
        self.capture('2026-09-18',[rec(GridProfit=100)])
        self.capture('2026-09-19',[rec(ListStatus='finished',GridProfit=55,ProfitWithdrawn=50)])
        self.assertEqual(self.day('2026-09-19')['daily_profit_i'],store.to_fixed(5))
        settlements.save_manual('g1',{'net_profit':'-195'},self.db)
        self.assertIsNone(self.row()['non_grid_profit'])
        settlements.save_manual('g1',{'net_profit':'-195','lifetime_grid_profit':'105'},self.db)
        self.assertEqual(self.row()['non_grid_profit'],'-300')

    def test_coin_requires_frozen_conversion_not_live_price(self):
        base=dict(Product='coin_margined_contract_grid',Symbol='ETH',RawGridProfit='0.1',ConversionPrice=2000,GridProfit=200)
        self.capture('2026-09-18',[rec(**base)])
        closed=rec(**{**base,'ListStatus':'finished','RawGridProfit':'0.11','GridProfit':330,'ConversionPrice':3000})
        self.capture('2026-09-19',[closed])
        self.assertIsNone(self.row()['grid_profit_usdt'])
        self.assertEqual(self.day('2026-09-19')['unresolved_count'],1)
        settlements.save_manual('g1',{'settlement_usdt_rate':'2500'},self.db)
        self.assertEqual(self.day('2026-09-19')['daily_profit_i'],store.to_fixed(25))
        closed['ConversionPrice']=4000
        closed['GridProfit']=440
        settlements.refresh(snap('2026-09-20',[closed]),self.db)
        self.assertEqual(self.row()['grid_profit_usdt'],'275')
        self.assertEqual(self.day('2026-09-19')['daily_profit_i'],store.to_fixed(25))

    def test_validation_and_zero_values(self):
        self.capture('2026-09-18',[rec(GridProfit=0)])
        self.capture('2026-09-19',[rec(ListStatus='finished',GridProfit=0,Closed='2026-09-19 18:00:00')])
        for body in [{'net_profit':'NaN'},{'net_profit':'Infinity'},{'settlement_usdt_rate':0},{'closed_at':'invalid'},{'oops':1}]:
            with self.assertRaises(store.CaptureError):
                settlements.save_manual('g1',body,self.db)
        settlements.save_manual('g1',{'net_profit':0},self.db)
        self.assertEqual(self.row()['status'],'settled')
        self.assertEqual(self.row()['non_grid_profit'],'0')

    def test_schema_10_backfills_existing_closure_without_changing_daily(self):
        self.capture('2026-09-18',[rec(GridProfit=100)])
        self.capture('2026-09-19',[rec(ListStatus='finished',GridProfit=105,TotalProfit=-195,Closed='2026-09-19 18:00:00')])
        before=self.day('2026-09-19')
        con=store.connect(self.db)
        con.execute('DELETE FROM grid_settlements')
        con.execute('UPDATE schema_meta SET version=10')
        con.commit();con.close()
        self.assertEqual(self.row()['reported_total_profit'],'-195')
        self.assertIsNone(self.row()['net_profit'])
        self.assertEqual(self.day('2026-09-19'),before)

    def test_refresh_before_next_daily_preserves_wallet_and_daily_snapshot(self):
        self.capture('2026-09-18',[rec(GridProfit=100)])
        before=self.day('2026-09-18')
        closed=rec(ListStatus='finished',GridProfit=105,Closed='2026-09-19 18:00:00')
        settlements.refresh(snap('2026-09-19',[closed]),self.db)
        self.assertEqual(self.day('2026-09-18'),before)
        self.assertFalse(self.row()['daily_reconciled'])
        self.capture('2026-09-20',[closed])
        self.assertEqual(self.day('2026-09-20')['daily_profit_i'],store.to_fixed(5))
        self.assertEqual(self.row()['capture_date'],'2026-09-20')

    def test_partial_finished_record_uses_previous_investment(self):
        self.capture('2026-09-18',[rec(GridProfit=100)])
        self.capture('2026-09-19',[rec(ListStatus='finished',GridProfit=105,Complete=False,Investment=None)])
        self.assertEqual(self.day('2026-09-19')['daily_profit_i'],store.to_fixed(5))
        self.assertEqual(self.row()['status'],'partial')

    def test_realized_and_total_fee_preserved_but_not_assumed_net(self):
        self.capture('2026-09-18',[rec(GridProfit=100)])
        self.capture('2026-09-19',[rec(ListStatus='finished',GridProfit=105,RawJson={
            'order': {'buOrderData': {'totalRealizedProfit':'-510.24677','totalFee':'-117.61731318'}}
        })])
        self.assertEqual(self.row()['reported_realized_profit'],'-510.24677')
        self.assertEqual(self.row()['fee'],'-117.61731318')
        self.assertIsNone(self.row()['net_profit'])
