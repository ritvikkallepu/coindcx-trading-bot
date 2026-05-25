"""
Bollinger Band + ATR hybrid trailing stop policy.

BB gives the structural reference (middle band).
ATR gives the noise buffer so the stop does not sit exactly on the band.

For a long:
  candidate_stop = middle_band - ATR * buffer_multiplier
  active_stop    = max(previous_stop, candidate_stop)

For a short:
  candidate_stop = middle_band + ATR * buffer_multiplier
  active_stop    = min(previous_stop, candidate_stop)
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from app.strategies.base import SignalDirection

if TYPE_CHECKING:
    from app.broker.models import PaperPosition


logger = logging.getLogger(__name__)

_MIN_PRICE = Decimal("0.00000001")


def should_activate(
    position: PaperPosition,
    current_r: Decimal,
    activation_r: Decimal,
    middle_band: Decimal,
) -> bool:
    """Return True when BB trail is allowed to take over from the static stop."""

    if current_r < activation_r:
        return False
    if position.direction == SignalDirection.LONG:
        return middle_band > position.entry_price
    return middle_band < position.entry_price


def compute_candidate_stop(
    middle_band: Decimal,
    atr: Decimal,
    buffer_multiplier: Decimal,
    direction: SignalDirection,
) -> Decimal:
    """Return the raw candidate stop before ratcheting."""

    buffer = atr * buffer_multiplier
    if direction == SignalDirection.LONG:
        return max(middle_band - buffer, _MIN_PRICE)
    return max(middle_band + buffer, _MIN_PRICE)


def ratchet_stop(
    previous_stop: Decimal | None,
    candidate_stop: Decimal,
    direction: SignalDirection,
) -> Decimal:
    """Apply the one-direction ratchet."""

    if previous_stop is None:
        return candidate_stop
    if direction == SignalDirection.LONG:
        return max(previous_stop, candidate_stop)
    return min(previous_stop, candidate_stop)


def advance_stage(
    current_stage: int,
    current_r: Decimal,
    stage2_r: Decimal,
    stage3_r: Decimal,
) -> int:
    """Advance the BB trail stage without ever decreasing it."""

    stage = max(1, min(int(current_stage), 3))
    if current_r >= stage3_r:
        return max(stage, 3)
    if current_r >= stage2_r:
        return max(stage, 2)
    return stage


def update_bb_trail(
    position: PaperPosition,
    current_r: Decimal,
    middle_band: Decimal,
    upper_band: Decimal,
    lower_band: Decimal,
    atr: Decimal,
    buffer_multiplier: Decimal,
    activation_r: Decimal,
    stage2_r: Decimal,
    stage3_r: Decimal,
    force_close_r: Decimal,
) -> dict:
    """Return updated BB trail metadata for the caller to merge into a position."""

    metadata = position.metadata if isinstance(position.metadata, dict) else {}
    previous_stop = _decimal_or_none(metadata.get("bb_trail_stop"))
    previous_active = _bool_value(metadata.get("bb_trail_active"), False)
    current_stage = _int_value(metadata.get("bb_trail_stage"), 1)
    active = previous_active or should_activate(
        position=position,
        current_r=current_r,
        activation_r=activation_r,
        middle_band=middle_band,
    )
    stage = advance_stage(current_stage, current_r, stage2_r, stage3_r)
    buffer_atr = atr * buffer_multiplier
    band_width = upper_band - lower_band
    best_price = _decimal_or_none(metadata.get("atr_best_price")) or position.entry_price
    stop = previous_stop

    if active:
        if band_width < atr * Decimal("0.5"):
            logger.debug(
                "BB trail squeeze guard held stop for %s: width=%s atr=%s",
                position.pair,
                band_width,
                atr,
            )
        else:
            candidate = compute_candidate_stop(
                middle_band=middle_band,
                atr=atr,
                buffer_multiplier=buffer_multiplier,
                direction=position.direction,
            )
            stop = ratchet_stop(previous_stop, candidate, position.direction)

    if stop is not None and stop <= 0:
        stop = None

    return {
        "bb_trail_enabled": True,
        "bb_trail_active": bool(active),
        "bb_trail_stop": stop,
        "bb_trail_stage": int(stage),
        "bb_trail_best_price": best_price,
        "bb_mid": middle_band,
        "bb_upper": upper_band,
        "bb_lower": lower_band,
        "bb_buffer_atr": buffer_atr,
        "bb_trail_force_close": current_r >= force_close_r,
        "stop_type": "bb_trail" if active and stop is not None else "atr",
    }


def _decimal_or_none(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except Exception:
        return None


def _bool_value(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _int_value(value: Any, default: int) -> int:
    try:
        return int(Decimal(str(value)))
    except Exception:
        return default
