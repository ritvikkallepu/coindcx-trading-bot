from __future__ import annotations

from decimal import Decimal

from app.config import RiskSettings
from app.risk.models import RiskContext
from app.strategies.base import SignalAction, SignalDirection, StrategySignal


ENTRY_ACTIONS = {SignalAction.ENTER_LONG, SignalAction.ENTER_SHORT}
EXIT_ACTIONS = {SignalAction.EXIT_LONG, SignalAction.EXIT_SHORT}


def is_entry_signal(signal: StrategySignal) -> bool:
    return signal.action in ENTRY_ACTIONS


def is_exit_signal(signal: StrategySignal) -> bool:
    return signal.action in EXIT_ACTIONS


def daily_loss_limit_amount(
    account_equity: Decimal,
    settings: RiskSettings,
) -> Decimal:
    return account_equity * (settings.max_daily_loss_pct / Decimal("100"))


def daily_loss_limit_reached(context: RiskContext, settings: RiskSettings) -> bool:
    if context.daily_realized_pnl >= 0:
        return False
    base_equity = context.daily_loss_limit_equity or context.account_equity
    return abs(context.daily_realized_pnl) >= daily_loss_limit_amount(base_equity, settings)


def allowed_leverage(context: RiskContext, settings: RiskSettings) -> Decimal:
    configured = Decimal(str(settings.max_leverage))
    if context.instrument is None or context.instrument.max_leverage is None:
        return configured
    return min(configured, context.instrument.max_leverage)


def validate_entry_signal(signal: StrategySignal) -> str | None:
    if signal.action == SignalAction.ENTER_LONG and signal.direction != SignalDirection.LONG:
        return "Long entry signal must have long direction."
    if signal.action == SignalAction.ENTER_SHORT and signal.direction != SignalDirection.SHORT:
        return "Short entry signal must have short direction."
    if signal.entry_price is None or signal.entry_price <= 0:
        return "Entry signal requires a positive entry price."
    if signal.stop_loss is None or signal.stop_loss <= 0:
        return "Entry signal requires a positive stop loss."

    if signal.direction == SignalDirection.LONG and signal.stop_loss >= signal.entry_price:
        return "Long entry stop loss must be below entry price."
    if signal.direction == SignalDirection.SHORT and signal.stop_loss <= signal.entry_price:
        return "Short entry stop loss must be above entry price."

    if signal.take_profit is not None:
        if signal.take_profit <= 0:
            return "Take profit must be positive when provided."
        if signal.direction == SignalDirection.LONG and signal.take_profit <= signal.entry_price:
            return "Long entry take profit must be above entry price."
        if signal.direction == SignalDirection.SHORT and signal.take_profit >= signal.entry_price:
            return "Short entry take profit must be below entry price."

    return None
