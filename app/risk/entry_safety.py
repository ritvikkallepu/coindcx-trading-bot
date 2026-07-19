from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Mapping

from app.config import RiskSettings
from app.strategies.base import SignalAction, StrategySignal
from app.strategies.entry_quality import B_SETUP_AGREEMENT_THRESHOLD


@dataclass(frozen=True)
class EntrySafetyAssessment:
    approved: bool
    reason: str | None = None
    metadata: dict[str, object] = field(default_factory=dict)


def assess_entry_safety(
    signal: StrategySignal,
    settings: RiskSettings,
) -> EntrySafetyAssessment:
    if not settings.entry_safety_enabled:
        return EntrySafetyAssessment(True)
    if signal.action not in {SignalAction.ENTER_LONG, SignalAction.ENTER_SHORT}:
        return EntrySafetyAssessment(True)
    if signal.entry_price is None or signal.entry_price <= 0:
        return EntrySafetyAssessment(True)
    if signal.stop_loss is None or signal.stop_loss <= 0:
        return EntrySafetyAssessment(True)

    entry_price = signal.entry_price
    stop_distance = abs(entry_price - signal.stop_loss)
    if stop_distance <= 0:
        return EntrySafetyAssessment(True)

    metadata: dict[str, object] = {}
    stop_distance_pct = (stop_distance / entry_price) * Decimal("100")
    metadata["entry_stop_distance_pct"] = stop_distance_pct

    if (
        settings.min_stop_distance_pct > 0
        and stop_distance_pct < settings.min_stop_distance_pct
    ):
        return EntrySafetyAssessment(
            False,
            (
                "entry_stop_distance_too_small: "
                f"{stop_distance_pct:.4f}% < {settings.min_stop_distance_pct}%"
            ),
            metadata,
        )

    atr = _first_decimal(
        signal.metadata,
        "atr_entry_atr",
        "entry_atr",
        "atr",
        "current_atr",
    )
    if atr is not None and atr > 0:
        atr_pct = (atr / entry_price) * Decimal("100")
        stop_atr_multiple = stop_distance / atr
        metadata["entry_atr_pct"] = atr_pct
        metadata["entry_stop_atr_multiple"] = stop_atr_multiple

        if settings.min_entry_atr_pct > 0 and atr_pct < settings.min_entry_atr_pct:
            return EntrySafetyAssessment(
                False,
                (
                    "entry_atr_too_low: "
                    f"{atr_pct:.4f}% < {settings.min_entry_atr_pct}%"
                ),
                metadata,
            )

        if (
            settings.min_stop_atr_multiple > 0
            and stop_atr_multiple < settings.min_stop_atr_multiple
        ):
            return EntrySafetyAssessment(
                False,
                (
                    "entry_stop_too_close_to_atr: "
                    f"{stop_atr_multiple:.4f}x < {settings.min_stop_atr_multiple}x"
                ),
                metadata,
            )

    volume_ratio = _entry_volume_ratio(signal.metadata)
    if volume_ratio is not None:
        metadata["entry_volume_ratio"] = volume_ratio
        if (
            settings.min_entry_volume_ratio > 0
            and volume_ratio < settings.min_entry_volume_ratio
        ):
            return EntrySafetyAssessment(
                False,
                (
                    "entry_liquidity_volume_too_low: "
                    f"{volume_ratio:.4f} < {settings.min_entry_volume_ratio}"
                ),
                metadata,
            )

    spread_pct = _first_decimal(
        signal.metadata,
        "entry_orderbook_spread_pct",
        "orderbook_spread_pct",
    )
    if spread_pct is not None:
        metadata["entry_orderbook_spread_pct"] = spread_pct
        if settings.max_entry_spread_pct > 0 and spread_pct > settings.max_entry_spread_pct:
            return EntrySafetyAssessment(
                False,
                (
                    "entry_spread_too_wide: "
                    f"{spread_pct:.4f}% > {settings.max_entry_spread_pct}%"
                ),
                metadata,
            )

    side_depth = _first_decimal(
        signal.metadata,
        "entry_orderbook_side_depth_margin",
        "orderbook_side_depth_margin",
    )
    if side_depth is not None:
        metadata["entry_orderbook_side_depth_margin"] = side_depth
        if (
            settings.min_entry_side_depth_margin > 0
            and side_depth < settings.min_entry_side_depth_margin
        ):
            return EntrySafetyAssessment(
                False,
                (
                    "entry_orderbook_depth_too_low: "
                    f"{side_depth:.4f} < {settings.min_entry_side_depth_margin}"
                ),
                metadata,
            )

    # 4. Short Strictness Filter
    if settings.short_strictness_enabled and signal.action == SignalAction.ENTER_SHORT:
        short_safety = _assess_short_strictness(signal, settings)
        if not short_safety.approved:
            return short_safety

    return EntrySafetyAssessment(True, metadata=metadata)


def _assess_short_strictness(
    signal: StrategySignal,
    settings: RiskSettings,
) -> EntrySafetyAssessment:
    metadata: dict[str, object] = {}
    failed_filters = []

    # A. Confidence Bonus
    required_conf = settings.live_min_confidence + settings.short_confidence_bonus
    metadata["short_required_confidence"] = required_conf
    metadata["actual_confidence"] = signal.confidence
    if signal.confidence < required_conf:
        failed_filters.append(f"confidence {signal.confidence:.2f} < {required_conf:.2f}")

    # B. Agreement Bonus. The fallback is the same shared B-tier threshold
    # used by both long and short strategy decisions.
    base_agreement = (
        _first_decimal(signal.metadata, "base_agreement_threshold")
        or B_SETUP_AGREEMENT_THRESHOLD
    )
    required_agreement = base_agreement + settings.short_min_agreement_bonus
    actual_agreement = _first_decimal(signal.metadata, "agreement_ratio", "short_agreement_ratio") or Decimal("0")
    
    metadata["short_required_agreement"] = required_agreement
    metadata["actual_agreement"] = actual_agreement
    
    if actual_agreement < required_agreement:
        failed_filters.append(f"agreement {actual_agreement:.2f} < {required_agreement:.2f}")

    # Indicators for trend/price
    ema_score = _first_decimal(signal.metadata, "ema_score")
    
    # C. Trend Confirmation
    if settings.short_require_trend_confirmation and ema_score is not None:
        if ema_score > Decimal("-0.30"): # Require at least mild bearish trend
            failed_filters.append("bearish trend confirmation missing")

    # D. Price Below EMA
    if settings.short_require_price_below_ema and ema_score is not None:
        if ema_score > 0: # ema_score > 0 means price above EMA
            failed_filters.append("price not below EMA")

    # E. Bearish Structure
    if settings.short_require_bearish_structure:
        struct_score = _first_decimal(signal.metadata, "structure_score")
        if struct_score is not None and struct_score > Decimal("-0.20"):
            failed_filters.append("bearish structure too weak")

    # F. Volume Confirmation
    if settings.short_require_volume_confirmation:
        vol_ratio = _entry_volume_ratio(signal.metadata)
        if vol_ratio is not None and vol_ratio < Decimal("1.2"): # Require some expansion for shorts
            failed_filters.append("volume confirmation too weak")

    if failed_filters:
        metadata["failed_short_filters"] = failed_filters
        return EntrySafetyAssessment(
            False,
            f"Rejected SHORT: {'; '.join(failed_filters)}",
            metadata
        )

    return EntrySafetyAssessment(True, metadata=metadata)


def _entry_volume_ratio(metadata: Mapping[str, Any]) -> Decimal | None:
    direct = _first_decimal(
        metadata,
        "entry_volume_ratio",
        "execution_volume_ratio",
        "volume_ratio",
    )
    if direct is not None:
        return direct

    visual = metadata.get("visual")
    if isinstance(visual, Mapping):
        return _first_decimal(visual, "volume_ratio")
    return None


def _first_decimal(values: Mapping[str, Any], *keys: str) -> Decimal | None:
    for key in keys:
        if key not in values:
            continue
        value = values.get(key)
        if value is None or value == "":
            continue
        if isinstance(value, Decimal):
            return value
        try:
            return Decimal(str(value))
        except Exception:
            continue
    return None
