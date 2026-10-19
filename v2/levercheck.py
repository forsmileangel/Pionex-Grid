"""Before/after check for leverage adjustments that keep the margin inside the same bot.

Read-only: reads v2-data/live-snapshot.json (refreshed every 30 minutes or by 「更新目前倉位」)
and writes baselines/reports under v2-data/audits/. Never touches the ledger or the Gist.

    python -m v2.levercheck --save-baseline    # before adjusting
    python -m v2.levercheck                    # after adjusting: compare with the latest baseline
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

from .gridsim import levels, liq_price
from .live import load_live_snapshot
from .store import TAIPEI

ROOT = Path(__file__).resolve().parent.parent
AUDIT_DIR = ROOT / "v2-data" / "audits"


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def bot_state(rec: dict) -> dict:
    data = ((rec.get("RawJson") or {}).get("order") or {}).get("buOrderData") or {}
    inverse = bool(rec.get("InverseCoinMargined"))
    quote_inv = _f(data.get("quoteInvestment"))
    extra = _f(data.get("extraMargin")) or 0.0
    mark = _f(rec.get("MarkPrice"))
    state = {
        "id": str(rec.get("ApiOrderId")), "symbol": rec.get("Symbol"), "inverse": inverse,
        "leverage": _f(data.get("leverage")) or _f(rec.get("LeverageValue")),
        "investment": _f(rec.get("Investment")),          # what the ledger / dashboard records
        "quote_investment": quote_inv, "extra_margin": extra,
        "capital": None if quote_inv is None else quote_inv + extra,
        "margin_balance": _f(data.get("marginBalance")), "per_volume": _f(data.get("perVolume")),
        "position": _f(rec.get("Position")), "mark": mark,
        "grid_profit": _f(data.get("gridProfit")),
        "reinvest": _f(data.get("profitReinvest")), "withdrawn": _f(data.get("profitWithdrawn")),
        "reduce": _f(data.get("profitReduce")),
        "top": _f(rec.get("Top")), "bottom": _f(rec.get("Bottom")), "rows": rec.get("Row"),
        "grid_type": rec.get("GridType") or "geometric",
        "liq_pionex": _f(data.get("estimateLiquidationPriceDown")),
    }
    state["notional_basis"] = (state["investment"] or 0) * (state["leverage"] or 0)
    return state


def snapshot_states(snapshot: dict) -> dict:
    return {str(r.get("ApiOrderId")): bot_state(r) for r in snapshot.get("Records", [])
            if r.get("ListStatus") == "running"}


def _pct(a, b):
    if a in (None, 0) or b is None:
        return None
    return (b / a - 1) * 100


def _liq_at(state: dict, mark: float) -> float | None:
    """Model liquidation if the bot's current holdings were valued at `mark` (same price for before/after)."""
    if state["inverse"] or not all(state.get(k) for k in ("top", "bottom", "rows", "per_volume", "margin_balance")):
        return None
    lv = levels(state["bottom"], state["top"], int(state["rows"]), state["grid_type"])
    equity = state["margin_balance"] + (state["position"] or 0) * (mark - state["mark"])
    position = state["per_volume"] * sum(1 for l in lv[:-1] if l >= mark)
    return liq_price(equity, mark, position, lv, state["per_volume"])


def compare(before: dict, after: dict) -> dict:
    rows = []
    for oid, b in before.items():
        a = after.get(oid)
        if a is None:
            rows.append({"id": oid, "symbol": b["symbol"], "verdict": "missing",
                         "notes": ["這個訂單已不在運行中：派網可能把它關掉並重開成新訂單，帳本會當成一筆關倉加一筆新倉。"]})
            continue
        notes, verdict = [], "same"
        lev_changed = (a["leverage"] or 0) != (b["leverage"] or 0)
        pv = _pct(b["per_volume"], a["per_volume"])
        cap = _pct(b["capital"], a["capital"])
        basis = _pct(b["notional_basis"], a["notional_basis"])
        inv = _pct(b["investment"], a["investment"])
        if lev_changed:
            verdict = "adjusted"
            notes.append(f"槓桿 {b['leverage']:g} → {a['leverage']:g} 倍")
        if pv is not None and abs(pv) > 1:
            verdict = "exposure_changed"
            notes.append(f"每格數量 {b['per_volume']:g} → {a['per_volume']:g}（{pv:+.0f}%）：實際曝險跟著{'變大' if pv > 0 else '變小'}")
        elif lev_changed:
            notes.append("每格數量沒變：實際曝險不變")
        if cap is not None and abs(cap) > 0.5:
            notes.append(f"投入＋追加保證金 {b['capital']:,.2f} → {a['capital']:,.2f}（{cap:+.1f}%）")
        if inv is not None and abs(inv) > 0.5:
            notes.append(f"帳本「投入」{b['investment']:,.2f} → {a['investment']:,.2f}：儀表板倉位大小、PT 投入會跟著變")
        # An add grows investment x leverage together with capital, so per-U figures stay comparable.
        if basis is not None and abs(basis - (cap or 0)) > 2:
            notes.append(f"投入 × 槓桿變了 {basis:+.0f}%：當天之後的「每 U 效率（3 倍換算）」與之前不能直接比")
        if b["grid_profit"] is not None and a["grid_profit"] is not None and a["grid_profit"] < b["grid_profit"] - max(1.0, abs(b["grid_profit"]) * 0.02):
            verdict = "grid_reset"
            notes.append(f"網格利潤從 {b['grid_profit']:,.2f} 掉到 {a['grid_profit']:,.2f}：可能被重置或轉入投資，帳本會判斷成複投／待確認")
        for key, label in (("reinvest", "複投"), ("withdrawn", "提領"), ("reduce", "減倉")):
            if (a[key] or 0) != (b[key] or 0):
                notes.append(f"API {label}累計 {b[key] or 0:g} → {a[key] or 0:g}")
        liq_b, liq_a = _liq_at(b, a["mark"]), _liq_at(a, a["mark"]) if a["mark"] else None
        if liq_b and liq_a:
            d = (liq_a / liq_b - 1) * 100
            if abs(d) > 2:
                notes.append(f"同一價格下的強平價 {liq_b:,.4g} → {liq_a:,.4g}（{d:+.0f}%）")
        rows.append({"id": oid, "symbol": a["symbol"], "verdict": verdict, "notes": notes,
                     "before": b, "after": a})
    for oid, a in after.items():
        if oid not in before:
            rows.append({"id": oid, "symbol": a["symbol"], "verdict": "new", "notes": ["基準之後新開的倉位"]})
    return {"rows": rows}


LABEL = {"same": "沒有變", "adjusted": "等效調整", "exposure_changed": "曝險改變", "grid_reset": "網格利潤重置",
         "missing": "訂單不見", "new": "新倉位"}


def main() -> int:
    parser = argparse.ArgumentParser(description="Leverage adjustment before/after check (read-only)")
    parser.add_argument("--save-baseline", action="store_true")
    parser.add_argument("--baseline", help="Baseline file (default: latest)")
    args = parser.parse_args()
    snap = load_live_snapshot() or {}
    states = snapshot_states(snap)
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    if args.save_baseline:
        stamp = datetime.now(TAIPEI).strftime("%Y%m%d-%H%M")
        path = AUDIT_DIR / f"leverage-baseline-{stamp}.json"
        path.write_text(json.dumps({"captured_at": snap.get("CapturedAt"), "bots": states}, ensure_ascii=False, indent=1),
                        encoding="utf-8")
        print(f"baseline {path} ({len(states)} bots, snapshot {snap.get('CapturedAt')})")
        return 0
    base_path = Path(args.baseline) if args.baseline else max(AUDIT_DIR.glob("leverage-baseline-*.json"), default=None)
    if not base_path:
        print("no baseline: run with --save-baseline first")
        return 1
    base = json.loads(base_path.read_text(encoding="utf-8"))
    result = compare(base["bots"], states)
    result.update(baseline=str(base_path), baseline_at=base.get("captured_at"), current_at=snap.get("CapturedAt"))
    out = AUDIT_DIR / f"leverage-check-{datetime.now(TAIPEI).strftime('%Y%m%d-%H%M')}.json"
    out.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"baseline {base.get('captured_at')} → now {snap.get('CapturedAt')}")
    for r in result["rows"]:
        print(f"[{LABEL[r['verdict']]}] {r['symbol']}  " + "；".join(r["notes"]))
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
