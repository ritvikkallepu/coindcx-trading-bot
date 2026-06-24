from __future__ import annotations

"""Profit giveback protection shared by paper, backtest, and live loops."""

from dataclasses import dataclass
from decimal import Decimal

from app.strategies.base import SignalDirection


@dataclass(frozen=True)
class ProfitGivebackResult:
    active: bool
    stop: Decimal | None
    lock_r: Decimal
    max_r: Decimal
    current_r: Decimal
    favorable_r: Decimal


def compute_profit_giveback_stop(
    *,
    entry_price: Decimal,
    initial_r: Decimal,
    direction: SignalDirection,
    current_stop: Decimal | None,
    current_price: Decimal,
    favorable_price: Decimal,
    previous_max_r: Decimal,
    activation_r: Decimal,
    lock_fraction: Decimal,
    min_lock_r: Decimal,
    tighten_after_r: Decimal,
    tighten_fraction: Decimal,
) -> ProfitGivebackResult:
    """Protect a fraction of the best R seen so a large winner cannot fade too far.

    The function is deliberately stateless. Callers persist ``max_r`` and the
    returned stop in their own position metadata.
    """

    zero = Decimal("0")
    if initial_r <= zero or entry_price <= zero:
        return ProfitGivebackResult(False, None, zero, previous_max_r, zero, zero)

    current_points = _profit_points(
        direction=direction,
        entry_price=entry_price,
        price=current_price,
    )
    favorable_points = _profit_points(
        direction=direction,
        entry_price=entry_price,
        price=favorable_price,
    )
    current_r = current_points / initial_r
    favorable_r = favorable_points / initial_r
    max_r = max(previous_max_r, current_r, favorable_r, zero)

    if max_r < max(activation_r, zero):
        return ProfitGivebackResult(False, None, zero, max_r, current_r, favorable_r)

    lock_fraction = _clamp(lock_fraction, Decimal("0.05"), Decimal("0.95"))
    tighten_fraction = _clamp(
        max(tighten_fraction, lock_fraction),
        lock_fraction,
        Decimal("0.98"),
    )
    min_lock_r = max(min_lock_r, zero)
    lock_r = max(min_lock_r, max_r * lock_fraction)
    if max_r >= tighten_after_r:
        lock_r = max(lock_r, max_r * tighten_fraction)

    if lock_r <= zero:
        return ProfitGivebackResult(False, None, zero, max_r, current_r, favorable_r)

    if direction == SignalDirection.LONG:
        stop = entry_price + (initial_r * lock_r)
        if current_stop is not None and current_stop > zero:
            stop = max(stop, current_stop)
    else:
        stop = entry_price - (initial_r * lock_r)
        if stop <= zero:
            return ProfitGivebackResult(False, None, lock_r, max_r, current_r, favorable_r)
        if current_stop is not None and current_stop > zero:
            stop = min(stop, current_stop)

    if stop <= zero:
        return ProfitGivebackResult(False, None, lock_r, max_r, current_r, favorable_r)
    return ProfitGivebackResult(True, stop, lock_r, max_r, current_r, favorable_r)


def post_profit_pullback_seen(
    *,
    direction: SignalDirection,
    exit_price: Decimal,
    candle_high: Decimal,
    candle_low: Decimal,
    atr: Decimal | None,
    pullback_atr: Decimal,
    pullback_pct: Decimal,
) -> bool:
    """Return True once price has reset enough after a profitable exit."""

    pct_distance = exit_price * max(pullback_pct, Decimal("0")) / Decimal("100")
    atr_distance = (atr or Decimal("0")) * max(pullback_atr, Decimal("0"))
    required = max(pct_distance, atr_distance)
    if required <= 0:
        return True
    if direction == SignalDirection.LONG:
        return candle_low <= exit_price - required
    return candle_high >= exit_price + required

def _profit_points(
    *,
    direction: SignalDirection,
    entry_price: Decimal,
    price: Decimal,
) -> Decimal:
    if direction == SignalDirection.LONG:
        return price - entry_price
    return entry_price - price


def _clamp(value: Decimal, low: Decimal, high: Decimal) -> Decimal:
    return min(max(value, low), high)
