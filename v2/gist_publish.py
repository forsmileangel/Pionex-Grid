"""Publish compact JSON files to a private Gist.

Never writes portfolio-tracker-holdings.json. Missing credentials or HTTP
errors are returned as a status dict — callers must not fail capture.
"""

from __future__ import annotations

import hashlib
import json
import urllib.error
import urllib.request
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from .store import DEFAULT_DB, TAIPEI, ledger_publish_payload, taipei_now

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CREDS = ROOT / "GIST PUBLISH.txt"
LEDGER_FILENAME = "pionex-grid-ledger.json"
LIVE_FILENAME = "pionex-grid-live.json"
LIVE_SCHEMA = 1
LIVE_SOURCE = "pionex-grid-v2-live"
HOLDINGS_FILENAME = "portfolio-tracker-holdings.json"
USER_AGENT = "pionex-grid-v2-publish"
STATE_FILENAME = "publish-state.json"
# One line per PATCH with GitHub's remaining hourly quota. The quota (5,000/hour) is shared
# by every token and app on the account, so this shows when something else drains it.
RATE_LOG_FILENAME = "gist-rate.log"
RATE_LOG_MAX_BYTES = 1_000_000


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


def _sum_usdt(values) -> str | None:
    total = None
    for value in values:
        if value not in (None, ""):
            total = (total or Decimal(0)) + Decimal(str(value))
    return None if total is None else str(total)


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
                # Added for v15.974.
                "equivalent_usdt": _usdt_of(row.get("equivalent")),
                "extra_margin_usdt": _usdt_of(row.get("extra_margin")),
                "equivalent_leverage": row.get("equivalent_leverage"),
                "funding_total_usdt": _usdt_of(row.get("funding_total")),
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
        "total_equivalent_usdt": _usdt_of(board.get("total_equivalent")),
        "leverage_nominal": board.get("leverage_nominal"),
        "leverage_equivalent": board.get("leverage_equivalent"),
        "total_grid_profit_usdt": _usdt_of(board.get("total_grid_profit")),
        "profit_24h_pct": board.get("profit_24h_pct"),
        "total_funding_usdt": _sum_usdt(r["funding_total_usdt"] for r in rows),
        "rows": rows,
    }


def _state_path(creds_path: Path | None = None) -> Path:
    # Follows the credential file so tests with temporary credentials never touch v2-data.
    return (DEFAULT_DB.parent if creds_path is None else Path(creds_path).parent) / STATE_FILENAME


def _material(value):
    """Drop TWD/FX views: they follow the bank rate, not the ledger itself."""
    if isinstance(value, dict):
        return {k: _material(v) for k, v in value.items()
                if k not in ("fx", "twd", "corrections") and not k.endswith("_twd")}
    if isinstance(value, list):
        return [_material(v) for v in value]
    return value


def content_hash(payload: dict) -> str:
    text = json.dumps(_material(payload), ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def read_publish_state(creds_path: Path | None = None) -> dict:
    try:
        data = json.loads(_state_path(creds_path).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _record_state(filename: str, payload: dict, result: dict, creds_path: Path | None) -> None:
    if result.get("skipped"):
        return
    state = read_publish_state(creds_path)
    entry = dict(state.get(filename) or {})
    entry.update(last_attempt_at=taipei_now().isoformat(), ok=bool(result.get("ok")),
                 error=None if result.get("ok") else str(result.get("error") or "unknown"))
    if result.get("ok"):
        entry.update(last_success_at=entry["last_attempt_at"], hash=content_hash(payload))
    if "rate_remaining" in result:
        entry.update(rate_remaining=result.get("rate_remaining"), rate_reset=result.get("rate_reset"))
    state[filename] = entry
    path = _state_path(creds_path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass  # bookkeeping only; never fail a publish because of it


def ledger_dirty(db_path: Path | None = None, creds_path: Path | None = None) -> bool:
    """True when local ledger content differs from the last successful Gist publish."""
    payload = ledger_publish_payload(db_path or DEFAULT_DB)
    if not payload.get("days"):
        return False
    last = (read_publish_state(creds_path).get(LEDGER_FILENAME) or {}).get("hash")
    return last != content_hash(payload)


def patch_gist_file(filename: str, payload: dict, creds_path: Path | None = None, extra: dict | None = None) -> dict:
    result = _patch_gist_file(filename, payload, creds_path, extra)
    _record_state(filename, payload, result, creds_path)
    _log_rate(filename, result, creds_path)
    return result


def _rate_info(status: int | None, headers, message: str = "") -> dict:
    """GitHub's remaining hourly quota from the response headers, and whether this was a rate limit."""
    headers = headers or {}
    remaining = str(headers.get("X-RateLimit-Remaining") or "").strip()
    reset = str(headers.get("X-RateLimit-Reset") or "").strip()
    info = {
        "rate_remaining": int(remaining) if remaining.isdigit() else None,
        "rate_reset": datetime.fromtimestamp(int(reset), TAIPEI).isoformat() if reset.isdigit() else None,
    }
    if status in (403, 429) and (remaining == "0" or str(headers.get("Retry-After") or "").strip()
                                 or "rate limit" in (message or "").lower()):
        info["rate_limited"] = True
    return info


def _log_rate(filename: str, result: dict, creds_path: Path | None) -> None:
    if result.get("skipped") or "rate_remaining" not in result:
        return
    remaining, reset = result.get("rate_remaining"), result.get("rate_reset")
    until = reset[11:16] if reset else "?"
    if result.get("rate_limited"):
        text = f"rate-limited until {until}"
    else:
        text = f"rate {'?' if remaining is None else remaining}/5000 reset {until}"
    status = "ok" if result.get("ok") else f"fail {result.get('status') or ''}".strip()
    line = f"{taipei_now().isoformat(timespec='seconds')} {filename} {status} {text}\n"
    path = _state_path(creds_path).parent / RATE_LOG_FILENAME
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > RATE_LOG_MAX_BYTES:
            kept = path.read_text(encoding="utf-8").splitlines(keepends=True)
            path.write_text("".join(kept[len(kept) // 2:]), encoding="utf-8")
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line)
    except OSError:
        pass  # logging only; never fail a publish because of it


def _patch_gist_file(filename: str, payload: dict, creds_path: Path | None = None, extra: dict | None = None) -> dict:
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
                **_rate_info(getattr(res, "status", 200), getattr(res, "headers", None)),
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
        # No retry here: a failed ledger publish is retried by the next 30-minute live task.
        return {"ok": False, "status": status, "error": msg, **_rate_info(status, getattr(exc, "headers", None), msg)}
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


def republish_ledger_if_dirty(db_path: Path | None = None, creds_path: Path | None = None) -> dict:
    """Retry a ledger publish that failed earlier. PATCHes the ledger file only."""
    if not read_publish_credentials(creds_path):
        return {"ok": False, "skipped": True, "reason": "missing GIST PUBLISH.txt"}
    if not ledger_dirty(db_path, creds_path):
        return {"ok": True, "skipped": True, "reason": "ledger already in sync"}
    return publish_ledger(db_path, creds_path)
