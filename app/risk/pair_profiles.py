from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from app.strategies.entry_quality import (
    A_SETUP_AGREEMENT_THRESHOLD,
    B_SETUP_AGREEMENT_THRESHOLD,
    DEFAULT_DIRECTIONAL_ENTRY_THRESHOLD,
)


MAJOR_LIQUID_BASES = frozenset(
    {
        "BTC",
        "ETH",
        "SOL",
        "XRP",
        "BNB",
        "DOGE",
        "ADA",
        "AVAX",
        "LINK",
        "LTC",
        "BCH",
        "DOT",
        "TRX",
        "NEAR",
        "SUI",
        "ATOM",
        "UNI",
        "AAVE",
        "ARB",
        "OP",
        "FIL",
        "ETC",
    }
)


@dataclass(frozen=True)
class PairTradingProfile:
    key: str
    label: str
    description: str
    config_overrides: dict[str, Any]

    def metadata(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "description": self.description,
            "config_overrides": self.config_overrides,
        }


DEFAULT_ALT_PROFILE = PairTradingProfile(
    key="volatile_alt",
    label="Volatile Alt",
    description="Default fast-move profile for thinner INR-M alt pairs.",
    config_overrides={},
)


MAJOR_LIQUID_PROFILE = PairTradingProfile(
    key="major_liquid",
    label="Major Liquid",
    description="Lower relative-volume burst requirements and calmer risk for high-liquidity majors.",
    config_overrides={
        "trade_quality_mode": "tiered",
        "long_entry_threshold": DEFAULT_DIRECTIONAL_ENTRY_THRESHOLD,
        "short_entry_threshold": DEFAULT_DIRECTIONAL_ENTRY_THRESHOLD,
        "short_agreement_threshold": B_SETUP_AGREEMENT_THRESHOLD,
        "a_setup_score_threshold": Decimal("0.48"),
        "a_setup_agreement_threshold": A_SETUP_AGREEMENT_THRESHOLD,
        "b_setup_score_threshold": Decimal("0.36"),
        "b_setup_agreement_threshold": B_SETUP_AGREEMENT_THRESHOLD,
        "b_setup_risk_multiplier": Decimal("0.50"),
        "balanced_breakout_volume_ratio_min": Decimal("1.25"),
        "balanced_breakout_body_ratio_min": Decimal("0.50"),
        "balanced_breakout_close_position_min": Decimal("0.62"),
        "balanced_breakout_max_extension_atr": Decimal("1.80"),
        "balanced_breakout_max_age_candles": 3,
        "balanced_breakout_risk_multiplier": Decimal("0.50"),
        "reversal_breakout_volume_ratio": Decimal("1.35"),
        "reversal_breakout_body_ratio": Decimal("0.55"),
        "reversal_breakout_close_position_ratio": Decimal("0.62"),
        "reversal_breakout_max_extension_atr": Decimal("1.90"),
        "reversal_breakout_ignition_volume_ratio": Decimal("2.20"),
        "reversal_breakout_ignition_body_ratio": Decimal("0.65"),
        "reversal_breakout_ignition_close_position_ratio": Decimal("0.70"),
        "reversal_breakout_ignition_max_extension_atr": Decimal("4.00"),
        "pullback_max_age_candles": 10,
        "pullback_max_distance_from_ema_atr": Decimal("0.80"),
        "pullback_resume_body_ratio_min": Decimal("0.40"),
        "late_chase_max_consecutive_impulse_candles": 4,
        "late_chase_volume_fade_ratio": Decimal("0.65"),
        "late_chase_max_extension_atr": Decimal("2.00"),
        "profile_risk_multiplier": Decimal("0.75"),
    },
)


def base_symbol(pair: str) -> str:
    symbol = str(pair or "").strip().upper().replace(" ", "")
    if symbol.startswith("B-"):
        symbol = symbol[2:]
    if "_" in symbol:
        return symbol.split("_", 1)[0]
    if "-" in symbol:
        return symbol.split("-", 1)[0]
    return symbol


def pair_profile_for(pair: str) -> PairTradingProfile:
    if base_symbol(pair) in MAJOR_LIQUID_BASES:
        return MAJOR_LIQUID_PROFILE
    return DEFAULT_ALT_PROFILE


def apply_pair_profile_to_config(
    pair: str,
    config: dict[str, Any],
) -> tuple[dict[str, Any], PairTradingProfile]:
    profile = pair_profile_for(pair)
    if not profile.config_overrides:
        merged = dict(config)
    else:
        merged = {**config, **profile.config_overrides}
    merged["pair_profile"] = profile.key
    merged["pair_profile_label"] = profile.label
    merged["pair_profile_description"] = profile.description
    return merged, profile
