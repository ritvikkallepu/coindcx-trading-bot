"""Shared entry-quality rules for directional hybrid strategies."""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from typing import Iterable

from app.strategies.base import SignalDirection


A_SETUP_AGREEMENT_THRESHOLD = Decimal("0.67")
B_SETUP_AGREEMENT_THRESHOLD = Decimal("0.50")
DEFAULT_DIRECTIONAL_ENTRY_THRESHOLD = Decimal("0.40")


def active_hybrid_components(
    *,
    ema_score: Decimal | None,
    visual_score: Decimal | None,
    bb_score: Decimal | None,
    bb_active: bool,
    oi_score: Decimal | None,
    oi_active: bool,
) -> tuple[Decimal, ...]:
    """Return only components that actively participate in the hybrid score."""

    components = [score for score in (ema_score, visual_score) if score is not None]
    if bb_active and bb_score is not None:
        components.append(bb_score)
    if oi_active and oi_score is not None:
        components.append(oi_score)
    return tuple(components)


def agreement_ratio(
    direction: SignalDirection,
    scores: Iterable[Decimal],
) -> Decimal:
    """Return the directional agreement ratio rounded for stable thresholds."""

    active_scores = tuple(scores)
    if not active_scores:
        return Decimal("0")
    matching = sum(
        1
        for score in active_scores
        if (score > 0 if direction == SignalDirection.LONG else score < 0)
    )
    ratio = Decimal(matching) / Decimal(len(active_scores))
    return ratio.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def directional_trend_confirmation(
    *,
    direction: SignalDirection,
    entry_price: Decimal,
    fast_ema: Decimal | None,
    slow_ema: Decimal | None,
    previous_slow_ema: Decimal | None,
) -> tuple[bool, str, dict[str, object]]:
    """Apply mirrored EMA trend, slope, and price confirmation."""

    metadata: dict[str, object] = {
        "directional_confirmation_checked": False,
    }
    if fast_ema is None or slow_ema is None or previous_slow_ema is None:
        return True, "directional_confirmation_unavailable", metadata

    if direction == SignalDirection.LONG:
        trend_aligned = fast_ema > slow_ema
        slope_aligned = slow_ema >= previous_slow_ema
        price_aligned = entry_price >= fast_ema
    else:
        trend_aligned = fast_ema < slow_ema
        slope_aligned = slow_ema <= previous_slow_ema
        price_aligned = entry_price <= fast_ema

    metadata.update(
        {
            "directional_confirmation_checked": True,
            "directional_trend_aligned": trend_aligned,
            "directional_slow_slope_aligned": slope_aligned,
            "directional_price_aligned": price_aligned,
        }
    )
    failed = []
    if not trend_aligned:
        failed.append("EMA trend")
    if not slope_aligned:
        failed.append("slow EMA slope")
    if not price_aligned:
        failed.append("price versus fast EMA")
    if failed:
        return False, f"{direction.value} confirmation failed: {', '.join(failed)}", metadata
    return True, f"{direction.value} confirmation passed", metadata
