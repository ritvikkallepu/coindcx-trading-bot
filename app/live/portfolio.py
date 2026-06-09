"""Pure helpers for summarizing the live CoinDCX futures portfolio."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Mapping

from app.risk.models import get_unit_contract_value


@dataclass(frozen=True)
class FuturesPortfolioSnapshot:
    """Account-level futures wallet and open-position values in margin currency."""

    margin_currency: str
    wallet_found: bool
    wallet_balance: Decimal
    locked_collateral: Decimal
    free_collateral: Decimal
    unrealized_pnl: Decimal
    portfolio_equity: Decimal
    open_position_count: int


def build_futures_portfolio_snapshot(
    *,
    wallets: Iterable[Mapping[str, Any]],
    positions: Iterable[Mapping[str, Any]],
    margin_currency: str,
    fallback_quote_to_margin_rate: Decimal,
) -> FuturesPortfolioSnapshot:
    """Build a read-only portfolio summary from CoinDCX wallet and position rows."""

    normalized_currency = margin_currency.strip().upper()
    wallet = next(
        (
            row
            for row in wallets
            if str(row.get("currency_short_name") or "").strip().upper()
            == normalized_currency
        ),
        None,
    )
    # CoinDCX reports `balance` as currently usable collateral. Locked margin is
    # separate and must be added for total wallet value, not subtracted again
    # when determining new-trade capacity.
    usable_balance = _decimal(wallet.get("balance")) if wallet else Decimal("0")
    locked_collateral = (
        sum(
            (
                abs(_decimal(wallet.get(key)))
                for key in (
                    "locked_balance",
                    "cross_order_margin",
                    "cross_user_margin",
                )
            ),
            Decimal("0"),
        )
        if wallet
        else Decimal("0")
    )
    wallet_balance = usable_balance + locked_collateral
    free_collateral = max(Decimal("0"), usable_balance)

    unrealized_pnl = Decimal("0")
    open_position_count = 0
    for position in positions:
        active_pos = _decimal(position.get("active_pos"))
        if active_pos == 0:
            continue
        open_position_count += 1
        entry_price = _decimal(position.get("avg_price"))
        mark_price = _decimal(position.get("mark_price"))
        if entry_price <= 0 or mark_price <= 0:
            continue
        pair = str(position.get("pair") or "")
        conversion = _positive_decimal(position.get("settlement_currency_avg_price"))
        unrealized_pnl += (
            (mark_price - entry_price)
            * active_pos
            * get_unit_contract_value(pair)
            * (conversion or fallback_quote_to_margin_rate)
        )

    return FuturesPortfolioSnapshot(
        margin_currency=normalized_currency,
        wallet_found=wallet is not None,
        wallet_balance=wallet_balance,
        locked_collateral=locked_collateral,
        free_collateral=free_collateral,
        unrealized_pnl=unrealized_pnl,
        portfolio_equity=wallet_balance + unrealized_pnl,
        open_position_count=open_position_count,
    )


def risk_capacity_before_reservations(
    *,
    allocated_capital: Decimal,
    existing_required_margin: Decimal,
    portfolio: FuturesPortfolioSnapshot | None,
) -> Decimal:
    """Return the capital ceiling used before RiskManager reserves open margin."""

    if portfolio is None or not portfolio.wallet_found:
        return max(Decimal("0"), allocated_capital)
    existing_margin = max(Decimal("0"), existing_required_margin)
    new_trade_capacity = min(
        max(Decimal("0"), allocated_capital),
        portfolio.free_collateral,
    )
    # RiskManager subtracts existing margin reservations later. Add them here
    # so only the already-netted CoinDCX usable balance limits the new order.
    return existing_margin + max(Decimal("0"), new_trade_capacity)


def _positive_decimal(value: Any) -> Decimal | None:
    result = _decimal(value)
    return result if result > 0 else None


def _decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value if value not in (None, "") else "0"))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"Invalid portfolio decimal value: {value!r}") from exc
