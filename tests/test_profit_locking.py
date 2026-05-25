from __future__ import annotations
import unittest
from decimal import Decimal
from unittest.mock import MagicMock
from dataclasses import replace
from app.broker.paper import PaperBroker
from app.data.candle_builder import OHLCVCandle
from app.risk.manager import RiskManager
from app.config import RiskSettings
from app.strategies.base import StrategySignal, SignalAction, SignalDirection

class TestProfitLocking(unittest.TestCase):
    def setUp(self):
        self.risk_settings = RiskSettings(
            max_risk_per_trade_pct=Decimal("4"),
            max_daily_loss_pct=Decimal("10"),
            maker_fee_rate=Decimal("0"),
            taker_fee_rate=Decimal("0"),
            fee_gst_rate=Decimal("0"),
        )
        self.risk_manager = RiskManager(self.risk_settings)
        self.broker = PaperBroker(starting_equity=Decimal("100000"))
        # Ensure profit locking is on (default)
        self.broker.profit_lock_enabled = True
        self.broker.auto_lock_profit_pct = Decimal("100")

    def test_win_then_loss_accounting(self):
        # A. Initial 100,000, Win +15,000
        from app.broker.models import PaperPosition
        self.broker.positions["B-BTC_USDT"] = PaperPosition(
            strategy_name="test",
            pair="B-BTC_USDT",
            direction=SignalDirection.LONG,
            entry_price=Decimal("50000"),
            quantity=Decimal("1"),
            leverage=Decimal("3"),
            opened_at_ms=500,
            updated_at_ms=500,
            metadata={"total_fee": Decimal("0")},
            quote_to_margin_rate=Decimal("1")
        )
        
        # Close with 15,000 gross profit (exit at 65000)
        from app.broker.models import PaperFill, PaperOrder, PaperOrderSide, PaperOrderStatus
        signal = StrategySignal(strategy_name="test", pair="B-BTC_USDT", interval="1m", action=SignalAction.EXIT_LONG, direction=SignalDirection.LONG, confidence=Decimal("1"), timestamp_ms=1000, entry_price=Decimal("50000"), stop_loss=Decimal("48000"), reason="test")
        
        # Mocking _paper_fill_price to return 65000 (15000 profit)
        self.broker._paper_fill_price = MagicMock(return_value=(Decimal("65000"), {}))
        
        self.broker._close_from_signal(signal, market_price=Decimal("65000"), timestamp_ms=1000, risk_decision=None, reason="test")
        
        snapshot = self.broker.snapshot()
        self.assertEqual(snapshot.total_equity, Decimal("115000"))
        self.assertEqual(snapshot.locked_profit, Decimal("15000"))
        self.assertEqual(snapshot.tradable_base, Decimal("100000"))
        self.assertEqual(snapshot.tradable_equity, Decimal("100000"))
        
        # B. Loss -5,000 (exit at 45000)
        self.broker.positions["B-BTC_USDT"] = PaperPosition(
            strategy_name="test",
            pair="B-BTC_USDT",
            direction=SignalDirection.LONG,
            entry_price=Decimal("50000"),
            quantity=Decimal("1"),
            leverage=Decimal("3"),
            opened_at_ms=1500,
            updated_at_ms=1500,
            metadata={"total_fee": Decimal("0")},
            quote_to_margin_rate=Decimal("1")
        )
        signal_loss = StrategySignal(strategy_name="test", pair="B-BTC_USDT", interval="1m", action=SignalAction.EXIT_LONG, direction=SignalDirection.LONG, confidence=Decimal("1"), timestamp_ms=2000, entry_price=Decimal("50000"), stop_loss=Decimal("48000"), reason="test")
        self.broker._paper_fill_price = MagicMock(return_value=(Decimal("45000"), {}))
        
        self.broker._close_from_signal(signal_loss, market_price=Decimal("45000"), timestamp_ms=2000, risk_decision=None, reason="test")
        
        snapshot = self.broker.snapshot()
        self.assertEqual(snapshot.total_equity, Decimal("110000"))
        self.assertEqual(snapshot.locked_profit, Decimal("15000"))
        self.assertEqual(snapshot.tradable_base, Decimal("95000"))
        self.assertEqual(snapshot.daily_loss_from_tradable_base, Decimal("5000"))
        
        # C. Second Loss -5,000
        self.broker.positions["B-BTC_USDT"] = PaperPosition(
            strategy_name="test",
            pair="B-BTC_USDT",
            direction=SignalDirection.LONG,
            entry_price=Decimal("50000"),
            quantity=Decimal("1"),
            leverage=Decimal("3"),
            opened_at_ms=2500,
            updated_at_ms=2500,
            metadata={"total_fee": Decimal("0")},
            quote_to_margin_rate=Decimal("1")
        )
        signal_loss_2 = StrategySignal(strategy_name="test", pair="B-BTC_USDT", interval="1m", action=SignalAction.EXIT_LONG, direction=SignalDirection.LONG, confidence=Decimal("1"), timestamp_ms=3000, entry_price=Decimal("50000"), stop_loss=Decimal("48000"), reason="test")
        self.broker._paper_fill_price = MagicMock(return_value=(Decimal("45000"), {}))
        
        self.broker._close_from_signal(signal_loss_2, market_price=Decimal("45000"), timestamp_ms=3000, risk_decision=None, reason="test")
        
        snapshot = self.broker.snapshot()
        self.assertEqual(snapshot.total_equity, Decimal("105000"))
        self.assertEqual(snapshot.locked_profit, Decimal("15000"))
        self.assertEqual(snapshot.tradable_base, Decimal("90000"))
        self.assertEqual(snapshot.daily_loss_from_tradable_base, Decimal("10000"))
        
        # Verify Risk Manager blocks new entry
        signal_entry = StrategySignal(strategy_name="test", pair="B-BTC_USDT", interval="1m", action=SignalAction.ENTER_LONG, direction=SignalDirection.LONG, confidence=Decimal("1"), timestamp_ms=4000, entry_price=Decimal("50000"), stop_loss=Decimal("48000"), reason="test")
        
        decision = self.risk_manager.evaluate_signal(
            signal_entry,
            account_equity=snapshot.tradable_equity,
            available_equity=snapshot.tradable_equity,
            daily_realized_pnl=-snapshot.daily_loss_from_tradable_base,
            daily_loss_limit_equity=snapshot.tradable_base + snapshot.daily_loss_from_tradable_base,
            requested_leverage=Decimal("3")
        )
        self.assertFalse(decision.approved)
        self.assertIn("Max daily loss reached", decision.reason)

    def test_manual_unlock(self):
        # Initial 100k, win 15k -> locked 15k, base 100k
        self.broker.locked_profit = Decimal("15000")
        self.broker.tradable_base = Decimal("100000")
        
        # D. Manual unlock 5,000
        self.broker.unlock_profit(Decimal("5000"), "test")
        
        self.assertEqual(self.broker.locked_profit, Decimal("10000"))
        self.assertEqual(self.broker.tradable_base, Decimal("105000"))
        self.assertEqual(self.broker.unlocked_profit, Decimal("5000"))

    def test_daily_reset_clears_daily_loss_override(self):
        self.broker.protected_profit_override_enabled = True
        self.broker.daily_loss_from_tradable_base = Decimal("2500")
        self.broker.last_pnl_reset_day = "2026-05-24"

        candle = OHLCVCandle(
            pair="B-BTC_USDT",
            interval="1m",
            open_time_ms=0,
            close_time_ms=0,
            open=Decimal("100"),
            high=Decimal("100"),
            low=Decimal("100"),
            close=Decimal("100"),
            volume=Decimal("0"),
        )

        self.broker.process_candle(candle)

        self.assertFalse(self.broker.protected_profit_override_enabled)
        self.assertEqual(self.broker.daily_loss_from_tradable_base, Decimal("0"))

    def test_profit_lock_uses_entry_fees_total(self):
        from app.broker.models import PaperPosition
        self.broker.fees_paid = Decimal("100")
        self.broker.positions["B-BTC_USDT"] = PaperPosition(
            strategy_name="test",
            pair="B-BTC_USDT",
            direction=SignalDirection.LONG,
            entry_price=Decimal("50000"),
            quantity=Decimal("1"),
            leverage=Decimal("3"),
            opened_at_ms=500,
            updated_at_ms=500,
            metadata={"entry_fees_total": Decimal("100")},
            quote_to_margin_rate=Decimal("1"),
        )
        signal = StrategySignal(
            strategy_name="test", pair="B-BTC_USDT", interval="1m",
            action=SignalAction.EXIT_LONG, direction=SignalDirection.LONG,
            confidence=Decimal("1"), timestamp_ms=1000,
            entry_price=Decimal("50000"), stop_loss=Decimal("48000"),
            reason="test",
        )
        self.broker._paper_fill_price = MagicMock(return_value=(Decimal("65000"), {}))

        self.broker._close_from_signal(
            signal,
            market_price=Decimal("65000"),
            timestamp_ms=1000,
            risk_decision=None,
            reason="test",
        )

        snapshot = self.broker.snapshot()
        self.assertEqual(snapshot.total_equity, Decimal("114900"))
        self.assertEqual(snapshot.locked_profit, Decimal("14900"))

    def test_risk_sizing_ignores_locked_profit(self):
        # E. Initial 100,000, Locked 15,000, Risk 4%
        # To have 100,000 tradable while 15,000 is locked, realized_pnl must be 15,000
        self.broker.realized_pnl = Decimal("15000")
        self.broker.locked_profit = Decimal("15000")
        self.broker.tradable_base = Decimal("100000")
        snapshot = self.broker.snapshot()
        
        self.assertEqual(snapshot.total_equity, Decimal("115000"))
        self.assertEqual(snapshot.tradable_equity, Decimal("100000"))
        
        # account_equity should be tradable_equity (100,000)
        signal = StrategySignal(strategy_name="test", pair="B-BTC_USDT", interval="1m", action=SignalAction.ENTER_LONG, direction=SignalDirection.LONG, confidence=Decimal("1"), timestamp_ms=4000, entry_price=Decimal("100"), stop_loss=Decimal("96"), reason="test")
        # 100 -> 96 is 4% risk. On 100,000 equity, risk amount should be 4,000.
        
        # Disable ALL safety filters to avoid any interference
        self.risk_settings = RiskSettings(
            max_risk_per_trade_pct=Decimal("4"),
            max_margin_usage_pct=Decimal("100"),
            max_total_open_notional_pct=Decimal("0"),
            max_total_risk_pct=Decimal("100"),
            entry_safety_enabled=False,
            min_stop_distance_pct=Decimal("0"),
            min_entry_atr_pct=Decimal("0"),
            min_stop_atr_multiple=Decimal("0"),
            pair_loss_throttle_enabled=False
        )
        self.risk_manager = RiskManager(self.risk_settings)

        decision = self.risk_manager.evaluate_signal(
            signal,
            account_equity=snapshot.tradable_equity,
            available_equity=snapshot.tradable_equity,
            requested_leverage=Decimal("3")
        )
        
        self.assertTrue(decision.approved, f"Decision rejected: {decision.reason}")
        # Risk amount = (entry - stop) * position_size
        # 4,000 = (100 - 96) * position_size => position_size = 1000
        self.assertEqual(decision.position_size, Decimal("1000"))
        
        # If it used total_equity (115,000), risk amount would be 4,600.
        # 4,600 = (100 - 96) * position_size => position_size = 1150
        self.assertNotEqual(decision.position_size, Decimal("1150"))

    def test_state_persistence(self):
        # G. State persistence restores locked_profit and tradable_base correctly
        from app.persistence.paper_state import PaperStateStore
        import os
        db_path = "test_persistence.db"
        if os.path.exists(db_path): os.remove(db_path)
        
        store = PaperStateStore(db_path)
        broker1 = PaperBroker(starting_equity=Decimal("100000"), state_store=store)
        broker1.locked_profit = Decimal("15000")
        broker1.tradable_base = Decimal("95000")
        broker1._save_state()
        
        # New broker instance with same store
        broker2 = PaperBroker(starting_equity=Decimal("100000"), state_store=store)
        self.assertEqual(broker2.locked_profit, Decimal("15000"))
        self.assertEqual(broker2.tradable_base, Decimal("95000"))
        
        store.close()
        if os.path.exists(db_path): os.remove(db_path)

    def test_zero_persisted_profit_lock_bases_fall_back_to_starting_equity(self):
        from app.persistence.paper_state import PaperStateStore
        import os
        db_path = "test_zero_profit_lock_base.db"
        if os.path.exists(db_path): os.remove(db_path)

        store = PaperStateStore(db_path)
        store.save({
            "equity": "100000",
            "positions": {},
            "fills": [],
            "realized_pnl": "0",
            "fees_paid": "0",
            "funding_paid": "0",
            "initial_equity": "0",
            "tradable_base": "0",
            "profit_lock_enabled": False,
        })

        broker = PaperBroker(starting_equity=Decimal("100000"), state_store=store)
        snapshot = broker.snapshot()

        self.assertEqual(snapshot.initial_equity, Decimal("100000"))
        self.assertEqual(snapshot.tradable_base, Decimal("100000"))

        store.close()
        if os.path.exists(db_path): os.remove(db_path)

if __name__ == "__main__":
    unittest.main()
