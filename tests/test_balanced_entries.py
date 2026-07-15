from __future__ import annotations
import unittest
from decimal import Decimal
from app.data.candle_builder import CandleSeries, OHLCVCandle
from app.data.indicators import latest_indicator_snapshot
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
                pair="B-SOL_USDT",
                interval=interval,
                open_time_ms=index * 3600000,
                close_time_ms=((index + 1) * 3600000) - 1,
                open=close,
                high=close * Decimal("1.005"),
                low=close * Decimal("0.995"),
                close=close,
                volume=Decimal("1000"),
            )
        )
    return CandleSeries(candles)

class TestBalancedEntries(unittest.TestCase):
    def setUp(self):
        self.strategy = HybridMetaStrategy(
            balanced_breakout_enabled=True,
            pullback_entry_enabled=True,
            late_chase_block_enabled=True
        )

    def test_long_balanced_breakout_enters_on_valid_breakout(self):
        closes = [Decimal("100") + (i % 5) for i in range(60)]
        exec_closes = [Decimal("104"), Decimal("104.5"), Decimal("106")]
        exec_candles = []
        for i, c in enumerate(exec_closes):
             exec_candles.append(OHLCVCandle(
                 pair="B-SOL_USDT", interval="1m", 
                 open_time_ms=60*3600000 + i*60000, close_time_ms=60*3600000 + (i+1)*60000-1,
                 open=c*Decimal("0.999"), high=c*Decimal("1.001"), low=c*Decimal("0.998"), close=c, 
                 volume=Decimal("1000") if i < 2 else Decimal("5000")
             ))
        series = _series_from_closes(closes)
        features = {
            "backtest_config": {
                "previous_parent_high": Decimal("105"),
                "previous_parent_low": Decimal("99"),
                "balanced_breakout_enabled": True,
                "balanced_breakout_volume_ratio_min": Decimal("1.1"),
                "balanced_breakout_body_ratio_min": Decimal("0.1"),
                "false_breakout_filter_enabled": True,
            },
            "execution_candles": exec_candles
        }
        context = StrategyContext(pair="B-SOL_USDT", interval="1h", candles=series,
            indicators=latest_indicator_snapshot(series), features=features)
        signal = self.strategy.evaluate(context)
        self.assertEqual(signal.action, SignalAction.ENTER_LONG, signal.reason)
        self.assertEqual(signal.metadata.get("entry_type"), "balanced_breakout")

    def test_late_long_chase_is_blocked(self):
        closes = [Decimal("100") for i in range(60)]
        # We need 4 consecutive impulse candles.
        # Child 0: 98 (neutral)
        # Child 1: 99 > 98 (impulse 1)
        # Child 2: 100 > 99 (impulse 2)
        # Child 3: 106 > 100 (impulse 3, breakout! age 0)
        # Child 4: 107 > 106 (impulse 4, breakout! age 1)
        exec_closes = [Decimal("98"), Decimal("99"), Decimal("100"), Decimal("106"), Decimal("107")]
        exec_opens = [Decimal("98"), Decimal("98.5"), Decimal("99.5"), Decimal("105"), Decimal("106.5")]
        
        exec_candles = []
        for i, c in enumerate(exec_closes):
             exec_candles.append(OHLCVCandle(
                 pair="B-SOL_USDT", interval="1m", 
                 open_time_ms=60*3600000 + i*60000, close_time_ms=60*3600000 + (i+1)*60000-1,
                 open=exec_opens[i], high=c+Decimal("0.1"), low=exec_opens[i]-Decimal("0.1"), close=c, volume=Decimal("5000")
             ))
        series = _series_from_closes(closes)
        features = {
            "backtest_config": {
                "previous_parent_high": Decimal("105"),
                "balanced_breakout_enabled": True,
                "balanced_breakout_volume_ratio_min": Decimal("1.0"),
                "balanced_breakout_body_ratio_min": Decimal("0.1"),
                "balanced_breakout_max_extension_atr": Decimal("50.0"),
                "late_chase_block_enabled": True,
                "late_chase_max_extension_atr": Decimal("50.0"),
                "late_chase_max_consecutive_impulse_candles": 3,
            },
            "execution_candles": exec_candles
        }
        context = StrategyContext(pair="B-SOL_USDT", interval="1h", candles=series,
            indicators=latest_indicator_snapshot(series), features=features)
        signal = self.strategy.evaluate(context)
        self.assertEqual(signal.action, SignalAction.HOLD)
        self.assertIn("late_chase_consecutive_impulse", signal.reason)

    def test_pullback_continuation_long_enters(self):
        closes = [Decimal("100") + (Decimal(i) * Decimal("0.1")) for i in range(60)]
        exec_closes = [Decimal("106"), Decimal("105.1"), Decimal("105.2"), Decimal("107")]
        exec_candles = []
        for i, c in enumerate(exec_closes):
             exec_candles.append(OHLCVCandle(
                 pair="B-SOL_USDT", interval="1m", 
                 open_time_ms=60*3600000 + i*60000, close_time_ms=60*3600000 + (i+1)*60000-1,
                 open=c-Decimal("0.1"), 
                 high=c+Decimal("0.1"), low=c-Decimal("0.1"), close=c, volume=Decimal("2000")
             ))
        series = _series_from_closes(closes)
        features = {
            "backtest_config": {
                "previous_parent_high": Decimal("105"),
                "pullback_entry_enabled": True,
                "pullback_max_distance_from_ema_atr": Decimal("50.0"),
                "pullback_resume_body_ratio_min": Decimal("0.1"),
                "balanced_breakout_enabled": False, 
            },
            "execution_candles": exec_candles
        }
        context = StrategyContext(pair="B-SOL_USDT", interval="1h", candles=series,
            indicators=latest_indicator_snapshot(series), features=features)
        signal = self.strategy.evaluate(context)
        self.assertEqual(signal.action, SignalAction.ENTER_LONG, signal.reason)
        self.assertEqual(signal.metadata.get("entry_type"), "pullback_continuation")

    def test_short_balanced_breakdown_enters(self):
        closes = [Decimal("100") - (i % 5) for i in range(60)]
        exec_closes = [Decimal("96"), Decimal("95.5"), Decimal("94")]
        exec_candles = []
        for i, c in enumerate(exec_closes):
             exec_candles.append(OHLCVCandle(
                 pair="B-SOL_USDT", interval="1m", 
                 open_time_ms=60*3600000 + i*60000, close_time_ms=60*3600000 + (i+1)*60000-1,
                 open=c*Decimal("1.001"), high=c*Decimal("1.002"), low=c*Decimal("0.999"), close=c, 
                 volume=Decimal("1000") if i < 2 else Decimal("5000")
             ))
        series = _series_from_closes(closes)
        features = {
            "backtest_config": {
                "previous_parent_low": Decimal("95"),
                "previous_parent_high": Decimal("101"),
                "balanced_breakout_enabled": True,
                "balanced_breakout_volume_ratio_min": Decimal("1.1"),
                "balanced_breakout_body_ratio_min": Decimal("0.1"),
            },
            "execution_candles": exec_candles
        }
        context = StrategyContext(pair="B-SOL_USDT", interval="1h", candles=series,
            indicators=latest_indicator_snapshot(series), features=features)
        signal = self.strategy.evaluate(context)
        self.assertEqual(signal.action, SignalAction.ENTER_SHORT, signal.reason)
        self.assertEqual(signal.metadata.get("entry_type"), "balanced_breakout")

    def test_short_pullback_continuation_enters(self):
        closes = [Decimal("100") - (Decimal(i) * Decimal("0.1")) for i in range(60)]
        exec_closes = [Decimal("94"), Decimal("94.9"), Decimal("94.8"), Decimal("93")]
        exec_candles = []
        for i, c in enumerate(exec_closes):
             exec_candles.append(OHLCVCandle(
                 pair="B-SOL_USDT", interval="1m", 
                 open_time_ms=60*3600000 + i*60000, close_time_ms=60*3600000 + (i+1)*60000-1,
                 open=c+Decimal("0.1") if i != 1 else c-Decimal("0.1"), 
                 high=c+Decimal("0.1"), low=c-Decimal("0.1"), close=c, volume=Decimal("2000")
             ))
        series = _series_from_closes(closes)
        features = {
            "backtest_config": {
                "previous_parent_low": Decimal("95"),
                "pullback_entry_enabled": True,
                "pullback_max_distance_from_ema_atr": Decimal("50.0"),
                "pullback_resume_body_ratio_min": Decimal("0.1"),
                "balanced_breakout_enabled": False, 
            },
            "execution_candles": exec_candles
        }
        context = StrategyContext(pair="B-SOL_USDT", interval="1h", candles=series,
            indicators=latest_indicator_snapshot(series), features=features)
        signal = self.strategy.evaluate(context)
        self.assertEqual(signal.action, SignalAction.ENTER_SHORT, signal.reason)
        self.assertEqual(signal.metadata.get("entry_type"), "pullback_continuation")
