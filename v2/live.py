"""Last one-click live board. Does not write the daily sqlite ledger."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from threading import Lock
from zoneinfo import ZoneInfo

from .fx import attach_twd, attach_twd_tree, usdt_twd
from .store import (
    CaptureError,
    _board_row,
    _fx_view,
    _pct_value,
    ingest,
    money,
    taipei_now,
    to_fixed,
)

TAIPEI = ZoneInfo("Asia/Taipei")
ROOT = Path(__file__).resolve().parent.parent
LIVE_PATH = ROOT / "v2-data" / "live-snapshot.json"
REFRESH_LOCK = Lock()


def live_path() -> Path:
    return LIVE_PATH


def save_live_snapshot(snapshot: dict, path: Path | None = None) -> Path:
    dest = Path(path or LIVE_PATH)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
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
        "investment_i": to_fixed(rec.get("Investment")),
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


def snapshot_to_board(snapshot: dict) -> dict:
    records = list(snapshot.get("Records") or [])
    running = [r for r in records if r.get("ListStatus", "running") == "running"]
    fx = usdt_twd()
    rate = fx.get("usdt_twd")
    rows = []
    total_24h = 0
    total_investment = 0
    total_grid = 0
    for rec in running:
        snap = record_to_snap(rec)
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
    return {
        "available": True,
        "kind": "live",
        "as_of": captured,
        "capture_date": None,
        "window": "past_24h",
        "position_count": len(rows),
        "total_profit_24h": attach_twd(money(total_24h), rate),
        "total_investment": attach_twd(money(total_investment), rate),
        "total_grid_profit": attach_twd(money(total_grid), rate),
        "true_grid_profit": attach_twd(money(total_grid), rate),
        "profit_24h_pct": _pct_value(total_24h, total_investment),
        "wallet_total": attach_twd({"usdt": wallet} if wallet else None, rate),
        "fx": _fx_view(fx),
        "rows": rows,
    }


def live_payload(path: Path | None = None) -> dict:
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
    return snapshot_to_board(snapshot)


def commit_live_to_daily(confirm_date: str, db_path: Path, live_file: Path | None = None) -> dict:
    today = datetime.now(TAIPEI).date().isoformat()
    if not confirm_date or confirm_date != today:
        raise CaptureError(f"confirm_date must be Taipei today ({today})")
    snapshot = load_live_snapshot(live_file)
    if snapshot is None:
        raise CaptureError("No live snapshot to write")
    try:
        if not isinstance(snapshot.get("CapturedAt"), str) or not snapshot["CapturedAt"]:
            raise ValueError("missing timestamp")
        captured = taipei_now(snapshot["CapturedAt"])
    except (TypeError, ValueError):
        raise CaptureError("即時快照缺少有效擷取時間，請先更新即時資料")
    if captured.date().isoformat() != today:
        raise CaptureError("即時快照不是台北今日資料，請先更新即時資料")
    result = ingest(snapshot, db_path=db_path, replace_date=True)
    try:
        from .gist_publish import publish_ledger
        result["publish"] = publish_ledger(db_path)
    except Exception as exc:
        result["publish"] = {"ok": False, "error": str(exc)}
    return result
