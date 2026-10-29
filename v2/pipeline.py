"""Capture the daily ledger, then export both Excel workbooks.

Excel failures are logged and still exit 0. Task Scheduler must not rerun a
capture that already succeeded.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

from .capture import DAILY_CUTOFF, TAIPEI, V1_CREDENTIALS, _collector_run_kwargs, capture, daily_capture_due
from .fileio import LockTimeout, append_line, file_lock
from .daily_report import DEFAULT_OUTPUT, export_report
from .legacy_excel import ledger_rows
from .paths import LEGACY_ROOT, PRODUCTION_REPO
from .store import DEFAULT_DB

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "v2-data"
STATE_PATH = DATA_DIR / "pipeline-state.json"
PREVIEW_STATE_PATH = DATA_DIR / "preview" / "pipeline-state.json"
LOG_PATH = DATA_DIR / "pipeline.log"
LOG_MAX_BYTES = 1_000_000
# The legacy Excel script ships with this checkout (the v1 folder has an identical copy). It
# takes its folders from parameters and defaults, not from where the file sits.
LEGACY_PS1 = ROOT / "pionex-grid-daily.ps1"
PRODUCTION_LEGACY_ROOT = LEGACY_ROOT
PRODUCTION_HISTORY = PRODUCTION_LEGACY_ROOT / "data" / "pionex-grid-history.xlsx"
# A normal export takes about 10 seconds. The scheduled task has no time limit and
# ignores a new start while one is running, so a hung Excel would block the next
# day's capture. The old 08:12 task stopped after an hour.
LEGACY_TIMEOUT_SECONDS = 600
# The legacy script copies the workbook to data/snapshot-legacy-excel-*.xlsx before every
# export; keep the newest ones only.
LEGACY_BACKUP_PATTERN = "snapshot-legacy-excel-*.xlsx"
LEGACY_BACKUP_KEEP = 14
# The 30-minute live task retries exports only after the daily task's own retries are over.
CATCH_UP_AFTER = timedelta(minutes=45)


def _stamp() -> str:
    now = datetime.now(TAIPEI)
    raw = now.strftime("%z") or "+0800"
    zone = raw[:3] + ":" + raw[3:] if len(raw) == 5 else raw
    return now.strftime("%Y-%m-%d %H:%M:%S ") + zone


def _log(step: str, message: str) -> None:
    append_line(LOG_PATH, f"{_stamp()} {step} {message}", LOG_MAX_BYTES)


def _latest_date(db_path: Path) -> str | None:
    if not db_path.exists():
        return None
    uri = db_path.resolve().as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=10)
    try:
        row = connection.execute("SELECT MAX(capture_date) FROM daily_summary").fetchone()
    finally:
        connection.close()
    if not row or not row[0]:
        return None
    return str(row[0])


def _load_state(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        _log("pipeline", f"fail 狀態檔無法讀取：{exc}")
        return {}
    return data if isinstance(data, dict) else {}


def _save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _needs_export(state: dict, key: str, latest: str) -> bool:
    previous = state.get(key)
    return not isinstance(previous, str) or latest > previous


def _outside_production() -> bool:
    return ROOT.resolve() != PRODUCTION_REPO.resolve()


def _same(left: Path, right: Path) -> bool:
    return left.resolve() == right.resolve()


def _legacy_targets(legacy_root: Path | None, legacy_workbook: Path | None) -> tuple[Path | None, Path | None]:
    if legacy_root is None and legacy_workbook is None:
        return None, None
    root = legacy_root
    workbook = legacy_workbook
    if root is None and workbook is not None:
        parent = workbook.parent
        root = parent.parent if parent.name.lower() == "data" else parent
    if workbook is None and root is not None:
        workbook = root / "data" / "pionex-grid-history.xlsx"
    return root, workbook


def _legacy_command(db_path: Path, root: Path | None, workbook: Path | None) -> list[str]:
    command = [
        "powershell.exe",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(LEGACY_PS1),
        "-V2Root",
        str(ROOT),
        "-DatabasePath",
        str(db_path),
    ]
    if root is not None and workbook is not None:
        command += ["-RootDir", str(root), "-WorkbookPath", str(workbook)]
    return command


def _blocked_legacy(root: Path | None, workbook: Path | None) -> bool:
    if not _outside_production():
        return False
    if root is None or workbook is None:
        return True
    return _same(root, PRODUCTION_LEGACY_ROOT) or _same(workbook, PRODUCTION_HISTORY)


def _blocked_report(output: Path) -> bool:
    return _outside_production() and _same(output, DEFAULT_OUTPUT)


def _note_publish(result: dict) -> None:
    publish = result.get("publish") or {}
    if publish.get("skipped"):
        print(f"v2 gist publish skipped: {publish.get('reason')}", file=sys.stderr)
    elif not publish.get("ok"):
        print(f"v2 gist publish failed: {publish.get('error') or 'unknown'}", file=sys.stderr)


def _step_capture(db_path: Path, if_missing: bool, skip_capture: bool) -> str:
    _log("capture", "start")
    if skip_capture:
        _log("capture", "skip --skip-capture")
        return "skipped"
    if if_missing:
        due, reason = daily_capture_due(db_path)
        if not due:
            _log("capture", f"skip {reason}")
            return "skipped"
    try:
        result = capture(V1_CREDENTIALS, db_path)
    except Exception as exc:
        _log("capture", f"fail {exc}")
        print(f"擷取失敗：{exc}", file=sys.stderr)
        return "failed"
    if result.get("skipped"):
        _log("capture", f"skip {result['reason']}")
        return "skipped"
    if result.get("backup_error"):
        _log("capture", f"warn {result['backup_error']}")
    _note_publish(result)
    _log("capture", "ok")
    return "ok"


def _refuse(step: str, label: str) -> str:
    message = "開發目錄拒絕寫入正式 Excel"
    _log(step, f"fail {message}")
    print(f"{label}：{message}", file=sys.stderr)
    return "failed"


def _prune_legacy_backups(workbook: Path | None) -> int:
    folder = (workbook or PRODUCTION_HISTORY).parent
    backups = sorted(folder.glob(LEGACY_BACKUP_PATTERN), key=lambda f: f.name, reverse=True)
    removed = 0
    for old in backups[LEGACY_BACKUP_KEEP:]:
        try:
            old.unlink()
            removed += 1
        except OSError:
            pass
    return removed


def _automation_excel_pids() -> set[int]:
    """EXCEL.EXE started through COM (an export), never a workbook the user opened."""
    try:
        listed = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process -Filter \"Name='EXCEL.EXE'\" | "
             "Where-Object { $_.CommandLine -match '/automation' } | ForEach-Object { $_.ProcessId }"],
            timeout=30, **{**_collector_run_kwargs(), "errors": "replace"},
        )
        return {int(x) for x in (listed.stdout or "").split() if x.isdigit()}
    except Exception:
        return set()


def _stop_new_excel(before: set[int]) -> list[int]:
    """Stopping powershell on a timeout leaves the COM Excel it started running."""
    stopped = []
    for pid in sorted(_automation_excel_pids() - before):
        try:
            subprocess.run(["taskkill", "/F", "/PID", str(pid)], timeout=30,
                           **{**_collector_run_kwargs(), "errors": "replace"})
            stopped.append(pid)
        except Exception:
            pass
    return stopped


def _step_legacy(db_path: Path, state_file: Path, state: dict, latest: str | None,
                 legacy_root: Path | None, legacy_workbook: Path | None) -> str:
    _log("legacy_excel", "start")
    root, workbook = _legacy_targets(legacy_root, legacy_workbook)
    if _blocked_legacy(root, workbook):
        return _refuse("legacy_excel", "舊版 Excel 匯出失敗")
    if not latest:
        _log("legacy_excel", "skip 帳本沒有日期")
        return "skipped"
    if not _needs_export(state, "legacy_excel", latest):
        _log("legacy_excel", "skip 已是最新，略過")
        return "skipped"
    try:
        # The script aborts on the first stderr line of the adapter, so its error is
        # only "Traceback". Run the adapter's read-only checks here for the reason.
        ledger_rows(db_path, datetime.now(TAIPEI).date())
        excel_before = _automation_excel_pids()
        # Same no-window flags as the collector: the scheduled task runs hidden,
        # and a plain child powershell.exe would otherwise flash a console.
        completed = subprocess.run(
            _legacy_command(db_path, root, workbook),
            timeout=LEGACY_TIMEOUT_SECONDS,
            **{**_collector_run_kwargs(), "errors": "replace"},
        )
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "excel failed").strip().splitlines()
            raise RuntimeError(detail[0][:300] if detail else f"exit {completed.returncode}")
    except subprocess.TimeoutExpired:
        message = f"超過 {LEGACY_TIMEOUT_SECONDS} 秒沒有結束，已中止"
        stopped = _stop_new_excel(excel_before)
        if stopped:
            message += f"，並關閉殘留的 Excel（pid {', '.join(map(str, stopped))}）"
        _log("legacy_excel", f"fail {message}")
        print(f"舊版 Excel 匯出失敗：{message}", file=sys.stderr)
        return "failed"
    except Exception as exc:
        _log("legacy_excel", f"fail {exc}")
        print(f"舊版 Excel 匯出失敗：{exc}", file=sys.stderr)
        return "failed"
    state["legacy_excel"] = latest
    _save_state(state_file, state)
    removed = _prune_legacy_backups(workbook)
    _log("legacy_excel", f"ok {latest}" + (f"（刪除 {removed} 份舊備份）" if removed else ""))
    return "ok"


def _step_report(db_path: Path, state_file: Path, state: dict, latest: str | None,
                 report_output: Path | None) -> str:
    _log("daily_report", "start")
    output = report_output or DEFAULT_OUTPUT
    if _blocked_report(output):
        return _refuse("daily_report", "每日報表匯出失敗")
    if not latest:
        _log("daily_report", "skip 帳本沒有日期")
        return "skipped"
    if not _needs_export(state, "daily_report", latest):
        _log("daily_report", "skip 已是最新，略過")
        return "skipped"
    try:
        export_report(db_path, output, datetime.now(TAIPEI).date(), allow_stale=False, archive=False)
    except Exception as exc:
        _log("daily_report", f"fail {exc}")
        print(f"每日報表匯出失敗：{exc}", file=sys.stderr)
        return "failed"
    state["daily_report"] = latest
    _save_state(state_file, state)
    _log("daily_report", f"ok {latest}")
    return "ok"


def run(args: argparse.Namespace) -> int:
    preview = bool(args.legacy_root or args.legacy_workbook or args.report_output)
    state_file = PREVIEW_STATE_PATH if preview else STATE_PATH
    capture_status = _step_capture(args.db, args.if_missing, args.skip_capture)
    if capture_status == "failed":
        print(json.dumps({"ok": False, "capture": "failed"}, ensure_ascii=False))
        return 1
    if args.skip_excel:
        _log("legacy_excel", "start")
        _log("legacy_excel", "skip --skip-excel")
        _log("daily_report", "start")
        _log("daily_report", "skip --skip-excel")
        print(json.dumps({
            "ok": True,
            "capture": capture_status,
            "legacy_excel": "skipped",
            "daily_report": "skipped",
        }, ensure_ascii=False))
        return 0
    try:
        latest = _latest_date(args.db)
    except sqlite3.Error as exc:
        _log("legacy_excel", f"fail {exc}")
        _log("daily_report", f"fail {exc}")
        print(f"Excel 匯出失敗：{exc}", file=sys.stderr)
        print(json.dumps({
            "ok": True,
            "capture": capture_status,
            "legacy_excel": "failed",
            "daily_report": "failed",
        }, ensure_ascii=False))
        return 0
    try:
        # The 30-minute catch-up uses the same lock, so two exports never open the workbook at once.
        with file_lock(state_file.parent / "excel-export", timeout=LEGACY_TIMEOUT_SECONDS + 60):
            state = _load_state(state_file)
            legacy_status = _step_legacy(
                args.db, state_file, state, latest, args.legacy_root, args.legacy_workbook)
            report_status = _step_report(args.db, state_file, state, latest, args.report_output)
    except LockTimeout as exc:
        _log("legacy_excel", f"fail {exc}")
        _log("daily_report", f"fail {exc}")
        legacy_status = report_status = "failed"
    print(json.dumps({
        "ok": True,
        "capture": capture_status,
        "legacy_excel": legacy_status,
        "daily_report": report_status,
        "latest_date": latest,
    }, ensure_ascii=False))
    return 0


def catch_up_exports(db_path: Path = DEFAULT_DB, now: datetime | None = None) -> dict:
    """Export a workbook the daily run could not write, e.g. because it was open in Excel.

    Called by the 30-minute live task. Production folder only, today's ledger only, and
    never while the daily task may still be retrying.
    """
    now = now or datetime.now(TAIPEI)
    if _outside_production():
        return {"skipped": True, "reason": "not the production folder"}
    cutoff = now.replace(hour=DAILY_CUTOFF[0], minute=DAILY_CUTOFF[1], second=0, microsecond=0)
    if now < cutoff + CATCH_UP_AFTER:
        return {"skipped": True, "reason": "the daily task may still be running"}
    latest = _latest_date(db_path)
    if latest != now.date().isoformat():
        return {"skipped": True, "reason": "today's ledger is not ready"}
    try:
        with file_lock(STATE_PATH.parent / "excel-export", timeout=0):
            state = _load_state(STATE_PATH)
            result = {}
            if _needs_export(state, "legacy_excel", latest):
                result["legacy_excel"] = _step_legacy(db_path, STATE_PATH, state, latest, None, None)
            if _needs_export(state, "daily_report", latest):
                result["daily_report"] = _step_report(db_path, STATE_PATH, state, latest, None)
    except LockTimeout:
        return {"skipped": True, "reason": "another export is running"}
    return result or {"skipped": True, "reason": "exports are current"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Capture the daily ledger, then export both Excel workbooks")
    parser.add_argument("--if-missing", action="store_true",
                        help="Capture only when today's daily record is missing; exports still run")
    parser.add_argument("--skip-capture", action="store_true", help="Do not call the Pionex API")
    parser.add_argument("--skip-excel", action="store_true", help="Do not export either workbook")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--legacy-root", type=Path)
    parser.add_argument("--legacy-workbook", type=Path)
    parser.add_argument("--report-output", type=Path)
    return run(parser.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
