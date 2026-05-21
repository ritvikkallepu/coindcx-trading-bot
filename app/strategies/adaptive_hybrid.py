from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from app.data.indicators import average_true_range, bollinger_bands, exponential_moving_average
from app.strategies.base import (
    SignalAction,
    SignalDirection,
    SignalFunnelReason,
    Strategy,
    StrategyContext,
    StrategySignal,
)
from app.strategies.bb_volume_reversion import BollingerVolumeMeanReversionStrategy
from app.strategies.ema_rsi_trend import EMARSICrossoverStrategy
from app.strategies.hybrid_meta import HybridMetaStrategy


ENTRY_ACTIONS = {SignalAction.ENTER_LONG, SignalAction.ENTER_SHORT}
EXIT_ACTIONS = {SignalAction.EXIT_LONG, SignalAction.EXIT_SHORT}


@dataclass(frozen=True)
class _MarketRegime:
    mode: str
    trend_score: Decimal
    range_score: Decimal
    reason: str
    trend_direction: SignalDirection | None = None
    ema_spread_pct: Decimal | None = None
    lookback_move_pct: Decimal | None = None
    bb_width_pct: Decimal | None = None
    bb_width_rank: Decimal | None = None
    band_proximity: Decimal | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "trend_score": self.trend_score,
            "range_score": self.range_score,
            "reason": self.reason,
            "trend_direction": self.trend_direction.value if self.trend_direction else None,
            "ema_spread_pct": self.ema_spread_pct,
            "lookback_move_pct": self.lookback_move_pct,
            "bb_width_pct": self.bb_width_pct,
            "bb_width_rank": self.bb_width_rank,
            "band_proximity": self.band_proximity,
        }


@dataclass(frozen=True)
class _StrategySelection:
    primary: Strategy
    secondary: Strategy
    reason: str


@dataclass(frozen=True)
class AdaptiveHybridStrategy(Strategy):
    name: str = "adaptive_hybrid"
    ema_strategy: EMARSICrossoverStrategy = field(default_factory=EMARSICrossoverStrategy)
    bb_strategy: BollingerVolumeMeanReversionStrategy = field(
        default_factory=BollingerVolumeMeanReversionStrategy
    )
    visual_filter: HybridMetaStrategy = field(
        default_factory=lambda: HybridMetaStrategy(
            name="adaptive_visual_filter",
            min_volume_ratio=Decimal("0.65"),
            max_spike_atr_multiple=Decimal("3.5"),
        )
    )
    require_oi_for_shorts_when_available: bool = True
    bearish_oi_threshold: Decimal = Decimal("-0.15")
    exit_on_opposite_entry: bool = False
    regime_lookback: int = 24
    trend_regime_threshold: Decimal = Decimal("0.55")
    range_regime_threshold: Decimal = Decimal("0.55")
    trend_spread_reference_pct: Decimal = Decimal("0.65")
    trend_move_reference_pct: Decimal = Decimal("3")
    strong_regime_margin: Decimal = Decimal("0.15")

    def evaluate(self, context: StrategyContext) -> StrategySignal:
        ema_signal = self.ema_strategy.evaluate(context)
        bb_signal = self.bb_strategy.evaluate(context)
        market_regime = self._market_regime(context)
        selection = self._select_primary_strategy(
            context=context,
            ema_signal=ema_signal,
            bb_signal=bb_signal,
            market_regime=market_regime,
        )
        primary = selection.primary
        secondary = selection.secondary
        primary_signal = ema_signal if primary is self.ema_strategy else bb_signal
        secondary_signal = bb_signal if primary is self.ema_strategy else ema_signal
        latest = context.latest_candle

        metadata = {
            "mode": "trend" if primary is self.ema_strategy else "mean_reversion",
            "market_regime": market_regime.to_dict(),
            "selection_reason": selection.reason,
            "primary_strategy": primary.name,
            "primary_signal": primary_signal.to_dict(),
            "secondary_strategy": secondary.name,
            "secondary_signal": secondary_signal.to_dict(),
        }
        open_direction = _open_position_direction(context.features, context.pair)

        if primary_signal.action in EXIT_ACTIONS:
            if not _exit_matches_position(primary_signal.action, open_direction):
                return StrategySignal.hold(
                    strategy_name=self.name,
                    pair=context.pair,
                    interval=context.interval,
                    timestamp_ms=latest.close_time_ms if latest is not None else 0,
                    reason="Adaptive hybrid ignored exit because no matching position is open.",
                    metadata=metadata,
                )
            return self._clone_signal(
                primary_signal,
                action=primary_signal.action,
                reason=f"Adaptive hybrid exit: {primary_signal.reason}",
                metadata=metadata,
            )

        if primary_signal.action not in ENTRY_ACTIONS:
            return StrategySignal.hold(
                strategy_name=self.name,
                pair=context.pair,
                interval=context.interval,
                timestamp_ms=latest.close_time_ms if latest is not None else 0,
                reason=f"Adaptive hybrid waiting: {primary_signal.reason}",
                funnel_reason=primary_signal.funnel_reason or SignalFunnelReason.BELOW_ENTRY_THRESHOLD,
                metadata=metadata,
            )

        if open_direction is not None:
            exit_signal = self._exit_for_open_position(
                context=context,
                open_direction=open_direction,
                primary_signal=primary_signal,
                metadata=metadata,
            )
            if exit_signal is not None:
                return exit_signal
            return StrategySignal.hold(
                strategy_name=self.name,
                pair=context.pair,
                interval=context.interval,
                timestamp_ms=latest.close_time_ms if latest is not None else 0,
                reason="Adaptive hybrid already has an open position.",
                metadata=metadata,
            )

        visual_signal = self.visual_filter.evaluate(context)
        visual_metadata = visual_signal.metadata.get("visual", {})
        metadata["visual"] = visual_metadata
        if isinstance(visual_metadata, dict) and visual_metadata.get("blocked"):
            return StrategySignal.hold(
                strategy_name=self.name,
                pair=context.pair,
                interval=context.interval,
                timestamp_ms=latest.close_time_ms if latest is not None else 0,
                reason=f"Adaptive hybrid visual filter blocked entry: {visual_metadata.get('reason')}",
                funnel_reason=SignalFunnelReason.VISUAL_SCREEN_BLOCKED,
                metadata=metadata,
            )

        oi_score, oi_metadata = self.visual_filter._open_interest_score(context.features)
        metadata["open_interest"] = oi_metadata
        if (
            self.require_oi_for_shorts_when_available
            and primary_signal.action == SignalAction.ENTER_SHORT
            and oi_metadata["score_used"]
            and oi_score > self.bearish_oi_threshold
        ):
            return StrategySignal.hold(
                strategy_name=self.name,
                pair=context.pair,
                interval=context.interval,
                timestamp_ms=latest.close_time_ms if latest is not None else 0,
                reason="Adaptive hybrid blocked short: open interest is not bearish enough.",
                funnel_reason=SignalFunnelReason.OI_SHORT_RESTRICTION,
                metadata=metadata,
            )

        conflict = _opposite_entry(primary_signal.action, secondary_signal.action)
        if conflict:
            return StrategySignal.hold(
                strategy_name=self.name,
                pair=context.pair,
                interval=context.interval,
                timestamp_ms=latest.close_time_ms if latest is not None else 0,
                reason="Adaptive hybrid blocked entry: secondary strategy disagrees.",
                funnel_reason=SignalFunnelReason.AGREEMENT_BELOW_MINIMUM,
                metadata=metadata,
            )

        return self._clone_signal(
            primary_signal,
            action=primary_signal.action,
            reason=f"Adaptive hybrid entry via {primary.name}: {primary_signal.reason}",
            metadata=metadata,
        )

    def _select_primary_strategy(
        self,
        *,
        context: StrategyContext,
        ema_signal: StrategySignal,
        bb_signal: StrategySignal,
        market_regime: _MarketRegime,
    ) -> _StrategySelection:
        open_source = _open_position_source_strategy(context.features, context.pair)
        if open_source == self.bb_strategy.name:
            return _StrategySelection(
                primary=self.bb_strategy,
                secondary=self.ema_strategy,
                reason="Existing open position was entered through Bollinger reversion.",
            )
        if open_source == self.ema_strategy.name:
            return _StrategySelection(
                primary=self.ema_strategy,
                secondary=self.bb_strategy,
                reason="Existing open position was entered through EMA-RSI trend.",
            )

        ema_entry = ema_signal.action in ENTRY_ACTIONS
        bb_entry = bb_signal.action in ENTRY_ACTIONS

        if ema_entry and not bb_entry:
            return _StrategySelection(
                primary=self.ema_strategy,
                secondary=self.bb_strategy,
                reason="EMA-RSI is the only actionable entry setup.",
            )

        if bb_entry and not ema_entry:
            if self._bb_entry_fights_strong_trend(bb_signal, market_regime):
                return _StrategySelection(
                    primary=self.ema_strategy,
                    secondary=self.bb_strategy,
                    reason="Bollinger entry is blocked because it fights the detected trend.",
                )
            return _StrategySelection(
                primary=self.bb_strategy,
                secondary=self.ema_strategy,
                reason="Bollinger reversion is the only actionable entry setup.",
            )

        if ema_entry and bb_entry:
            if market_regime.mode == "mean_reversion":
                return _StrategySelection(
                    primary=self.bb_strategy,
                    secondary=self.ema_strategy,
                    reason="Both setups fired; market regime favors mean reversion.",
                )
            if market_regime.mode == "trend":
                return _StrategySelection(
                    primary=self.ema_strategy,
                    secondary=self.bb_strategy,
                    reason="Both setups fired; market regime favors trend following.",
                )
            if bb_signal.confidence > ema_signal.confidence:
                return _StrategySelection(
                    primary=self.bb_strategy,
                    secondary=self.ema_strategy,
                    reason="Both setups fired; Bollinger confidence is higher in a neutral regime.",
                )
            return _StrategySelection(
                primary=self.ema_strategy,
                secondary=self.bb_strategy,
                reason="Both setups fired; EMA-RSI confidence is higher in a neutral regime.",
            )

        if market_regime.mode == "mean_reversion":
            return _StrategySelection(
                primary=self.bb_strategy,
                secondary=self.ema_strategy,
                reason="No entry fired; market regime currently favors mean reversion.",
            )
        return _StrategySelection(
            primary=self.ema_strategy,
            secondary=self.bb_strategy,
            reason="No entry fired; market regime favors trend or is neutral.",
        )

    def _market_regime(self, context: StrategyContext) -> _MarketRegime:
        latest = context.latest_candle
        if latest is None:
            return _MarketRegime(
                mode="neutral",
                trend_score=Decimal("0"),
                range_score=Decimal("0"),
                reason="No candles available for regime detection.",
            )

        closes = context.candles.closes()
        if len(closes) < 3 or latest.close <= 0:
            return _MarketRegime(
                mode="neutral",
                trend_score=Decimal("0"),
                range_score=Decimal("0"),
                reason="Not enough valid candles for regime detection.",
            )

        fast = exponential_moving_average(closes, self.ema_strategy.fast_period)
        slow = exponential_moving_average(closes, self.ema_strategy.slow_period)
        ema_spread_pct = Decimal("0")
        ema_spread_signed = Decimal("0")
        if fast[-1] is not None and slow[-1] is not None:
            ema_spread_signed = ((fast[-1] - slow[-1]) / latest.close) * Decimal("100")
            ema_spread_pct = abs(ema_spread_signed)

        lookback_size = min(max(self.regime_lookback, 1), len(closes) - 1)
        lookback_start = closes[-(lookback_size + 1)]
        lookback_move_pct = Decimal("0")
        if lookback_start > 0:
            lookback_move_pct = ((latest.close - lookback_start) / lookback_start) * Decimal("100")

        # Instrument-agnostic normalization:
        # Instead of static percentage thresholds, use ATR-derived ones.
        atr_values = average_true_range(context.candles, self.ema_strategy.atr_period)
        atr = atr_values[-1]
        
        effective_spread_ref = self.trend_spread_reference_pct
        effective_move_ref = self.trend_move_reference_pct
        
        if atr is not None and atr > 0 and latest.close > 0:
            natr_pct = (atr / latest.close) * Decimal("100")
            # A healthy trend spread is roughly 0.4x ATR.
            # A healthy lookback move is roughly 1.5x ATR.
            effective_spread_ref = natr_pct * Decimal("0.4")
            effective_move_ref = natr_pct * Decimal("1.5")

        trend_score = _clamp(
            (
                _ratio(ema_spread_pct, effective_spread_ref) * Decimal("0.65")
            )
            + (
                _ratio(abs(lookback_move_pct), effective_move_ref)
                * Decimal("0.35")
            ),
            Decimal("0"),
            Decimal("1"),
        )
        trend_direction = _trend_direction(ema_spread_signed, lookback_move_pct)

        bb_width_pct: Decimal | None = None
        bb_width_rank: Decimal | None = None
        band_proximity: Decimal | None = None
        range_score = Decimal("0")
        bands = bollinger_bands(closes, period=self.bb_strategy.bollinger_period)
        current_band = bands[-1] if bands else None
        if current_band is not None:
            bb_width_pct = current_band.width_pct
            recent_widths = [
                band.width_pct
                for band in bands[-self.bb_strategy.squeeze_lookback :]
                if band is not None
            ]
            bb_width_rank = _rank_fraction(recent_widths, current_band.width_pct)
            band_proximity = _band_proximity(latest.close, current_band)
            range_score = _clamp(
                ((Decimal("1") - bb_width_rank) * Decimal("0.55"))
                + (band_proximity * Decimal("0.45")),
                Decimal("0"),
                Decimal("1"),
            )

        if (
            trend_score >= self.trend_regime_threshold
            and trend_score >= range_score
        ):
            mode = "trend"
            reason = "EMA spread and lookback move show directional pressure."
        elif (
            range_score >= self.range_regime_threshold
            and range_score > trend_score
        ):
            mode = "mean_reversion"
            reason = "Bollinger width/proximity favors a reversion setup."
        else:
            mode = "neutral"
            reason = "No strong trend or reversion regime detected."

        return _MarketRegime(
            mode=mode,
            trend_score=trend_score,
            range_score=range_score,
            reason=reason,
            trend_direction=trend_direction,
            ema_spread_pct=ema_spread_pct,
            lookback_move_pct=lookback_move_pct,
            bb_width_pct=bb_width_pct,
            bb_width_rank=bb_width_rank,
            band_proximity=band_proximity,
        )

    def _bb_entry_fights_strong_trend(
        self,
        bb_signal: StrategySignal,
        market_regime: _MarketRegime,
    ) -> bool:
        if market_regime.mode != "trend" or market_regime.trend_direction is None:
            return False
        if (
            market_regime.trend_score - market_regime.range_score
            < self.strong_regime_margin
        ):
            return False
        if bb_signal.action == SignalAction.ENTER_LONG:
            return market_regime.trend_direction == SignalDirection.SHORT
        if bb_signal.action == SignalAction.ENTER_SHORT:
            return market_regime.trend_direction == SignalDirection.LONG
        return False

    def _exit_for_open_position(
        self,
        *,
        context: StrategyContext,
        open_direction: SignalDirection,
        primary_signal: StrategySignal,
        metadata: dict[str, Any],
    ) -> StrategySignal | None:
        latest = context.latest_candle
        if latest is None:
            return None
        if not self.exit_on_opposite_entry:
            return None
        if open_direction == SignalDirection.LONG and primary_signal.action == SignalAction.ENTER_SHORT:
            return StrategySignal(
                strategy_name=self.name,
                pair=context.pair,
                interval=context.interval,
                action=SignalAction.EXIT_LONG,
                confidence=primary_signal.confidence,
                reason="Adaptive hybrid trend reversed; exiting long before considering short.",
                timestamp_ms=latest.close_time_ms,
                direction=SignalDirection.LONG,
                entry_price=latest.close,
                metadata=metadata,
            )
        if open_direction == SignalDirection.SHORT and primary_signal.action == SignalAction.ENTER_LONG:
            return StrategySignal(
                strategy_name=self.name,
                pair=context.pair,
                interval=context.interval,
                action=SignalAction.EXIT_SHORT,
                confidence=primary_signal.confidence,
                reason="Adaptive hybrid trend reversed; exiting short before considering long.",
                timestamp_ms=latest.close_time_ms,
                direction=SignalDirection.SHORT,
                entry_price=latest.close,
                metadata=metadata,
            )
        return None

    def _clone_signal(
        self,
        signal: StrategySignal,
        *,
        action: SignalAction,
        reason: str,
        metadata: dict[str, Any],
    ) -> StrategySignal:
        merged_metadata = dict(metadata)
        merged_metadata["source_signal"] = signal.to_dict()
        return StrategySignal(
            strategy_name=self.name,
            pair=signal.pair,
            interval=signal.interval,
            action=action,
            confidence=signal.confidence,
            reason=reason,
            timestamp_ms=signal.timestamp_ms,
            direction=signal.direction,
            entry_price=signal.entry_price,
            stop_loss=signal.stop_loss,
            take_profit=signal.take_profit,
            metadata=merged_metadata,
        )


def _opposite_entry(action: SignalAction, other: SignalAction) -> bool:
    return (
        action == SignalAction.ENTER_LONG
        and other == SignalAction.ENTER_SHORT
        or action == SignalAction.ENTER_SHORT
        and other == SignalAction.ENTER_LONG
    )


def _exit_matches_position(
    action: SignalAction,
    direction: SignalDirection | None,
) -> bool:
    if action == SignalAction.EXIT_LONG:
        return direction == SignalDirection.LONG
    if action == SignalAction.EXIT_SHORT:
        return direction == SignalDirection.SHORT
    return False


def _open_position_direction(
    features: dict[str, Any],
    pair: str,
) -> SignalDirection | None:
    raw = features.get("open_position")
    if not isinstance(raw, dict):
        return None
    raw_pair = raw.get("pair")
    if raw_pair is not None and raw_pair != pair:
        return None
    direction = raw.get("direction")
    if isinstance(direction, SignalDirection):
        return direction
    if isinstance(direction, str):
        normalized = direction.strip().lower()
        if normalized == SignalDirection.LONG.value:
            return SignalDirection.LONG
        if normalized == SignalDirection.SHORT.value:
            return SignalDirection.SHORT
    return None


def _open_position_source_strategy(
    features: dict[str, Any],
    pair: str,
) -> str | None:
    raw = features.get("open_position")
    if not isinstance(raw, dict):
        return None
    raw_pair = raw.get("pair")
    if raw_pair is not None and raw_pair != pair:
        return None
    metadata = raw.get("metadata")
    if not isinstance(metadata, dict):
        return None
    primary_strategy = metadata.get("primary_strategy")
    if isinstance(primary_strategy, str) and primary_strategy:
        return primary_strategy
    source_signal = metadata.get("source_signal")
    if isinstance(source_signal, dict):
        strategy_name = source_signal.get("strategy_name")
        if isinstance(strategy_name, str) and strategy_name:
            return strategy_name
    return None


def _ratio(value: Decimal, reference: Decimal) -> Decimal:
    if reference <= 0:
        return Decimal("0")
    return _clamp(value / reference, Decimal("0"), Decimal("1"))


def _clamp(value: Decimal, low: Decimal, high: Decimal) -> Decimal:
    return min(max(value, low), high)


def _rank_fraction(values: list[Decimal], current: Decimal) -> Decimal:
    if not values:
        return Decimal("1")
    less_or_equal = sum(1 for value in values if value <= current)
    return Decimal(less_or_equal) / Decimal(len(values))


def _band_proximity(close: Decimal, band: Any) -> Decimal:
    if close <= 0 or band.upper == band.lower:
        return Decimal("0")
    if close <= band.middle:
        distance = (band.middle - close) / max(
            band.middle - band.lower,
            Decimal("0.00000001"),
        )
    else:
        distance = (close - band.middle) / max(
            band.upper - band.middle,
            Decimal("0.00000001"),
        )
    return _clamp(distance, Decimal("0"), Decimal("1"))


def _trend_direction(
    ema_spread_signed: Decimal,
    lookback_move_pct: Decimal,
) -> SignalDirection | None:
    score = ema_spread_signed + (lookback_move_pct * Decimal("0.5"))
    if score > 0:
        return SignalDirection.LONG
    if score < 0:
        return SignalDirection.SHORT
    return None
