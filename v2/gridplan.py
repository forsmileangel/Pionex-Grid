"""Daily-pairing plans: which symbols, leverage and range can turn capital into a target U/day.

Profit per U comes from replaying recent 15-minute candles through the grid simulator
(calibrated against the user's own bots); liquidation distance comes from the same
position model that matches Pionex's estimate within a few percent.

    python -m v2.gridplan --date 2026-10-08     # needs data/<date>.json and data/market-<date>.json
"""

from __future__ import annotations

import argparse
import json
import math
import pickle
import sqlite3
import statistics
from contextlib import closing
from pathlib import Path

from .gridsim import fetch_candles, levels, liq_price, new_bot_liq, per_volume, simulate
from .healthcheck import OUT_DIR
from .store import DEFAULT_DB

STEP = 0.0022            # the user's own bots run ~0.21%-0.25% per grid
DOWNS = (0.30, 0.40, 0.50, 0.60, 0.70)
UPS = (0.15, 0.25, 0.40)
LEVS = (3, 4, 5, 6)
WINDOWS = {"30": 2880, "60": 5760}   # 15-minute bars
LIQUIDITY_SHARE = 0.015              # one bot's capital at most 1.5% of the symbol's 24h volume

PLANS = {
    "保守": {"liq_min": 50, "dd_mult": 1.0, "lev_max": 4, "per_symbol": 0.09, "per_sector": 0.22, "alts": 0.20,
            "desc": "強平距離至少 50%，且能撐過 180 天最大回撤，槓桿最多 4 倍，單一標的 ≤ 9%、單一產業 ≤ 22%、山寨幣合計 ≤ 20%。"},
    "平衡": {"liq_min": 40, "dd_mult": 0.85, "lev_max": 5, "per_symbol": 0.11, "per_sector": 0.28, "alts": 0.30,
            "desc": "強平距離至少 40%，且 ≥ 180 天最大回撤的 85%，槓桿最多 5 倍，單一標的 ≤ 11%、單一產業 ≤ 28%、山寨幣合計 ≤ 30%。"},
    "積極": {"liq_min": 30, "dd_mult": 0.7, "lev_max": 6, "per_symbol": 0.13, "per_sector": 0.35, "alts": 0.40,
            "desc": "強平距離至少 30%，且 ≥ 180 天最大回撤的 70%，槓桿最多 6 倍，單一標的 ≤ 13%、單一產業 ≤ 35%、山寨幣合計 ≤ 40%。"},
}


def liq_check(snapshot: dict) -> list[dict]:
    """Model liquidation vs Pionex's own estimate for every running U-margined bot."""
    out = []
    for r in snapshot.get("Records", []):
        if r.get("ListStatus") != "running" or r.get("InverseCoinMargined"):
            continue
        data = ((r.get("RawJson") or {}).get("order") or {}).get("buOrderData") or {}
        est = data.get("estimateLiquidationPriceDown")
        if not est:
            continue
        lv = levels(r["Bottom"], r["Top"], r["Row"], r.get("GridType") or "geometric")
        model = liq_price(r["MarginBalance"], r["MarkPrice"], r["Position"], lv, r["PerVolume"])
        if model:
            out.append({"symbol": r["Symbol"], "model": model, "pionex": float(est), "diff_pct": (model / float(est) - 1) * 100})
    return out


def calibration(db: Path, snapshot: dict, candles: dict) -> dict:
    """Realized ledger profit / simulated profit on the user's running U-margined bots."""
    with closing(sqlite3.connect(f"file:{db}?mode=ro", uri=True)) as con:
        dates = [r[0] for r in con.execute("SELECT capture_date FROM daily_summary ORDER BY capture_date DESC LIMIT 30")]
        rows = []
        for r in snapshot.get("Records", []):
            if r.get("ListStatus") != "running" or r.get("InverseCoinMargined"):
                continue
            sym = r["Symbol"].split("/")[0] + "_USDT_PERP"
            if sym not in candles:
                continue
            real = dict(con.execute(
                f"""SELECT capture_date, daily_profit_i/1e8 FROM daily_grid_profit WHERE bu_order_id=? AND status='continue'
                    AND capture_date IN ({','.join('?' * len(dates))})""", (r["ApiOrderId"], *dates)).fetchall())
            lv = levels(r["Bottom"], r["Top"], r["Row"], r.get("GridType") or "geometric")
            sim = simulate(candles[sym][-WINDOWS["30"] - 200:], lv, r["PerVolume"])
            days = [d for d in real if d in sim["by_day"] and d != min(sim["by_day"])]
            if len(days) < 5:
                continue
            rows.append({"symbol": r["Symbol"], "days": len(days), "real": sum(real[d] for d in days),
                         "sim": sum(sim["by_day"][d] for d in days)})
    factor = sum(x["real"] for x in rows) / sum(x["sim"] for x in rows)
    return {"factor": factor, "rows": rows, "median": statistics.median(x["real"] / x["sim"] for x in rows)}


def evaluate(symbol_candles: list[dict], factor: float) -> list[dict]:
    """Every (down, up, leverage) config: predicted %/U/day for 30 and 60 days, liquidation distance."""
    price = symbol_candles[-1]["c"]
    out = []
    for d in DOWNS:
        for u in UPS:
            rows = max(60, round(math.log((1 + u) / (1 - d)) / math.log(1 + STEP)))
            rate = {}
            for name, bars in WINDOWS.items():
                window = symbol_candles[-bars:]
                start = window[0]["o"]
                lv = levels(start * (1 - d), start * (1 + u), rows)
                pv = per_volume(1.0, 1.0, lv)
                sim = simulate(window, lv, pv, start_price=start)
                ndays = max(len(window) / 96, 1)
                rate[name] = sum(sim["by_day"].values()) / ndays * factor * 100   # %/U/day at 1x
                rate[name + "_outside"] = sim["outside_share"]
            for lev in LEVS:
                liq, _ = new_bot_liq(price, price * (1 - d), price * (1 + u), rows, lev)
                r30, r60 = rate["30"] * lev, rate["60"] * lev
                cap = [r / (1 - o) for r, o in ((r30, rate["30_outside"]), (r60, rate["60_outside"])) if o < 0.9]
                out.append({"down": d, "up": u, "rows": rows, "lev": lev,
                            "r30": r30, "r60": r60, "rate": (r30 + r60) / 2,
                            "capacity": sum(cap) / len(cap) if cap else 0.0,
                            "outside30": rate["30_outside"], "outside60": rate["60_outside"],
                            "liq": liq, "liq_dist": (1 - liq / price) * 100 if liq else 100.0,
                            "bottom": price * (1 - d), "top": price * (1 + u)})
    return out


def best_config(configs: list[dict], plan: dict, maxdd: float) -> dict | None:
    need = max(plan["liq_min"], plan["dd_mult"] * maxdd)
    ok = [c for c in configs if c["lev"] <= plan["lev_max"] and c["liq_dist"] >= need
          and (c["outside30"] + c["outside60"]) / 2 < 0.4]
    if not ok:
        return None
    # expected = average of the 30- and 60-day replays, so idle time out of range is priced in
    return max(ok, key=lambda c: c["rate"])


CHIP_SECTORS = {"半導體", "記憶體／儲存", "光通訊"}
MAJORS = {"BTC", "ETH", "SOL", "XRP", "BNB"}
RATE_FLOOR = {"保守": 0.09, "平衡": 0.11, "積極": 0.13}   # %/U/day an existing bot must earn to stay
SHOCKS = [
    {"name": "全部跌 20%", "drop": {"*": 20}},
    {"name": "全部跌 30%", "drop": {"*": 30}},
    {"name": "全部跌 40%", "drop": {"*": 40}},
    {"name": "加密跌 45%、美股跌 20%", "drop": {"加密貨幣": 45, "*": 20}},
    {"name": "晶片股跌 40%、其他跌 15%", "drop": {"晶片／AI 硬體": 40, "*": 15}},
]


def group_of(m: dict) -> str:
    if m["sector"] in CHIP_SECTORS:
        return "晶片／AI 硬體"
    if m["class"] == "加密貨幣":
        return "加密主流" if m["base"] in MAJORS else "加密山寨"
    return m["sector"]


def shock_for(item: dict, shock: dict) -> float:
    d = shock["drop"]
    return d.get(item["group"], d.get(item["class"], d["*"]))


def margin_for_liq(rec: dict, target_dist: float) -> float | None:
    """Extra margin so the Pionex-style liquidation sits target_dist% below the mark (U-margined only)."""
    if not rec or rec.get("InverseCoinMargined"):
        return None
    lv = levels(rec["Bottom"], rec["Top"], rec["Row"], rec.get("GridType") or "geometric")
    mark = rec["MarkPrice"]
    target = mark * (1 - target_dist / 100)

    def liq(extra):
        return liq_price(rec["MarginBalance"] + extra, mark, rec["Position"], lv, rec["PerVolume"]) or 0

    if liq(0) <= target:
        return 0.0
    lo, hi = 0.0, rec["MarginBalance"] * 5
    if liq(hi) > target:
        return None
    for _ in range(40):
        mid = (lo + hi) / 2
        if liq(mid) > target:
            lo = mid
        else:
            hi = mid
    return hi


def existing_actions(name: str, plan: dict, existing: list[dict]) -> list[dict]:
    out = []
    for e in existing:
        need = max(plan["liq_min"], plan["dd_mult"] * (e.get("maxdd180_pct") or 0))
        act = {**e, "need_liq": need, "top_up": 0.0}
        rate = e["rate"]
        if e["coin"]:
            act.update(action="close_later", reason="幣本位倉，等活動贈品後關倉")
        elif rate is None or rate < RATE_FLOOR[name]:
            act.update(action="close",
                       reason=f"近 30 天效率 {rate or 0:.3f}%，低於本方案保留門檻 {RATE_FLOOR[name]:.2f}%")
        elif (e["liq_distance_pct"] or 0) >= need:
            act.update(action="keep", reason=f"強平距離 {e['liq_distance_pct']:.0f}%，已達本方案要求的 {need:.0f}%")
        else:
            x = margin_for_liq(e["rec"], need)
            if x is None or x > e["capital"] * 0.8:
                act.update(action="close", reason=f"要把強平距離拉到 {need:.0f}% 需補的保證金太多")
            else:
                amt = math.ceil(x / 50) * 50
                act.update(action="top_up", top_up=amt,
                           reason=f"補保證金約 {amt:,.0f} U，強平距離從 {e['liq_distance_pct']:.0f}% 拉到 {need:.0f}%")
        out.append(act)
    return out


def build_portfolio(name: str, plan: dict, symbols: list[dict], capital: float, existing: list[dict]) -> dict:
    actions = existing_actions(name, plan, existing)
    kept = [a for a in actions if a["action"] in ("keep", "top_up")]
    by_group, by_base = {}, {}
    used = 0.0
    items = []
    for k in kept:
        cap_k = k["capital"] + k["top_up"]
        by_group[k["group"]] = by_group.get(k["group"], 0.0) + cap_k
        by_base[k["base"]] = by_base.get(k["base"], 0.0) + cap_k
        used += cap_k
        liq_after = k["need_liq"] if k["top_up"] else (k["liq_distance_pct"] or 0)
        items.append({"base": k["base"], "class": k["class"], "group": k["group"], "amount": cap_k,
                      "daily": k["daily"], "liq_dist": liq_after, "existing": True})
    picks = []
    for m in symbols:
        cfg = best_config(m["configs"], plan, m["maxdd180_pct"])
        if cfg:
            picks.append({**m, "group": group_of(m), "cfg": cfg, "rate": cfg["rate"]})
    picks.sort(key=lambda p: -p["rate"])
    alloc, left = [], capital - used
    for p in picks:
        if left < capital * 0.02:
            break
        cap = min(capital * plan["per_symbol"] - by_base.get(p["base"], 0.0),
                  p["amount24h"] * LIQUIDITY_SHARE,
                  capital * plan["per_sector"] - by_group.get(p["group"], 0.0),
                  left)
        if p["group"] == "加密山寨":
            cap = min(cap, capital * plan["alts"] - by_group.get("加密山寨", 0.0))
        amount = math.floor(cap / 500) * 500
        if amount < capital * 0.02:
            continue
        daily = amount * p["rate"] / 100
        row = {k: p[k] for k in ("base", "class", "sector", "group", "rate", "corr_book", "atr14_pct",
                                 "maxdd180_pct", "held", "last")}
        row.update(amount=amount, daily=daily, cfg=p["cfg"])
        alloc.append(row)
        by_group[p["group"]] = by_group.get(p["group"], 0.0) + amount
        by_base[p["base"]] = by_base.get(p["base"], 0.0) + amount
        items.append({"base": p["base"], "class": p["class"], "group": p["group"], "amount": amount,
                      "daily": daily, "liq_dist": p["cfg"]["liq_dist"], "existing": False})
        left -= amount
    total = sum(i["amount"] for i in items)
    daily = sum(i["daily"] for i in items)
    stress = []
    for sh in SHOCKS:
        hit = [i for i in items if i["liq_dist"] <= shock_for(i, sh)]
        stress.append({"name": sh["name"], "symbols": [i["base"] for i in hit],
                       "amount": sum(i["amount"] for i in hit), "daily_lost": sum(i["daily"] for i in hit)})
    return {"name": name, "desc": plan["desc"], "capital": capital, "total": total, "daily": daily,
            "rate_pct": daily / total * 100 if total else 0,
            "existing": [{k: v for k, v in a.items() if k != "rec"} for a in actions], "new": alloc,
            "kept_capital": used, "new_capital": sum(a["amount"] for a in alloc),
            "groups": sorted(({"name": g, "share": v / total * 100, "amount": v} for g, v in by_group.items()),
                             key=lambda g: -g["share"]),
            "stress": stress}


def support_table(symbols: list[dict], targets=(0.15, 0.20, 0.25), down=0.5, up=0.25) -> list[dict]:
    """For a fixed -50%/+25% range: the lowest leverage that reaches each daily target, and its liquidation."""
    rows = []
    for s in symbols:
        cfgs = sorted((c for c in s["configs"] if c["down"] == down and c["up"] == up), key=lambda c: c["lev"])
        per_lev = {c["lev"]: c for c in cfgs}
        if 3 not in per_lev:
            continue
        rate1 = per_lev[3]["capacity"] / 3
        need = {}
        for t in targets:
            lev = next((c["lev"] for c in cfgs if c["capacity"] >= t), None)
            need[f"{t:.2f}"] = {"lev": lev, "liq_dist": per_lev[lev]["liq_dist"] if lev else None}
        rows.append({"base": s["base"], "class": s["class"], "sector": s["sector"], "held": s["held"],
                     "rate_1x": rate1, "liq_by_lev": {str(l): per_lev[l]["liq_dist"] for l in per_lev},
                     "need": need, "maxdd180_pct": s["maxdd180_pct"], "atr14_pct": s["atr14_pct"],
                     "corr_book": s["corr_book"]})
    return sorted(rows, key=lambda r: -r["rate_1x"])


def existing_bots(db: Path, data: dict, snapshot: dict, market_by_base: dict) -> list[dict]:
    """Running bots with realized 30-day U/day on capital (investment + extra margin)."""
    recs = {str(r.get("ApiOrderId")): r for r in snapshot.get("Records", [])}
    with closing(sqlite3.connect(f"file:{db}?mode=ro", uri=True)) as con:
        dates = [r[0] for r in con.execute("SELECT capture_date FROM daily_summary ORDER BY capture_date DESC LIMIT 30")]
        out = []
        for r in data["positions"]["rows"]:
            rows = con.execute(
                f"""SELECT daily_profit_i/1e8 FROM daily_grid_profit WHERE bu_order_id=? AND status='continue'
                    AND daily_profit_i IS NOT NULL AND capture_date IN ({','.join('?' * len(dates))})""",
                (r["id"], *dates)).fetchall()
            daily = statistics.fmean(x[0] for x in rows) if rows else None
            capital = r["investment"] + (r.get("extra_margin") or 0)
            m = market_by_base.get(r["base"], {})
            item = {"id": r["id"], "symbol": r["symbol"], "base": r["base"], "class": r["class"], "sector": r["sector"],
                    "lev": r["lev"], "capital": capital, "daily": daily or 0.0,
                    "rate": daily / capital * 100 if daily is not None and capital else None,
                    "liq_distance_pct": r.get("liq_distance_pct"), "maxdd180_pct": m.get("maxdd180_pct"),
                    "coin": "/" not in r["symbol"], "rec": recs.get(r["id"], {}), "days": len(rows)}
            item["group"] = group_of(item)
            out.append(item)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Daily pairing plans (read-only)")
    parser.add_argument("--date", required=True)
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--capital", type=float, default=200_000)
    parser.add_argument("--refresh", action="store_true", help="Refetch 15-minute candles")
    args = parser.parse_args()
    data = json.loads((OUT_DIR / "data" / f"{args.date}.json").read_text(encoding="utf-8"))
    market = json.loads((OUT_DIR / "data" / f"market-{args.date}.json").read_text(encoding="utf-8"))
    from .live import load_live_snapshot
    snapshot = load_live_snapshot() or {}
    cache_path = OUT_DIR / "data" / "cache" / f"candles-15m-{args.date}.pkl"
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    candles = {} if args.refresh or not cache_path.exists() else pickle.loads(cache_path.read_bytes())
    universe = [m for m in market["symbols"] if m["held"] or m["base"] not in ("BR", "QNT", "PUMPFUN")]
    for m in universe:
        if m["symbol"] not in candles:
            candles[m["symbol"]] = fetch_candles(m["symbol"], "15M", WINDOWS["60"] + 200)
            cache_path.write_bytes(pickle.dumps(candles))
    calib = calibration(Path(args.db), snapshot, candles)
    calib["liq_check"] = liq_check(snapshot)
    symbols = []
    for m in universe:
        c = candles.get(m["symbol"]) or []
        if len(c) < WINDOWS["60"]:
            continue
        symbols.append({**{k: m[k] for k in ("symbol", "base", "class", "sector", "held", "corr_book", "atr14_pct",
                                             "maxdd180_pct", "amount24h", "last")},
                        "configs": evaluate(c, calib["factor"])})
    existing = existing_bots(Path(args.db), data, snapshot, {m["base"]: m for m in market["symbols"]})
    plans = {name: build_portfolio(name, plan, symbols, args.capital, existing) for name, plan in PLANS.items()}
    out = {"date": args.date, "capital": args.capital, "calibration": calib, "plans": plans,
           "support": support_table(symbols),
           "current": {"capital": sum(e["capital"] for e in existing), "daily": sum(e["daily"] for e in existing)},
           "symbols": [{k: v for k, v in s.items() if k != "configs"} | {
               "best": {n: best_config(s["configs"], p, s["maxdd180_pct"]) for n, p in PLANS.items()},
               "at3x_wide": next((c for c in s["configs"] if c["lev"] == 3 and c["down"] == 0.6 and c["up"] == 0.4), None)}
               for s in symbols]}
    path = OUT_DIR / "data" / f"plans-{args.date}.json"
    path.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(path)
    for p in plans.values():
        print(p["name"], round(p["total"]), "U ->", round(p["daily"], 1), "U/day", f"{p['rate_pct']:.3f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
