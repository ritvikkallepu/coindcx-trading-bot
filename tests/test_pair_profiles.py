from __future__ import annotations

import unittest
from decimal import Decimal

from app.risk.pair_profiles import (
    apply_pair_profile_to_config,
    base_symbol,
    pair_profile_for,
)


class PairProfileTests(unittest.TestCase):
    def test_base_symbol_normalizes_inr_futures_pairs(self) -> None:
        self.assertEqual(base_symbol("B-BTC_USDT"), "BTC")
        self.assertEqual(base_symbol(" sol_usdt "), "SOL")
        self.assertEqual(base_symbol("ETH-USDT"), "ETH")

    def test_major_liquid_profile_applies_to_large_liquid_pairs(self) -> None:
        for pair in ("B-BTC_USDT", "B-ETH_USDT", "B-SOL_USDT"):
            with self.subTest(pair=pair):
                self.assertEqual(pair_profile_for(pair).key, "major_liquid")

    def test_default_alt_profile_applies_to_thinner_alt_pairs(self) -> None:
        self.assertEqual(pair_profile_for("B-BSB_USDT").key, "volatile_alt")
        self.assertEqual(pair_profile_for("B-ALICE_USDT").key, "volatile_alt")

    def test_major_profile_overrides_entry_timing_without_mutating_input(self) -> None:
        base_config = {
            "balanced_breakout_volume_ratio_min": Decimal("1.80"),
            "late_chase_max_extension_atr": Decimal("2.20"),
        }

        merged, profile = apply_pair_profile_to_config("B-SOL_USDT", base_config)

        self.assertEqual(profile.key, "major_liquid")
        self.assertEqual(merged["pair_profile"], "major_liquid")
        self.assertEqual(merged["balanced_breakout_volume_ratio_min"], Decimal("1.25"))
        self.assertEqual(merged["late_chase_max_extension_atr"], Decimal("2.00"))
        self.assertEqual(base_config["balanced_breakout_volume_ratio_min"], Decimal("1.80"))

    def test_alt_profile_keeps_existing_config_values(self) -> None:
        merged, profile = apply_pair_profile_to_config(
            "B-BSB_USDT",
            {"balanced_breakout_volume_ratio_min": Decimal("1.80")},
        )

        self.assertEqual(profile.key, "volatile_alt")
        self.assertEqual(merged["balanced_breakout_volume_ratio_min"], Decimal("1.80"))


if __name__ == "__main__":
    unittest.main()
