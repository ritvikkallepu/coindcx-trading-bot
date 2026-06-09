from __future__ import annotations

import unittest
from decimal import Decimal

from app.data.candle_builder import OHLCVCandle
from app.strategies.base import SignalDirection
from app.strategies.swing_structure import confirmed_pivots, find_latest_impulse


def _candles(rows: list[tuple[str, str]]) -> list[OHLCVCandle]:
    candles: list[OHLCVCandle] = []
    for index, (high, low) in enumerate(rows):
        close = Decimal(high) - ((Decimal(high) - Decimal(low)) / Decimal("2"))
        candles.append(
            OHLCVCandle(
                pair="B-BTC_USDT",
                interval="1h",
                open_time_ms=index * 3_600_000,
                close_time_ms=((index + 1) * 3_600_000) - 1,
                open=close,
                high=Decimal(high),
                low=Decimal(low),
                close=close,
                volume=Decimal("100"),
            )
        )
    return candles


class SwingStructureTests(unittest.TestCase):
    def test_confirms_local_pivots_without_using_latest_candle(self) -> None:
        candles = _candles([
            ("100", "99"),
            ("101", "98"),
            ("103", "100"),
            ("106", "104"),
            ("105", "102"),
            ("104", "101"),
        ])

        pivots = confirmed_pivots(candles, left_bars=1, right_bars=1)

        self.assertEqual([(pivot.kind, pivot.index) for pivot in pivots], [("low", 1), ("high", 3)])

    def test_finds_latest_meaningful_long_impulse_and_skips_tiny_latest_leg(self) -> None:
        candles = _candles([
            ("100", "99"),
            ("101", "98"),
            ("103", "100"),
            ("106", "104"),
            ("104", "102"),
            ("105", "103"),
            ("104", "102.5"),
        ])

        impulse = find_latest_impulse(
            candles,
            direction=SignalDirection.LONG,
            lookback=20,
            max_pullback_candles=10,
            left_bars=1,
            right_bars=1,
            min_range=Decimal("5"),
        )

        self.assertIsNotNone(impulse)
        assert impulse is not None
        self.assertEqual(impulse.start_index, 1)
        self.assertEqual(impulse.end_index, 3)
        self.assertEqual(impulse.range, Decimal("8"))

    def test_finds_latest_meaningful_short_impulse(self) -> None:
        candles = _candles([
            ("107", "105"),
            ("108", "106"),
            ("106", "103"),
            ("104", "100"),
            ("105", "102"),
            ("104", "101"),
        ])

        impulse = find_latest_impulse(
            candles,
            direction=SignalDirection.SHORT,
            lookback=20,
            max_pullback_candles=10,
            left_bars=1,
            right_bars=1,
            min_range=Decimal("5"),
        )

        self.assertIsNotNone(impulse)
        assert impulse is not None
        self.assertEqual(impulse.start_index, 1)
        self.assertEqual(impulse.end_index, 3)
        self.assertEqual(impulse.range, Decimal("8"))

    def test_rejects_impulse_when_pullback_is_too_old(self) -> None:
        candles = _candles([
            ("100", "99"),
            ("101", "98"),
            ("103", "100"),
            ("106", "104"),
            ("105", "102"),
            ("104", "101"),
            ("103", "100"),
        ])

        impulse = find_latest_impulse(
            candles,
            direction=SignalDirection.LONG,
            lookback=20,
            max_pullback_candles=2,
            left_bars=1,
            right_bars=1,
            min_range=Decimal("5"),
        )

        self.assertIsNone(impulse)

    def test_ignores_same_candle_outside_bar_impulse(self) -> None:
        candles = _candles([
            ("100", "99"),
            ("110", "90"),
            ("105", "95"),
            ("104", "96"),
        ])

        impulse = find_latest_impulse(
            candles,
            direction=SignalDirection.SHORT,
            lookback=20,
            max_pullback_candles=10,
            left_bars=1,
            right_bars=1,
            min_range=Decimal("5"),
        )

        self.assertIsNone(impulse)


if __name__ == "__main__":
    unittest.main()
