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
    if context.protected_profit_override_enabled:
        return False
    if context.daily_realized_pnl >= 0:
        return False
    
    # 1. Percentage-based limit
    base_equity = context.daily_loss_limit_equity or context.account_equity
    pct_limit = daily_loss_limit_amount(base_equity, settings)
    if abs(context.daily_realized_pnl) >= pct_limit:
         return True
         
    # 2. Live absolute INR limit
    if context.trading_mode == "live":
        if abs(context.daily_realized_pnl) >= settings.live_max_daily_loss_inr:
            return True
            
    return False


def allowed_leverage(context: RiskContext, settings: RiskSettings) -> Decimal:
    """Return the lower of configured leverage and exchange instrument limit."""

    configured = Decimal(str(settings.max_leverage))
    if not settings.enforce_exchange_leverage_limit:
        return configured

    exchange_limit = exchange_leverage_limit(context)
    if exchange_limit is None:
        return configured
    return min(configured, exchange_limit)


def exchange_leverage_limit(context: RiskContext) -> Decimal | None:
    """Return the positive exchange-reported leverage limit, when available."""

    instrument_limit = getattr(context.instrument, "max_leverage", None)
    if instrument_limit is None:
        return None
    try:
        exchange_limit = Decimal(str(instrument_limit))
    except Exception:
        return None
    if exchange_limit <= 0:
        return None
    return exchange_limit


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
