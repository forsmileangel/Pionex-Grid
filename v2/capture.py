"""Run the Node collector and ingest into SQLite. Does not write v1 Excel."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from datetime import datetime
from zoneinfo import ZoneInfo

from .store import DEFAULT_DB, CaptureError, connect, ingest, vanished_positions

TAIPEI = ZoneInfo("Asia/Taipei")

ROOT = Path(__file__).resolve().parent.parent
V1_CREDENTIALS = Path(r"D:\My-project\pionex grid record\PIONEX API.txt")
API_JS = ROOT / "pionex-grid-api.mjs"
FINISHED_CACHE = ROOT / "v2-data" / "finished-details.json"
# Pionex rewrites this timestamp on closed bots without any money field changing.
_VOLATILE_RAW_KEYS = {"lastFundingFeeTs"}
# A daily detail capture takes about 20 seconds. Each request also has its own timeout
# in the collector; this one stops a stuck Node process, which would otherwise hold the
# 08:05 task (and the server's refresh lock) until someone killed it.
COLLECTOR_TIMEOUT_SECONDS = 300


def node_path() -> str:
    env = os.environ.get("PIONEX_NODE_PATH")
    if env and Path(env).exists():
        return env
    found = shutil.which("node")
    if found:
        # which() keeps the PATHEXT spelling, such as node.EXE.
        return str(Path(found).resolve())
    bundled = Path(r"C:\Users\Forever you\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe")
    if bundled.exists():
        return str(bundled)
    return "node"


def _collector_run_kwargs() -> dict:
    kwargs = {
        "capture_output": True,
        "text": True,
        "encoding": "utf-8",
        "stdin": subprocess.DEVNULL,
    }
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        info = subprocess.STARTUPINFO()
        info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        info.wShowWindow = 0
        kwargs["startupinfo"] = info
    return kwargs


def _stable_raw(value):
    if isinstance(value, dict):
        return {k: _stable_raw(v) for k, v in value.items() if k not in _VOLATILE_RAW_KEYS}
    if isinstance(value, list):
        return [_stable_raw(v) for v in value]
    return value


def _read_finished_cache(path: Path) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) and isinstance(data.get("records"), dict) else {}


def _zero_like(value) -> bool:
    if value is None or value == "" or value == [] or value == {}:
        return True
    try:
        return float(value) == 0
    except (TypeError, ValueError):
        return False


def _list_detail_check(snapshot: dict) -> dict:
    """On a detail capture, find fields the running list leaves empty while the detail has a value.

    The fast live refresh builds running bots from the list, so such a field would be lost there.
    A trade between the two reads only changes values, so it is not counted.
    """
    checked = 0
    missing = []
    for rec in snapshot.get("Records") or []:
        if rec.get("ListStatus") != "running":
            continue
        raw = rec.get("RawJson") or {}
        listed = (raw.get("list") or {}).get("buOrderData")
        detail = (raw.get("order") or {}).get("buOrderData")
        if not isinstance(listed, dict) or not isinstance(detail, dict):
            continue
        checked += 1
        for key, value in detail.items():
            if listed.get(key) in (None, "") and not _zero_like(value):
                missing.append(f"{rec.get('Symbol')}:{key}")
    return {"checked": checked, "missing": len(missing), "fields": missing[:20]}


def _list_mode_allowed(cache: Path) -> bool:
    """Fast mode stays on until a daily detail capture finds a field the list leaves out."""
    check = _read_finished_cache(cache).get("last_list_check")
    return not (isinstance(check, dict) and (check.get("missing") or 0) > 0)


def _update_finished_cache(snapshot: dict, path: Path, full: bool, list_check: dict | None = None) -> dict:
    """Remember the detail of closed bots fetched in this capture.

    Never raises: losing the cache only means the next live refresh fetches every closed bot.
    A full capture also reports how many closed bots differ from the remembered copy.
    """
    try:
        data = _read_finished_cache(path)
        records = dict(data.get("records") or {})
        stamp = snapshot.get("CapturedAt")
        fetched = reused = checked = changed = 0
        for rec in snapshot.get("Records") or []:
            oid = str(rec.get("ApiOrderId") or "")
            if rec.get("ListStatus") != "finished" or not oid:
                continue
            if rec.get("DetailSource") == "cache":
                reused += 1
                continue
            fetched += 1
            old = (records.get(oid) or {}).get("record")
            if isinstance(old, dict):
                checked += 1
                if _stable_raw(old.get("RawJson")) != _stable_raw(rec.get("RawJson")):
                    changed += 1
            records[oid] = {"fetched_at": stamp, "record": rec}
        out = {
            "version": 1,
            "records": records,
            "last_full_check": data.get("last_full_check"),
            "last_reuse": data.get("last_reuse"),
            "last_list_check": data.get("last_list_check"),
        }
        if list_check is not None:
            out["last_list_check"] = {"at": stamp, **list_check}
        if full:
            out["last_full_check"] = {"at": stamp, "fetched": fetched, "checked": checked, "changed": changed}
        else:
            out["last_reuse"] = {"at": stamp, "fetched": fetched, "reused": reused, "changed": changed}
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, prefix=path.name + ".",
                                             suffix=".tmp", delete=False) as tmp:
                tmp_path = Path(tmp.name)
                tmp.write(json.dumps(out, ensure_ascii=False))
            os.replace(tmp_path, path)
        finally:
            if tmp_path:
                tmp_path.unlink(missing_ok=True)
        return {"ok": True, "records": len(records), "fetched": fetched, "reused": reused, "changed": changed}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def finished_cache_status(path: Path | None = None) -> dict:
    data = _read_finished_cache(Path(path or FINISHED_CACHE))
    if not data:
        return {"available": False}
    return {
        "available": True,
        "count": len(data["records"]),
        "last_full_check": data.get("last_full_check"),
        "last_reuse": data.get("last_reuse"),
        "last_list_check": data.get("last_list_check"),
    }


def collect_snapshot(credentials: Path, reuse_finished: bool = False, cache_path: Path | None = None,
                     running_from_list: bool = False) -> dict:
    """Run the collector. reuse_finished lets it take settled closed bots from the local cache;
    the daily capture leaves it off so every closed bot is fetched and re-checked once a day.
    running_from_list builds running bots from the list (fast live mode); the daily detail capture
    checks the list against the details and turns fast mode off if the list ever leaves a field out."""
    if not credentials.exists():
        raise CaptureError(f"Credential file not found: {credentials}")
    cache = Path(cache_path or FINISHED_CACHE)
    with tempfile.NamedTemporaryFile(prefix="pionex-v2-", suffix=".json", delete=False) as tmp:
        out = Path(tmp.name)
    try:
        cmd = [node_path(), str(API_JS), "--credentials", str(credentials), "--include-finished", "--output", str(out)]
        if reuse_finished and cache.exists():
            cmd += ["--finished-cache", str(cache)]
        if running_from_list and _list_mode_allowed(cache):
            cmd += ["--running-from-list"]
        try:
            proc = subprocess.run(cmd, timeout=COLLECTOR_TIMEOUT_SECONDS, **_collector_run_kwargs())
        except subprocess.TimeoutExpired:
            raise CaptureError(f"Collector did not finish within {COLLECTOR_TIMEOUT_SECONDS} seconds; stopped")
        if proc.returncode != 0:
            raise CaptureError((proc.stderr or proc.stdout or "collector failed").strip())
        snapshot = json.loads(out.read_text(encoding="utf-8"))
    finally:
        out.unlink(missing_ok=True)
    list_check = None if snapshot.get("RunningSource") == "list" else _list_detail_check(snapshot)
    snapshot["FinishedCache"] = _update_finished_cache(snapshot, cache, full=not reuse_finished, list_check=list_check)
    return snapshot


def record_intraday(snapshot: dict, source: str, db_path: Path | None = None, intraday_db: Path | None = None) -> dict:
    """Keep every capture as an intraday point. Must never break the ledger or Gist path."""
    try:
        from .intraday import record_snapshot
        return record_snapshot(snapshot, source, db_path=intraday_db, ledger_db=db_path)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def capture_live(credentials: Path, dest: Path | None = None, skip_publish: bool = False,
                 source: str = "live", db_path: Path | None = None, intraday_db: Path | None = None) -> dict:
    from .live import save_live_snapshot, snapshot_to_board

    snapshot = collect_snapshot(credentials, reuse_finished=True, running_from_list=True)
    save_live_snapshot(snapshot, dest)
    board = snapshot_to_board(snapshot)
    result = {
        "ok": True,
        "kind": "live",
        "as_of": snapshot.get("CapturedAt"),
        "position_count": board["position_count"],
        "board": board,
        "intraday": record_intraday(snapshot, source, db_path or DEFAULT_DB, intraday_db),
        "finished_cache": snapshot.get("FinishedCache"),
    }
    if skip_publish:
        result["publish"] = {"ok": False, "skipped": True, "reason": "skip-publish"}
        return result
    try:
        from .gist_publish import publish_live
        result["publish"] = publish_live(board)
    except Exception as exc:
        result["publish"] = {"ok": False, "error": str(exc)}
    return result


def _today_captured_at(db_path: Path, now: datetime) -> datetime | None:
    if not Path(db_path).exists():
        return None
    con = connect(db_path)
    try:
        row = con.execute("""SELECT r.captured_at FROM daily_summary d JOIN capture_runs r ON r.id=d.run_id
                             WHERE d.capture_date=?""", (now.date().isoformat(),)).fetchone()
    finally:
        con.close()
    return datetime.fromisoformat(row["captured_at"]) if row else None


def capture(credentials: Path, db_path: Path, replace_date: bool = False, skip_publish: bool = False,
            allow_missing: bool = False) -> dict:
    if not replace_date:
        now = datetime.now(TAIPEI)
        existing = _today_captured_at(db_path, now)
        cutoff = now.replace(hour=8, minute=5, second=0, microsecond=0)
        if existing is not None and existing >= cutoff:
            # A second run the same day would move today's cut-off from 08:05 to now,
            # stretching today and shrinking tomorrow without any trace. Only an
            # explicit --replace-date does that.
            reason = f"today's daily record ({existing:%H:%M}) already exists; use --replace-date to replace it"
            return {"skipped": True, "reason": reason, "publish": {"ok": False, "skipped": True, "reason": reason}}
        # A record taken before 08:05 is replaced so the day still ends at the 08:05 cut-off.
        replace_date = existing is not None
    snapshot = collect_snapshot(credentials)
    missing = vanished_positions(snapshot, db_path)
    if missing and not allow_missing:
        raise CaptureError(
            f"Pionex lists are missing {len(missing)} open position(s) ({', '.join(missing)}); nothing was written. "
            "Run again later; if they are really gone, rerun with --allow-missing")
    intraday = record_intraday(snapshot, "daily", db_path)
    result = ingest(snapshot, db_path=db_path, replace_date=replace_date)
    result["intraday"] = intraday
    result["finished_cache"] = snapshot.get("FinishedCache")
    if skip_publish:
        result["publish"] = {"ok": False, "skipped": True, "reason": "skip-publish"}
        return result
    try:
        from .gist_publish import publish_ledger
        result["publish"] = publish_ledger(db_path)
    except Exception as exc:
        result["publish"] = {"ok": False, "error": str(exc)}
    return result


def daily_capture_due(db_path: Path, now: datetime | None = None) -> tuple[bool, str]:
    """For the logon catch-up: run only when today's 08:05 capture is missing."""
    now = now or datetime.now(TAIPEI)
    if (now.hour, now.minute) < (8, 5):
        return False, "before 08:05; the scheduled capture will run"
    if Path(db_path).exists():
        con = connect(db_path)
        try:
            if con.execute("SELECT 1 FROM daily_summary WHERE capture_date=?", (now.date().isoformat(),)).fetchone():
                return False, "today's daily record already exists"
        finally:
            con.close()
    return True, "today's daily record is missing"


def main() -> int:
    parser = argparse.ArgumentParser(description="Capture Pionex grids into the SQLite ledger")
    parser.add_argument("--credentials", default=str(V1_CREDENTIALS))
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--replace-date", action="store_true")
    parser.add_argument("--allow-missing", action="store_true",
                        help="Ingest even when an open position is in neither Pionex list (marks it closed)")
    parser.add_argument("--skip-publish", action="store_true", help="Do not PATCH Gist after ingest")
    parser.add_argument("--live", action="store_true", help="Fetch current positions only; do not write daily sqlite")
    parser.add_argument("--if-missing", action="store_true",
                        help="Capture only if today's daily record is missing and it is past 08:05 (logon catch-up)")
    args = parser.parse_args()
    if args.if_missing and not args.live:
        due, reason = daily_capture_due(Path(args.db))
        if not due:
            print(json.dumps({"ok": True, "skipped": True, "reason": reason}, ensure_ascii=False))
            return 0
    try:
        if args.live:
            result = capture_live(Path(args.credentials), skip_publish=args.skip_publish, db_path=Path(args.db))
            ledger_retry = None
            if not args.skip_publish:
                # The 30-minute task also retries a ledger publish that failed earlier,
                # as a separate PATCH that carries only the ledger file.
                try:
                    from .gist_publish import republish_ledger_if_dirty
                    ledger_retry = republish_ledger_if_dirty(Path(args.db))
                except Exception as exc:
                    ledger_retry = {"ok": False, "error": str(exc)}
            print(json.dumps({
                "ok": True,
                "kind": "live",
                "as_of": result.get("as_of"),
                "position_count": result.get("position_count"),
                "intraday": result.get("intraday"),
                "finished_cache": result.get("finished_cache"),
                "publish": result.get("publish"),
                "ledger_retry": ledger_retry,
            }, ensure_ascii=False, indent=2))
            pub = result.get("publish") or {}
            if pub.get("skipped"):
                print(f"v2 live gist publish skipped: {pub.get('reason')}", file=sys.stderr)
            elif not pub.get("ok"):
                print(f"v2 live gist publish failed: {pub.get('error') or 'unknown'}", file=sys.stderr)
            return 0
        result = capture(
            Path(args.credentials),
            Path(args.db),
            replace_date=args.replace_date,
            skip_publish=args.skip_publish,
            allow_missing=args.allow_missing,
        )
    except CaptureError as exc:
        print(f"v2 capture failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result.get("skipped"):
        print(f"v2 capture skipped: {result['reason']}", file=sys.stderr)
        return 0
    pub = result.get("publish") or {}
    if pub.get("skipped"):
        print(f"v2 gist publish skipped: {pub.get('reason')}", file=sys.stderr)
    elif not pub.get("ok"):
        print(f"v2 gist publish failed: {pub.get('error') or 'unknown'}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
