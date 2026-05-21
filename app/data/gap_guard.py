from __future__ import annotations

from dataclasses import dataclass
from app.data.candle_builder import OHLCVCandle, interval_to_ms


@dataclass(frozen=True)
class GapCheckResult:
    has_gap: bool
    missed_candles: int
    prev_close_time_ms: int
    next_open_time_ms: int
    gap_ms: int


class CandleGapGuard:
    def __init__(self, interval: str) -> None:
        self.interval = interval
        self.interval_ms = interval_to_ms(interval)

    def check(self, prev: OHLCVCandle, next: OHLCVCandle) -> GapCheckResult:
        gap_ms = next.open_time_ms - prev.open_time_ms
        threshold = prev.open_time_ms + (self.interval_ms * 3) // 2
        has_gap = next.open_time_ms > threshold

        if not has_gap:
            return GapCheckResult(
                has_gap=False,
                missed_candles=0,
                prev_close_time_ms=prev.close_time_ms,
                next_open_time_ms=next.open_time_ms,
                gap_ms=gap_ms,
            )

        missed_candles = round(gap_ms / self.interval_ms) - 1
        return GapCheckResult(
            has_gap=True,
            missed_candles=max(missed_candles, 1),
            prev_close_time_ms=prev.close_time_ms,
            next_open_time_ms=next.open_time_ms,
            gap_ms=gap_ms,
        )
