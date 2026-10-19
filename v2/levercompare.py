"""Side-by-side of two ways to size a grid position, for the 槓桿對照（試做） page. Read-only.

Ours: equivalent position = investment + extra margin (the margin you keep in the bot);
      equivalent leverage = investment x Pionex leverage / equivalent position.
Theirs: equivalent position = quoteInvestment in USDT x Pionex leverage (the leveraged size);
        coin-margined quoteInvestment is priced at the current coin price, not the open price.
Both are taken from the latest daily snapshot; nothing is written.
"""
from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from .store import (
    TAIPEI,
    _board_row,
    _extra_usdt_i,
    _is_coin_margined,
    _last_success,
    _leverage_value,
    _num,
    _rows,
    connect,
    equivalent_i,
    from_fixed,
    taipei_now,
)


def _f(value_i) -> float | None:
    text = from_fixed(value_i)
    return None if text is None else float(text)


def _order_data(snap: dict) -> dict:
    raw = snap.get("raw_json") or {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return {}
    return ((raw.get("order") or {}).get("buOrderData") or {}) if isinstance(raw, dict) else {}


def _in_usdt(snap: dict, value) -> float | None:
    """A raw Pionex amount in USDT; coin-margined amounts are in the coin."""
    value = _num(value)
    if value is None or not _is_coin_margined(snap.get("product")):
        return value
    price = _f(snap.get("conversion_price_i"))
    return value * price if price else None


def _their_position(snap: dict) -> float | None:
    """quoteInvestment in USDT; coin-margined uses the current coin price."""
    if _is_coin_margined(snap.get("product")):
        value = _in_usdt(snap, _order_data(snap).get("quoteInvestment"))
        if value is not None:
            return value
    return _f(snap.get("investment_i"))


def _days_open(created: str | None, as_of: datetime) -> float | None:
    if not created:
        return None
    try:
        start = datetime.strptime(created[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=TAIPEI)
    except ValueError:
        return None
    return max((as_of - start).total_seconds() / 86400, 1 / 24)


def _annual(grid: float | None, base: float | None, days: float | None) -> float | None:
    if grid is None or not base or base <= 0 or not days:
        return None
    return round(grid / base * 365 / days * 100, 2)


def _share(part: float | None, whole: float) -> float | None:
    return None if part is None or whole <= 0 else round(part / whole * 100, 2)


def compare_payload(db_path: Path | None = None) -> dict:
    con = connect(db_path)
    try:
        last = _last_success(con)
        if last is None:
            return {"as_of": None, "capture_date": None, "rows": [], "totals": None}
        snaps = _rows(
            con,
            """SELECT s.*, p.lifecycle, p.created_at AS position_created FROM grid_snapshots s
               JOIN grid_positions p ON p.bu_order_id=s.bu_order_id
               WHERE s.run_id=? AND s.list_status='running'
               ORDER BY s.symbol, s.created_at""",
            (last["id"],),
        )
    finally:
        con.close()

    as_of = taipei_now(last["captured_at"])
    rows = []
    for snap in snaps:
        lev = _leverage_value(snap)
        invest = _f(snap.get("investment_i")) or 0.0
        extra = _f(_extra_usdt_i(snap)) or 0.0
        ours = _f(equivalent_i(snap)) or 0.0
        their_pos = _their_position(snap)
        theirs = None if their_pos is None else their_pos * lev
        grid = _f(snap.get("grid_profit_i"))
        days = _days_open(snap.get("position_created") or snap.get("created_at"), as_of)
        board = _board_row(snap, None, None, snap.get("investment_i"))
        dist = _num(board.get("liq_distance_pct"))
        data = _order_data(snap)
        rows.append({
            "bu_order_id": snap.get("bu_order_id"),
            "symbol": snap.get("symbol"),
            "leverage": board.get("leverage"),
            "lev": lev,
            "coin_margined": _is_coin_margined(snap.get("product")),
            "investment": invest,
            "their_position": their_pos,
            "extra_margin": extra,
            "ours": ours,
            "theirs": theirs,
            "our_leverage": round(invest * lev / ours, 2) if ours else None,
            "ratio": round(theirs / ours, 2) if theirs is not None and ours else None,
            "grid_profit": grid,
            "days_open": None if days is None else round(days, 2),
            "our_eff": _annual(grid, ours, days),
            "their_eff": _annual(grid, theirs, days),
            "liq_distance_pct": dist,
            # Their funding column uses fundingFeePayment (payments only); ours uses the net total.
            "funding_net": _in_usdt(snap, data.get("totalFundingFee")),
            "funding_paid": _in_usdt(snap, data.get("fundingFeePayment")),
        })

    sum_ours = sum(r["ours"] for r in rows)
    sum_theirs = sum(r["theirs"] or 0 for r in rows)
    for r in rows:
        r["our_share"] = _share(r["ours"], sum_ours)
        r["their_share"] = _share(r["theirs"], sum_theirs)

    notional = sum(r["investment"] * r["lev"] for r in rows)
    daily_grid = sum(r["grid_profit"] / r["days_open"] for r in rows
                     if r["grid_profit"] is not None and r["days_open"])

    def weighted(key: str) -> float | None:
        pairs = [(r[key], r["liq_distance_pct"]) for r in rows
                 if r[key] and r["liq_distance_pct"] is not None]
        weight = sum(w for w, _ in pairs)
        return round(sum(w * d for w, d in pairs) / weight, 2) if weight else None

    totals = {
        "count": len(rows),
        "investment": sum(r["investment"] for r in rows),
        "their_position": sum(r["their_position"] or 0 for r in rows),
        "extra_margin": sum(r["extra_margin"] for r in rows),
        "ours": sum_ours,
        "theirs": sum_theirs,
        "our_leverage": round(notional / sum_ours, 2) if sum_ours else None,
        "ratio": round(sum_theirs / sum_ours, 2) if sum_ours else None,
        "grid_profit": sum(r["grid_profit"] or 0 for r in rows),
        # Sum of each bot's grid profit per day, annualized on the summed base.
        "our_eff": round(daily_grid * 365 / sum_ours * 100, 2) if sum_ours else None,
        "their_eff": round(daily_grid * 365 / sum_theirs * 100, 2) if sum_theirs else None,
        "liq_ours": weighted("ours"),
        "liq_theirs": weighted("theirs"),
        "funding_net": sum(r["funding_net"] or 0 for r in rows),
        "funding_paid": sum(r["funding_paid"] or 0 for r in rows),
    }
    return {"as_of": last["captured_at"], "capture_date": last["capture_date"],
            "rows": rows, "totals": totals}
