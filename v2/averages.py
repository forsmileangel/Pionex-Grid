"""Daily-profit averages for the two calendars: the month so far, and the last 30 days.

One day is one daily record, the previous 08:15 to this 08:15. A record after a missed capture
covers 1 + gap_days days, so the average divides by the days it really spans (the same rule as
healthcheck's avg_daily_30). The ledger's first day is a baseline whose 0 is a definition, not a
measured day, and a day with a pending closure is provisional; both are left out, and the
pending ones are counted in "skipped" so the page can say so.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

RECENT_DAYS = 30


def _bucket(keys: tuple[str, ...]) -> dict:
    return {"days": 0, "skipped": 0, "from": None, "to": None, "sums": {key: 0 for key in keys}}


def _add(bucket: dict, entry: dict, keys: tuple[str, ...]) -> None:
    if entry["baseline"]:
        return
    if entry["pending"]:
        bucket["skipped"] += 1
        return
    bucket["days"] += entry["covered"]
    bucket["from"] = bucket["from"] or entry["date"]
    bucket["to"] = entry["date"]
    for key in keys:
        bucket["sums"][key] += entry.get(key) or 0


def profit_averages(entries: list[dict], keys: tuple[str, ...], recent_days: int = RECENT_DAYS) -> dict:
    """Sum each key per month and over the last recent_days days.

    entries are oldest first: {"date", "covered", "baseline", "pending", <key>: int | None}.
    Returns {"months": {"YYYY-MM": bucket}, "recent": bucket}; a bucket holds the days counted,
    the pending days left out, the first and last date counted, and the integer sum per key.
    A missing amount adds 0; for funding that is right, because the next day's change covers it.
    """
    months: dict[str, dict] = {}
    recent = _bucket(keys)
    if entries:
        since = (date.fromisoformat(entries[-1]["date"]) - timedelta(days=recent_days)).isoformat()
        for entry in entries:
            _add(months.setdefault(entry["date"][:7], _bucket(keys)), entry, keys)
            if entry["date"] > since:
                _add(recent, entry, keys)
    return {"months": months, "recent": recent}


def average_i(total: int, days: int) -> int | None:
    """Integer-unit daily average, or None when no day was counted."""
    return int(round(Decimal(total) / days)) if days else None


def bucket_view(bucket: dict, money_view) -> dict:
    """The bucket with {key: {"total", "avg"}}; money_view turns integer units into a money view."""
    days = bucket["days"]
    view = {"days": days, "skipped": bucket["skipped"], "from": bucket["from"], "to": bucket["to"]}
    for key, total in bucket["sums"].items():
        view[key] = {"total": money_view(total if days else None), "avg": money_view(average_i(total, days))}
    return view


def averages_view(result: dict, money_view) -> dict:
    return {
        "months": {key: bucket_view(bucket, money_view) for key, bucket in result["months"].items()},
        "recent": bucket_view(result["recent"], money_view),
    }


def closure_totals(closures: list[dict], latest: str | None, money_view, recent_days: int = RECENT_DAYS) -> dict:
    """關倉損益 per day, per month and over the same recent window as the averages.

    closures: {"date" (the closing row's daily record), "id", "symbol", "reason", "net" (integer
    units of USDT, None while the settlement is incomplete)}. Kept apart from the averages: a
    closure's result is the money lost or won on the position, not grid profit.
    """
    since = (date.fromisoformat(latest) - timedelta(days=recent_days)).isoformat() if latest else None
    blank = lambda: {"count": 0, "pending": 0, "net": 0}  # noqa: E731
    days: dict[str, dict] = {}
    months: dict[str, dict] = {}
    recent = blank()
    for c in sorted(closures, key=lambda c: (c["date"], c["symbol"] or "")):
        day = days.setdefault(c["date"], {**blank(), "items": []})
        day["items"].append({"id": c["id"], "symbol": c["symbol"], "reason": c["reason"], "net": money_view(c["net"])})
        for bucket in [day, months.setdefault(c["date"][:7], blank())] + ([recent] if since and c["date"] > since else []):
            bucket["count"] += 1
            if c["net"] is None:
                bucket["pending"] += 1
            else:
                bucket["net"] += c["net"]
    view = lambda b: {**b, "net": money_view(b["net"]) if b["count"] > b["pending"] else None}  # noqa: E731
    return {"days": {k: view(b) for k, b in days.items()}, "months": {k: view(b) for k, b in months.items()},
            "recent": view(recent)}
