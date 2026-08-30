"""SQLite ledger for Pionex contract-grid snapshots."""

from __future__ import annotations

import json
import shutil
import sqlite3
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from zoneinfo import ZoneInfo

from .fx import attach_twd, attach_twd_tree, usdt_twd
from .liq import normalize_liq

SCALE = 100_000_000
SCHEMA_VERSION = 10
EVENT_EPS = 1_000_000
ADD_MIN_I = 10 * SCALE
REINVEST_MIN_I = 20 * SCALE
SMALL_ADD_I = 100 * SCALE
ADD_TYPICAL_I = 200 * SCALE
REVIEW_DROP_I = 100 * SCALE
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
  grid_profit_coin_i INTEGER, conversion_price_i INTEGER, conversion_symbol TEXT,
  grid_profit_24h_i INTEGER, fee_i INTEGER, funding_fee_i INTEGER,
  profit_reinvest_i INTEGER, profit_withdrawn_i INTEGER, profit_reduce_i INTEGER,
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
  withdrawn_i INTEGER,
  reinvest_i INTEGER,
  reduce_i INTEGER,
  lifetime_i INTEGER,
  event TEXT,
  gap_days INTEGER,
  grid_profit_coin_i INTEGER,
  daily_profit_coin_i INTEGER,
  conversion_price_i INTEGER,
  fx_gap_i INTEGER,
  UNIQUE(capture_date, bu_order_id)
);
CREATE TABLE IF NOT EXISTS daily_summary (
  capture_date TEXT PRIMARY KEY,
  run_id INTEGER NOT NULL REFERENCES capture_runs(id),
  daily_profit_i INTEGER,
  cumulative_i INTEGER,
  true_profit_i INTEGER,
  investment_i INTEGER,
  active_count INTEGER,
  new_count INTEGER,
  closed_count INTEGER,
  unresolved_count INTEGER,
  gap_days INTEGER,
  fx_gap_i INTEGER
);
CREATE TABLE IF NOT EXISTS event_overrides (
  capture_date TEXT NOT NULL,
  bu_order_id TEXT NOT NULL,
  decision TEXT NOT NULL,
  decided_at TEXT NOT NULL,
  PRIMARY KEY (capture_date, bu_order_id)
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


def _nz(value) -> int:
    return int(value or 0)


def _lifetime_i(grid_i, withdrawn_i=0, reinvest_i=0, reduce_i=0, inferred_i=0) -> int:
    return _nz(grid_i) + _nz(withdrawn_i) + _nz(reinvest_i) + _nz(reduce_i) + _nz(inferred_i)


def _rowget(row, key, default=None):
    if row is None:
        return default
    try:
        value = row[key]
    except (KeyError, IndexError, TypeError):
        return default
    return default if value is None else value


def _number_from(data: dict, *keys) -> float:
    if not isinstance(data, dict):
        return 0.0
    for key in keys:
        val = data.get(key)
        if val in (None, ""):
            continue
        try:
            number = float(val)
        except (TypeError, ValueError):
            continue
        if number == number:
            return number
    return 0.0


def _raw_bu_data(raw_json) -> dict:
    if isinstance(raw_json, dict):
        raw = raw_json
    else:
        try:
            raw = json.loads(raw_json or "{}")
        except (TypeError, json.JSONDecodeError):
            return {}
    if not isinstance(raw, dict):
        return {}
    order = raw.get("order")
    if isinstance(order, dict):
        data = order.get("buOrderData")
        if isinstance(data, dict):
            return data
    data = raw.get("buOrderData")
    return data if isinstance(data, dict) else raw


def _prefer_stock(stored, raw) -> int:
    stored_i = _nz(stored)
    raw_i = _nz(raw)
    return raw_i if stored_i == 0 and raw_i > 0 else stored_i


def _stock_triple_from_raw(raw_json, conversion=1.0) -> tuple[int, int, int]:
    data = _raw_bu_data(raw_json)
    conv = conversion if conversion not in (None, 0) else 1.0
    def one(*keys) -> int:
        return to_fixed(_number_from(data, *keys) * conv) or 0
    return (
        one("profitWithdrawn", "profitWithdrawnU", "profit_withdrawn", "profitExited"),
        one("profitReinvest", "profit_reinvest"),
        one("profitReduce", "profit_reduce"),
    )


def _conversion_for_snapshot(snap, data: dict) -> float:
    product = _rowget(snap, "product") or ""
    if product != "coin_margined_contract_grid":
        return 1.0
    raw_g = _number_from(data, "gridProfit", "grid_profit")
    grid_i = _rowget(snap, "grid_profit_i")
    if raw_g and grid_i:
        return float(Decimal(grid_i) / Decimal(SCALE) / Decimal(str(raw_g)))
    mark_i = _rowget(snap, "mark_price_i")
    if mark_i:
        return float(Decimal(mark_i) / Decimal(SCALE))
    return 1.0


def _stocks_from_snapshot(snap) -> tuple[int, int, int]:
    raw_json = _rowget(snap, "raw_json")
    conv = _conversion_for_snapshot(snap, _raw_bu_data(raw_json))
    raw_wd, raw_re, raw_rd = _stock_triple_from_raw(raw_json, conv)
    return (
        _prefer_stock(_rowget(snap, "profit_withdrawn_i"), raw_wd),
        _prefer_stock(_rowget(snap, "profit_reinvest_i"), raw_re),
        _prefer_stock(_rowget(snap, "profit_reduce_i"), raw_rd),
    )


def _rec_stock_triple(rec: dict) -> tuple[int, int, int]:
    conv = rec.get("ConversionPrice") or 1.0
    if rec.get("Product") != "coin_margined_contract_grid":
        conv = 1.0
    raw_wd, raw_re, raw_rd = _stock_triple_from_raw(rec.get("RawJson"), conv)
    return (
        _prefer_stock(to_fixed(rec.get("ProfitWithdrawn")), raw_wd),
        _prefer_stock(to_fixed(rec.get("ProfitReinvest")), raw_re),
        _prefer_stock(to_fixed(rec.get("ProfitReduce")), raw_rd),
    )


def _mul_fixed(left_i, right_i) -> int:
    return int((Decimal(_nz(left_i)) * Decimal(_nz(right_i)) / Decimal(SCALE)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _is_coin_margined(product) -> bool:
    return (product or "") == "coin_margined_contract_grid"


def _coin_from_rec(rec: dict) -> tuple[int | None, int | None, str | None]:
    if not _is_coin_margined(rec.get("Product")):
        return None, None, None
    raw = rec.get("RawGridProfit")
    if raw in (None, ""):
        data = _raw_bu_data(rec.get("RawJson"))
        if isinstance(data, dict) and any(k in data for k in ("gridProfit", "grid_profit")):
            raw = _number_from(data, "gridProfit", "grid_profit")
        else:
            raw = None
    px = rec.get("ConversionPrice")
    if px in (None, "", 0) and raw not in (None, 0) and rec.get("GridProfit") not in (None, ""):
        try:
            px = float(rec.get("GridProfit")) / float(raw)
        except (TypeError, ValueError, ZeroDivisionError):
            px = None
    sym = rec.get("ConversionSymbol") or rec.get("RawGridProfitCurrency")
    return to_fixed(raw), to_fixed(px), None if not sym else str(sym)


def _coin_from_snapshot(snap) -> tuple[int | None, int | None, str | None]:
    if snap is None or not _is_coin_margined(_rowget(snap, "product")):
        return None, None, None
    coin_i = _rowget(snap, "grid_profit_coin_i")
    px_i = _rowget(snap, "conversion_price_i")
    sym = _rowget(snap, "conversion_symbol")
    data = _raw_bu_data(_rowget(snap, "raw_json"))
    raw = _number_from(data, "gridProfit", "grid_profit")
    if coin_i in (None, 0) and raw:
        coin_i = to_fixed(raw)
    if px_i in (None, 0) and raw and _rowget(snap, "grid_profit_i"):
        try:
            px_i = to_fixed(Decimal(_rowget(snap, "grid_profit_i")) / Decimal(SCALE) / Decimal(str(raw)))
        except (ArithmeticError, ValueError, TypeError, ZeroDivisionError):
            px_i = None
    if not sym:
        quote = data.get("quote") if isinstance(data, dict) else None
        order = _raw_bu_data(_rowget(snap, "raw_json"))
        # quote lives on order, not buOrderData
        raw_blob = _rowget(snap, "raw_json")
        try:
            blob = json.loads(raw_blob) if isinstance(raw_blob, str) else (raw_blob or {})
        except (TypeError, json.JSONDecodeError):
            blob = {}
        order_obj = blob.get("order") if isinstance(blob, dict) else {}
        quote = (order_obj or {}).get("quote") if isinstance(order_obj, dict) else quote
        if quote:
            sym = f"{str(quote).replace('.PERP', '')}_USDT"
    return coin_i, px_i, None if not sym else str(sym)


def _prev_revalued(prev, px_today_i):
    if prev is None or px_today_i in (None, 0):
        return prev
    prev_coin = _rowget(prev, "grid_profit_coin_i")
    if prev_coin is None:
        return prev
    if hasattr(prev, "keys"):
        copied = {key: prev[key] for key in prev.keys()}
    else:
        copied = dict(prev)
    copied["grid_profit_i"] = _mul_fixed(prev_coin, px_today_i)
    return copied


def _fx_gap_i(prev, coin_i, px_i, mtm_i) -> int:
    if prev is None or coin_i is None or px_i in (None, 0):
        return 0
    prev_coin = _rowget(prev, "grid_profit_coin_i")
    if prev_coin is None:
        return 0
    prev_mtm = _nz(_rowget(prev, "grid_profit_i"))
    return _mul_fixed(prev_coin, px_i) - prev_mtm


def _amounts_close(left, right) -> bool:
    a, b = _nz(left), _nz(right)
    if a <= 0 or b <= 0:
        return False
    diff = abs(a - b)
    rel = max(a, b) // 4
    floor = to_fixed(50) or 0
    return diff <= max(floor, rel)


def _load_overrides(con: sqlite3.Connection) -> dict[tuple[str, str], str]:
    try:
        rows = con.execute("SELECT capture_date, bu_order_id, decision FROM event_overrides")
    except sqlite3.OperationalError:
        return {}
    return {(row["capture_date"], row["bu_order_id"]): row["decision"] for row in rows}


def _classify_event(status: str, prev, grid_i, inv_i, withdrawn_i, reinvest_i, reduce_i, override=None) -> tuple[str, int, int]:
    """Return (event, daily_i, lifetime_i).

    複投: 網格利潤明顯掉進倉位（倉位變大、網格利潤少掉相近的一筆）。
    加倉: 倉位變多，網格利潤仍正常走、沒有被重置。
    lifetime is running true profit from our snapshots, not Pionex lifetime stocks.
    """
    if status in ("baseline", "new") or prev is None:
        return status, 0, _nz(grid_i)
    prev_grid = _rowget(prev, "grid_profit_i")
    prev_inv = _rowget(prev, "investment_i")
    prev_wd = _rowget(prev, "withdrawn_i")
    prev_re = _rowget(prev, "reinvest_i")
    prev_rd = _rowget(prev, "reduce_i")
    if prev_wd is None:
        prev_wd = withdrawn_i
    if prev_re is None:
        prev_re = reinvest_i
    if prev_rd is None:
        prev_rd = reduce_i
    g_d = _nz(grid_i) - _nz(prev_grid)
    inv_d = _nz(inv_i) - _nz(prev_inv)
    re_d = _nz(reinvest_i) - _nz(prev_re)
    wd_d = _nz(withdrawn_i) - _nz(prev_wd)
    rd_d = _nz(reduce_i) - _nz(prev_rd)
    grid_drop = max(0, -g_d)
    if re_d > EVENT_EPS and g_d >= -EVENT_EPS:
        re_d = 0
    if wd_d > EVENT_EPS and g_d >= -EVENT_EPS:
        wd_d = 0
    if rd_d > EVENT_EPS and g_d >= -EVENT_EPS:
        rd_d = 0
    events = []
    inferred = 0
    kind = None
    matched = inv_d >= REINVEST_MIN_I and grid_drop >= REINVEST_MIN_I and _amounts_close(inv_d, grid_drop)
    api_reinvest = re_d >= REINVEST_MIN_I and grid_drop >= REINVEST_MIN_I
    if override in ("reinvest", "add"):
        kind = override
    elif matched or api_reinvest:
        kind = "reinvest"
    elif inv_d >= ADD_MIN_I and grid_drop < REINVEST_MIN_I:
        kind = "add"
    elif inv_d >= ADD_TYPICAL_I and grid_drop < REVIEW_DROP_I:
        kind = "add"
    elif ADD_MIN_I <= inv_d < SMALL_ADD_I and grid_drop >= REINVEST_MIN_I:
        kind = "reinvest"
    elif inv_d >= ADD_MIN_I and grid_drop >= REVIEW_DROP_I:
        kind = "review"
    elif SMALL_ADD_I <= inv_d < ADD_TYPICAL_I and grid_drop >= REINVEST_MIN_I:
        kind = "review"
    elif inv_d >= ADD_MIN_I:
        kind = "add"
    if kind == "reinvest":
        events.append("reinvest")
        if re_d <= EVENT_EPS:
            cap = _nz(prev_grid) if matched or api_reinvest else grid_drop
            inferred = min(max(inv_d, 0), cap)
    elif kind == "add":
        events.append("add")
        re_d = 0
    elif kind == "review":
        events.append("review")
        re_d = 0
    if wd_d > EVENT_EPS:
        events.append("withdraw")
    if rd_d > EVENT_EPS:
        events.append("reduce")
    daily = g_d + re_d + wd_d + rd_d + inferred
    prev_life = _rowget(prev, "lifetime_i")
    if prev_life is None:
        prev_life = _nz(prev_grid)
    life = _nz(prev_life) + daily
    if status == "closed":
        event = "closed" if not events else "closed+" + "+".join(events)
        return event, daily, life
    if not events:
        return "continue", daily, life
    return "+".join(events), daily, life


def _true_grid_profit_i(con: sqlite3.Connection, capture_date: str) -> int:
    rows = con.execute(
        """SELECT bu_order_id, lifetime_i FROM daily_grid_profit
           WHERE capture_date<=? AND lifetime_i IS NOT NULL
           ORDER BY capture_date DESC, id DESC""",
        (capture_date,),
    )
    seen = set()
    total = 0
    for row in rows:
        oid = row["bu_order_id"]
        if oid in seen:
            continue
        seen.add(oid)
        total += _nz(row["lifetime_i"])
    return total


def rebuild_daily_profits(con: sqlite3.Connection) -> None:
    """Recompute daily/lifetime from snapshots. Backfills profitReinvest stocks from raw JSON."""
    runs = list(con.execute("SELECT * FROM capture_runs WHERE status='success' ORDER BY capture_date, id"))
    if not runs:
        return
    overrides = _load_overrides(con)
    prev_by_id: dict[str, dict] = {}
    for run in runs:
        date = run["capture_date"]
        snaps = {
            row["bu_order_id"]: row
            for row in con.execute("SELECT * FROM grid_snapshots WHERE run_id=?", (run["id"],))
        }
        profit_rows = list(con.execute("SELECT * FROM daily_grid_profit WHERE capture_date=? ORDER BY id", (date,)))
        daily_total = 0
        new_cum = 0
        fx_gap_total = 0
        for prow in profit_rows:
            oid = prow["bu_order_id"]
            snap = snaps.get(oid)
            status = prow["status"]
            prev = prev_by_id.get(oid)
            coin_i = daily_coin_i = px_i = fx_gap = None
            if snap is not None:
                withdrawn_i, reinvest_i, reduce_i = _stocks_from_snapshot(snap)
                coin_i, px_i, px_sym = _coin_from_snapshot(snap)
                con.execute(
                    """UPDATE grid_snapshots SET profit_withdrawn_i=?, profit_reinvest_i=?, profit_reduce_i=?,
                         grid_profit_coin_i=?, conversion_price_i=?, conversion_symbol=? WHERE id=?""",
                    (withdrawn_i, reinvest_i, reduce_i, coin_i, px_i, px_sym, snap["id"]),
                )
                grid_i = snap["grid_profit_i"]
                inv_i = snap["investment_i"]
            else:
                withdrawn_i = _nz(prow["withdrawn_i"])
                reinvest_i = _nz(prow["reinvest_i"])
                reduce_i = _nz(prow["reduce_i"])
                grid_i = prow["grid_profit_i"]
                inv_i = prow["investment_i"]
            if status == "closed_unresolved":
                event = prow["event"] or "closed_unresolved"
                daily_i = None
                life_i = prev["lifetime_i"] if prev and prev.get("lifetime_i") is not None else prow["lifetime_i"]
                cum_i = prev["cumulative_i"] if prev else prow["cumulative_i"]
                if prev:
                    withdrawn_i = prev.get("withdrawn_i", withdrawn_i)
                    reinvest_i = prev.get("reinvest_i", reinvest_i)
                    reduce_i = prev.get("reduce_i", reduce_i)
                    coin_i = prev.get("grid_profit_coin_i")
                    px_i = prev.get("conversion_price_i")
                grid_i = None
            else:
                class_grid = _mul_fixed(coin_i, px_i) if coin_i is not None and px_i else grid_i
                class_prev = _prev_revalued(prev, px_i) if coin_i is not None else prev
                event, daily_i, life_i = _classify_event(
                    status, class_prev, class_grid, inv_i, withdrawn_i, reinvest_i, reduce_i,
                    override=overrides.get((date, oid)),
                )
                cum_i = grid_i
                daily_total += daily_i
                if status in ("baseline", "new", "continue") and grid_i is not None:
                    new_cum += grid_i
                if coin_i is not None:
                    daily_coin_i = 0 if status in ("baseline", "new") or prev is None else coin_i - _nz(prev.get("grid_profit_coin_i"))
                    fx_gap = 0 if status in ("baseline", "new") else _fx_gap_i(prev, coin_i, px_i, grid_i)
                    fx_gap_total += _nz(fx_gap)
            con.execute(
                """UPDATE daily_grid_profit SET grid_profit_i=?, daily_profit_i=?, cumulative_i=?,
                     investment_i=?, withdrawn_i=?, reinvest_i=?, reduce_i=?, lifetime_i=?, event=?,
                     grid_profit_coin_i=?, daily_profit_coin_i=?, conversion_price_i=?, fx_gap_i=?
                   WHERE id=?""",
                (grid_i, daily_i, cum_i, inv_i, withdrawn_i, reinvest_i, reduce_i, life_i, event,
                 coin_i, daily_coin_i, px_i, fx_gap, prow["id"]),
            )
            prev_by_id[oid] = {
                "grid_profit_i": grid_i,
                "investment_i": inv_i,
                "withdrawn_i": withdrawn_i,
                "reinvest_i": reinvest_i,
                "reduce_i": reduce_i,
                "lifetime_i": life_i,
                "cumulative_i": cum_i,
                "status": status,
                "grid_profit_coin_i": coin_i,
                "conversion_price_i": px_i,
            }
        true_i = _true_grid_profit_i(con, date)
        con.execute(
            "UPDATE daily_summary SET daily_profit_i=?, cumulative_i=?, true_profit_i=?, fx_gap_i=? WHERE capture_date=?",
            (daily_total, new_cum, true_i, fx_gap_total, date),
        )


def apply_event_decision(capture_date: str, bu_order_id: str, decision: str, db_path: Path | None = None) -> dict:
    if decision not in ("add", "reinvest"):
        raise CaptureError("decision must be add or reinvest")
    if not capture_date or not bu_order_id:
        raise CaptureError("capture_date and bu_order_id are required")
    con = connect(db_path)
    try:
        row = con.execute(
            "SELECT 1 FROM daily_grid_profit WHERE capture_date=? AND bu_order_id=?",
            (capture_date, bu_order_id),
        ).fetchone()
        if row is None:
            raise CaptureError(f"No daily row for {capture_date} {bu_order_id}")
        con.execute(
            """INSERT INTO event_overrides(capture_date, bu_order_id, decision, decided_at)
               VALUES (?,?,?,?)
               ON CONFLICT(capture_date, bu_order_id) DO UPDATE SET
                 decision=excluded.decision, decided_at=excluded.decided_at""",
            (capture_date, bu_order_id, decision, datetime.now(TAIPEI).isoformat()),
        )
        rebuild_daily_profits(con)
        con.commit()
        updated = con.execute(
            "SELECT event, daily_profit_i, lifetime_i FROM daily_grid_profit WHERE capture_date=? AND bu_order_id=?",
            (capture_date, bu_order_id),
        ).fetchone()
        return {
            "ok": True,
            "capture_date": capture_date,
            "bu_order_id": bu_order_id,
            "decision": decision,
            "event": updated["event"] if updated else decision,
            "daily_profit": from_fixed(updated["daily_profit_i"]) if updated else None,
        }
    finally:
        con.close()


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
    con = sqlite3.connect(str(path), timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout = 30000")
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
    run_cols = {row[1] for row in con.execute("PRAGMA table_info(capture_runs)")}
    if "wallet_total_usdt" not in run_cols:
        con.execute("ALTER TABLE capture_runs ADD COLUMN wallet_total_usdt TEXT")
    if "wallet_json" not in run_cols:
        con.execute("ALTER TABLE capture_runs ADD COLUMN wallet_json TEXT")
    for name, decl in (
        ("fx_usdt_twd", "REAL"),
        ("fx_usdt_usd", "REAL"),
        ("fx_usd_cash_buy", "REAL"),
        ("fx_source", "TEXT"),
        ("fx_as_of", "TEXT"),
    ):
        if name not in run_cols:
            con.execute(f"ALTER TABLE capture_runs ADD COLUMN {name} {decl}")
    snap_cols = {row[1] for row in con.execute("PRAGMA table_info(grid_snapshots)")}
    if "profit_reduce_i" not in snap_cols:
        con.execute("ALTER TABLE grid_snapshots ADD COLUMN profit_reduce_i INTEGER")
    sum_cols = {row[1] for row in con.execute("PRAGMA table_info(daily_summary)")}
    if "true_profit_i" not in sum_cols:
        con.execute("ALTER TABLE daily_summary ADD COLUMN true_profit_i INTEGER")
    profit_cols = {row[1] for row in con.execute("PRAGMA table_info(daily_grid_profit)")}
    for name, decl in (
        ("withdrawn_i", "INTEGER"),
        ("reinvest_i", "INTEGER"),
        ("reduce_i", "INTEGER"),
        ("lifetime_i", "INTEGER"),
        ("event", "TEXT"),
    ):
        if name not in profit_cols:
            con.execute(f"ALTER TABLE daily_grid_profit ADD COLUMN {name} {decl}")
    snap_cols = {row[1] for row in con.execute("PRAGMA table_info(grid_snapshots)")}
    for name, decl in (
        ("grid_profit_coin_i", "INTEGER"),
        ("conversion_price_i", "INTEGER"),
        ("conversion_symbol", "TEXT"),
    ):
        if name not in snap_cols:
            con.execute(f"ALTER TABLE grid_snapshots ADD COLUMN {name} {decl}")
    profit_cols = {row[1] for row in con.execute("PRAGMA table_info(daily_grid_profit)")}
    for name, decl in (
        ("grid_profit_coin_i", "INTEGER"),
        ("daily_profit_coin_i", "INTEGER"),
        ("conversion_price_i", "INTEGER"),
        ("fx_gap_i", "INTEGER"),
    ):
        if name not in profit_cols:
            con.execute(f"ALTER TABLE daily_grid_profit ADD COLUMN {name} {decl}")
    sum_cols = {row[1] for row in con.execute("PRAGMA table_info(daily_summary)")}
    if "fx_gap_i" not in sum_cols:
        con.execute("ALTER TABLE daily_summary ADD COLUMN fx_gap_i INTEGER")
    con.execute(
        """CREATE TABLE IF NOT EXISTS event_overrides (
             capture_date TEXT NOT NULL,
             bu_order_id TEXT NOT NULL,
             decision TEXT NOT NULL,
             decided_at TEXT NOT NULL,
             PRIMARY KEY (capture_date, bu_order_id)
           )"""
    )
    latest = con.execute("SELECT version FROM schema_meta ORDER BY version DESC LIMIT 1").fetchone()
    current = int(latest["version"]) if latest is not None else 0
    if current < SCHEMA_VERSION:
        if current < 10:
            rebuild_daily_profits(con)
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
    withdrawn_i, reinvest_i, reduce_i = _rec_stock_triple(rec)
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
        reinvest_i,
        withdrawn_i,
        reduce_i,
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
        overrides = _load_overrides(con)

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
        wallet = snapshot.get("Wallet") or {}
        if wallet:
            con.execute(
                "UPDATE capture_runs SET wallet_total_usdt=?, wallet_json=? WHERE id=?",
                (wallet.get("totalInUsdt"), json.dumps(wallet, ensure_ascii=False), run_id),
            )
        fx = snapshot.get("Fx") or usdt_twd()
        con.execute(
            """UPDATE capture_runs SET fx_usdt_twd=?, fx_usdt_usd=?, fx_usd_cash_buy=?, fx_source=?, fx_as_of=?
               WHERE id=?""",
            (
                fx.get("usdt_twd"),
                fx.get("usdt_usd"),
                fx.get("usd_cash_buy"),
                fx.get("source"),
                fx.get("as_of"),
                run_id,
            ),
        )
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
                    profit_reinvest_i, profit_withdrawn_i, profit_reduce_i, extra_margin_i, margin_balance_i, init_margin_i,
                    risk_status, margin_status, estimate_liq_up_i, estimate_liq_down_i, liquidation_price_i,
                    liquidation_triggered, matched_grids, order_count, volume_s, loss_stop_type, loss_stop,
                    profit_stop_type, profit_stop, pause_price_s, moving_indicator_type, moving_top_s, moving_bottom_s,
                    estimated_step_pct_s, estimated_step_price_s, estimated_range_pct_s, complete, raw_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                _snapshot_fields(run_id, rec),
            )
            _apply_live_fields(con, run_id, rec)
            coin_i, px_i, px_sym = _coin_from_rec(rec)
            if coin_i is not None or px_i is not None:
                con.execute(
                    "UPDATE grid_snapshots SET grid_profit_coin_i=?, conversion_price_i=?, conversion_symbol=? WHERE run_id=? AND bu_order_id=?",
                    (coin_i, px_i, px_sym, run_id, oid),
                )
            grid_profit_i = to_fixed(rec.get("GridProfit"))
            investment_i = to_fixed(rec.get("Investment"))
            withdrawn_i, reinvest_i, reduce_i = _rec_stock_triple(rec)
            prev_row = _latest_profit(con, oid)
            if is_baseline or pos is None:
                status = "baseline" if is_baseline else "new"
                if status == "new":
                    new_count += 1
                prev_i = None
            elif prev_row is None or prev_row["status"] in ("closed", "closed_unresolved"):
                status = "new"
                new_count += 1
                prev_i = None
            else:
                status = "continue"
                prev_i = prev_row["grid_profit_i"]
                if prev_i is None or grid_profit_i is None:
                    raise CaptureError(f"Cannot compute daily profit for {oid}")
            class_grid = _mul_fixed(coin_i, px_i) if coin_i is not None and px_i else grid_profit_i
            class_prev = _prev_revalued(prev_row, px_i) if coin_i is not None else prev_row
            event, daily_i, life_i = _classify_event(
                status, class_prev, class_grid, investment_i, withdrawn_i, reinvest_i, reduce_i,
                override=overrides.get((capture_date, oid)),
            )
            daily_coin_i = 0 if status in ("baseline", "new") or prev_row is None else (None if coin_i is None else coin_i - _nz(_rowget(prev_row, "grid_profit_coin_i")))
            fx_gap = _fx_gap_i(prev_row, coin_i, px_i, grid_profit_i) if status not in ("baseline", "new") else 0
            cum_i = grid_profit_i
            profit_rows.append((
                run_id, capture_date, oid, status, prev_i, grid_profit_i, daily_i, cum_i, investment_i,
                withdrawn_i, reinvest_i, reduce_i, life_i, event, gap_days,
                coin_i, daily_coin_i, px_i, fx_gap,
            ))
            daily_total += daily_i
            investment_total += investment_i or 0

        for oid, pos in by_id.items():
            if pos["lifecycle"] != "active" or oid in running_ids:
                continue
            rec = finished_by_id.get(oid)
            prev_row = _latest_profit(con, oid)
            prev_i = prev_row["grid_profit_i"] if prev_row else None
            if rec:
                withdrawn_i, reinvest_i, reduce_i = _rec_stock_triple(rec)
            else:
                withdrawn_i = _rowget(prev_row, "withdrawn_i", 0)
                reinvest_i = _rowget(prev_row, "reinvest_i", 0)
                reduce_i = _rowget(prev_row, "reduce_i", 0)
            coin_i = daily_coin_i = px_i = fx_gap = None
            if rec is not None and rec.get("Complete") and rec.get("GridProfit") is not None and prev_i is not None:
                final_i = to_fixed(rec.get("GridProfit"))
                coin_i, px_i, px_sym = _coin_from_rec(rec)
                class_grid = _mul_fixed(coin_i, px_i) if coin_i is not None and px_i else final_i
                class_prev = _prev_revalued(prev_row, px_i) if coin_i is not None else prev_row
                event, daily_i, life_i = _classify_event(
                    "closed", class_prev, class_grid, to_fixed(rec.get("Investment")), withdrawn_i or 0, reinvest_i or 0, reduce_i or 0,
                    override=overrides.get((capture_date, oid)),
                )
                daily_coin_i = None if coin_i is None else coin_i - _nz(_rowget(prev_row, "grid_profit_coin_i"))
                fx_gap = _fx_gap_i(prev_row, coin_i, px_i, final_i)
                cum_i = final_i
                status = "closed"
                closed_count += 1
                daily_total += daily_i
                con.execute(
                    """INSERT INTO grid_snapshots(run_id, bu_order_id, list_status, bot_status, symbol, created_at, closed_at, product,
                        leverage, trend, grid_type, top_s, bottom_s, row_n, per_volume_s, top_i, bottom_i, per_volume_i,
                        open_price_s, position_s, position_open_price_s, base_amount_s, quote_amount_s,
                        investment_i, grid_profit_i, total_profit_i, grid_profit_24h_i, fee_i, funding_fee_i,
                        profit_reinvest_i, profit_withdrawn_i, profit_reduce_i, extra_margin_i, margin_balance_i, init_margin_i,
                        risk_status, margin_status, estimate_liq_up_i, estimate_liq_down_i, liquidation_price_i,
                        liquidation_triggered, matched_grids, order_count, volume_s, loss_stop_type, loss_stop,
                        profit_stop_type, profit_stop, pause_price_s, moving_indicator_type, moving_top_s, moving_bottom_s,
                        estimated_step_pct_s, estimated_step_price_s, estimated_range_pct_s, complete, raw_json)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    _snapshot_fields(run_id, rec),
                )
                _apply_live_fields(con, run_id, rec)
                if coin_i is not None or px_i is not None:
                    con.execute(
                        "UPDATE grid_snapshots SET grid_profit_coin_i=?, conversion_price_i=?, conversion_symbol=? WHERE run_id=? AND bu_order_id=?",
                        (coin_i, px_i, px_sym, run_id, oid),
                    )
                con.execute(
                    "UPDATE grid_positions SET lifecycle='closed', closed_at=? WHERE bu_order_id=?",
                    (rec.get("Closed") or captured.strftime("%Y-%m-%d %H:%M:%S"), oid),
                )
            else:
                status = "closed_unresolved"
                daily_i = None
                final_i = None
                event = "closed_unresolved"
                life_i = _rowget(prev_row, "lifetime_i")
                if life_i is None:
                    life_i = _nz(prev_i)
                cum_i = prev_row["cumulative_i"] if prev_row else None
                withdrawn_i = _rowget(prev_row, "withdrawn_i", 0)
                reinvest_i = _rowget(prev_row, "reinvest_i", 0)
                reduce_i = _rowget(prev_row, "reduce_i", 0)
                unresolved += 1
                con.execute("UPDATE grid_positions SET lifecycle='closed_unresolved' WHERE bu_order_id=?", (oid,))
            profit_rows.append((
                run_id, capture_date, oid, status, prev_i, final_i if status == "closed" else None, daily_i, cum_i,
                None, withdrawn_i or 0, reinvest_i or 0, reduce_i or 0, life_i, event, gap_days,
                coin_i, daily_coin_i, px_i, fx_gap,
            ))

        for row in profit_rows:
            con.execute(
                """INSERT INTO daily_grid_profit(run_id, capture_date, bu_order_id, status, prev_grid_profit_i,
                     grid_profit_i, daily_profit_i, cumulative_i, investment_i, withdrawn_i, reinvest_i, reduce_i,
                     lifetime_i, event, gap_days, grid_profit_coin_i, daily_profit_coin_i, conversion_price_i, fx_gap_i)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                row,
            )

        new_cum = 0
        fx_gap_total = 0
        for row in profit_rows:
            if row[3] in ("baseline", "new", "continue") and row[5] is not None:
                new_cum += row[5]
            if row[18] is not None:
                fx_gap_total += row[18]
        true_i = _true_grid_profit_i(con, capture_date)
        con.execute(
            """INSERT INTO daily_summary(capture_date, run_id, daily_profit_i, cumulative_i, true_profit_i, investment_i,
                 active_count, new_count, closed_count, unresolved_count, gap_days, fx_gap_i)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (capture_date, run_id, daily_total, new_cum, true_i, investment_total, len(running), new_count, closed_count, unresolved, gap_days, fx_gap_total),
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


def _pct_value(profit_i, investment_i) -> float | None:
    if profit_i is None or not investment_i:
        return None
    return float(Decimal(profit_i) / Decimal(investment_i) * 100)


def _fx_view(fx: dict | None) -> dict:
    fx = fx or {}
    rate = fx.get("usdt_twd") if fx.get("usdt_twd") is not None else fx.get("usd_twd")
    return {
        "usdt_twd": rate,
        "usd_twd": rate,
        "usdt_usd": fx.get("usdt_usd"),
        "usd_cash_buy": fx.get("usd_cash_buy"),
        "as_of": fx.get("as_of"),
        "source": fx.get("source"),
    }


def _rate_of(row, live: float | None) -> float | None:
    stored = None
    if row is not None:
        try:
            stored = row["fx_usdt_twd"]
        except (KeyError, IndexError, TypeError):
            stored = None
    if stored is not None:
        return float(stored)
    return live


def _excel_num(value_i) -> float | None:
    text = from_fixed(value_i)
    return None if text is None else float(text)


def _excel_twd(value_i, rate) -> int | None:
    if value_i is None or not rate:
        return None
    text = from_fixed(value_i)
    if text is None:
        return None
    return int((Decimal(text) * Decimal(str(rate))).quantize(Decimal("1."), rounding=ROUND_HALF_UP))


def _excel_pct(profit_i, investment_i) -> float | None:
    pct = _pct_value(profit_i, investment_i)
    if pct is None:
        return None
    return float(Decimal(str(pct)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))


def _float_money(value: int | None) -> float:
    text = from_fixed(value)
    return float(text) if text is not None else 0.0


def _leverage_value(snap: dict) -> float:
    text = str(snap.get("leverage") or "")
    digits = []
    buf = ""
    for ch in text:
        if ch.isdigit() or ch == ".":
            buf += ch
        elif buf:
            break
    try:
        value = float(buf) if buf else 1.0
    except ValueError:
        value = 1.0
    return value if value > 0 else 1.0


def _pnl_metrics(snap: dict, mark: float | None, as_of: datetime | None = None) -> dict:
    """Match Pionex grid UI: total profit = trend + grid; funding shown separately."""
    investment = _float_money(snap.get("investment_i"))
    grid = _float_money(snap.get("grid_profit_i"))
    funding = _float_money(snap.get("funding_fee_i"))
    openp = _num(snap.get("position_open_price_s")) or _num(snap.get("open_price_s"))
    coin = snap.get("product") == "coin_margined_contract_grid"
    side = -1.0 if (snap.get("trend") or "") == "short" else 1.0
    if coin and openp and mark and 0 < openp < 1 < mark:
        openp = 1.0 / openp
        side = -side
    trend = None
    if investment > 0 and mark and openp and openp > 0:
        lev = _leverage_value(snap)
        trend = investment * lev * (mark - openp) / openp * side
    total = None if trend is None else trend + grid
    created = snap.get("position_created") or snap.get("created_at")
    grid_apr = None
    total_apr = None
    days_open = None
    if investment > 0 and created:
        try:
            created_dt = datetime.strptime(created[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=TAIPEI)
            end = as_of or datetime.now(TAIPEI)
            days_open = max((end - created_dt).total_seconds() / 86400, 1 / 24)
            grid_apr = grid / investment * 365 / days_open * 100
            if total is not None:
                total_apr = total / investment * 365 / days_open * 100
        except ValueError:
            pass
    return {
        "trend_profit": money(to_fixed(trend) if trend is not None else None),
        "funding": money(to_fixed(funding)),
        "grid_profit": money(snap.get("grid_profit_i")),
        "total_pnl": money(to_fixed(total) if total is not None else None),
        "grid_annualized_pct": _pct(grid_apr),
        "annualized_pct": _pct(total_apr),
        "days_open": days_open,
    }


def _board_row(snap: dict, profit_24h_i: int | None, daily_i: int | None, size_i: int | None) -> dict:
    mark = _num(snap.get("mark_price_s"))
    if mark is None and snap.get("mark_price_i") is not None:
        mark = float(Decimal(snap["mark_price_i"]) / Decimal(SCALE))
    liq = _effective_liq(snap.get("trend"), from_fixed(snap.get("estimate_liq_down_i")), from_fixed(snap.get("estimate_liq_up_i")), from_fixed(snap.get("liquidation_price_i")))
    if liq is None:
        liq = _num(from_fixed(snap.get("liquidation_price_i")))
    liq, distance, _mode = normalize_liq(
        snap.get("trend"),
        mark,
        liq,
        coin_margined=snap.get("product") == "coin_margined_contract_grid",
    )
    size_i = snap.get("investment_i") if snap.get("investment_i") is not None else size_i
    profit_i = profit_24h_i if profit_24h_i is not None else daily_i
    pct = None
    if profit_i is not None and size_i:
        pct = float(Decimal(profit_i) / Decimal(size_i) * 100)
    pnl = _pnl_metrics(snap, mark)
    return {
        "bu_order_id": snap.get("bu_order_id"),
        "opened_at": snap.get("position_created") or snap.get("created_at"),
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
        "event": snap.get("event") or snap.get("status"),
        **pnl,
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
        month_fx = None
        if last is not None:
            month_prefix = last["capture_date"][:7]
            row = con.execute(
                "SELECT SUM(fx_gap_i) AS n FROM daily_summary WHERE capture_date LIKE ?",
                (month_prefix + "%",),
            ).fetchone()
            month_fx = money(row["n"]) if row and row["n"] is not None else money(0)
        return {
            "ok": last is not None,
            "last_success": dict(last) if last else None,
            "unresolved": unresolved,
            "missing_dates": missing,
            "recent_errors": [dict(row) for row in failed],
            "coin_fx_gap_month": month_fx,
            "coin_fx_gap_month_label": (last["capture_date"][:7] if last else None),
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
            item["true_profit"] = money(row["true_profit_i"] if row["true_profit_i"] is not None else row["cumulative_i"])
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
            return {
                "as_of": None,
                "window": "past_24h",
                "rows": [],
                "total_profit_24h": money(0),
                "profit_24h_pct": None,
                "position_count": 0,
                "fx": _fx_view(usdt_twd()),
            }
        snaps = _rows(
            con,
            """SELECT s.*, p.lifecycle, p.created_at AS position_created FROM grid_snapshots s
               JOIN grid_positions p ON p.bu_order_id=s.bu_order_id
               WHERE s.run_id=? AND s.list_status='running'
               ORDER BY s.symbol, s.created_at""",
            (last["id"],),
        )
        profits = {
            row["bu_order_id"]: row
            for row in con.execute("SELECT * FROM daily_grid_profit WHERE run_id=?", (last["id"],))
        }
        summary_row = con.execute("SELECT true_profit_i FROM daily_summary WHERE capture_date=?", (last["capture_date"],)).fetchone()
        summary_true = summary_row["true_profit_i"] if summary_row else None
        rows = []
        total_24h = 0
        total_investment = 0
        total_grid_profit = 0
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
            if snap.get("investment_i"):
                total_investment += snap["investment_i"]
            if snap.get("grid_profit_i"):
                total_grid_profit += snap["grid_profit_i"]
            rows.append(row)
        fx = usdt_twd()
        rate = fx.get("usdt_twd")
        rows = [attach_twd_tree(row, rate) for row in rows]
        return {
            "as_of": last["captured_at"],
            "capture_date": last["capture_date"],
            "window": "past_24h",
            "position_count": len(rows),
            "total_profit_24h": attach_twd(money(total_24h), rate),
            "total_investment": attach_twd(money(total_investment), rate),
            "total_grid_profit": attach_twd(money(total_grid_profit), rate),
            "true_grid_profit": attach_twd(money(summary_true), rate) if summary_true is not None else attach_twd(money(total_grid_profit), rate),
            "profit_24h_pct": _pct_value(total_24h, total_investment),
            "wallet_total": attach_twd({"usdt": last["wallet_total_usdt"]} if last["wallet_total_usdt"] else None, rate),
            "fx": _fx_view(fx),
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
                """SELECT s.*, p.lifecycle, p.created_at AS position_created, d.status, d.event, d.daily_profit_i, d.prev_grid_profit_i, d.cumulative_i
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
            item["true_profit"] = money(summary["true_profit_i"] if summary["true_profit_i"] is not None else summary["cumulative_i"])
            item["investment"] = money(summary["investment_i"])
            item["closed_count"] = summary["closed_count"]
            item["rows"] = rows
            days.append(item)
        fx = usdt_twd()
        rate = fx.get("usdt_twd")
        for item in days:
            item["daily_profit"] = attach_twd(item["daily_profit"], rate)
            item["cumulative"] = attach_twd(item["cumulative"], rate)
            item["true_profit"] = attach_twd(item["true_profit"], rate)
            item["investment"] = attach_twd(item["investment"], rate)
            item["rows"] = [attach_twd_tree(row, rate) for row in item["rows"]]
        return {"days": days, "fx": _fx_view(fx)}
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
            for key in list(row.keys()):
                if key.endswith("_i") and row[key] is not None:
                    row[key[:-2]] = money(row[key])
            row["investment"] = money(row.get("investment_i"))
            row["grid_profit"] = money(row.get("grid_profit_i"))
            row["margin_balance"] = money(row.get("margin_balance_i"))
        for row in profits:
            row["daily_profit"] = money(row["daily_profit_i"])
            row["grid_profit"] = money(row["grid_profit_i"])
            row["prev_grid_profit"] = money(row.get("prev_grid_profit_i"))
            row["cumulative"] = money(row.get("cumulative_i"))
            row["investment"] = money(row.get("investment_i"))
        latest = dict(snaps[-1]) if snaps else {}
        raw = {}
        if latest.get("raw_json"):
            try:
                raw = json.loads(latest["raw_json"])
            except json.JSONDecodeError:
                raw = {}
        order = raw.get("order") or {}
        api_data = order.get("buOrderData") or {}
        api_order = {k: v for k, v in order.items() if k != "buOrderData"}
        list_item = raw.get("list") or {}
        board = None
        if latest:
            board = _board_row(latest, latest.get("grid_profit_24h_i"), None, latest.get("investment_i"))
            fx = usdt_twd()
            board = attach_twd_tree(board, fx.get("usdt_twd"))
            board["fx"] = _fx_view(fx)
        snapshot_fields = []
        if latest:
            for key, value in latest.items():
                if key == "raw_json":
                    continue
                snapshot_fields.append({"key": key, "value": value})
        api_fields = [{"key": k, "value": api_data[k]} for k in sorted(api_data.keys())]
        order_fields = [{"key": k, "value": api_order[k]} for k in sorted(api_order.keys())]
        list_fields = [{"key": k, "value": list_item[k]} for k in sorted(list_item.keys())]
        return {
            "position": dict(pos) if pos else None,
            "snapshots": snaps,
            "profits": profits,
            "latest": latest,
            "board": board,
            "snapshot_fields": snapshot_fields,
            "api_fields": api_fields,
            "order_fields": order_fields,
            "list_fields": list_fields,
        }
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
        live_rate = (usdt_twd() or {}).get("usdt_twd")
        overview = [["日期", "當日總網格利潤 USDT", "當日總網格利潤 TWD", "當日投資倉位 USDT", "單日%", "累計總網格利潤 USDT", "累計總網格利潤 TWD", "真實網格總利潤 USDT", "真實網格總利潤 TWD", "活躍", "新增", "關倉", "錢包總市值 USDT", "錢包總市值 TWD", "USDT/TWD", "匯率來源"]]
        dates = []
        for row in con.execute(
            """SELECT d.*, r.fx_usdt_twd, r.fx_source, r.wallet_total_usdt
               FROM daily_summary d
               JOIN capture_runs r ON r.id=d.run_id
               ORDER BY d.capture_date"""
        ):
            rate = _rate_of(row, live_rate)
            wallet = row["wallet_total_usdt"]
            wallet_twd = None
            if wallet not in (None, "") and rate:
                wallet_twd = int((Decimal(str(wallet)) * Decimal(str(rate))).quantize(Decimal("1."), rounding=ROUND_HALF_UP))
            dates.append(row["capture_date"])
            overview.append([
                row["capture_date"],
                _excel_num(row["daily_profit_i"]),
                _excel_twd(row["daily_profit_i"], rate),
                _excel_num(row["investment_i"]),
                _excel_pct(row["daily_profit_i"], row["investment_i"]),
                _excel_num(row["cumulative_i"]),
                _excel_twd(row["cumulative_i"], rate),
                _excel_num(row["true_profit_i"] if row["true_profit_i"] is not None else row["cumulative_i"]),
                _excel_twd(row["true_profit_i"] if row["true_profit_i"] is not None else row["cumulative_i"], rate),
                row["active_count"],
                row["new_count"],
                row["closed_count"],
                None if wallet in (None, "") else float(wallet),
                wallet_twd,
                rate,
                row["fx_source"],
            ])
        detail = [["日期", "建倉日期", "標的", "網格ID", "狀態", "倉位大小 USDT", "當日利潤 USDT", "單日%", "累計網格利潤 USDT", "當次網格利潤 USDT"]]
        profit_map = {}
        positions = []
        seen = set()
        for row in con.execute(
            """SELECT d.*, p.symbol, p.created_at
               FROM daily_grid_profit d
               JOIN grid_positions p ON p.bu_order_id=d.bu_order_id
               ORDER BY d.capture_date, p.symbol, p.created_at"""
        ):
            detail.append([
                row["capture_date"],
                row["created_at"],
                row["symbol"],
                row["bu_order_id"],
                row["status"],
                _excel_num(row["investment_i"]),
                _excel_num(row["daily_profit_i"]),
                _excel_pct(row["daily_profit_i"], row["investment_i"]),
                _excel_num(row["cumulative_i"]),
                _excel_num(row["grid_profit_i"]),
            ])
            profit_map[(row["capture_date"], row["bu_order_id"])] = _excel_num(row["daily_profit_i"])
            key = row["bu_order_id"]
            if key not in seen:
                seen.add(key)
                positions.append((row["symbol"], key, row["created_at"]))
        matrix = [["標的", "網格ID", "建倉日期"] + dates]
        for symbol, oid, created in positions:
            matrix.append([symbol, oid, created] + [profit_map.get((day, oid)) for day in dates])
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
            "每日總覽": overview,
            "每日倉位明細": detail,
            "倉位×日期": matrix,
            "網格設定": settings,
            "執行紀錄": runs,
        }
    finally:
        con.close()


LEDGER_SCHEMA = 1
LEDGER_SOURCE = "pionex-grid-v2"
LEDGER_BASELINE_DATE = "2026-08-26"


def _twd_from_usdt(usdt, rate) -> int | None:
    if usdt in (None, "") or rate in (None, ""):
        return None
    try:
        return int((Decimal(str(usdt)) * Decimal(str(rate))).quantize(Decimal("1."), rounding=ROUND_HALF_UP))
    except (ArithmeticError, ValueError, TypeError):
        return None


def ledger_publish_payload(db_path: Path | None = None) -> dict:
    """Compact daily-profit JSON for Portfolio Tracker. Does not include raw API blobs."""
    con = connect(db_path)
    try:
        live = usdt_twd()
        live_rate = live.get("usdt_twd") if live else None
        summaries = list(
            con.execute(
                """SELECT d.*, r.captured_at, r.fx_usdt_twd, r.fx_source, r.fx_as_of, r.wallet_total_usdt
                   FROM daily_summary d
                   JOIN capture_runs r ON r.id=d.run_id
                   ORDER BY d.capture_date DESC"""
            )
        )
        if not summaries:
            return {
                "schema": LEDGER_SCHEMA,
                "source": LEDGER_SOURCE,
                "baseline_date": LEDGER_BASELINE_DATE,
                "available": False,
                "days": [],
            }
        first_date = min(row["capture_date"] for row in summaries)
        days = []
        for summary in summaries:
            rate = _rate_of(summary, live_rate)
            wallet_usdt = summary["wallet_total_usdt"]
            rows = []
            for row in con.execute(
                """SELECT d.*, p.symbol, p.created_at
                   FROM daily_grid_profit d
                   JOIN grid_positions p ON p.bu_order_id=d.bu_order_id
                   WHERE d.capture_date=?
                   ORDER BY p.symbol, p.created_at""",
                (summary["capture_date"],),
            ):
                rows.append(
                    {
                        "id": row["bu_order_id"],
                        "symbol": row["symbol"],
                        "status": row["status"],
                        "investment_usdt": from_fixed(row["investment_i"]),
                        "daily_profit_usdt": from_fixed(row["daily_profit_i"]),
                        "cumulative_usdt": from_fixed(row["cumulative_i"]),
                        "daily_profit_coin": from_fixed(row["daily_profit_coin_i"]) if "daily_profit_coin_i" in row.keys() else None,
                        "fx_gap_usdt": from_fixed(row["fx_gap_i"]) if "fx_gap_i" in row.keys() else None,
                    }
                )
            days.append(
                {
                    "date": summary["capture_date"],
                    "daily_profit_usdt": from_fixed(summary["daily_profit_i"]),
                    "daily_profit_twd": _excel_twd(summary["daily_profit_i"], rate),
                    "investment_usdt": from_fixed(summary["investment_i"]),
                    "pct": _excel_pct(summary["daily_profit_i"], summary["investment_i"]),
                    "cumulative_usdt": from_fixed(summary["cumulative_i"]),
                    "cumulative_twd": _excel_twd(summary["cumulative_i"], rate),
                    "true_profit_usdt": from_fixed(summary["true_profit_i"] if summary["true_profit_i"] is not None else summary["cumulative_i"]),
                    "true_profit_twd": _excel_twd(summary["true_profit_i"] if summary["true_profit_i"] is not None else summary["cumulative_i"], rate),
                    "fx_gap_usdt": from_fixed(summary["fx_gap_i"]) if "fx_gap_i" in summary.keys() else None,
                    "active_count": summary["active_count"],
                    "new_count": summary["new_count"],
                    "closed_count": summary["closed_count"],
                    "wallet_usdt": None if wallet_usdt in (None, "") else str(wallet_usdt),
                    "wallet_twd": _twd_from_usdt(wallet_usdt, rate),
                    "rows": rows,
                }
            )
        latest_row = summaries[0]
        latest_rate = _rate_of(latest_row, live_rate)
        latest_wallet = latest_row["wallet_total_usdt"]
        fx_source = latest_row["fx_source"]
        fx_as_of = latest_row["fx_as_of"]
        if latest_rate is None and live:
            latest_rate = live.get("usdt_twd")
            fx_source = live.get("source")
            fx_as_of = live.get("as_of")
        return {
            "schema": LEDGER_SCHEMA,
            "source": LEDGER_SOURCE,
            "baseline_date": first_date or LEDGER_BASELINE_DATE,
            "available": True,
            "captured_at": latest_row["captured_at"],
            "capture_date": latest_row["capture_date"],
            "fx": {
                "usdt_twd": latest_rate,
                "source": fx_source,
                "as_of": fx_as_of,
            },
            "wallet": {
                "usdt": None if latest_wallet in (None, "") else str(latest_wallet),
                "twd": _twd_from_usdt(latest_wallet, latest_rate),
            },
            "latest": {
                "daily_profit_usdt": from_fixed(latest_row["daily_profit_i"]),
                "daily_profit_twd": _excel_twd(latest_row["daily_profit_i"], latest_rate),
                "cumulative_usdt": from_fixed(latest_row["cumulative_i"]),
                "cumulative_twd": _excel_twd(latest_row["cumulative_i"], latest_rate),
                "investment_usdt": from_fixed(latest_row["investment_i"]),
                "active_count": latest_row["active_count"],
                "gap_days": latest_row["gap_days"],
            },
            "days": days,
        }
    finally:
        con.close()

