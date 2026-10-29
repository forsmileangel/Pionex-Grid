"""Funding fees per grid and per day, read from the daily snapshots already in the ledger.

Pionex reports funding as a running total per grid (totalFundingFee: paid is negative, received
positive). The daily amount is that total minus the grid's previous daily snapshot. Nothing here
writes the ledger, and funding is never added to daily profit, so Portfolio Tracker is unaffected.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from .averages import averages_view, profit_averages
from .fx import attach_twd, usdt_twd
from .store import _equivalent_by_run, _fx_view, connect, equivalent_i, money, to_fixed

COIN_PRODUCT = "coin_margined_contract_grid"


def _days_between(a: str, b: str) -> int:
    return (date.fromisoformat(b) - date.fromisoformat(a)).days


def _ratio_pct(part: int | None, whole: int | None) -> float | None:
    """part / whole × 100, only when the whole is a positive amount."""
    if part is None or not whole or whole <= 0:
        return None
    return round(float(Decimal(part) / Decimal(whole) * 100), 2)


def funding_by_day(con) -> tuple[dict, dict]:
    """Funding each grid paid or received since its previous daily snapshot.

    Returns (rows, totals):
      rows[(date, id)] = {delta_i, cum_i, span_days, first, equivalent_i}
      totals[date] = running total over every grid seen so far (closed grids keep their last value)
    Coin-margined grids are differenced in coin and priced at that day's conversion price, like
    daily grid profit, so a move in the coin price is not counted as funding.
    """
    status = {(r["capture_date"], r["bu_order_id"]): r["status"]
              for r in con.execute("SELECT capture_date, bu_order_id, status FROM daily_grid_profit")}
    rows: dict = {}
    totals: dict = {}
    last: dict = {}
    for snap in con.execute(
            """SELECT r.capture_date, s.bu_order_id, s.product, s.funding_fee_i, s.conversion_price_i,
                      s.investment_i, s.extra_margin_i, s.mark_price_i
               FROM grid_snapshots s JOIN capture_runs r ON r.id=s.run_id
               WHERE r.status='success' AND s.funding_fee_i IS NOT NULL
               ORDER BY r.capture_date, r.id"""):
        day, oid, cum = snap["capture_date"], snap["bu_order_id"], snap["funding_fee_i"]
        price = snap["conversion_price_i"] if snap["product"] == COIN_PRODUCT else None
        coin = Decimal(cum) / Decimal(price) if price else None
        prev = last.get(oid)
        if prev is None:
            # A grid opened since the last capture starts from 0; the ledger's first day has no
            # earlier snapshot, so that day's funding is unknown.
            delta = cum if status.get((day, oid)) == "new" else None
            span = None
        else:
            if coin is not None and prev["coin"] is not None:
                delta = int((coin - prev["coin"]) * Decimal(price))
            else:
                delta = cum - prev["cum"]
            span = _days_between(prev["day"], day)
        rows[(day, oid)] = {"delta_i": delta, "cum_i": cum, "span_days": span, "first": prev is None,
                            "equivalent_i": equivalent_i(snap)}
        last[oid] = {"day": day, "cum": cum, "coin": coin}
        totals[day] = sum(v["cum"] for v in last.values())
    return rows, totals


def funding_by_date(rows: dict) -> dict:
    """{date: {"funding_i", "span"}}; funding_i is None when no grid had a comparable earlier snapshot."""
    by_date: dict[str, dict] = {}
    for (day, _oid), row in rows.items():
        agg = by_date.setdefault(day, {"funding_i": None, "span": 1})
        if row["delta_i"] is not None:
            agg["funding_i"] = (agg["funding_i"] or 0) + row["delta_i"]
            agg["span"] = max(agg["span"], row["span_days"] or 1)
    return by_date


def funding_payload(db_path: Path | None = None) -> dict:
    con = connect(db_path)
    try:
        rows, totals = funding_by_day(con)
        equivalent = _equivalent_by_run(con)
        summaries = list(con.execute("SELECT * FROM daily_summary ORDER BY capture_date"))
        positions = {r["bu_order_id"]: dict(r) for r in con.execute("SELECT * FROM grid_positions")}
        wallets = {r["id"]: to_fixed(r["wallet_total_usdt"])
                   for r in con.execute("SELECT id, wallet_total_usdt FROM capture_runs")}
        detail = list(con.execute(
            """SELECT capture_date, bu_order_id, status, event, daily_profit_i, investment_i, lifetime_i
               FROM daily_grid_profit ORDER BY capture_date, id"""))
    finally:
        con.close()

    rate = usdt_twd().get("usdt_twd")
    m = lambda value: attach_twd(money(value), rate)  # noqa: E731

    by_date = funding_by_date(rows)

    days = []
    total = None
    for s in summaries:
        day = s["capture_date"]
        # A backfilled day without funding data keeps the previous running total.
        total = totals.get(day, total)
        agg = by_date.get(day, {"funding_i": None, "span": 1})
        f, span = agg["funding_i"], agg["span"]
        equiv = equivalent.get(s["run_id"])
        annual = None
        if f is not None and equiv:
            annual = round(float(Decimal(f) / Decimal(equiv) * 365 / span * 100), 2)
        true_profit = s["true_profit_i"] if s["true_profit_i"] is not None else s["cumulative_i"]
        grid = s["daily_profit_i"]
        days.append({
            "date": day,
            "funding": m(f),
            "grid_profit": m(grid),
            # Net for the day = grid profit + funding; unknown when funding is.
            "net": m(None if f is None or grid is None else grid + f),
            "running_grid": m(s["cumulative_i"]),
            "wallet": m(wallets.get(s["run_id"])),
            "active_count": s["active_count"],
            "pct_of_grid": _ratio_pct(f, s["daily_profit_i"]),
            "equivalent": m(equiv),
            "annual_pct": annual,
            "span_days": span,
            "total": m(total),
            "true_profit": m(true_profit),
            "total_pct_of_grid": _ratio_pct(total, true_profit),
            "unresolved": bool(s["unresolved_count"]),
        })

    rows_by_date: dict[str, list] = {}
    latest_lifetime: dict[str, int] = {}
    for d in detail:
        key = (d["capture_date"], d["bu_order_id"])
        f = rows.get(key) or {}
        pos = positions.get(d["bu_order_id"]) or {}
        rows_by_date.setdefault(d["capture_date"], []).append({
            "bu_order_id": d["bu_order_id"],
            "symbol": pos.get("symbol"),
            "status": d["status"],
            "event": d["event"],
            "funding_day": m(f.get("delta_i")),
            "funding_cum": m(f.get("cum_i")),
            "daily_profit": m(d["daily_profit_i"]),
            "net": m(None if f.get("delta_i") is None or d["daily_profit_i"] is None
                     else d["daily_profit_i"] + f["delta_i"]),
            "investment": m(d["investment_i"]),
            "equivalent": m(f.get("equivalent_i")),
            "pct_of_grid": _ratio_pct(f.get("delta_i"), d["daily_profit_i"]),
        })
        if d["lifetime_i"] is not None:
            latest_lifetime[d["bu_order_id"]] = d["lifetime_i"]

    last_day = summaries[-1]["capture_date"] if summaries else None
    since = (date.fromisoformat(last_day) - timedelta(days=30)).isoformat() if last_day else ""
    per_grid: dict[str, dict] = {}
    for (day, oid), row in rows.items():
        g = per_grid.setdefault(oid, {"cum": None, "d30": 0})
        g["cum"] = row["cum_i"]
        if day > since and row["delta_i"] is not None:
            g["d30"] += row["delta_i"]
    grids = []
    for oid, g in per_grid.items():
        pos = positions.get(oid) or {}
        lifetime = latest_lifetime.get(oid)
        grids.append({
            "bu_order_id": oid,
            "symbol": pos.get("symbol"),
            "lifecycle": pos.get("lifecycle"),
            "created_at": pos.get("created_at"),
            "closed_at": pos.get("closed_at"),
            "funding_cum": m(g["cum"]),
            "funding_30d": m(g["d30"]),
            "lifetime_grid": m(lifetime),
            "pct_of_grid": _ratio_pct(g["cum"], lifetime),
        })
    grids.sort(key=lambda g: g["funding_cum"]["int"] or 0)

    # 當月效率 and 近 30 天日均 including funding, by the calendar's rules (v2/averages.py).
    # A day without funding adds only its grid profit; the next day's funding covers it.
    entries = []
    for i, (item, s) in enumerate(zip(days, summaries)):
        grid, fund = s["daily_profit_i"], item["funding"]["int"]
        entries.append({"date": item["date"], "covered": 1 + (s["gap_days"] or 0), "baseline": i == 0,
                        "pending": item["unresolved"] or grid is None,
                        "grid_profit": grid, "funding": fund, "net": None if grid is None else grid + (fund or 0)})
    sums = profit_averages(entries, ("grid_profit", "funding", "net"))
    averages = averages_view(sums, m)
    pairs = [(averages["months"][key], bucket) for key, bucket in sums["months"].items()]
    pairs.append((averages["recent"], sums["recent"]))
    for view, bucket in pairs:
        view["pct_of_grid"] = (_ratio_pct(bucket["sums"]["funding"], bucket["sums"]["grid_profit"])
                               if bucket["days"] else None)

    return {
        "days": days[::-1],
        "rows_by_date": rows_by_date,
        "grids": grids,
        "averages": averages,
        "fx": _fx_view(usdt_twd()),
    }
