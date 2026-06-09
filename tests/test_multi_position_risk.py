from __future__ import annotations

import unittest
from decimal import Decimal
from app.config import RiskSettings
from app.risk.manager import RiskManager
from app.risk.models import RiskContext, InstrumentMetadata, OpenPosition
from app.strategies.base import StrategySignal, SignalAction, SignalDirection

class TestMultiPositionRisk(unittest.TestCase):
    def setUp(self) -> None:
        self.risk_settings = RiskSettings(
            max_open_positions=3,
            max_open_positions_per_pair=1,
            allow_multi_pair_positions=True,
            allow_same_pair_pyramiding=False,
            max_margin_usage_pct=Decimal("50.0"),
            max_risk_per_trade_pct=Decimal("1.0")
        )
        self.risk_manager = RiskManager(self.risk_settings)
        self.instrument = InstrumentMetadata(
            pair="B-SOL_USDT",
            min_quantity=Decimal("0.1"),
            quantity_step=Decimal("0.1"),
            min_notional=Decimal("10"),
            tick_size=Decimal("0.01")
        )

    def test_btc_open_does_not_block_sol(self) -> None:
        # 1. Open BTC position
        open_positions = [
            OpenPosition(
                pair="B-BTC_USDT",
                direction=SignalDirection.LONG,
                quantity=Decimal("0.01"),
                entry_price=Decimal("50000"),
                leverage=Decimal("10"),
                stop_loss=Decimal("49000")
            )
        ]
        
        # 2. Evaluate SOL signal
        signal = StrategySignal(
            strategy_name="test_strat",
            pair="B-SOL_USDT",
            interval="15m",
            action=SignalAction.ENTER_LONG,
            confidence=Decimal("1"),
            direction=SignalDirection.LONG,
            entry_price=Decimal("100"),
            stop_loss=Decimal("90"),
            take_profit=Decimal("120"),
            timestamp_ms=1000,
            reason="test"
        )
        
        decision = self.risk_manager.evaluate_signal(
            signal,
            account_equity=Decimal("10000"),
            available_equity=Decimal("10000"),
            open_positions=tuple(open_positions),
            daily_realized_pnl=Decimal("0"),
            trading_mode="paper"
        )
        
        self.assertTrue(decision.approved, f"Should allow SOL entry while BTC is open. Reason: {decision.reason}")

    def test_btc_open_blocks_second_btc(self) -> None:
        # 1. Open BTC position
        open_positions = [
            OpenPosition(
                pair="B-BTC_USDT",
                direction=SignalDirection.LONG,
                quantity=Decimal("0.01"),
                entry_price=Decimal("50000"),
                leverage=Decimal("10"),
                stop_loss=Decimal("49000")
            )
        ]
        
        # 2. Evaluate BTC signal
        signal = StrategySignal(
            strategy_name="test_strat",
            pair="B-BTC_USDT",
            interval="15m",
            action=SignalAction.ENTER_LONG,
            confidence=Decimal("1"),
            direction=SignalDirection.LONG,
            entry_price=Decimal("51000"),
            timestamp_ms=1000,
            reason="test"
        )
        
        decision = self.risk_manager.evaluate_signal(
            signal,
            account_equity=Decimal("1000"),
            available_equity=Decimal("1000"),
            open_positions=tuple(open_positions),
            daily_realized_pnl=Decimal("0"),
            trading_mode="paper"
        )
        
        self.assertFalse(decision.approved)
        self.assertIn("same_pair_position_blocked", decision.reason)

    def test_max_open_positions_does_not_limit_distinct_pairs(self) -> None:
        # 1. Open 3 different-pair positions (old max)
        open_positions = [
            OpenPosition(pair="B-BTC_USDT", direction=SignalDirection.LONG, quantity=Decimal("1"), entry_price=Decimal("1"), leverage=Decimal("1")),
            OpenPosition(pair="B-SOL_USDT", direction=SignalDirection.LONG, quantity=Decimal("1"), entry_price=Decimal("1"), leverage=Decimal("1")),
            OpenPosition(pair="B-BSB_USDT", direction=SignalDirection.LONG, quantity=Decimal("1"), entry_price=Decimal("1"), leverage=Decimal("1")),
        ]
        
        # 2. Evaluate 4th different coin. This should be controlled by
        # margin/risk caps, not by the old global position count.
        signal = StrategySignal(
            strategy_name="test_strat",
            pair="B-ETH_USDT",
            interval="15m",
            action=SignalAction.ENTER_LONG,
            confidence=Decimal("1"),
            direction=SignalDirection.LONG,
            entry_price=Decimal("2000"),
            stop_loss=Decimal("1900"),
            take_profit=Decimal("2200"),
            timestamp_ms=1000,
            reason="test"
        )
        
        decision = self.risk_manager.evaluate_signal(
            signal,
            account_equity=Decimal("10000"),
            available_equity=Decimal("10000"),
            open_positions=tuple(open_positions),
            daily_realized_pnl=Decimal("0"),
            trading_mode="paper"
        )
        
        self.assertTrue(decision.approved, decision.reason)

    def test_margin_usage_limit(self) -> None:
        # Max margin % = 50%
        # Equity = 1000 -> Max margin allowed = 500
        
        # 1. Open position using 400 margin
        open_positions = [
            OpenPosition(
                pair="B-BTC_USDT",
                direction=SignalDirection.LONG,
                quantity=Decimal("0.08"), # 0.08 * 50000 = 4000 notional
                entry_price=Decimal("50000"),
                leverage=Decimal("10"), # 4000 / 10 = 400 margin
            )
        ]
        
        # 2. Try to open another position requiring 200 margin (Total 600 > 500)
        signal = StrategySignal(
            strategy_name="test_strat",
            pair="B-SOL_USDT",
            interval="15m",
            action=SignalAction.ENTER_LONG,
            confidence=Decimal("1"),
            direction=SignalDirection.LONG,
            entry_price=Decimal("100"),
            stop_loss=Decimal("95"), # 5% risk
            timestamp_ms=1000,
            reason="test"
        )
        
        self.risk_settings = RiskSettings(
            max_open_positions=3,
            max_margin_usage_pct=Decimal("50.0"),
            max_risk_per_trade_pct=Decimal("20.0")
        )
        self.risk_manager = RiskManager(self.risk_settings)

        decision = self.risk_manager.evaluate_signal(
            signal,
            account_equity=Decimal("1000"),
            available_equity=Decimal("1000"),
            open_positions=tuple(open_positions),
            daily_realized_pnl=Decimal("0"),
            trading_mode="paper"
        )
        
        self.assertFalse(decision.approved)
        self.assertIn("margin_usage_blocked", decision.reason)

if __name__ == "__main__":
    unittest.main()
