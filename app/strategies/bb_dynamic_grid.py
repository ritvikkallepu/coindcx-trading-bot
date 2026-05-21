from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from app.data.indicators import BollingerBandPoint, average_true_range, bollinger_bands
from app.persistence.paper_state import PaperStateStore
from app.strategies.base import (
    SignalAction,
    SignalDirection,
    Strategy,
    StrategyContext,
    StrategySignal,
    clamp_confidence,
)


@dataclass
class _GridTrailState:
    direction: SignalDirection
    entries: int
    last_grid_entry_price: Decimal
    entry_price: Decimal
    best_close: Decimal | None = None
    trailing_level: Decimal | None = None
    trailing_active: bool = False


@dataclass
class BollingerDynamicFuturesGridStrategy(Strategy):
    name: str = "bb_dynamic_grid"
    bollinger_period: int = 20
    atr_period: int = 14
    grid_levels: int = 6
    max_grid_entries: int = 4
    min_grid_spacing_pct: Decimal = Decimal("0.006")
    stop_atr_multiple: Decimal = Decimal("2")
    fallback_stop_pct: Decimal = Decimal("0.035")
    trail_activation_pct: Decimal = Decimal("1")
    trail_distance_pct: Decimal = Decimal("2")
    state_store: PaperStateStore | None = None
    _states: dict[str, _GridTrailState] = field(default_factory=dict, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.state_store:
             # We need to know which pair/interval to load for.
             # Strategy.evaluate() is called per pair.
             # Strategy objects are often shared.
             # Wait, the instructions said:
             # "On init: call load_strategy_state() with key = f"{self.name}:{pair}:{interval}" 
             # and restore _states dict if found"
             # But __init__ doesn't know the pair/interval yet.
             # Let's check where it's instantiated.
             pass

    def _get_state(self, context: StrategyContext) -> _GridTrailState | None:
        state_key = f"{self.name}:{context.pair}:{context.interval}"
        if state_key not in self._states and self.state_store:
            saved = self.state_store.load_strategy_state(state_key)
            if saved:
                # Convert dict back to _GridTrailState
                self._states[state_key] = _GridTrailState(
                    direction=SignalDirection(saved["direction"]),
                    entries=int(saved["entries"]),
                    last_grid_entry_price=Decimal(str(saved["last_grid_entry_price"])),
                    entry_price=Decimal(str(saved["entry_price"])),
                    best_close=Decimal(str(saved["best_close"])) if saved.get("best_close") else None,
                    trailing_level=Decimal(str(saved["trailing_level"])) if saved.get("trailing_level") else None,
                    trailing_active=bool(saved["trailing_active"]),
                )
        return self._states.get(state_key)

    def _set_state(self, context: StrategyContext, state: _GridTrailState) -> None:
        state_key = f"{self.name}:{context.pair}:{context.interval}"
        self._states[state_key] = state
        if self.state_store:
            self.state_store.save_strategy_state(state_key, asdict(state))

    def _pop_state(self, context: StrategyContext) -> None:
        state_key = f"{self.name}:{context.pair}:{context.interval}"
        self._states.pop(state_key, None)
        if self.state_store:
            # Maybe clear? Instructions didn't say to clear, just to save after every mutation.
            # Pop is a mutation.
            self.state_store.save_strategy_state(state_key, {})

    def evaluate(self, context: StrategyContext) -> StrategySignal:
        latest = context.latest_candle
        if latest is None:
            return self._hold(context, "No candles available.")

        required = max(self.bollinger_period, self.atr_period)
        if len(context.candles) < required:
            return self._hold(context, f"Need at least {required} candles.")

        closes = context.candles.closes()
        bands = bollinger_bands(closes, period=self.bollinger_period)
        atr_values = average_true_range(context.candles, self.atr_period)
        current_band = bands[-1]
        current_atr = atr_values[-1]
        if current_band is None:
            return self._hold(context, "Bollinger Bands are still warming up.")

        position = _open_position(context)
        if position is None:
            self._pop_state(context)
            return self._evaluate_new_grid(
                context=context,
                band=current_band,
                atr=current_atr,
            )

        if position.get("strategy_name") not in {None, "", self.name}:
            return self._hold(
                context,
                "Another strategy owns the open position.",
                self._base_metadata(current_band, current_atr, context),
            )

        return self._evaluate_open_grid(
            context=context,
            position=position,
            band=current_band,
            atr=current_atr,
        )

    def _evaluate_new_grid(
        self,
        *,
        context: StrategyContext,
        band: BollingerBandPoint,
        atr: Decimal | None,
    ) -> StrategySignal:
        latest = context.latest_candle
        assert latest is not None

        metadata = self._base_metadata(band, atr, context)
        if band.upper <= band.lower:
            return self._hold(context, "Bollinger range is not usable.", metadata)

        if latest.close <= band.lower:
            return self._entry_signal(
                context=context,
                direction=SignalDirection.LONG,
                reason="Lower Bollinger grid boundary tagged; opening long futures grid basket.",
                band=band,
                atr=atr,
                metadata=metadata,
                allow_scale_in=False,
                next_entry_count=1,
            )

        if latest.close >= band.upper:
            return self._entry_signal(
                context=context,
                direction=SignalDirection.SHORT,
                reason="Upper Bollinger grid boundary tagged; opening short futures grid basket.",
                band=band,
                atr=atr,
                metadata=metadata,
                allow_scale_in=False,
                next_entry_count=1,
            )

        return self._hold(
            context,
            "Price is inside the Bollinger grid range.",
            metadata,
        )

    def _evaluate_open_grid(
        self,
        *,
        context: StrategyContext,
        position: dict[str, Any],
        band: BollingerBandPoint,
        atr: Decimal | None,
    ) -> StrategySignal:
        latest = context.latest_candle
        assert latest is not None

        direction = _direction(position.get("direction"))
        if direction is None:
            return self._hold(context, "Open position direction is unavailable.")

        state = self._state_for_position(context, position, direction)
        activation_pct, distance_pct = self._trail_settings(context)
        self._update_dynamic_trail(
            state=state,
            latest_close=latest.close,
            activation_pct=activation_pct,
            distance_pct=distance_pct,
        )
        self._set_state(context, state)

        metadata = self._base_metadata(band, atr, context)
        metadata.update(
            {
                "grid_entries": state.entries,
                "max_grid_entries": self.max_grid_entries,
                "last_grid_entry_price": state.last_grid_entry_price,
                "grid_trailing_active": state.trailing_active,
                "grid_best_close": state.best_close,
                "grid_trailing_level": state.trailing_level,
                "trail_activation_pct": activation_pct,
                "trail_distance_pct": distance_pct,
            }
        )

        if self._dynamic_close_triggered(state, latest.close):
            action = (
                SignalAction.EXIT_LONG
                if direction == SignalDirection.LONG
                else SignalAction.EXIT_SHORT
            )
            return StrategySignal(
                strategy_name=self.name,
                pair=context.pair,
                interval=context.interval,
                action=action,
                direction=direction,
                confidence=Decimal("0.9"),
                reason=(
                    "Candle close broke the dynamic futures-grid trailing line; "
                    "closing the full grid basket."
                ),
                timestamp_ms=latest.close_time_ms,
                entry_price=latest.close,
                metadata=metadata,
            )

        spacing = self._grid_spacing(latest.close, band, atr)
        metadata["grid_spacing"] = spacing
        if self._scale_in_due(state, latest.close, spacing):
            return self._entry_signal(
                context=context,
                direction=direction,
                reason="Price moved to the next futures-grid level; adding to the basket.",
                band=band,
                atr=atr,
                metadata=metadata,
                allow_scale_in=True,
                next_entry_count=state.entries + 1,
            )

        return self._hold(
            context,
            "Open futures grid is waiting for next grid level or close-confirmed trail.",
            metadata,
        )

    def _entry_signal(
        self,
        *,
        context: StrategyContext,
        direction: SignalDirection,
        reason: str,
        band: BollingerBandPoint,
        atr: Decimal | None,
        metadata: dict[str, Any],
        allow_scale_in: bool,
        next_entry_count: int,
    ) -> StrategySignal:
        latest = context.latest_candle
        assert latest is not None

        action = (
            SignalAction.ENTER_LONG
            if direction == SignalDirection.LONG
            else SignalAction.ENTER_SHORT
        )
        stop_loss = self._disaster_stop(
            price=latest.close,
            direction=direction,
            atr=atr,
        )
        entry_metadata = {
            **metadata,
            "allow_scale_in": allow_scale_in,
            "grid_entries": next_entry_count,
            "max_grid_entries": self.max_grid_entries,
            "last_grid_entry_price": latest.close,
            "grid_mode": "linear_futures_grid",
            "exit_mode": "close_confirmed_dynamic_trail",
            "fixed_take_profit": False,
        }
        confidence = self._entry_confidence(latest.close, band, direction)
        return StrategySignal(
            strategy_name=self.name,
            pair=context.pair,
            interval=context.interval,
            action=action,
            confidence=confidence,
            reason=reason,
            timestamp_ms=latest.close_time_ms,
            direction=direction,
            entry_price=latest.close,
            stop_loss=stop_loss,
            take_profit=None,
            metadata=entry_metadata,
        )

    def _state_for_position(
        self,
        context: StrategyContext,
        position: dict[str, Any],
        direction: SignalDirection,
    ) -> _GridTrailState:
        key = _state_key(context)
        metadata = position.get("metadata") if isinstance(position.get("metadata"), dict) else {}
        entry_price = _decimal(position.get("entry_price"), Decimal("0"))
        entries = _entry_count(metadata)
        last_grid_entry = _decimal(
            metadata.get("last_grid_entry_price")
            or metadata.get("last_scale_in_price"),
            entry_price,
        )

        state = self._get_state(context)
        if (
            state is None
            or state.direction != direction
            or state.entry_price != entry_price
        ):
            state = _GridTrailState(
                direction=direction,
                entries=entries,
                last_grid_entry_price=last_grid_entry,
                entry_price=entry_price,
                best_close=_decimal_or_none(metadata.get("grid_best_close")),
                trailing_level=_decimal_or_none(metadata.get("grid_trailing_level")),
                trailing_active=bool(metadata.get("grid_trailing_active", False)),
            )
            self._set_state(context, state)
            return state

        if entries > state.entries:
            state.entries = entries
            state.last_grid_entry_price = last_grid_entry
            self._set_state(context, state)
        return state

    def _update_dynamic_trail(
        self,
        *,
        state: _GridTrailState,
        latest_close: Decimal,
        activation_pct: Decimal,
        distance_pct: Decimal,
    ) -> None:
        activation = activation_pct / Decimal("100")
        distance = distance_pct / Decimal("100")
        if distance <= 0:
            return

        if state.direction == SignalDirection.LONG:
            activation_price = state.entry_price * (Decimal("1") + activation)
            if latest_close < activation_price:
                return
            state.trailing_active = True
            state.best_close = max(state.best_close or latest_close, latest_close)
            state.trailing_level = state.best_close * (Decimal("1") - distance)
            return

        activation_price = state.entry_price * (Decimal("1") - activation)
        if latest_close > activation_price:
            return
        state.trailing_active = True
        state.best_close = min(state.best_close or latest_close, latest_close)
        state.trailing_level = state.best_close * (Decimal("1") + distance)

    def _dynamic_close_triggered(
        self,
        state: _GridTrailState,
        latest_close: Decimal,
    ) -> bool:
        if not state.trailing_active or state.trailing_level is None:
            return False
        if state.direction == SignalDirection.LONG:
            return latest_close <= state.trailing_level
        return latest_close >= state.trailing_level

    def _scale_in_due(
        self,
        state: _GridTrailState,
        latest_close: Decimal,
        spacing: Decimal,
    ) -> bool:
        if state.entries >= self.max_grid_entries:
            return False
        if spacing <= 0:
            return False
        if state.direction == SignalDirection.LONG:
            return latest_close <= state.last_grid_entry_price - spacing
        return latest_close >= state.last_grid_entry_price + spacing

    def _grid_spacing(self, latest_close: Decimal, band: BollingerBandPoint, atr: Decimal | None) -> Decimal:
        band_spacing = (band.upper - band.lower) / Decimal(max(self.grid_levels, 1))
        minimum_spacing_pct = latest_close * self.min_grid_spacing_pct
        
        # ATR-relative spacing: at least 0.25x ATR
        minimum_spacing_atr = (atr * Decimal("0.25")) if atr is not None and atr > 0 else Decimal("0")
        
        return max(band_spacing, minimum_spacing_pct, minimum_spacing_atr)

    def _disaster_stop(
        self,
        *,
        price: Decimal,
        direction: SignalDirection,
        atr: Decimal | None,
    ) -> Decimal:
        atr_distance = (
            atr * self.stop_atr_multiple
            if atr is not None and atr > 0
            else Decimal("0")
        )
        fallback_distance = price * self.fallback_stop_pct
        distance = max(atr_distance, fallback_distance)
        if direction == SignalDirection.LONG:
            return max(price - distance, Decimal("0.00000001"))
        return price + distance

    def _trail_settings(self, context: StrategyContext) -> tuple[Decimal, Decimal]:
        config = context.features.get("backtest_config")
        if not isinstance(config, dict):
            return self.trail_activation_pct, self.trail_distance_pct
        return (
            _decimal(
                config.get("trailing_stop_activation_pct"),
                self.trail_activation_pct,
            ),
            _decimal(
                config.get("trailing_stop_distance_pct"),
                self.trail_distance_pct,
            ),
        )

    def _base_metadata(
        self,
        band: BollingerBandPoint,
        atr: Decimal | None,
        context: StrategyContext,
    ) -> dict[str, Any]:
        activation_pct, distance_pct = self._trail_settings(context)
        return {
            "strategy_type": "bb_dynamic_grid",
            "bb_middle": band.middle,
            "bb_upper": band.upper,
            "bb_lower": band.lower,
            "bb_width_pct": band.width_pct,
            "atr": atr,
            "grid_levels": self.grid_levels,
            "max_grid_entries": self.max_grid_entries,
            "trail_activation_pct": activation_pct,
            "trail_distance_pct": distance_pct,
        }

    def _entry_confidence(
        self,
        price: Decimal,
        band: BollingerBandPoint,
        direction: SignalDirection,
    ) -> Decimal:
        if price <= 0:
            return Decimal("0.55")
        if direction == SignalDirection.LONG:
            overshoot = max(band.lower - price, Decimal("0")) / price
        else:
            overshoot = max(price - band.upper, Decimal("0")) / price
        return clamp_confidence(Decimal("0.62") + min(overshoot * Decimal("10"), Decimal("0.18")))

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


def _state_key(context: StrategyContext) -> str:
    return f"{context.pair}:{context.interval}"


def _open_position(context: StrategyContext) -> dict[str, Any] | None:
    value = context.features.get("open_position")
    if isinstance(value, dict):
        return value
    return None


def _direction(value: Any) -> SignalDirection | None:
    if value == SignalDirection.LONG:
        return SignalDirection.LONG
    if value == SignalDirection.SHORT:
        return SignalDirection.SHORT
    text = str(value).strip().lower()
    if text == SignalDirection.LONG.value:
        return SignalDirection.LONG
    if text == SignalDirection.SHORT.value:
        return SignalDirection.SHORT
    return None


def _entry_count(metadata: dict[str, Any]) -> int:
    value = metadata.get("grid_entries")
    if value is None:
        value = int(metadata.get("scale_in_count", 0)) + 1
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return 1


def _decimal(value: Any, default: Decimal) -> Decimal:
    if value is None or value == "":
        return default
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return default


def _decimal_or_none(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
