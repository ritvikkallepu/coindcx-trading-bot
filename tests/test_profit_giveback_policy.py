from __future__ import annotations

import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

from app.backtest.engine import _BacktestSafetyState
from app.backtest.models import BacktestConfig
from app.broker.models import PaperFill, PaperOrderSide
from app.config import RiskSettings, Settings
from app.data.candle_builder import CandleSeries, OHLCVCandle
from app.live.paper_loop import PaperTradingLoop
from app.risk.profit_protection import (
    compute_profit_giveback_stop,
    post_profit_pullback_seen,
)
from app.strategies.base import (
    SignalAction,
    SignalDirection,
    StrategySignal,
)


class ProfitGivebackPolicyTests(unittest.TestCase):
    def test_long_locks_fraction_of_best_r_from_favorable_high(self) -> None:
        result = compute_profit_giveback_stop(
            entry_price=Decimal("100"),
            initial_r=Decimal("10"),
            direction=SignalDirection.LONG,
            current_stop=Decimal("95"),
            current_price=Decimal("105"),
            favorable_price=Decimal("130"),
            previous_max_r=Decimal("0"),
            activation_r=Decimal("1.0"),
            lock_fraction=Decimal("0.50"),
            min_lock_r=Decimal("0.25"),
            tighten_after_r=Decimal("3.0"),
            tighten_fraction=Decimal("0.70"),
        )

        self.assertTrue(result.active)
        self.assertEqual(result.max_r, Decimal("3"))
        self.assertEqual(result.lock_r, Decimal("2.10"))
        self.assertEqual(result.stop, Decimal("121.00"))

    def test_long_ratchet_never_loosen_current_stop(self) -> None:
        result = compute_profit_giveback_stop(
            entry_price=Decimal("100"),
            initial_r=Decimal("10"),
            direction=SignalDirection.LONG,
            current_stop=Decimal("125"),
            current_price=Decimal("105"),
            favorable_price=Decimal("130"),
            previous_max_r=Decimal("0"),
            activation_r=Decimal("1.0"),
            lock_fraction=Decimal("0.50"),
            min_lock_r=Decimal("0.25"),
            tighten_after_r=Decimal("3.0"),
            tighten_fraction=Decimal("0.70"),
        )

        self.assertEqual(result.stop, Decimal("125"))

    def test_short_locks_fraction_of_best_r_from_favorable_low(self) -> None:
        result = compute_profit_giveback_stop(
            entry_price=Decimal("100"),
            initial_r=Decimal("10"),
            direction=SignalDirection.SHORT,
            current_stop=Decimal("110"),
            current_price=Decimal("95"),
            favorable_price=Decimal("70"),
            previous_max_r=Decimal("0"),
            activation_r=Decimal("1.0"),
            lock_fraction=Decimal("0.50"),
            min_lock_r=Decimal("0.25"),
            tighten_after_r=Decimal("3.0"),
            tighten_fraction=Decimal("0.70"),
        )

        self.assertTrue(result.active)
        self.assertEqual(result.max_r, Decimal("3"))
        self.assertEqual(result.lock_r, Decimal("2.10"))
        self.assertEqual(result.stop, Decimal("79.00"))

    def test_does_not_activate_before_activation_r(self) -> None:
        result = compute_profit_giveback_stop(
            entry_price=Decimal("100"),
            initial_r=Decimal("10"),
            direction=SignalDirection.LONG,
            current_stop=Decimal("95"),
            current_price=Decimal("105"),
            favorable_price=Decimal("108"),
            previous_max_r=Decimal("0"),
            activation_r=Decimal("1.0"),
            lock_fraction=Decimal("0.50"),
            min_lock_r=Decimal("0.25"),
            tighten_after_r=Decimal("3.0"),
            tighten_fraction=Decimal("0.70"),
        )

        self.assertFalse(result.active)
        self.assertIsNone(result.stop)
        self.assertEqual(result.max_r, Decimal("0.8"))

    def test_post_profit_pullback_seen_for_long(self) -> None:
        self.assertTrue(
            post_profit_pullback_seen(
                direction=SignalDirection.LONG,
                exit_price=Decimal("100"),
                candle_high=Decimal("101"),
                candle_low=Decimal("98.9"),
                atr=None,
                pullback_atr=Decimal("0"),
                pullback_pct=Decimal("1"),
            )
        )
        self.assertFalse(
            post_profit_pullback_seen(
                direction=SignalDirection.LONG,
                exit_price=Decimal("100"),
                candle_high=Decimal("101"),
                candle_low=Decimal("99.5"),
                atr=None,
                pullback_atr=Decimal("0"),
                pullback_pct=Decimal("1"),
            )
        )

    def test_post_profit_pullback_seen_for_short(self) -> None:
        self.assertTrue(
            post_profit_pullback_seen(
                direction=SignalDirection.SHORT,
                exit_price=Decimal("100"),
                candle_high=Decimal("101.1"),
                candle_low=Decimal("99"),
                atr=None,
                pullback_atr=Decimal("0"),
                pullback_pct=Decimal("1"),
            )
        )
        self.assertFalse(
            post_profit_pullback_seen(
                direction=SignalDirection.SHORT,
                exit_price=Decimal("100"),
                candle_high=Decimal("100.5"),
                candle_low=Decimal("99"),
                atr=None,
                pullback_atr=Decimal("0"),
                pullback_pct=Decimal("1"),
            )
        )

    def test_paper_guard_blocks_then_requires_pullback(self) -> None:
        pair = "B-BTC_USDT"
        loop = object.__new__(PaperTradingLoop)
        loop.settings = Settings(
            strategy_interval="5m",
            execution_interval="5m",
            paper_intrabar_enabled=True,
            risk=RiskSettings(
                post_profit_reentry_guard_enabled=True,
                post_profit_reentry_cooldown_candles=2,
                post_profit_reentry_pullback_atr=Decimal("0"),
                post_profit_reentry_pullback_pct=Decimal("1"),
            ),
        )
        loop.logger = MagicMock()
        loop._pair_recent_net_pnls = {}
        loop._consecutive_losing_trades = 0
        loop._global_loss_cooldown_until_ms = 0
        loop._pair_cooldown_until_ms = {}
        loop._same_direction_cooldown_until_ms = {}
        loop._same_direction_reversal_cooldown_until_ms = {}
        loop._post_profit_reentry_state = {}
        loop.series = {pair: CandleSeries([_candle(0, "100")])}

        exit_candle = _candle(1, "100")
        loop._observe_closed_fill(
            PaperFill(
                fill_id="f1",
                order_id="o1",
                pair=pair,
                side=PaperOrderSide.SELL,
                quantity=Decimal("1"),
                price=Decimal("100"),
                fee=Decimal("0"),
                timestamp_ms=exit_candle.close_time_ms,
                realized_pnl=Decimal("10"),
                metadata={"exit_trigger_type": "take_profit"},
            ),
            exit_candle,
        )
        signal = _entry_signal(pair, SignalDirection.LONG)

        self.assertEqual(
            loop._paper_entry_safety_rejection(signal, _candle(2, "100")),
            "paper_same_direction_cooldown",
        )
        self.assertEqual(
            loop._paper_entry_safety_rejection(
                signal,
                _candle(4, "100", high="101", low="99.5"),
            ),
            "paper_post_profit_pullback_required",
        )
        self.assertIsNone(
            loop._paper_entry_safety_rejection(
                signal,
                _candle(5, "99", high="101", low="98.9"),
            )
        )
        self.assertTrue(loop._post_profit_reentry_state[pair]["pullback_seen"])

    def test_backtest_guard_uses_execution_interval_and_pullback(self) -> None:
        pair = "B-BTC_USDT"
        config = BacktestConfig(
            pair=pair,
            interval="1h",
            execution_interval="5m",
            starting_equity=Decimal("1000"),
            leverage=Decimal("1"),
            post_profit_reentry_guard_enabled=True,
            post_profit_reentry_cooldown_candles=2,
            post_profit_reentry_pullback_atr=Decimal("0"),
            post_profit_reentry_pullback_pct=Decimal("1"),
        )
        state = _BacktestSafetyState(
            config=config,
            interval_ms=3_600_000,
            reentry_interval_ms=300_000,
            peak_equity=Decimal("1000"),
        )
        state.observe_trade(
            SimpleNamespace(
                net_pnl=Decimal("10"),
                direction=SignalDirection.LONG,
                exit_price=Decimal("100"),
                exit_time_ms=1_000,
                exit_reason="take_profit",
            )
        )
        signal = _entry_signal(pair, SignalDirection.LONG)

        self.assertEqual(
            state.post_profit_reentry_reason(
                signal,
                candle=_candle(1, "100"),
                atr=None,
            ),
            "post_profit_reentry_cooldown",
        )
        self.assertEqual(
            state.post_profit_reentry_reason(
                signal,
                candle=_candle(3, "100", high="101", low="99.5"),
                atr=None,
            ),
            "post_profit_pullback_required",
        )
        self.assertIsNone(
            state.post_profit_reentry_reason(
                signal,
                candle=_candle(4, "99", high="101", low="98.9"),
                atr=None,
            )
        )


def _entry_signal(pair: str, direction: SignalDirection) -> StrategySignal:
    action = (
        SignalAction.ENTER_LONG
        if direction == SignalDirection.LONG
        else SignalAction.ENTER_SHORT
    )
    return StrategySignal(
        strategy_name="test",
        pair=pair,
        interval="5m",
        action=action,
        direction=direction,
        confidence=Decimal("1"),
        reason="test",
        timestamp_ms=0,
        entry_price=Decimal("100"),
        stop_loss=Decimal("95"),
    )


def _candle(
    index: int,
    close: str,
    *,
    high: str | None = None,
    low: str | None = None,
) -> OHLCVCandle:
    open_time_ms = index * 300_000
    close_price = Decimal(close)
    return OHLCVCandle(
        pair="B-BTC_USDT",
        interval="5m",
        open_time_ms=open_time_ms,
        close_time_ms=open_time_ms + 299_999,
        open=close_price,
        high=Decimal(high) if high is not None else close_price,
        low=Decimal(low) if low is not None else close_price,
        close=close_price,
        volume=Decimal("100"),
    )


if __name__ == "__main__":
    unittest.main()
