"""Publish the compact ledger JSON to a private Gist second file.

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


def patch_ledger_payload(payload: dict, creds_path: Path | None = None) -> dict:
    creds = read_publish_credentials(creds_path)
    if not creds:
        return {"ok": False, "skipped": True, "reason": "missing GIST PUBLISH.txt"}
    gist_id, token = creds
    body = json.dumps(
        {"files": {LEDGER_FILENAME: {"content": json.dumps(payload, ensure_ascii=False)}}},
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
            return {
                "ok": True,
                "filename": LEDGER_FILENAME,
                "capture_date": payload.get("capture_date"),
                "updated_at": data.get("updated_at"),
                "days": len(payload.get("days") or []),
            }
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


def publish_ledger(db_path: Path | None = None, creds_path: Path | None = None) -> dict:
    if not read_publish_credentials(creds_path):
        return {"ok": False, "skipped": True, "reason": "missing GIST PUBLISH.txt"}
    payload = ledger_publish_payload(db_path or DEFAULT_DB)
    if not payload.get("days"):
        return {"ok": False, "skipped": True, "reason": "empty ledger"}
    return patch_ledger_payload(payload, creds_path=creds_path)
