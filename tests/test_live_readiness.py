from __future__ import annotations

import unittest
from decimal import Decimal
from unittest.mock import MagicMock, patch

from app.config import Settings, RiskSettings
from app.execution.live import LiveExecutionEngine, build_live_entry_order
from app.risk.models import RiskDecision, RiskContext, OpenPosition
from app.strategies.base import SignalAction, SignalDirection, StrategySignal
from app.live.live_loop import LiveTradingLoop

def _signal(
    *,
    action: SignalAction = SignalAction.ENTER_LONG,
    direction: SignalDirection = SignalDirection.LONG,
    stop_loss: Decimal | None = Decimal("95"),
    entry_price: Decimal = Decimal("100"),
) -> StrategySignal:
    return StrategySignal(
        strategy_name="test",
        pair="B-BTC_USDT",
        interval="1m",
        action=action,
        direction=direction,
        confidence=Decimal("1"),
        reason="test",
        timestamp_ms=1,
        entry_price=entry_price,
        stop_loss=stop_loss,
        take_profit=Decimal("110"),
    )

def _decision(signal: StrategySignal | None = None, position_size: Decimal = Decimal("2")) -> RiskDecision:
    s = signal or _signal()
    return RiskDecision(
        approved=True,
        reason="approved",
        signal=s,
        position_size=position_size,
        notional=position_size * s.entry_price,
        leverage=Decimal("2"),
        max_loss=Decimal("10"),
    )

class LiveReadinessTests(unittest.TestCase):
    def test_real_live_blocked_without_confirmation(self) -> None:
        settings = Settings(
            trading_mode="live",
            live_trading_enabled=True,
            live_pilot_dry_run=False,
            live_confirm_i_understand_risk="NO", # BLOCKED
            coindcx_api_key="key",
            coindcx_api_secret="secret"
        )
        # Testing validation in load_settings indirectly if we could, 
        # but here we test the engine's check.
        engine = LiveExecutionEngine(MagicMock(), settings, dry_run=False)
        report = engine.process_decision(_decision())
        self.assertFalse(report.accepted)
        self.assertIn("LIVE_CONFIRM_I_UNDERSTAND_RISK", report.reason)

    def test_kill_switch_blocks_entries(self) -> None:
        risk_settings = RiskSettings(live_kill_switch=True)
        settings = Settings(risk=risk_settings)
        # The loop should check this
        loop = LiveTradingLoop(settings)
        loop.kill_switch_active = True
        
        # Manually trigger process_trading_candle or handle_signal
        with patch.object(loop, '_handle_signal') as mock_handle:
             loop._process_trading_candle(MagicMock(is_closed=True))
             mock_handle.assert_not_called()

    def test_live_max_order_notional_blocks_oversized_order(self) -> None:
        risk_settings = RiskSettings(
            live_max_order_notional=Decimal("1000"),
            live_risk_approval_enabled=True # MUST BE ENABLED
        )
        settings = Settings(risk=risk_settings, trading_mode="live", live_trading_enabled=True)
        from app.risk.manager import RiskManager
        manager = RiskManager(risk_settings)
        
        oversized_decision_context = RiskContext(
            signal=_signal(entry_price=Decimal("100")),
            account_equity=Decimal("10000"),
            available_equity=Decimal("10000"),
            trading_mode="live",
            live_trading_enabled=True
        )
        
        decision = manager.evaluate(oversized_decision_context)
        self.assertFalse(decision.approved)
        self.assertIn("live_notional_cap_blocked", decision.reason)

    def test_missing_stop_loss_blocks_live_entry(self) -> None:
        with self.assertRaises(ValueError):
            build_live_entry_order(_decision(_signal(stop_loss=None)))

    def test_profit_lock_accounting_win(self) -> None:
        settings = Settings()
        from dataclasses import replace
        settings = replace(settings, risk=replace(settings.risk, compound_profits=False))
        
        loop = LiveTradingLoop(settings)
        loop.starting_equity = Decimal("100000")
        loop.local_state["tradable_base"] = "100000"
        loop.local_state["locked_profit"] = "0"
        loop.local_state["daily_loss_from_tradable_base"] = "0"
        
        # Simulate +15,000 profit
        loop._update_accounting_balances(Decimal("15000"))
        
        self.assertEqual(loop.local_state["locked_profit"], "15000")
        self.assertEqual(loop.local_state["tradable_base"], "100000")

    def test_profit_lock_accounting_loss(self) -> None:
        loop = LiveTradingLoop(Settings())
        loop.local_state["tradable_base"] = "100000"
        loop.local_state["locked_profit"] = "15000"
        loop.local_state["daily_loss_from_tradable_base"] = "0"
        
        # Simulate -5,000 loss
        loop._update_accounting_balances(Decimal("-5000"))
        
        self.assertEqual(loop.local_state["locked_profit"], "15000")
        self.assertEqual(loop.local_state["tradable_base"], "95000")
        self.assertEqual(loop.local_state["daily_loss_from_tradable_base"], "5000")

    def test_stop_can_only_tighten(self) -> None:
        # Phase 5 logic in _apply_profit_protection
        settings = Settings(live_pilot_dry_run=False, live_trading_enabled=True)
        loop = LiveTradingLoop(settings)
        # Ensure clean state for test
        loop.kill_switch_active = False
        loop.local_state["kill_switch_active"] = False
        
        loop.local_state["positions"][loop.pairs[0]] = {
            "active_pos": "1",
            "avg_price": "100",
            "stop_loss_trigger": "90"
        }
        # Initialize position_metadata dictionary
        loop.local_state["position_metadata"] = {}
        loop.local_state["position_metadata"][loop.pairs[0]] = {
            "initial_entry_price": "100",
            "initial_stop_loss": "90",
            "initial_r": "10",
            "max_r_hit": "1.5" # Should be at +1.2R tier -> lock +0.25R (92.5)
        }
        
        with patch.object(loop.execution_engine, 'update_tpsl') as mock_update:
             mock_handle = MagicMock()
             mock_update.return_value = MagicMock(accepted=True)
             
             # Current stop 90. Logic wants to move to 102.5.
             loop._apply_profit_protection(MagicMock(close=Decimal("115"), high=Decimal("115"), low=Decimal("114"), pair=loop.pairs[0]))
             mock_update.assert_called()
             self.assertEqual(mock_update.call_args[1]['stop_loss'], Decimal("102.5"))
             
             # Now try to loosen it (should be blocked by code safety)
             # If we manually set logic to try to move to 91
             mock_update.reset_mock()
             loop.local_state["positions"][loop.pairs[0]]["stop_loss_trigger"] = "103"
             # max_r is still 1.5, so target is 102.5. 102.5 < 103 (loosening for LONG).
             loop._apply_profit_protection(MagicMock(close=Decimal("115"), high=Decimal("115"), low=Decimal("114"), pair=loop.pairs[0]))
             mock_update.assert_not_called()

    def test_reconcile_blocks_entries_if_stop_loss_missing(self) -> None:
        settings = Settings(live_trading_enabled=True, trading_mode="live")
        loop = LiveTradingLoop(settings, starting_equity=Decimal("2000"))
        
        # Mock exchange having a position without SL
        mock_pos = MagicMock(is_open=True, has_stop_loss=False, pair=loop.pairs[0])
        # To simulate a BOT-OWNED position, it MUST be in local_state already
        loop.local_state["positions"][loop.pairs[0]] = {
            "pair": loop.pairs[0],
            "active_pos": "1",
            "source": "exchange",
            "manual_entry": False
        }
        mock_pos.to_dict.return_value = loop.local_state["positions"][loop.pairs[0]]
        
        with patch.object(loop.execution_engine.sync, 'fetch_position', return_value=mock_pos):
             loop.reconcile()
             self.assertTrue(loop.kill_switch_active)
             self.assertTrue(loop.local_state["kill_switch_active"])

if __name__ == "__main__":
    unittest.main()
