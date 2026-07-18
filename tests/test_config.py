from __future__ import annotations

import tempfile
import unittest
import os
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from app.config import load_settings


class ConfigTests(unittest.TestCase):
    def test_loads_dotenv_and_redacts_secrets(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with tempfile.TemporaryDirectory() as tmp:
                env_file = Path(tmp) / ".env"
                env_file.write_text(
                    "\n".join(
                        [
                            "COINDCX_API_KEY=abcd1234efgh",
                            "COINDCX_API_SECRET=secret1234value",
                            "TRADING_MODE=paper",
                            "LIVE_TRADING_ENABLED=false",
                        ]
                    ),
                    encoding="utf-8",
                )

                settings = load_settings(env_file)
                self.assertTrue(settings.is_paper_trading)
                self.assertFalse(settings.live_trading_allowed)
                self.assertEqual(settings.futures_margin_currency, "INR")
                self.assertEqual(settings.paper_starting_equity, Decimal("100000"))
                self.assertEqual(settings.paper_starting_equity_currency, "INR")
                self.assertEqual(settings.risk.atr_trailing_multiple, Decimal("1.2"))
                safe = settings.safe_dict()
                self.assertEqual(safe["coindcx_api_key"], "abcd...efgh")
                self.assertEqual(safe["coindcx_api_secret"], "secr...alue")

    def test_live_trading_requires_mode_and_enable_flag(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with tempfile.TemporaryDirectory() as tmp:
                env_file = Path(tmp) / ".env"
                env_file.write_text(
                    "\n".join(
                        [
                            "TRADING_MODE=paper",
                            "LIVE_TRADING_ENABLED=true",
                        ]
                    ),
                    encoding="utf-8",
                )
                self.assertFalse(load_settings(env_file).live_trading_allowed)

                env_file.write_text(
                    "\n".join(
                        [
                            "TRADING_MODE=live",
                            "LIVE_TRADING_ENABLED=true",
                        ]
                    ),
                    encoding="utf-8",
                )
                self.assertTrue(load_settings(env_file).live_trading_allowed)

    def test_loads_risk_settings_from_env(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with tempfile.TemporaryDirectory() as tmp:
                env_file = Path(tmp) / ".env"
                env_file.write_text(
                    "\n".join(
                        [
                            "MAX_RISK_PER_TRADE_PCT=0.5",
                            "MAX_DAILY_LOSS_PCT=2.5",
                            "MAX_OPEN_POSITIONS=3",
                            "MAX_LEVERAGE=4",
                            "ENFORCE_EXCHANGE_LEVERAGE_LIMIT=false",
                            "MAX_TOTAL_OPEN_NOTIONAL_PCT=250",
                            "MAX_TOTAL_RISK_PCT=6",
                            "MAX_MARGIN_PER_PAIR=750",
                            "LIQUIDATION_BUFFER_PCT=1.5",
                            "TRAILING_STOP_ENABLED=true",
                            "TRAILING_STOP_ACTIVATION_PCT=1.25",
                            "TRAILING_STOP_DISTANCE_PCT=2.75",
                            "LIVE_RISK_APPROVAL_ENABLED=true",
                            "LIVE_MAX_ORDER_NOTIONAL=1500",
                            "LIVE_MAX_MARGIN_PER_ORDER=400",
                            "LIVE_REQUIRE_STOP_LOSS=false",
                            "LIVE_PILOT_DRY_RUN=false",
                            "LIVE_POSITION_MARGIN_TYPE=isolated",
                        ]
                    ),
                    encoding="utf-8",
                )

                settings = load_settings(env_file)
                risk = settings.risk
                self.assertEqual(risk.max_risk_per_trade_pct, Decimal("0.5"))
                self.assertEqual(risk.max_daily_loss_pct, Decimal("2.5"))
                self.assertEqual(risk.max_open_positions, 3)
                self.assertEqual(risk.max_leverage, 4)
                self.assertFalse(risk.enforce_exchange_leverage_limit)
                self.assertEqual(risk.max_total_open_notional_pct, Decimal("250"))
                self.assertEqual(risk.max_total_risk_pct, Decimal("6"))
                self.assertEqual(risk.max_margin_per_pair, Decimal("750"))
                self.assertEqual(risk.liquidation_buffer_pct, Decimal("1.5"))
                self.assertTrue(risk.trailing_stop_enabled)
                self.assertEqual(risk.trailing_stop_activation_pct, Decimal("1.25"))
            self.assertEqual(risk.trailing_stop_distance_pct, Decimal("2.75"))
            self.assertTrue(risk.live_risk_approval_enabled)
            self.assertEqual(risk.live_max_order_notional, Decimal("1500"))
            self.assertEqual(risk.live_max_margin_per_order, Decimal("400"))
            self.assertFalse(risk.live_require_stop_loss)
            self.assertFalse(settings.live_pilot_dry_run)
            self.assertEqual(settings.live_position_margin_type, "isolated")


if __name__ == "__main__":
    unittest.main()
