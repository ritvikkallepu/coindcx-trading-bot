from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from app.data.indicators import average_true_range, macd, relative_strength_index, simple_moving_average
from app.strategies.base import (
    SignalAction,
    SignalDirection,
    Strategy,
    StrategyContext,
    StrategySignal,
    clamp_confidence,
)


@dataclass(frozen=True)
class RSIMACDMomentumStrategy(Strategy):
    """RSI + MACD momentum strategy with strategy-owned exits.

    Entries use RSI reclaim/loss of the 50 zone plus a fresh MACD momentum flip.
    Normal exits are also RSI/MACD based. The only broker-side exit this strategy
    intentionally leaves active is the initial emergency stop loss.
    """

    name: str = "rsi_macd_momentum"
    rsi_period: int = 14
    macd_fast_period: int = 12
    macd_slow_period: int = 26
    macd_signal_period: int = 9
    atr_period: int = 14
    volume_period: int = 20
    extension_lookback: int = 5
    rsi_long_confirm: Decimal = Decimal("50")
    rsi_short_confirm: Decimal = Decimal("50")
    rsi_long_max: Decimal = Decimal("68")
    rsi_short_min: Decimal = Decimal("32")
    long_exit_rsi: Decimal = Decimal("45")
    short_exit_rsi: Decimal = Decimal("55")
    early_long_failure_rsi: Decimal = Decimal("48")
    early_short_failure_rsi: Decimal = Decimal("52")
    long_exhaustion_rsi: Decimal = Decimal("70")
    long_exhaustion_exit_rsi: Decimal = Decimal("65")
    short_exhaustion_rsi: Decimal = Decimal("30")
    short_exhaustion_exit_rsi: Decimal = Decimal("35")
    macd_cross_max_age: int = 2
    rsi_cross_max_age: int = 2
    exit_histogram_confirm_candles: int = 2
    exhaustion_lookback: int = 8
    early_failure_candles: int = 3
    min_volume_ratio: Decimal = Decimal("0.50")
    min_rsi_delta: Decimal = Decimal("0.25")
    min_macd_histogram_atr: Decimal = Decimal("0.02")
    max_extension_atr: Decimal = Decimal("1.20")
    stop_atr_multiple: Decimal = Decimal("2.0")
    fallback_stop_pct: Decimal = Decimal("0.012")

    def evaluate(self, context: StrategyContext) -> StrategySignal:
        latest = context.latest_candle
        if latest is None:
            return self._hold(context, "No candles available.")

        required = max(
            self.macd_slow_period + self.macd_signal_period + 1,
            self.rsi_period + 2,
            self.atr_period + 1,
            self.volume_period + 1,
            self.extension_lookback + 1,
            self.exhaustion_lookback + 1,
        )
        if len(context.candles) < required:
            return self._hold(context, f"Need at least {required} candles.")

        closes = context.candles.closes()
        volumes = context.candles.volumes()
        rsi_values = relative_strength_index(closes, self.rsi_period)
        macd_values = macd(
            closes,
            fast_period=self.macd_fast_period,
            slow_period=self.macd_slow_period,
            signal_period=self.macd_signal_period,
        )
        atr_values = average_true_range(context.candles, self.atr_period)
        volume_sma = simple_moving_average(volumes, self.volume_period)

        current_rsi = rsi_values[-1]
        previous_rsi = rsi_values[-2]
        current_macd = macd_values[-1]
        previous_macd = macd_values[-2]
        current_atr = atr_values[-1]
        average_volume = volume_sma[-1]
        if (
            current_rsi is None
            or previous_rsi is None
            or current_macd is None
            or previous_macd is None
            or current_atr is None
            or current_atr <= 0
            or average_volume is None
            or average_volume <= 0
        ):
            return self._hold(context, "Indicators are still warming up.")

        volume_ratio = latest.volume / average_volume if average_volume > 0 else Decimal("0")
        recent_average = (
            sum(closes[-self.extension_lookback :], Decimal("0"))
            / Decimal(self.extension_lookback)
        )
        extension_atr = (
            abs(latest.close - recent_average) / current_atr
            if current_atr > 0
            else Decimal("999")
        )
        base_metadata = self._base_metadata(
            context=context,
            current_rsi=current_rsi,
            previous_rsi=previous_rsi,
            current_macd=current_macd,
            previous_macd=previous_macd,
            current_atr=current_atr,
            volume_ratio=volume_ratio,
            extension_atr=extension_atr,
        )

        open_position = _open_position(context.features, context.pair)
        if open_position is not None:
            return self._evaluate_exit(
                context=context,
                open_position=open_position,
                rsi_values=rsi_values,
                macd_values=macd_values,
                metadata=base_metadata,
            )

        long_block = self._entry_block(
            direction=SignalDirection.LONG,
            rsi_values=rsi_values,
            macd_values=macd_values,
            current_atr=current_atr,
            volume_ratio=volume_ratio,
            extension_atr=extension_atr,
        )
        if long_block is None:
            return self._entry_signal(
                context=context,
                direction=SignalDirection.LONG,
                atr=current_atr,
                reason="RSI reclaimed 50 and MACD momentum flipped bullish.",
                metadata={
                    **base_metadata,
                    "entry_type": "rsi_macd_momentum",
                    "macd_cross_type": "bullish",
                    "rsi_trigger": "cross_above_50",
                },
            )

        short_block = self._entry_block(
            direction=SignalDirection.SHORT,
            rsi_values=rsi_values,
            macd_values=macd_values,
            current_atr=current_atr,
            volume_ratio=volume_ratio,
            extension_atr=extension_atr,
        )
        if short_block is None:
            return self._entry_signal(
                context=context,
                direction=SignalDirection.SHORT,
                atr=current_atr,
                reason="RSI lost 50 and MACD momentum flipped bearish.",
                metadata={
                    **base_metadata,
                    "entry_type": "rsi_macd_momentum",
                    "macd_cross_type": "bearish",
                    "rsi_trigger": "cross_below_50",
                },
            )

        return self._hold(
            context,
            _combine_hold_reason(long_block, short_block),
            {
                **base_metadata,
                "entry_type": "rsi_macd_momentum",
                "long_rejection": long_block,
                "short_rejection": short_block,
            },
        )

    def _entry_block(
        self,
        *,
        direction: SignalDirection,
        rsi_values: list[Decimal | None],
        macd_values: list[Any],
        current_atr: Decimal,
        volume_ratio: Decimal,
        extension_atr: Decimal,
    ) -> str | None:
        current_rsi = rsi_values[-1]
        previous_rsi = rsi_values[-2]
        current_macd = macd_values[-1]
        previous_macd = macd_values[-2]
        if (
            current_rsi is None
            or previous_rsi is None
            or current_macd is None
            or previous_macd is None
        ):
            return "Indicators are still warming up."

        if volume_ratio < self.min_volume_ratio:
            return f"Volume ratio below threshold: {volume_ratio:.2f} < {self.min_volume_ratio}."
        if extension_atr > self.max_extension_atr:
            return f"Late chase blocked: extension {extension_atr:.2f} ATR > {self.max_extension_atr}."
        if abs(current_rsi - previous_rsi) < self.min_rsi_delta:
            return "RSI is too flat near the trigger zone."
        if abs(current_macd.histogram) < current_atr * self.min_macd_histogram_atr:
            return "MACD histogram is too flat versus ATR."

        if direction == SignalDirection.LONG:
            if current_rsi < self.rsi_long_confirm:
                return "RSI has not reclaimed 50 for a long."
            if current_rsi > self.rsi_long_max:
                return f"RSI overheated for a fresh long: {current_rsi:.2f} > {self.rsi_long_max}."
            rsi_age = _recent_rsi_cross_age(
                rsi_values,
                threshold=self.rsi_long_confirm,
                direction=direction,
                max_age=self.rsi_cross_max_age,
            )
            if rsi_age is None:
                return "RSI 50 reclaim is stale or missing."
            macd_age = _recent_macd_cross_age(
                macd_values,
                direction=direction,
                max_age=self.macd_cross_max_age,
            )
            if macd_age is None:
                return "MACD bullish cross/flip is stale or missing."
            if current_macd.histogram <= 0:
                return "MACD histogram is not bullish for a long."
            return None

        if current_rsi > self.rsi_short_confirm:
            return "RSI has not lost 50 for a short."
        if current_rsi < self.rsi_short_min:
            return f"RSI too oversold for a fresh short: {current_rsi:.2f} < {self.rsi_short_min}."
        rsi_age = _recent_rsi_cross_age(
            rsi_values,
            threshold=self.rsi_short_confirm,
            direction=direction,
            max_age=self.rsi_cross_max_age,
        )
        if rsi_age is None:
            return "RSI 50 loss is stale or missing."
        macd_age = _recent_macd_cross_age(
            macd_values,
            direction=direction,
            max_age=self.macd_cross_max_age,
        )
        if macd_age is None:
            return "MACD bearish cross/flip is stale or missing."
        if current_macd.histogram >= 0:
            return "MACD histogram is not bearish for a short."
        return None

    def _evaluate_exit(
        self,
        *,
        context: StrategyContext,
        open_position: dict[str, Any],
        rsi_values: list[Decimal | None],
        macd_values: list[Any],
        metadata: dict[str, Any],
    ) -> StrategySignal:
        latest = context.latest_candle
        assert latest is not None

        source_strategy = str(open_position.get("strategy_name") or "")
        position_metadata = open_position.get("metadata")
        position_metadata = position_metadata if isinstance(position_metadata, dict) else {}
        if (
            source_strategy != self.name
            and str(position_metadata.get("strategy_exit_model") or "") != self.name
        ):
            return self._hold(
                context,
                f"Open position is managed by {source_strategy or 'another strategy'}.",
                {**metadata, "entry_type": "rsi_macd_momentum"},
            )

        direction = _direction(open_position.get("direction"))
        if direction is None:
            return self._hold(context, "Open position direction is unavailable.", metadata)

        current_rsi = rsi_values[-1]
        previous_rsi = rsi_values[-2]
        current_macd = macd_values[-1]
        previous_macd = macd_values[-2]
        if (
            current_rsi is None
            or previous_rsi is None
            or current_macd is None
            or previous_macd is None
        ):
            return self._hold(context, "Waiting for RSI/MACD exit indicators.", metadata)

        opened_at_ms = int(open_position.get("opened_at_ms") or 0)
        candles_held = sum(1 for candle in context.candles if candle.close_time_ms > opened_at_ms)
        exit_reason = self._exit_reason(
            direction=direction,
            candles_held=candles_held,
            rsi_values=rsi_values,
            macd_values=macd_values,
        )
        if exit_reason is None:
            return self._hold(
                context,
                f"Already {direction.value}; RSI/MACD momentum has not invalidated the position.",
                {
                    **metadata,
                    "entry_type": "rsi_macd_momentum",
                    "candles_held": candles_held,
                },
            )

        action = (
            SignalAction.EXIT_LONG
            if direction == SignalDirection.LONG
            else SignalAction.EXIT_SHORT
        )
        confidence = Decimal("0.62") + (
            abs(current_rsi - self.rsi_long_confirm) / Decimal("100")
        )
        return StrategySignal(
            strategy_name=self.name,
            pair=context.pair,
            interval=context.interval,
            action=action,
            confidence=clamp_confidence(confidence),
            reason=exit_reason,
            timestamp_ms=latest.close_time_ms,
            direction=direction,
            entry_price=latest.close,
            metadata={
                **metadata,
                "entry_type": "rsi_macd_exit",
                "exit_model": "rsi_macd_momentum",
                "exit_trigger_type": "strategy_momentum_exit",
                "candles_held": candles_held,
            },
        )

    def _exit_reason(
        self,
        *,
        direction: SignalDirection,
        candles_held: int,
        rsi_values: list[Decimal | None],
        macd_values: list[Any],
    ) -> str | None:
        current_rsi = rsi_values[-1]
        previous_rsi = rsi_values[-2]
        current_macd = macd_values[-1]
        previous_macd = macd_values[-2]
        if (
            current_rsi is None
            or previous_rsi is None
            or current_macd is None
            or previous_macd is None
        ):
            return None

        if direction == SignalDirection.LONG:
            if (
                candles_held <= self.early_failure_candles
                and current_rsi < self.early_long_failure_rsi
                and current_macd.histogram < 0
            ):
                return "RSI/MACD early failure exit: long lost momentum soon after entry."
            if (
                current_rsi <= self.long_exit_rsi
                and _last_histograms_same_side(
                    macd_values,
                    positive=False,
                    count=self.exit_histogram_confirm_candles,
                )
            ):
                return "RSI/MACD exit: long RSI broke down and MACD stayed bearish."
            if (
                _line_crossed_down(previous_macd, current_macd)
                and current_rsi <= self.early_long_failure_rsi
                and current_rsi < previous_rsi
            ):
                return "RSI/MACD exit: MACD crossed bearish while RSI was falling."
            if (
                _recent_rsi_extreme(rsi_values, high=True, lookback=self.exhaustion_lookback)
                >= self.long_exhaustion_rsi
                and current_rsi <= self.long_exhaustion_exit_rsi
                and _histogram_weakening(macd_values, direction)
            ):
                return "RSI/MACD exhaustion exit: long momentum faded after overbought RSI."
            return None

        if (
            candles_held <= self.early_failure_candles
            and current_rsi > self.early_short_failure_rsi
            and current_macd.histogram > 0
        ):
            return "RSI/MACD early failure exit: short lost momentum soon after entry."
        if (
            current_rsi >= self.short_exit_rsi
            and _last_histograms_same_side(
                macd_values,
                positive=True,
                count=self.exit_histogram_confirm_candles,
            )
        ):
            return "RSI/MACD exit: short RSI recovered and MACD stayed bullish."
        if (
            _line_crossed_up(previous_macd, current_macd)
            and current_rsi >= self.early_short_failure_rsi
            and current_rsi > previous_rsi
        ):
            return "RSI/MACD exit: MACD crossed bullish while RSI was rising."
        if (
            _recent_rsi_extreme(rsi_values, high=False, lookback=self.exhaustion_lookback)
            <= self.short_exhaustion_rsi
            and current_rsi >= self.short_exhaustion_exit_rsi
            and _histogram_weakening(macd_values, direction)
        ):
            return "RSI/MACD exhaustion exit: short momentum faded after oversold RSI."
        return None

    def _entry_signal(
        self,
        *,
        context: StrategyContext,
        direction: SignalDirection,
        atr: Decimal,
        reason: str,
        metadata: dict[str, Any],
    ) -> StrategySignal:
        latest = context.latest_candle
        assert latest is not None

        stop_distance = atr * self.stop_atr_multiple if atr > 0 else latest.close * self.fallback_stop_pct
        if direction == SignalDirection.LONG:
            action = SignalAction.ENTER_LONG
            stop_loss = max(latest.close - stop_distance, Decimal("0.00000001"))
        else:
            action = SignalAction.ENTER_SHORT
            stop_loss = latest.close + stop_distance

        confidence = Decimal("0.58") + (
            abs(metadata.get("rsi", Decimal("50")) - Decimal("50")) / Decimal("100")
        )
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
            stop_loss=stop_loss,
            take_profit=None,
            metadata={
                **metadata,
                "strategy_managed_exits": True,
                "strategy_exit_model": self.name,
                "emergency_stop_model": "static_atr",
                "stop_atr_multiple": self.stop_atr_multiple,
                "take_profit_priority": "strategy_exit",
                "atr_dynamic_exits_enabled": False,
                "atr_stop_enabled": False,
                "atr_take_profit_enabled": False,
                "atr_trailing_enabled": False,
                "profit_lock_enabled": False,
                "bb_trail_enabled": False,
                "trailing_stop_enabled": False,
            },
        )

    def _base_metadata(
        self,
        *,
        context: StrategyContext,
        current_rsi: Decimal,
        previous_rsi: Decimal,
        current_macd: Any,
        previous_macd: Any,
        current_atr: Decimal,
        volume_ratio: Decimal,
        extension_atr: Decimal,
    ) -> dict[str, Any]:
        pair_profile = context.features.get("pair_profile")
        metadata: dict[str, Any] = {
            "rsi": current_rsi,
            "previous_rsi": previous_rsi,
            "macd_line": current_macd.macd,
            "macd_signal": current_macd.signal,
            "macd_histogram": current_macd.histogram,
            "previous_macd_histogram": previous_macd.histogram,
            "atr": current_atr,
            "volume_ratio": volume_ratio,
            "extension_atr": extension_atr,
            "rsi_period": self.rsi_period,
            "macd_fast_period": self.macd_fast_period,
            "macd_slow_period": self.macd_slow_period,
            "macd_signal_period": self.macd_signal_period,
            "strategy_exit_model": self.name,
        }
        if isinstance(pair_profile, dict):
            metadata["pair_profile"] = pair_profile.get("key", "")
            metadata["pair_profile_label"] = pair_profile.get("label", "")
        return metadata

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


def _open_position(features: dict[str, Any], pair: str) -> dict[str, Any] | None:
    raw = features.get("open_position")
    if not isinstance(raw, dict) or raw.get("pair") != pair:
        return None
    return raw


def _direction(value: Any) -> SignalDirection | None:
    if value == SignalDirection.LONG or value == SignalDirection.LONG.value:
        return SignalDirection.LONG
    if value == SignalDirection.SHORT or value == SignalDirection.SHORT.value:
        return SignalDirection.SHORT
    return None


def _recent_rsi_cross_age(
    values: list[Decimal | None],
    *,
    threshold: Decimal,
    direction: SignalDirection,
    max_age: int,
) -> int | None:
    for age in range(max_age + 1):
        current_index = len(values) - 1 - age
        previous_index = current_index - 1
        if previous_index < 0:
            break
        previous = values[previous_index]
        current = values[current_index]
        if previous is None or current is None:
            continue
        if direction == SignalDirection.LONG and previous < threshold <= current:
            return age
        if direction == SignalDirection.SHORT and previous > threshold >= current:
            return age
    return None


def _recent_macd_cross_age(
    values: list[Any],
    *,
    direction: SignalDirection,
    max_age: int,
) -> int | None:
    for age in range(max_age + 1):
        current_index = len(values) - 1 - age
        previous_index = current_index - 1
        if previous_index < 0:
            break
        previous = values[previous_index]
        current = values[current_index]
        if previous is None or current is None:
            continue
        if direction == SignalDirection.LONG and (
            _line_crossed_up(previous, current) or _histogram_crossed_positive(previous, current)
        ):
            return age
        if direction == SignalDirection.SHORT and (
            _line_crossed_down(previous, current) or _histogram_crossed_negative(previous, current)
        ):
            return age
    return None


def _line_crossed_up(previous: Any, current: Any) -> bool:
    return previous.macd <= previous.signal and current.macd > current.signal


def _line_crossed_down(previous: Any, current: Any) -> bool:
    return previous.macd >= previous.signal and current.macd < current.signal


def _histogram_crossed_positive(previous: Any, current: Any) -> bool:
    return previous.histogram <= 0 and current.histogram > 0


def _histogram_crossed_negative(previous: Any, current: Any) -> bool:
    return previous.histogram >= 0 and current.histogram < 0


def _last_histograms_same_side(values: list[Any], *, positive: bool, count: int) -> bool:
    if count <= 0 or len(values) < count:
        return False
    recent = values[-count:]
    if any(point is None for point in recent):
        return False
    if positive:
        return all(point.histogram > 0 for point in recent)
    return all(point.histogram < 0 for point in recent)


def _histogram_weakening(values: list[Any], direction: SignalDirection) -> bool:
    if len(values) < 3 or any(point is None for point in values[-3:]):
        return False
    first, second, third = values[-3], values[-2], values[-1]
    if direction == SignalDirection.LONG:
        return third.histogram < second.histogram < first.histogram
    return third.histogram > second.histogram > first.histogram


def _recent_rsi_extreme(values: list[Decimal | None], *, high: bool, lookback: int) -> Decimal:
    recent = [value for value in values[-lookback:] if value is not None]
    if not recent:
        return Decimal("0") if high else Decimal("100")
    return max(recent) if high else min(recent)


def _combine_hold_reason(long_block: str, short_block: str) -> str:
    if long_block == short_block:
        return long_block
    return f"No RSI/MACD setup. Long: {long_block} Short: {short_block}"
