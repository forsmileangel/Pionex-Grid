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
from datetime import datetime
from pathlib import Path

from .capture import TAIPEI, V1_CREDENTIALS, _collector_run_kwargs, capture, daily_capture_due
from .daily_report import DEFAULT_OUTPUT, export_report
from .store import DEFAULT_DB

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "v2-data"
STATE_PATH = DATA_DIR / "pipeline-state.json"
PREVIEW_STATE_PATH = DATA_DIR / "preview" / "pipeline-state.json"
LOG_PATH = DATA_DIR / "pipeline.log"
LEGACY_PS1 = Path(r"D:\My-project\pionex grid record\pionex-grid-daily.ps1")
PRODUCTION_REPO = Path(r"D:\My-project\pionex grid record-v2")
PRODUCTION_LEGACY_ROOT = Path(r"D:\My-project\pionex grid record")
PRODUCTION_HISTORY = PRODUCTION_LEGACY_ROOT / "data" / "pionex-grid-history.xlsx"


def _stamp() -> str:
    now = datetime.now(TAIPEI)
    raw = now.strftime("%z") or "+0800"
    zone = raw[:3] + ":" + raw[3:] if len(raw) == 5 else raw
    return now.strftime("%Y-%m-%d %H:%M:%S ") + zone


def _log(step: str, message: str) -> None:
    path = LOG_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"{_stamp()} {step} {message}\n")


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
    _note_publish(result)
    _log("capture", "ok")
    return "ok"


def _refuse(step: str, label: str) -> str:
    message = "開發目錄拒絕寫入正式 Excel"
    _log(step, f"fail {message}")
    print(f"{label}：{message}", file=sys.stderr)
    return "failed"


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
        # Same no-window flags as the collector: the scheduled task runs hidden,
        # and a plain child powershell.exe would otherwise flash a console.
        completed = subprocess.run(
            _legacy_command(db_path, root, workbook),
            **{**_collector_run_kwargs(), "errors": "replace"},
        )
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "excel failed").strip().splitlines()
            raise RuntimeError(detail[0][:300] if detail else f"exit {completed.returncode}")
    except Exception as exc:
        _log("legacy_excel", f"fail {exc}")
        print(f"舊版 Excel 匯出失敗：{exc}", file=sys.stderr)
        return "failed"
    state["legacy_excel"] = latest
    _save_state(state_file, state)
    _log("legacy_excel", f"ok {latest}")
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
    state = _load_state(state_file)
    legacy_status = _step_legacy(
        args.db, state_file, state, latest, args.legacy_root, args.legacy_workbook)
    report_status = _step_report(args.db, state_file, state, latest, args.report_output)
    print(json.dumps({
        "ok": True,
        "capture": capture_status,
        "legacy_excel": legacy_status,
        "daily_report": report_status,
        "latest_date": latest,
    }, ensure_ascii=False))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Capture the daily ledger, then export both Excel workbooks")
    parser.add_argument("--if-missing", action="store_true",
                        help="Capture only when today's 08:05 record is missing; exports still run")
    parser.add_argument("--skip-capture", action="store_true", help="Do not call the Pionex API")
    parser.add_argument("--skip-excel", action="store_true", help="Do not export either workbook")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--legacy-root", type=Path)
    parser.add_argument("--legacy-workbook", type=Path)
    parser.add_argument("--report-output", type=Path)
    return run(parser.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
