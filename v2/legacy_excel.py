"""Read the v2 ledger for the original 16-column Excel table. No API or DB writes."""

from __future__ import annotations

import argparse
import json
import sqlite3
from contextlib import closing
from datetime import date, datetime
from pathlib import Path

from .store import DEFAULT_DB, SCALE, TAIPEI


def amount(value):
    return None if value is None else value / SCALE


def ledger_rows(db_path: Path, today: date, allow_stale: bool = False) -> dict:
    with closing(sqlite3.connect(db_path.resolve().as_uri() + '?mode=ro', uri=True)) as con:
        con.row_factory = sqlite3.Row
        con.execute('PRAGMA query_only=ON')
        con.execute('BEGIN')
        summaries = list(con.execute('''SELECT d.*, r.captured_at, r.status AS run_status
            FROM daily_summary d JOIN capture_runs r ON r.id=d.run_id ORDER BY d.capture_date'''))
        if not summaries or any(d['run_status'] != 'success' for d in summaries):
            raise ValueError('每日帳本尚未完成，保留原 Excel')
        latest = date.fromisoformat(summaries[-1]['capture_date'])
        if latest > today or (latest != today and not allow_stale):
            raise ValueError('今日 v2 帳本尚未完成，保留原 Excel；請在每日擷取後重跑')

        rows = []
        running_by_id = {}
        period_total = 0
        labels = {'baseline': '基準日', 'new': '新增', 'continue': '持續',
                  'closed': '關倉', 'closed_unresolved': '關倉待補',
                  'withdraw': '提取', 'reinvest': '複投', 'reduce': '減倉',
                  'add': '加倉', 'review': '待確認'}
        for day in summaries:
            timestamp = datetime.fromisoformat(day['captured_at']).astimezone(TAIPEI)
            details = list(con.execute('''SELECT d.*, p.symbol, p.created_at, p.product, s.leverage
                FROM daily_grid_profit d JOIN grid_positions p ON p.bu_order_id=d.bu_order_id
                LEFT JOIN grid_snapshots s ON s.run_id=d.run_id AND s.bu_order_id=d.bu_order_id
                WHERE d.capture_date=? ORDER BY p.symbol, p.created_at, d.bu_order_id''',
                (day['capture_date'],)))
            if sum(r['daily_profit_i'] or 0 for r in details) != day['daily_profit_i']:
                raise ValueError('每日明細與合計不一致，保留原 Excel')
            source = 'v2 SQLite' + ('（關倉待補，暫計）' if day['unresolved_count'] else '')
            previous = []
            for record in details:
                oid = record['bu_order_id']
                running_by_id[oid] = running_by_id.get(oid, 0) + (record['daily_profit_i'] or 0)
                if record['prev_grid_profit_i'] is not None:
                    previous.append(record['prev_grid_profit_i'])
                product = '幣本位合約網格' if record['product'] == 'coin_margined_contract_grid' else '合約網格'
                leverage = (record['leverage'] or '').replace(' long', ' 做多').replace(' short', ' 做空')
                status = '／'.join(labels.get(e, e) for e in (record['event'] or record['status']).split('+'))
                rows.append([day['capture_date'], timestamp.strftime('%H:%M:%S'), '網格', oid,
                    record['symbol'], record['created_at'], (product + ' ' + leverage).strip(), status,
                    amount(record['grid_profit_i']), amount(record['prev_grid_profit_i']),
                    amount(record['daily_profit_i']), amount(running_by_id[oid]),
                    amount(record['investment_i']), '否' if record['daily_profit_i'] is None else '是',
                    day['active_count'], source])
            period_total += day['daily_profit_i']
            rows.append([day['capture_date'], timestamp.strftime('%H:%M:%S'), '日總計', '__DAILY_TOTAL__',
                '合約網格合計', '', '合約網格', '日總計', amount(day['cumulative_i']),
                amount(sum(previous)) if previous else None, amount(day['daily_profit_i']),
                amount(period_total), amount(day['investment_i']), '否', day['active_count'], source])
        return {'source': 'v2-sqlite', 'dates': [d['capture_date'] for d in summaries],
                'note': '來源：v2 每日快照。每日利潤含關倉最後增量，提領不重複加回。累計欄為記錄期間每日利潤加總，基準日為 0；關倉待補時為暫計。',
                'schedule_note': '每日 08:12 更新 Excel，採 v2 每日快照時間。重跑會重建帳本涵蓋日期，補登回填原區間；當天帳本未完成時保留原檔。',
                'latest_date': latest.isoformat(), 'rows': rows}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, default=DEFAULT_DB)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--allow-stale', action='store_true')
    args = parser.parse_args()
    payload = ledger_rows(args.db, datetime.now(TAIPEI).date(), args.allow_stale)
    args.output.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
