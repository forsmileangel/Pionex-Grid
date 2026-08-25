"""Daily USD/TWD rate. USDT is treated as 1 USD for conversion."""

from __future__ import annotations

import json
import time
import urllib.request
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "v2-data" / "usd-twd.json"
TTL_SECONDS = 6 * 3600
URL = "https://open.er-api.com/v6/latest/USD"


def usd_twd() -> dict:
    now = time.time()
    cached = _read_cache()
    if cached and now - cached.get("fetched_at", 0) < TTL_SECONDS:
        return cached
    try:
        req = urllib.request.Request(URL, headers={"User-Agent": "pionex-grid-v2", "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=12) as response:
            data = json.loads(response.read().decode("utf-8"))
        rate = data.get("rates", {}).get("TWD")
        if rate is None:
            raise ValueError("TWD missing")
        payload = {
            "usd_twd": float(rate),
            "as_of": data.get("time_last_update_utc") or "",
            "source": "exchangerate-api.com",
            "fetched_at": now,
        }
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        CACHE.write_text(json.dumps(payload), encoding="utf-8")
        return payload
    except Exception:
        return cached or {"usd_twd": None, "as_of": "", "source": "", "fetched_at": 0}


def _read_cache() -> dict | None:
    if not CACHE.exists():
        return None
    try:
        return json.loads(CACHE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def attach_twd(amount: dict | None, rate: float | None) -> dict | None:
    if not amount or amount.get("usdt") is None:
        return amount
    out = dict(amount)
    if not rate:
        return out
    twd = (Decimal(str(amount["usdt"])) * Decimal(str(rate))).quantize(Decimal("1."), rounding=ROUND_HALF_UP)
    out["twd"] = str(int(twd))
    return out


def attach_twd_tree(row: dict, rate: float | None) -> dict:
    keys = ("size", "investment", "grid_profit", "profit_24h", "daily_profit", "total_pnl", "trend_profit", "funding")
    for key in keys:
        if key in row:
            row[key] = attach_twd(row[key], rate)
    return row
