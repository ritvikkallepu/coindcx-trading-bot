from __future__ import annotations

import unittest
from decimal import Decimal
from dataclasses import dataclass, replace

from app.backtest.engine import BacktestEngine, _BacktestSafetyState, _entry_blocked_by_loss_cooldown, _apply_entry_safety_filter
from app.backtest.models import BacktestConfig, BacktestEquityPoint, BacktestTrade, backtest_report_diagnostics
from app.broker.paper import PaperBroker
from app.data.candle_builder import OHLCVCandle, CandleSeries
from app.risk.models import RiskDecision
from app.strategies.base import SignalAction, SignalDirection, StrategySignal, StrategyEngine, StrategyContext

def _candle(close: Decimal, high: Decimal | None = None, low: Decimal | None = None, open_price: Decimal | None = None, timestamp_ms: int = 0) -> OHLCVCandle:
    return OHLCVCandle(
        pair="B-BTC_USDT",
        interval="5m",
        open_time_ms=timestamp_ms,
        close_time_ms=timestamp_ms + 300000,
        open=open_price or close,
        high=high or close,
        low=low or close,
        close=close,
        volume=Decimal("100"),
    )

def _config(**kwargs) -> BacktestConfig:
    defaults = {
        "pair": "B-BTC_USDT",
        "interval": "5m",
        "starting_equity": Decimal("1000"),
        "leverage": Decimal("3"),
        "stop_loss_cooldown_candles": 0,
        "loss_cooldown_candles": 0,
        "atr_entry_filter_enabled": True,
    }
    return BacktestConfig(**{**defaults, **kwargs})

@dataclass
class MockIndicators:
    atr: Decimal | None = None

class EnhancedFeaturesTests(unittest.TestCase):
    def test_equity_giveback_guard(self) -> None:
        config = _config(
            equity_giveback_guard_enabled=True,
            equity_giveback_threshold_pct=Decimal("0.05"),
            equity_giveback_cooldown_candles=10,
        )
        state = _BacktestSafetyState(config=config, interval_ms=300000, peak_equity=Decimal("1000"))
        state.observe_candle(_candle(Decimal("1200"), timestamp_ms=0), Decimal("1200"))
        state.observe_candle(_candle(Decimal("1100"), timestamp_ms=300000), Decimal("1100"))
        self.assertTrue(state.active(600000))

    def test_post_spike_cooldown(self) -> None:
        config = _config(
            post_spike_cooldown_enabled=True,
            post_spike_lookback_candles=2,
            post_spike_gain_threshold_pct=Decimal("0.10"),
            post_spike_cooldown_candles=5,
        )
        state = _BacktestSafetyState(config=config, interval_ms=300000, peak_equity=Decimal("1000"))
        state.observe_candle(_candle(Decimal("1000"), timestamp_ms=0), Decimal("1000"))
        state.observe_candle(_candle(Decimal("1000"), timestamp_ms=300000), Decimal("1000"))
        state.observe_candle(_candle(Decimal("1200"), timestamp_ms=600000), Decimal("1200"))
        self.assertTrue(state.active(900000))

    def test_loss_streak_cooldown_consecutive(self) -> None:
        config = _config(
            loss_streak_cooldown_enabled=True,
            consecutive_loss_limit=2,
            loss_streak_cooldown_candles=5,
        )
        state = _BacktestSafetyState(config=config, interval_ms=300000, peak_equity=Decimal("1000"))
        t = lambda pnl: BacktestTrade("B-BTC_USDT", "S", SignalDirection.LONG, Decimal("1"), Decimal("100"), Decimal("100"), 0, 300000, pnl, Decimal("0"), pnl, "stop")
        state.observe_trade(t(Decimal("-10")))
        state.observe_trade(t(Decimal("-10")))
        self.assertTrue(state.active(600000))

    def test_breakeven_long(self) -> None:
        broker = PaperBroker(starting_equity=Decimal("10000"))
        signal = StrategySignal(
            strategy_name="S",
            pair="B-BTC_USDT",
            interval="5m",
            action=SignalAction.ENTER_LONG,
            direction=SignalDirection.LONG,
            confidence=Decimal("1"),
            reason="test",
            timestamp_ms=0,
            entry_price=Decimal("100"),
            stop_loss=Decimal("98"),
            take_profit=Decimal("200"),
            metadata={"atr_dynamic_exits_enabled": True}
        )
        decision = RiskDecision(approved=True, reason="test", signal=signal, position_size=Decimal("1"))
        report = broker.execute_decision(decision, market_price=Decimal("100"), timestamp_ms=0)
        self.assertTrue(report.accepted, f"Entry rejected: {report.reason}")
        
        broker.update_dynamic_atr_exits(
            _candle(Decimal("102"), timestamp_ms=300000),
            atr=Decimal("1"), stop_multiple=Decimal("5"), take_profit_multiple=Decimal("10"),
            breakeven_enabled=True, breakeven_activation_r=Decimal("1.0")
        )
        pos = broker.open_positions()[0]
        self.assertEqual(pos.stop_loss, Decimal("100"))

    def test_profit_lock_short(self) -> None:
        broker = PaperBroker(starting_equity=Decimal("10000"))
        signal = StrategySignal(
            strategy_name="S",
            pair="B-BTC_USDT",
            interval="5m",
            action=SignalAction.ENTER_SHORT,
            direction=SignalDirection.SHORT,
            confidence=Decimal("1"),
            reason="test",
            timestamp_ms=0,
            entry_price=Decimal("100"),
            stop_loss=Decimal("110"),
            take_profit=Decimal("50"),
            metadata={"atr_dynamic_exits_enabled": True}
        )
        decision = RiskDecision(approved=True, reason="test", signal=signal, position_size=Decimal("1"))
        report = broker.execute_decision(decision, market_price=Decimal("100"), timestamp_ms=0)
        self.assertTrue(report.accepted, f"Entry rejected: {report.reason}")
        
        broker.update_dynamic_atr_exits(
            _candle(Decimal("85"), timestamp_ms=300000),
            atr=Decimal("1"), stop_multiple=Decimal("10"), take_profit_multiple=Decimal("5"),
            profit_lock_enabled=True, profit_lock_activation_r=Decimal("1.5"), profit_lock_r=Decimal("0.5")
        )
        pos = broker.open_positions()[0]
        self.assertEqual(pos.stop_loss, Decimal("95"))

    def test_chop_filter_atr(self) -> None:
        config = _config(chop_filter_enabled=True, block_low_atr_enabled=True, min_atr_pct=Decimal("0.01"))
        signal = StrategySignal(
            strategy_name="S",
            pair="B-BTC_USDT",
            interval="5m",
            action=SignalAction.ENTER_LONG,
            direction=SignalDirection.LONG,
            confidence=Decimal("1"),
            reason="test",
            timestamp_ms=0
        )
        decision = RiskDecision(approved=True, reason="test", signal=signal)
        
        series = CandleSeries()
        latest = _candle(Decimal("100"))
        ind = MockIndicators(atr=Decimal("0.5"))
        
        filtered = _apply_entry_safety_filter(decision, series=series, latest_candle=latest, indicators=ind, config=config)
        self.assertFalse(filtered.approved, "Should be blocked by low ATR")
        self.assertEqual(filtered.reason, "atr_too_low")

    def test_diagnostics_dependency(self) -> None:
        t = lambda pnl: BacktestTrade("B-BTC_USDT", "S", SignalDirection.LONG, Decimal("1"), Decimal("100"), Decimal("100"), 0, 0, pnl, Decimal("0"), pnl, "tp" if pnl > 0 else "stop")
        trades = [t(Decimal("100")), t(Decimal("50")), t(Decimal("10")), t(Decimal("10")), t(Decimal("10")), t(Decimal("1"))]
        diag = backtest_report_diagnostics([], trades=trades)
        self.assertEqual(diag["dependency"]["top_5_winners_total_pnl"], Decimal("180"))

if __name__ == "__main__":
    unittest.main()
