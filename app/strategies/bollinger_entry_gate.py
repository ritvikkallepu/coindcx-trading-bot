"""Bollinger entry-quality gate used when BB trail mode is enabled."""

from __future__ import annotations

from decimal import Decimal
from typing import Sequence

from app.data.candle_builder import OHLCVCandle
from app.data.indicators import BollingerBandPoint
from app.strategies.base import SignalDirection


def evaluate_bb_entry_gate(
    *,
    direction: SignalDirection,
    entry_price: Decimal,
    entry_candle: OHLCVCandle,
    recent_candles: Sequence[OHLCVCandle],
    bands: Sequence[BollingerBandPoint | None],
    atr: Decimal,
    enabled: bool,
    touch_lookback: int = 5,
    touch_buffer_atr: Decimal = Decimal("0.25"),
    long_max_position: Decimal = Decimal("0.45"),
    short_min_position: Decimal = Decimal("0.55"),
    max_middle_slope_atr: Decimal = Decimal("0.75"),
) -> dict[str, object]:
    """Return pass/fail metadata for the BB entry gate.

    Band position is normalized as lower=0, middle=0.5, upper=1.
    """

    base_metadata: dict[str, object] = {
        "bb_entry_gate_enabled": bool(enabled),
        "bb_entry_gate_passed": True,
        "bb_entry_gate_reason": "disabled",
    }
    if not enabled:
        return base_metadata

    band = _latest_band(bands)
    if band is None or band.upper <= band.lower:
        return {
            **base_metadata,
            "bb_entry_gate_passed": False,
            "bb_entry_gate_reason": "BB entry gate blocked: Bollinger bands unavailable.",
        }
    if atr <= 0:
        return {
            **base_metadata,
            "bb_entry_gate_passed": False,
            "bb_entry_gate_reason": "BB entry gate blocked: ATR unavailable.",
        }

    width = band.upper - band.lower
    band_position = (entry_price - band.lower) / width
    candles = list(recent_candles)[-max(1, int(touch_lookback)) :]
    candles.append(entry_candle)
    buffer = atr * touch_buffer_atr
    middle_slope_atr = _middle_slope_atr(bands, atr)

    if direction == SignalDirection.LONG:
        touched = min(c.low for c in candles) <= band.lower + buffer
        reclaimed = entry_price >= band.lower
        zone_ok = band_position <= long_max_position
        slope_ok = middle_slope_atr >= -max_middle_slope_atr
        required_zone = f"<= {long_max_position}"
        side_text = "long"
        band_text = "lower"
    else:
        touched = max(c.high for c in candles) >= band.upper - buffer
        reclaimed = entry_price <= band.upper
        zone_ok = band_position >= short_min_position
        slope_ok = middle_slope_atr <= max_middle_slope_atr
        required_zone = f">= {short_min_position}"
        side_text = "short"
        band_text = "upper"

    metadata = {
        **base_metadata,
        "bb_entry_gate_reason": "passed",
        "bb_entry_band_position": band_position,
        "bb_entry_recent_touch": touched,
        "bb_entry_reclaimed_band": reclaimed,
        "bb_entry_middle_slope_atr": middle_slope_atr,
        "bb_entry_required_zone": required_zone,
        "bb_entry_lower": band.lower,
        "bb_entry_middle": band.middle,
        "bb_entry_upper": band.upper,
    }

    if not touched:
        return _blocked(
            metadata,
            f"BB entry gate blocked {side_text}: no recent {band_text}-band touch.",
        )
    if not reclaimed:
        return _blocked(
            metadata,
            f"BB entry gate blocked {side_text}: price has not reclaimed the {band_text} band.",
        )
    if not zone_ok:
        return _blocked(
            metadata,
            (
                f"BB entry gate blocked {side_text}: band position "
                f"{band_position:.2f} is outside required zone {required_zone}."
            ),
        )
    if not slope_ok:
        return _blocked(
            metadata,
            (
                f"BB entry gate blocked {side_text}: middle-band slope "
                f"{middle_slope_atr:.2f} ATR is too strongly against the trade."
            ),
        )

    return metadata


def _blocked(metadata: dict[str, object], reason: str) -> dict[str, object]:
    return {
        **metadata,
        "bb_entry_gate_passed": False,
        "bb_entry_gate_reason": reason,
    }


def _latest_band(
    bands: Sequence[BollingerBandPoint | None],
) -> BollingerBandPoint | None:
    for band in reversed(bands):
        if band is not None:
            return band
    return None


def _middle_slope_atr(
    bands: Sequence[BollingerBandPoint | None],
    atr: Decimal,
) -> Decimal:
    if atr <= 0:
        return Decimal("0")
    valid = [band for band in bands if band is not None]
    if len(valid) < 2:
        return Decimal("0")
    previous = valid[-min(4, len(valid))]
    latest = valid[-1]
    return (latest.middle - previous.middle) / atr
