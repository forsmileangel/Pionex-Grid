"""Shadow daily capture at 08:15, saved beside the ledger and never written into it.

Pionex settles funding at 08:00 Taipei. At 08:05 some positions have not posted
it yet, so it lands in the next day's record. This keeps an 08:15 snapshot per
day so a week of data shows whether a later daily capture would catch it.
No ledger, intraday, Gist or Excel writes; closed-bot details are cached in
v2-data/shadow/, not in the shared cache the live and daily captures use.

    python -m v2.shadow            capture (08:10-08:30 only) into v2-data/shadow/
    python -m v2.shadow --report   compare each saved day with that day's ledger capture
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from contextlib import closing
from datetime import datetime
from pathlib import Path

from .capture import TAIPEI, V1_CREDENTIALS, collect_snapshot
from .store import DEFAULT_DB, SCALE, CaptureError

ROOT = Path(__file__).resolve().parent.parent
SHADOW_DIR = ROOT / "v2-data" / "shadow"
FINISHED_CACHE_NAME = "finished-details.json"
# A start after the PC was off would not measure 08:15, so skip it.
WINDOW = ((8, 10), (8, 30))


def capture_shadow(credentials: Path, now: datetime | None = None, force: bool = False) -> dict:
    now = now or datetime.now(TAIPEI)
    if not force and not (WINDOW[0] <= (now.hour, now.minute) <= WINDOW[1]):
        return {"ok": True, "skipped": True, "reason": f"outside 08:10-08:30 ({now:%H:%M})"}
    SHADOW_DIR.mkdir(parents=True, exist_ok=True)
    # Same collector call as the 30-minute live capture, with its own closed-bot cache:
    # the shared one is rewritten by the live and daily captures around the same time.
    snapshot = collect_snapshot(credentials, reuse_finished=True, running_from_list=True,
                                cache_path=SHADOW_DIR / FINISHED_CACHE_NAME)
    path = SHADOW_DIR / f"{now.date().isoformat()}-{now:%H%M}.json"
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)
    return {"ok": True, "path": str(path), "captured_at": snapshot.get("CapturedAt")}


def _last_settlement(raw) -> int | None:
    """Epoch seconds of the last funding settlement (Pionex sends seconds)."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw or "{}")
        except json.JSONDecodeError:
            return None
    raw = raw if isinstance(raw, dict) else {}
    for part in ("order", "list"):
        value = ((raw.get(part) or {}).get("buOrderData") or {}).get("lastFundingFeeTs")
        if value:
            value = int(value)
            return value // 1000 if value > 10**11 else value
    return None


def compare_day(official: dict, shadow: dict, settle: int) -> dict:
    """official/shadow map position id -> (last settlement epoch s, cumulative funding USDT)."""
    shared = [oid for oid in official if oid in shadow and official[oid][0] and shadow[oid][0]]
    late = [oid for oid in shared if official[oid][0] < settle <= shadow[oid][0]]
    return {
        "positions": len(shared),
        "official_posted": sum(official[oid][0] >= settle for oid in shared),
        "shadow_posted": sum(shadow[oid][0] >= settle for oid in shared),
        "late": len(late),
        "late_funding": round(sum(shadow[oid][1] - official[oid][1] for oid in late), 2),
    }


def _official(con, day: str) -> tuple[str | None, dict]:
    rows = con.execute(
        """SELECT r.captured_at, s.bu_order_id, s.funding_fee_i, s.raw_json
           FROM daily_summary d JOIN capture_runs r ON r.id=d.run_id
           JOIN grid_snapshots s ON s.run_id=r.id
           WHERE d.capture_date=? AND s.list_status='running'""", (day,)).fetchall()
    captured = rows[0][0] if rows else None
    return captured, {str(oid): (_last_settlement(raw), (fee or 0) / SCALE) for _, oid, fee, raw in rows}


def _shadow(path: Path) -> tuple[str | None, dict]:
    snapshot = json.loads(path.read_text(encoding="utf-8"))
    records = [r for r in snapshot.get("Records", []) if r.get("ListStatus") == "running"]
    return snapshot.get("CapturedAt"), {
        str(r.get("ApiOrderId")): (_last_settlement(r.get("RawJson")), float(r.get("FundingFee") or 0))
        for r in records}


def _clock(stamp: str | None) -> str:
    return datetime.fromisoformat(stamp).astimezone(TAIPEI).strftime("%H:%M") if stamp else "--:--"


def report(db_path: Path) -> list[dict]:
    rows = []
    with closing(sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True)) as con:
        for path in sorted(SHADOW_DIR.glob("????-??-??-????.json")):
            day = path.name[:10]
            settle = int(datetime.fromisoformat(f"{day}T08:00:00").replace(tzinfo=TAIPEI).timestamp())
            official_at, official = _official(con, day)
            shadow_at, shadow = _shadow(path)
            rows.append({"date": day, "official_at": _clock(official_at), "shadow_at": _clock(shadow_at),
                         **compare_day(official, shadow, settle)})
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Shadow 08:15 capture to measure late 08:00 funding")
    parser.add_argument("--report", action="store_true", help="Compare saved 08:15 snapshots with the ledger")
    parser.add_argument("--force", action="store_true", help="Capture outside 08:10-08:30 (testing only)")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    args = parser.parse_args(argv)
    if args.report:
        rows = report(args.db)
        if not rows:
            print("v2-data/shadow 還沒有 08:15 快照")
        for r in rows:
            print(f"{r['date']}  {r['official_at']} 已入帳 {r['official_posted']}/{r['positions']}  "
                  f"{r['shadow_at']} 已入帳 {r['shadow_posted']}/{r['positions']}  "
                  f"晚到 {r['late']} 倉 {r['late_funding']:+.2f} U")
        return 0
    try:
        result = capture_shadow(V1_CREDENTIALS, force=args.force)
    except CaptureError as exc:
        print(f"影子擷取失敗：{exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
