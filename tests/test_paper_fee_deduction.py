from __future__ import annotations

import unittest
from decimal import Decimal
from app.config import Settings, RiskSettings
from app.data.candle_builder import OHLCVCandle
from app.live.paper_loop import PaperTradingLoop
from app.broker.models import PaperOrderSide
from app.strategies.base import SignalAction, SignalDirection, StrategySignal
from app.risk.models import RiskDecision

class PaperFeeDeductionTests(unittest.TestCase):
    def setUp(self) -> None:
        from app.persistence.paper_state import PaperStateStore, PaperSessionStore
        from pathlib import Path
        self.db_path = "test_fee_state.db"
        self.session_path = "data/test_fee_session.json"
        
        if Path(self.db_path).exists(): Path(self.db_path).unlink()
        if Path(self.session_path).exists(): Path(self.session_path).unlink()

        self.state_store = PaperStateStore(self.db_path)
        self.session_store = PaperSessionStore(self.session_path)

        self.settings = Settings(
            paper_starting_equity=Decimal("10000"),
            quote_to_margin_rate=Decimal("1"),
            risk=RiskSettings(
                maker_fee_rate=Decimal("0.01"), # 1% fee for easy math
                taker_fee_rate=Decimal("0.01"),
                fee_gst_rate=Decimal("0"),
                slippage_pct=Decimal("0") # Task: 0 slippage
            )
        )
        self.loop = PaperTradingLoop(
            self.settings,
            state_store=self.state_store,
            session_store=self.session_store
        )

    def tearDown(self) -> None:
        from pathlib import Path
        self.state_store.close()
        if Path(self.db_path).exists(): Path(self.db_path).unlink()
        if Path(self.session_path).exists(): Path(self.session_path).unlink()

    def test_fees_are_deducted_from_equity(self) -> None:
        # 1. Initial equity
        self.assertEqual(self.loop.broker.snapshot().equity, Decimal("10000"))
        
        # 2. Mock a trade entry
        signal = StrategySignal(
            strategy_name="S", pair="B-BTC_USDT", interval="1m",
            action=SignalAction.ENTER_LONG, direction=SignalDirection.LONG,
            confidence=Decimal("1"), reason="test", timestamp_ms=1000,
            entry_price=Decimal("100"), stop_loss=Decimal("90")
        )
        decision = RiskDecision(approved=True, reason="test", signal=signal, position_size=Decimal("10")) # 10 units
        
        # Entry notional = 10 * 100 = 1000. Fee = 1% of 1000 = 10.
        self.loop.broker.execute_decision(decision, market_price=Decimal("100"), timestamp_ms=1000)
        
        # 3. Check equity
        # Equity = 10000 - 10 (fee) = 9990.
        snapshot = self.loop.broker.snapshot({ "B-BTC_USDT": Decimal("100") })
        self.assertEqual(snapshot.equity, Decimal("9990"))
        self.assertEqual(self.loop.broker.fees_paid, Decimal("10"))

    def test_session_persistence_restores_fees(self) -> None:
        self.loop.broker.fees_paid = Decimal("50.5")
        self.loop.broker.realized_pnl = Decimal("100")
        self.loop.broker._save_state()
        
        # New loop
        new_loop = PaperTradingLoop(self.settings, state_store=self.loop.state_store)
        self.assertEqual(new_loop.broker.fees_paid, Decimal("50.5"))
        self.assertEqual(new_loop.broker.realized_pnl, Decimal("100"))

    def test_closed_trade_dict_includes_entry_and_exit_fees(self) -> None:
        entry_signal = StrategySignal(
            strategy_name="S",
            pair="B-BTC_USDT",
            interval="1m",
            action=SignalAction.ENTER_LONG,
            direction=SignalDirection.LONG,
            confidence=Decimal("1"),
            reason="entry",
            timestamp_ms=1000,
            entry_price=Decimal("100"),
            stop_loss=Decimal("90"),
        )
        entry_decision = RiskDecision(
            approved=True,
            reason="approved",
            signal=entry_signal,
            position_size=Decimal("10"),
            leverage=Decimal("1"),
            max_loss=Decimal("100"),
        )
        entry_report = self.loop.broker.execute_decision(
            entry_decision,
            market_price=Decimal("100"),
            timestamp_ms=1000,
        )
        self.assertTrue(entry_report.accepted)
        self.assertEqual(entry_report.fill.fee, Decimal("10"))

        exit_signal = StrategySignal(
            strategy_name="S",
            pair="B-BTC_USDT",
            interval="1m",
            action=SignalAction.EXIT_LONG,
            direction=SignalDirection.LONG,
            confidence=Decimal("1"),
            reason="take_profit",
            timestamp_ms=2000,
            entry_price=Decimal("110"),
        )
        exit_decision = RiskDecision(
            approved=True,
            reason="approved",
            signal=exit_signal,
        )
        exit_report = self.loop.broker.execute_decision(
            exit_decision,
            market_price=Decimal("110"),
            timestamp_ms=2000,
        )
        self.assertTrue(exit_report.accepted)

        candle = OHLCVCandle(
            pair="B-BTC_USDT",
            interval="1m",
            open_time_ms=2000,
            close_time_ms=59999,
            open=Decimal("100"),
            high=Decimal("110"),
            low=Decimal("100"),
            close=Decimal("110"),
            volume=Decimal("1"),
        )
        trade = self.loop._map_fill_to_trade_dict(
            exit_report.fill,
            candle,
            position=exit_report.position,
        )

        self.assertEqual(Decimal(trade["entry_fee"]), Decimal("10"))
        self.assertEqual(Decimal(trade["exit_fee"]), Decimal("11"))
        self.assertEqual(Decimal(trade["total_fees"]), Decimal("21"))
        self.assertEqual(Decimal(trade["fees"]), Decimal("21"))
        self.assertEqual(Decimal(trade["gross_pnl"]), Decimal("100"))
        self.assertEqual(Decimal(trade["net_pnl"]), Decimal("79"))

    def test_paper_safety_observes_true_net_after_entry_fee(self) -> None:
        entry_signal = StrategySignal(
            strategy_name="S",
            pair="B-BTC_USDT",
            interval="1m",
            action=SignalAction.ENTER_LONG,
            direction=SignalDirection.LONG,
            confidence=Decimal("1"),
            reason="entry",
            timestamp_ms=1000,
            entry_price=Decimal("100"),
            stop_loss=Decimal("90"),
        )
        entry_decision = RiskDecision(
            approved=True,
            reason="approved",
            signal=entry_signal,
            position_size=Decimal("10"),
            leverage=Decimal("1"),
            max_loss=Decimal("100"),
        )
        self.loop.broker.execute_decision(
            entry_decision,
            market_price=Decimal("100"),
            timestamp_ms=1000,
        )

        exit_signal = StrategySignal(
            strategy_name="S",
            pair="B-BTC_USDT",
            interval="1m",
            action=SignalAction.EXIT_LONG,
            direction=SignalDirection.LONG,
            confidence=Decimal("1"),
            reason="small_gross_win",
            timestamp_ms=2000,
            entry_price=Decimal("101"),
        )
        exit_report = self.loop.broker.execute_decision(
            RiskDecision(approved=True, reason="approved", signal=exit_signal),
            market_price=Decimal("101"),
            timestamp_ms=2000,
        )
        candle = OHLCVCandle(
            pair="B-BTC_USDT",
            interval="1m",
            open_time_ms=2000,
            close_time_ms=59999,
            open=Decimal("100"),
            high=Decimal("101"),
            low=Decimal("100"),
            close=Decimal("101"),
            volume=Decimal("1"),
        )

        self.loop._observe_closed_fill(
            exit_report.fill,
            candle,
            position=exit_report.position,
        )

        self.assertEqual(exit_report.fill.realized_pnl, Decimal("10"))
        self.assertEqual(exit_report.fill.fee, Decimal("10.1"))
        self.assertEqual(self.loop._consecutive_losing_trades, 1)

if __name__ == "__main__":
    unittest.main()
