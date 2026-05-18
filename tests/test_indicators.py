from __future__ import annotations

import unittest
from decimal import Decimal

from app.data.candle_builder import CandleSeries, OHLCVCandle
from app.data.indicators import (
    average_true_range,
    bollinger_bands,
    exponential_moving_average,
    latest_indicator_snapshot,
    macd,
    relative_strength_index,
    volume_profile,
)


def _series(count: int = 40) -> CandleSeries:
    candles = []
    for index in range(count):
        close = Decimal("100") + Decimal(index)
        candles.append(
            OHLCVCandle(
                pair="B-BTC_USDT",
                interval="1h",
                open_time_ms=index * 3_600_000,
                close_time_ms=((index + 1) * 3_600_000) - 1,
                open=close - Decimal("0.5"),
                high=close + Decimal("1"),
                low=close - Decimal("1"),
                close=close,
                volume=Decimal("10") + Decimal(index),
            )
        )
    return CandleSeries(candles)


class IndicatorTests(unittest.TestCase):
    def test_ema_seeds_with_sma(self) -> None:
        values = [Decimal("1"), Decimal("2"), Decimal("3"), Decimal("4")]
        ema = exponential_moving_average(values, 3)
        self.assertEqual(ema[0], None)
        self.assertEqual(ema[1], None)
        self.assertEqual(ema[2], Decimal("2"))
        self.assertEqual(ema[3], Decimal("3.0"))

    def test_rsi_reaches_100_when_no_losses(self) -> None:
        values = [Decimal(index) for index in range(1, 20)]
        rsi = relative_strength_index(values, 14)
        self.assertEqual(rsi[-1], Decimal("100"))

    def test_rsi_is_50_when_price_is_flat(self) -> None:
        values = [Decimal("100") for _ in range(20)]
        rsi = relative_strength_index(values, 14)
        self.assertEqual(rsi[-1], Decimal("50"))

    def test_macd_eventually_returns_points(self) -> None:
        points = macd(_series(45).closes())
        self.assertIsNotNone(points[-1])
        self.assertGreater(points[-1].macd, Decimal("0"))  # type: ignore[union-attr]

    def test_bollinger_bands_returns_width(self) -> None:
        bands = bollinger_bands(_series(25).closes(), period=20)
        self.assertIsNone(bands[18])
        self.assertIsNotNone(bands[-1])
        self.assertGreater(bands[-1].width_pct, Decimal("0"))  # type: ignore[union-attr]

    def test_atr_returns_smoothed_value(self) -> None:
        atr = average_true_range(_series(20), period=14)
        self.assertIsNone(atr[12])
        self.assertIsNotNone(atr[-1])
        self.assertGreater(atr[-1], Decimal("0"))  # type: ignore[operator]

    def test_volume_profile_bins_volume(self) -> None:
        profile = volume_profile(_series(10), bins=5)
        self.assertEqual(len(profile), 5)
        self.assertEqual(
            sum((item.volume for item in profile), Decimal("0")),
            sum(_series(10).volumes(), Decimal("0")),
        )

    def test_latest_snapshot_contains_core_indicators(self) -> None:
        snapshot = latest_indicator_snapshot(_series(45), volume_profile_bins=6)
        self.assertIsNotNone(snapshot.ema_fast)
        self.assertIsNotNone(snapshot.ema_slow)
        self.assertIsNotNone(snapshot.rsi)
        self.assertIsNotNone(snapshot.macd)
        self.assertIsNotNone(snapshot.bollinger)
        self.assertIsNotNone(snapshot.atr)
        self.assertEqual(len(snapshot.volume_profile), 6)

    def test_snapshot_to_dict_converts_nested_decimals(self) -> None:
        snapshot = latest_indicator_snapshot(_series(45), volume_profile_bins=2)
        data = snapshot.to_dict()

        self.assertIsInstance(data["ema_fast"], str)
        self.assertIsInstance(data["macd"]["macd"], str)  # type: ignore[index]
        self.assertIsInstance(data["volume_profile"][0]["volume"], str)  # type: ignore[index]


if __name__ == "__main__":
    unittest.main()
