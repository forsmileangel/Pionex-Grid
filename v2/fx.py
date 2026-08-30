"""USDT→TWD to match Pionex: BOT USD cash-buy × USDT/USD."""

from __future__ import annotations

import json
import time
import urllib.request
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "v2-data" / "usdt-twd.json"
TTL_SECONDS = 6 * 3600
BOT_URL = "https://rate.bot.com.tw/xrt/flcsv/0/day"
COINGECKO_URL = "https://api.coingecko.com/api/v3/simple/price?ids=tether&vs_currencies=usd"
COINBASE_URL = "https://api.coinbase.com/v2/prices/USDT-USD/spot"
USD_TWD_URL = "https://open.er-api.com/v6/latest/USD"
UA = {"User-Agent": "pionex-grid-v2", "Accept": "*/*"}
BOT_BUY_LABELS = {"buying", "本行買入"}


def parse_bot_usd_cash_buy(text: str) -> float:
    for raw in text.splitlines():
        line = raw.lstrip("\ufeff").strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 3 and parts[0].upper() == "USD" and parts[1].lower() in BOT_BUY_LABELS:
            return float(parts[2])
    raise ValueError("USD cash buy missing")


def compose_usdt_twd(usd_cash_buy: float, usdt_usd: float | None) -> float:
    peg = Decimal("1") if not usdt_usd else Decimal(str(usdt_usd))
    rate = Decimal(str(usd_cash_buy)) * peg
    return float(rate.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP))


def usdt_twd() -> dict:
    now = time.time()
    cached = _read_cache()
    if cached and now - cached.get("fetched_at", 0) < TTL_SECONDS and cached.get("usdt_twd"):
        return _with_alias(cached)
    try:
        payload = _fetch_live(now)
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        CACHE.write_text(json.dumps(payload), encoding="utf-8")
        return _with_alias(payload)
    except Exception:
        if cached and cached.get("usdt_twd"):
            return _with_alias(cached)
        return _empty()


def usd_twd() -> dict:
    return usdt_twd()


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
    keys = (
        "size", "investment", "grid_profit", "profit_24h", "daily_profit", "total_pnl",
        "trend_profit", "funding", "true_grid_profit", "withdrawn", "reinvest",
    )
    for key in keys:
        if key in row:
            row[key] = attach_twd(row[key], rate)
    return row


def _with_alias(payload: dict) -> dict:
    out = dict(payload)
    rate = out.get("usdt_twd")
    if rate is not None:
        out["usd_twd"] = rate
    return out


def _empty() -> dict:
    return {
        "usdt_twd": None,
        "usd_twd": None,
        "usdt_usd": None,
        "usd_cash_buy": None,
        "as_of": "",
        "source": "",
        "fetched_at": 0,
    }


def _usdt_usd() -> float:
    try:
        gecko = json.loads(_get(COINGECKO_URL))
        return float(gecko["tether"]["usd"])
    except Exception:
        pass
    try:
        coinbase = json.loads(_get(COINBASE_URL))
        return float(coinbase["data"]["amount"])
    except Exception:
        return 1.0


def _get(url: str, timeout: int = 12) -> str:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return response.read().decode("utf-8")


def _fetch_live(now: float) -> dict:
    cash_buy = None
    bot_error = None
    try:
        cash_buy = parse_bot_usd_cash_buy(_get(BOT_URL))
    except Exception as exc:
        bot_error = exc
    usdt_usd = _usdt_usd()
    if cash_buy:
        rate = compose_usdt_twd(cash_buy, usdt_usd)
        return {
            "usdt_twd": rate,
            "usdt_usd": usdt_usd,
            "usd_cash_buy": cash_buy,
            "as_of": time.strftime("%Y-%m-%d %H:%M:%SZ", time.gmtime(now)),
            "source": "bot-cash-usdt",
            "fetched_at": now,
        }
    usd_twd_mid = json.loads(_get(USD_TWD_URL)).get("rates", {}).get("TWD")
    if usd_twd_mid is None:
        raise bot_error or ValueError("TWD missing")
    rate = compose_usdt_twd(float(usd_twd_mid), usdt_usd)
    return {
        "usdt_twd": rate,
        "usdt_usd": usdt_usd,
        "usd_cash_buy": None,
        "as_of": time.strftime("%Y-%m-%d %H:%M:%SZ", time.gmtime(now)),
        "source": "usd-twd-fallback",
        "fetched_at": now,
    }


def _read_cache() -> dict | None:
    if not CACHE.exists():
        return None
    try:
        return json.loads(CACHE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
