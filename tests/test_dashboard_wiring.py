from __future__ import annotations

import unittest
from decimal import Decimal

from app.config import Settings
from app.dashboard.state import DashboardDefaults
from app.dashboard.server import run_backtest_for_dashboard
from unittest.mock import patch, MagicMock


class DashboardWiringTests(unittest.TestCase):
    @patch("app.dashboard.server.load_historical_candle_series_between")
    @patch("app.dashboard.server.load_historical_candle_series")
    @patch("app.dashboard.server.CoinDCXFuturesClient")
    @patch("app.dashboard.server.BacktestEngine")
    def test_dashboard_inputs_reach_backtest_config(
        self, mock_engine_cls, mock_client_cls, mock_load_candles, mock_load_candles_between
    ) -> None:
        from app.backtest.models import BacktestResult, BacktestMetrics, BacktestConfig
        from app.broker.models import PaperAccountSnapshot
        
        config_template = BacktestConfig(
            pair="B-BTC_USDT", interval="1h", starting_equity=Decimal("1000"), leverage=Decimal("3")
        )
        
        metrics = BacktestMetrics(
            starting_equity=Decimal("1000"), final_equity=Decimal("1000"),
            total_return=Decimal("0"), total_return_pct=Decimal("0"),
            realized_pnl=Decimal("0"), unrealized_pnl=Decimal("0"),
            fees_paid=Decimal("0"), net_pnl=Decimal("0"),
            trade_count=0, winning_trades=0, losing_trades=0,
            win_rate_pct=None, average_win=None, average_loss=None,
            profit_factor=None, max_drawdown=Decimal("0"),
            max_drawdown_pct=Decimal("0"), sharpe_ratio=None
        )
        
        account = PaperAccountSnapshot(
            starting_equity=Decimal("1000"), realized_pnl=Decimal("0"),
            unrealized_pnl=Decimal("0"), fees_paid=Decimal("0"),
            equity=Decimal("1000"), open_position_count=0, open_notional=Decimal("0")
        )
        
        mock_result = BacktestResult(
            config=config_template,
            candles_loaded=1, candles_used=1,
            metrics=metrics, final_account=account,
            equity_curve=[], trades=[], orders=[], fills=[]
        )
        
        mock_engine = mock_engine_cls.return_value
        mock_engine.run.return_value = mock_result
        mock_load_candles.return_value = [MagicMock()]
        
        params = {
            "pair": "B-BTC_USDT",
            "risk_per_trade_pct": "1.5",
            "max_daily_loss_pct": "12.5",
            "trailing_stop_enabled": "on",
            "trailing_stop_activation_pct": "1.2",
            "trailing_stop_distance_pct": "2.5",
            "atr_dynamic_exits_enabled": "on",
            "atr_policy_mode": "router",
            "atr_stop_multiple": "1.8",
            "atr_trailing_multiple": "2.2",
            "funding_fee_pct": "0.01",
            "funding_interval_hours": "4",
            "slippage_pct": "0.05",
            "stop_slippage_pct": "0.08",
            "intrabar_reentry_enabled": "off",
        }
        
        run_backtest_for_dashboard(
            settings=Settings(),
            defaults=DashboardDefaults(),
            params=params,
        )
        
        # Check BacktestConfig passed to BacktestEngine
        config = mock_engine_cls.call_args[1]["config"]
        self.assertEqual(config.pair, "B-BTC_USDT")
        self.assertEqual(config.risk_per_trade_pct, Decimal("1.5"))
        self.assertEqual(config.max_daily_loss_pct, Decimal("12.5"))
        self.assertTrue(config.trailing_stop_enabled)
        self.assertEqual(config.trailing_stop_activation_pct, Decimal("1.2"))
        self.assertEqual(config.trailing_stop_distance_pct, Decimal("2.5"))
        self.assertTrue(config.atr_dynamic_exits_enabled)
        self.assertEqual(config.atr_policy_mode, "router")
        self.assertEqual(config.atr_stop_multiple, Decimal("1.8"))
        self.assertEqual(config.atr_trailing_multiple, Decimal("2.2"))
        self.assertEqual(config.funding_fee_rate, Decimal("0.0001"))
        self.assertEqual(config.funding_interval_hours, 4)
        self.assertEqual(config.slippage_pct, Decimal("0.05"))
        self.assertEqual(config.stop_slippage_pct, Decimal("0.08"))
        
        # Check RiskManager settings
        risk_manager = mock_engine_cls.call_args[1]["risk_manager"]
        self.assertEqual(risk_manager.settings.max_risk_per_trade_pct, Decimal("1.5"))
        self.assertEqual(risk_manager.settings.max_daily_loss_pct, Decimal("12.5"))
        self.assertTrue(risk_manager.settings.trailing_stop_enabled)
        self.assertEqual(risk_manager.settings.trailing_stop_activation_pct, Decimal("1.2"))
        self.assertEqual(risk_manager.settings.trailing_stop_distance_pct, Decimal("2.5"))

if __name__ == "__main__":
    unittest.main()
