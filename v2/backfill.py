"""Fill a missed daily capture from the 30-minute intraday record of that day.

Picks the first intraday snapshot at or after 08:05 Taipei on the missing date.
Dry run by default; --apply writes the ledger, then republishes the Gist ledger.

    python -m v2.backfill --date 2026-10-04
    python -m v2.backfill --date 2026-10-04 --apply
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

from .intraday import default_intraday_db
from .store import DEFAULT_DB, CaptureError, from_fixed, insert_missing_day

DAILY_TIME = "08:05"


def pick_intraday_run(date: str, intraday_db: Path, at: str | None = None) -> tuple[dict, list[dict]]:
    floor = at or f"{date}T{DAILY_TIME}"
    with closing(sqlite3.connect(f"file:{intraday_db}?mode=ro", uri=True)) as con:
        con.row_factory = sqlite3.Row
        run = con.execute(
            """SELECT * FROM intraday_runs WHERE captured_at >= ? AND substr(captured_at, 1, 10) = ?
               ORDER BY captured_at LIMIT 1""", (floor, date)).fetchone()
        if run is None:
            raise CaptureError(f"No intraday record on {date} at or after {floor[11:16]}")
        points = [dict(r) for r in con.execute("SELECT * FROM intraday_points WHERE run_id=?", (run["id"],))]
    return dict(run), points


def main() -> int:
    parser = argparse.ArgumentParser(description="Fill a missed daily capture from intraday records")
    parser.add_argument("--date", required=True, help="Missing Taipei date, YYYY-MM-DD")
    parser.add_argument("--at", help="Use the first intraday record at or after this ISO time instead of 08:05")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--intraday-db", default="")
    parser.add_argument("--apply", action="store_true", help="Write the ledger (default is a dry run)")
    parser.add_argument("--skip-publish", action="store_true")
    args = parser.parse_args()
    db = Path(args.db)
    intraday_db = Path(args.intraday_db) if args.intraday_db else default_intraday_db(db)
    try:
        run, points = pick_intraday_run(args.date, intraday_db, args.at)
        preview = {
            "date": args.date,
            "source_record": run["captured_at"],
            "positions": len(points),
            "grid_profit_total": from_fixed(sum(p["grid_profit_i"] or 0 for p in points)),
        }
        if not args.apply:
            print(json.dumps({"dry_run": True, **preview}, ensure_ascii=False, indent=2))
            return 0
        source = f"intraday {run['source']} {run['captured_at'][11:16]}"
        result = insert_missing_day(run["captured_at"], points, source, db)
    except CaptureError as exc:
        print(f"backfill failed: {exc}", file=sys.stderr)
        return 1
    publish = {"ok": False, "skipped": True, "reason": "skip-publish"}
    if not args.skip_publish:
        try:
            from .gist_publish import publish_ledger
            publish = publish_ledger(db)
        except Exception as exc:
            publish = {"ok": False, "error": str(exc)}
    print(json.dumps({**preview, **result, "publish": publish}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
