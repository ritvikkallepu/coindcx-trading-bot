from __future__ import annotations

from decimal import Decimal
from math import sqrt

from app.backtest.models import (
    BacktestConfig,
    BacktestEquityPoint,
    BacktestMetrics,
    BacktestTrade,
)
from app.broker.models import PaperAccountSnapshot


CANDLES_PER_YEAR = {
    "1m": 365 * 24 * 60,
    "5m": 365 * 24 * 12,
    "15m": 365 * 24 * 4,
    "30m": 365 * 24 * 2,
    "30min": 365 * 24 * 2,
    "1h": 365 * 24,
    "2h": 365 * 12,
    "2hr": 365 * 12,
    "4h": 365 * 6,
    "1d": 365,
    "24h": 365,
}


def compute_backtest_metrics(
    *,
    config: BacktestConfig,
    final_account: PaperAccountSnapshot,
    equity_curve: list[BacktestEquityPoint],
    trades: list[BacktestTrade],
) -> BacktestMetrics:
    starting_equity = config.starting_equity
    final_equity = final_account.equity
    total_return = final_equity - starting_equity
    total_return_pct = _pct(total_return, starting_equity)
    net_pnl = (
        final_account.realized_pnl
        + final_account.unrealized_pnl
        - final_account.fees_paid
        - final_account.funding_paid
    )

    wins = [trade.net_pnl for trade in trades if trade.net_pnl > 0]
    losses = [trade.net_pnl for trade in trades if trade.net_pnl < 0]
    gross_profit = sum(wins, Decimal("0"))
    gross_loss = abs(sum(losses, Decimal("0")))

    trade_count = len(trades)
    win_rate_pct = (
        (Decimal(len(wins)) / Decimal(trade_count)) * Decimal("100")
        if trade_count
        else None
    )
    average_win = gross_profit / Decimal(len(wins)) if wins else None
    average_loss = sum(losses, Decimal("0")) / Decimal(len(losses)) if losses else None
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else None
    max_drawdown, max_drawdown_pct = _max_drawdown(equity_curve)

    return BacktestMetrics(
        starting_equity=starting_equity,
        final_equity=final_equity,
        total_return=total_return,
        total_return_pct=total_return_pct,
        realized_pnl=final_account.realized_pnl,
        unrealized_pnl=final_account.unrealized_pnl,
        fees_paid=final_account.fees_paid,
        net_pnl=net_pnl,
        trade_count=trade_count,
        winning_trades=len(wins),
        losing_trades=len(losses),
        win_rate_pct=win_rate_pct,
        average_win=average_win,
        average_loss=average_loss,
        profit_factor=profit_factor,
        max_drawdown=max_drawdown,
        max_drawdown_pct=max_drawdown_pct,
        sharpe_ratio=_sharpe_ratio(equity_curve, config.interval),
        funding_paid=final_account.funding_paid,
    )


def _pct(value: Decimal, base: Decimal) -> Decimal:
    if base == 0:
        return Decimal("0")
    return (value / base) * Decimal("100")


def _max_drawdown(
    equity_curve: list[BacktestEquityPoint],
) -> tuple[Decimal, Decimal]:
    peak: Decimal | None = None
    max_drawdown = Decimal("0")
    max_drawdown_pct = Decimal("0")

    for point in equity_curve:
        if peak is None or point.equity > peak:
            peak = point.equity
        if peak is None or peak <= 0:
            continue
        drawdown = peak - point.equity
        drawdown_pct = (drawdown / peak) * Decimal("100")
        if drawdown > max_drawdown:
            max_drawdown = drawdown
            max_drawdown_pct = drawdown_pct

    return max_drawdown, max_drawdown_pct


def _sharpe_ratio(
    equity_curve: list[BacktestEquityPoint],
    interval: str,
) -> Decimal | None:
    returns: list[float] = []
    previous: Decimal | None = None
    for point in equity_curve:
        if previous is not None and previous > 0:
            returns.append(float((point.equity - previous) / previous))
        previous = point.equity

    if len(returns) < 2:
        return None

    average = sum(returns) / len(returns)
    variance = sum((value - average) ** 2 for value in returns) / (len(returns) - 1)
    stdev = sqrt(variance)
    if stdev == 0:
        return None
    candles_per_year = CANDLES_PER_YEAR.get(interval)
    annualizer = sqrt(candles_per_year) if candles_per_year else sqrt(len(returns))
    return Decimal(str((average / stdev) * annualizer))
