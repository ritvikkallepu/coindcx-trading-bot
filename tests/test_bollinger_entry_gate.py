from __future__ import annotations

import unittest
from decimal import Decimal

from app.data.candle_builder import CandleSeries, OHLCVCandle
from app.data.indicators import BollingerBandPoint, latest_indicator_snapshot
from app.strategies.base import SignalAction, SignalDirection, StrategyContext
from app.strategies.bollinger_entry_gate import evaluate_bb_entry_gate
from app.strategies.hybrid_meta import HybridMetaStrategy


def _candle(
    close: Decimal,
    *,
    low: Decimal | None = None,
    high: Decimal | None = None,
    index: int = 0,
) -> OHLCVCandle:
    return OHLCVCandle(
        pair="B-BTC_USDT",
        interval="5m",
        open_time_ms=index * 300_000,
        close_time_ms=((index + 1) * 300_000) - 1,
        open=close,
        high=high or close,
        low=low or close,
        close=close,
        volume=Decimal("1000"),
    )


class BollingerEntryGateTests(unittest.TestCase):
    def test_long_passes_after_lower_band_touch_and_reclaim(self) -> None:
        band = BollingerBandPoint(
            middle=Decimal("100"),
            upper=Decimal("110"),
            lower=Decimal("90"),
            width_pct=Decimal("20"),
        )

        result = evaluate_bb_entry_gate(
            direction=SignalDirection.LONG,
            entry_price=Decimal("94"),
            entry_candle=_candle(Decimal("94"), low=Decimal("89")),
            recent_candles=[_candle(Decimal("93"), low=Decimal("89"))],
            bands=[band],
            atr=Decimal("4"),
            enabled=True,
        )

        self.assertTrue(result["bb_entry_gate_passed"])
        self.assertEqual(result["bb_entry_band_position"], Decimal("0.2"))

    def test_long_blocks_chase_above_middle_band(self) -> None:
        band = BollingerBandPoint(
            middle=Decimal("100"),
            upper=Decimal("110"),
            lower=Decimal("90"),
            width_pct=Decimal("20"),
        )

        result = evaluate_bb_entry_gate(
            direction=SignalDirection.LONG,
            entry_price=Decimal("105"),
            entry_candle=_candle(Decimal("105"), low=Decimal("89")),
            recent_candles=[_candle(Decimal("93"), low=Decimal("89"))],
            bands=[band],
            atr=Decimal("4"),
            enabled=True,
        )

        self.assertFalse(result["bb_entry_gate_passed"])
        self.assertIn("outside required zone", str(result["bb_entry_gate_reason"]))

    def test_short_passes_after_upper_band_touch_and_reject(self) -> None:
        band = BollingerBandPoint(
            middle=Decimal("100"),
            upper=Decimal("110"),
            lower=Decimal("90"),
            width_pct=Decimal("20"),
        )

        result = evaluate_bb_entry_gate(
            direction=SignalDirection.SHORT,
            entry_price=Decimal("106"),
            entry_candle=_candle(Decimal("106"), high=Decimal("111")),
            recent_candles=[_candle(Decimal("108"), high=Decimal("111"))],
            bands=[band],
            atr=Decimal("4"),
            enabled=True,
        )

        self.assertTrue(result["bb_entry_gate_passed"])
        self.assertEqual(result["bb_entry_band_position"], Decimal("0.8"))

    def test_long_blocks_when_middle_band_falls_too_hard(self) -> None:
        previous = BollingerBandPoint(
            middle=Decimal("105"),
            upper=Decimal("115"),
            lower=Decimal("95"),
            width_pct=Decimal("19"),
        )
        current = BollingerBandPoint(
            middle=Decimal("100"),
            upper=Decimal("110"),
            lower=Decimal("90"),
            width_pct=Decimal("20"),
        )

        result = evaluate_bb_entry_gate(
            direction=SignalDirection.LONG,
            entry_price=Decimal("94"),
            entry_candle=_candle(Decimal("94"), low=Decimal("89")),
            recent_candles=[_candle(Decimal("93"), low=Decimal("89"))],
            bands=[previous, current],
            atr=Decimal("4"),
            enabled=True,
        )

        self.assertFalse(result["bb_entry_gate_passed"])
        self.assertIn("middle-band slope", str(result["bb_entry_gate_reason"]))

    def test_hybrid_blocks_long_chase_when_bb_trail_enabled(self) -> None:
        candles = [
            _candle(Decimal("100"), index=i)
            for i in range(30)
        ]
        for offset, close in enumerate(
            [Decimal("101"), Decimal("102"), Decimal("103"), Decimal("104"), Decimal("105"), Decimal("106")],
            start=30,
        ):
            candles.append(
                OHLCVCandle(
                    pair="B-BTC_USDT",
                    interval="5m",
                    open_time_ms=offset * 300_000,
                    close_time_ms=((offset + 1) * 300_000) - 1,
                    open=close - Decimal("0.5"),
                    high=close + Decimal("0.5"),
                    low=close - Decimal("0.5"),
                    close=close,
                    volume=Decimal("2000"),
                )
            )
        series = CandleSeries(candles)
        strategy = HybridMetaStrategy(
            fast_period=2,
            slow_period=4,
            rsi_period=3,
            bollinger_period=5,
            atr_period=3,
            volume_period=3,
            visual_lookback=5,
            entry_threshold=Decimal("0.05"),
            min_volume_ratio=Decimal("0.40"),
            balanced_breakout_enabled=False,
            pullback_entry_enabled=False,
        )

        signal = strategy.evaluate(
            StrategyContext(
                pair="B-BTC_USDT",
                interval="5m",
                candles=series,
                indicators=latest_indicator_snapshot(series),
                features={
                    "backtest_config": {
                        "bb_trail_enabled": True,
                        "long_entry_threshold": Decimal("0.05"),
                        "trade_quality_mode": "strict",
                    }
                },
            )
        )

        self.assertEqual(signal.action, SignalAction.HOLD)
        self.assertIn("BB entry gate blocked long", signal.reason)
        self.assertFalse(signal.metadata["bb_entry_gate_passed"])


if __name__ == "__main__":
    unittest.main()
