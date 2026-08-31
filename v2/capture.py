"""Run the Node collector and ingest into SQLite. Does not write v1 Excel."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from datetime import datetime
from zoneinfo import ZoneInfo

from .store import DEFAULT_DB, CaptureError, connect, ingest

TAIPEI = ZoneInfo("Asia/Taipei")

ROOT = Path(__file__).resolve().parent.parent
V1_CREDENTIALS = Path(r"D:\My-project\pionex grid record\PIONEX API.txt")
API_JS = ROOT / "pionex-grid-api.mjs"


def node_path() -> str:
    env = os.environ.get("PIONEX_NODE_PATH")
    if env and Path(env).exists():
        return env
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


def collect_snapshot(credentials: Path) -> dict:
    if not credentials.exists():
        raise CaptureError(f"Credential file not found: {credentials}")
    with tempfile.NamedTemporaryFile(prefix="pionex-v2-", suffix=".json", delete=False) as tmp:
        out = Path(tmp.name)
    try:
        cmd = [node_path(), str(API_JS), "--credentials", str(credentials), "--include-finished", "--output", str(out)]
        proc = subprocess.run(cmd, **_collector_run_kwargs())
        if proc.returncode != 0:
            raise CaptureError((proc.stderr or proc.stdout or "collector failed").strip())
        return json.loads(out.read_text(encoding="utf-8"))
    finally:
        out.unlink(missing_ok=True)


def capture_live(credentials: Path, dest: Path | None = None, skip_publish: bool = False) -> dict:
    from .live import save_live_snapshot, snapshot_to_board

    snapshot = collect_snapshot(credentials)
    save_live_snapshot(snapshot, dest)
    board = snapshot_to_board(snapshot)
    result = {
        "ok": True,
        "kind": "live",
        "as_of": snapshot.get("CapturedAt"),
        "position_count": board["position_count"],
        "board": board,
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


def capture(credentials: Path, db_path: Path, replace_date: bool = False, skip_publish: bool = False) -> dict:
    if not replace_date and Path(db_path).exists():
        con = connect(db_path)
        try:
            today = datetime.now(TAIPEI).date().isoformat()
            if con.execute("SELECT 1 FROM daily_summary WHERE capture_date=?", (today,)).fetchone():
                replace_date = True
        finally:
            con.close()
    snapshot = collect_snapshot(credentials)
    result = ingest(snapshot, db_path=db_path, replace_date=replace_date)
    if skip_publish:
        result["publish"] = {"ok": False, "skipped": True, "reason": "skip-publish"}
        return result
    try:
        from .gist_publish import publish_ledger
        result["publish"] = publish_ledger(db_path)
    except Exception as exc:
        result["publish"] = {"ok": False, "error": str(exc)}
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Capture Pionex grids into the SQLite ledger")
    parser.add_argument("--credentials", default=str(V1_CREDENTIALS))
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--replace-date", action="store_true")
    parser.add_argument("--skip-publish", action="store_true", help="Do not PATCH Gist after ingest")
    parser.add_argument("--live", action="store_true", help="Fetch current positions only; do not write daily sqlite")
    args = parser.parse_args()
    try:
        if args.live:
            result = capture_live(Path(args.credentials), skip_publish=args.skip_publish)
            print(json.dumps({
                "ok": True,
                "kind": "live",
                "as_of": result.get("as_of"),
                "position_count": result.get("position_count"),
                "publish": result.get("publish"),
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
        )
    except CaptureError as exc:
        print(f"v2 capture failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    pub = result.get("publish") or {}
    if pub.get("skipped"):
        print(f"v2 gist publish skipped: {pub.get('reason')}", file=sys.stderr)
    elif not pub.get("ok"):
        print(f"v2 gist publish failed: {pub.get('error') or 'unknown'}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
