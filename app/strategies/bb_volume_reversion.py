from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.data.indicators import average_true_range, bollinger_bands, simple_moving_average
from app.strategies.base import (
    SignalAction,
    SignalDirection,
    Strategy,
    StrategyContext,
    StrategySignal,
    clamp_confidence,
)


@dataclass(frozen=True)
class BollingerVolumeMeanReversionStrategy(Strategy):
    name: str = "bb_volume_reversion"
    bollinger_period: int = 20
    squeeze_lookback: int = 40
    squeeze_rank_threshold: Decimal = Decimal("0.35")
    volume_period: int = 20
    volume_multiplier: Decimal = Decimal("1.15")
    atr_period: int = 14
    stop_atr_multiple: Decimal = Decimal("1.2")
    atr_trailing_multiple: Decimal = Decimal("2")
    fallback_stop_pct: Decimal = Decimal("0.008")

    def evaluate(self, context: StrategyContext) -> StrategySignal:
        latest = context.latest_candle
        if latest is None:
            return self._hold(context, "No candles available.")

        required = max(
            self.bollinger_period + self.squeeze_lookback,
            self.volume_period,
            self.atr_period,
        )
        if len(context.candles) < required:
            return self._hold(context, f"Need at least {required} candles.")

        closes = context.candles.closes()
        volumes = context.candles.volumes()
        bands = bollinger_bands(closes, period=self.bollinger_period)
        atr_values = average_true_range(context.candles, self.atr_period)
        volume_sma_values = simple_moving_average(volumes, self.volume_period)

        current_band = bands[-1]
        current_atr = atr_values[-1]
        average_volume = volume_sma_values[-1]
        if current_band is None or average_volume is None:
            return self._hold(context, "Indicators are still warming up.")

        recent_widths = [
            band.width_pct
            for band in bands[-self.squeeze_lookback :]
            if band is not None
        ]
        squeeze_rank = _rank_fraction(recent_widths, current_band.width_pct)
        volume_confirmed = latest.volume >= average_volume * self.volume_multiplier
        squeezed = squeeze_rank <= self.squeeze_rank_threshold

        metadata = {
            "bb_middle": current_band.middle,
            "bb_upper": current_band.upper,
            "bb_lower": current_band.lower,
            "bb_width_pct": current_band.width_pct,
            "squeeze_rank": squeeze_rank,
            "volume": latest.volume,
            "average_volume": average_volume,
            "volume_confirmed": volume_confirmed,
            "atr": current_atr,
        }

        if squeezed and volume_confirmed and latest.close <= current_band.lower:
            return self._entry_signal(
                context=context,
                direction=SignalDirection.LONG,
                reason="Price tagged the lower Bollinger Band after a squeeze with volume confirmation.",
                target=current_band.middle,
                atr=current_atr,
                metadata=metadata,
            )

        if squeezed and volume_confirmed and latest.close >= current_band.upper:
            return self._entry_signal(
                context=context,
                direction=SignalDirection.SHORT,
                reason="Price tagged the upper Bollinger Band after a squeeze with volume confirmation.",
                target=current_band.middle,
                atr=current_atr,
                metadata=metadata,
            )

        return self._hold(
            context,
            "No Bollinger squeeze mean-reversion setup.",
            metadata,
        )

    def _entry_signal(
        self,
        *,
        context: StrategyContext,
        direction: SignalDirection,
        reason: str,
        target: Decimal,
        atr: Decimal | None,
        metadata: dict,
    ) -> StrategySignal:
        latest = context.latest_candle
        assert latest is not None

        stop_distance = (
            atr * self.stop_atr_multiple
            if atr is not None and atr > 0
            else latest.close * self.fallback_stop_pct
        )
        if direction == SignalDirection.LONG:
            action = SignalAction.ENTER_LONG
            stop_loss = latest.close - stop_distance
        else:
            action = SignalAction.ENTER_SHORT
            stop_loss = latest.close + stop_distance

        squeeze_rank = metadata.get("squeeze_rank") or Decimal("1")
        confidence = Decimal("0.6") + ((Decimal("1") - squeeze_rank) * Decimal("0.25"))

        # Respect global config overrides
        config = context.features.get("backtest_config")
        config = config if isinstance(config, dict) else {}

        metadata = {
            "stop_atr_multiple": self.stop_atr_multiple,
            "atr_trailing_multiple": self.atr_trailing_multiple,
            "trailing_atr_multiple": self.atr_trailing_multiple,
            **metadata,
        }

        for key in (
            "trailing_stop_enabled",
            "trailing_stop_activation_pct",
            "trailing_stop_distance_pct",
            "atr_stop_enabled",
            "atr_take_profit_enabled",
            "atr_trailing_enabled",
            "profit_lock_enabled",
            "bb_trail_enabled",
        ):
            if key in config:
                metadata[key] = config[key]
        if "atr_dynamic_exits_enabled" in config:
            metadata["atr_dynamic_exits_enabled"] = _bool_value(config.get("atr_dynamic_exits_enabled"), True)

        return StrategySignal(
            strategy_name=self.name,
            pair=context.pair,
            interval=context.interval,
            action=action,
            confidence=clamp_confidence(confidence),
            reason=reason,
            timestamp_ms=latest.close_time_ms,
            direction=direction,
            entry_price=latest.close,
            stop_loss=max(stop_loss, Decimal("0")),
            take_profit=max(target, Decimal("0")),
            metadata=metadata,
        )

    def _hold(
        self,
        context: StrategyContext,
        reason: str,
        metadata: dict | None = None,
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


def _bool_value(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False
    return default


def _rank_fraction(values: list[Decimal], current: Decimal) -> Decimal:
    if not values:
        return Decimal("1")
    less_or_equal = sum(1 for value in values if value <= current)
    return Decimal(less_or_equal) / Decimal(len(values))

