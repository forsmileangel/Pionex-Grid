"""Grid health check: local ledger metrics plus public Pionex market data.

Read-only. Never writes the ledger, never calls private endpoints, never PATCHes the Gist.

    python -m v2.healthcheck              # data/YYYY-MM-DD.json + data/market-YYYY-MM-DD.json
    python -m v2.healthcheck --render     # YYYY-MM-DD-派網網格健檢.html from data + notes/YYYY-MM-DD.json
"""

from __future__ import annotations

import argparse
import html
import json
import math
import sqlite3
import statistics
import time
import urllib.request
from contextlib import closing
from datetime import date, datetime, timedelta
from pathlib import Path

from .live import live_payload, load_live_snapshot
from .store import DEFAULT_DB, TAIPEI

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "網格建議"
API = "https://api.pionex.com"
KLINE_DAYS = 200

# Bases ending in X that are crypto, not tokenized US stocks.
CRYPTO_X = {"AVAX", "TRX", "STX", "SNX", "IMX", "ZRX", "GMX", "CFX", "FRAX", "MYX", "POLYX", "INX", "ARX", "ONX"}
COMMODITY = {"XAU", "XAG", "XAUT", "PAXG", "CL", "BRENTOIL", "NATGAS", "USOX", "SLVX", "BNOX", "UNGX", "CPERX", "GSGX", "URAX", "URNMX"}
# Leveraged/inverse ETFs, pre-IPO synthetics and stablecoins are not grid candidates.
EXCLUDE = {"SOXLX", "SOXSX", "TQQQX", "KORUX", "NVDLX", "OPENAI", "ANTHROPIC", "SKHY", "SMSN", "CBRS", "CNNL",
           "UNITREEX", "CXMTX", "FRAX", "USDC", "FDUSD", "BSPX", "INFQX", "CNLX", "SNXXX", "BEX"}
MAJOR_CRYPTO = {"BTC", "ETH", "SOL", "XRP", "BNB"}
SECTOR = {
    **{b: "記憶體／儲存" for b in ("SNDKX", "MUX", "SKHX", "STXX", "WDCX", "DRAMX")},
    **{b: "半導體" for b in ("AMDX", "AVGOX", "TSMX", "NVDAX", "MRVLX", "INTCX", "SMCIX", "QCOMX", "ARMX", "ASMLX",
                            "AMATX", "LRCXX", "KLACX", "ALABX", "CRDOX", "TXNX", "SOXXX", "SMHX", "AXTIX", "SITMX")},
    **{b: "光通訊" for b in ("AAOIX", "LITEX", "COHRX", "GLWX", "NOKX")},
    **{b: "加密相關股" for b in ("CRCLX", "MSTRX", "COINX", "HOODX", "IRENX", "BMNRX", "CIFRX")},
    **{b: "大型科技" for b in ("GOOGLX", "METAX", "AMZNX", "AAPLX", "MSFTX", "ORCLX", "NFLXX", "IBMX", "CSCOX", "CRMX")},
    **{b: "AI 軟體／雲端" for b in ("PLTRX", "NBISX", "CRWVX", "APPX", "NOWX", "DELLX", "HPEX")},
    **{b: "大盤指數" for b in ("QQQX", "SPYX", "EWYX", "EWJX", "VGKX", "XLVX", "XLPX")},
    **{b: "醫療" for b in ("LLYX", "MRNAX", "HIMSX", "UNHX")},
    **{b: "國防／太空" for b in ("ONDSX", "RKLBX", "SPCX", "ASTSX", "LMTX", "RTXX")},
    **{b: "能源／電力" for b in ("OKLOX", "CEGX", "GEVX", "FLNCX")},
    "TSLAX": "電動車",
    **{b: "加密主流" for b in MAJOR_CRYPTO},
    **{b: "商品" for b in COMMODITY},
}


def asset_class(base: str) -> str:
    if base in COMMODITY:
        return "商品"
    if base.endswith("X") and base not in CRYPTO_X:
        return "美股代幣"
    return "加密貨幣"


def sector_of(base: str) -> str:
    if base in SECTOR:
        return SECTOR[base]
    return "美股其他" if asset_class(base) == "美股代幣" else "加密其他"


def _get(path: str) -> dict:
    req = urllib.request.Request(API + path, headers={"User-Agent": "pionex-grid-healthcheck"})
    with urllib.request.urlopen(req, timeout=30) as res:
        data = json.loads(res.read().decode("utf-8"))
    if not data.get("result"):
        raise RuntimeError(f"{path}: {data}")
    return data["data"]


def _num(v):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _lev(text) -> float:
    digits = "".join(ch if ch.isdigit() or ch == "." else " " for ch in str(text or "")).split()
    try:
        return float(digits[0]) if digits else 1.0
    except ValueError:
        return 1.0


def base_of(symbol: str) -> str:
    return str(symbol or "").split("/")[0].upper()


# ---------------------------------------------------------------- market data

def kline_metrics(klines: list[dict], book: list[float] | None = None) -> dict | None:
    rows = sorted(klines, key=lambda k: k["time"])
    if len(rows) < 60:
        return None
    c = [float(k["close"]) for k in rows]
    h = [float(k["high"]) for k in rows]
    lo = [float(k["low"]) for k in rows]
    tr = [max(h[i] - lo[i], abs(h[i] - c[i - 1]), abs(lo[i] - c[i - 1])) for i in range(1, len(rows))]
    last = c[-1]
    n90 = min(90, len(c) - 1)
    path = sum(abs(c[i] - c[i - 1]) for i in range(len(c) - n90, len(c)))
    er90 = abs(c[-1] - c[-1 - n90]) / path if path else 1.0
    range90 = statistics.fmean((h[i] - lo[i]) / c[i] * 100 for i in range(len(c) - n90, len(c)))
    w = c[-180:]
    peak, mdd = w[0], 0.0
    for x in w:
        peak = max(peak, x)
        mdd = max(mdd, (peak - x) / peak)
    hi180, lo180 = max(h[-180:]), min(lo[-180:])
    rets = [math.log(c[i] / c[i - 1]) for i in range(len(c) - n90, len(c))]
    corr = None
    if book and len(book) >= len(rets) and len(rets) > 20:
        try:
            corr = statistics.correlation(rets, book[-len(rets):])
        except statistics.StatisticsError:
            corr = None
    return {
        "days": len(rows),
        "last": last,
        "atr14_pct": statistics.fmean(tr[-14:]) / last * 100,
        "range90_pct": range90,
        "er90": er90,
        "grid_score": range90 * (1 - er90),
        "ret90_pct": (c[-1] / c[-1 - n90] - 1) * 100,
        "ret180_pct": (c[-1] / w[0] - 1) * 100,
        "maxdd180_pct": mdd * 100,
        "hi180": hi180,
        "lo180": lo180,
        "pos180_pct": (last - lo180) / (hi180 - lo180) * 100 if hi180 > lo180 else None,
        "corr_book": corr,
        "_rets": rets,
    }


def suggest_params(m: dict) -> dict:
    """Leverage from daily volatility, capped by the 180-day drawdown.

    Calibrated on the user's own bots: 3x kept ~45-62% to liquidation, 4x ~35-56%, 6x ~27%.
    The target keeps liquidation at least 1.5x the worst 180-day drawdown away (and >= 35%).
    """
    atr = m["atr14_pct"]
    lev = 6 if atr <= 1.8 else 5 if atr <= 2.5 else 4 if atr <= 4 else 3
    dd = m["maxdd180_pct"]
    if dd >= 40:
        lev = min(lev, 3)
    elif dd >= 25:
        lev = min(lev, 4)
    price = m["last"]
    bottom = min(m["lo180"] * 0.9, price * 0.6)
    top = max(m["hi180"] * 1.05, price * 1.35)
    step = min(max(atr / 10, 0.2), 0.5) / 100
    rows = int(round(math.log(top / bottom) / math.log(1 + step)))
    return {"leverage": lev, "bottom": bottom, "top": top, "rows": max(80, min(rows, 1000)), "step_pct": step * 100,
            "liq_target_pct": max(35.0, round(dd * 1.5))}


def market_data(held_bases: dict[str, float], log=print) -> dict:
    symbols = _get("/api/v1/common/symbols?type=PERP")["symbols"]
    tickers = {t["symbol"]: t for t in _get("/api/v1/market/tickers?type=PERP")["tickers"]}
    usdt = [s for s in symbols if s["quoteCurrency"] == "USDT" and s["status"] == "TRADING"]
    universe = []
    for s in usdt:
        base = s["baseCurrency"]
        amount = _num(tickers.get(s["symbol"], {}).get("amount")) or 0
        cls = asset_class(base)
        floor = 1_000_000 if cls == "美股代幣" else 3_000_000
        held = base in held_bases
        if held or (base not in EXCLUDE and amount >= floor):
            universe.append({"symbol": s["symbol"], "base": base, "class": cls, "sector": sector_of(base),
                             "amount24h": amount, "held": held})
    klines = {}
    for i, u in enumerate(universe):
        try:
            klines[u["symbol"]] = _get(f"/api/v1/market/klines?symbol={u['symbol']}&interval=1D&limit={KLINE_DAYS}")["klines"]
        except Exception as exc:  # one bad symbol must not stop the screen
            log(f"klines {u['symbol']}: {exc}")
        time.sleep(0.12)
    # Investment-weighted daily return of what is held: the book a new symbol should not duplicate.
    held_rets = {}
    for u in universe:
        if u["held"] and u["symbol"] in klines:
            m = kline_metrics(klines[u["symbol"]])
            if m:
                held_rets[u["base"]] = m["_rets"]
    book = None
    if held_rets:
        n = min(len(r) for r in held_rets.values())
        total = sum(held_bases[b] for b in held_rets)
        book = [sum(held_bases[b] * held_rets[b][-n:][i] for b in held_rets) / total for i in range(n)]
    out = []
    for u in universe:
        m = kline_metrics(klines.get(u["symbol"], []), book)
        if not m:
            continue
        m.pop("_rets", None)
        out.append({**u, **m, "suggest": suggest_params(m)})
    return {"fetched_at": datetime.now(TAIPEI).isoformat(), "symbols": out}


# ---------------------------------------------------------------- positions

def _ledger(db: Path):
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return closing(con)


def ledger_stats(db: Path, leverage_by_id: dict[str, float]) -> dict:
    with _ledger(db) as con:
        dates = [r[0] for r in con.execute("SELECT capture_date FROM daily_summary ORDER BY capture_date DESC LIMIT 30")]
        eff: dict[str, dict[str, list]] = {}
        # Each day uses that day's own leverage: a later leverage change must not rescale history.
        for r in con.execute(
                """SELECT d.capture_date, d.bu_order_id, d.daily_profit_i, d.investment_i, s.leverage
                   FROM daily_grid_profit d
                   LEFT JOIN grid_snapshots s ON s.run_id = d.run_id AND s.bu_order_id = d.bu_order_id
                   WHERE d.capture_date >= ? AND d.status='continue' AND d.investment_i > 0
                     AND d.daily_profit_i IS NOT NULL""",
                (dates[-1] if dates else "9999",)):
            lev = (_lev(r["leverage"]) if r["leverage"] else None) or leverage_by_id.get(r["bu_order_id"])
            if not lev:
                continue
            value = r["daily_profit_i"] / r["investment_i"] * 100 * 3 / lev
            eff.setdefault(r["bu_order_id"], {})[r["capture_date"]] = value
        d14 = set(dates[:14])
        per_bot = {}
        for oid, by_date in eff.items():
            v30 = list(by_date.values())
            v14 = [v for d, v in by_date.items() if d in d14]
            per_bot[oid] = {"eff14": statistics.fmean(v14) if v14 else None,
                            "eff30": statistics.fmean(v30) if v30 else None, "days": len(v30)}
        monthly = [dict(r) for r in con.execute(
            """SELECT substr(capture_date,1,7) AS month, count(*) AS days, sum(daily_profit_i)/1e8 AS profit,
                      sum(gap_days) AS gaps FROM daily_summary GROUP BY 1 ORDER BY 1""")]
        recent = con.execute(
            "SELECT capture_date, daily_profit_i/1e8 AS p, gap_days FROM daily_summary ORDER BY capture_date DESC LIMIT 30").fetchall()
        wallet = [{"date": r["capture_date"], "usdt": _num(r["wallet_total_usdt"])} for r in con.execute(
            "SELECT capture_date, wallet_total_usdt FROM capture_runs WHERE status='success' ORDER BY capture_date")]
    days_covered = sum(1 + (r["gap_days"] or 0) for r in recent)
    avg30 = sum(r["p"] for r in recent) / days_covered if days_covered else None
    return {"per_bot": per_bot, "monthly": monthly, "avg_daily_30": avg30, "days_30": days_covered,
            "wallet_history": [w for w in wallet if w["usdt"]]}


def positions(db: Path) -> dict:
    board = live_payload()
    snap = load_live_snapshot() or {}
    recs = {str(r.get("ApiOrderId")): r for r in snap.get("Records", [])}
    rows = []
    for b in board.get("rows", []):
        oid = b["bu_order_id"]
        rec = recs.get(oid, {})
        inverse = bool(rec.get("InverseCoinMargined"))
        mark = _num(b.get("mark_price"))
        top, bottom = _num(rec.get("Top")), _num(rec.get("Bottom"))
        if inverse and top and bottom:
            top, bottom = 1 / bottom, 1 / top
        inv = _num((b.get("investment") or {}).get("usdt")) or 0
        margin = _num(rec.get("MarginBalance"))
        if inverse:
            exposure = abs(_num(rec.get("Position")) or 0)
            equity = (margin or 0) * (mark or 0)
        else:
            exposure = abs(_num(rec.get("Notional")) or 0)
            equity = margin or inv
        fee = _num(rec.get("Fee"))
        funding = _num((b.get("funding") or {}).get("usdt"))
        grid = _num((b.get("grid_profit") or {}).get("usdt")) or 0
        if inverse and fee is not None and mark:
            fee = fee * mark if abs(fee) < 50 else fee
        cost = (abs(min(funding or 0, 0)) + abs(fee or 0))
        rows.append({
            "id": oid, "symbol": b["symbol"], "base": base_of(b["symbol"]), "opened_at": b.get("opened_at"),
            "leverage": b.get("leverage"), "lev": _lev(b.get("leverage")), "trend": b.get("trend"),
            "investment": inv, "extra_margin": (_num(rec.get("ExtraMargin")) or 0) * (mark if inverse else 1),
            "capital": inv + (_num(rec.get("ExtraMargin")) or 0) * (mark if inverse else 1),
            "equity": equity, "exposure": exposure,
            "eff_leverage": exposure / equity if equity else None,
            "mark": mark, "bottom": bottom, "top": top, "rows": rec.get("Row"),
            "range_pos_pct": (mark - bottom) / (top - bottom) * 100 if mark and top and bottom and top > bottom else None,
            "to_top_pct": (top / mark - 1) * 100 if mark and top else None,
            "liq": _num(b.get("liq_price")), "liq_distance_pct": _num(b.get("liq_distance_pct")),
            "grid_profit": grid, "profit_24h": _num((b.get("profit_24h") or {}).get("usdt")),
            "funding": funding, "fee": fee, "cost_drag_pct": cost / grid * 100 if grid > 0 else None,
            "trend_profit": _num((b.get("trend_profit") or {}).get("usdt")),
            "total_pnl": _num((b.get("total_pnl") or {}).get("usdt")),
            "grid_apr_pct": _num(b.get("grid_annualized_pct")), "days_open": b.get("days_open"),
            "class": asset_class(base_of(b["symbol"])), "sector": sector_of(base_of(b["symbol"])),
        })
    stats = ledger_stats(db, {r["id"]: r["lev"] for r in rows})
    for r in rows:
        r.update({k: stats["per_bot"].get(r["id"], {}).get(k) for k in ("eff14", "eff30")})
    wallet = board.get("wallet_total") or {}
    fx = board.get("fx") or {}
    return {
        "as_of": board.get("as_of"), "rows": rows,
        "wallet_usdt": _num(wallet.get("usdt")),
        "fx_usdt_twd": _num(fx.get("usdt_twd") if fx.get("usdt_twd") is not None else fx.get("usd_twd")),
        # Capital = investment + extra margin: moving margin between the two (leverage changes) keeps it stable.
        "invested": sum(r["capital"] for r in rows),
        "grid_total": sum(r["grid_profit"] for r in rows),
        "profit_24h": sum(r["profit_24h"] or 0 for r in rows),
        "ledger": stats,
    }


def wallet_categories(db: Path) -> list[dict]:
    with _ledger(db) as con:
        row = con.execute("SELECT wallet_json FROM capture_runs WHERE status='success' AND wallet_json IS NOT NULL "
                          "ORDER BY capture_date DESC LIMIT 1").fetchone()
    try:
        cats = json.loads(row[0]).get("categories", []) if row else []
    except (TypeError, ValueError):
        cats = []
    names = {"trading_bot": "交易機器人", "spot": "現貨餘額", "pionex_card": "派網卡", "meme_guru": "Meme Guru",
             "arbitrage": "套利", "trader_account": "交易員帳戶"}
    return [{"type": c.get("type"), "name": names.get(c.get("type"), c.get("title") or c.get("type")),
             "usdt": _num(c.get("totalInUsdt"))} for c in cats]


def portfolio(pos: dict) -> dict:
    rows = pos["rows"]
    total = pos["invested"] or 1
    def group(key):
        out = {}
        for r in rows:
            g = out.setdefault(r[key], {"name": r[key], "investment": 0.0, "bots": 0})
            g["investment"] += r["capital"]
            g["bots"] += 1
        for g in out.values():
            g["share_pct"] = g["investment"] / total * 100
        return sorted(out.values(), key=lambda g: -g["investment"])
    stress = []
    for drop in (10, 20, 30, 40, 50):
        hit = [r for r in rows if r["liq_distance_pct"] is not None and r["liq_distance_pct"] <= drop]
        stress.append({"drop_pct": drop, "bots": [r["symbol"] for r in hit],
                       "equity_at_risk": sum(r["equity"] or 0 for r in hit)})
    return {"by_underlying": group("base"), "by_sector": group("sector"), "by_class": group("class"), "stress": stress}


# ---------------------------------------------------------------- goal

def project(start: float, invested_ratio: float, daily_rate: float, months: int, deposit: float) -> list[float]:
    """Month-end wallet: grid income compounds on the invested share; deposits go in each month."""
    path, wallet = [], start
    for _ in range(months):
        for _ in range(30):
            wallet += wallet * invested_ratio * daily_rate
        wallet += deposit
        path.append(wallet)
    return path


def needed_deposit(start, ratio, rate, months, target) -> float:
    lo, hi = 0.0, target
    if project(start, ratio, rate, months, 0)[-1] >= target:
        return 0.0
    for _ in range(60):
        mid = (lo + hi) / 2
        if project(start, ratio, rate, months, mid)[-1] >= target:
            hi = mid
        else:
            lo = mid
    return hi


def goal(pos: dict, targets: dict) -> dict:
    wallet, invested = pos["wallet_usdt"], pos["invested"]
    avg = pos["ledger"]["avg_daily_30"] or 0
    ratio = invested / wallet if wallet else 0
    base_rate = avg / invested if invested else 0
    scenarios = {"保守": 0.8, "持平": 1.0, "樂觀": 1.3}
    out = {"wallet": wallet, "invested_ratio": ratio, "avg_daily": avg, "daily_rate_pct": base_rate * 100,
           "targets": [], "paths": {}}
    for name, k in scenarios.items():
        out["paths"][name] = project(wallet, ratio, base_rate * k, 12, 0)
    for t in targets["list"]:
        row = {"label": t["label"], "usdt": t["usdt"], "months": t["months"], "by_scenario": {}}
        for name, k in scenarios.items():
            row["by_scenario"][name] = {
                "profit_only": project(wallet, ratio, base_rate * k, t["months"], 0)[-1],
                "monthly_deposit": needed_deposit(wallet, ratio, base_rate * k, t["months"], t["usdt"]),
            }
        out["targets"].append(row)
    return out


# ---------------------------------------------------------------- main

def build(day: str, db: Path, skip_market: bool = False) -> dict:
    pos = positions(db)
    fx = pos["fx_usdt_twd"] or 31.4
    targets = {"list": [
        {"label": "半年：派網錢包 20 萬美金", "usdt": 200_000, "months": 6},
        {"label": "一年：總錢包台幣 900 萬", "usdt": 9_000_000 / fx, "months": 12},
    ]}
    data = {"date": day, "generated_at": datetime.now(TAIPEI).isoformat(), "positions": pos,
            "wallet_categories": wallet_categories(db), "portfolio": portfolio(pos), "goal": goal(pos, targets)}
    market_path = OUT_DIR / "data" / f"market-{day}.json"
    if not skip_market:
        held = {}
        for r in pos["rows"]:
            held[r["base"]] = held.get(r["base"], 0) + r["capital"]
        market = market_data(held)
        market_path.write_text(json.dumps(market, ensure_ascii=False, indent=1), encoding="utf-8")
    if market_path.exists():
        market = json.loads(market_path.read_text(encoding="utf-8"))
        by_base = {m["base"]: m for m in market["symbols"]}
        for r in pos["rows"]:
            r["market"] = {k: by_base.get(r["base"], {}).get(k) for k in
                           ("atr14_pct", "range90_pct", "er90", "grid_score", "ret90_pct", "maxdd180_pct")}
        for m in market["symbols"]:
            m["suggest"] = suggest_params(m)
        data["candidates"] = sorted([m for m in market["symbols"] if not m["held"]], key=lambda m: -m["grid_score"])
    return data


def main() -> int:
    parser = argparse.ArgumentParser(description="Pionex grid health check (read-only)")
    parser.add_argument("--date", default=date.today().isoformat())
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--skip-market", action="store_true", help="Reuse data/market-DATE.json")
    parser.add_argument("--render", action="store_true", help="Render HTML from data + notes")
    args = parser.parse_args()
    (OUT_DIR / "data").mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "notes").mkdir(parents=True, exist_ok=True)
    data_path = OUT_DIR / "data" / f"{args.date}.json"
    if args.render:
        from .healthcheck_report import render
        data = json.loads(data_path.read_text(encoding="utf-8"))
        notes_path = OUT_DIR / "notes" / f"{args.date}.json"
        notes = json.loads(notes_path.read_text(encoding="utf-8")) if notes_path.exists() else {}
        out = OUT_DIR / f"{args.date}-派網網格健檢.html"
        out.write_text(render(data, notes), encoding="utf-8")
        print(out)
        return 0
    data = build(args.date, Path(args.db), args.skip_market)
    data_path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    print(data_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
