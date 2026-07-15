from __future__ import annotations

import unittest
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

from app.backtest.models import BacktestConfig
from app.config import RiskSettings, load_settings
from app.strategies.base import SignalDirection
from app.strategies.entry_quality import (
    A_SETUP_AGREEMENT_THRESHOLD,
    B_SETUP_AGREEMENT_THRESHOLD,
    active_hybrid_components,
    agreement_ratio,
    directional_trend_confirmation,
)
from app.strategies.hybrid_meta import HybridMetaV2Strategy


class EntryQualityTests(unittest.TestCase):
    def test_inactive_indicators_are_excluded_from_agreement(self) -> None:
        scores = active_hybrid_components(
            ema_score=Decimal("-0.7"),
            visual_score=Decimal("-0.4"),
            bb_score=Decimal("0"),
            bb_active=False,
            oi_score=Decimal("0"),
            oi_active=False,
        )

        self.assertEqual(scores, (Decimal("-0.7"), Decimal("-0.4")))
        self.assertEqual(
            agreement_ratio(SignalDirection.SHORT, scores),
            Decimal("1.00"),
        )

    def test_two_of_three_components_rounds_to_a_tier_agreement(self) -> None:
        scores = (Decimal("-0.7"), Decimal("-0.4"), Decimal("0.2"))

        self.assertEqual(
            agreement_ratio(SignalDirection.SHORT, scores),
            A_SETUP_AGREEMENT_THRESHOLD,
        )

    def test_agreement_is_mirrored_between_directions(self) -> None:
        scores = (Decimal("-0.7"), Decimal("-0.4"), Decimal("0.2"))

        self.assertEqual(
            agreement_ratio(SignalDirection.SHORT, scores), Decimal("0.67")
        )
        self.assertEqual(
            agreement_ratio(SignalDirection.LONG, scores), Decimal("0.33")
        )

    def test_small_rebound_does_not_confirm_a_long_in_bearish_trend(self) -> None:
        approved, reason, metadata = directional_trend_confirmation(
            direction=SignalDirection.LONG,
            entry_price=Decimal("99"),
            fast_ema=Decimal("98"),
            slow_ema=Decimal("100"),
            previous_slow_ema=Decimal("101"),
        )

        self.assertFalse(approved)
        self.assertIn("EMA trend", reason)
        self.assertIn("slow EMA slope", reason)
        self.assertTrue(metadata["directional_price_aligned"])

    def test_established_bearish_trend_confirms_a_short(self) -> None:
        approved, _, metadata = directional_trend_confirmation(
            direction=SignalDirection.SHORT,
            entry_price=Decimal("97"),
            fast_ema=Decimal("98"),
            slow_ema=Decimal("100"),
            previous_slow_ema=Decimal("101"),
        )

        self.assertTrue(approved)
        self.assertTrue(metadata["directional_trend_aligned"])
        self.assertTrue(metadata["directional_slow_slope_aligned"])
        self.assertTrue(metadata["directional_price_aligned"])

    def test_defaults_are_symmetric_across_risk_and_backtest(self) -> None:
        risk = RiskSettings()
        backtest = BacktestConfig(
            pair="B-BTC_USDT",
            interval="5m",
            starting_equity=Decimal("100000"),
            leverage=Decimal("3"),
        )

        self.assertFalse(risk.short_strictness_enabled)
        self.assertEqual(risk.short_confidence_bonus, Decimal("0"))
        self.assertEqual(risk.short_min_agreement_bonus, Decimal("0"))
        self.assertEqual(backtest.long_entry_threshold, backtest.short_entry_threshold)
        self.assertEqual(
            backtest.b_setup_agreement_threshold,
            B_SETUP_AGREEMENT_THRESHOLD,
        )
        self.assertEqual(
            backtest.short_agreement_threshold,
            B_SETUP_AGREEMENT_THRESHOLD,
        )
        self.assertEqual(
            HybridMetaV2Strategy().entry_threshold,
            backtest.long_entry_threshold,
        )

    def test_environment_loader_does_not_reenable_short_strictness(self) -> None:
        with TemporaryDirectory() as temp_dir:
            settings = load_settings(Path(temp_dir) / "missing.env")

        self.assertFalse(settings.risk.short_strictness_enabled)
        self.assertEqual(settings.risk.short_confidence_bonus, Decimal("0"))
        self.assertEqual(settings.risk.short_min_agreement_bonus, Decimal("0"))


if __name__ == "__main__":
    unittest.main()
