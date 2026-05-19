from __future__ import annotations

import logging
from decimal import Decimal

from app.config import RiskSettings
from app.risk.exchange_rules import liquidation_guard, normalize_signal_prices_to_tick
from app.risk.limits import (
    allowed_leverage,
    daily_loss_limit_amount,
    daily_loss_limit_reached,
    is_entry_signal,
    is_exit_signal,
    validate_entry_signal,
)
from app.risk.models import (
    InstrumentMetadata,
    OpenPosition,
    OpenPositions,
    RiskContext,
    RiskDecision,
)
from app.risk.position_sizing import (
    PositionSizingResult,
    fixed_fraction_position_size,
    round_down_to_step,
)
from app.strategies.base import StrategySignal


class RiskManager:
    def __init__(
        self,
        settings: RiskSettings | None = None,
        *,
        logger: logging.Logger | None = None,
    ) -> None:
        self.settings = settings or RiskSettings()
        self.logger = logger or logging.getLogger(__name__)

    def evaluate(self, context: RiskContext) -> RiskDecision:
        signal = context.signal
        available_equity = (
            context.available_equity
            if context.available_equity is not None
            else context.account_equity
        )

        if context.live_trading_allowed:
            return self._reject(
                signal,
                "Risk approval is paper-only; live execution is not enabled in this bot yet.",
            )

        if is_exit_signal(signal):
            return RiskDecision(
                approved=True,
                reason="Exit signal approved; no new position risk is added.",
                signal=signal,
            )

        if not is_entry_signal(signal):
            return self._reject(signal, "Signal does not request a risked entry.")

        if context.account_equity <= 0:
            return self._reject(signal, "Account equity must be positive.")
        if available_equity <= 0:
            return self._reject(signal, "Available equity must be positive; account may be depleted.")

        if daily_loss_limit_reached(context, self.settings):
            limit = daily_loss_limit_amount(
                context.daily_loss_limit_equity or context.account_equity,
                self.settings,
            )
            return self._reject(
                signal,
                f"Max daily loss reached: {context.daily_realized_pnl} <= -{limit}.",
            )

        if (
            context.open_position_count >= self.settings.max_open_positions
            and not _is_approved_scale_in(context)
        ):
            return self._reject(
                signal,
                (
                    "Max open positions reached: "
                    f"{context.open_position_count}/{self.settings.max_open_positions}."
                ),
            )

        try:
            signal = normalize_signal_prices_to_tick(signal, context.instrument)
            context = RiskContext(
                signal=signal,
                account_equity=context.account_equity,
                available_equity=context.available_equity,
                risk_base_mode=context.risk_base_mode,
                open_positions=context.open_positions,
                daily_realized_pnl=context.daily_realized_pnl,
                daily_loss_limit_equity=context.daily_loss_limit_equity,
                instrument=context.instrument,
                requested_leverage=context.requested_leverage,
                trading_mode=context.trading_mode,
                live_trading_enabled=context.live_trading_enabled,
            )
        except ValueError as exc:
            return self._reject(signal, str(exc))

        signal_error = validate_entry_signal(signal)
        if signal_error is not None:
            return self._reject(signal, signal_error)

        configured_allowed_leverage = allowed_leverage(context, self.settings)
        requested_leverage = context.requested_leverage or Decimal("1")
        if requested_leverage <= 0:
            return self._reject(signal, "Requested leverage must be positive.")
        if requested_leverage > configured_allowed_leverage:
            return self._reject(
                signal,
                (
                    "Requested leverage exceeds the configured or instrument limit: "
                    f"{requested_leverage} > {configured_allowed_leverage}."
                ),
            )

        liquidation_error, liquidation_metadata = liquidation_guard(
            signal=signal,
            leverage=requested_leverage,
            buffer_pct=self.settings.liquidation_buffer_pct,
        )
        if liquidation_error is not None:
            return self._reject(signal, liquidation_error)

        multiplier_applies = bool(signal.metadata.get("risk_multiplier_applies")) or bool(
            signal.metadata.get("atr_dynamic_exits_enabled")
        )
        risk_multiplier = (
            _risk_multiplier(signal.metadata.get("risk_multiplier"))
            if multiplier_applies
            else Decimal("1")
        )
        if risk_multiplier <= 0:
            return self._reject(signal, "Risk multiplier must be positive.")
        effective_risk_per_trade_pct = (
            self.settings.max_risk_per_trade_pct * min(risk_multiplier, Decimal("1"))
        )

        try:
            sizing = fixed_fraction_position_size(
                account_equity=context.account_equity,
                max_risk_per_trade_pct=effective_risk_per_trade_pct,
                signal=signal,
                leverage=requested_leverage,
                instrument=context.instrument,
            )
        except ValueError as exc:
            return self._reject(signal, str(exc))

        if sizing.position_size <= 0 or sizing.notional <= 0:
            return self._reject(signal, "Calculated position size is zero.")

        basket_metadata: dict[str, object] = {}
        try:
            sizing, basket_metadata = _cap_scale_in_to_basket_risk(
                context=context,
                settings=self.settings,
                sizing=sizing,
            )
        except ValueError as exc:
            return self._reject(signal, str(exc))

        if sizing.position_size <= 0 or sizing.notional <= 0:
            return self._reject(signal, "Calculated position size is zero after basket risk cap.")

        margin_metadata: dict[str, object] = {}
        try:
            margin_metadata = _validate_margin_sufficiency(
                context=context,
                sizing=sizing,
                available_equity=available_equity,
                leverage=requested_leverage,
            )
        except ValueError as exc:
            return self._reject(signal, str(exc))

        instrument_error = self._validate_instrument_minimums(
            context=context,
            position_size=sizing.position_size,
            notional=sizing.notional,
        )
        if instrument_error is not None:
            return self._reject(signal, instrument_error)

        exposure_metadata: dict[str, object] = {}
        try:
            exposure_metadata = _validate_total_exposure(
                context=context,
                settings=self.settings,
                sizing=sizing,
            )
        except ValueError as exc:
            return self._reject(signal, str(exc))

        metadata = {
            "risk_budget": sizing.risk_budget,
            "max_risk_per_trade_pct": self.settings.max_risk_per_trade_pct,
            "effective_risk_per_trade_pct": effective_risk_per_trade_pct,
            "risk_percent_used": effective_risk_per_trade_pct,
            "risk_multiplier": min(risk_multiplier, Decimal("1")),
            "risk_multiplier_applies": multiplier_applies,
            "risk_base_mode": context.risk_base_mode,
            "risk_base_amount": context.account_equity,
            "planned_risk_amount": sizing.risk_budget,
            "equity_before_trade": available_equity,
            "max_daily_loss_pct": self.settings.max_daily_loss_pct,
            "max_open_positions": self.settings.max_open_positions,
            "max_total_open_notional_pct": self.settings.max_total_open_notional_pct,
            "max_total_risk_pct": self.settings.max_total_risk_pct,
            "configured_max_leverage": self.settings.max_leverage,
            "allowed_leverage": configured_allowed_leverage,
            "capped_by_leverage": sizing.capped_by_leverage,
            "paper_only": True,
            **liquidation_metadata,
            **margin_metadata,
            **exposure_metadata,
            **basket_metadata,
        }

        return RiskDecision(
            approved=True,
            reason="Entry approved by paper risk rules.",
            signal=signal,
            position_size=sizing.position_size,
            notional=sizing.notional,
            leverage=requested_leverage,
            max_loss=sizing.max_loss,
            metadata=metadata,
        )

    def evaluate_signal(
        self,
        signal: StrategySignal,
        *,
        account_equity: Decimal,
        available_equity: Decimal | None = None,
        risk_base_mode: str = "current",
        open_positions: OpenPositions = (),
        daily_realized_pnl: Decimal = Decimal("0"),
        daily_loss_limit_equity: Decimal | None = None,
        instrument: InstrumentMetadata | None = None,
        requested_leverage: Decimal | None = None,
        trading_mode: str = "paper",
        live_trading_enabled: bool = False,
    ) -> RiskDecision:
        return self.evaluate(
            RiskContext(
                signal=signal,
                account_equity=account_equity,
                available_equity=available_equity,
                risk_base_mode=risk_base_mode,
                open_positions=open_positions,
                daily_realized_pnl=daily_realized_pnl,
                daily_loss_limit_equity=daily_loss_limit_equity,
                instrument=instrument,
                requested_leverage=requested_leverage,
                trading_mode=trading_mode,
                live_trading_enabled=live_trading_enabled,
            )
        )

    def _validate_instrument_minimums(
        self,
        *,
        context: RiskContext,
        position_size: Decimal,
        notional: Decimal,
    ) -> str | None:
        instrument = context.instrument
        if instrument is None:
            return None
        if instrument.min_quantity is not None and position_size < instrument.min_quantity:
            return (
                "Calculated position size is below instrument minimum quantity: "
                f"{position_size} < {instrument.min_quantity}."
            )
        if instrument.min_notional is not None and notional < instrument.min_notional:
            return (
                "Calculated notional is below instrument minimum notional: "
                f"{notional} < {instrument.min_notional}."
            )
        return None

    def _reject(self, signal: StrategySignal, reason: str) -> RiskDecision:
        self.logger.info("Risk rejected %s: %s", signal.action.value, reason)
        return RiskDecision(approved=False, reason=reason, signal=signal)


def _is_approved_scale_in(context: RiskContext) -> bool:
    signal = context.signal
    if not signal.metadata.get("allow_scale_in"):
        return False
    if isinstance(context.open_positions, int):
        return False
    if signal.direction is None:
        return False
    return _matching_scale_position(context) is not None


def _risk_multiplier(value: object) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if value is None:
        return Decimal("1")
    try:
        return Decimal(str(value))
    except Exception:
        return Decimal("1")


def _matching_scale_position(context: RiskContext) -> OpenPosition | None:
    if isinstance(context.open_positions, int):
        return None
    return next(
        (
            position
            for position in context.open_positions
            if _same_position(position, context.signal)
        ),
        None,
    )


def _cap_scale_in_to_basket_risk(
    *,
    context: RiskContext,
    settings: RiskSettings,
    sizing: PositionSizingResult,
) -> tuple[PositionSizingResult, dict[str, object]]:
    position = _matching_scale_position(context)
    if position is None:
        return sizing, {}

    signal = context.signal
    if signal.entry_price is None or signal.stop_loss is None:
        return sizing, {}

    stop_distance = abs(signal.entry_price - signal.stop_loss)
    if stop_distance <= 0:
        return sizing, {}

    risk_budget = context.account_equity * (
        settings.max_risk_per_trade_pct / Decimal("100")
    )
    existing_loss_at_stop = abs(position.entry_price - signal.stop_loss) * abs(position.quantity)
    remaining_risk = risk_budget - existing_loss_at_stop
    if remaining_risk <= 0:
        raise ValueError(
            "Scale-in exceeds basket risk limit: existing position already uses "
            f"{existing_loss_at_stop} of {risk_budget} risk budget."
        )

    max_additional_quantity = remaining_risk / stop_distance
    adjusted_quantity = min(sizing.position_size, max_additional_quantity)
    adjusted_for_basket = adjusted_quantity < sizing.position_size
    if context.instrument is not None and context.instrument.quantity_step is not None:
        adjusted_quantity = round_down_to_step(
            adjusted_quantity,
            context.instrument.quantity_step,
        )

    if adjusted_quantity <= 0:
        raise ValueError(
            "Scale-in exceeds basket risk limit after instrument quantity rounding."
        )

    new_loss_at_stop = adjusted_quantity * stop_distance
    basket_loss_at_stop = existing_loss_at_stop + new_loss_at_stop
    if basket_loss_at_stop > risk_budget:
        raise ValueError(
            "Scale-in exceeds basket risk limit: "
            f"{basket_loss_at_stop} > {risk_budget}."
        )

    adjusted_sizing = PositionSizingResult(
        position_size=adjusted_quantity,
        notional=adjusted_quantity * signal.entry_price,
        max_loss=basket_loss_at_stop,
        risk_budget=sizing.risk_budget,
        capped_by_leverage=sizing.capped_by_leverage or adjusted_for_basket,
    )
    return adjusted_sizing, {
        "basket_risk_checked": True,
        "basket_existing_loss_at_stop": existing_loss_at_stop,
        "basket_new_loss_at_stop": new_loss_at_stop,
        "basket_loss_at_stop": basket_loss_at_stop,
        "basket_risk_budget": risk_budget,
        "position_size_adjusted_for_basket_risk": adjusted_for_basket,
    }


def _validate_total_exposure(
    *,
    context: RiskContext,
    settings: RiskSettings,
    sizing: PositionSizingResult,
) -> dict[str, object]:
    if isinstance(context.open_positions, int):
        return {}

    metadata: dict[str, object] = {}
    existing_notional = sum(position.notional for position in context.open_positions if position.is_open)
    projected_notional = existing_notional + sizing.notional
    metadata["existing_open_notional"] = existing_notional
    metadata["projected_open_notional"] = projected_notional

    if settings.max_total_open_notional_pct > 0:
        notional_limit = context.account_equity * (
            settings.max_total_open_notional_pct / Decimal("100")
        )
        metadata["max_total_open_notional"] = notional_limit
        if projected_notional > notional_limit:
            raise ValueError(
                "Projected open notional exceeds total exposure limit: "
                f"{projected_notional} > {notional_limit}."
            )

    if settings.max_total_risk_pct <= 0:
        return metadata

    projected_risk = _projected_total_risk_at_stop(context=context, sizing=sizing)
    total_risk_limit = context.account_equity * (
        settings.max_total_risk_pct / Decimal("100")
    )
    metadata["projected_total_risk_at_stop"] = projected_risk
    metadata["max_total_risk_amount"] = total_risk_limit
    if projected_risk > total_risk_limit:
        raise ValueError(
            "Projected total risk at stop exceeds exposure limit: "
            f"{projected_risk} > {total_risk_limit}."
        )
    return metadata


def _projected_total_risk_at_stop(
    *,
    context: RiskContext,
    sizing: PositionSizingResult,
) -> Decimal:
    if isinstance(context.open_positions, int):
        return sizing.max_loss

    total = Decimal("0")
    matching_position = _matching_scale_position(context)
    for position in context.open_positions:
        if not position.is_open:
            continue
        if matching_position is not None and position is matching_position:
            continue
        loss_at_stop = position.loss_at_stop
        if loss_at_stop is None:
            raise ValueError(
                "Cannot evaluate total risk exposure because an open position "
                f"for {position.pair} is missing a stop loss."
            )
        total += loss_at_stop
    return total + sizing.max_loss


def _validate_margin_sufficiency(
    *,
    context: RiskContext,
    sizing: PositionSizingResult,
    available_equity: Decimal,
    leverage: Decimal,
) -> dict[str, object]:
    if leverage <= 0:
        raise ValueError("Requested leverage must be positive.")

    existing_margin = _existing_required_margin(context.open_positions)
    free_equity = available_equity - existing_margin
    if free_equity <= 0:
        raise ValueError(
            "Insufficient available equity after existing margin reservations."
        )

    required_margin = sizing.notional / leverage
    if required_margin > free_equity:
        raise ValueError(
            "Required margin exceeds available equity: "
            f"{required_margin} > {free_equity}."
        )
    if sizing.risk_budget > available_equity:
        raise ValueError(
            "Planned risk exceeds available equity: "
            f"{sizing.risk_budget} > {available_equity}."
        )

    max_notional = available_equity * leverage
    if sizing.notional > max_notional:
        raise ValueError(
            "Position notional exceeds equity times leverage: "
            f"{sizing.notional} > {max_notional}."
        )

    return {
        "available_equity": available_equity,
        "available_margin": free_equity,
        "existing_required_margin": existing_margin,
        "required_margin": required_margin,
        "margin_ok": True,
        "account_blown": False,
        "position_quantity": sizing.position_size,
        "position_notional": sizing.notional,
        "max_position_notional": max_notional,
    }


def _existing_required_margin(open_positions: OpenPositions) -> Decimal:
    if isinstance(open_positions, int):
        return Decimal("0")
    total = Decimal("0")
    for position in open_positions:
        if not position.is_open:
            continue
        leverage = position.leverage if position.leverage > 0 else Decimal("1")
        total += position.notional / leverage
    return total


def _same_position(position: OpenPosition, signal: StrategySignal) -> bool:
    direction = (
        position.direction.value
        if hasattr(position.direction, "value")
        else str(position.direction)
    )
    signal_direction = signal.direction.value if signal.direction is not None else ""
    return (
        position.is_open
        and position.pair == signal.pair
        and direction == signal_direction
    )
