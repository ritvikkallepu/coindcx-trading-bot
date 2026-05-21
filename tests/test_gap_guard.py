from __future__ import annotations

import unittest
from decimal import Decimal
from app.data.candle_builder import OHLCVCandle, interval_to_ms
from app.data.gap_guard import CandleGapGuard


def _candle(pair: str, interval: str, open_time_ms: int) -> OHLCVCandle:
    interval_ms = interval_to_ms(interval)
    return OHLCVCandle(
        pair=pair,
        interval=interval,
        open_time_ms=open_time_ms,
        close_time_ms=open_time_ms + interval_ms - 1,
        open=Decimal("100"),
        high=Decimal("101"),
        low=Decimal("99"),
        close=Decimal("100.5"),
        volume=Decimal("10"),
    )


class GapGuardTests(unittest.TestCase):
    def test_consecutive_candles_no_gap(self) -> None:
        guard = CandleGapGuard("5m")
        interval_ms = interval_to_ms("5m")
        
        c1 = _candle("BTC", "5m", 0)
        c2 = _candle("BTC", "5m", interval_ms)
        
        result = guard.check(c1, c2)
        self.assertFalse(result.has_gap)
        self.assertEqual(result.missed_candles, 0)
        self.assertEqual(result.gap_ms, interval_ms)

    def test_exactly_one_missed_candle(self) -> None:
        guard = CandleGapGuard("5m")
        interval_ms = interval_to_ms("5m")
        
        # Misses the candle at index 1
        c1 = _candle("BTC", "5m", 0)
        c2 = _candle("BTC", "5m", interval_ms * 2)
        
        result = guard.check(c1, c2)
        self.assertTrue(result.has_gap)
        self.assertEqual(result.missed_candles, 1)
        self.assertEqual(result.gap_ms, interval_ms * 2)

    def test_multiple_missed_candles(self) -> None:
        guard = CandleGapGuard("5m")
        interval_ms = interval_to_ms("5m")
        
        # Misses 4 candles (indices 1, 2, 3, 4)
        c1 = _candle("BTC", "5m", 0)
        c2 = _candle("BTC", "5m", interval_ms * 5)
        
        result = guard.check(c1, c2)
        self.assertTrue(result.has_gap)
        self.assertEqual(result.missed_candles, 4)

    def test_gap_smaller_than_threshold_no_flag(self) -> None:
        guard = CandleGapGuard("5m")
        interval_ms = interval_to_ms("5m")
        
        # Next candle is slightly delayed (e.g. by network jitter)
        # but is still well within the 1.5x threshold (1.2x)
        c1 = _candle("BTC", "5m", 0)
        c2 = _candle("BTC", "5m", int(interval_ms * 1.2))
        
        result = guard.check(c1, c2)
        self.assertFalse(result.has_gap)
        self.assertEqual(result.missed_candles, 0)

    def test_works_on_one_hour_interval(self) -> None:
        guard = CandleGapGuard("1h")
        interval_ms = interval_to_ms("1h")
        
        # Misses 2 hours
        c1 = _candle("BTC", "1h", 0)
        c2 = _candle("BTC", "1h", interval_ms * 3)
        
        result = guard.check(c1, c2)
        self.assertTrue(result.has_gap)
        self.assertEqual(result.missed_candles, 2)
        self.assertEqual(result.gap_ms, interval_ms * 3)


if __name__ == "__main__":
    unittest.main()
