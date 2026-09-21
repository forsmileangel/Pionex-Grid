"""Closed-bot facts and explicit manual corrections, separate from wallet values."""
from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation

from . import store

SCHEMA = """
CREATE TABLE IF NOT EXISTS grid_settlements (
  bu_order_id TEXT PRIMARY KEY REFERENCES grid_positions(bu_order_id),
  observed_at TEXT NOT NULL,
  api_record_json TEXT NOT NULL DEFAULT '{}',
  manual_json TEXT NOT NULL DEFAULT '{}',
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settlement_audit (
  id INTEGER PRIMARY KEY,
  bu_order_id TEXT NOT NULL,
  changed_at TEXT NOT NULL,
  before_json TEXT NOT NULL,
  after_json TEXT NOT NULL
);
"""


def install(con):
    # execute individually: executescript would commit the caller's transaction.
    for statement in SCHEMA.split(';'):
        if statement.strip():
            con.execute(statement)


def seed_history(con):
    """Recover already-recorded closures without inventing a settlement price."""
    for pos in con.execute("SELECT * FROM grid_positions WHERE lifecycle != 'active'").fetchall():
        oid = pos['bu_order_id']
        if con.execute('SELECT 1 FROM grid_settlements WHERE bu_order_id=?', (oid,)).fetchone():
            continue
        snap = con.execute("""SELECT s.*,r.captured_at FROM grid_snapshots s
            JOIN capture_runs r ON r.id=s.run_id WHERE s.bu_order_id=? AND s.list_status='finished'
            ORDER BY r.capture_date DESC,s.id DESC LIMIT 1""", (oid,)).fetchone()
        rec = {}
        if snap:
            rec = {k: store.from_fixed(snap[v]) for k, v in {
                'GridProfit': 'grid_profit_i', 'TotalProfit': 'total_profit_i',
                'FundingFee': 'funding_fee_i', 'Fee': 'fee_i', 'Investment': 'investment_i',
                'ProfitWithdrawn': 'profit_withdrawn_i', 'ProfitReinvest': 'profit_reinvest_i',
                'ProfitReduce': 'profit_reduce_i', 'RawGridProfit': 'grid_profit_coin_i',
            }.items()}
            rec.update(ApiOrderId=oid, Symbol=pos['symbol'], Product=pos['product'],
                       Closed=snap['closed_at'], Created=pos['created_at'], ListStatus='finished',
                       RawJson=json.loads(snap['raw_json'] or '{}'))
        observed = snap['captured_at'] if snap else store.taipei_now().isoformat()
        con.execute('INSERT INTO grid_settlements VALUES (?,?,?,?,?)',
                    (oid, observed, json.dumps(rec, ensure_ascii=False), '{}', observed))


def observe(con, snapshot):
    stamp = store.taipei_now(snapshot.get('CapturedAt')).isoformat()
    positions = store._positions(con)
    for rec in snapshot.get('Records', []):
        oid = str(rec.get('ApiOrderId') or '')
        if rec.get('ListStatus') != 'finished' or oid not in positions:
            continue
        existing = con.execute('SELECT * FROM grid_settlements WHERE bu_order_id=?', (oid,)).fetchone()
        # Nulls in a later partial response must not erase a previously returned fact.
        old = json.loads(existing['api_record_json']) if existing else {}
        old.update({k: v for k, v in rec.items() if v is not None and v != ''})
        con.execute("""INSERT INTO grid_settlements VALUES (?,?,?,?,?)
            ON CONFLICT(bu_order_id) DO UPDATE SET api_record_json=excluded.api_record_json,
            updated_at=excluded.updated_at""",
            (oid, stamp, json.dumps(old, ensure_ascii=False), '{}', stamp))
    for row in con.execute("""SELECT DISTINCT d.bu_order_id,r.captured_at FROM daily_grid_profit d
        JOIN capture_runs r ON r.id=d.run_id WHERE d.status IN ('closed','closed_unresolved')
        ORDER BY r.captured_at""").fetchall():
        con.execute('INSERT OR IGNORE INTO grid_settlements VALUES (?,?,?,?,?)',
                    (row['bu_order_id'], row['captured_at'], '{}', '{}', stamp))


def _facts(row, pos):
    rec = json.loads(row['api_record_json'])
    manual = json.loads(row['manual_json'])
    coin = pos['product'] == 'coin_margined_contract_grid'
    currency = (pos['symbol'].split('/')[-1] if '/' in pos['symbol'] else pos['symbol']) if coin else 'USDT'
    raw = (rec.get('RawJson') or {}).get('order', {})
    data = raw.get('buOrderData') or {}
    currency = rec.get('SettlementCurrency') or raw.get('quote') or currency
    def value(key, api):
        return manual[key] if key in manual else api
    grid = value('grid_profit', (rec.get('RawGridProfit') if coin else rec.get('GridProfit')))
    reported = rec.get('RawTotalProfit', data.get('totalProfit')) if coin else rec.get('TotalProfit')
    funding = value('funding_fee', rec.get('RawFundingFee', data.get('totalFundingFee')) if coin else rec.get('FundingFee'))
    api_fee = rec.get('RawFee') if coin else rec.get('Fee')
    if api_fee is None:
        api_fee = data.get('totalFee', data.get('fee'))
    fee = value('fee', api_fee)
    realized = rec.get('RawRealizedProfit')
    if realized is None:
        realized = data.get('totalRealizedProfit')
    # No known API net-settlement field: totalProfit has unverified fee scope.
    net = manual.get('net_profit')
    rate = manual.get('settlement_usdt_rate') if coin else '1'
    rate_i = store.to_fixed(rate)
    def usd(amount):
        if amount is None or not rate_i:
            return None
        return store.from_fixed(store._mul_fixed(store.to_fixed(amount), rate_i))
    return dict(rec=rec, manual=manual, coin=coin, currency=currency,
                closed_at=value('closed_at', rec.get('Closed')),
                grid_profit=grid, reported_total_profit=reported, reported_realized_profit=realized, funding_fee=funding, fee=fee,
                net_profit=net, settlement_usdt_rate=rate,
                grid_profit_usdt=usd(grid), net_profit_usdt=usd(net),
                funding_fee_usdt=usd(funding), fee_usdt=usd(fee))


def apply_to_daily(con):
    """Update the ORIGINAL closing interval; never add catch-up income to today."""
    for row in con.execute('SELECT * FROM grid_settlements').fetchall():
        pos = con.execute('SELECT * FROM grid_positions WHERE bu_order_id=?', (row['bu_order_id'],)).fetchone()
        facts = _facts(row, pos)
        target = con.execute("""SELECT * FROM daily_grid_profit WHERE bu_order_id=?
            AND status IN ('closed','closed_unresolved') ORDER BY capture_date,id LIMIT 1""",
            (row['bu_order_id'],)).fetchone()
        if not target:
            continue
        if facts['grid_profit_usdt'] is None:
            # Clearing a manual override must also remove its derived daily value.
            con.execute("DELETE FROM grid_snapshots WHERE run_id=? AND bu_order_id=? AND list_status='finished'",
                        (target['run_id'], row['bu_order_id']))
            con.execute("UPDATE daily_grid_profit SET status='closed_unresolved',event='closed_unresolved' WHERE id=?", (target['id'],))
            con.execute("UPDATE grid_positions SET lifecycle='closed_unresolved',closed_at=? WHERE bu_order_id=?",
                        (facts['closed_at'],row['bu_order_id']))
            continue
        prev = con.execute("""SELECT * FROM daily_grid_profit WHERE bu_order_id=? AND capture_date<?
            AND status IN ('baseline','new','continue') ORDER BY capture_date DESC LIMIT 1""",
            (row['bu_order_id'], target['capture_date'])).fetchone()
        if not prev:
            continue
        rec = facts['rec']
        grid_i = store.to_fixed(facts['grid_profit_usdt'])
        inv_i = store.to_fixed(rec.get('Investment'))
        if inv_i is None:
            inv_i = prev['investment_i']
        stocks = []
        for key, column in [('ProfitWithdrawn', 'withdrawn_i'), ('ProfitReinvest', 'reinvest_i'), ('ProfitReduce', 'reduce_i')]:
            if facts['coin']:
                raw_key = 'Raw' + key
                stocks.append(store._mul_fixed(store.to_fixed(rec[raw_key]), store.to_fixed(facts['settlement_usdt_rate']))
                              if rec.get(raw_key) is not None else prev[column])
            else:
                stocks.append(store.to_fixed(rec[key]) if rec.get(key) is not None else prev[column])
        coin_i = store.to_fixed(facts['grid_profit']) if facts['coin'] else None
        px_i = store.to_fixed(facts['settlement_usdt_rate']) if facts['coin'] else None
        # Snapshot belongs to the original interval even when the API arrived later.
        con.execute("""INSERT INTO grid_snapshots(run_id,bu_order_id,list_status,symbol,created_at,
            closed_at,product,investment_i,grid_profit_i,grid_profit_coin_i,conversion_price_i,
            profit_withdrawn_i,profit_reinvest_i,profit_reduce_i,total_profit_i,funding_fee_i,fee_i,complete,raw_json)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(run_id,bu_order_id) DO UPDATE SET
              closed_at=excluded.closed_at,investment_i=excluded.investment_i,grid_profit_i=excluded.grid_profit_i,
              grid_profit_coin_i=excluded.grid_profit_coin_i,conversion_price_i=excluded.conversion_price_i,
              profit_withdrawn_i=excluded.profit_withdrawn_i,profit_reinvest_i=excluded.profit_reinvest_i,
              profit_reduce_i=excluded.profit_reduce_i,funding_fee_i=excluded.funding_fee_i,
              fee_i=excluded.fee_i,total_profit_i=excluded.total_profit_i,
              complete=excluded.complete,raw_json=excluded.raw_json""",
            (target['run_id'],row['bu_order_id'],'finished',pos['symbol'],pos['created_at'],
             facts['closed_at'],pos['product'],inv_i,grid_i,coin_i,px_i,*stocks,
             store.to_fixed(facts['reported_total_profit']) if not facts['coin'] else None,
             store.to_fixed(facts['funding_fee_usdt']),store.to_fixed(facts['fee_usdt']),1,
             json.dumps(rec.get('RawJson') or {},ensure_ascii=False)))
        con.execute("UPDATE daily_grid_profit SET status='closed',prev_grid_profit_i=? WHERE id=?",
                    (prev['grid_profit_i'],target['id']))
        con.execute("UPDATE grid_positions SET lifecycle='closed',closed_at=? WHERE bu_order_id=?",
                    (facts['closed_at'],row['bu_order_id']))
    store.rebuild_daily_profits(con)
    con.execute("""UPDATE daily_summary SET
        closed_count=(SELECT count(*) FROM daily_grid_profit d WHERE d.capture_date=daily_summary.capture_date AND d.status='closed'),
        unresolved_count=(SELECT count(*) FROM daily_grid_profit d WHERE d.capture_date=daily_summary.capture_date AND d.status='closed_unresolved')""")


def payload(con):
    rows = []
    for row in con.execute('SELECT * FROM grid_settlements ORDER BY observed_at DESC,bu_order_id'):
        pos = con.execute('SELECT * FROM grid_positions WHERE bu_order_id=?', (row['bu_order_id'],)).fetchone()
        f = _facts(row, pos)
        daily = con.execute("""SELECT * FROM daily_grid_profit WHERE bu_order_id=?
            AND status IN ('closed','closed_unresolved') ORDER BY capture_date LIMIT 1""", (row['bu_order_id'],)).fetchone()
        # A total lifetime amount is safe automatically only for bots with no profit stock movements.
        moved = con.execute("""SELECT 1 FROM daily_grid_profit WHERE bu_order_id=? AND
            (COALESCE(withdrawn_i,0)<>0 OR COALESCE(reinvest_i,0)<>0 OR COALESCE(reduce_i,0)<>0
             OR event LIKE '%reinvest%' OR event LIKE '%withdraw%' OR event LIKE '%reduce%') LIMIT 1""",
             (row['bu_order_id'],)).fetchone()
        moved = moved or any(f['rec'].get(k) not in (None,0,'0','0.0') for k in ['ProfitWithdrawn','ProfitReinvest','ProfitReduce'])
        lifetime = f['manual'].get('lifetime_grid_profit')
        if lifetime is None and not moved:
            lifetime = f['grid_profit']
        non_grid = None
        if lifetime is not None and f['net_profit'] is not None:
            non_grid = store.from_fixed(store.to_fixed(f['net_profit']) - store.to_fixed(lifetime))
        complete = f['closed_at'] and f['grid_profit'] is not None and f['net_profit'] is not None
        rows.append({
            'id': row['bu_order_id'], 'symbol': pos['symbol'], 'currency': f['currency'],
            'closed_at': f['closed_at'], 'observed_at': row['observed_at'], 'updated_at': row['updated_at'],
            'capture_date': daily['capture_date'] if daily else None,
            'status': 'settled' if complete else 'partial' if f['grid_profit'] is not None else 'pending',
            'source': 'manual+api' if f['manual'] else 'api',
            **{k: f[k] for k in ['grid_profit','grid_profit_usdt','net_profit','net_profit_usdt',
                               'reported_total_profit','reported_realized_profit','funding_fee','fee','settlement_usdt_rate']},
            'lifetime_grid_profit': lifetime, 'non_grid_profit': non_grid,
            'recorded_grid_profit_usdt': store.from_fixed(daily['lifetime_i']) if daily else None,
            'daily_reconciled': bool(daily and daily['status']=='closed'),
            'manual': f['manual'],
            'audit_count': con.execute('SELECT count(*) FROM settlement_audit WHERE bu_order_id=?', (row['bu_order_id'],)).fetchone()[0],
        })
    return {'rows': rows, 'pending_count': sum(r['status']!='settled' for r in rows)}


def get_settlements(db_path=None):
    con = store.connect(db_path)
    try:
        return payload(con)
    finally:
        con.close()


def refresh(snapshot, db_path=None):
    con = store.connect(db_path)
    try:
        observe(con, snapshot)
        apply_to_daily(con)
        con.commit()
        store._backup(store.Path(db_path or store.DEFAULT_DB))
        return payload(con)
    finally:
        con.close()


def save_manual(oid, body, db_path=None):
    allowed = {'closed_at','grid_profit','lifetime_grid_profit','net_profit','funding_fee','fee','settlement_usdt_rate','note'}
    if not isinstance(body, dict) or set(body) - allowed:
        raise store.CaptureError('補登欄位不正確')
    manual = {}
    for key, value in body.items():
        if value is None or str(value).strip() == '':
            continue
        text = str(value).strip()
        if key == 'note':
            if len(text) > 1000:
                raise store.CaptureError('備註最多 1000 字')
            manual[key] = text
        elif key == 'closed_at':
            try:
                stamp = store.taipei_now(text)
                if stamp > store.taipei_now():
                    raise ValueError('future')
                manual[key] = stamp.isoformat()
            except (ValueError, TypeError):
                raise store.CaptureError('請填有效的實際關倉時間（台北），不可晚於現在')
        else:
            try:
                number = Decimal(text)
                if not number.is_finite() or abs(number) >= Decimal('90000000000'):
                    raise ValueError('out of range')
                if key == 'settlement_usdt_rate' and number <= 0:
                    raise ValueError('non-positive rate')
                manual[key] = store.from_fixed(store.to_fixed(number))
            except (InvalidOperation, ValueError, OverflowError):
                raise store.CaptureError('金額須為有效數字，換算價須大於零')
    con = store.connect(db_path)
    try:
        row = con.execute('SELECT * FROM grid_settlements WHERE bu_order_id=?', (oid,)).fetchone()
        if row is None:
            raise store.CaptureError('找不到關倉紀錄，請先補抓關倉資料')
        encoded = json.dumps(manual,ensure_ascii=False,sort_keys=True)
        if json.loads(row['manual_json']) != manual:
            stamp = store.taipei_now().isoformat()
            con.execute('INSERT INTO settlement_audit(bu_order_id,changed_at,before_json,after_json) VALUES (?,?,?,?)',
                        (oid,stamp,row['manual_json'],encoded))
            con.execute('UPDATE grid_settlements SET manual_json=?,updated_at=? WHERE bu_order_id=?', (encoded,stamp,oid))
            apply_to_daily(con)
            con.commit()
            store._backup(store.Path(db_path or store.DEFAULT_DB))
        return payload(con)
    finally:
        con.close()
