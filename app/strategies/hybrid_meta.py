from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from app.data.indicators import (
    average_true_range,
    bollinger_bands,
    exponential_moving_average,
    relative_strength_index,
    simple_moving_average,
)
from app.strategies.base import (
    SignalAction,
    SignalDirection,
    SignalFunnelReason,
    Strategy,
    StrategyContext,
    StrategySignal,
    clamp_confidence,
)
from app.strategies.atr_policy import ATRPolicyRouter


_BB_CONFLICT_DAMPING = Decimal("0.15")


@dataclass(frozen=True)
class _QualityDecision:
    allowed: bool
    tier: str
    risk_multiplier: Decimal
    reason: str
    funnel_reason: SignalFunnelReason | None = None


@dataclass(frozen=True)
class HybridMetaStrategy(Strategy):
    name: str = "hybrid_meta"
    fast_period: int = 9
    slow_period: int = 21
    rsi_period: int = 14
    bollinger_period: int = 20
    atr_period: int = 14
    volume_period: int = 20
    visual_lookback: int = 8
    ema_weight: Decimal = Decimal("0.45")
    bb_weight: Decimal = Decimal("0.25")
    visual_weight: Decimal = Decimal("0.20")
    oi_weight: Decimal = Decimal("0.10")
    entry_threshold: Decimal = Decimal("0.40")
    exit_threshold: Decimal = Decimal("0.42")
    min_volume_ratio: Decimal = Decimal("0.60")
    max_spike_atr_multiple: Decimal = Decimal("3.5")
    stop_atr_multiple: Decimal = Decimal("1.35")
    take_profit_atr_multiple: Decimal = Decimal("2.4")
    atr_trailing_multiple: Decimal = Decimal("2.0")
    fallback_stop_pct: Decimal = Decimal("0.012")
    fallback_take_profit_pct: Decimal = Decimal("0.024")
    allow_short_without_open_interest: bool = True

    def evaluate(self, context: StrategyContext) -> StrategySignal:
        latest = context.latest_candle
        if latest is None:
            return self._hold(context, "No candles available.")

        required = max(
            self.slow_period + 1,
            self.rsi_period + 1,
            self.bollinger_period,
            self.atr_period,
            self.volume_period,
            self.visual_lookback,
        )
        if len(context.candles) < required:
            return self._hold(context, f"Need at least {required} candles.")

        closes = context.candles.closes()
        volumes = context.candles.volumes()
        fast = exponential_moving_average(closes, self.fast_period)
        slow = exponential_moving_average(closes, self.slow_period)
        rsi_values = relative_strength_index(closes, self.rsi_period)
        bands = bollinger_bands(closes, period=self.bollinger_period)
        atr_values = average_true_range(context.candles, self.atr_period)
        volume_sma = simple_moving_average(volumes, self.volume_period)

        ema_score = self._ema_rsi_score(
            latest_close=latest.close,
            fast_current=fast[-1],
            slow_current=slow[-1],
            rsi_current=rsi_values[-1],
            atr=atr_values[-1],
        )
        bb_score = self._bb_reversion_score(latest.close, bands[-1])
        visual = self._visual_score(
            context=context,
            atr=atr_values[-1],
            average_volume=volume_sma[-1],
        )
        oi_score, oi_metadata = self._open_interest_score(context.features)
        
        # Instrument-agnostic regime detection:
        # Strong trend: spread >= 0.5x ATR
        # Weak regime: spread <= 0.15x ATR
        spread = abs(fast[-1] - slow[-1]) if (fast[-1] is not None and slow[-1] is not None) else Decimal("0")
        atr = atr_values[-1]
        is_strong_trend = (atr is not None and atr > 0 and spread >= atr * Decimal("0.5"))
        is_weak_regime = (atr is not None and atr > 0 and spread <= atr * Decimal("0.15"))
        
        final_score, active_weight = self._combined_score(
            ema_score=ema_score,
            bb_score=bb_score,
            visual_score=visual["score"],
            oi_score=oi_score,
            oi_active=oi_metadata["score_used"],
            is_strong_trend=is_strong_trend,
            is_weak_regime=is_weak_regime,
        )
        quality = _quality_settings(context.features, self.entry_threshold)
        if quality["mode"] == "strict":
            quality = {
                **quality,
                "long_entry_threshold": self.entry_threshold,
                "short_entry_threshold": self.entry_threshold,
            }
        long_agreement = _agreement_ratio(
            direction=SignalDirection.LONG,
            scores=(ema_score, bb_score, visual["score"], oi_score),
        )
        short_agreement = _agreement_ratio(
            direction=SignalDirection.SHORT,
            scores=(ema_score, bb_score, visual["score"], oi_score),
        )
        raw_direction = _raw_candidate_direction(final_score)
        # Diagnostic: Detect if BB score is fighting EMA direction
        ema_opposed_direction = False
        if ema_score > 0 and bb_score < 0:
            ema_opposed_direction = True
        elif ema_score < 0 and bb_score > 0:
            ema_opposed_direction = True

        metadata = {
            "ema_score": ema_score,
            "bb_score": bb_score,
            "visual_score": visual["score"],
            "open_interest_score": oi_score,
            "open_interest": oi_metadata,
            "open_position": self._open_position_metadata(context.features, context.pair),
            "final_score": final_score,
            "active_weight": active_weight,
            "ema_opposed_direction": ema_opposed_direction,
            "visual": visual,
            "ema_fast": fast[-1],
            "ema_slow": slow[-1],
            "rsi": rsi_values[-1],
            "bollinger": bands[-1],
            "atr": atr_values[-1],
            "average_volume": volume_sma[-1],
            "volume": latest.volume,
            "trade_quality_mode": quality["mode"],
            "quality_thresholds": {
                key: value
                for key, value in quality.items()
                if key not in {"mode", "controlled_shorts_enabled"}
            },
            "long_agreement_ratio": long_agreement,
            "short_agreement_ratio": short_agreement,
        }
        if raw_direction is not None:
            metadata["signal_funnel_raw_candidate"] = True
            metadata["signal_funnel_raw_direction"] = raw_direction.value

        open_direction = self._open_position_direction(context.features, context.pair)
        if open_direction == SignalDirection.LONG:
            if final_score <= -self.exit_threshold:
                return StrategySignal(
                    strategy_name=self.name,
                    pair=context.pair,
                    interval=context.interval,
                    action=SignalAction.EXIT_LONG,
                    confidence=clamp_confidence(abs(final_score)),
                    reason="Hybrid score flipped bearish; long invalidated.",
                    timestamp_ms=latest.close_time_ms,
                    direction=SignalDirection.LONG,
                    entry_price=latest.close,
                    metadata=metadata,
                )
            return self._hold(
                context,
                "Already long; hybrid score has not invalidated the position.",
                {**metadata, "funnel_reason": SignalFunnelReason.EXISTING_POSITION_BLOCKED},
            )

        if open_direction == SignalDirection.SHORT:
            if final_score >= self.exit_threshold:
                return StrategySignal(
                    strategy_name=self.name,
                    pair=context.pair,
                    interval=context.interval,
                    action=SignalAction.EXIT_SHORT,
                    confidence=clamp_confidence(abs(final_score)),
                    reason="Hybrid score flipped bullish; short invalidated.",
                    timestamp_ms=latest.close_time_ms,
                    direction=SignalDirection.SHORT,
                    entry_price=latest.close,
                    metadata=metadata,
                )
            return self._hold(
                context,
                "Already short; hybrid score has not invalidated the position.",
                {**metadata, "funnel_reason": SignalFunnelReason.EXISTING_POSITION_BLOCKED},
            )

        if visual["blocked"]:
            return self._hold(context, f"Visual screen blocked: {visual['reason']}", {**metadata, "funnel_reason": SignalFunnelReason.VISUAL_SCREEN_BLOCKED})

        if quality["mode"] == "tiered":
            if final_score >= quality["long_entry_threshold"]:
                decision = _quality_decision(
                    direction=SignalDirection.LONG,
                    final_score=final_score,
                    agreement_ratio=long_agreement,
                    visual_score=visual["score"],
                    quality=quality,
                )
                if not decision.allowed:
                    return self._hold(context, decision.reason, {**metadata, **_quality_metadata(decision, long_agreement)})
                return self._entry_signal(
                    context=context,
                    direction=SignalDirection.LONG,
                    final_score=final_score,
                    atr=atr_values[-1],
                    reason=f"Hybrid {decision.tier}-tier long setup confirmed.",
                    metadata={**metadata, **_quality_metadata(decision, long_agreement)},
                )

            if final_score <= -quality["short_entry_threshold"]:
                short_block, short_funnel_reason = _short_block_reason(
                    final_score=final_score,
                    agreement_ratio=short_agreement,
                    oi_metadata=oi_metadata,
                    oi_score=oi_score,
                    quality=quality,
                    allow_short_without_open_interest=self.allow_short_without_open_interest,
                )
                if short_block is not None:
                    return self._hold(context, short_block, {**metadata, "funnel_reason": short_funnel_reason.value if short_funnel_reason else None})
                decision = _quality_decision(
                    direction=SignalDirection.SHORT,
                    final_score=final_score,
                    agreement_ratio=short_agreement,
                    visual_score=visual["score"],
                    quality=quality,
                )
                if not decision.allowed:
                    return self._hold(context, decision.reason, {**metadata, **_quality_metadata(decision, short_agreement)})
                return self._entry_signal(
                    context=context,
                    direction=SignalDirection.SHORT,
                    final_score=final_score,
                    atr=atr_values[-1],
                    reason=f"Hybrid {decision.tier}-tier short setup confirmed.",
                    metadata={**metadata, **_quality_metadata(decision, short_agreement)},
                )

            return self._hold(context, "Hybrid score below tiered entry threshold.", {**metadata, "funnel_reason": SignalFunnelReason.BELOW_ENTRY_THRESHOLD})

        long_threshold = (
            quality["long_entry_threshold"]
            if quality["mode"] == "loose"
            else self.entry_threshold
        )
        short_threshold = (
            quality["short_entry_threshold"]
            if quality["mode"] == "loose"
            else self.entry_threshold
        )

        if final_score >= long_threshold:
            return self._entry_signal(
                context=context,
                direction=SignalDirection.LONG,
                final_score=final_score,
                atr=atr_values[-1],
                reason="Hybrid score confirmed long setup.",
                metadata={
                    **metadata,
                    "setup_tier": "strict",
                    "agreement_ratio": long_agreement,
                },
            )

        if final_score <= -short_threshold:
            short_block, short_funnel_reason = _short_block_reason(
                final_score=final_score,
                agreement_ratio=short_agreement,
                oi_metadata=oi_metadata,
                oi_score=oi_score,
                quality=quality,
                allow_short_without_open_interest=self.allow_short_without_open_interest,
            )
            if short_block is not None:
                return self._hold(
                    context,
                    short_block,
                    {**metadata, "funnel_reason": short_funnel_reason.value if short_funnel_reason else None},
                )
            return self._entry_signal(
                context=context,
                direction=SignalDirection.SHORT,
                final_score=final_score,
                atr=atr_values[-1],
                reason="Hybrid score confirmed short setup.",
                metadata={
                    **metadata,
                    "setup_tier": "strict",
                    "agreement_ratio": short_agreement,
                },
            )

        return self._hold(context, "Hybrid score below entry threshold.", {**metadata, "funnel_reason": SignalFunnelReason.BELOW_ENTRY_THRESHOLD})

    def _ema_rsi_score(
        self,
        *,
        latest_close: Decimal,
        fast_current: Decimal | None,
        slow_current: Decimal | None,
        rsi_current: Decimal | None,
        atr: Decimal | None = None,
    ) -> Decimal:
        if fast_current is None or slow_current is None or rsi_current is None:
            return Decimal("0")
        if latest_close <= 0:
            return Decimal("0")

        spread = fast_current - slow_current
        
        # Instrument-agnostic spread score:
        # If ATR is available, use it to normalize the spread.
        # A 1.0 spread score is achieved when spread reaches 0.4x ATR.
        if atr is not None and atr > 0:
            spread_score = _clamp(spread / (atr * Decimal("0.4")), Decimal("-1"), Decimal("1"))
        else:
            # Fallback to legacy static 0.75% threshold
            spread_pct = (spread / latest_close) * Decimal("100")
            spread_score = _clamp(spread_pct / Decimal("0.75"), Decimal("-1"), Decimal("1"))
            
        rsi_score = _clamp((rsi_current - Decimal("50")) / Decimal("22"), Decimal("-1"), Decimal("1"))
        return _clamp((spread_score * Decimal("0.65")) + (rsi_score * Decimal("0.35")), Decimal("-1"), Decimal("1"))

    def _bb_reversion_score(self, close: Decimal, band: Any) -> Decimal:
        if band is None or close <= 0:
            return Decimal("0")
        if band.upper == band.lower:
            return Decimal("0")
        if close <= band.middle:
            distance = (band.middle - close) / max(band.middle - band.lower, Decimal("0.00000001"))
            return _clamp(distance, Decimal("0"), Decimal("1"))
        distance = (close - band.middle) / max(band.upper - band.middle, Decimal("0.00000001"))
        return -_clamp(distance, Decimal("0"), Decimal("1"))

    def _visual_score(
        self,
        *,
        context: StrategyContext,
        atr: Decimal | None,
        average_volume: Decimal | None,
    ) -> dict[str, Any]:
        candles = list(context.candles)
        latest = candles[-1]
        if latest.close <= 0:
            return {"score": Decimal("0"), "blocked": True, "reason": "latest close is not positive"}

        volume_ratio = (
            latest.volume / average_volume
            if average_volume is not None and average_volume > 0
            else Decimal("1")
        )
        
        hard_block_ratio = Decimal("0.35")
        if volume_ratio < hard_block_ratio:
            return {
                "score": Decimal("0"),
                "blocked": True,
                "reason": f"critically low volume ratio: {volume_ratio}",
                "volume_ratio": volume_ratio,
            }

        volume_scale = Decimal("1")
        if volume_ratio < self.min_volume_ratio:
            volume_scale = (volume_ratio - hard_block_ratio) / (self.min_volume_ratio - hard_block_ratio)
            volume_scale = _clamp(volume_scale, Decimal("0"), Decimal("1"))

        candle_range = latest.high - latest.low
        if atr is not None and atr > 0 and candle_range > atr * self.max_spike_atr_multiple:
            return {
                "score": Decimal("0"),
                "blocked": True,
                "reason": "latest candle range is too large versus ATR",
                "volume_ratio": volume_ratio,
            }

        lookback = candles[-self.visual_lookback :]
        start = lookback[0].close
        if start <= 0:
            momentum_score = Decimal("0")
        else:
            momentum_pct = ((latest.close - start) / start) * Decimal("100")
            momentum_score = _clamp(momentum_pct / Decimal("3"), Decimal("-1"), Decimal("1"))

        higher_highs = sum(1 for index in range(1, len(lookback)) if lookback[index].high > lookback[index - 1].high)
        lower_lows = sum(1 for index in range(1, len(lookback)) if lookback[index].low < lookback[index - 1].low)
        structure_score = Decimal(higher_highs - lower_lows) / Decimal(max(len(lookback) - 1, 1))
        
        raw_visual_score = (momentum_score * Decimal("0.7")) + (structure_score * Decimal("0.3"))
        score = _clamp(raw_visual_score * volume_scale, Decimal("-1"), Decimal("1"))

        return {
            "score": score,
            "blocked": False,
            "reason": "visual structure accepted",
            "volume_ratio": volume_ratio,
            "volume_scale": volume_scale,
            "momentum_score": momentum_score,
            "structure_score": structure_score,
        }

    def _open_interest_score(self, features: dict[str, Any]) -> tuple[Decimal, dict[str, Any]]:
        raw = features.get("open_interest")
        if not isinstance(raw, dict):
            return Decimal("0"), {"source": "not_configured", "score_used": False}
        value = raw.get("score")
        if value is None:
            return Decimal("0"), {"source": raw.get("source", "unknown"), "score_used": False}
        score = _clamp(Decimal(str(value)), Decimal("-1"), Decimal("1"))
        return score, {
            "source": raw.get("source", "unknown"),
            "score_used": True,
            "change_pct": raw.get("change_pct"),
            "score": score,
        }

    def _open_position_direction(
        self,
        features: dict[str, Any],
        pair: str,
    ) -> SignalDirection | None:
        metadata = self._open_position_metadata(features, pair)
        if metadata is None:
            return None
        direction = metadata.get("direction")
        if direction == SignalDirection.LONG.value:
            return SignalDirection.LONG
        if direction == SignalDirection.SHORT.value:
            return SignalDirection.SHORT
        return None

    def _open_position_metadata(
        self,
        features: dict[str, Any],
        pair: str,
    ) -> dict[str, Any] | None:
        raw = features.get("open_position")
        if not isinstance(raw, dict):
            return None
        raw_pair = raw.get("pair")
        if raw_pair is not None and raw_pair != pair:
            return None

        direction = raw.get("direction")
        if isinstance(direction, SignalDirection):
            direction_value = direction.value
        elif isinstance(direction, str):
            direction_value = direction.strip().lower()
        else:
            return None
        if direction_value not in {SignalDirection.LONG.value, SignalDirection.SHORT.value}:
            return None

        return {
            "pair": pair,
            "direction": direction_value,
            "entry_price": raw.get("entry_price"),
            "quantity": raw.get("quantity"),
            "opened_at_ms": raw.get("opened_at_ms"),
            "strategy_name": raw.get("strategy_name"),
        }

    def _combined_score(
        self,
        *,
        ema_score: Decimal,
        bb_score: Decimal,
        visual_score: Decimal,
        oi_score: Decimal,
        oi_active: bool,
        is_strong_trend: bool = False,
        is_weak_regime: bool = False,
    ) -> tuple[Decimal, Decimal]:
        effective_bb_weight = self.bb_weight
        effective_ema_weight = self.ema_weight
        
        # Conflict damping: if BB opposes EMA, reduce BB impact
        # Only applies if both signals are meaningful (abs > 0.10)
        if (
            ((ema_score > 0 and bb_score < 0) or (ema_score < 0 and bb_score > 0))
            and abs(ema_score) > Decimal("0.10")
            and abs(bb_score) > Decimal("0.10")
        ):
            damping = _BB_CONFLICT_DAMPING
            if is_strong_trend:
                # Reduce BB influence further in a strong trend
                damping *= Decimal("0.5")
            effective_bb_weight *= damping
            
        if is_weak_regime:
            # Reduce trend following confidence in weak/choppy regime
            effective_ema_weight *= Decimal("0.7")

        weighted = (
            (ema_score * effective_ema_weight)
            + (bb_score * effective_bb_weight)
            + (visual_score * self.visual_weight)
        )
        active_weight = effective_ema_weight + effective_bb_weight + self.visual_weight
        if oi_active:
            weighted += oi_score * self.oi_weight
            active_weight += self.oi_weight
        if active_weight <= 0:
            return Decimal("0"), active_weight
        return _clamp(weighted / active_weight, Decimal("-1"), Decimal("1")), active_weight

    def _entry_signal(
        self,
        *,
        context: StrategyContext,
        direction: SignalDirection,
        final_score: Decimal,
        atr: Decimal | None,
        reason: str,
        metadata: dict[str, Any],
    ) -> StrategySignal:
        latest = context.latest_candle
        assert latest is not None
        policy = ATRPolicyRouter().select(
            direction=direction,
            entry_price=latest.close,
            atr=atr,
            metadata=metadata,
        )
        policy_metadata = policy.to_metadata()
        tier_risk_multiplier = _decimal_from_metadata(
            metadata.get("setup_tier_risk_multiplier"),
            Decimal("1"),
        )
        combined_risk_multiplier = min(
            tier_risk_multiplier,
            policy.risk_multiplier if policy.risk_multiplier > 0 else Decimal("1"),
        )
        metadata = {
            "stop_atr_multiple": self.stop_atr_multiple,
            "take_profit_atr_multiple": self.take_profit_atr_multiple,
            "atr_trailing_multiple": self.atr_trailing_multiple,
            "trailing_atr_multiple": self.atr_trailing_multiple,
            **metadata,
            **policy_metadata,
            "risk_multiplier": combined_risk_multiplier,
            "risk_multiplier_applies": bool(metadata.get("risk_multiplier_applies")),
        }
        stop_distance = (
            atr * self.stop_atr_multiple
            if atr is not None and atr > 0
            else latest.close * self.fallback_stop_pct
        )
        take_profit_distance = (
            atr * self.take_profit_atr_multiple
            if atr is not None and atr > 0
            else latest.close * self.fallback_take_profit_pct
        )
        if direction == SignalDirection.LONG:
            action = SignalAction.ENTER_LONG
            stop_loss = latest.close - stop_distance
            take_profit = latest.close + take_profit_distance
        else:
            action = SignalAction.ENTER_SHORT
            stop_loss = latest.close + stop_distance
            take_profit = latest.close - take_profit_distance

        return StrategySignal(
            strategy_name=self.name,
            pair=context.pair,
            interval=context.interval,
            action=action,
            direction=direction,
            confidence=clamp_confidence(abs(final_score)),
            reason=reason,
            timestamp_ms=latest.close_time_ms,
            entry_price=latest.close,
            stop_loss=max(stop_loss, Decimal("0")),
            take_profit=max(take_profit, Decimal("0")),
            metadata=metadata,
        )

    def _hold(
        self,
        context: StrategyContext,
        reason: str,
        metadata: dict[str, Any] | None = None,
    ) -> StrategySignal:
        latest = context.latest_candle
        return StrategySignal.hold(
            strategy_name=self.name,
            pair=context.pair,
            interval=context.interval,
            timestamp_ms=latest.close_time_ms if latest is not None else 0,
            reason=reason,
            metadata=metadata,
        )


def _clamp(value: Decimal, low: Decimal, high: Decimal) -> Decimal:
    return min(max(value, low), high)


def _quality_settings(
    features: dict[str, Any],
    default_entry_threshold: Decimal,
) -> dict[str, Any]:
    raw = features.get("backtest_config")
    config = raw if isinstance(raw, dict) else {}
    mode = str(config.get("trade_quality_mode") or "strict").strip().lower()
    if mode not in {"strict", "loose", "tiered"}:
        mode = "strict"
    return {
        "mode": mode,
        "controlled_shorts_enabled": _bool_value(
            config.get("controlled_shorts_enabled"),
            False,
        ),
        "a_setup_score_threshold": _decimal_from_metadata(
            config.get("a_setup_score_threshold"),
            Decimal("0.50"),
        ),
        "a_setup_agreement_threshold": _decimal_from_metadata(
            config.get("a_setup_agreement_threshold"),
            Decimal("0.65"),
        ),
        "b_setup_score_threshold": _decimal_from_metadata(
            config.get("b_setup_score_threshold"),
            default_entry_threshold,
        ),
        "b_setup_agreement_threshold": _decimal_from_metadata(
            config.get("b_setup_agreement_threshold"),
            Decimal("0.55"),
        ),
        "b_setup_risk_multiplier": _decimal_from_metadata(
            config.get("b_setup_risk_multiplier"),
            Decimal("0.50"),
        ),
        "minimum_visual_score": _decimal_from_metadata(
            config.get("minimum_visual_score"),
            Decimal("0"),
        ),
        "long_entry_threshold": _decimal_from_metadata(
            config.get("long_entry_threshold"),
            default_entry_threshold,
        ),
        "short_entry_threshold": _decimal_from_metadata(
            config.get("short_entry_threshold"),
            default_entry_threshold,
        ),
        "short_agreement_threshold": _decimal_from_metadata(
            config.get("short_agreement_threshold"),
            Decimal("0.60"),
        ),
    }


def _raw_candidate_direction(
    final_score: Decimal,
) -> SignalDirection | None:
    # A candidate is anything with a non-negligible directional bias (e.g. > 0.10)
    # even if it's below the actual entry thresholds (usually 0.40+).
    interest_threshold = Decimal("0.10")
    if final_score >= interest_threshold:
        return SignalDirection.LONG
    if final_score <= -interest_threshold:
        return SignalDirection.SHORT
    return None


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


def _quality_decision(
    *,
    direction: SignalDirection,
    final_score: Decimal,
    agreement_ratio: Decimal,
    visual_score: Decimal,
    quality: dict[str, Any],
) -> _QualityDecision:
    minimum_visual = quality["minimum_visual_score"]
    visual_ok = (
        visual_score >= minimum_visual
        if direction == SignalDirection.LONG
        else visual_score <= -minimum_visual
    )
    if not visual_ok:
        return _QualityDecision(
            allowed=False,
            tier="C",
            risk_multiplier=Decimal("0"),
            reason="Trade quality blocked C setup: visual score is below the configured minimum.",
            funnel_reason=SignalFunnelReason.MINIMUM_VISUAL_SCORE_FAILED,
        )

    score_abs = abs(final_score)
    if (
        score_abs >= quality["a_setup_score_threshold"]
        and agreement_ratio >= quality["a_setup_agreement_threshold"]
    ):
        return _QualityDecision(
            allowed=True,
            tier="A",
            risk_multiplier=Decimal("1"),
            reason="A setup: strong score and strong component agreement.",
        )

    if (
        score_abs >= quality["b_setup_score_threshold"]
        and agreement_ratio >= quality["b_setup_agreement_threshold"]
    ):
        risk_multiplier = min(
            max(quality["b_setup_risk_multiplier"], Decimal("0.01")),
            Decimal("1"),
        )
        return _QualityDecision(
            allowed=True,
            tier="B",
            risk_multiplier=risk_multiplier,
            reason="B setup: moderate score/agreement, reduced risk.",
        )

    funnel_reason = (
        SignalFunnelReason.BELOW_B_SETUP_THRESHOLD
        if score_abs < quality["b_setup_score_threshold"]
        else SignalFunnelReason.AGREEMENT_BELOW_MINIMUM
    )

    return _QualityDecision(
        allowed=False,
        tier="C",
        risk_multiplier=Decimal("0"),
        reason="Trade quality blocked C setup: score/agreement below B-tier minimum.",
        funnel_reason=funnel_reason,
    )


def _quality_metadata(
    decision: _QualityDecision,
    agreement_ratio: Decimal,
) -> dict[str, Any]:
    return {
        "setup_tier": decision.tier,
        "setup_tier_reason": decision.reason,
        "setup_tier_risk_multiplier": decision.risk_multiplier,
        "agreement_ratio": agreement_ratio,
        "risk_multiplier": decision.risk_multiplier,
        "risk_multiplier_applies": decision.allowed and decision.risk_multiplier < 1,
        "funnel_reason": decision.funnel_reason.value if decision.funnel_reason else None,
    }


def _short_block_reason(
    *,
    final_score: Decimal,
    agreement_ratio: Decimal,
    oi_metadata: dict[str, Any],
    oi_score: Decimal,
    quality: dict[str, Any],
    allow_short_without_open_interest: bool,
) -> tuple[str | None, SignalFunnelReason | None]:
    controlled_shorts = bool(quality["controlled_shorts_enabled"])
    if agreement_ratio < quality["short_agreement_threshold"]:
        return "Short setup blocked: component agreement is below the short threshold.", SignalFunnelReason.AGREEMENT_BELOW_MINIMUM
    if oi_metadata.get("score_used") and oi_score > 0:
        return "Short setup blocked: open interest contradicts the short.", SignalFunnelReason.OI_SHORT_RESTRICTION
    if oi_metadata.get("score_used"):
        return None, None
    if allow_short_without_open_interest:
        return None, None
    if not controlled_shorts:
        return "Short setup requires open-interest confirmation.", SignalFunnelReason.OI_SHORT_RESTRICTION
    if abs(final_score) < quality["short_entry_threshold"]:
        return "Short setup blocked: no-OI short needs stronger score confirmation.", SignalFunnelReason.SHORT_THRESHOLD_FAILED
    return None, None


def _bool_value(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _decimal_from_metadata(value: Any, default: Decimal) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if value is None or value == "":
        return default
    try:
        return Decimal(str(value))
    except Exception:
        return default


@dataclass(frozen=True)
class HybridMetaV2Strategy(HybridMetaStrategy):
    name: str = "hybrid_meta_v2"
    bb_weight: Decimal = Decimal("0.20")
    visual_weight: Decimal = Decimal("0.25")
    entry_threshold: Decimal = Decimal("0.45")
    exit_threshold: Decimal = Decimal("0.42")
    min_volume_ratio: Decimal = Decimal("0.60")
    max_spike_atr_multiple: Decimal = Decimal("3.0")
    stop_atr_multiple: Decimal = Decimal("1.5")
    take_profit_atr_multiple: Decimal = Decimal("2.6")
    allow_short_without_open_interest: bool = True
