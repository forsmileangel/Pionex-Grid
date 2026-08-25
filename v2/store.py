"""SQLite ledger for Pionex contract-grid snapshots."""

from __future__ import annotations

import json
import shutil
import sqlite3
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from zoneinfo import ZoneInfo

SCALE = 100_000_000
SCHEMA_VERSION = 2
TAIPEI = ZoneInfo("Asia/Taipei")
ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "v2-data" / "pionex-grid.sqlite"
BACKUP_DIR = ROOT / "v2-data" / "backups"

SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS schema_meta (
  version INTEGER NOT NULL,
  applied_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS capture_runs (
  id INTEGER PRIMARY KEY,
  captured_at TEXT NOT NULL,
  capture_date TEXT NOT NULL,
  expected_count INTEGER,
  actual_count INTEGER,
  running_count INTEGER,
  finished_count INTEGER,
  status TEXT NOT NULL,
  source TEXT,
  error TEXT,
  gap_days INTEGER,
  previous_success_at TEXT
);
CREATE TABLE IF NOT EXISTS grid_positions (
  bu_order_id TEXT PRIMARY KEY,
  symbol TEXT NOT NULL,
  created_at TEXT NOT NULL,
  closed_at TEXT,
  lifecycle TEXT NOT NULL,
  product TEXT
);
CREATE TABLE IF NOT EXISTS grid_snapshots (
  id INTEGER PRIMARY KEY,
  run_id INTEGER NOT NULL REFERENCES capture_runs(id),
  bu_order_id TEXT NOT NULL,
  list_status TEXT,
  bot_status TEXT,
  symbol TEXT,
  created_at TEXT,
  closed_at TEXT,
  product TEXT,
  leverage TEXT,
  trend TEXT,
  grid_type TEXT,
  top_s TEXT, bottom_s TEXT, row_n INTEGER, per_volume_s TEXT,
  top_i INTEGER, bottom_i INTEGER, per_volume_i INTEGER,
  open_price_s TEXT, position_s TEXT, position_open_price_s TEXT,
  base_amount_s TEXT, quote_amount_s TEXT,
  investment_i INTEGER, grid_profit_i INTEGER, total_profit_i INTEGER,
  grid_profit_24h_i INTEGER, fee_i INTEGER, funding_fee_i INTEGER,
  profit_reinvest_i INTEGER, profit_withdrawn_i INTEGER,
  extra_margin_i INTEGER, margin_balance_i INTEGER, init_margin_i INTEGER,
  risk_status TEXT, margin_status TEXT,
  estimate_liq_up_i INTEGER, estimate_liq_down_i INTEGER, liquidation_price_i INTEGER,
  liquidation_triggered INTEGER,
  matched_grids INTEGER, order_count INTEGER, volume_s TEXT,
  loss_stop_type TEXT, loss_stop TEXT, profit_stop_type TEXT, profit_stop TEXT,
  pause_price_s TEXT, moving_indicator_type TEXT, moving_top_s TEXT, moving_bottom_s TEXT,
  estimated_step_pct_s TEXT, estimated_step_price_s TEXT, estimated_range_pct_s TEXT,
  complete INTEGER NOT NULL,
  raw_json TEXT,
  UNIQUE(run_id, bu_order_id)
);
CREATE TABLE IF NOT EXISTS daily_grid_profit (
  id INTEGER PRIMARY KEY,
  run_id INTEGER NOT NULL REFERENCES capture_runs(id),
  capture_date TEXT NOT NULL,
  bu_order_id TEXT NOT NULL,
  status TEXT NOT NULL,
  prev_grid_profit_i INTEGER,
  grid_profit_i INTEGER,
  daily_profit_i INTEGER,
  cumulative_i INTEGER,
  investment_i INTEGER,
  gap_days INTEGER,
  UNIQUE(capture_date, bu_order_id)
);
CREATE TABLE IF NOT EXISTS daily_summary (
  capture_date TEXT PRIMARY KEY,
  run_id INTEGER NOT NULL REFERENCES capture_runs(id),
  daily_profit_i INTEGER,
  cumulative_i INTEGER,
  investment_i INTEGER,
  active_count INTEGER,
  new_count INTEGER,
  closed_count INTEGER,
  unresolved_count INTEGER,
  gap_days INTEGER
);
"""


class CaptureError(Exception):
    pass


def to_fixed(value) -> int | None:
    if value is None or value == "":
        return None
    scaled = (Decimal(str(value)) * Decimal(SCALE)).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return int(scaled)


def from_fixed(value: int | None) -> str | None:
    if value is None:
        return None
    return str(Decimal(value) / Decimal(SCALE))


def taipei_now(captured_at: str | None = None) -> datetime:
    if captured_at:
        dt = datetime.fromisoformat(captured_at.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=TAIPEI)
        return dt.astimezone(TAIPEI)
    return datetime.now(TAIPEI)


def connect(db_path: Path | None = None) -> sqlite3.Connection:
    path = Path(db_path or DEFAULT_DB)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path))
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA journal_mode = WAL")
    con.executescript(SCHEMA)
    row = con.execute("SELECT version FROM schema_meta ORDER BY version DESC LIMIT 1").fetchone()
    if row is None:
        con.execute("INSERT INTO schema_meta(version, applied_at) VALUES (?, ?)", (SCHEMA_VERSION, datetime.now(TAIPEI).isoformat()))
        con.commit()
    _migrate(con)
    return con


def _migrate(con: sqlite3.Connection) -> None:
    cols = {row[1] for row in con.execute("PRAGMA table_info(grid_snapshots)")}
    for name, decl in (
        ("mark_price_s", "TEXT"),
        ("mark_price_i", "INTEGER"),
        ("notional_i", "INTEGER"),
        ("liq_distance_pct_s", "TEXT"),
    ):
        if name not in cols:
            con.execute(f"ALTER TABLE grid_snapshots ADD COLUMN {name} {decl}")
    latest = con.execute("SELECT version FROM schema_meta ORDER BY version DESC LIMIT 1").fetchone()
    if latest is None or int(latest["version"]) < SCHEMA_VERSION:
        con.execute("INSERT INTO schema_meta(version, applied_at) VALUES (?, ?)", (SCHEMA_VERSION, datetime.now(TAIPEI).isoformat()))
        con.commit()


def _last_success(con: sqlite3.Connection) -> sqlite3.Row | None:
    return con.execute(
        "SELECT * FROM capture_runs WHERE status='success' ORDER BY capture_date DESC, id DESC LIMIT 1"
    ).fetchone()


def _positions(con: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    return {row["bu_order_id"]: row for row in con.execute("SELECT * FROM grid_positions")}


def _latest_profit(con: sqlite3.Connection, bu_order_id: str) -> sqlite3.Row | None:
    return con.execute(
        """SELECT * FROM daily_grid_profit
           WHERE bu_order_id=? AND status IN ('baseline','new','continue','closed')
           ORDER BY capture_date DESC, id DESC LIMIT 1""",
        (bu_order_id,),
    ).fetchone()


def _apply_live_fields(con: sqlite3.Connection, run_id: int, rec: dict) -> None:
    con.execute(
        """UPDATE grid_snapshots
           SET mark_price_s=?, mark_price_i=?, notional_i=?, liq_distance_pct_s=?, liquidation_price_i=COALESCE(?, liquidation_price_i)
           WHERE run_id=? AND bu_order_id=?""",
        (
            None if rec.get("MarkPrice") is None else str(rec.get("MarkPrice")),
            to_fixed(rec.get("MarkPrice")),
            to_fixed(rec.get("Notional")),
            None if rec.get("LiqDistancePct") is None else str(rec.get("LiqDistancePct")),
            to_fixed(rec.get("LiqPrice")),
            run_id,
            rec.get("ApiOrderId"),
        ),
    )


def _snapshot_fields(run_id: int, rec: dict) -> tuple:
    return (
        run_id,
        rec.get("ApiOrderId"),
        rec.get("ListStatus"),
        rec.get("BotStatus"),
        rec.get("Symbol"),
        rec.get("Created"),
        rec.get("Closed") or None,
        rec.get("Product"),
        rec.get("Leverage"),
        rec.get("Trend"),
        rec.get("GridType"),
        None if rec.get("Top") is None else str(rec.get("Top")),
        None if rec.get("Bottom") is None else str(rec.get("Bottom")),
        rec.get("Row"),
        None if rec.get("PerVolume") is None else str(rec.get("PerVolume")),
        to_fixed(rec.get("Top")),
        to_fixed(rec.get("Bottom")),
        to_fixed(rec.get("PerVolume")),
        None if rec.get("OpenPrice") is None else str(rec.get("OpenPrice")),
        None if rec.get("Position") is None else str(rec.get("Position")),
        None if rec.get("PositionOpenPrice") is None else str(rec.get("PositionOpenPrice")),
        None if rec.get("BaseAmount") is None else str(rec.get("BaseAmount")),
        None if rec.get("QuoteAmount") is None else str(rec.get("QuoteAmount")),
        to_fixed(rec.get("Investment")),
        to_fixed(rec.get("GridProfit")),
        to_fixed(rec.get("TotalProfit")),
        to_fixed(rec.get("GridProfit24h")),
        to_fixed(rec.get("Fee")),
        to_fixed(rec.get("FundingFee")),
        to_fixed(rec.get("ProfitReinvest")),
        to_fixed(rec.get("ProfitWithdrawn")),
        to_fixed(rec.get("ExtraMargin")),
        to_fixed(rec.get("MarginBalance")),
        to_fixed(rec.get("InitMargin")),
        rec.get("RiskStatus"),
        rec.get("MarginStatus"),
        to_fixed(rec.get("EstimateLiqUp")),
        to_fixed(rec.get("EstimateLiqDown")),
        to_fixed(rec.get("LiquidationPrice")),
        1 if rec.get("LiquidationTriggered") else 0,
        rec.get("MatchedGrids"),
        rec.get("OrderCount"),
        None if rec.get("Volume") is None else str(rec.get("Volume")),
        rec.get("LossStopType"),
        rec.get("LossStop"),
        rec.get("ProfitStopType"),
        rec.get("ProfitStop"),
        None if rec.get("PausePrice") is None else str(rec.get("PausePrice")),
        rec.get("MovingIndicatorType"),
        None if rec.get("MovingTop") is None else str(rec.get("MovingTop")),
        None if rec.get("MovingBottom") is None else str(rec.get("MovingBottom")),
        None if rec.get("EstimatedStepPct") is None else str(rec.get("EstimatedStepPct")),
        None if rec.get("EstimatedStepPrice") is None else str(rec.get("EstimatedStepPrice")),
        None if rec.get("EstimatedRangePct") is None else str(rec.get("EstimatedRangePct")),
        1 if rec.get("Complete", True) else 0,
        json.dumps(rec.get("RawJson") or {}, ensure_ascii=False),
    )


def ingest(snapshot: dict, db_path: Path | None = None, replace_date: bool = False) -> dict:
    records = list(snapshot.get("Records") or [])
    captured = taipei_now(snapshot.get("CapturedAt"))
    capture_date = captured.date().isoformat()
    running = [r for r in records if r.get("ListStatus", "running") == "running"]
    finished = [r for r in records if r.get("ListStatus") == "finished"]
    expected = int(snapshot.get("ExpectedCardCount") or len(running))
    if expected != len(running):
        raise CaptureError(f"Incomplete running set: expected {expected}, got {len(running)}")
    ids = [str(r.get("ApiOrderId") or "") for r in records]
    if any(not i for i in ids) or len(set(ids)) != len(ids):
        raise CaptureError("Missing or duplicate API order id")
    for rec in running:
        if rec.get("GridProfit") is None or rec.get("Investment") is None:
            raise CaptureError(f"Running grid {rec.get('ApiOrderId')} is missing profit or investment")

    con = connect(db_path)
    try:
        existing = con.execute("SELECT capture_date FROM daily_summary WHERE capture_date=?", (capture_date,)).fetchone()
        if existing and not replace_date:
            raise CaptureError(f"A record for {capture_date} already exists")
        last = _last_success(con)
        if last and last["capture_date"] == capture_date and not replace_date:
            raise CaptureError(f"A record for {capture_date} already exists")
        previous_success_at = last["captured_at"] if last else None
        gap_days = 0
        if last:
            prev_date = datetime.fromisoformat(last["capture_date"]).date()
            gap_days = max(0, (captured.date() - prev_date).days - 1)
        by_id = _positions(con)
        finished_by_id = {str(r["ApiOrderId"]): r for r in finished}

        con.execute("BEGIN")
        if replace_date:
            old_runs = [row["id"] for row in con.execute("SELECT id FROM capture_runs WHERE capture_date=?", (capture_date,))]
            con.execute("DELETE FROM daily_grid_profit WHERE capture_date=?", (capture_date,))
            con.execute("DELETE FROM daily_summary WHERE capture_date=?", (capture_date,))
            con.execute("DELETE FROM grid_snapshots WHERE run_id IN (SELECT id FROM capture_runs WHERE capture_date=?)", (capture_date,))
            con.execute("DELETE FROM capture_runs WHERE capture_date=?", (capture_date,))
            last = _last_success(con)
            previous_success_at = last["captured_at"] if last else None
            gap_days = 0
            if last:
                prev_date = datetime.fromisoformat(last["capture_date"]).date()
                gap_days = max(0, (captured.date() - prev_date).days)
            by_id = _positions(con)

        cur = con.execute(
            """INSERT INTO capture_runs(captured_at, capture_date, expected_count, actual_count, running_count,
                 finished_count, status, source, error, gap_days, previous_success_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                captured.isoformat(),
                capture_date,
                expected,
                len(running),
                len(running),
                len(finished),
                "pending",
                snapshot.get("Source"),
                None,
                gap_days,
                previous_success_at,
            ),
        )
        run_id = cur.lastrowid
        is_baseline = last is None
        profit_rows = []
        running_ids = set()
        daily_total = 0
        investment_total = 0
        new_count = 0
        closed_count = 0
        unresolved = 0

        for rec in running:
            oid = str(rec["ApiOrderId"])
            running_ids.add(oid)
            pos = by_id.get(oid)
            con.execute(
                """INSERT INTO grid_positions(bu_order_id, symbol, created_at, closed_at, lifecycle, product)
                   VALUES (?,?,?,?,?,?)
                   ON CONFLICT(bu_order_id) DO UPDATE SET
                     symbol=excluded.symbol, lifecycle='active', closed_at=NULL, product=excluded.product""",
                (oid, rec.get("Symbol"), rec.get("Created"), None, "active", rec.get("Product")),
            )
            con.execute(
                """INSERT INTO grid_snapshots(run_id, bu_order_id, list_status, bot_status, symbol, created_at, closed_at, product,
                    leverage, trend, grid_type, top_s, bottom_s, row_n, per_volume_s, top_i, bottom_i, per_volume_i,
                    open_price_s, position_s, position_open_price_s, base_amount_s, quote_amount_s,
                    investment_i, grid_profit_i, total_profit_i, grid_profit_24h_i, fee_i, funding_fee_i,
                    profit_reinvest_i, profit_withdrawn_i, extra_margin_i, margin_balance_i, init_margin_i,
                    risk_status, margin_status, estimate_liq_up_i, estimate_liq_down_i, liquidation_price_i,
                    liquidation_triggered, matched_grids, order_count, volume_s, loss_stop_type, loss_stop,
                    profit_stop_type, profit_stop, pause_price_s, moving_indicator_type, moving_top_s, moving_bottom_s,
                    estimated_step_pct_s, estimated_step_price_s, estimated_range_pct_s, complete, raw_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                _snapshot_fields(run_id, rec),
            )
            _apply_live_fields(con, run_id, rec)
            grid_profit_i = to_fixed(rec.get("GridProfit"))
            investment_i = to_fixed(rec.get("Investment"))
            prev_row = _latest_profit(con, oid)
            if is_baseline or pos is None:
                status = "baseline" if is_baseline else "new"
                daily_i = 0
                prev_i = None
                if status == "new":
                    new_count += 1
                cum_i = 0 if prev_row is None else (prev_row["cumulative_i"] or 0)
            elif prev_row is None or prev_row["status"] in ("closed", "closed_unresolved"):
                status = "new"
                daily_i = 0
                prev_i = None
                new_count += 1
                cum_i = 0
            else:
                status = "continue"
                prev_i = prev_row["grid_profit_i"]
                if prev_i is None or grid_profit_i is None:
                    raise CaptureError(f"Cannot compute daily profit for {oid}")
                daily_i = grid_profit_i - prev_i
                cum_i = (prev_row["cumulative_i"] or 0) + daily_i
            profit_rows.append((run_id, capture_date, oid, status, prev_i, grid_profit_i, daily_i, cum_i, investment_i, gap_days))
            daily_total += daily_i
            investment_total += investment_i or 0

        for oid, pos in by_id.items():
            if pos["lifecycle"] != "active" or oid in running_ids:
                continue
            rec = finished_by_id.get(oid)
            prev_row = _latest_profit(con, oid)
            prev_i = prev_row["grid_profit_i"] if prev_row else None
            if rec is not None and rec.get("Complete") and rec.get("GridProfit") is not None and prev_i is not None:
                final_i = to_fixed(rec.get("GridProfit"))
                daily_i = final_i - prev_i
                cum_i = (prev_row["cumulative_i"] or 0) + daily_i
                status = "closed"
                closed_count += 1
                daily_total += daily_i
                con.execute(
                    """INSERT INTO grid_snapshots(run_id, bu_order_id, list_status, bot_status, symbol, created_at, closed_at, product,
                        leverage, trend, grid_type, top_s, bottom_s, row_n, per_volume_s, top_i, bottom_i, per_volume_i,
                        open_price_s, position_s, position_open_price_s, base_amount_s, quote_amount_s,
                        investment_i, grid_profit_i, total_profit_i, grid_profit_24h_i, fee_i, funding_fee_i,
                        profit_reinvest_i, profit_withdrawn_i, extra_margin_i, margin_balance_i, init_margin_i,
                        risk_status, margin_status, estimate_liq_up_i, estimate_liq_down_i, liquidation_price_i,
                        liquidation_triggered, matched_grids, order_count, volume_s, loss_stop_type, loss_stop,
                        profit_stop_type, profit_stop, pause_price_s, moving_indicator_type, moving_top_s, moving_bottom_s,
                        estimated_step_pct_s, estimated_step_price_s, estimated_range_pct_s, complete, raw_json)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    _snapshot_fields(run_id, rec),
                )
                _apply_live_fields(con, run_id, rec)
                con.execute(
                    "UPDATE grid_positions SET lifecycle='closed', closed_at=? WHERE bu_order_id=?",
                    (rec.get("Closed") or captured.strftime("%Y-%m-%d %H:%M:%S"), oid),
                )
            else:
                status = "closed_unresolved"
                daily_i = None
                final_i = None
                cum_i = prev_row["cumulative_i"] if prev_row else None
                unresolved += 1
                con.execute("UPDATE grid_positions SET lifecycle='closed_unresolved' WHERE bu_order_id=?", (oid,))
            profit_rows.append(
                (run_id, capture_date, oid, status, prev_i, final_i if status == "closed" else None, daily_i, cum_i, pos["lifecycle"] and None, gap_days)
            )

        for row in profit_rows:
            con.execute(
                """INSERT INTO daily_grid_profit(run_id, capture_date, bu_order_id, status, prev_grid_profit_i,
                     grid_profit_i, daily_profit_i, cumulative_i, investment_i, gap_days)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                row,
            )

        prev_summary = con.execute("SELECT cumulative_i FROM daily_summary ORDER BY capture_date DESC LIMIT 1").fetchone()
        prev_cum = prev_summary["cumulative_i"] if prev_summary else 0
        new_cum = (prev_cum or 0) + daily_total
        con.execute(
            """INSERT INTO daily_summary(capture_date, run_id, daily_profit_i, cumulative_i, investment_i,
                 active_count, new_count, closed_count, unresolved_count, gap_days)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (capture_date, run_id, daily_total, new_cum, investment_total, len(running), new_count, closed_count, unresolved, gap_days),
        )
        con.execute("UPDATE capture_runs SET status='success' WHERE id=?", (run_id,))
        con.commit()
        _backup(Path(db_path or DEFAULT_DB))
        return {
            "run_id": run_id,
            "capture_date": capture_date,
            "status": "success",
            "active": len(running),
            "new": new_count,
            "closed": closed_count,
            "unresolved": unresolved,
            "daily_profit": from_fixed(daily_total),
            "gap_days": gap_days,
        }
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def _backup(db_path: Path) -> None:
    if not db_path.exists():
        return
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(TAIPEI).strftime("%Y%m%d-%H%M%S")
    shutil.copy2(db_path, BACKUP_DIR / f"pionex-grid-{stamp}.sqlite")
    backups = sorted(BACKUP_DIR.glob("pionex-grid-*.sqlite"), reverse=True)
    for old in backups[30:]:
        old.unlink(missing_ok=True)


def restore_latest_backup(db_path: Path | None = None) -> Path:
    backups = sorted(BACKUP_DIR.glob("pionex-grid-*.sqlite"))
    if not backups:
        raise CaptureError("No backup to restore")
    target = Path(db_path or DEFAULT_DB)
    shutil.copy2(backups[-1], target)
    return backups[-1]


def money(value: int | None) -> dict:
    return {"int": value, "usdt": from_fixed(value)}


def _num(value) -> float | None:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None


def _effective_liq(trend: str | None, down, up, liq) -> float | None:
    down_n, up_n, liq_n = _num(down), _num(up), _num(liq)
    if (trend or "") == "short" and up_n and up_n > 0:
        return up_n
    if (trend or "") != "short" and down_n and down_n > 0:
        return down_n
    if liq_n and liq_n > 0:
        return liq_n
    return None


def _pct(value: float | None) -> str | None:
    if value is None:
        return None
    return f"{value:.2f}"


def _board_row(snap: dict, profit_24h_i: int | None, daily_i: int | None, size_i: int | None) -> dict:
    mark = _num(snap.get("mark_price_s"))
    if mark is None and snap.get("mark_price_i") is not None:
        mark = float(Decimal(snap["mark_price_i"]) / Decimal(SCALE))
    liq = _effective_liq(snap.get("trend"), from_fixed(snap.get("estimate_liq_down_i")), from_fixed(snap.get("estimate_liq_up_i")), from_fixed(snap.get("liquidation_price_i")))
    if liq is None:
        liq = _num(from_fixed(snap.get("liquidation_price_i")))
    size_i = snap.get("investment_i") if snap.get("investment_i") is not None else size_i
    distance = None
    if mark and mark > 0 and liq and liq > 0:
        if (snap.get("trend") or "") == "short":
            distance = (liq - mark) / mark * 100
        else:
            distance = (mark - liq) / mark * 100
    elif snap.get("liq_distance_pct_s"):
        distance = _num(snap.get("liq_distance_pct_s"))
    profit_i = profit_24h_i if profit_24h_i is not None else daily_i
    pct = None
    if profit_i is not None and size_i:
        pct = float(Decimal(profit_i) / Decimal(size_i) * 100)
    return {
        "bu_order_id": snap.get("bu_order_id"),
        "opened_at": snap.get("created_at"),
        "symbol": snap.get("symbol"),
        "trend": snap.get("trend"),
        "leverage": snap.get("leverage"),
        "size": money(size_i),
        "investment": money(snap.get("investment_i")),
        "mark_price": None if mark is None else str(mark),
        "liq_price": None if liq is None else str(liq),
        "liq_distance_pct": _pct(distance),
        "grid_profit": money(snap.get("grid_profit_i")),
        "profit_24h": money(profit_24h_i),
        "daily_profit": money(daily_i),
        "day_pct": _pct(pct),
        "lifecycle": snap.get("lifecycle"),
        "status": snap.get("status"),
    }


def _rows(con: sqlite3.Connection, sql: str, params=()) -> list[dict]:
    return [dict(row) for row in con.execute(sql, params)]


def status_payload(db_path: Path | None = None) -> dict:
    con = connect(db_path)
    try:
        last = _last_success(con)
        failed = con.execute("SELECT * FROM capture_runs WHERE status!='success' ORDER BY id DESC LIMIT 5").fetchall()
        unresolved = con.execute("SELECT COUNT(*) AS n FROM grid_positions WHERE lifecycle='closed_unresolved'").fetchone()["n"]
        dates = [row["capture_date"] for row in con.execute("SELECT capture_date FROM daily_summary ORDER BY capture_date")]
        missing = []
        if len(dates) >= 2:
            start = datetime.fromisoformat(dates[0]).date()
            end = datetime.fromisoformat(dates[-1]).date()
            have = set(dates)
            cur = start
            while cur <= end:
                iso = cur.isoformat()
                if iso not in have:
                    missing.append(iso)
                cur += timedelta(days=1)
        return {
            "ok": last is not None,
            "last_success": dict(last) if last else None,
            "unresolved": unresolved,
            "missing_dates": missing,
            "recent_errors": [dict(row) for row in failed],
        }
    finally:
        con.close()


def daily_payload(db_path: Path | None = None) -> list[dict]:
    con = connect(db_path)
    try:
        out = []
        for row in con.execute("SELECT * FROM daily_summary ORDER BY capture_date"):
            item = dict(row)
            item["daily_profit"] = money(row["daily_profit_i"])
            item["cumulative"] = money(row["cumulative_i"])
            item["investment"] = money(row["investment_i"])
            out.append(item)
        return out
    finally:
        con.close()


def daily_date_payload(capture_date: str, db_path: Path | None = None) -> dict:
    con = connect(db_path)
    try:
        summary = con.execute("SELECT * FROM daily_summary WHERE capture_date=?", (capture_date,)).fetchone()
        if summary is None:
            return {"date": capture_date, "rows": []}
        rows = []
        for row in con.execute(
            """SELECT p.*, g.symbol, g.created_at FROM daily_grid_profit p
               JOIN grid_positions g ON g.bu_order_id=p.bu_order_id
               WHERE p.capture_date=? ORDER BY p.id""",
            (capture_date,),
        ):
            item = dict(row)
            item["prev_grid_profit"] = money(row["prev_grid_profit_i"])
            item["grid_profit"] = money(row["grid_profit_i"])
            item["daily_profit"] = money(row["daily_profit_i"])
            item["cumulative"] = money(row["cumulative_i"])
            item["investment"] = money(row["investment_i"])
            rows.append(item)
        payload = dict(summary)
        payload["daily_profit"] = money(summary["daily_profit_i"])
        payload["cumulative"] = money(summary["cumulative_i"])
        payload["investment"] = money(summary["investment_i"])
        payload["rows"] = rows
        return payload
    finally:
        con.close()


def board_payload(db_path: Path | None = None) -> dict:
    con = connect(db_path)
    try:
        last = _last_success(con)
        if last is None:
            return {"as_of": None, "window": "past_24h", "rows": [], "total_profit_24h": money(0), "position_count": 0}
        snaps = _rows(
            con,
            """SELECT s.*, p.lifecycle FROM grid_snapshots s
               JOIN grid_positions p ON p.bu_order_id=s.bu_order_id
               WHERE s.run_id=? AND s.list_status='running'
               ORDER BY s.symbol, s.created_at""",
            (last["id"],),
        )
        profits = {
            row["bu_order_id"]: row
            for row in con.execute("SELECT * FROM daily_grid_profit WHERE run_id=?", (last["id"],))
        }
        rows = []
        total_24h = 0
        for snap in snaps:
            profit = profits.get(snap["bu_order_id"])
            row = _board_row(
                snap,
                snap.get("grid_profit_24h_i"),
                profit["daily_profit_i"] if profit else None,
                snap.get("investment_i"),
            )
            if snap.get("grid_profit_24h_i"):
                total_24h += snap["grid_profit_24h_i"]
            rows.append(row)
        return {
            "as_of": last["captured_at"],
            "capture_date": last["capture_date"],
            "window": "past_24h",
            "position_count": len(rows),
            "total_profit_24h": money(total_24h),
            "rows": rows,
        }
    finally:
        con.close()


def days_payload(db_path: Path | None = None) -> dict:
    con = connect(db_path)
    try:
        days = []
        summaries = list(con.execute("SELECT * FROM daily_summary ORDER BY capture_date DESC"))
        for summary in summaries:
            snaps = _rows(
                con,
                """SELECT s.*, p.lifecycle, d.status, d.daily_profit_i, d.prev_grid_profit_i, d.cumulative_i
                   FROM daily_grid_profit d
                   JOIN grid_positions p ON p.bu_order_id=d.bu_order_id
                   LEFT JOIN grid_snapshots s ON s.run_id=d.run_id AND s.bu_order_id=d.bu_order_id
                   WHERE d.capture_date=?
                   ORDER BY p.symbol, p.created_at""",
                (summary["capture_date"],),
            )
            rows = []
            for snap in snaps:
                merged = dict(snap)
                rows.append(_board_row(merged, merged.get("grid_profit_24h_i"), merged.get("daily_profit_i"), merged.get("investment_i")))
            item = dict(summary)
            item["daily_profit"] = money(summary["daily_profit_i"])
            item["cumulative"] = money(summary["cumulative_i"])
            item["investment"] = money(summary["investment_i"])
            item["rows"] = rows
            days.append(item)
        return {"days": days}
    finally:
        con.close()


def grids_index(db_path: Path | None = None) -> list[dict]:
    con = connect(db_path)
    try:
        return _rows(con, "SELECT bu_order_id, symbol, created_at, lifecycle FROM grid_positions ORDER BY symbol, created_at")
    finally:
        con.close()


def grids_payload(db_path: Path | None = None) -> list[dict]:
    con = connect(db_path)
    try:
        last = _last_success(con)
        if last is None:
            return []
        rows = _rows(
            con,
            """SELECT s.*, p.lifecycle FROM grid_snapshots s
               JOIN grid_positions p ON p.bu_order_id=s.bu_order_id
               WHERE s.run_id=? ORDER BY s.symbol""",
            (last["id"],),
        )
        for row in rows:
            row["investment"] = money(row["investment_i"])
            row["grid_profit"] = money(row["grid_profit_i"])
            row["estimate_note"] = "估算"
        return rows
    finally:
        con.close()


def grid_history_payload(bu_order_id: str, db_path: Path | None = None) -> dict:
    con = connect(db_path)
    try:
        pos = con.execute("SELECT * FROM grid_positions WHERE bu_order_id=?", (bu_order_id,)).fetchone()
        snaps = _rows(con, "SELECT * FROM grid_snapshots WHERE bu_order_id=? ORDER BY run_id", (bu_order_id,))
        profits = _rows(con, "SELECT * FROM daily_grid_profit WHERE bu_order_id=? ORDER BY capture_date", (bu_order_id,))
        for row in snaps:
            row["investment"] = money(row["investment_i"])
            row["grid_profit"] = money(row["grid_profit_i"])
            row["margin_balance"] = money(row["margin_balance_i"])
            row["estimate_liq_up"] = money(row["estimate_liq_up_i"])
            row["estimate_liq_down"] = money(row["estimate_liq_down_i"])
        for row in profits:
            row["daily_profit"] = money(row["daily_profit_i"])
            row["grid_profit"] = money(row["grid_profit_i"])
        return {"position": dict(pos) if pos else None, "snapshots": snaps, "profits": profits}
    finally:
        con.close()


def summary_latest(db_path: Path | None = None) -> dict:
    con = connect(db_path)
    try:
        last = con.execute("SELECT * FROM daily_summary ORDER BY capture_date DESC LIMIT 1").fetchone()
        if last is None:
            return {"available": False}
        return {
            "available": True,
            "capture_date": last["capture_date"],
            "daily_profit_usdt": from_fixed(last["daily_profit_i"]),
            "cumulative_usdt": from_fixed(last["cumulative_i"]),
            "investment_usdt": from_fixed(last["investment_i"]),
            "active_count": last["active_count"],
            "new_count": last["new_count"],
            "closed_count": last["closed_count"],
            "unresolved_count": last["unresolved_count"],
            "gap_days": last["gap_days"],
        }
    finally:
        con.close()


def export_sheets(db_path: Path | None = None) -> dict[str, list[list]]:
    con = connect(db_path)
    try:
        daily = [["日期", "每日利潤", "累計", "投資額", "活躍", "新增", "關倉", "未決", "跨越天數"]]
        for row in con.execute("SELECT * FROM daily_summary ORDER BY capture_date"):
            daily.append([
                row["capture_date"], from_fixed(row["daily_profit_i"]), from_fixed(row["cumulative_i"]),
                from_fixed(row["investment_i"]), row["active_count"], row["new_count"],
                row["closed_count"], row["unresolved_count"], row["gap_days"],
            ])
        detail = [["日期", "網格ID", "狀態", "前次利潤", "當次利潤", "每日利潤", "累計", "投資額", "跨越天數"]]
        for row in con.execute("SELECT * FROM daily_grid_profit ORDER BY capture_date, id"):
            detail.append([
                row["capture_date"], row["bu_order_id"], row["status"],
                from_fixed(row["prev_grid_profit_i"]), from_fixed(row["grid_profit_i"]),
                from_fixed(row["daily_profit_i"]), from_fixed(row["cumulative_i"]),
                from_fixed(row["investment_i"]), row["gap_days"],
            ])
        settings = [["網格ID", "交易對", "建立", "類型", "上下限", "格數", "gridType", "perVolume", "槓桿", "狀態"]]
        for row in con.execute(
            """SELECT p.*, s.top_s, s.bottom_s, s.row_n, s.grid_type, s.per_volume_s, s.leverage
               FROM grid_positions p
               LEFT JOIN grid_snapshots s ON s.bu_order_id=p.bu_order_id
               AND s.id=(SELECT MAX(id) FROM grid_snapshots WHERE bu_order_id=p.bu_order_id)"""
        ):
            settings.append([
                row["bu_order_id"], row["symbol"], row["created_at"], row["product"],
                f"{row['top_s']}/{row['bottom_s']}", row["row_n"], row["grid_type"],
                row["per_volume_s"], row["leverage"], row["lifecycle"],
            ])
        runs = [["ID", "時間", "日期", "狀態", "活躍", "finished", "跨越天數", "錯誤"]]
        for row in con.execute("SELECT * FROM capture_runs ORDER BY id"):
            runs.append([
                row["id"], row["captured_at"], row["capture_date"], row["status"],
                row["running_count"], row["finished_count"], row["gap_days"], row["error"],
            ])
        return {
            "每日報酬": daily,
            "網格日明細": detail,
            "網格設定": settings,
            "執行紀錄": runs,
        }
    finally:
        con.close()
