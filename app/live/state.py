from __future__ import annotations

import threading
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class LivePaperState:
    running: bool = False
    execution_mode: str = "paper"
    live_dry_run: bool = True
    pair: str = ""
    watchlist: list[str] = field(default_factory=list)
    scanned_pairs: dict[str, str] = field(default_factory=dict)
    interval: str = ""
    strategy_interval: str = ""
    execution_interval: str = ""
    strategy: str = ""
    candle_count: int = 0
    equity: str = "0"
    starting_equity: str = "0"
    realized_pnl: str = "0"
    unrealized_pnl: str = "0"
    net_realized_pnl: str = "0"
    fees_paid: str = "0"
    open_notional: str = "0"
    return_abs: str = "0"
    return_pct: str = "0"
    max_drawdown_pct: str = "0"
    peak_equity: str = "0"
    open_positions: int = 0
    total_fills: int = 0
    positions_json: str = "[]"
    live_positions_json: str = "[]"
    live_orders_json: str = "[]"
    live_sync_json: str = "{}"
    equity_history_json: str = "[]"
    candles_json: str = "{}"
    last_updated: str = ""
    session_started_at: str = ""
    error: str = ""
    entry_type_counts: dict[str, int] = field(default_factory=dict)
    recent_diagnostics: list[dict[str, Any]] = field(default_factory=list)
    pair_profiles: dict[str, dict[str, Any]] = field(default_factory=dict)
    pair_overrides: dict[str, dict[str, Any]] = field(default_factory=dict)

    initial_equity: str = "0"
    allocated_capital: str = "0"
    portfolio_equity: str = "0"
    wallet_balance: str = "0"
    wallet_locked_collateral: str = "0"
    wallet_free_collateral: str = "0"
    portfolio_unrealized_pnl: str = "0"
    usable_capital: str = "0"
    total_equity: str = "0"
    tradable_equity: str = "0"
    tradable_base: str = "0"
    locked_profit: str = "0"
    unlocked_profit: str = "0"
    max_leveraged_notional: str = "0"
    effective_leverage: str = "1"
    daily_loss_from_tradable_base: str = "0"
    profit_lock_enabled: bool = True
    protected_profit_override_enabled: bool = False
    
    # New Live Safety Fields
    live_kill_switch: bool = False
    live_max_order_notional: str = "5000"
    live_max_margin_per_order: str = "1000"
    live_require_stop_loss: bool = True


_state_lock = threading.Lock()
_live_state = LivePaperState()


def get_live_state() -> dict[str, Any]:
    with _state_lock:
        return asdict(_live_state)


def _update_live_state(**kwargs: Any) -> None:
    with _state_lock:
        for key, value in kwargs.items():
            setattr(_live_state, key, value)


def reset_live_state(**overrides: Any) -> None:
    fresh = asdict(LivePaperState())
    fresh.update(overrides)
    with _state_lock:
        for key, value in fresh.items():
            setattr(_live_state, key, value)
