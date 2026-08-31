"""Publish compact JSON files to a private Gist.

Never writes portfolio-tracker-holdings.json. Missing credentials or HTTP
errors are returned as a status dict — callers must not fail capture.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path

from .store import DEFAULT_DB, ledger_publish_payload

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CREDS = ROOT / "GIST PUBLISH.txt"
LEDGER_FILENAME = "pionex-grid-ledger.json"
LIVE_FILENAME = "pionex-grid-live.json"
LIVE_SCHEMA = 1
LIVE_SOURCE = "pionex-grid-v2-live"
HOLDINGS_FILENAME = "portfolio-tracker-holdings.json"
USER_AGENT = "pionex-grid-v2-publish"


def read_publish_credentials(path: Path | None = None) -> tuple[str, str] | None:
    file_path = Path(path or DEFAULT_CREDS)
    if not file_path.exists():
        return None
    lines = [
        line.strip()
        for line in file_path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    if len(lines) < 2:
        return None
    gist_id, token = lines[0], lines[1]
    if not gist_id or not token:
        return None
    return gist_id, token


def _usdt_of(money) -> str | None:
    if money is None:
        return None
    if isinstance(money, dict):
        value = money.get("usdt")
        return None if value in (None, "") else str(value)
    return str(money)


def live_publish_payload(board: dict) -> dict:
    rows = []
    for row in board.get("rows") or []:
        rows.append(
            {
                "symbol": row.get("symbol"),
                "leverage": row.get("leverage"),
                "trend": row.get("trend"),
                "investment_usdt": _usdt_of(row.get("investment") or row.get("size")),
                "grid_profit_usdt": _usdt_of(row.get("grid_profit")),
                "profit_24h_usdt": _usdt_of(row.get("profit_24h")),
                "mark_price": row.get("mark_price"),
                "liq_price": row.get("liq_price"),
            }
        )
    wallet = board.get("wallet_total") or {}
    return {
        "schema": LIVE_SCHEMA,
        "source": LIVE_SOURCE,
        "captured_at": board.get("as_of"),
        "position_count": board.get("position_count") or 0,
        "wallet": {
            "usdt": None if wallet.get("usdt") in (None, "") else str(wallet.get("usdt")),
            "twd": wallet.get("twd"),
        },
        "total_profit_24h_usdt": _usdt_of(board.get("total_profit_24h")),
        "total_investment_usdt": _usdt_of(board.get("total_investment")),
        "total_grid_profit_usdt": _usdt_of(board.get("total_grid_profit")),
        "profit_24h_pct": board.get("profit_24h_pct"),
        "rows": rows,
    }


def patch_gist_file(filename: str, payload: dict, creds_path: Path | None = None, extra: dict | None = None) -> dict:
    if filename in (HOLDINGS_FILENAME,):
        return {"ok": False, "error": "refusing to patch holdings file"}
    creds = read_publish_credentials(creds_path)
    if not creds:
        return {"ok": False, "skipped": True, "reason": "missing GIST PUBLISH.txt"}
    gist_id, token = creds
    body = json.dumps(
        {"files": {filename: {"content": json.dumps(payload, ensure_ascii=False)}}},
        ensure_ascii=False,
    ).encode("utf-8")
    req = urllib.request.Request(
        f"https://api.github.com/gists/{gist_id}",
        data=body,
        method="PATCH",
        headers={
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github.v3+json",
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": USER_AGENT,
            "Cache-Control": "no-store",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as res:
            data = json.loads(res.read().decode("utf-8"))
            result = {
                "ok": True,
                "filename": filename,
                "updated_at": data.get("updated_at"),
            }
            if extra:
                result.update(extra)
            return result
    except urllib.error.HTTPError as exc:
        status = exc.code
        msg = f"HTTP {status}"
        try:
            err_body = json.loads(exc.read().decode("utf-8"))
            if err_body.get("message"):
                msg = str(err_body["message"])
        except Exception:
            pass
        return {"ok": False, "status": status, "error": msg}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def patch_ledger_payload(payload: dict, creds_path: Path | None = None) -> dict:
    return patch_gist_file(
        LEDGER_FILENAME,
        payload,
        creds_path=creds_path,
        extra={"capture_date": payload.get("capture_date"), "days": len(payload.get("days") or [])},
    )


def publish_ledger(db_path: Path | None = None, creds_path: Path | None = None) -> dict:
    if not read_publish_credentials(creds_path):
        return {"ok": False, "skipped": True, "reason": "missing GIST PUBLISH.txt"}
    payload = ledger_publish_payload(db_path or DEFAULT_DB)
    if not payload.get("days"):
        return {"ok": False, "skipped": True, "reason": "empty ledger"}
    return patch_ledger_payload(payload, creds_path=creds_path)


def publish_live(board: dict, creds_path: Path | None = None) -> dict:
    if not read_publish_credentials(creds_path):
        return {"ok": False, "skipped": True, "reason": "missing GIST PUBLISH.txt"}
    if not board or not board.get("available"):
        return {"ok": False, "skipped": True, "reason": "empty live board"}
    payload = live_publish_payload(board)
    return patch_gist_file(
        LIVE_FILENAME,
        payload,
        creds_path=creds_path,
        extra={"captured_at": payload.get("captured_at"), "position_count": payload.get("position_count")},
    )
