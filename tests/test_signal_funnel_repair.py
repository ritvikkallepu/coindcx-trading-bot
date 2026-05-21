from __future__ import annotations

import unittest
from decimal import Decimal
from dataclasses import replace

from app.backtest.engine import BacktestEngine
from app.backtest.models import BacktestConfig, backtest_signal_funnel
from app.data.candle_builder import OHLCVCandle
from app.risk.manager import RiskManager
from app.broker.models import PaperExecutionReport, PaperAccountSnapshot, PaperExecutionStatus, PaperOrder, PaperOrderSide, PaperOrderStatus
from app.strategies.base import SignalAction, SignalDirection, SignalFunnelReason, Strategy, StrategyEngine, StrategySignal


class SignalFunnelRepairTests(unittest.TestCase):
    def test_below_entry_threshold_appears_in_funnel(self) -> None:
        config = BacktestConfig(pair="BTC", interval="1h", starting_equity=Decimal("1000"), leverage=Decimal("1"))
        
        # Signal marked as raw but with BELOW_ENTRY_THRESHOLD funnel reason
        signal = StrategySignal(
            strategy_name="test", pair="BTC", interval="1h", 
            action=SignalAction.HOLD, direction=SignalDirection.LONG, 
            confidence=Decimal("0"), reason="Weak score", timestamp_ms=1,
            metadata={
                "signal_funnel_raw_candidate": True, 
                "signal_funnel_raw_direction": "long",
                "funnel_reason": SignalFunnelReason.BELOW_ENTRY_THRESHOLD
            }
        )
        
        account = PaperAccountSnapshot(Decimal("1000"), Decimal("0"), Decimal("0"), Decimal("0"), Decimal("1000"), 0, Decimal("0"))
        reports = [PaperExecutionReport(accepted=False, status=PaperExecutionStatus.REJECTED, reason="Weak", account=account, signal=signal)]
        
        funnel = backtest_signal_funnel(config=config, reports=reports, trades=[], candles_loaded=10, candles_used=10)
        self.assertEqual(funnel["block_reasons"][SignalFunnelReason.BELOW_ENTRY_THRESHOLD.value], 1)
        self.assertEqual(funnel["explained_rejections_total"], 1)
        self.assertEqual(funnel["raw_candidates_total"], 1)

    def test_existing_position_blocked_appears_in_funnel(self) -> None:
        config = BacktestConfig(pair="BTC", interval="1h", starting_equity=Decimal("1000"), leverage=Decimal("1"))
        
        # Case 1: Explicit funnel reason
        signal1 = StrategySignal(
            strategy_name="test", pair="BTC", interval="1h", 
            action=SignalAction.HOLD, direction=SignalDirection.LONG, 
            confidence=Decimal("0"), reason="Already long", timestamp_ms=1,
            metadata={
                "signal_funnel_raw_candidate": True, 
                "signal_funnel_raw_direction": "long",
                "funnel_reason": SignalFunnelReason.EXISTING_POSITION_BLOCKED
            }
        )
        
        # Case 2: Textual fallback
        signal2 = StrategySignal(
            strategy_name="test", pair="BTC", interval="1h", 
            action=SignalAction.HOLD, direction=SignalDirection.LONG, 
            confidence=Decimal("0"), reason="Already long", timestamp_ms=2,
            metadata={
                "signal_funnel_raw_candidate": True, 
                "signal_funnel_raw_direction": "long"
                # missing funnel_reason
            }
        )
        
        account = PaperAccountSnapshot(Decimal("1000"), Decimal("0"), Decimal("0"), Decimal("0"), Decimal("1000"), 0, Decimal("0"))
        reports = [
            PaperExecutionReport(accepted=False, status=PaperExecutionStatus.REJECTED, reason="Already long", account=account, signal=signal1),
            PaperExecutionReport(accepted=False, status=PaperExecutionStatus.REJECTED, reason="Already long", account=account, signal=signal2)
        ]
        
        funnel = backtest_signal_funnel(config=config, reports=reports, trades=[], candles_loaded=10, candles_used=10)
        self.assertEqual(funnel["block_reasons"][SignalFunnelReason.EXISTING_POSITION_BLOCKED.value], 2)
        self.assertEqual(funnel["raw_candidates_total"], 2)

    def test_unmapped_strategy_hold_increments_strategy_hold_unmapped(self) -> None:
        config = BacktestConfig(pair="BTC", interval="1h", starting_equity=Decimal("1000"), leverage=Decimal("1"))
        
        # Strategy HOLD that doesn't match any textual fallback and has no funnel_reason
        signal = StrategySignal(
            strategy_name="test", pair="BTC", interval="1h", 
            action=SignalAction.HOLD, direction=SignalDirection.LONG, 
            confidence=Decimal("0"), reason="Some obscure strategy reason", timestamp_ms=1,
            metadata={
                "signal_funnel_raw_candidate": True, 
                "signal_funnel_raw_direction": "long"
            }
        )
        
        account = PaperAccountSnapshot(Decimal("1000"), Decimal("0"), Decimal("0"), Decimal("0"), Decimal("1000"), 0, Decimal("0"))
        reports = [PaperExecutionReport(accepted=False, status=PaperExecutionStatus.REJECTED, reason="Obscure", account=account, signal=signal)]
        
        funnel = backtest_signal_funnel(config=config, reports=reports, trades=[], candles_loaded=10, candles_used=10)
        self.assertEqual(funnel["block_reasons"][SignalFunnelReason.STRATEGY_HOLD_UNMAPPED.value], 1)
        self.assertEqual(funnel["explained_rejections_total"], 1)

    def test_daily_loss_guard_rejection_appears_in_funnel(self) -> None:
        config = BacktestConfig(pair="BTC", interval="1h", starting_equity=Decimal("1000"), leverage=Decimal("1"))
        
        signal = StrategySignal(
            strategy_name="test", pair="BTC", interval="1h", 
            action=SignalAction.ENTER_LONG, direction=SignalDirection.LONG, 
            confidence=Decimal("1"), reason="Entry", timestamp_ms=1,
            metadata={"signal_funnel_raw_candidate": True, "signal_funnel_raw_direction": "long"}
        )
        
        account = PaperAccountSnapshot(Decimal("1000"), Decimal("0"), Decimal("0"), Decimal("0"), Decimal("1000"), 0, Decimal("0"))
        reports = [
            PaperExecutionReport(accepted=False, status=PaperExecutionStatus.REJECTED, reason="Risk decision rejected: Max daily loss reached", account=account, signal=signal)
        ]
        
        funnel = backtest_signal_funnel(config=config, reports=reports, trades=[], candles_loaded=10, candles_used=10)
        self.assertEqual(funnel["block_reasons"][SignalFunnelReason.DAILY_LOSS_GUARD_BLOCKED.value], 1)

if __name__ == "__main__":
    unittest.main()
