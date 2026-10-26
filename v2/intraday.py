"""Intraday (30-minute) grid-profit points and per-bucket efficiency.

The Pionex API only exposes cumulative grid profit, never fills, so these
points can never be re-fetched: they live in their own SQLite file, get a daily
backup, and recording never raises into the capture path.
"""

from __future__ import annotations

import json
import sqlite3
from bisect import bisect_left, bisect_right
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path

from .store import (
    DEFAULT_DB,
    SCALE,
    TAIPEI,
    _classify_event,
    _coin_from_rec,
    _display_direction,
    _leverage_value,
    _mul_fixed,
    _prev_revalued,
    _rec_stock_triple,
    to_fixed,
)

INTRADAY_FILENAME = "intraday.sqlite"
VIEWS_FILENAME = "efficiency-views.json"
BACKUP_KEEP = 7
GAP_SECONDS = 75 * 60
MIN_EFF_HOURS = 0.25
MAX_BUCKETS = 2000
HEAT_DAYS = 180
GRANULARITIES = {"30m": 0.5, "1h": 1.0, "4h": 4.0, "1d": 24.0, "1w": 168.0, "1M": 720.0}
ORDER = ["30m", "1h", "4h", "1d", "1w", "1M"]
FINE = {"30m", "1h", "4h"}
QUIET_EVENTS = {None, "", "continue", "start", "baseline", "new"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS intraday_runs (
  id INTEGER PRIMARY KEY,
  captured_at TEXT NOT NULL UNIQUE,
  source TEXT NOT NULL,
  position_count INTEGER NOT NULL,
  recorded_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS intraday_points (
  run_id INTEGER NOT NULL REFERENCES intraday_runs(id),
  bu_order_id TEXT NOT NULL,
  captured_at TEXT NOT NULL,
  symbol TEXT,
  product TEXT,
  leverage TEXT,
  lev_x REAL,
  trend TEXT,
  created_at TEXT,
  investment_i INTEGER,
  grid_profit_i INTEGER,
  grid_profit_coin_i INTEGER,
  conversion_price_i INTEGER,
  withdrawn_i INTEGER,
  reinvest_i INTEGER,
  reduce_i INTEGER,
  grid_profit_24h_i INTEGER,
  delta_i INTEGER,
  ref_kind TEXT,
  event TEXT,
  PRIMARY KEY (run_id, bu_order_id)
);
CREATE INDEX IF NOT EXISTS intraday_points_by_grid ON intraday_points(bu_order_id, captured_at);
CREATE INDEX IF NOT EXISTS intraday_points_by_time ON intraday_points(captured_at);
"""


class EfficiencyError(ValueError):
    def __init__(self, message: str, suggest: str | None = None):
        super().__init__(message)
        self.suggest = suggest


def default_intraday_db(ledger_db: Path | None = None) -> Path:
    """Lives beside the ledger, so a temporary test ledger gets a temporary intraday file."""
    return Path(ledger_db or DEFAULT_DB).parent / INTRADAY_FILENAME


def views_path(intraday_db: Path | None = None) -> Path:
    return Path(intraday_db or default_intraday_db()).parent / VIEWS_FILENAME


def connect(db_path: Path | None = None) -> sqlite3.Connection:
    path = Path(db_path or default_intraday_db())
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path), timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout = 30000")
    con.execute("PRAGMA journal_mode = WAL")
    con.executescript(SCHEMA)
    return con


def _ledger_con(ledger_db: Path | None) -> sqlite3.Connection | None:
    """Plain read connection: no ledger migrations, and never creates the file."""
    path = Path(ledger_db or DEFAULT_DB)
    if not path.exists():
        return None
    con = sqlite3.connect(str(path), timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout = 30000")
    return con


def _parse_time(text) -> datetime | None:
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(str(text).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TAIPEI)
    return dt.astimezone(TAIPEI)


def _iso(dt: datetime) -> str:
    return dt.astimezone(TAIPEI).replace(microsecond=0).isoformat()


def _usdt(value_i) -> float | None:
    return None if value_i is None else value_i / SCALE


# ---------------------------------------------------------------- recording


def record_snapshot(snapshot: dict, source: str = "live", db_path: Path | None = None,
                    ledger_db: Path | None = None) -> dict:
    """Append one snapshot's running grids. Returns a status dict; never raises."""
    try:
        return _record(snapshot, source, Path(db_path or default_intraday_db(ledger_db)), ledger_db)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _ledger_refs(ledger_db: Path | None, ids: list[str]) -> dict[str, dict]:
    if not ids:
        return {}
    con = _ledger_con(ledger_db)
    if con is None:
        return {}
    try:
        marks = ",".join("?" for _ in ids)
        rows = con.execute(
            f"""SELECT * FROM daily_grid_profit
                WHERE bu_order_id IN ({marks}) AND status IN ('baseline','new','continue','closed')
                ORDER BY capture_date, id""",
            ids,
        )
        return {row["bu_order_id"]: dict(row) for row in rows}
    except sqlite3.OperationalError:
        return {}
    finally:
        con.close()


def _record(snapshot: dict, source: str, db_path: Path, ledger_db: Path | None) -> dict:
    records = list(snapshot.get("Records") or [])
    running = [r for r in records if r.get("ListStatus", "running") == "running"]
    expected = int(snapshot.get("ExpectedCardCount") or len(running))
    if expected != len(running):
        return {"ok": False, "skipped": True, "reason": f"incomplete running set: expected {expected}, got {len(running)}"}
    captured = _parse_time(snapshot.get("CapturedAt"))
    if captured is None:
        return {"ok": False, "skipped": True, "reason": "missing CapturedAt"}
    for rec in running:
        if not rec.get("ApiOrderId") or rec.get("GridProfit") is None or rec.get("Investment") is None:
            return {"ok": False, "skipped": True, "reason": f"grid {rec.get('ApiOrderId')} is missing profit or investment"}
    at = _iso(captured)
    con = connect(db_path)
    try:
        if con.execute("SELECT 1 FROM intraday_runs WHERE captured_at=?", (at,)).fetchone():
            return {"ok": True, "skipped": True, "reason": "already recorded", "captured_at": at}
        latest = con.execute("SELECT MAX(captured_at) AS at FROM intraday_runs").fetchone()["at"]
        if latest and latest > at:
            return {"ok": False, "skipped": True, "reason": f"older than last point {latest}"}
        prevs = {}
        for rec in running:
            oid = str(rec["ApiOrderId"])
            row = con.execute(
                "SELECT * FROM intraday_points WHERE bu_order_id=? ORDER BY captured_at DESC LIMIT 1", (oid,)
            ).fetchone()
            if row is not None:
                prevs[oid] = dict(row)
        ledger = _ledger_refs(ledger_db, [str(r["ApiOrderId"]) for r in running if str(r["ApiOrderId"]) not in prevs])
        con.execute("BEGIN")
        run_id = con.execute(
            "INSERT INTO intraday_runs(captured_at, source, position_count, recorded_at) VALUES (?,?,?,?)",
            (at, source, len(running), _iso(datetime.now(TAIPEI))),
        ).lastrowid
        events = []
        for rec in running:
            oid = str(rec["ApiOrderId"])
            grid_i = to_fixed(rec.get("GridProfit"))
            inv_i = to_fixed(rec.get("Investment"))
            withdrawn_i, reinvest_i, reduce_i = _rec_stock_triple(rec)
            coin_i, px_i, _sym = _coin_from_rec(rec)
            ref, ref_kind = (prevs[oid], "intraday") if oid in prevs else (ledger.get(oid), "ledger" if oid in ledger else None)
            if ref is None:
                event, delta_i = "start", None
            else:
                class_grid = _mul_fixed(coin_i, px_i) if coin_i is not None and px_i else grid_i
                class_prev = _prev_revalued(ref, px_i) if coin_i is not None else ref
                event, delta_i, _life = _classify_event(
                    "continue", class_prev, class_grid, inv_i, withdrawn_i, reinvest_i, reduce_i,
                )
                if "review" in event.split("+"):
                    # Unclear add-vs-reinvest: keep the chain flat rather than guess a big jump.
                    delta_i = 0
            if event not in QUIET_EVENTS:
                events.append({"id": oid, "event": event})
            trend, leverage = _display_direction({
                "trend": rec.get("Trend"),
                "leverage": rec.get("Leverage"),
                "product": rec.get("Product"),
                "raw_json": rec.get("RawJson"),
            })
            con.execute(
                """INSERT INTO intraday_points(run_id, bu_order_id, captured_at, symbol, product, leverage, lev_x, trend,
                     created_at, investment_i, grid_profit_i, grid_profit_coin_i, conversion_price_i, withdrawn_i,
                     reinvest_i, reduce_i, grid_profit_24h_i, delta_i, ref_kind, event)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    run_id, oid, at, rec.get("Symbol"), rec.get("Product"), leverage,
                    _leverage_value({"leverage": rec.get("Leverage")}), trend, rec.get("Created"),
                    inv_i, grid_i, coin_i, px_i, withdrawn_i, reinvest_i, reduce_i,
                    to_fixed(rec.get("GridProfit24h")), delta_i, ref_kind, event,
                ),
            )
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()
    result = {"ok": True, "captured_at": at, "points": len(running), "events": events}
    if source == "daily":
        try:
            backup(db_path)
        except Exception as exc:
            result["backup_error"] = str(exc)
    return result


def backup(db_path: Path | None = None) -> Path | None:
    path = Path(db_path or default_intraday_db())
    if not path.exists():
        return None
    folder = path.parent / "backups"
    folder.mkdir(parents=True, exist_ok=True)
    dest = folder / f"intraday-{datetime.now(TAIPEI).strftime('%Y%m%d')}.sqlite"
    with closing(sqlite3.connect(str(path))) as source:
        with closing(sqlite3.connect(str(dest))) as target:
            source.backup(target)
    for old in sorted(folder.glob("intraday-*.sqlite"), reverse=True)[BACKUP_KEEP:]:
        old.unlink(missing_ok=True)
    return dest


# ---------------------------------------------------------------- buckets


def _floor(dt: datetime, g: str) -> datetime:
    dt = dt.astimezone(TAIPEI)
    if g == "30m":
        return dt.replace(minute=0 if dt.minute < 30 else 30, second=0, microsecond=0)
    if g == "1h":
        return dt.replace(minute=0, second=0, microsecond=0)
    if g == "4h":
        return dt.replace(hour=dt.hour - dt.hour % 4, minute=0, second=0, microsecond=0)
    day = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    if g == "1w":
        return day - timedelta(days=day.weekday())
    if g == "1M":
        return day.replace(day=1)
    return day


def _step(dt: datetime, g: str) -> datetime:
    if g == "1M":
        return dt.replace(year=dt.year + 1, month=1) if dt.month == 12 else dt.replace(month=dt.month + 1)
    return dt + timedelta(hours=GRANULARITIES[g])


def bucket_count(start: datetime, end: datetime, g: str) -> int:
    first = _floor(start, g)
    if g == "1M":
        return (end.year - first.year) * 12 + end.month - first.month + 1
    return int((end - first).total_seconds() // (GRANULARITIES[g] * 3600)) + 1


def bucket_bounds(start: datetime, end: datetime, g: str, limit: int = MAX_BUCKETS) -> list[datetime]:
    """Bucket edges [b0, b1, ..., bn]; every bucket starts on a Taipei clock boundary."""
    if g not in GRANULARITIES:
        raise EfficiencyError(f"unknown granularity {g}")
    if bucket_count(start, end, g) > limit:
        raise EfficiencyError(f"區間超過 {MAX_BUCKETS} 個時段，請改用較粗的顆粒度", suggest_granularity(start, end, g))
    edges = [_floor(start, g)]
    while edges[-1] < end:
        edges.append(_step(edges[-1], g))
    if len(edges) == 1:
        edges.append(_step(edges[0], g))
    return edges


def suggest_granularity(start: datetime, end: datetime, g: str) -> str:
    for cand in ORDER[ORDER.index(g) if g in ORDER else 0:]:
        if bucket_count(start, end, cand) <= MAX_BUCKETS:
            return cand
    return ORDER[-1]


class _Curve:
    """Cumulative grid profit of one grid over time; buckets are differences of it."""

    __slots__ = ("ts", "cs", "inv", "lev", "seg")

    def __init__(self, points: list[tuple]):
        # points: (epoch, cumulative, investment, leverage, kind)
        self.ts = [p[0] for p in points]
        self.cs = [p[1] for p in points]
        inv, lev, last_inv, last_lev = [], [], None, 3.0
        for p in points:
            last_inv = p[2] if p[2] else last_inv
            last_lev = p[3] if p[3] else last_lev
            inv.append(last_inv)
            lev.append(last_lev)
        self.inv, self.lev = inv, lev
        seg = []
        for a, b in zip(points, points[1:]):
            if a[4] == "l" or b[4] == "l":
                seg.append(3)
            elif b[0] - a[0] > GAP_SECONDS:
                seg.append(2)
            else:
                seg.append(0)
        self.seg = seg

    def value_at(self, x: float) -> float:
        ts, cs = self.ts, self.cs
        i = bisect_right(ts, x) - 1
        if i < 0:
            return cs[0]
        if i >= len(ts) - 1:
            return cs[-1]
        return cs[i] + (cs[i + 1] - cs[i]) * (x - ts[i]) / (ts[i + 1] - ts[i])

    def bucket(self, a: float, b: float):
        ts = self.ts
        if len(ts) < 2:
            return None
        lo, hi = max(a, ts[0]), min(b, ts[-1])
        if hi <= lo:
            return None
        k0 = max(0, bisect_right(ts, lo) - 1)
        k1 = min(len(ts) - 2, bisect_left(ts, hi) - 1)
        flag = max(self.seg[k0:k1 + 1]) if k1 >= k0 else 0
        if flag == 0 and hi - lo < b - a - 1:
            flag = 1
        j = max(0, bisect_right(ts, hi) - 1)
        return self.value_at(hi) - self.value_at(lo), (hi - lo) / 3600, flag, self.inv[j], self.lev[j]


def _eff(profit: float, cov_h: float, inv, lev) -> float | None:
    if not inv or inv <= 0 or cov_h < MIN_EFF_HOURS:
        return None
    return profit * 3 / (lev or 3.0) / inv * 100 * 24 / cov_h


# ---------------------------------------------------------------- loading


def _ledger_points(con: sqlite3.Connection | None, ids: set[str] | None) -> dict[str, list[dict]]:
    if con is None:
        return {}
    try:
        rows = con.execute(
            """SELECT d.bu_order_id, d.status, d.event, d.daily_profit_i, d.investment_i,
                      r.captured_at, s.leverage
               FROM daily_grid_profit d
               JOIN capture_runs r ON r.id=d.run_id
               LEFT JOIN grid_snapshots s ON s.run_id=d.run_id AND s.bu_order_id=d.bu_order_id
               WHERE r.status='success' AND d.status IN ('baseline','new','continue','closed')
                 AND d.daily_profit_i IS NOT NULL
               ORDER BY r.captured_at, d.id"""
        )
    except sqlite3.OperationalError:
        return {}
    out: dict[str, list[dict]] = {}
    for row in rows:
        oid = row["bu_order_id"]
        if ids is not None and oid not in ids:
            continue
        t = _parse_time(row["captured_at"])
        if t is None:
            continue
        out.setdefault(oid, []).append({
            "t": t.timestamp(),
            "daily": row["daily_profit_i"] / SCALE,
            "inv": _usdt(row["investment_i"]),
            "lev": _leverage_value({"leverage": row["leverage"]}) if row["leverage"] else None,
            "event": row["event"],
        })
    return out


def _intraday_points(con: sqlite3.Connection, start: datetime, end: datetime, ids: set[str] | None):
    margin = timedelta(hours=36)
    rows = con.execute(
        """SELECT bu_order_id, captured_at, investment_i, lev_x, delta_i, ref_kind, event
           FROM intraday_points WHERE captured_at>=? AND captured_at<=? ORDER BY captured_at""",
        (_iso(start - margin), _iso(end + margin)),
    )
    out: dict[str, list[dict]] = {}
    for row in rows:
        oid = row["bu_order_id"]
        if ids is not None and oid not in ids:
            continue
        out.setdefault(oid, []).append({
            "at": row["captured_at"],
            "t": _parse_time(row["captured_at"]).timestamp(),
            "delta": 0.0 if row["delta_i"] is None else row["delta_i"] / SCALE,
            "ref": row["ref_kind"],
            "inv": _usdt(row["investment_i"]),
            "lev": row["lev_x"],
            "event": row["event"],
        })
    firsts = {row["bu_order_id"]: row["first"] for row in con.execute(
        "SELECT bu_order_id, MIN(captured_at) AS first FROM intraday_points GROUP BY bu_order_id")}
    return out, firsts


def _build_curve(ledger: list[dict], intraday: list[dict], first_at: str | None) -> tuple[_Curve, list[dict]]:
    """Ledger points only before the grid's first intraday point; joined by that point's delta."""
    t0 = _parse_time(first_at).timestamp() if first_at else None
    use_ledger = not intraday or intraday[0]["at"] == first_at
    points, used_ledger = [], []
    c = 0.0
    if use_ledger:
        for row in ledger:
            if t0 is not None and row["t"] >= t0:
                break
            c += row["daily"]
            points.append((row["t"], c, row["inv"], row["lev"], "l"))
            used_ledger.append(row)
    for k, row in enumerate(intraday):
        if k == 0 and points:
            c = points[-1][1] + (row["delta"] if row["ref"] == "ledger" else 0.0)
        elif k == 0:
            c = 0.0
        else:
            c += row["delta"]
        points.append((row["t"], c, row["inv"], row["lev"], "i"))
    return _Curve(points), used_ledger


def _label(meta: dict) -> str:
    symbol = str(meta.get("symbol") or "?").replace("/USDT", "")
    lev = str(meta.get("leverage") or "")
    parts = lev.split()
    lev_x = parts[0] if parts else ""
    side = {"long": "多", "short": "空"}.get(meta.get("trend") or (parts[1] if len(parts) > 1 else ""), "")
    coin = "幣本位" if meta.get("product") == "coin_margined_contract_grid" else ""
    created = str(meta.get("created_at") or "")
    head = " ".join(part for part in (symbol, coin, lev_x + side) if part)
    return f"{head} · {created[5:10]} 建" if len(created) >= 10 else head


def positions_meta(ledger_db: Path | None = None, intraday_db: Path | None = None) -> list[dict]:
    metas: dict[str, dict] = {}
    con = _ledger_con(ledger_db)
    if con is not None:
        try:
            for row in con.execute("SELECT * FROM grid_positions"):
                metas[row["bu_order_id"]] = {
                    "id": row["bu_order_id"], "symbol": row["symbol"], "created_at": row["created_at"],
                    "lifecycle": row["lifecycle"], "product": row["product"],
                }
            for row in con.execute(
                """SELECT s.bu_order_id, s.trend, s.leverage, s.product, s.raw_json FROM grid_snapshots s
                   JOIN (SELECT bu_order_id, MAX(run_id) AS run_id FROM grid_snapshots GROUP BY bu_order_id) m
                     ON m.bu_order_id=s.bu_order_id AND m.run_id=s.run_id"""
            ):
                meta = metas.get(row["bu_order_id"])
                if meta is None:
                    continue
                trend, leverage = _display_direction(dict(row))
                meta.update(trend=trend, leverage=leverage, lev_x=_leverage_value({"leverage": row["leverage"]}))
            for row in con.execute(
                """SELECT d.bu_order_id, MIN(r.captured_at) AS first, MAX(r.captured_at) AS last
                   FROM daily_grid_profit d JOIN capture_runs r ON r.id=d.run_id
                   WHERE r.status='success' GROUP BY d.bu_order_id"""
            ):
                if row["bu_order_id"] in metas:
                    metas[row["bu_order_id"]].update(ledger_since=row["first"], ledger_until=row["last"])
        except sqlite3.OperationalError:
            pass
        finally:
            con.close()
    icon = connect(intraday_db)
    try:
        for row in icon.execute(
            """SELECT p.* , f.first, f.last FROM intraday_points p
               JOIN (SELECT bu_order_id, MIN(captured_at) AS first, MAX(captured_at) AS last
                     FROM intraday_points GROUP BY bu_order_id) f
                 ON f.bu_order_id=p.bu_order_id AND f.last=p.captured_at"""
        ):
            meta = metas.setdefault(row["bu_order_id"], {
                "id": row["bu_order_id"], "symbol": row["symbol"], "created_at": row["created_at"],
                "lifecycle": "active", "product": row["product"],
            })
            meta.update(trend=row["trend"], leverage=row["leverage"], lev_x=row["lev_x"],
                        intraday_since=row["first"], last_seen=row["last"])
    finally:
        icon.close()
    ordered = sorted(metas.values(), key=lambda m: (str(m.get("created_at") or ""), m["id"]))
    for i, meta in enumerate(ordered):
        meta["color"] = i
        meta["label"] = _label(meta)
        meta.setdefault("lev_x", _leverage_value({"leverage": meta.get("leverage")}))
    return ordered


def efficiency_meta(ledger_db: Path | None = None, intraday_db: Path | None = None) -> dict:
    intraday_db = Path(intraday_db or default_intraday_db(ledger_db))
    positions = positions_meta(ledger_db, intraday_db)
    con = connect(intraday_db)
    try:
        span = con.execute("SELECT MIN(captured_at) AS first, MAX(captured_at) AS last FROM intraday_runs").fetchone()
    finally:
        con.close()
    ledger_since = min((p["ledger_since"] for p in positions if p.get("ledger_since")), default=None)
    return {
        "positions": positions,
        "ledger_since": ledger_since,
        "intraday_since": span["first"],
        "last_at": span["last"],
        "granularities": ORDER,
        "max_buckets": MAX_BUCKETS,
        "views": load_views(views_path(intraday_db)),
    }


# ---------------------------------------------------------------- series


def _bucketize(curves: dict[str, _Curve], edges: list[float], fine: bool):
    """Per grid: profit, daily efficiency, investment, covered hours, flag, leverage per bucket.

    Flags: 0 normal, 1 partly covered, 2 spans a gap over 75 minutes, 3 daily-ledger (08:15) data.
    Fine granularities never show ledger data: one point a day cannot be split into hours.
    """
    keys = ("profit", "eff", "inv", "cov", "flag", "lev")
    series = {}
    for oid, curve in curves.items():
        s = {key: [] for key in keys}
        for a, b in zip(edges, edges[1:]):
            got = curve.bucket(a, b)
            if got is None or (fine and got[2] == 3):
                for key in keys:
                    s[key].append(None)
                continue
            p, h, f, i, lev = got
            for key, value in zip(keys, (p, _eff(p, h, i, lev), i, h, f, lev)):
                s[key].append(value)
        series[oid] = s
    return series


def _round(values, digits=6):
    return [None if v is None else round(v, digits) for v in values]


def _basis3x(inv: float, lev) -> float:
    """Capital at 3x: investment x leverage / 3. Unchanged when leverage rises and margin
    moves to extra margin, so weighting by it keeps totals steady across leverage moves."""
    return inv * (lev or 3.0) / 3


def _total(series: dict, n: int, edges: list[float], last_t: float | None):
    """Summed profit; efficiency = profit / 3x capital-hours, so partial grids count fairly."""
    profit, eff, flag = [], [], []
    for k in range(n):
        p_sum, num, den, inv_sum, worst, seen = 0.0, 0.0, 0.0, 0.0, 0, False
        for s in series.values():
            p = s["profit"][k]
            if p is None:
                continue
            seen = True
            p_sum += p
            if s["inv"][k] and s["cov"][k]:
                basis = _basis3x(s["inv"][k], s["lev"][k])
                num += p
                den += basis * s["cov"][k]
                inv_sum += basis
            if s["flag"][k] in (2, 3):
                worst = max(worst, s["flag"][k])
        if not seen:
            profit.append(None); eff.append(None); flag.append(None)
            continue
        if worst == 0 and last_t is not None and edges[k + 1] > last_t + 1:
            worst = 1
        profit.append(p_sum)
        eff.append(num / den * 2400 if den > 0 and den / inv_sum >= MIN_EFF_HOURS else None)
        flag.append(worst)
    return {"profit": _round(profit), "eff": _round(eff), "flag": flag}


def _ranking(series: dict, edges: list[float]) -> list[dict]:
    out = []
    for oid, s in series.items():
        num = den = total = 0.0
        cov_all = cov_real = 0.0
        count = positive = 0
        best = worst = None
        for k, p in enumerate(s["profit"]):
            if p is None:
                continue
            count += 1
            total += p
            positive += 1 if p > 0 else 0
            h = s["cov"][k] or 0.0
            cov_all += h
            if s["flag"][k] in (0, 1):
                cov_real += h
            if s["inv"][k]:
                num += p
                den += _basis3x(s["inv"][k], s["lev"][k]) * h
            e = s["eff"][k]
            if e is not None and h >= (edges[k + 1] - edges[k]) / 3600 / 2:
                if best is None or e > best["eff"]:
                    best = {"t": int(edges[k] * 1000), "eff": round(e, 6), "profit": round(p, 6)}
                if worst is None or e < worst["eff"]:
                    worst = {"t": int(edges[k] * 1000), "eff": round(e, 6), "profit": round(p, 6)}
        if not count:
            continue
        eff = num / den * 2400 if den > 0 else None
        out.append({
            "id": oid,
            "profit": round(total, 6),
            "eff": None if eff is None else round(eff, 6),
            "buckets": count,
            "positive_ratio": round(positive / count, 4),
            "coverage": round(cov_real / cov_all, 4) if cov_all else None,
            "best": best,
            "worst": worst,
        })
    out.sort(key=lambda r: (r["eff"] is None, -(r["eff"] or 0)))
    return out


def _summary(series: dict) -> dict:
    profit = num = den = cov_all = cov_real = 0.0
    for s in series.values():
        for k, p in enumerate(s["profit"]):
            if p is None:
                continue
            profit += p
            h = s["cov"][k] or 0.0
            cov_all += h
            if s["flag"][k] in (0, 1):
                cov_real += h
            if s["inv"][k]:
                num += p
                den += _basis3x(s["inv"][k], s["lev"][k]) * h
    return {
        "profit": round(profit, 6),
        "eff": round(num / den * 2400, 6) if den > 0 else None,
        "coverage": round(cov_real / cov_all, 4) if cov_all else None,
    }


def _heatmap(curves: dict[str, _Curve], start: datetime, end: datetime) -> dict | None:
    heat_start = max(start, end - timedelta(days=HEAT_DAYS))
    hours = bucket_bounds(heat_start, end, "1h", limit=HEAT_DAYS * 24 + 48)
    edges = [h.timestamp() for h in hours]
    series = _bucketize(curves, edges, fine=True)
    hod = [h.hour for h in hours[:-1]]
    rows = {}
    tot_p, tot_num, tot_den, tot_n = [0.0] * 24, [0.0] * 24, [0.0] * 24, [0] * 24
    for oid, s in series.items():
        p_sum, num, den, n = [0.0] * 24, [0.0] * 24, [0.0] * 24, [0] * 24
        for k, p in enumerate(s["profit"]):
            if p is None or s["flag"][k] not in (0, 1):
                continue
            h = hod[k]
            p_sum[h] += p
            n[h] += 1
            if s["inv"][k] and s["cov"][k]:
                num[h] += p
                den[h] += _basis3x(s["inv"][k], s["lev"][k]) * s["cov"][k]
        if not any(n):
            continue
        rows[oid] = {
            "profit": [round(p_sum[h] / n[h], 6) if n[h] else None for h in range(24)],
            "eff": [round(num[h] / den[h] * 2400, 6) if den[h] > 0 else None for h in range(24)],
            "n": n,
        }
        for h in range(24):
            tot_p[h] += p_sum[h]
            tot_num[h] += num[h]
            tot_den[h] += den[h]
            tot_n[h] = max(tot_n[h], n[h])
    if not rows:
        return None
    return {
        "from": _iso(heat_start),
        "rows": rows,
        # Total per hour-of-day: summed grids' average profit, efficiency on 3x capital-hours.
        "total": {
            "profit": [round(tot_p[h] / tot_n[h], 6) if tot_n[h] else None for h in range(24)],
            "eff": [round(tot_num[h] / tot_den[h] * 2400, 6) if tot_den[h] > 0 else None for h in range(24)],
            "n": tot_n,
        },
    }


def efficiency_series(start=None, end=None, granularity: str = "1h", ids=None, extras: bool = True,
                      ledger_db: Path | None = None, intraday_db: Path | None = None,
                      now: datetime | None = None) -> dict:
    g = granularity if granularity in GRANULARITIES else None
    if g is None:
        raise EfficiencyError(f"unknown granularity {granularity}")
    intraday_db = Path(intraday_db or default_intraday_db(ledger_db))
    want = None if not ids else {str(i) for i in ids if str(i)}
    now = (now or datetime.now(TAIPEI)).astimezone(TAIPEI)
    end_dt = (_parse_time(end) if isinstance(end, str) else end) or now
    lcon = _ledger_con(ledger_db)
    try:
        ledger = _ledger_points(lcon, want)
    finally:
        if lcon is not None:
            lcon.close()
    icon = connect(intraday_db)
    try:
        if start in (None, ""):
            first = icon.execute("SELECT MIN(captured_at) AS at FROM intraday_runs").fetchone()["at"]
            firsts_ledger = [rows[0]["t"] for rows in ledger.values() if rows]
            start_dt = datetime.fromtimestamp(min(firsts_ledger), TAIPEI) if firsts_ledger else None
            if first and (start_dt is None or _parse_time(first) < start_dt):
                start_dt = _parse_time(first)
            start_dt = start_dt or end_dt - timedelta(days=7)
        else:
            start_dt = _parse_time(start) if isinstance(start, str) else start
            if start_dt is None:
                raise EfficiencyError("invalid from")
        if end_dt <= start_dt:
            raise EfficiencyError("結束時間要晚於開始時間")
        edges_dt = bucket_bounds(start_dt, end_dt, g)
        load_start = edges_dt[0]
        if extras:
            heat_start = max(start_dt, end_dt - timedelta(days=HEAT_DAYS))
            load_start = min(load_start, _floor(heat_start, "1h"))
        intraday, firsts = _intraday_points(icon, load_start, edges_dt[-1], want)
    finally:
        icon.close()
    edges = [e.timestamp() for e in edges_dt]
    curves, events, last_t = {}, [], None
    for oid in set(ledger) | set(intraday):
        curve, used_ledger = _build_curve(ledger.get(oid, []), intraday.get(oid, []), firsts.get(oid))
        if len(curve.ts) < 1:
            continue
        curves[oid] = curve
        if intraday.get(oid):
            t = intraday[oid][-1]["t"]
            last_t = t if last_t is None else max(last_t, t)
        lo, hi = edges[0], edges[-1]
        for row in used_ledger:
            if row["event"] not in QUIET_EVENTS and lo <= row["t"] <= hi:
                events.append({"t": int(row["t"] * 1000), "id": oid, "event": row["event"], "source": "ledger"})
        for row in intraday.get(oid, []):
            if row["event"] not in QUIET_EVENTS and lo <= row["t"] <= hi:
                events.append({"t": int(row["t"] * 1000), "id": oid, "event": row["event"], "source": "intraday"})
    series = _bucketize(curves, edges, fine=g in FINE)
    n = len(edges) - 1
    payload = {
        "granularity": g,
        "from": _iso(edges_dt[0]),
        "to": _iso(edges_dt[-1]),
        "requested": {"from": _iso(start_dt), "to": _iso(end_dt)},
        "buckets": [int(e * 1000) for e in edges[:-1]],
        "bucket_hours": [round((b - a) / 3600, 4) for a, b in zip(edges, edges[1:])],
        "last_at": None if last_t is None else _iso(datetime.fromtimestamp(last_t, TAIPEI)),
        "series": {
            oid: {"profit": _round(s["profit"]), "eff": _round(s["eff"]), "inv": _round(s["inv"], 4), "flag": s["flag"]}
            for oid, s in series.items() if any(v is not None for v in s["profit"])
        },
        "total": _total(series, n, edges, last_t),
        "summary": _summary(series),
        "events": sorted(events, key=lambda e: e["t"]),
    }
    if extras:
        payload["ranking"] = _ranking(series, edges)
        payload["heatmap"] = _heatmap(curves, start_dt, end_dt)
    return payload


# ---------------------------------------------------------------- status


def intraday_status(db_path: Path | None = None, now: datetime | None = None) -> dict:
    path = Path(db_path or default_intraday_db())
    if not path.exists():
        return {"available": False}
    now = (now or datetime.now(TAIPEI)).astimezone(TAIPEI)
    con = connect(path)
    try:
        span = con.execute("SELECT MIN(captured_at) AS first, MAX(captured_at) AS last, COUNT(*) AS n FROM intraday_runs").fetchone()
        if not span["n"]:
            return {"available": False}
        today = _iso(now.replace(hour=0, minute=0, second=0, microsecond=0))
        today_count = con.execute("SELECT COUNT(*) FROM intraday_runs WHERE captured_at>=?", (today,)).fetchone()[0]
        since = max(_parse_time(span["first"]), now - timedelta(hours=24))
        times = [_parse_time(r[0]).timestamp() for r in con.execute(
            "SELECT captured_at FROM intraday_runs WHERE captured_at>=? ORDER BY captured_at",
            (_iso(since - timedelta(minutes=20)),))]
    finally:
        con.close()
    missing = []
    slot = _floor(since, "30m")
    if slot < since - timedelta(minutes=10):
        slot = _step(slot, "30m")
    while slot <= now - timedelta(minutes=15):
        s = slot.timestamp()
        i = bisect_left(times, s - 600)
        if i >= len(times) or times[i] > s + 900:
            missing.append(slot.strftime("%m-%d %H:%M"))
        slot = _step(slot, "30m")
    return {
        "available": True,
        "first_at": span["first"],
        "last_at": span["last"],
        "runs": span["n"],
        "today_count": today_count,
        "missing_24h": missing[-48:],
    }


# ---------------------------------------------------------------- saved views

VIEW_FIELDS = ("positions", "range", "from", "to", "granularity", "metric", "chart", "shared_y")
MAX_VIEWS = 30
MAX_NAME = 40
MAX_VIEWS_BYTES = 100_000


def _clean_views(doc) -> dict:
    if not isinstance(doc, dict):
        raise ValueError("常用檢視格式錯誤")
    views = doc.get("views") or []
    if not isinstance(views, list):
        raise ValueError("常用檢視格式錯誤")
    if len(views) > MAX_VIEWS:
        raise ValueError(f"常用檢視最多 {MAX_VIEWS} 組")
    out, names = [], set()
    for view in views:
        if not isinstance(view, dict):
            raise ValueError("常用檢視格式錯誤")
        name = str(view.get("name") or "").strip()
        if not name or len(name) > MAX_NAME:
            raise ValueError(f"名稱需要 1～{MAX_NAME} 個字")
        if name in names:
            raise ValueError(f"名稱「{name}」重複")
        names.add(name)
        settings = view.get("settings") or {}
        if not isinstance(settings, dict):
            raise ValueError("常用檢視格式錯誤")
        clean = {}
        for key in VIEW_FIELDS:
            if key not in settings:
                continue
            value = settings[key]
            if key == "positions":
                if value == "all":
                    clean[key] = "all"
                elif isinstance(value, list) and len(value) <= 300 and all(
                        isinstance(x, str) and 0 < len(x) <= 80 for x in value):
                    clean[key] = list(value)
                else:
                    raise ValueError("倉位清單格式錯誤")
            elif value is None or isinstance(value, (bool, int, float)) or (isinstance(value, str) and len(value) <= 64):
                clean[key] = value
            else:
                raise ValueError(f"{key} 格式錯誤")
        out.append({"name": name, "settings": clean})
    default = doc.get("default")
    return {"views": out, "default": default if default in names else None}


def load_views(path: Path | None = None) -> dict:
    try:
        return _clean_views(json.loads(Path(path or views_path()).read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return {"views": [], "default": None}


def save_views(doc, path: Path | None = None) -> dict:
    clean = _clean_views(doc)
    text = json.dumps(clean, ensure_ascii=False, indent=1)
    if len(text.encode("utf-8")) > MAX_VIEWS_BYTES:
        raise ValueError("常用檢視內容太大")
    dest = Path(path or views_path())
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(dest)
    return clean
