"""Offline position tuning report, calibrated to the ledger's liquidation estimates.

The liquidation model is for long, linear grids: each crossed level buys one lot.
Daily averages and rounding follow the reference report (last 30 captured days).
"""
from __future__ import annotations

import argparse
import html
import json
import math
import sqlite3
from contextlib import closing
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_FLOOR
from pathlib import Path

from . import funding, healthcheck, live, store

MMR = 0.005
TARGET = 0.30
H = 0.0023
FEE_CLOSE = 0.0005
FEE_ROUND_TRIP = 0.0004
DEFAULT_TICK = 0.01


def max_levels(bottom, top, tick=DEFAULT_TICK, geometric=True) -> int:
    """Maximum price lines whose lowest interval is at least one tick.

    Return one (no intervals) when the range is narrower than a tick.
    Decimal arithmetic keeps exact arithmetic-grid boundaries from rounding down.
    """
    if not all(math.isfinite(x) for x in (bottom, top, tick)) or not 0 < bottom < top or tick <= 0:
        raise ValueError("Require finite 0 < bottom < top and tick > 0")
    if geometric:
        intervals = math.log(top / bottom) / math.log1p(tick / bottom)
        nearest = round(intervals)
        if math.isclose(intervals, nearest, rel_tol=0, abs_tol=1e-10):
            intervals = nearest
        return math.floor(intervals) + 1
    intervals = (Decimal(str(top)) - Decimal(str(bottom))) / Decimal(str(tick))
    return int(intervals.to_integral_value(rounding=ROUND_FLOOR)) + 1


def levels(bottom, top, rows, geometric=True):
    """Grid price lines; rows is the line count, not the interval count."""
    if not 0 < bottom < top or rows < 2:
        raise ValueError("Require 0 < bottom < top and at least two price lines")
    if geometric:
        ratio = (top / bottom) ** (1 / (rows - 1))
        return [bottom * ratio ** i for i in range(rows)]
    return [bottom + (top - bottom) * i / (rows - 1) for i in range(rows)]


def position_at(lv, quantity, price):
    return quantity * sum(v > price for v in lv)


def loss_and_qty(lv, q, q0, p, x):
    """Loss and position on a fall from p to x, buying at each line in [x, p)."""
    buys = [v for v in lv if x <= v < p]
    return q0 * (p - x) + sum(q * (v - x) for v in buys), q0 + q * len(buys)


def equity_for(lv, q, q0, p, liq):
    loss, qty = loss_and_qty(lv, q, q0, p, liq)
    return loss + MMR * qty * liq


def liq_price(lv, q, q0, p, equity):
    """Solve downward liquidation; 0 means equity covers a fall to zero."""
    lo, hi = 0.0, p
    if equity <= MMR * q0 * p:
        return p
    if equity >= equity_for(lv, q, q0, p, 0):
        return 0.0
    for _ in range(80):
        mid = (lo + hi) / 2
        if equity > equity_for(lv, q, q0, p, mid):
            hi = mid
        else:
            lo = mid
    return hi


def _scale_at_target(lv, q, q0, p, equity):
    # Preserve the reference scenario's search bounds and rounding.
    lo, hi = 0.05, 5.0
    for _ in range(60):
        k = (lo + hi) / 2
        if liq_price(lv, q * k, q0 * k, p, equity) <= p * (1 - TARGET):
            lo = k
        else:
            hi = k
    return round(lo, 2)


def _ledger(path):
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(path)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    return closing(con)


def _time(value):
    when = datetime.fromisoformat(value)
    return when.replace(tzinfo=store.TAIPEI) if when.tzinfo is None else when


def collect(db_path, live_path) -> dict:
    """Read the latest successful running positions, local marks and daily totals."""
    with _ledger(db_path) as con:
        run = con.execute("SELECT id, captured_at FROM capture_runs WHERE status='success' "
                          "ORDER BY capture_date DESC, id DESC LIMIT 1").fetchone()
        if run is None:
            raise ValueError("No successful capture in ledger")
        snaps = [dict(r) for r in con.execute(
            "SELECT * FROM grid_snapshots WHERE run_id=? AND list_status='running'", (run["id"],))]
        days = [r[0] for r in con.execute(
            "SELECT capture_date FROM daily_summary ORDER BY capture_date DESC LIMIT 30")]
        funds, _ = funding.funding_by_day(con)
        daily = {}
        for r in con.execute(
                f"SELECT bu_order_id, daily_profit_i, gap_days FROM daily_grid_profit "
                f"WHERE capture_date IN ({','.join('?' * len(days))}) AND status='continue' "
                "AND daily_profit_i IS NOT NULL", days):
            value = daily.setdefault(r["bu_order_id"], [0, 0])
            value[0] += r["daily_profit_i"]
            value[1] += 1 + (r["gap_days"] or 0)
    snapshot = json.loads(Path(live_path).read_text(encoding="utf-8"))
    marks = {str(r["ApiOrderId"]): r.get("MarkPrice") for r in snapshot["Records"]}
    now = _time(run["captured_at"])
    out = []
    for s in snaps:
        bd = (json.loads(s["raw_json"] or "{}").get("order") or {}).get("buOrderData") or {}
        top, bottom, rows = float(s["top_s"]), float(s["bottom_s"]), int(s["row_n"])
        p = float(marks.get(s["bu_order_id"]) or s["mark_price_s"])
        geo = s["grid_type"] == "geometric"
        levels(bottom, top, rows, geo)  # Validate before calculating spacing.
        step = ((top / bottom) ** (1 / (rows - 1)) - 1) * 100 if geo else (top - bottom) / (rows - 1) / p * 100
        u = math.log(p / bottom) / math.log(top / bottom) if geo else (p - bottom) / (top - bottom)
        inv, extra = (s["investment_i"] or 0) / store.SCALE, (s["extra_margin_i"] or 0) / store.SCALE
        liq = (s["estimate_liq_down_i"] or s["liquidation_price_i"] or 0) / store.SCALE
        pos = float(s["position_s"] or 0)
        age = (now - _time(s["created_at"])).total_seconds() / 86400
        paired = bd.get("exchangeOrderPairedCount")
        g30 = daily.get(s["bu_order_id"], [0, 0])
        f30 = sum(v["delta_i"] or 0 for (d, oid), v in funds.items()
                  if oid == s["bu_order_id"] and d in days) / store.SCALE
        out.append({
            "id": s["bu_order_id"], "symbol": s["symbol"], "leverage": s["leverage"], "type": s["grid_type"],
            "bottom": bottom, "top": top, "row": rows, "per_volume": s["per_volume_s"], "price": p,
            "step_pct": round(step, 4), "pionex_step_pct": bd.get("estimatedStepPct") or s["estimated_step_pct_s"],
            "net_pct": round(step - FEE_ROUND_TRIP * 100, 4), "pos_in_range": round(u, 3),
            "investment": inv, "extra": extra, "position": pos, "notional": round(pos * p, 2),
            "eff_lev": round(pos * p / (inv + extra), 2) if inv + extra else None,
            "liq": liq, "liq_dist_pct": round((p - liq) / p * 100, 1) if liq else None,
            "age_days": round(age, 1), "paired": paired,
            "paired_per_day": round(paired / age, 1) if paired and age > 0 else None,
            "grid30": round(g30[0] / store.SCALE, 2), "days30": g30[1],
            "grid_day": round(g30[0] / store.SCALE / g30[1], 2) if g30[1] else None,
            "yield_day_pct": round(g30[0] / store.SCALE / g30[1] / (inv + extra) * 100, 3) if g30[1] and inv + extra else None,
            "fund30": round(f30, 2), "fund_day": round(f30 / g30[1], 2) if g30[1] else None,
            "fund_share": round(f30 / (g30[0] / store.SCALE) * 100, 1) if g30[0] > 0 else None,
            "created": s["created_at"][:10], "top_bot_ratio": round(top / bottom, 3),
        })
    out.sort(key=lambda r: -(r["investment"] + r["extra"]))
    return {"as_of": run["captured_at"], "live_at": snapshot.get("CapturedAt"), "rows": out}


def model(data, db_path) -> dict:
    """Add margin, size, top-trim and rebuild scenarios without modifying data."""
    end = _time(data["as_of"]).date()
    since = (end - timedelta(days=60)).isoformat()
    with _ledger(db_path) as con:
        highs = {r["symbol"]: (r["low"], r["high"]) for r in con.execute(
            """SELECT s.symbol, MIN(CAST(s.mark_price_s AS REAL)) AS low,
                      MAX(CAST(s.mark_price_s AS REAL)) AS high
               FROM grid_snapshots s JOIN capture_runs r ON r.id=s.run_id
               WHERE r.status='success' AND s.list_status='running' AND s.mark_price_s IS NOT NULL
                 AND r.capture_date >= ? AND r.capture_date <= ? GROUP BY s.symbol""", (since, end.isoformat()))}
    out = []
    for original in data["rows"]:
        row = dict(original)
        b, t, n = row["bottom"], row["top"], row["row"]
        p, q, q0 = row["price"], float(row["per_volume"]), row["position"]
        geo = row["type"] == "geometric"
        lv = levels(b, t, n, geo)
        e0 = equity_for(lv, q, q0, p, row["liq"]) if row["liq"] else None
        low60, high60 = highs.get(row["symbol"], (None, None))
        m = {"e0": round(e0, 2) if e0 is not None else None, "low60": low60, "high60": high60, "q": q,
             "rows_for_h": round(math.log(t / b) / math.log(1 + H)) + 1,
             "model_liq_check": round(liq_price(lv, q, q0, p, e0), 6 if p < 10 else 2) if e0 is not None else None,
             "add_margin_for_target": None, "k_at_target": None, "release_at_target": None}
        row["model"] = m
        out.append(row)
        if e0 is None:
            continue
        need = equity_for(lv, q, q0, p, p * (1 - TARGET))
        m["add_margin_for_target"] = round(max(0.0, need - e0), 0)
        m["k_at_target"] = _scale_at_target(lv, q, q0, p, e0)
        # The reference extras use the already rounded equity and scale.
        m["release_at_target"] = round(max(0.0, m["e0"] - need), 0)
        t2 = max(high60 * 1.05, p * 1.25) if high60 else t
        if b < t2 < t * 0.97:
            n2 = (round(math.log(t2 / b) / math.log((t / b) ** (1 / (n - 1)))) + 1 if geo
                  else round((t2 - b) / ((t - b) / (n - 1))) + 1)
            n2 = max(2, n2)
            lv2 = levels(b, t2, n2, geo)
            q02 = position_at(lv2, q, p)
            cut = q0 - q02
            equity = e0 - cut * p * FEE_CLOSE
            liq2 = liq_price(lv2, q, q02, p, equity)
            tr = {"top": round(t2, 6 if t2 < 10 else 2), "rows": n2, "position": round(q02, 4),
                  "position_cut_pct": round(cut / q0 * 100, 1) if q0 else None,
                  "liq": round(liq2, 6 if p < 10 else 2), "liq_dist_pct": round((p - liq2) / p * 100, 1),
                  "close_fee": round(cut * p * FEE_CLOSE, 2), "k_at_target": _scale_at_target(lv2, q, q02, p, equity)}
            tr["rebuild_fee"] = round((q0 + tr["position"]) * p * FEE_CLOSE, 2)
            tr["fund_day_after"] = round((row["fund_day"] or 0) * tr["position"] / q0, 2) if q0 else None
            tr["fund_saved_day"] = round((row["fund_day"] or 0) - tr["fund_day_after"], 2) if tr["fund_day_after"] is not None else None
            m["trim"] = tr
        k = m["k_at_target"]
        if k > 1.05:
            kh = round(1 + (k - 1) / 2, 2)
            lh = liq_price(lv, q * kh, q0 * kh, p, m["e0"])
            m["half"] = {"k": kh, "grid_day": round((row["grid_day"] or 0) * kh, 2),
                         "fund_day": round((row["fund_day"] or 0) * kh, 2),
                         "liq_dist_pct": round((p - lh) / p * 100, 1),
                         "add_invest": round(row["investment"] * (kh - 1), 0)}
    return {**data, "rows": out}


def render(data) -> str:
    """Static, self-contained HTML; raw records and credentials are never embedded."""
    def fmt(value, digits=2):
        return "—" if value is None else f"{value:,.{digits}f}"

    cards, table = [], []
    for r in data["rows"]:
        m = r["model"]
        name = html.escape(f"{r['symbol']} · {r['leverage'] or ''}")
        label = "資料不足" if m["e0"] is None else "補保證金" if m["add_margin_for_target"] else "維持／調整"
        table.append(f"<tr><th>{name}</th><td>{fmt(r['net_pct'], 3)}%</td>"
                     f"<td>{fmt(r['liq_dist_pct'], 1)}%</td><td>{fmt(r['grid_day'])}</td>"
                     f"<td>{fmt(r['fund_day'])}</td><td>{label}</td></tr>")
        acts = [f"補至 {TARGET:.0%} 強平距離：保證金 {fmt(m['add_margin_for_target'], 0)} U；"
                f"權益不變下部位倍數 {fmt(m['k_at_target'])}；可釋出保證金 {fmt(m['release_at_target'], 0)} U。",
                f"同區間改為等比目標格距 {H:.2%}：約 {m['rows_for_h']} 條價格線。"]
        if tr := m.get("trim"):
            acts.append(f"上緣降至 {fmt(tr['top'], 6)}，{tr['rows']} 條價格線；部位減少 "
                        f"{fmt(tr['position_cut_pct'], 1)}%，強平距離 {fmt(tr['liq_dist_pct'], 1)}%。"
                        f"減倉手續費 {fmt(tr['close_fee'])} U；關倉重開手續費 {fmt(tr['rebuild_fee'])} U；"
                        f"調整後每日資金費 {fmt(tr['fund_day_after'])} U。")
        if half := m.get("half"):
            acts.append(f"加碼空間取一半：{fmt(half['k'])} 倍，增加投資 {fmt(half['add_invest'], 0)} U；"
                        f"每日配對 {fmt(half['grid_day'])} U、資金費 {fmt(half['fund_day'])} U，"
                        f"強平距離 {fmt(half['liq_dist_pct'], 1)}%。")
        kind = "等比" if r["type"] == "geometric" else "等差"
        cards.append(f"<article><h2>{name}</h2><p>{kind} {fmt(r['bottom'], 6)}–{fmt(r['top'], 6)}，"
                     f"{r['row']} 條價格線；格距 {fmt(r['step_pct'], 4)}%，淨格距 {fmt(r['net_pct'], 4)}%。</p>"
                     f"<p>現價 {fmt(r['price'], 6)}，區間位置 {fmt(r['pos_in_range'] * 100, 1)}%；"
                     f"近 60 天帳本觀測價 {fmt(m['low60'], 6)}–{fmt(m['high60'], 6)}。</p>"
                     f"<p>投資 {fmt(r['investment'])} ＋ 保證金 {fmt(r['extra'])} U；部位 {fmt(r['position'], 4)}，"
                     f"名目價值 {fmt(r['notional'])} U；強平價 {fmt(r['liq'], 6)}；模型權益 {fmt(m['e0'])} U。</p>"
                     f"<p>近 30 個擷取日，每日配對 {fmt(r['grid_day'])} U、資金費 {fmt(r['fund_day'])} U；"
                     f"日收益率 {fmt(r['yield_day_pct'], 3)}%。</p><ul>"
                     + "".join(f"<li>{a}</li>" for a in acts) + "</ul></article>")
    title = html.escape(str(data.get("date", data["as_of"][:10]))) + " 倉位參數調整"
    return f"""<!doctype html>
<html lang="zh-Hant"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title><style>
:root{{color-scheme:light;--accent:#216c67}}*{{box-sizing:border-box}}body{{margin:0;background:#f7f5f0;color:#252a2b;
font:16px/1.8 system-ui,sans-serif}}main{{max-width:1100px;margin:auto;padding:40px 24px}}h1,h2{{font-family:Georgia,
'Noto Serif TC',serif;line-height:1.3}}h1{{font-size:2.5rem}}h2{{color:var(--accent)}}article{{background:#fff;
border:1px solid #e5e1d8;padding:24px;margin:24px 0}}.scroll{{overflow-x:auto}}table{{width:100%;border-collapse:collapse;
white-space:nowrap}}th,td{{padding:12px;text-align:left;border-bottom:1px solid #d6d1c6}}thead{{color:var(--accent)}}
small{{color:#555}}li{{margin:8px 0}}@media(max-width:600px){{main{{padding:20px 14px}}h1{{font-size:2rem}}}}
@media print{{article{{break-inside:avoid}}main{{padding:0}}}}</style></head><body><main><h1>{title}</h1>
<small>帳本：{html.escape(str(data['as_of']))} · 即時快照：{html.escape(str(data.get('live_at') or '—'))}</small>
<p>多頭線性網格模型，維持保證金率 {MMR:.1%}，目標強平距離 {TARGET:.0%}。下跌每越過一格買入一格，
以帳本預估強平價反推權益。60 天高點來自帳本觀測價；關倉重開每邊費率 {FEE_CLOSE:.2%}，
格距扣除來回費率 {FEE_ROUND_TRIP:.2%}。資金費正值為收入、負值為支出。</p>
<p>加碼情境按部位比例估算配對與資金費，維持當前權益；強平估算不含未來資金費與滑價。
倍數搜尋範圍為 0.05–5；淨值與強平資料不足時以「—」表示。縮上緣後，價格超過上緣即無部位。</p>
<div class="scroll"><table><thead><tr><th>標的</th><th>淨格距</th><th>強平距離</th><th>每日配對 U</th>
<th>每日資金費 U</th><th>調整</th></tr></thead><tbody>{''.join(table)}</tbody></table></div>
{''.join(cards) or '<p>沒有運行中的倉位。</p>'}</main></body></html>"""


def write(day, db_path=None, live_path=None, out_dir=None) -> Path:
    day = date.fromisoformat(day).isoformat()
    db_path = Path(db_path) if db_path is not None else store.DEFAULT_DB
    live_path = Path(live_path) if live_path is not None else live.LIVE_PATH
    out_dir = Path(out_dir) if out_dir is not None else healthcheck.OUT_DIR
    data = model(collect(db_path, live_path), db_path)
    data["date"] = day
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / f"{day}-倉位參數調整.html"
    dest.write_text(render(data), encoding="utf-8")
    return dest


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Offline position tuning report")
    parser.add_argument("--date", type=lambda value: date.fromisoformat(value).isoformat(), default=date.today().isoformat())
    parser.add_argument("--db", type=Path, default=store.DEFAULT_DB)
    parser.add_argument("--live", type=Path, default=live.LIVE_PATH)
    parser.add_argument("--out", type=Path, default=healthcheck.OUT_DIR)
    parser.add_argument("--json", action="store_true", help="Print calculated positions without writing HTML")
    args = parser.parse_args(argv)
    if args.json:
        print(json.dumps(model(collect(args.db, args.live), args.db), ensure_ascii=False, indent=1))
    else:
        print(write(args.date, args.db, args.live, args.out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
