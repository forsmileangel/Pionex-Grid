"""Long futures-grid simulator: daily pairing profit and liquidation price.

Pionex mechanics, verified on the user's bots: each grid trades a fixed base quantity
(perVolume ~= investment x leverage / sum of grid prices); the position is perVolume x the
number of grid levels above the price; a pair closes when a level bought on the way down is
sold one level higher.
"""

from __future__ import annotations

import bisect
import json
import math
import time
import urllib.request
from datetime import datetime, timedelta, timezone

API = "https://api.pionex.com"
TPE = timezone(timedelta(hours=8))
FEE_RATE = 0.0002  # maker fee per side on grid limit orders; calibration absorbs the rest


def levels(bottom: float, top: float, rows: int, kind: str = "geometric") -> list[float]:
    if kind == "arithmetic":
        step = (top - bottom) / rows
        return [bottom + step * k for k in range(rows + 1)]
    ratio = (top / bottom) ** (1 / rows)
    return [bottom * ratio ** k for k in range(rows + 1)]


def per_volume(investment: float, leverage: float, lv: list[float]) -> float:
    return investment * leverage / sum(lv)


def fetch_candles(symbol: str, interval: str = "15M", bars: int = 2880) -> list[dict]:
    out, end = [], None
    while len(out) < bars:
        q = f"/api/v1/market/klines?symbol={symbol}&interval={interval}&limit=500" + (f"&endTime={end}" if end else "")
        req = urllib.request.Request(API + q, headers={"User-Agent": "pionex-grid-healthcheck"})
        with urllib.request.urlopen(req, timeout=30) as res:
            ks = json.loads(res.read().decode("utf-8"))["data"]["klines"]
        if not ks:
            break
        out += ks
        end = min(k["time"] for k in ks) - 1
        time.sleep(0.1)
    rows = sorted({k["time"]: k for k in out}.values(), key=lambda k: k["time"])
    return [{"t": k["time"], "o": float(k["open"]), "h": float(k["high"]), "l": float(k["low"]), "c": float(k["close"])}
            for k in rows]


def ledger_day(ts_ms: int) -> str:
    """Daily ledger interval ending 08:15 Taipei on the returned date."""
    t = datetime.fromtimestamp(ts_ms / 1000, TPE) - timedelta(hours=8, minutes=5)
    return (t.date() + timedelta(days=1)).isoformat()


def simulate(candles: list[dict], lv: list[float], pv: float, start_price: float | None = None,
             fee_rate: float = FEE_RATE) -> dict:
    """Walk each candle open->low->high->close (or ->high->low for red candles)."""
    n = len(lv) - 1
    p0 = start_price if start_price is not None else candles[0]["o"]
    held = [lv[k] >= p0 for k in range(n)]  # level k bought, waiting to sell at k+1
    profit_by_day: dict[str, float] = {}
    pairs = 0
    outside = 0
    prev = p0

    def move(a: float, b: float, day: str):
        nonlocal pairs
        if b < a:
            lo = bisect.bisect_left(lv, b)
            hi = bisect.bisect_left(lv, a)
            for k in range(max(lo, 0), min(hi, n)):
                held[k] = True
        elif b > a:
            lo = bisect.bisect_right(lv, a)
            hi = bisect.bisect_right(lv, b)
            for j in range(max(lo, 1), min(hi, n + 1)):
                k = j - 1
                if held[k]:
                    held[k] = False
                    gain = pv * (lv[k + 1] - lv[k]) - fee_rate * pv * (lv[k] + lv[k + 1])
                    profit_by_day[day] = profit_by_day.get(day, 0.0) + gain
                    pairs += 1

    for c in candles:
        day = ledger_day(c["t"])
        path = [c["o"], c["l"], c["h"], c["c"]] if c["c"] >= c["o"] else [c["o"], c["h"], c["l"], c["c"]]
        move(prev, path[0], day)
        for a, b in zip(path, path[1:]):
            move(a, b, day)
        prev = path[-1]
        if not (lv[0] <= c["c"] <= lv[-1]):
            outside += 1
    return {"by_day": profit_by_day, "pairs": pairs, "outside_share": outside / max(len(candles), 1),
            "end_price": prev, "position": pv * sum(1 for k in range(n) if held[k])}


def liq_price(equity: float, price: float, position: float, lv: list[float], pv: float, mm: float = 0.005) -> float | None:
    """Price where equity falls to maintenance margin as the grid keeps buying on the way down."""
    below = [l for l in reversed(lv) if l < price]
    coins, eq, p = position, equity, price
    for l in below + [0.0]:
        nxt = max(l, 0.0)
        # equity is linear in price until the next level: eq + coins * (x - p)
        floor_eq = eq + coins * (nxt - p)
        if floor_eq <= mm * coins * nxt:
            # solve eq + coins*(x - p) = mm*coins*x
            x = (coins * p - eq) / (coins * (1 - mm)) if coins else None
            return x if x and x > 0 else None
        eq, p = floor_eq, nxt
        if l > 0:
            coins += pv
    return None


def new_bot_liq(price: float, bottom: float, top: float, rows: int, leverage: float, investment: float = 1000.0,
                kind: str = "geometric", mm: float = 0.005) -> tuple[float | None, float]:
    """Liquidation of a freshly opened bot: levels above price bought at price."""
    lv = levels(bottom, top, rows, kind)
    pv = per_volume(investment, leverage, lv)
    position = pv * sum(1 for l in lv[:-1] if l >= price)
    return liq_price(investment, price, position, lv, pv, mm), pv
