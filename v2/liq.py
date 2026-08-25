"""Liquidation display price and remaining distance.

USDT-margined (U本位) is the default. Coin-margined inverse contracts
(USDT.PERP quoted in ETH/BTC) report prices in reciprocal units, which
makes a naive long/short formula go negative. If distance is not in
0–100%, invert and/or flip the side until it is.
"""

from __future__ import annotations

import math


def _distance(side: str, mark: float, liq: float) -> float:
    if side == "short":
        return (liq - mark) / mark * 100
    return (mark - liq) / mark * 100


def _in_range(distance: float | None) -> bool:
    return distance is not None and 0 <= distance <= 100


def _mismatch(mark: float, liq: float) -> bool:
    if mark <= 0 or liq <= 0:
        return False
    return abs(math.log10(mark) - math.log10(liq)) >= 2


def normalize_liq(trend: str | None, mark: float | None, liq: float | None, coin_margined: bool = False) -> tuple[float | None, float | None, str]:
    """Return (display_liq, distance_pct, mode). Distance is remaining room, 0–100 when safe."""
    if mark is None or liq is None or mark <= 0 or liq <= 0:
        return liq, None, "missing"
    side = "short" if (trend or "") == "short" else "long"
    direct = _distance(side, mark, liq)
    inverse = coin_margined or _mismatch(mark, liq)

    if not inverse:
        if _in_range(direct):
            return liq, direct, "usdt"
        return liq, 0.0, "usdt-clamped"

    display = (1.0 / liq) if _mismatch(mark, liq) else liq
    flip = "long" if side == "short" else "short"
    options = [
        (display, _distance(flip, mark, display), "coin-flip"),
        (display, _distance(side, mark, display), "coin-same"),
        (liq, _distance(flip, mark, liq), "flip-only"),
        (liq, direct, "raw"),
    ]
    good = [item for item in options if _in_range(item[1])]
    if good:
        return good[0]
    return display, 0.0, "coin-clamped"
