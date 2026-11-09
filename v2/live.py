"""Last one-click live board. Does not write the daily sqlite ledger."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from threading import Lock
from zoneinfo import ZoneInfo

from .fileio import atomic_write_text, file_lock
from .fx import attach_twd, attach_twd_tree, usdt_twd
from .range_stats import funding_between, parse_time
from .store import (
    CaptureError,
    _board_row,
    _fx_view,
    _pct_value,
    ingest,
    position_totals,
    money,
    taipei_now,
    to_fixed,
    vanished_positions,
)

TAIPEI = ZoneInfo("Asia/Taipei")
ROOT = Path(__file__).resolve().parent.parent
LIVE_PATH = ROOT / "v2-data" / "live-snapshot.json"
REFRESH_LOCK = Lock()


def live_path() -> Path:
    return LIVE_PATH


def save_live_snapshot(snapshot: dict, path: Path | None = None) -> Path:
    # The live task and the dashboard both write this file; "write today" reads it.
    dest = Path(path or LIVE_PATH)
    with file_lock(dest):
        atomic_write_text(dest, json.dumps(snapshot, ensure_ascii=False))
    return dest


def load_live_snapshot(path: Path | None = None) -> dict | None:
    file_path = Path(path or LIVE_PATH)
    if not file_path.exists():
        return None
    try:
        data = json.loads(file_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    return data


def record_to_snap(rec: dict) -> dict:
    return {
        "bu_order_id": rec.get("ApiOrderId"),
        "symbol": rec.get("Symbol"),
        "position_created": rec.get("Created"),
        "created_at": rec.get("Created"),
        "leverage": rec.get("Leverage"),
        "trend": rec.get("Trend"),
        "product": rec.get("Product"),
        "raw_json": rec.get("RawJson"),
        "investment_i": to_fixed(rec.get("Investment")),
        "extra_margin_i": to_fixed(rec.get("ExtraMargin")),
        "conversion_price_i": to_fixed(rec.get("ConversionPrice")),
        "grid_profit_i": to_fixed(rec.get("GridProfit")),
        "grid_profit_24h_i": to_fixed(rec.get("GridProfit24h")),
        "mark_price_s": None if rec.get("MarkPrice") is None else str(rec.get("MarkPrice")),
        "open_price_s": None if rec.get("OpenPrice") is None else str(rec.get("OpenPrice")),
        "position_open_price_s": None if rec.get("PositionOpenPrice") is None else str(rec.get("PositionOpenPrice")),
        "liquidation_price_i": to_fixed(rec.get("LiqPrice")),
        "estimate_liq_down_i": to_fixed(rec.get("EstimateLiqDown")),
        "estimate_liq_up_i": to_fixed(rec.get("EstimateLiqUp")),
        "funding_fee_i": to_fixed(rec.get("FundingFee")),
        "list_status": rec.get("ListStatus"),
        "lifecycle": "active" if rec.get("ListStatus", "running") == "running" else "closed",
    }


def snapshot_to_board(snapshot: dict, ledger_db: Path | None = None, intraday_db: Path | None = None) -> dict:
    records = list(snapshot.get("Records") or [])
    running = [r for r in records if r.get("ListStatus", "running") == "running"]
    fx = usdt_twd()
    rate = fx.get("usdt_twd")
    rows = []
    snaps = []
    total_24h = 0
    total_investment = 0
    total_grid = 0
    for rec in running:
        snap = record_to_snap(rec)
        snaps.append(snap)
        row = _board_row(snap, snap.get("grid_profit_24h_i"), None, snap.get("investment_i"))
        if snap.get("grid_profit_24h_i"):
            total_24h += snap["grid_profit_24h_i"]
        if snap.get("investment_i"):
            total_investment += snap["investment_i"]
        if snap.get("grid_profit_i"):
            total_grid += snap["grid_profit_i"]
        rows.append(row)
    captured = snapshot.get("CapturedAt")
    wallet = (snapshot.get("Wallet") or {}).get("totalInUsdt")
    rows = [attach_twd_tree(row, rate) for row in rows]
    totals = position_totals(snaps)
    funding_fields = {"total_funding_24h": None, "total_net_24h": None,
                      "net_24h_pct": None, "funding_24h_missing": len(rows),
                      "funding_24h_pending": 0, "funding_24h_note": None}
    try:
        end = parse_time(captured)
        funding = funding_between([s["bu_order_id"] for s in snaps], end - timedelta(hours=24), end,
                                  ledger_db, intraday_db, allow_pending=True)
        values, notes = [], []
        for row, snap in zip(rows, snaps):
            value, note = funding.get(snap["bu_order_id"], (None, None))
            if value is not None and note:
                notes.append(note)
            profit = snap.get("grid_profit_24h_i")
            net = None if value is None or profit is None else profit + value
            row["funding_24h"] = None if value is None else attach_twd(money(value), rate)
            row["net_24h"] = None if net is None else attach_twd(money(net), rate)
            values.append(value)
        missing = sum(v is None for v in values)
        total_funding = None if missing else sum(values)
        total_net = None if total_funding is None else total_24h + total_funding
        funding_fields = {
            "total_funding_24h": None if total_funding is None else attach_twd(money(total_funding), rate),
            "total_net_24h": None if total_net is None else attach_twd(money(total_net), rate),
            "net_24h_pct": _pct_value(total_net, totals["equivalent_i"] or total_investment),
            "funding_24h_missing": missing,
            # A settlement Pionex has not posted yet (a few minutes after 00:00/08:00/16:00) is left out, and said so.
            "funding_24h_pending": len(notes),
            "funding_24h_note": f"{len(notes)} 倉 {notes[0]}" if notes else None,
        }
    except Exception:
        # Funding is optional: an unavailable/corrupt database must not take down the live board.
        for row in rows:
            row["funding_24h"] = row["net_24h"] = None
    return {
        "available": True,
        "kind": "live",
        "as_of": captured,
        "capture_date": None,
        "window": "past_24h",
        "position_count": len(rows),
        "total_profit_24h": attach_twd(money(total_24h), rate),
        **funding_fields,
        "total_investment": attach_twd(money(total_investment), rate),
        "total_equivalent": attach_twd(money(totals["equivalent_i"]), rate),
        "leverage_nominal": totals["leverage_nominal"],
        "leverage_equivalent": totals["leverage_equivalent"],
        "total_grid_profit": attach_twd(money(total_grid), rate),
        "true_grid_profit": attach_twd(money(total_grid), rate),
        # Percent on the equivalent position, so moving margin to extra margin does not inflate it.
        "profit_24h_pct": _pct_value(total_24h, totals["equivalent_i"] or total_investment),
        "wallet_total": attach_twd({"usdt": wallet} if wallet else None, rate),
        "fx": _fx_view(fx),
        "rows": rows,
    }


def live_payload(path: Path | None = None, ledger_db: Path | None = None, intraday_db: Path | None = None) -> dict:
    snapshot = load_live_snapshot(path)
    if snapshot is None:
        fx = usdt_twd()
        return {
            "available": False,
            "kind": "live",
            "as_of": None,
            "rows": [],
            "position_count": 0,
            "fx": _fx_view(fx),
        }
    return snapshot_to_board(snapshot, ledger_db, intraday_db)


def commit_live_to_daily(confirm_date: str, db_path: Path, live_file: Path | None = None) -> dict:
    today = datetime.now(TAIPEI).date().isoformat()
    if not confirm_date or confirm_date != today:
        raise CaptureError(f"confirm_date must be Taipei today ({today})")
    snapshot = load_live_snapshot(live_file)
    if snapshot is None:
        raise CaptureError("No live snapshot to write")
    if snapshot.get("RunningSource") == "list":
        raise CaptureError("即時快照是快速模式，寫入倉位歷史前必須先完整重抓")
    try:
        if not isinstance(snapshot.get("CapturedAt"), str) or not snapshot["CapturedAt"]:
            raise ValueError("missing timestamp")
        captured = taipei_now(snapshot["CapturedAt"])
    except (TypeError, ValueError):
        raise CaptureError("即時快照缺少有效擷取時間，請先更新即時資料")
    if captured.date().isoformat() != today:
        raise CaptureError("即時快照不是台北今日資料，請先更新即時資料")
    missing = vanished_positions(snapshot, db_path)
    if missing:
        raise CaptureError(f"即時快照少了 {len(missing)} 個進行中的倉位（{', '.join(missing)}），請先更新即時資料")
    # Recompute only today (and any closing day a settlement changed); 08:15 rebuilds everything.
    result = ingest(snapshot, db_path=db_path, replace_date=True, full_rebuild=False)
    try:
        from .gist_publish import publish_ledger
        result["publish"] = publish_ledger(db_path)
    except Exception as exc:
        result["publish"] = {"ok": False, "error": str(exc)}
    return result
