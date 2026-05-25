from __future__ import annotations
import unittest
from decimal import Decimal
from app.data.candle_builder import CandleSeries, OHLCVCandle
from app.data.indicators import BollingerBandPoint, latest_indicator_snapshot
from app.strategies.base import SignalAction, SignalDirection, StrategyContext
from app.strategies.hybrid_meta import HybridMetaStrategy

def _series_from_closes(
    closes: list[Decimal],
    *,
    interval: str = "1h",
) -> CandleSeries:
    candles: list[OHLCVCandle] = []
    for index, close in enumerate(closes):
        candles.append(
            OHLCVCandle(
                pair="B-EDEN_USDT",
                interval=interval,
                open_time_ms=index * 3600000,
                close_time_ms=((index + 1) * 3600000) - 1,
                open=close,
                high=close * Decimal("1.001"),
                low=close * Decimal("0.999"),
                close=close,
                volume=Decimal("1000"),
            )
        )
    return CandleSeries(candles)

class TestSignalFlipStabilization(unittest.TestCase):
    def setUp(self):
        self.strategy = HybridMetaStrategy(
            signal_flip_grace_candles=3,
            signal_flip_confirm_candles=2,
            signal_flip_min_hold_candles=2,
            signal_flip_exit_requires_price_confirmation=True
        )

    def test_long_no_exit_during_min_hold(self):
        # Index 0-49: bullish
        # Index 50-54: sharp drop
        closes = [Decimal("100") + i for i in range(50)] + [Decimal("120"), Decimal("100"), Decimal("80"), Decimal("60"), Decimal("40")]
        series = _series_from_closes(closes)
        
        # Position opened at the close of candle 53 (T-1)
        opened_at_ms = series[53].close_time_ms
        
        features = {
            "open_position": {
                "pair": "B-EDEN_USDT",
                "direction": "long",
                "entry_price": Decimal("149"),
                "opened_at_ms": opened_at_ms,
            }
        }
        
        context = StrategyContext(
            pair="B-EDEN_USDT",
            interval="1h",
            candles=series,
            indicators=latest_indicator_snapshot(series),
            features=features
        )
        
        signal = self.strategy.evaluate(context)
        
        self.assertEqual(signal.action, SignalAction.HOLD)
        self.assertEqual(signal.metadata.get("final_exit_reason"), "signal_flip_ignored_min_hold")

    def test_long_no_exit_during_grace_period(self):
        closes = [Decimal("100") + i for i in range(50)] + [Decimal("120"), Decimal("100"), Decimal("80"), Decimal("60"), Decimal("40")]
        series = _series_from_closes(closes)
        
        # hold_candles = 3 (within grace period of 3)
        opened_at_ms = series[51].close_time_ms
        
        features = {
            "open_position": {
                "pair": "B-EDEN_USDT",
                "direction": "long",
                "entry_price": Decimal("149"),
                "opened_at_ms": opened_at_ms,
            }
        }
        
        context = StrategyContext(
            pair="B-EDEN_USDT",
            interval="1h",
            candles=series,
            indicators=latest_indicator_snapshot(series),
            features=features
        )
        
        signal = self.strategy.evaluate(context)
        
        self.assertEqual(signal.action, SignalAction.HOLD)
        self.assertEqual(signal.metadata.get("final_exit_reason"), "signal_flip_ignored_grace_period")

    def test_long_exit_after_confirmation(self):
        closes = [Decimal("100") + i for i in range(50)] + [Decimal("120"), Decimal("100"), Decimal("80"), Decimal("60"), Decimal("40")]
        series = _series_from_closes(closes)
        
        # Position opened long ago
        opened_at_ms = series[30].close_time_ms
        
        features = {
            "open_position": {
                "pair": "B-EDEN_USDT",
                "direction": "long",
                "entry_price": Decimal("130"),
                "opened_at_ms": opened_at_ms,
            }
        }
        
        context = StrategyContext(
            pair="B-EDEN_USDT",
            interval="1h",
            candles=series,
            indicators=latest_indicator_snapshot(series),
            features=features
        )
        
        signal = self.strategy.evaluate(context)
        
        # Should be EXIT_LONG after confirmation (confirm_candles=2)
        # We check T (40) and T-1 (60). Both should be bearish enough.
        self.assertEqual(signal.action, SignalAction.EXIT_LONG)
        self.assertTrue(signal.reason.startswith("signal_flip_exit_confirmed"))
        self.assertGreaterEqual(signal.metadata.get("signal_flip_confirm_count"), 2)

    def test_short_exit_after_confirmation(self):
        closes = [Decimal("100") - i for i in range(50)] + [Decimal("80"), Decimal("100"), Decimal("120"), Decimal("140"), Decimal("160")]
        series = _series_from_closes(closes)
        
        # Position opened long ago
        opened_at_ms = series[30].close_time_ms
        
        features = {
            "open_position": {
                "pair": "B-EDEN_USDT",
                "direction": "short",
                "entry_price": Decimal("70"),
                "opened_at_ms": opened_at_ms,
            }
        }
        
        context = StrategyContext(
            pair="B-EDEN_USDT",
            interval="1h",
            candles=series,
            indicators=latest_indicator_snapshot(series),
            features=features
        )
        
        signal = self.strategy.evaluate(context)
        
        self.assertEqual(signal.action, SignalAction.EXIT_SHORT)
        self.assertTrue(signal.reason.startswith("signal_flip_exit_confirmed"))
        self.assertGreaterEqual(signal.metadata.get("signal_flip_confirm_count"), 2)

    def test_price_confirmation_blocks_long_exit(self):
        # Index 0-49: bullish (100 to 149)
        # Index 50-53: sharp drop (120, 100, 80, 60)
        # Index 54: small bounce to 110 (still bearish trend, but above entry if entry was 105)
        closes = [Decimal("100") + i for i in range(50)] + [Decimal("120"), Decimal("100"), Decimal("80"), Decimal("60"), Decimal("110")]
        series = _series_from_closes(closes)
        
        # Entry price: 105
        # Opened at index 5 (long ago)
        opened_at_ms = series[5].close_time_ms
        
        features = {
            "open_position": {
                "pair": "B-EDEN_USDT",
                "direction": "long",
                "entry_price": Decimal("105"),
                "opened_at_ms": opened_at_ms,
            }
        }
        
        context = StrategyContext(
            pair="B-EDEN_USDT",
            interval="1h",
            candles=series,
            indicators=latest_indicator_snapshot(series),
            features=features
        )
        
        signal = self.strategy.evaluate(context)
        
        # Final candle is 110, which is > entry (105).
        # T-1 was 60, T-2 was 80. So it's confirmed bearish trend.
        # But price 110 > entry 105 should block the exit.
        self.assertEqual(signal.action, SignalAction.HOLD)
        self.assertEqual(signal.metadata.get("final_exit_reason"), "signal_flip_waiting_price_confirmation")

    def test_bb_managed_long_ignores_flip_until_lower_band_breaks(self):
        series = _series_from_closes(
            [Decimal("100"), Decimal("99"), Decimal("98"), Decimal("99"), Decimal("97"), Decimal("98")]
        )
        bands = [
            BollingerBandPoint(
                middle=Decimal("100"),
                upper=Decimal("110"),
                lower=Decimal("90"),
                width_pct=Decimal("20"),
            )
            for _ in range(len(series))
        ]
        allowed, reason, metadata = self.strategy._check_signal_flip_exit(
            context=StrategyContext(
                pair="B-EDEN_USDT",
                interval="1h",
                candles=series,
                indicators=latest_indicator_snapshot(series),
                features={},
            ),
            direction=SignalDirection.LONG,
            final_score=Decimal("-1"),
            open_pos={
                "entry_price": Decimal("100"),
                "opened_at_ms": series[0].close_time_ms,
                "metadata": {"bb_trail_enabled": True, "bb_entry_gate_passed": True},
            },
            fast=[Decimal("1") for _ in range(len(series))],
            slow=[Decimal("2") for _ in range(len(series))],
            rsi_values=[Decimal("30") for _ in range(len(series))],
            bands=bands,
            atr_values=[Decimal("4") for _ in range(len(series))],
            volume_sma=[Decimal("1000") for _ in range(len(series))],
        )

        self.assertFalse(allowed)
        self.assertEqual(reason, "signal_flip_ignored_bb_structure_intact")
        self.assertEqual(metadata["final_exit_reason"], "signal_flip_ignored_bb_structure_intact")
        self.assertFalse(metadata["signal_flip_bb_structure_failed"])

    def test_bb_managed_long_allows_flip_after_lower_band_breaks(self):
        series = _series_from_closes(
            [Decimal("100"), Decimal("99"), Decimal("98"), Decimal("97"), Decimal("95"), Decimal("89")]
        )
        bands = [
            BollingerBandPoint(
                middle=Decimal("100"),
                upper=Decimal("110"),
                lower=Decimal("90"),
                width_pct=Decimal("20"),
            )
            for _ in range(len(series))
        ]
        allowed, reason, metadata = self.strategy._check_signal_flip_exit(
            context=StrategyContext(
                pair="B-EDEN_USDT",
                interval="1h",
                candles=series,
                indicators=latest_indicator_snapshot(series),
                features={},
            ),
            direction=SignalDirection.LONG,
            final_score=Decimal("-1"),
            open_pos={
                "entry_price": Decimal("100"),
                "opened_at_ms": series[0].close_time_ms,
                "metadata": {"bb_trail_enabled": True, "bb_entry_gate_passed": True},
            },
            fast=[Decimal("1") for _ in range(len(series))],
            slow=[Decimal("2") for _ in range(len(series))],
            rsi_values=[Decimal("30") for _ in range(len(series))],
            bands=bands,
            atr_values=[Decimal("4") for _ in range(len(series))],
            volume_sma=[Decimal("1000") for _ in range(len(series))],
        )

        self.assertTrue(allowed)
        self.assertEqual(reason, "signal_flip_exit_confirmed_bb_structure_failed")
        self.assertTrue(metadata["signal_flip_bb_structure_failed"])

    def test_bb_managed_short_ignores_flip_until_upper_band_breaks(self):
        series = _series_from_closes(
            [Decimal("100"), Decimal("101"), Decimal("102"), Decimal("101"), Decimal("103"), Decimal("102")]
        )
        bands = [
            BollingerBandPoint(
                middle=Decimal("100"),
                upper=Decimal("110"),
                lower=Decimal("90"),
                width_pct=Decimal("20"),
            )
            for _ in range(len(series))
        ]
        allowed, reason, metadata = self.strategy._check_signal_flip_exit(
            context=StrategyContext(
                pair="B-EDEN_USDT",
                interval="1h",
                candles=series,
                indicators=latest_indicator_snapshot(series),
                features={},
            ),
            direction=SignalDirection.SHORT,
            final_score=Decimal("1"),
            open_pos={
                "entry_price": Decimal("100"),
                "opened_at_ms": series[0].close_time_ms,
                "metadata": {"bb_trail_enabled": True, "bb_entry_gate_passed": True},
            },
            fast=[Decimal("2") for _ in range(len(series))],
            slow=[Decimal("1") for _ in range(len(series))],
            rsi_values=[Decimal("70") for _ in range(len(series))],
            bands=bands,
            atr_values=[Decimal("4") for _ in range(len(series))],
            volume_sma=[Decimal("1000") for _ in range(len(series))],
        )

        self.assertFalse(allowed)
        self.assertEqual(reason, "signal_flip_ignored_bb_structure_intact")
        self.assertEqual(metadata["final_exit_reason"], "signal_flip_ignored_bb_structure_intact")
