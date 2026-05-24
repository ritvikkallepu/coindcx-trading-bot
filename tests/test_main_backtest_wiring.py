from __future__ import annotations

import unittest
from decimal import Decimal
from unittest.mock import MagicMock, patch

from app.backtest.models import BacktestConfig, BacktestMetrics, BacktestResult
from app.broker.models import PaperAccountSnapshot
from app.main import backtest_command


class MainBacktestWiringTests(unittest.TestCase):
    @patch("app.main.record_backtest_result", return_value={})
    @patch("app.main._print_json")
    @patch("app.main.BacktestEngine")
    @patch("app.main.load_historical_candle_series_between")
    @patch("app.main.load_historical_candle_series")
    @patch("app.main.CoinDCXFuturesClient")
    def test_backtest_command_accepts_loss_streak_cooldown_candles(
        self,
        _client_cls,
        load_series,
        load_execution_series,
        engine_cls,
        _print_json,
        _record_history,
    ) -> None:
        parent_candle = MagicMock()
        parent_candle.open_time_ms = 1_000
        parent_candle.close_time_ms = 3_601_000
        load_series.return_value = [parent_candle]
        load_execution_series.return_value = [MagicMock()]

        account = PaperAccountSnapshot(
            starting_equity=Decimal("10000"),
            realized_pnl=Decimal("0"),
            unrealized_pnl=Decimal("0"),
            fees_paid=Decimal("0"),
            equity=Decimal("10000"),
            open_position_count=0,
            open_notional=Decimal("0"),
        )
        metrics = BacktestMetrics(
            starting_equity=Decimal("10000"),
            final_equity=Decimal("10000"),
            total_return=Decimal("0"),
            total_return_pct=Decimal("0"),
            realized_pnl=Decimal("0"),
            unrealized_pnl=Decimal("0"),
            fees_paid=Decimal("0"),
            net_pnl=Decimal("0"),
            trade_count=0,
            winning_trades=0,
            losing_trades=0,
            win_rate_pct=None,
            average_win=None,
            average_loss=None,
            profit_factor=None,
            max_drawdown=Decimal("0"),
            max_drawdown_pct=Decimal("0"),
            sharpe_ratio=None,
        )
        result = BacktestResult(
            config=BacktestConfig(
                pair="B-BSB_USDT",
                interval="1h",
                starting_equity=Decimal("10000"),
                leverage=Decimal("5"),
                strategy_name="hybrid_meta_v2",
            ),
            candles_loaded=1,
            candles_used=1,
            metrics=metrics,
            final_account=account,
            equity_curve=[],
            trades=[],
            orders=[],
            fills=[],
        )
        engine_cls.return_value.run.return_value = result

        backtest_command(
            pair="B-BSB_USDT",
            interval="1h",
            lookback=1000,
            account_equity=Decimal("10000"),
            leverage=Decimal("5"),
            risk_per_trade_pct=Decimal("3"),
            compound_risk_equity=False,
            stop_loss_pct=None,
            take_profit_pct=None,
            maker_fee_rate=Decimal("0.0002"),
            taker_fee_rate=Decimal("0.0005"),
            fee_gst_rate=Decimal("0.18"),
            entry_fee_type="maker",
            exit_fee_type="taker",
            slippage_pct=Decimal("0.02"),
            stop_slippage_pct=Decimal("0.06"),
            funding_fee_rate=Decimal("0"),
            funding_interval_hours=8,
            trailing_stop_enabled=None,
            trailing_stop_activation_pct=None,
            trailing_stop_distance_pct=None,
            atr_dynamic_exits_enabled=True,
            atr_period=14,
            atr_stop_multiple=Decimal("1.5"),
            atr_take_profit_multiple=Decimal("3"),
            atr_take_profit_mode="none",
            execution_interval="1m",
            intrabar_reentry_enabled=False,
            max_reentries_per_candle=0,
            reentry_cooldown_candles=1,
            stop_loss_cooldown_candles=1,
            max_consecutive_losses=2,
            loss_cooldown_candles=4,
            strategy_name="hybrid_meta_v2",
            recent_count=30,
            equity_giveback_guard_enabled=False,
            equity_giveback_threshold_pct=Decimal("3.5"),
            equity_giveback_cooldown_candles=72,
            loss_streak_cooldown_enabled=False,
            consecutive_loss_limit=3,
            loss_streak_cooldown_candles=12,
            rolling_loss_window=8,
            rolling_loss_limit=5,
            rolling_loss_cooldown_candles=36,
            post_spike_cooldown_enabled=False,
            post_spike_lookback_candles=50,
            post_spike_gain_threshold_pct=Decimal("5"),
            post_spike_cooldown_candles=24,
            breakeven_enabled=False,
            breakeven_activation_r=Decimal("1"),
            breakeven_offset_r=Decimal("0"),
            profit_lock_enabled=False,
            profit_lock_activation_r=Decimal("1.5"),
            profit_lock_r=Decimal("0.5"),
            atr_trail_after_r_enabled=False,
            atr_trail_activation_r=Decimal("2"),
            chop_filter_enabled=False,
            min_ema_gap_pct=Decimal("0.15"),
            min_atr_pct=Decimal("0.2"),
            block_flat_ema_enabled=False,
            block_low_atr_enabled=False,
            atr_trailing_multiple=Decimal("2"),
        )

        config = engine_cls.call_args.kwargs["config"]
        self.assertEqual(config.loss_streak_cooldown_candles, 12)
        self.assertEqual(config.execution_interval, "1m")
        self.assertTrue(config.atr_dynamic_exits_enabled)


if __name__ == "__main__":
    unittest.main()
