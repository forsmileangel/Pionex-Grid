"""區間統計: grid profit, funding and net per position between two times you pick.

Grid profit comes from the 30-minute intraday points: each point's delta_i is the change since the
position's previous point, with add/reinvest jumps already taken out. A position opened inside the
range counts all of its grid profit; one closed inside it adds the last stretch up to its settlement.

Funding is settled at fixed times, 00:00, 08:00 and 16:00 Taipei for these products, and each
observation of a position carries its running total and the time of its last settlement
(lastFundingFeeTs). A range (start, end] gets the settlements inside it: the total as of end minus
the total as of start. The total as of a time t is read from an observation whose last settlement
falls between the last scheduled settlement up to t and t itself, so a point taken seconds after a
settlement that Pionex has not posted yet is never mistaken for one that includes it. Observations
are the intraday points (funding is kept there from 1.0.4 on), the 08:15 daily snapshots and the
closing settlement; a time with none of them around says so instead of guessing.

Usage: python -m v2.range_stats --from "2026-11-05 22:30" --to "2026-11-06 08:00" [--exclude SNXXX/USDT] [--json]
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from . import intraday, settlements, store
from .store import SCALE, TAIPEI

SNAP = timedelta(minutes=5)      # an intraday run this close to the asked time stands for it
SETTLE_HOURS = (0, 8, 16)        # Taipei; confirmed from lastFundingFeeTs on 2026-11-05/06
COIN = "coin_margined_contract_grid"


class RangeError(ValueError):
    pass


def parse_time(text) -> datetime:
    when = intraday._parse_time(str(text).strip().replace(" ", "T") if text else None)
    if when is None:
        raise RangeError(f"看不懂的時間：{text}（例：2026-11-05 22:30）")
    return when


def _epoch(text) -> float:
    return intraday._parse_time(text).timestamp()


def _anchor(times: list[str], when: datetime) -> str | None:
    """The intraday run standing for an asked time: the nearest one within SNAP, else the last before it."""
    t = when.timestamp()
    near = [x for x in times if abs(_epoch(x) - t) <= SNAP.total_seconds()]
    if near:
        return min(near, key=lambda x: abs(_epoch(x) - t))
    before = [x for x in times if _epoch(x) <= t]
    return before[-1] if before else None


def _matches(oid: str, symbol: str | None, excluded: set[str]) -> bool:
    return any(x == oid or x == symbol or (len(x) >= 8 and oid.startswith(x)) for x in excluded)


def last_settlement(t: float) -> float:
    """Epoch of the last scheduled funding settlement at or before epoch t."""
    when = datetime.fromtimestamp(t, TAIPEI)
    for day in (when, when - timedelta(days=1)):
        for hour in sorted(SETTLE_HOURS, reverse=True):
            slot = day.replace(hour=hour, minute=0, second=0, microsecond=0)
            if slot.timestamp() <= t:
                return slot.timestamp()
    raise AssertionError("unreachable")


def _funding_as_of(obs: list[tuple], t: float, created: float | None, closed: float | None):
    """(total, price, how) for a position's funding total as of epoch t.
    obs = [(captured, total, last_settlement_ts, price)] sorted by time; how is 'exact' or why it is unknown."""
    if closed is not None and t >= closed:
        final = [o for o in obs if o[0] >= closed]
        return (final[-1][1], final[-1][3], "exact") if final else (None, None, "關倉結算沒有資金費")
    settle = last_settlement(t)
    if created is not None and created > settle:
        return 0, None, "exact"          # opened after the last settlement up to t: nothing paid yet
    fit = [o for o in obs if o[2] is not None and settle <= o[2] <= t]
    if fit:
        return fit[-1][1], fit[-1][3], "exact"
    at = datetime.fromtimestamp(settle, TAIPEI).strftime("%m/%d %H:%M")
    return None, None, f"沒有 {at} 那次結算之後的資金費紀錄"


def _funding_delta(start, end, coin: bool):
    (s_total, s_px, _), (e_total, e_px, _) = start, end
    if coin and s_px and e_px and s_total is not None:
        # Coin-margined totals are coin amounts priced at that moment; difference the coin, price it at the end.
        return int((Decimal(e_total) / e_px - Decimal(s_total) / s_px) * e_px)
    return e_total - s_total


def range_stats(start: datetime, end: datetime, exclude=(), ledger_db: Path | None = None,
                intraday_db: Path | None = None) -> dict:
    if end <= start:
        raise RangeError("結束時間要晚於開始時間")
    excluded = {str(x).strip() for x in exclude if str(x).strip()}
    icon = intraday.connect(intraday_db or intraday.default_intraday_db(ledger_db))
    try:
        times = [r["captured_at"] for r in icon.execute("SELECT captured_at FROM intraday_runs ORDER BY captured_at")]
        if not times:
            raise RangeError("還沒有每 30 分鐘的紀錄")
        first, last = times[0], times[-1]
        g_from, g_to = _anchor(times, start), _anchor(times, end)
        if g_from is None:
            raise RangeError(f"開始時間早於第一筆每 30 分鐘紀錄（{first[:16]}）")
        if g_to is None or g_to <= g_from:
            raise RangeError("這個區間內沒有每 30 分鐘的紀錄")
        points = [dict(r) for r in icon.execute(
            """SELECT bu_order_id, captured_at, symbol, product, leverage, created_at, investment_i, grid_profit_i,
                      grid_profit_coin_i, conversion_price_i, delta_i, event, funding_fee_i, funding_ts
               FROM intraday_points WHERE captured_at > ? AND captured_at <= ? ORDER BY captured_at""", (g_from, g_to))]
        ids = sorted({p["bu_order_id"] for p in points})
        fund_points = {}
        for p in icon.execute(
                f"""SELECT bu_order_id, captured_at, funding_fee_i, funding_ts, conversion_price_i FROM intraday_points
                    WHERE funding_fee_i IS NOT NULL AND bu_order_id IN ({','.join('?' * len(ids))})""", ids) if ids else []:
            fund_points.setdefault(p["bu_order_id"], []).append(
                (_epoch(p["captured_at"]), p["funding_fee_i"], p["funding_ts"], p["conversion_price_i"]))
    finally:
        icon.close()
    t_from, t_to = start.timestamp(), min(end.timestamp(), _epoch(last))

    lcon = store.connect(ledger_db)
    try:
        positions = {r["bu_order_id"]: dict(r) for r in lcon.execute("SELECT * FROM grid_positions")}
        settled = {r["id"]: r for r in settlements.payload(lcon)["rows"]}
        # Positions that closed inside the range may have no intraday point in it at all.
        for oid, pos in positions.items():
            closed = intraday._parse_time(pos.get("closed_at"))
            if closed and t_from < closed.timestamp() <= t_to and oid not in ids:
                ids.append(oid)
        ledger_obs = {}
        if ids:
            for r in lcon.execute(
                    f"""SELECT s.bu_order_id, r.captured_at, s.funding_fee_i, s.conversion_price_i, s.raw_json, s.list_status,
                               s.closed_at FROM grid_snapshots s JOIN capture_runs r ON r.id=s.run_id
                        WHERE r.status='success' AND s.funding_fee_i IS NOT NULL
                          AND s.bu_order_id IN ({','.join('?' * len(ids))})""", ids):
                at = _epoch(r["captured_at"])
                if r["list_status"] == "finished" and r["closed_at"]:
                    # The settlement's total is final at the close, whenever it was recorded.
                    at = intraday._parse_time(str(r["closed_at"]).replace(" ", "T")).timestamp()
                ledger_obs.setdefault(r["bu_order_id"], []).append(
                    (at, r["funding_fee_i"], intraday.funding_ts(r["raw_json"]), r["conversion_price_i"]))
    finally:
        lcon.close()

    by_id: dict[str, dict] = {}
    for p in points:
        row = by_id.setdefault(p["bu_order_id"], {"grid_i": 0, "last": None, "first": p, "notes": []})
        if p["event"] == "start":
            created = intraday._parse_time(str(p["created_at"] or "").replace(" ", "T"))
            if created is not None and created.timestamp() > _epoch(g_from):
                row["grid_i"] += p["grid_profit_i"] or 0           # opened inside the range: all of it
            else:
                row["notes"].append("區間內才開始有每 30 分鐘紀錄，之前的配對沒算")
        else:
            row["grid_i"] += p["delta_i"] or 0
            if "review" in str(p["event"] or "").split("+"):
                row["notes"].append("有一次加倉／複投對不上，那一段先算 0")
        row["last"] = p

    rows, kept, out = [], [], []
    for oid in ids:
        pos = positions.get(oid, {})
        row = by_id.get(oid, {"grid_i": 0, "last": None, "first": None, "notes": []})
        meta = row["last"] or {}
        symbol = meta.get("symbol") or pos.get("symbol")
        created = intraday._parse_time(str(pos.get("created_at") or meta.get("created_at") or "").replace(" ", "T"))
        closed = intraday._parse_time(str(pos.get("closed_at") or "").replace(" ", "T"))
        closed_t = closed.timestamp() if closed else None
        coin = (meta.get("product") or pos.get("product")) == COIN
        grid_i = row["grid_i"]
        s = settled.get(oid)
        if closed_t is not None and t_from < closed_t <= t_to:
            final = store.to_fixed(s["grid_profit_usdt"]) if s and s.get("grid_profit_usdt") is not None else None
            if final is None:
                row["notes"].append("關倉結算還沒補齊，最後一段配對沒算")
            elif row["last"] is None:
                if created and created.timestamp() > t_from:
                    grid_i = final                                  # opened and closed between two points
            elif not coin:
                grid_i += final - (row["last"]["grid_profit_i"] or 0)
            else:
                row["notes"].append("幣本位關倉的最後一段配對沒算")
        obs = sorted(fund_points.get(oid, []) + ledger_obs.get(oid, []))
        c_t, o_t = (created.timestamp() if created else None), closed_t
        a = _funding_as_of(obs, t_from, c_t, o_t)
        b = _funding_as_of(obs, t_to, c_t, o_t)
        funding_i = _funding_delta(a, b, coin) if a[0] is not None and b[0] is not None else None
        why = None if funding_i is not None else (a[2] if a[0] is None else b[2])
        status = ("開＋關" if created and created.timestamp() > t_from and closed_t and closed_t <= t_to else
                  "關倉" if closed_t and closed_t <= t_to else "新開" if created and created.timestamp() > t_from else "運行中")
        item = {"id": oid, "symbol": symbol, "leverage": meta.get("leverage"), "status": status,
                "investment_i": meta.get("investment_i"), "grid_i": grid_i, "funding_i": funding_i,
                "funding_missing": why, "net_i": None if funding_i is None else grid_i + funding_i,
                "excluded": _matches(oid, symbol, excluded), "notes": sorted(set(row["notes"]))}
        rows.append(item)
        (out if item["excluded"] else kept).append(item)
    rows.sort(key=lambda r: (r["excluded"], -(r["grid_i"] or 0)))

    def totals(items):
        grid = sum(r["grid_i"] for r in items)
        known = [r["funding_i"] for r in items if r["funding_i"] is not None]
        funding = sum(known) if known else None
        missing = sum(r["funding_i"] is None for r in items)
        return {"count": len(items), "grid_i": grid, "funding_i": funding, "funding_missing": missing,
                "net_i": None if funding is None else grid + funding,
                "pct_of_grid": None if funding is None or grid <= 0 else round(funding / grid * 100, 1)}

    return {"from": intraday._iso(start), "to": intraday._iso(end), "grid_from": g_from, "grid_to": g_to,
            "first_point": first, "last_point": last, "rows": rows, "totals": totals(kept),
            "excluded_totals": totals(out), "excluded": sorted(excluded)}


def payload(start, end, exclude=(), ledger_db: Path | None = None, intraday_db: Path | None = None) -> dict:
    """range_stats with money views (USDT and TWD) for the API."""
    data = range_stats(parse_time(start), parse_time(end), exclude, ledger_db, intraday_db)
    rate = store.usdt_twd().get("usdt_twd")
    m = lambda value: store.attach_twd(store.money(value), rate)  # noqa: E731
    for item in data["rows"] + [data["totals"], data["excluded_totals"]]:
        for key in ("grid_i", "funding_i", "net_i", "investment_i"):
            if key in item:
                item[key[:-2]] = m(item.pop(key))
    return data


def _fmt(value_i) -> str:
    return "—" if value_i is None else f"{value_i / SCALE:+,.2f}"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="區間統計：指定時段的每倉配對、資金費、淨利")
    parser.add_argument("--from", dest="start", required=True, help='開始，例 "2026-11-05 22:30"（台北時間）')
    parser.add_argument("--to", dest="end", required=True, help='結束，例 "2026-11-06 08:00"')
    parser.add_argument("--exclude", action="append", default=[], help="排除的標的或倉位編號，可重複或用逗號分隔")
    parser.add_argument("--db", type=Path, default=None, help="帳本（預設正式帳本）")
    parser.add_argument("--json", action="store_true", help="輸出 JSON")
    args = parser.parse_args(argv)
    exclude = [x for part in args.exclude for x in part.split(",")]
    try:
        data = range_stats(parse_time(args.start), parse_time(args.end), exclude, args.db)
    except RangeError as exc:
        print(f"錯誤：{exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0
    t, x = data["totals"], data["excluded_totals"]
    print(f"區間 {data['from']} → {data['to']}（配對用 {data['grid_from'][5:16]} → {data['grid_to'][5:16]} 的紀錄）")
    print(f"{'標的':<12}{'槓桿':<9}{'狀態':<6}{'配對':>10}{'資金費':>10}{'淨利':>10}{'吃掉':>8}")
    for r in data["rows"]:
        pct = "" if r["funding_i"] is None or r["grid_i"] <= 0 else f"{r['funding_i'] / r['grid_i'] * 100:.1f}%"
        mark = "（排除）" if r["excluded"] else ""
        print(f"{(r['symbol'] or '?'):<12}{(r['leverage'] or ''):<9}{r['status']:<6}{_fmt(r['grid_i']):>10}"
              f"{_fmt(r['funding_i']):>10}{_fmt(r['net_i']):>10}{pct:>8}{mark}"
              + (f"  ※{r['funding_missing']}" if r["funding_missing"] else "") + "".join(f"  ※{n}" for n in r["notes"]))
    print(f"合計 {t['count']} 倉：配對 {_fmt(t['grid_i'])}  資金費 {_fmt(t['funding_i'])}  淨利 {_fmt(t['net_i'])}"
          + (f"  資金費吃掉 {abs(t['pct_of_grid'])}%" if t["pct_of_grid"] is not None else "")
          + (f"  （{t['funding_missing']} 倉資金費缺資料，未計）" if t["funding_missing"] else ""))
    if x["count"]:
        print(f"排除 {x['count']} 倉：配對 {_fmt(x['grid_i'])}  資金費 {_fmt(x['funding_i'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
