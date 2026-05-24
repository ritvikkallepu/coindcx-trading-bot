from __future__ import annotations

import unittest
from decimal import Decimal

from app.backtest.models import (
    BacktestConfig,
    BacktestEquityPoint,
    BacktestMetrics,
    BacktestResult,
    BacktestTrade,
)
from app.broker.models import PaperAccountSnapshot, PaperExecutionReport, PaperExecutionStatus
from app.config import Settings
from app.dashboard.state import (
    DashboardDefaults,
    build_backtest_dashboard_payload,
    build_strategy_profile,
    build_status_payload,
    classify_run_quality,
    downsample_equity_curve,
)
from app.dashboard.server import _bool_param, _fee_config_params, _fee_rate_param
from app.risk.models import RiskDecision
from app.strategies.base import SignalAction, SignalDirection, StrategySignal


def _equity_point(index: int, equity: Decimal) -> BacktestEquityPoint:
    return BacktestEquityPoint(
        timestamp_ms=index,
        close_price=Decimal("100"),
        equity=equity,
        realized_pnl=equity - Decimal("1000"),
        unrealized_pnl=Decimal("0"),
        fees_paid=Decimal("0"),
        open_position_count=0,
        open_notional=Decimal("0"),
    )


class DashboardStateTests(unittest.TestCase):
    def test_status_payload_keeps_live_trading_locked_by_default(self) -> None:
        payload = build_status_payload(
            settings=Settings(),
            defaults=DashboardDefaults(),
        )

        self.assertEqual(payload["bot"]["status"], "paper_ready")
        self.assertFalse(payload["bot"]["live_trading_allowed"])
        self.assertTrue(payload["safety"]["live_orders_locked"])
        self.assertEqual(payload["defaults"]["pair"], "B-SOL_USDT")
        self.assertEqual(payload["defaults"]["strategy"], "bb_dynamic_grid")
        self.assertEqual(payload["defaults"]["fee_gst_rate"], "0.18")
        self.assertEqual(payload["defaults"]["entry_fee_type"], "maker")
        self.assertEqual(payload["defaults"]["exit_fee_type"], "taker")
        self.assertEqual(payload["defaults"]["stop_slippage_pct"], "0.02")
        self.assertEqual(payload["defaults"]["funding_fee_rate"], "0")
        self.assertEqual(payload["defaults"]["funding_interval_hours"], 8)
        self.assertEqual(payload["bot"]["futures_margin_currency"], "INR")
        self.assertEqual(payload["strategy_profile"]["mode"], "Futures Grid")

    def test_strategy_profile_describes_adaptive_market_regime_selector(self) -> None:
        one_hour = build_strategy_profile(strategy="adaptive_hybrid", interval="1h")
        four_hour = build_strategy_profile(strategy="adaptive_hybrid", interval="4h")

        self.assertEqual(one_hour["mode"], "Market Adaptive")
        self.assertEqual(four_hour["mode"], "Market Adaptive")
        self.assertEqual(one_hour["primary"], "Regime selector")
        self.assertEqual(four_hour["secondary"], "EMA-RSI / Bollinger")

    def test_strategy_profile_describes_dynamic_grid(self) -> None:
        profile = build_strategy_profile(strategy="bb_dynamic_grid", interval="4h")

        self.assertEqual(profile["mode"], "Futures Grid")
        self.assertEqual(profile["filter"], "Close-confirmed trail")

    def test_strategy_profile_describes_weighted_hybrid_v2(self) -> None:
        profile = build_strategy_profile(strategy="hybrid_meta_v2", interval="1h")

        self.assertEqual(profile["mode"], "Score Blend V2")
        self.assertEqual(profile["filter"], "Entry-only visual screen")

    def test_dashboard_fee_pct_uses_coindcx_inr_m_rates(self) -> None:
        self.assertEqual(
            _fee_rate_param({"fee_type": "inr_maker"}, Decimal("0")),
            Decimal("0.0002"),
        )
        self.assertEqual(
            _fee_rate_param({"fee_type": "inr_taker"}, Decimal("0")),
            Decimal("0.0005"),
        )
        self.assertEqual(
            _fee_rate_param({"fee_pct": "0.05"}, Decimal("0")),
            Decimal("0.0005"),
        )

    def test_dashboard_normalizes_old_percent_like_fee_rate_input(self) -> None:
        self.assertEqual(
            _fee_rate_param({"fee_rate": "0.05"}, Decimal("0")),
            Decimal("0.0005"),
        )

    def test_dashboard_fee_config_uses_maker_and_taker_rates(self) -> None:
        config = _fee_config_params(
            {
                "maker_fee_pct": "0.02",
                "taker_fee_pct": "0.05",
                "entry_fee_type": "maker",
                "exit_fee_type": "taker",
            },
            DashboardDefaults(),
        )

        self.assertEqual(config["maker_fee_rate"], Decimal("0.0002"))
        self.assertEqual(config["taker_fee_rate"], Decimal("0.0005"))
        self.assertEqual(config["fee_gst_rate"], Decimal("0.18"))
        self.assertEqual(config["entry_fee_type"], "maker")
        self.assertEqual(config["exit_fee_type"], "taker")

    def test_dashboard_fee_config_accepts_gst_percentage(self) -> None:
        config = _fee_config_params(
            {"maker_fee_pct": "0.02", "taker_fee_pct": "0.05", "fee_gst_pct": "18"},
            DashboardDefaults(fee_gst_rate=Decimal("0")),
        )

        self.assertEqual(config["maker_fee_rate"], Decimal("0.0002"))
        self.assertEqual(config["taker_fee_rate"], Decimal("0.0005"))
        self.assertEqual(config["fee_gst_rate"], Decimal("0.18"))

    def test_dashboard_bool_param_parses_false_strings(self) -> None:
        self.assertFalse(_bool_param("false", True))
        self.assertFalse(_bool_param("0", True))
        self.assertFalse(_bool_param("off", True))
        self.assertTrue(_bool_param("true", False))

    def test_downsample_equity_curve_keeps_first_and_last_points(self) -> None:
        points = [_equity_point(index, Decimal(1000 + index)) for index in range(10)]

        sampled = downsample_equity_curve(points, max_points=4)

        self.assertEqual(len(sampled), 4)
        self.assertEqual(sampled[0].timestamp_ms, 0)
        self.assertEqual(sampled[-1].timestamp_ms, 9)

    def test_backtest_payload_contains_chart_points_and_counts(self) -> None:
        config = BacktestConfig(
            pair="B-SOL_USDT",
            interval="1h",
            starting_equity=Decimal("1000"),
            leverage=Decimal("2"),
            strategy_name="ema_rsi_trend",
        )
        account = PaperAccountSnapshot(
            starting_equity=Decimal("1000"),
            realized_pnl=Decimal("50"),
            unrealized_pnl=Decimal("5"),
            fees_paid=Decimal("2"),
            equity=Decimal("1053"),
            open_position_count=1,
            open_notional=Decimal("500"),
        )
        metrics = BacktestMetrics(
            starting_equity=Decimal("1000"),
            final_equity=Decimal("1053"),
            total_return=Decimal("53"),
            total_return_pct=Decimal("5.3"),
            realized_pnl=Decimal("50"),
            unrealized_pnl=Decimal("5"),
            fees_paid=Decimal("2"),
            net_pnl=Decimal("53"),
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
            config=config,
            candles_loaded=3,
            candles_used=3,
            metrics=metrics,
            final_account=account,
            equity_curve=[
                _equity_point(1, Decimal("1000")),
                _equity_point(2, Decimal("1040")),
                _equity_point(3, Decimal("1053")),
            ],
            trades=[],
            orders=[],
            fills=[],
        )

        payload = build_backtest_dashboard_payload(
            result,
            recent_count=2,
            max_equity_points=2,
        )

        self.assertEqual(payload["config"]["pair"], "B-SOL_USDT")
        self.assertEqual(payload["metrics"]["final_equity"], "1053")
        self.assertEqual(payload["counts"]["candles_loaded"], 3)
        self.assertIn("signal_funnel", payload)
        self.assertEqual(payload["signal_funnel"]["requested_candles"], 3)
        self.assertEqual(payload["signal_funnel"]["actual_candles_loaded"], 3)
        self.assertEqual(len(payload["equity_curve"]), 2)
        self.assertEqual(payload["equity_curve"][-1]["equity"], "1053")
        self.assertEqual(payload["run_quality"]["label"], "No Trades")
        self.assertEqual(payload["strategy_profile"]["status"], "Baseline")

    def test_backtest_payload_exposes_all_trades_with_timeline(self) -> None:
        result = BacktestResult(
            config=BacktestConfig(
                pair="B-SOL_USDT",
                interval="1h",
                starting_equity=Decimal("1000"),
                leverage=Decimal("3"),
                strategy_name="hybrid_meta",
            ),
            candles_loaded=3,
            candles_used=3,
            metrics=BacktestMetrics(
                starting_equity=Decimal("1000"),
                final_equity=Decimal("1010"),
                total_return=Decimal("10"),
                total_return_pct=Decimal("1"),
                realized_pnl=Decimal("10"),
                unrealized_pnl=Decimal("0"),
                fees_paid=Decimal("0"),
                net_pnl=Decimal("10"),
                trade_count=2,
                winning_trades=1,
                losing_trades=1,
                win_rate_pct=Decimal("50"),
                average_win=Decimal("12"),
                average_loss=Decimal("-2"),
                profit_factor=Decimal("6"),
                max_drawdown=Decimal("2"),
                max_drawdown_pct=Decimal("0.2"),
                sharpe_ratio=None,
            ),
            final_account=PaperAccountSnapshot(
                starting_equity=Decimal("1000"),
                realized_pnl=Decimal("10"),
                unrealized_pnl=Decimal("0"),
                fees_paid=Decimal("0"),
                equity=Decimal("1010"),
                open_position_count=0,
                open_notional=Decimal("0"),
            ),
            equity_curve=[_equity_point(1, Decimal("1000"))],
            trades=[
                BacktestTrade(
                    pair="B-SOL_USDT",
                    strategy_name="hybrid_meta",
                    direction=SignalDirection.LONG,
                    quantity=Decimal("1"),
                    entry_price=Decimal("100"),
                    exit_price=Decimal("112"),
                    entry_time_ms=0,
                    exit_time_ms=3_600_000,
                    gross_pnl=Decimal("12"),
                    fees=Decimal("0"),
                    net_pnl=Decimal("12"),
                    exit_reason="Take profit triggered.",
                ),
                BacktestTrade(
                    pair="B-SOL_USDT",
                    strategy_name="hybrid_meta",
                    direction=SignalDirection.LONG,
                    quantity=Decimal("1"),
                    entry_price=Decimal("100"),
                    exit_price=Decimal("98"),
                    entry_time_ms=3_600_000,
                    exit_time_ms=7_200_000,
                    gross_pnl=Decimal("-2"),
                    fees=Decimal("0"),
                    net_pnl=Decimal("-2"),
                    exit_reason="Stop loss triggered.",
                ),
            ],
            orders=[],
            fills=[],
        )

        payload = build_backtest_dashboard_payload(result, recent_count=1)

        self.assertEqual(len(payload["recent_trades"]), 1)
        self.assertEqual(len(payload["trades"]), 2)
        self.assertEqual(len(payload["trade_timeline"]), 2)
        self.assertEqual(payload["trade_timeline"][0]["trade_number"], 1)
        self.assertEqual(payload["trade_timeline"][0]["duration"], "1h")
        self.assertEqual(payload["trade_timeline"][1]["exit_reason"], "Stop loss triggered.")

    def test_backtest_payload_exposes_entry_rejection_reasons(self) -> None:
        signal = StrategySignal(
            strategy_name="test",
            pair="B-KITE_USDT",
            interval="1h",
            action=SignalAction.ENTER_LONG,
            direction=SignalDirection.LONG,
            confidence=Decimal("1"),
            reason="test",
            timestamp_ms=1,
            entry_price=Decimal("1"),
            stop_loss=Decimal("0.9"),
        )
        decision = RiskDecision(
            approved=False,
            reason="Requested leverage exceeds the configured or instrument limit: 3 > 2.",
            signal=signal,
        )
        account = PaperAccountSnapshot(
            starting_equity=Decimal("1000"),
            realized_pnl=Decimal("0"),
            unrealized_pnl=Decimal("0"),
            fees_paid=Decimal("0"),
            equity=Decimal("1000"),
            open_position_count=0,
            open_notional=Decimal("0"),
        )
        result = BacktestResult(
            config=BacktestConfig(
                pair="B-KITE_USDT",
                interval="1h",
                starting_equity=Decimal("1000"),
                leverage=Decimal("3"),
            ),
            candles_loaded=1,
            candles_used=1,
            metrics=BacktestMetrics(
                starting_equity=Decimal("1000"),
                final_equity=Decimal("1000"),
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
            ),
            final_account=account,
            equity_curve=[_equity_point(1, Decimal("1000"))],
            trades=[],
            orders=[],
            fills=[],
            reports=[
                PaperExecutionReport(
                    accepted=False,
                    status=PaperExecutionStatus.REJECTED,
                    reason="Risk decision rejected.",
                    account=account,
                    risk_decision=decision,
                    signal=signal,
                )
            ],
        )

        payload = build_backtest_dashboard_payload(result)

        self.assertEqual(
            payload["diagnostics"]["entry_rejection_reasons"],
            {
                "Requested leverage exceeds the configured or instrument limit: 3 > 2.": 1
            },
        )

    def test_run_quality_marks_strong_paper_run(self) -> None:
        config = BacktestConfig(
            pair="B-SOL_USDT",
            interval="1h",
            starting_equity=Decimal("1000"),
            leverage=Decimal("3"),
            strategy_name="adaptive_hybrid",
        )
        account = PaperAccountSnapshot(
            starting_equity=Decimal("1000"),
            realized_pnl=Decimal("100"),
            unrealized_pnl=Decimal("0"),
            fees_paid=Decimal("10"),
            equity=Decimal("1090"),
            open_position_count=0,
            open_notional=Decimal("0"),
        )
        result = BacktestResult(
            config=config,
            candles_loaded=1000,
            candles_used=1000,
            metrics=BacktestMetrics(
                starting_equity=Decimal("1000"),
                final_equity=Decimal("1090"),
                total_return=Decimal("90"),
                total_return_pct=Decimal("9"),
                realized_pnl=Decimal("100"),
                unrealized_pnl=Decimal("0"),
                fees_paid=Decimal("10"),
                net_pnl=Decimal("90"),
                trade_count=10,
                winning_trades=6,
                losing_trades=4,
                win_rate_pct=Decimal("60"),
                average_win=Decimal("20"),
                average_loss=Decimal("-10"),
                profit_factor=Decimal("2"),
                max_drawdown=Decimal("30"),
                max_drawdown_pct=Decimal("3"),
                sharpe_ratio=Decimal("1.5"),
            ),
            final_account=account,
            equity_curve=[_equity_point(1, Decimal("1090"))],
            trades=[],
            orders=[],
            fills=[],
        )

        self.assertEqual(classify_run_quality(result)["label"], "Watchlist+")

    def test_backtest_payload_contains_signal_funnel_with_blocked_reasons(self) -> None:
        config = BacktestConfig(
            pair="B-SOL_USDT",
            interval="1h",
            starting_equity=Decimal("1000"),
            leverage=Decimal("3"),
            requested_candles=1000,
        )
        account = PaperAccountSnapshot(
            starting_equity=Decimal("1000"),
            realized_pnl=Decimal("0"),
            unrealized_pnl=Decimal("0"),
            fees_paid=Decimal("0"),
            equity=Decimal("1000"),
            open_position_count=0,
            open_notional=Decimal("0"),
        )
        signal = StrategySignal(
            strategy_name="test",
            pair="B-SOL_USDT",
            interval="1h",
            action=SignalAction.ENTER_LONG,
            direction=SignalDirection.LONG,
            confidence=Decimal("1"),
            reason="test",
            timestamp_ms=1,
            entry_price=Decimal("100"),
            stop_loss=Decimal("90"),
            metadata={"signal_funnel_raw_candidate": True, "signal_funnel_raw_direction": "long"},
        )
        reports = [
            PaperExecutionReport(
                accepted=False,
                status=PaperExecutionStatus.REJECTED,
                reason="Components do not agree on trend.",
                account=account,
                signal=signal,
            ),
            PaperExecutionReport(
                accepted=False,
                status=PaperExecutionStatus.REJECTED,
                reason="Visual screen blocked entry.",
                account=account,
                signal=signal,
            ),
        ]
        result = BacktestResult(
            config=config,
            candles_loaded=500,
            candles_used=500,
            metrics=BacktestMetrics(
                starting_equity=Decimal("1000"),
                final_equity=Decimal("1000"),
                total_return=Decimal("0"),
                total_return_pct=Decimal("0"),
                realized_pnl=Decimal("0"),
                unrealized_pnl=Decimal("0"),
                fees_paid=Decimal("0"),
                net_pnl=Decimal("0"),
                trade_count=0,
                winning_trades=0,
                losing_trades=4,
                win_rate_pct=None,
                average_win=None,
                average_loss=None,
                profit_factor=None,
                max_drawdown=Decimal("0"),
                max_drawdown_pct=Decimal("0"),
                sharpe_ratio=None,
            ),
            final_account=account,
            equity_curve=[_equity_point(1, Decimal("1000"))],
            trades=[],
            orders=[],
            fills=[],
            reports=reports,
        )

        payload = build_backtest_dashboard_payload(result)
        funnel = payload["signal_funnel"]

        self.assertEqual(funnel["requested_candles"], 1000)
        self.assertEqual(funnel["actual_candles_loaded"], 500)
        self.assertEqual(funnel["block_reasons"]["agreement_below_minimum"], 1)
        self.assertEqual(funnel["block_reasons"]["visual_screen_blocked"], 1)
        self.assertEqual(funnel["raw_long_candidates"], 2)

    def test_backtest_payload_handles_missing_funnel_fields(self) -> None:
        # Simulate a result that might be missing some fields if backtest_signal_funnel was older
        config = BacktestConfig(
            pair="B-SOL_USDT",
            interval="1h",
            starting_equity=Decimal("1000"),
            leverage=Decimal("3"),
        )
        account = PaperAccountSnapshot(
            starting_equity=Decimal("1000"),
            realized_pnl=Decimal("0"),
            unrealized_pnl=Decimal("0"),
            fees_paid=Decimal("0"),
            equity=Decimal("1000"),
            open_position_count=0,
            open_notional=Decimal("0"),
        )
        result = BacktestResult(
            config=config,
            candles_loaded=1,
            candles_used=1,
            metrics=BacktestMetrics(
                starting_equity=Decimal("1000"),
                final_equity=Decimal("1000"),
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
            ),
            final_account=account,
            equity_curve=[_equity_point(1, Decimal("1000"))],
            trades=[],
            orders=[],
            fills=[],
        )

        payload = build_backtest_dashboard_payload(result)
        # Even if backtest_signal_funnel was somehow compromised, to_dict should still provide a structure.
        # We ensure it is present and has the expected keys.
        self.assertIn("signal_funnel", payload)
        self.assertIn("requested_candles", payload["signal_funnel"])
        self.assertIn("block_reasons", payload["signal_funnel"])
        self.assertIn("cooldown_blocked", payload["signal_funnel"]["block_reasons"])


if __name__ == "__main__":
    unittest.main()
