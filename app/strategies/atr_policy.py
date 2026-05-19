from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Any

from app.strategies.base import SignalDirection


@dataclass(frozen=True)
class ATRPolicy:
    trade_mode: str
    atr_profile: str
    entry_allowed: bool
    atr_stop_enabled: bool
    atr_take_profit_enabled: bool
    atr_trailing_enabled: bool
    partial_take_profit_enabled: bool
    stop_atr_multiple: Decimal
    take_profit_atr_multiple: Decimal
    trailing_atr_multiple: Decimal
    atr_take_profit_mode: str
    risk_multiplier: Decimal
    reason: str

    def to_metadata(self) -> dict[str, Any]:
        data = asdict(self)
        return {
            **data,
            "atr_policy": data,
            "atr_policy_reason": self.reason,
        }


class ATRPolicyRouter:
    def select(
        self,
        *,
        direction: SignalDirection,
        entry_price: Decimal,
        atr: Decimal | None,
        metadata: dict[str, Any],
    ) -> ATRPolicy:
        natr_pct = _natr_pct(entry_price=entry_price, atr=atr)
        volume_ratio = _decimal_from_nested(metadata, ("visual", "volume_ratio"), Decimal("1"))
        final_score = _decimal_from_metadata(metadata.get("final_score"), Decimal("0"))
        ema_score = _decimal_from_metadata(metadata.get("ema_score"), Decimal("0"))
        bb_score = _decimal_from_metadata(metadata.get("bb_score"), Decimal("0"))
        visual_score = _decimal_from_metadata(metadata.get("visual_score"), Decimal("0"))
        oi_score = _decimal_from_metadata(metadata.get("open_interest_score"), Decimal("0"))
        agreement_ratio = _agreement_ratio(
            direction=direction,
            scores=(ema_score, bb_score, visual_score, oi_score),
        )

        low_volatility = natr_pct is not None and natr_pct < Decimal("0.03")
        compression_or_breakout = (
            abs(final_score) >= Decimal("0.50")
            or abs(bb_score) >= Decimal("0.45")
            or agreement_ratio >= Decimal("0.65")
        )
        if low_volatility and volume_ratio < Decimal("0.8") and not compression_or_breakout:
            return ATRPolicy(
                trade_mode="low_volatility",
                atr_profile="block_low_volatility",
                entry_allowed=False,
                atr_stop_enabled=False,
                atr_take_profit_enabled=False,
                atr_trailing_enabled=False,
                partial_take_profit_enabled=False,
                stop_atr_multiple=Decimal("0"),
                take_profit_atr_multiple=Decimal("0"),
                trailing_atr_multiple=Decimal("0"),
                atr_take_profit_mode="none",
                risk_multiplier=Decimal("0"),
                reason="ATR/NATR is too low and volume is weak; expected move may not beat costs.",
            )
        if low_volatility and volume_ratio < Decimal("0.8"):
            return ATRPolicy(
                trade_mode="low_volatility_compression",
                atr_profile="low_vol_compression_b",
                entry_allowed=True,
                atr_stop_enabled=True,
                atr_take_profit_enabled=True,
                atr_trailing_enabled=False,
                partial_take_profit_enabled=False,
                stop_atr_multiple=Decimal("1.4"),
                take_profit_atr_multiple=Decimal("1.8"),
                trailing_atr_multiple=Decimal("0"),
                atr_take_profit_mode="entry_atr",
                risk_multiplier=Decimal("0.5"),
                reason="Low volatility but score/compression is strong enough; allow reduced-risk B-style setup.",
            )

        if natr_pct is not None and natr_pct > Decimal("8"):
            return ATRPolicy(
                trade_mode="high_volatility",
                atr_profile="high_vol_defensive_runner",
                entry_allowed=True,
                atr_stop_enabled=True,
                atr_take_profit_enabled=False,
                atr_trailing_enabled=True,
                partial_take_profit_enabled=False,
                stop_atr_multiple=Decimal("2.8"),
                take_profit_atr_multiple=Decimal("0"),
                trailing_atr_multiple=Decimal("2.8"),
                atr_take_profit_mode="none",
                risk_multiplier=Decimal("0.5"),
                reason="Volatility is overheated; use reduced risk, wider ATR stop, and no full ATR TP.",
            )

        if (
            abs(final_score) >= Decimal("0.55")
            and volume_ratio >= Decimal("1.30")
            and agreement_ratio >= Decimal("0.65")
            and _aligned(visual_score, direction, Decimal("0.20"))
        ):
            return ATRPolicy(
                trade_mode="breakout",
                atr_profile="breakout_runner",
                entry_allowed=True,
                atr_stop_enabled=True,
                atr_take_profit_enabled=False,
                atr_trailing_enabled=True,
                partial_take_profit_enabled=False,
                stop_atr_multiple=Decimal("2.2"),
                take_profit_atr_multiple=Decimal("0"),
                trailing_atr_multiple=Decimal("2.2"),
                atr_take_profit_mode="none",
                risk_multiplier=Decimal("1"),
                reason="Breakout-style score with volume and component agreement; let the winner run.",
            )

        if (
            _aligned(ema_score, direction, Decimal("0.35"))
            and _aligned(visual_score, direction, Decimal("0.25"))
            and agreement_ratio >= Decimal("0.65")
        ):
            return ATRPolicy(
                trade_mode="trend_continuation",
                atr_profile="trend_runner",
                entry_allowed=True,
                atr_stop_enabled=True,
                atr_take_profit_enabled=False,
                atr_trailing_enabled=True,
                partial_take_profit_enabled=False,
                stop_atr_multiple=Decimal("2.0"),
                take_profit_atr_multiple=Decimal("0"),
                trailing_atr_multiple=Decimal("2.0"),
                atr_take_profit_mode="none",
                risk_multiplier=Decimal("1"),
                reason="EMA and visual structure agree; use ATR stop/trailing without a full ATR TP cap.",
            )

        if (
            _aligned(bb_score, direction, Decimal("0.35"))
            and abs(ema_score) < Decimal("0.30")
            and volume_ratio <= Decimal("1.25")
        ):
            return ATRPolicy(
                trade_mode="mean_reversion",
                atr_profile="mean_reversion_target",
                entry_allowed=True,
                atr_stop_enabled=True,
                atr_take_profit_enabled=True,
                atr_trailing_enabled=False,
                partial_take_profit_enabled=False,
                stop_atr_multiple=Decimal("1.5"),
                take_profit_atr_multiple=Decimal("2.0"),
                trailing_atr_multiple=Decimal("0"),
                atr_take_profit_mode="entry_atr",
                risk_multiplier=Decimal("0.75"),
                reason="Bollinger rejection dominates while trend is weak; use a defined ATR target.",
            )

        return ATRPolicy(
            trade_mode="balanced",
            atr_profile="balanced_runner",
            entry_allowed=True,
            atr_stop_enabled=True,
            atr_take_profit_enabled=False,
            atr_trailing_enabled=True,
            partial_take_profit_enabled=False,
            stop_atr_multiple=Decimal("2.0"),
            take_profit_atr_multiple=Decimal("0"),
            trailing_atr_multiple=Decimal("2.0"),
            atr_take_profit_mode="none",
            risk_multiplier=Decimal("1"),
            reason="Mixed setup; use protective ATR stop/trailing and avoid full ATR take-profit.",
        )


def _aligned(score: Decimal, direction: SignalDirection, threshold: Decimal) -> bool:
    if direction == SignalDirection.LONG:
        return score >= threshold
    return score <= -threshold


def _agreement_ratio(
    *,
    direction: SignalDirection,
    scores: tuple[Decimal, ...],
) -> Decimal:
    directional_scores = [score for score in scores if score != 0]
    if not directional_scores:
        return Decimal("0")
    if direction == SignalDirection.LONG:
        agreed = sum(1 for score in directional_scores if score > 0)
    else:
        agreed = sum(1 for score in directional_scores if score < 0)
    return Decimal(agreed) / Decimal(len(directional_scores))


def _natr_pct(*, entry_price: Decimal, atr: Decimal | None) -> Decimal | None:
    if atr is None or atr <= 0 or entry_price <= 0:
        return None
    return (atr / entry_price) * Decimal("100")


def _decimal_from_nested(
    metadata: dict[str, Any],
    path: tuple[str, ...],
    default: Decimal,
) -> Decimal:
    current: Any = metadata
    for key in path:
        if not isinstance(current, dict):
            return default
        current = current.get(key)
    return _decimal_from_metadata(current, default)


def _decimal_from_metadata(value: Any, default: Decimal) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if value is None:
        return default
    try:
        return Decimal(str(value))
    except Exception:
        return default
