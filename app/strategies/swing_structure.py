from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.data.candle_builder import OHLCVCandle
from app.strategies.base import SignalDirection


@dataclass(frozen=True)
class PivotPoint:
    """Confirmed local swing point."""

    index: int
    kind: str
    price: Decimal
    time_ms: int


@dataclass(frozen=True)
class ImpulseSwing:
    """Directional impulse leg used for structure-based entries."""

    direction: SignalDirection
    start_index: int
    end_index: int
    start_price: Decimal
    end_price: Decimal
    start_time_ms: int
    end_time_ms: int

    @property
    def low(self) -> Decimal:
        return min(self.start_price, self.end_price)

    @property
    def high(self) -> Decimal:
        return max(self.start_price, self.end_price)

    @property
    def range(self) -> Decimal:
        return self.high - self.low


def confirmed_pivots(
    candles: list[OHLCVCandle],
    *,
    left_bars: int = 2,
    right_bars: int = 2,
) -> list[PivotPoint]:
    """Return confirmed pivot highs/lows using neighboring candles."""

    if left_bars < 1 or right_bars < 1:
        raise ValueError("left_bars and right_bars must be >= 1")
    if len(candles) < left_bars + right_bars + 1:
        return []

    pivots: list[PivotPoint] = []
    for index in range(left_bars, len(candles) - right_bars):
        candle = candles[index]
        left = candles[index - left_bars:index]
        right = candles[index + 1:index + right_bars + 1]
        neighbors = left + right

        if all(candle.high >= item.high for item in neighbors) and any(
            candle.high > item.high for item in neighbors
        ):
            pivots.append(
                PivotPoint(
                    index=index,
                    kind="high",
                    price=candle.high,
                    time_ms=candle.close_time_ms,
                )
            )
        if all(candle.low <= item.low for item in neighbors) and any(
            candle.low < item.low for item in neighbors
        ):
            pivots.append(
                PivotPoint(
                    index=index,
                    kind="low",
                    price=candle.low,
                    time_ms=candle.close_time_ms,
                )
            )
    return _compress_alternating_pivots(pivots)


def find_latest_impulse(
    candles: list[OHLCVCandle],
    *,
    direction: SignalDirection,
    lookback: int,
    max_pullback_candles: int,
    left_bars: int = 2,
    right_bars: int = 2,
    min_range: Decimal | None = None,
) -> ImpulseSwing | None:
    """Find the latest confirmed impulse leg that still has a live pullback."""

    if len(candles) < 4:
        return None
    start = max(0, len(candles) - lookback)
    window = candles[start:]
    pivots = [
        PivotPoint(
            index=pivot.index + start,
            kind=pivot.kind,
            price=pivot.price,
            time_ms=pivot.time_ms,
        )
        for pivot in confirmed_pivots(
            window,
            left_bars=left_bars,
            right_bars=right_bars,
        )
    ]
    if len(pivots) < 2:
        return None

    latest_index = len(candles) - 1
    target_end_kind = "high" if direction == SignalDirection.LONG else "low"
    target_start_kind = "low" if direction == SignalDirection.LONG else "high"
    min_range = min_range if min_range is not None else Decimal("0")

    for pivot_index in range(len(pivots) - 1, 0, -1):
        end = pivots[pivot_index]
        start_pivot = pivots[pivot_index - 1]
        if end.kind != target_end_kind or start_pivot.kind != target_start_kind:
            continue
        if end.index <= start_pivot.index:
            continue
        pullback_age = latest_index - end.index
        if pullback_age <= 0 or pullback_age > max_pullback_candles:
            continue
        impulse = ImpulseSwing(
            direction=direction,
            start_index=start_pivot.index,
            end_index=end.index,
            start_price=start_pivot.price,
            end_price=end.price,
            start_time_ms=start_pivot.time_ms,
            end_time_ms=end.time_ms,
        )
        if impulse.range >= min_range:
            return impulse
    return None


def _compress_alternating_pivots(pivots: list[PivotPoint]) -> list[PivotPoint]:
    compressed: list[PivotPoint] = []
    for pivot in pivots:
        if not compressed or compressed[-1].kind != pivot.kind:
            compressed.append(pivot)
            continue

        previous = compressed[-1]
        replace_previous = (
            pivot.price >= previous.price
            if pivot.kind == "high"
            else pivot.price <= previous.price
        )
        if replace_previous:
            compressed[-1] = pivot
    return compressed
