"""Run the Node collector and ingest into SQLite. Does not write v1 Excel."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from .store import DEFAULT_DB, CaptureError, ingest

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


def capture(credentials: Path, db_path: Path, replace_date: bool = False) -> dict:
    if not credentials.exists():
        raise CaptureError(f"Credential file not found: {credentials}")
    with tempfile.NamedTemporaryFile(prefix="pionex-v2-", suffix=".json", delete=False) as tmp:
        out = Path(tmp.name)
    try:
        cmd = [node_path(), str(API_JS), "--credentials", str(credentials), "--include-finished", "--output", str(out)]
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
        if proc.returncode != 0:
            raise CaptureError((proc.stderr or proc.stdout or "collector failed").strip())
        snapshot = json.loads(out.read_text(encoding="utf-8"))
        return ingest(snapshot, db_path=db_path, replace_date=replace_date)
    finally:
        out.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Capture Pionex grids into the v2 SQLite ledger")
    parser.add_argument("--credentials", default=str(V1_CREDENTIALS))
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--replace-date", action="store_true")
    args = parser.parse_args()
    try:
        result = capture(Path(args.credentials), Path(args.db), replace_date=args.replace_date)
    except CaptureError as exc:
        print(f"v2 capture failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
