from __future__ import annotations

import logging
from dataclasses import replace
from decimal import Decimal

from app.config import RiskSettings
from app.risk.entry_safety import assess_entry_safety
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

        # Multi-position checks
        if isinstance(context.open_positions, int):
            total_open = max(0, context.open_positions)
            pair_open = 0 # Assume different pair if only count is provided
        else:
            open_positions = context.open_positions
            total_open = len([p for p in open_positions if p.is_open])
            pair_positions = [p for p in open_positions if p.is_open and p.pair == signal.pair]
            pair_open = len(pair_positions)

        # 1. Per-pair limit (Check FIRST for specific reason)
        if pair_open > 0:
            is_scale_in = _is_approved_scale_in(context)
            if not self.settings.allow_same_pair_pyramiding and not is_scale_in:
                return self._reject(
                    signal,
                    f"same_pair_position_blocked: {signal.pair} already has an open position"
                )

            if pair_open >= self.settings.max_open_positions_per_pair and not is_scale_in:
                return self._reject(
                    signal,
                    f"max_open_positions_per_pair_blocked: {pair_open}/{self.settings.max_open_positions_per_pair} for {signal.pair}"
                )

        # 2. Global limit
        if total_open >= self.settings.max_open_positions and pair_open == 0:
            return self._reject(
                signal,
                f"max_open_positions_blocked: {total_open}/{self.settings.max_open_positions}",
            )

        # 3. Multi-pair check
        if total_open > 0 and pair_open == 0 and not self.settings.allow_multi_pair_positions:
            return self._reject(
                signal,
                "multi_pair_positions_blocked: only one pair allowed at a time"
            )

        try:
            signal = normalize_signal_prices_to_tick(signal, context.instrument)
            context = RiskContext(
                signal=signal,
                account_equity=context.account_equity,
                available_equity=available_equity,
                risk_base_mode=context.risk_base_mode,
                open_positions=context.open_positions,
                daily_realized_pnl=context.daily_realized_pnl,
                daily_loss_limit_equity=context.daily_loss_limit_equity,
                instrument=context.instrument,
                requested_leverage=context.requested_leverage,
                quote_to_margin_rate=context.quote_to_margin_rate,
                unit_contract_value=context.unit_contract_value,
                trading_mode=context.trading_mode,
                live_trading_enabled=context.live_trading_enabled,
            )
        except Exception as exc:
            return self._reject(signal, f"Normalization error: {exc}")

        validation_error = validate_entry_signal(signal)
        if validation_error:
            return self._reject(signal, validation_error)

        safety = assess_entry_safety(signal, self.settings)
        if safety.metadata:
            signal = replace(
                signal,
                metadata={**signal.metadata, **safety.metadata},
            )
            context = replace(context, signal=signal)
        if not safety.approved:
            return self._reject(signal, safety.reason or "Entry safety rejected signal.")

        configured_allowed_leverage = allowed_leverage(context, self.settings)
        requested_leverage = context.requested_leverage or Decimal("1")
        if requested_leverage > configured_allowed_leverage:
            return self._reject(
                signal,
                f"Requested leverage exceeds allowed limit: {requested_leverage} > {configured_allowed_leverage}.",
            )

        basket_metadata: dict[str, object] = {}
        try:
            basket_metadata = _validate_risk_basket(
                context=context,
                settings=self.settings,
            )
        except ValueError as exc:
            return self._reject(signal, str(exc))

        try:
            signal_risk_multiplier = _risk_multiplier(
                signal.metadata.get("risk_multiplier", Decimal("1"))
            )
            basket_risk_multiplier = _risk_multiplier(
                basket_metadata.get("basket_risk_multiplier", Decimal("1"))
            )
        except ValueError as exc:
            return self._reject(signal, str(exc))

        effective_risk_multiplier = min(
            signal_risk_multiplier,
            basket_risk_multiplier,
            Decimal("1"),
        )

        sizing = _calculate_position_size(
            context=context,
            settings=self.settings,
            account_equity=context.account_equity,
            leverage=requested_leverage,
            risk_multiplier=effective_risk_multiplier,
        )
        if sizing.position_size <= 0 or sizing.notional <= 0:
            return self._reject(signal, "Calculated position size is zero.")

        margin_metadata: dict[str, object] = {}
        try:
            margin_metadata = _validate_margin_sufficiency(
                context=context,
                sizing=sizing,
                available_equity=available_equity,
                leverage=requested_leverage,
                max_margin_usage_pct=self.settings.max_margin_usage_pct,
                max_margin_per_trade_pct=self.settings.max_margin_per_trade_pct,
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

        # Check total portfolio risk (Planned risk amount sum)
        total_risk_metadata: dict[str, object] = {}
        try:
            total_risk_metadata = _validate_projected_risk(
                context=context,
                settings=self.settings,
                sizing=sizing,
            )
        except ValueError as exc:
            return self._reject(signal, str(exc))

        liquidation_error, liquidation_metadata = liquidation_guard(
            signal=signal,
            leverage=requested_leverage,
            buffer_pct=self.settings.liquidation_buffer_pct,
        )
        if liquidation_error:
            return self._reject(signal, liquidation_error)

        # Mark if we adjusted for basket risk
        is_adjusted = (pair_open > 0)

        metadata = {
            "risk_budget": sizing.risk_budget,
            "max_risk_per_trade_pct": self.settings.max_risk_per_trade_pct,
            "effective_risk_per_trade_pct": (
                (sizing.max_loss / context.account_equity * Decimal("100"))
                if context.account_equity > 0
                else Decimal("0")
            ),
            "risk_percent_used": (
                (sizing.max_loss / context.account_equity * Decimal("100"))
                if context.account_equity > 0
                else Decimal("0")
            ),
            "risk_multiplier": effective_risk_multiplier,
            "risk_multiplier_applies": (
                effective_risk_multiplier < Decimal("1")
                or bool(signal.metadata.get("risk_multiplier_applies"))
            ),
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
            "basket_risk_checked": True,
            "position_size_adjusted_for_basket_risk": is_adjusted,
            "quote_to_margin_rate": context.quote_to_margin_rate,
            "unit_contract_value": context.unit_contract_value,
            "notional_currency": (
                context.instrument.margin_currency if context.instrument else "account"
            ),
            "price_quote_currency": (
                context.instrument.quote_currency if context.instrument else "quote"
            ),
            **liquidation_metadata,
            **margin_metadata,
            **exposure_metadata,
            **basket_metadata,
            **total_risk_metadata,
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
        quote_to_margin_rate: Decimal = Decimal("1"),
        unit_contract_value: Decimal = Decimal("1"),
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
                quote_to_margin_rate=quote_to_margin_rate,
                unit_contract_value=unit_contract_value,
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
        if instrument.min_notional is not None:
            min_notional = instrument.min_notional * context.quote_to_margin_rate
            if notional >= min_notional:
                return None
            return (
                "Calculated notional is below instrument minimum notional: "
                f"{notional} < {min_notional}."
            )
        return None

    def _reject(self, signal: StrategySignal, reason: str) -> RiskDecision:
        return RiskDecision(
            approved=False,
            reason=reason,
            signal=signal,
        )


def _calculate_position_size(
    *,
    context: RiskContext,
    settings: RiskSettings,
    account_equity: Decimal,
    leverage: Decimal,
    risk_multiplier: Decimal,
) -> PositionSizingResult:
    max_risk_pct = settings.max_risk_per_trade_pct * risk_multiplier

    instrument_risk_budget = account_equity * (max_risk_pct / Decimal("100"))

    existing_pair_risk = Decimal("0")
    if not isinstance(context.open_positions, int):
        for p in context.open_positions:
            if p.is_open and p.pair == context.signal.pair:
                if p.stop_loss is not None:
                    existing_pair_risk += p.loss_at_stop or Decimal("0")
                else:
                    existing_pair_risk += p.notional

    remaining_budget = max(Decimal("0"), instrument_risk_budget - existing_pair_risk)

    # If we are scaling in, the effective max_risk_pct for this specific entry is lower.
    effective_max_risk_pct = (remaining_budget / account_equity * Decimal("100")) if account_equity > 0 else Decimal("0")

    result = fixed_fraction_position_size(
        account_equity=account_equity,
        max_risk_per_trade_pct=effective_max_risk_pct,
        signal=context.signal,
        leverage=leverage,
        instrument=context.instrument,
        quote_to_margin_rate=context.quote_to_margin_rate,
        unit_contract_value=context.unit_contract_value,
    )

    # Mark if we adjusted for basket risk
    if existing_pair_risk > 0:
        # result is frozen, but we can't easily add metadata to it.
        # But evaluate() will use the result.risk_budget.
        pass

    return result


def _validate_risk_basket(
    *,
    context: RiskContext,
    settings: RiskSettings,
) -> dict[str, object]:
    # Placeholder for future correlated risk basket logic
    return {
        "basket_risk_checked": True,
        "basket_risk_multiplier": Decimal("1"),
    }


def _risk_multiplier(value: object) -> Decimal:
    if isinstance(value, Decimal):
        multiplier = value
    else:
        try:
            multiplier = Decimal(str(value))
        except Exception as exc:
            raise ValueError("Risk multiplier must be numeric.") from exc
    if multiplier <= 0:
        raise ValueError("Risk multiplier must be positive.")
    return min(multiplier, Decimal("1"))


def _validate_margin_sufficiency(
    *,
    context: RiskContext,
    sizing: PositionSizingResult,
    available_equity: Decimal,
    leverage: Decimal,
    max_margin_usage_pct: Decimal = Decimal("100"),
    max_margin_per_trade_pct: Decimal = Decimal("0"),
) -> dict[str, object]:
    if leverage <= 0:
        raise ValueError("Requested leverage must be positive.")

    existing_margin = _existing_required_margin(context.open_positions)
    required_margin = sizing.notional / leverage
    total_projected_margin = existing_margin + required_margin

    # Portfolio-wide margin cap
    max_margin_allowed = available_equity * (max_margin_usage_pct / Decimal("100"))
    per_trade_margin_limit = (
        available_equity * (max_margin_per_trade_pct / Decimal("100"))
        if max_margin_per_trade_pct > 0
        else Decimal("0")
    )

    if total_projected_margin > max_margin_allowed:
        raise ValueError(
            f"margin_usage_blocked: projected total margin {total_projected_margin:.2f} "
            f"exceeds allowed {max_margin_allowed:.2f} ({max_margin_usage_pct}%) - exceeds available equity."
        )

    if per_trade_margin_limit > 0 and required_margin > per_trade_margin_limit:
        raise ValueError(
            "per_trade_margin_blocked: required margin "
            f"{required_margin:.2f} exceeds per-trade limit "
            f"{per_trade_margin_limit:.2f} ({max_margin_per_trade_pct}%)."
        )

    free_equity = available_equity - existing_margin
    if free_equity <= 0:
        raise ValueError(
            "Insufficient available equity after existing margin reservations."
        )

    if sizing.risk_budget > free_equity:
        raise ValueError(
            "Planned risk exceeds available equity after existing margin reservations: "
            f"{sizing.risk_budget} > {free_equity}."
        )

    if required_margin > free_equity:
        raise ValueError(
            "Required margin exceeds available equity: "
            f"{required_margin} > {free_equity}."
        )

    return {
        "projected_margin": required_margin,
        "existing_margin": existing_margin,
        "available_equity_after_margin_reservations": free_equity,
        "max_margin_per_trade_pct": max_margin_per_trade_pct,
        "per_trade_margin_limit": per_trade_margin_limit,
    }


def _validate_total_exposure(
    *,
    context: RiskContext,
    settings: RiskSettings,
    sizing: PositionSizingResult,
) -> dict[str, object]:
    if settings.max_total_open_notional_pct <= 0:
        return {}

    existing_notional = _existing_open_notional(context.open_positions)
    total_notional = existing_notional + sizing.notional
    limit = context.account_equity * (
        settings.max_total_open_notional_pct / Decimal("100")
    )

    if total_notional > limit:
        raise ValueError(
            f"Total open notional exceeds limit: {total_notional} > {limit}."
        )

    return {"total_projected_notional": total_notional}


def _validate_projected_risk(
    *,
    context: RiskContext,
    settings: RiskSettings,
    sizing: PositionSizingResult,
) -> dict[str, object]:
    if settings.max_total_risk_pct <= 0:
        return {}

    existing_risk = _existing_open_risk(context.open_positions)
    total_risk = existing_risk + sizing.max_loss
    limit = context.account_equity * (
        settings.max_total_risk_pct / Decimal("100")
    )

    if total_risk > limit:
        raise ValueError(
            f"Projected total risk {total_risk:.2f} exceeds limit {limit:.2f} "
            f"({settings.max_total_risk_pct}%)."
        )

    return {"total_projected_risk": total_risk}


def _existing_required_margin(open_positions: OpenPositions) -> Decimal:
    if isinstance(open_positions, int):
        return Decimal("0")
    return sum(
        (p.notional / p.leverage) for p in open_positions if p.is_open
    )


def _existing_open_notional(open_positions: OpenPositions) -> Decimal:
    if isinstance(open_positions, int):
        return Decimal("0")
    return sum((p.notional) for p in open_positions if p.is_open)


def _existing_open_risk(open_positions: OpenPositions) -> Decimal:
    if isinstance(open_positions, int):
        return Decimal("0")

    total = Decimal("0")
    for p in open_positions:
        if not p.is_open: continue
        if p.stop_loss is not None:
            total += p.loss_at_stop or Decimal("0")
        else:
            total += p.notional
    return total


def _is_approved_scale_in(context: RiskContext) -> bool:
    if not isinstance(context.open_positions, (list, tuple)):
        return False

    signal = context.signal
    if not signal.metadata.get("allow_scale_in"):
        return False

    position = next((p for p in context.open_positions if p.pair == signal.pair and p.is_open), None)
    if position is None:
        return False

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
