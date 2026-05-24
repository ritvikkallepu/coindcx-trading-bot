from __future__ import annotations

import logging
import threading
import json
import time
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from app.config import Settings
from app.data.candle_builder import CandleSeries, OHLCVCandle, interval_to_ms
from app.data.gap_guard import CandleGapGuard
from app.data.indicators import latest_indicator_snapshot
from app.data.pipeline import MarketDataPipeline
from app.exchange.coindcx_rest import CoinDCXFuturesClient
from app.exchange.coindcx_ws import CoinDCXFuturesWebSocketClient, MarketSubscription
from app.exchange.coindcx_channels import futures_candle_channel, futures_orderbook_channel
from app.persistence.paper_state import PaperStateStore, PaperSessionStore
from app.broker.paper import PaperBroker
from app.broker.models import PaperFill, PaperOrderSide, PaperPosition, PaperExecutionReport
from app.risk.manager import RiskManager
from app.risk.models import OpenPosition, RiskDecision
from app.risk.limits import is_entry_signal, is_exit_signal
from app.risk.pair_performance import pair_recent_risk_profile
from app.strategies.base import StrategyEngine, StrategyContext, SignalAction, SignalDirection
from app.strategies.defaults import strategy_engine_for_name
from app.utils.json import to_jsonable
from app.live.summary_logger import PaperTradingSummaryLogger


@dataclass
class LivePaperState:
    running: bool = False
    pair: str = ""
    watchlist: list[str] = field(default_factory=list)
    scanned_pairs: dict[str, str] = field(default_factory=dict)
    interval: str = ""
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
    equity_history_json: str = "[]"
    candles_json: str = "{}"
    last_updated: str = ""
    error: str = ""


_state_lock = threading.Lock()
_live_state = LivePaperState()


def get_live_state() -> dict:
    with _state_lock:
        return asdict(_live_state)


def _update_live_state(**kwargs) -> None:
    with _state_lock:
        for k, v in kwargs.items():
            setattr(_live_state, k, v)


def _decimal_metadata(value: Any, default: Decimal) -> Decimal:
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except Exception:
        return default


def _bool_metadata(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False
    return default


def _closed_position_direction(fill: PaperFill) -> SignalDirection | None:
    if fill.side == PaperOrderSide.SELL:
        return SignalDirection.LONG
    if fill.side == PaperOrderSide.BUY:
        return SignalDirection.SHORT
    return None


def _is_position_close_fill(fill: PaperFill) -> bool:
    metadata = fill.metadata if isinstance(fill.metadata, dict) else {}
    return (
        fill.realized_pnl != 0
        or bool(metadata.get("exit_trigger_type"))
        or bool(metadata.get("reason"))
    )


def _paper_exit_reason(fill: PaperFill) -> str:
    metadata = fill.metadata if isinstance(fill.metadata, dict) else {}
    trigger_type = str(metadata.get("exit_trigger_type") or "").strip()
    detail = str(metadata.get("reason") or "").strip()
    detail_l = detail.lower()

    if trigger_type == "stop_loss":
        if "breakeven" in detail_l:
            return "breakeven_stop"
        if "profit lock" in detail_l:
            return "profit_lock_stop"
        if "dynamic atr" in detail_l:
            return "dynamic_atr_stop"
        return "stop_loss"
    if trigger_type == "take_profit":
        if "dynamic atr" in detail_l:
            return "dynamic_atr_take_profit"
        return "take_profit"
    return trigger_type or detail or "signal"


def _paper_stop_style_exit(exit_reason: str) -> bool:
    normalized = exit_reason.lower().replace(" ", "_")
    return (
        "stop" in normalized
        or normalized in {"dynamic_atr_stop", "profit_lock_stop", "breakeven_stop"}
    )


def _paper_take_profit_style_exit(exit_reason: str) -> bool:
    normalized = exit_reason.lower().replace(" ", "_")
    return "take_profit" in normalized


def _position_stop_management_active(metadata: dict[str, Any]) -> bool:
    return (
        bool(metadata.get("stop_type"))
        or _bool_metadata(metadata.get("trailing_stop_active"), False)
        or _bool_metadata(metadata.get("atr_dynamic_exit_active"), False)
    )


def _position_take_profit_suppressed(metadata: dict[str, Any]) -> bool:
    if _bool_metadata(metadata.get("take_profit_suppressed_by_trailing"), False):
        return True
    if _bool_metadata(metadata.get("profit_lock_enabled"), False):
        return True
    if (
        _bool_metadata(metadata.get("atr_dynamic_exits_enabled"), False)
        and _bool_metadata(metadata.get("atr_trailing_enabled"), False)
    ):
        return True
    return _bool_metadata(metadata.get("trailing_stop_enabled"), False)


def _position_stop_type(metadata: dict[str, Any]) -> str:
    raw = str(metadata.get("stop_type") or "").strip().lower()
    if raw in {"profit_lock", "profit lock"}:
        return "profit lock"
    if raw in {"breakeven", "break even"}:
        return "breakeven"
    if raw == "atr":
        return "ATR"
    if _bool_metadata(metadata.get("trailing_stop_active"), False):
        return "trailing"
    if _bool_metadata(metadata.get("atr_dynamic_exit_active"), False):
        return "ATR"
    if _bool_metadata(metadata.get("profit_lock_enabled"), False):
        return "profit lock pending"
    if _bool_metadata(metadata.get("atr_trailing_enabled"), False):
        return "ATR pending"
    if _bool_metadata(metadata.get("trailing_stop_enabled"), False):
        return "trailing pending"
    return ""


def _quantity_unit(pair: str) -> str:
    symbol = pair
    if symbol.startswith("B-"):
        symbol = symbol[2:]
    return symbol.split("_", 1)[0] or "contracts"


def _paper_reentry_override(signal: Any, *, strict: bool) -> bool:
    metadata = signal.metadata if isinstance(signal.metadata, dict) else {}
    if metadata.get("momentum_ignition"):
        return True
    if metadata.get("entry_type") == "intrabar_reversal_breakout" and not strict:
        return True

    final_score = abs(_decimal_metadata(metadata.get("final_score"), Decimal("0")))
    agreement = _decimal_metadata(
        metadata.get("agreement_ratio")
        or metadata.get("long_agreement_ratio")
        or metadata.get("short_agreement_ratio"),
        Decimal("0"),
    )
    visual_score = _decimal_metadata(metadata.get("visual_score"), Decimal("0"))
    visual = metadata.get("visual")
    visual_blocked = isinstance(visual, dict) and bool(visual.get("blocked"))
    if visual_blocked:
        return False

    if strict:
        return (
            final_score >= Decimal("0.70")
            and agreement >= Decimal("0.75")
            and visual_score >= Decimal("0.15")
        )
    return (
        final_score >= Decimal("0.62")
        and agreement >= Decimal("0.68")
        and visual_score >= Decimal("0.05")
    )


_active_loop: PaperTradingLoop | None = None


def get_active_loop() -> PaperTradingLoop | None:
    return _active_loop


def set_active_loop(loop: PaperTradingLoop | None) -> None:
    global _active_loop
    _active_loop = loop


class PaperTradingLoop:
    def __init__(
        self, 
        settings: Settings, 
        strategy_name: str = "adaptive_hybrid",
        state_store: PaperStateStore | None = None,
        session_store: PaperSessionStore | None = None,
    ) -> None:
        self.settings = settings
        self.logger = logging.getLogger(__name__)
        
        self.strategy_engine = strategy_engine_for_name(strategy_name)
        self.risk_manager = RiskManager(settings.risk)
        self.state_store = state_store or PaperStateStore()
        self.session_store = session_store or PaperSessionStore() # Task 1
        
        self.broker = PaperBroker(
            starting_equity=settings.paper_starting_equity,
            maker_fee_rate=settings.risk.maker_fee_rate,
            taker_fee_rate=settings.risk.taker_fee_rate,
            fee_gst_rate=settings.risk.fee_gst_rate,
            slippage_pct=settings.risk.slippage_pct,
            stop_slippage_pct=settings.risk.stop_slippage_pct,
            trailing_stop_enabled=settings.risk.trailing_stop_enabled,
            trailing_stop_activation_pct=settings.risk.trailing_stop_activation_pct,
            trailing_stop_distance_pct=settings.risk.trailing_stop_distance_pct,
            quote_to_margin_rate=settings.quote_to_margin_rate,
            account_currency=settings.futures_margin_currency,
            price_quote_currency=settings.price_quote_currency,
            state_store=self.state_store,
            logger=logging.getLogger("app.broker.paper")
        )
        
        self.summary_logger = PaperTradingSummaryLogger()
        self.client = CoinDCXFuturesClient(settings)
        
        # Multi-pair infrastructure
        self.series: dict[str, CandleSeries] = {}
        self.execution_series: dict[str, CandleSeries] = {}
        self.gap_guards: dict[str, CandleGapGuard] = {}
        
        self.candle_count = 0
        self.equity_history: list[dict[str, Any]] = [] # Task 2
        self.candle_history: dict[str, list[dict[str, Any]]] = {}
        self._position_entry_candle: dict[str, int] = {}
        self._position_entry_prices: dict[str, Decimal] = {}
        
        self._watchlist: list[str] = []
        self._current_interval = "unknown"
        self._ws_client = None
        self._stop_requested = False
        self._closed_count: int = 0
        self._entries_this_parent_candle: dict[str, int] = {}
        self._current_parent_open_ms: dict[str, int] = {}
        self._pending_stream_candles: dict[tuple[str, str], OHLCVCandle] = {}
        self._pair_cooldown_until_ms: dict[str, int] = {}
        self._same_direction_cooldown_until_ms: dict[tuple[str, str], int] = {}
        self._same_direction_reversal_cooldown_until_ms: dict[tuple[str, str], int] = {}
        self._pair_recent_net_pnls: dict[str, list[Decimal]] = {}
        self._global_loss_cooldown_until_ms: int = 0
        self._consecutive_losing_trades: int = 0
        
        self._init_audit_log()

        # Task 4: Restore state
        if self.broker.restored_state_ignored:
            self.state_store.clear()
            self.session_store.clear()
        else:
            self._load_session()

    def _init_audit_log(self) -> None:
        import os
        import csv
        from pathlib import Path
        path = Path("data/paper_intrabar_audit.csv")
        if not path.parent.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            with open(path, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow([
                    "timestamp", "pair", "strategy_interval", "execution_interval",
                    "open", "high", "low", "close", "volume",
                    "previous_parent_high", "previous_parent_low",
                    "breakout_candidate_side", "decision", "rejection_reason",
                    "open_position_count", "same_pair_position_open",
                    "total_open_positions", "risk_approved"
                ])

    def _log_audit(self, row: list[Any]) -> None:
        import csv
        with open("data/paper_intrabar_audit.csv", "a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(row)

    def _positions_payload(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for p in self.broker.open_positions():
            metadata = p.metadata if isinstance(p.metadata, dict) else {}
            active_stop = (
                metadata.get("atr_stop_loss")
                or metadata.get("last_trailing_stop")
                or (p.stop_loss if _position_stop_management_active(metadata) else None)
            )
            stop_type = _position_stop_type(metadata)
            tp_suppressed = _position_take_profit_suppressed(metadata)
            rows.append(
                {
                    "pair": p.pair,
                    "direction": p.direction.value,
                    "quantity": str(p.quantity),
                    "entry_price": str(p.entry_price),
                    "unrealized_pnl": str(p.unrealized_pnl(self.broker.mark_price_for(p.pair, p.entry_price))),
                    "notional": str(p.margin_notional(self.broker.mark_price_for(p.pair, p.entry_price))),
                    "notional_currency": self.settings.futures_margin_currency,
                    "pnl_currency": self.settings.futures_margin_currency,
                    "price_quote_currency": self.settings.price_quote_currency,
                    "quote_to_margin_rate": str(p.quote_to_margin_rate),
                    "quantity_unit": _quantity_unit(p.pair),
                    "risk_percent_used": str(metadata.get("risk_percent_used") or ""),
                    "planned_risk_amount": str(metadata.get("planned_risk_amount") or ""),
                    "required_margin": str(metadata.get("required_margin") or ""),
                    "strategy": p.strategy_name,
                    "stop_loss": str(p.stop_loss) if p.stop_loss else None,
                    "take_profit": str(p.take_profit) if p.take_profit else None,
                    "active_trailing_stop": str(active_stop) if active_stop else None,
                    "take_profit_suppressed_by_trailing": tp_suppressed,
                    "atr_best_price": str(metadata.get("atr_best_price") or ""),
                    "stop_type": stop_type,
                    "atr_latest": str(metadata.get("atr_latest") or ""),
                    "trailing_stop_active": _bool_metadata(metadata.get("trailing_stop_active"), False),
                    "atr_dynamic_exit_active": _bool_metadata(metadata.get("atr_dynamic_exit_active"), False),
                    "profit_lock_enabled": _bool_metadata(metadata.get("profit_lock_enabled"), False),
                    "atr_trailing_enabled": _bool_metadata(metadata.get("atr_trailing_enabled"), False),
                }
            )
        return rows

    def _candle_payload(self, candle: OHLCVCandle) -> dict[str, Any]:
        return {
            "pair": candle.pair,
            "interval": candle.interval,
            "t": candle.close_time_ms,
            "open_time_ms": candle.open_time_ms,
            "open": str(candle.open),
            "high": str(candle.high),
            "low": str(candle.low),
            "close": str(candle.close),
            "volume": str(candle.volume),
        }

    def _seed_candle_history(self, pair: str, series: CandleSeries) -> None:
        self.candle_history[pair] = [
            self._candle_payload(candle) for candle in list(series)[-120:]
        ]

    def _record_live_candle(self, candle: OHLCVCandle) -> None:
        preferred_interval = (
            self.settings.execution_interval
            if self.settings.paper_intrabar_enabled
            else self._current_interval
        )
        if preferred_interval != "unknown" and candle.interval != preferred_interval:
            return

        history = self.candle_history.setdefault(candle.pair, [])
        payload = self._candle_payload(candle)
        if history and history[-1].get("t") == payload["t"]:
            history[-1] = payload
        else:
            history.append(payload)
        if len(history) > 240:
            del history[:-240]

    def _candles_payload(self) -> dict[str, Any]:
        chart_pair = ""
        for position in self.broker.open_positions():
            if position.pair in self.candle_history:
                chart_pair = position.pair
                break
        if not chart_pair:
            for pair in self._watchlist:
                if pair in self.candle_history:
                    chart_pair = pair
                    break
        if not chart_pair and self.candle_history:
            chart_pair = next(iter(self.candle_history))

        candles = self.candle_history.get(chart_pair, [])[-120:] if chart_pair else []
        interval = candles[-1].get("interval", "") if candles else ""
        return {
            "pair": chart_pair,
            "interval": interval,
            "candles": candles,
        }

    def _publish_live_snapshot(
        self,
        *,
        interval: str | None = None,
        last_updated: str | None = None,
        error: str | None = None,
    ) -> None:
        snapshot = self.broker.snapshot()
        drawdown = self._equity_drawdown_summary(snapshot.equity)
        
        # Build scanned pairs status
        scanned: dict[str, str] = {}
        for pair in self._watchlist:
            if pair in self.series:
                latest = self.series[pair].latest()
                if latest:
                    ts = time.strftime('%H:%M:%S', time.gmtime(latest.close_time_ms / 1000))
                    scanned[pair] = f"Closed: {ts} @ {latest.close}"
                else:
                    scanned[pair] = "Waiting for data..."
            else:
                scanned[pair] = "Not initialized"

        update: dict[str, Any] = {
            "candle_count": self.candle_count,
            "equity": str(snapshot.equity),
            "realized_pnl": str(self.broker.realized_pnl),
            "unrealized_pnl": str(snapshot.unrealized_pnl),
            "net_realized_pnl": str(self.broker.realized_pnl - self.broker.fees_paid),
            "fees_paid": str(self.broker.fees_paid),
            "open_notional": str(snapshot.open_notional),
            "return_abs": str(snapshot.equity - snapshot.starting_equity),
            "return_pct": str(
                ((snapshot.equity - snapshot.starting_equity) / snapshot.starting_equity)
                * Decimal("100")
                if snapshot.starting_equity > 0
                else Decimal("0")
            ),
            "max_drawdown_pct": str(drawdown["max_drawdown_pct"]),
            "peak_equity": str(drawdown["peak_equity"]),
            "open_positions": len(self.broker.positions),
            "total_fills": len(self.broker.fills),
            "positions_json": json.dumps(to_jsonable(self._positions_payload())),
            "equity_history_json": json.dumps(to_jsonable(self.equity_history[-500:])),
            "candles_json": json.dumps(to_jsonable(self._candles_payload())),
            "watchlist": self._watchlist,
            "scanned_pairs": scanned,
        }
        if interval is not None:
            update["interval"] = interval
        if last_updated is not None:
            update["last_updated"] = last_updated
        if error is not None:
            update["error"] = error
        _update_live_state(**update)

    def _equity_drawdown_summary(self, current_equity: Decimal) -> dict[str, Decimal]:
        values = [self.broker.starting_equity]
        for point in self.equity_history:
            if isinstance(point, dict):
                value = _decimal_metadata(point.get("equity"), Decimal("0"))
                if value > 0:
                    values.append(value)
        if current_equity > 0:
            values.append(current_equity)

        peak = Decimal("0")
        max_drawdown = Decimal("0")
        for value in values:
            if value > peak:
                peak = value
            if peak > 0:
                drawdown = ((peak - value) / peak) * Decimal("100")
                if drawdown > max_drawdown:
                    max_drawdown = drawdown
        return {
            "peak_equity": peak,
            "max_drawdown_pct": max_drawdown,
        }

    def _load_session(self) -> None:
        saved = self.session_store.load_session()
        if saved:
            self.candle_count = int(saved.get("candle_count", 0))
            self.equity_history = saved.get("equity_history", [])
            raw_candle_history = saved.get("candle_history")
            if isinstance(raw_candle_history, dict):
                self.candle_history = {
                    str(pair): list(values)[-240:]
                    for pair, values in raw_candle_history.items()
                    if isinstance(values, list)
                }
            self._position_entry_candle = {
                k: int(v) for k, v in saved.get("position_entry_candle", {}).items()
                if isinstance(saved.get("position_entry_candle"), dict)
            }
            self._position_entry_prices = {
                k: Decimal(str(v)) for k, v in saved.get("position_entry_prices", {}).items()
                if isinstance(saved.get("position_entry_prices"), dict)
            }
            
            # Robust loading for parent candle state (could be int in old sessions)
            raw_entries = saved.get("entries_this_parent_candle")
            if isinstance(raw_entries, dict):
                self._entries_this_parent_candle = {k: int(v) for k, v in raw_entries.items()}
            elif isinstance(raw_entries, (int, float)):
                # Default to first pair if known, or leave as empty dict (will be initialized on first candle)
                pass

            raw_open_ms = saved.get("current_parent_open_ms")
            if isinstance(raw_open_ms, dict):
                self._current_parent_open_ms = {k: int(v) for k, v in raw_open_ms.items()}
            elif isinstance(raw_open_ms, (int, float)):
                pass

            raw_pair_cooldown = saved.get("pair_cooldown_until_ms")
            if isinstance(raw_pair_cooldown, dict):
                self._pair_cooldown_until_ms = {
                    str(pair): int(value)
                    for pair, value in raw_pair_cooldown.items()
                    if value is not None
                }

            raw_direction_cooldown = saved.get("same_direction_cooldown_until_ms")
            if isinstance(raw_direction_cooldown, dict):
                restored: dict[tuple[str, str], int] = {}
                for raw_key, value in raw_direction_cooldown.items():
                    if value is None:
                        continue
                    key = str(raw_key)
                    if "|" not in key:
                        continue
                    pair, direction = key.rsplit("|", 1)
                    if pair and direction:
                        restored[(pair, direction)] = int(value)
                self._same_direction_cooldown_until_ms = restored

            raw_reversal_cooldown = saved.get("same_direction_reversal_cooldown_until_ms")
            if isinstance(raw_reversal_cooldown, dict):
                restored_reversal: dict[tuple[str, str], int] = {}
                for raw_key, value in raw_reversal_cooldown.items():
                    if value is None:
                        continue
                    key = str(raw_key)
                    if "|" not in key:
                        continue
                    pair, direction = key.rsplit("|", 1)
                    if pair and direction:
                        restored_reversal[(pair, direction)] = int(value)
                self._same_direction_reversal_cooldown_until_ms = restored_reversal

            raw_pair_pnls = saved.get("pair_recent_net_pnls")
            if isinstance(raw_pair_pnls, dict):
                restored_pnls: dict[str, list[Decimal]] = {}
                lookback = max(self.settings.risk.pair_loss_lookback, 1)
                for pair, values in raw_pair_pnls.items():
                    if not isinstance(values, list):
                        continue
                    pnls: list[Decimal] = []
                    for value in values[-lookback:]:
                        try:
                            pnls.append(Decimal(str(value)))
                        except Exception:
                            continue
                    restored_pnls[str(pair)] = pnls
                self._pair_recent_net_pnls = restored_pnls

            self._global_loss_cooldown_until_ms = int(
                saved.get("global_loss_cooldown_until_ms", 0) or 0
            )
            self._consecutive_losing_trades = int(
                saved.get("consecutive_losing_trades", 0) or 0
            )
                
            self.logger.info("Restored paper session: candle_count=%d", self.candle_count)

    def _save_session(self) -> None:
        # Task 3: Autosave
        snapshot = self.broker.snapshot()
        state = {
            "candle_count": self.candle_count,
            "equity_history": self.equity_history,
            "candle_history": self.candle_history,
            "position_entry_candle": self._position_entry_candle,
            "position_entry_prices": {k: str(v) for k, v in self._position_entry_prices.items()},
            "entries_this_parent_candle": self._entries_this_parent_candle,
            "current_parent_open_ms": self._current_parent_open_ms,
            "pair_cooldown_until_ms": self._pair_cooldown_until_ms,
            "same_direction_cooldown_until_ms": {
                f"{pair}|{direction}": value
                for (pair, direction), value in self._same_direction_cooldown_until_ms.items()
            },
            "same_direction_reversal_cooldown_until_ms": {
                f"{pair}|{direction}": value
                for (pair, direction), value in self._same_direction_reversal_cooldown_until_ms.items()
            },
            "pair_recent_net_pnls": {
                pair: [str(value) for value in values]
                for pair, values in self._pair_recent_net_pnls.items()
            },
            "global_loss_cooldown_until_ms": self._global_loss_cooldown_until_ms,
            "consecutive_losing_trades": self._consecutive_losing_trades,
            "last_updated": datetime.now(timezone.utc).isoformat(),
            # Mirror some broker state for easy JSON access if needed
            "realized_pnl": str(self.broker.realized_pnl),
            "fees_paid": str(self.broker.fees_paid),
            "equity": str(snapshot.equity),
        }
        try:
            self.session_store.save_session(state)
        except Exception as exc:
            self.logger.exception("Failed to save paper session snapshot.")
            _update_live_state(
                error=f"Failed to save paper session snapshot: {exc}",
                last_updated=datetime.now(timezone.utc).isoformat(),
            )

    def stop(self) -> None:
        self._stop_requested = True
        if self._ws_client:
            self._ws_client.stop()
        self._save_session()

    def run(self, pairs: str | list[str], interval: str) -> None:
        if isinstance(pairs, str):
            pairs = [pairs]
        
        self._watchlist = pairs
        self._stop_requested = False
        
        # Task 9: If intrabar is enabled, interval is the strategy_interval
        strategy_interval = self.settings.strategy_interval if self.settings.paper_intrabar_enabled else interval
        self._current_interval = strategy_interval
        
        _update_live_state(
            running=True,
            pair=", ".join(pairs),
            interval=strategy_interval,
            strategy=self.strategy_engine.strategies[0].name if self.strategy_engine.strategies else "unknown",
            starting_equity=str(self.broker.starting_equity),
            error="",
        )
        self._publish_live_snapshot(interval=strategy_interval, last_updated="starting")
        
        set_active_loop(self)
        try:
            subscriptions = []
            for pair in pairs:
                self.gap_guards[pair] = CandleGapGuard(strategy_interval)
                self._warm_up(pair, strategy_interval)
                
                if self.settings.paper_intrabar_enabled:
                    execution_interval = self.settings.execution_interval
                    self.logger.info("[%s] Intrabar execution enabled: %s -> %s", pair, strategy_interval, execution_interval)
                    self._warm_up_execution(pair, execution_interval)

                subscriptions.append(MarketSubscription(futures_candle_channel(pair, strategy_interval), "candlestick"))
                if self.settings.paper_intrabar_enabled and strategy_interval != self.settings.execution_interval:
                    subscriptions.append(MarketSubscription(futures_candle_channel(pair, self.settings.execution_interval), "candlestick"))
                subscriptions.append(MarketSubscription(futures_orderbook_channel(pair, 50), "depth-snapshot"))
                subscriptions.append(MarketSubscription(futures_orderbook_channel(pair, 50), "depth-update"))
            
            pipeline = MarketDataPipeline()
            original_handle_raw = pipeline.handle_raw
            def hooked_handle_raw(event_name: str, payload: Any):
                events = original_handle_raw(event_name, payload)
                for event in events:
                    event_type = getattr(event, "event_type", None)
                    if event_type in {"orderbook_snapshot", "orderbook_update"}:
                        orderbook = pipeline.store.orderbooks.get(event.pair)
                        if orderbook is None:
                            continue
                        self.broker.update_orderbook(
                            event.pair,
                            bids=orderbook.bids,
                            asks=orderbook.asks,
                            timestamp_ms=orderbook.timestamp_ms,
                        )
                    if event_type == "candle":
                        from app.data.candle_builder import OHLCVCandle
                        candle = OHLCVCandle.from_candle_event(event)
                        self._handle_stream_candle_snapshot(candle)
                return events
            
            pipeline.handle_raw = hooked_handle_raw

            reconnect_attempt = 0
            while not self._stop_requested:
                self._ws_client = CoinDCXFuturesWebSocketClient(self.settings, pipeline=pipeline)
                try:
                    if reconnect_attempt == 0:
                        self.logger.info(
                            "Starting live multi-pair paper loop for %s @ %s",
                            pairs,
                            strategy_interval,
                        )
                    else:
                        self.logger.info(
                            "Reconnecting paper websocket for %s @ %s (attempt %d)",
                            pairs,
                            strategy_interval,
                            reconnect_attempt + 1,
                        )
                    self._ws_client.run(subscriptions)
                    if self._stop_requested:
                        break
                    reconnect_attempt += 1
                    delay = self._websocket_reconnect_delay_seconds(reconnect_attempt)
                    message = (
                        "CoinDCX websocket disconnected unexpectedly; "
                        f"replaying missed candles and reconnecting in {delay:g}s."
                    )
                    self.logger.warning(message)
                    _update_live_state(
                        running=True,
                        error=message,
                        last_updated=datetime.now(timezone.utc).isoformat(),
                    )
                except KeyboardInterrupt:
                    self.logger.info("Graceful shutdown requested.")
                    self._stop_requested = True
                    break
                except Exception as exc:
                    reconnect_attempt += 1
                    delay = self._websocket_reconnect_delay_seconds(reconnect_attempt)
                    message = (
                        "Paper websocket failed; "
                        f"replaying missed candles and reconnecting in {delay:g}s: {exc}"
                    )
                    self.logger.exception(message)
                    _update_live_state(
                        running=True,
                        error=message,
                        last_updated=datetime.now(timezone.utc).isoformat(),
                    )
                finally:
                    if self._ws_client is not None:
                        self._ws_client.stop()
                    self._ws_client = None
                    self._save_session()

                if self._stop_requested:
                    break
                self._replay_missed_candles_after_disconnect(pairs, strategy_interval)
                self._sleep_before_reconnect(delay)
            
            snapshot = self.broker.snapshot()
            self.logger.info(
                "Final Paper State: equity=%.2f, open_positions=%d", 
                snapshot.equity, snapshot.open_position_count
            )
        finally:
            set_active_loop(None)
            self._ws_client = None
            _update_live_state(running=False, error="")

    def _websocket_reconnect_delay_seconds(self, attempt: int) -> float:
        return min(30.0, float(2 ** min(max(attempt - 1, 0), 5)))

    def _sleep_before_reconnect(self, delay_seconds: float) -> None:
        deadline = time.monotonic() + max(0.0, delay_seconds)
        while not self._stop_requested:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(0.5, remaining))

    def _replay_missed_candles_after_disconnect(
        self,
        pairs: list[str],
        strategy_interval: str,
    ) -> None:
        intervals = [strategy_interval]
        if (
            self.settings.paper_intrabar_enabled
            and self.settings.execution_interval != strategy_interval
        ):
            intervals.append(self.settings.execution_interval)

        for pair in pairs:
            for interval in intervals:
                try:
                    self._replay_missed_candles(pair, interval)
                except Exception:
                    self.logger.exception(
                        "[%s] Failed to replay missed %s candles after websocket reconnect.",
                        pair,
                        interval,
                    )

    def _replay_missed_candles(self, pair: str, interval: str) -> int:
        from app.backtest.data_loader import load_historical_candle_series_between

        series = (
            self.execution_series.get(pair)
            if self.settings.paper_intrabar_enabled
            and interval == self.settings.execution_interval
            else self.series.get(pair)
        )
        if series is None:
            return 0
        latest = series.latest()
        if latest is None:
            return 0

        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        from_ts = int((latest.close_time_ms + 1) / 1000)
        to_ts = int(now_ms / 1000)
        if from_ts >= to_ts:
            return 0

        interval_ms = interval_to_ms(interval)
        maxlen = max(10, min(1000, int(((to_ts - from_ts) * 1000) / interval_ms) + 5))
        replay_series = load_historical_candle_series_between(
            client=self.client,
            pair=pair,
            interval=interval,
            from_ts=from_ts,
            to_ts=to_ts,
            maxlen=maxlen,
        )
        missing = [
            replace(candle, is_closed=True)
            for candle in replay_series
            if candle.close_time_ms > latest.close_time_ms and candle.close_time_ms < now_ms
        ]
        if not missing:
            return 0

        for candle in sorted(missing, key=lambda item: item.close_time_ms):
            self._on_candle(candle)
        self.logger.info(
            "[%s] Replayed %d missed %s candle(s) after websocket reconnect.",
            pair,
            len(missing),
            interval,
        )
        return len(missing)

    def _handle_stream_candle_snapshot(self, candle: OHLCVCandle) -> None:
        for closed_candle in self._closed_candles_from_stream(candle):
            self._on_candle(closed_candle)
        if (
            self.settings.paper_intrabar_enabled
            and candle.interval == self.settings.execution_interval
        ):
            self._on_candle(candle, allow_partial_execution=True)

    def _closed_candles_from_stream(self, candle: OHLCVCandle) -> list[OHLCVCandle]:
        """Convert streaming candle snapshots into finalized candles.

        CoinDCX sends repeated updates for the still-forming candle. Some payloads do
        not carry a reliable close flag, so finality is inferred when the stream
        advances to the next candle bucket.
        """
        key = (candle.pair, candle.interval)
        previous = self._pending_stream_candles.get(key)
        if previous is None:
            self._pending_stream_candles[key] = candle
            return []

        if candle.open_time_ms < previous.open_time_ms:
            return []

        if candle.open_time_ms == previous.open_time_ms:
            self._pending_stream_candles[key] = candle
            return []

        self._pending_stream_candles[key] = candle
        return [replace(previous, is_closed=True)]

    def _warm_up(self, pair: str, interval: str, lookback: int = 200) -> None:
        from app.backtest.data_loader import (
            load_historical_candle_series,
            REST_RESOLUTION_BY_INTERVAL,
        )
        _update_live_state(last_updated=f"warming up {pair}...")
        self.logger.info("Warming up with %d REST candles for %s %s...", lookback, pair, interval)

        if interval not in REST_RESOLUTION_BY_INTERVAL:
            raise ValueError(
                f"Interval {interval!r} not supported for REST warmup. "
                f"Choose one of: {sorted(REST_RESOLUTION_BY_INTERVAL)}"
            )

        self.series[pair] = load_historical_candle_series(
            client=self.client,
            pair=pair,
            interval=interval,
            lookback=lookback,
        )
        loaded = len(self.series[pair])
        self.logger.info("[%s] Warmed up with %d candles", pair, loaded)
        self._seed_candle_history(pair, self.series[pair])
        self._publish_live_snapshot(last_updated=datetime.now(timezone.utc).isoformat())
        if loaded == 0:
            message = (
                f"Warmup returned 0 candles for {pair} {interval}. "
                "Check pair format or wait for live candles to build indicators."
            )
            self.logger.warning(message)
            _update_live_state(error=message, last_updated=datetime.now(timezone.utc).isoformat())

    def _warm_up_execution(self, pair: str, interval: str, lookback: int = 200) -> None:
        from app.backtest.data_loader import load_historical_candle_series
        self.logger.info("[%s] Warming up execution series (%s)...", pair, interval)
        self.execution_series[pair] = load_historical_candle_series(
            client=self.client,
            pair=pair,
            interval=interval,
            lookback=lookback,
        )
        loaded = len(self.execution_series[pair])
        self._seed_candle_history(pair, self.execution_series[pair])
        self._publish_live_snapshot(last_updated=datetime.now(timezone.utc).isoformat())
        if loaded == 0:
            message = (
                f"Execution warmup returned 0 candles for {pair} {interval}. "
                "Check pair format or wait for live candles."
            )
            self.logger.warning(message)
            _update_live_state(error=message, last_updated=datetime.now(timezone.utc).isoformat())

    def _on_candle(
        self,
        candle: OHLCVCandle,
        *,
        allow_partial_execution: bool = False,
    ) -> None:
        if self._stop_requested:
            if self._ws_client:
                self._ws_client.stop()
            return

        is_live_execution_update = (
            allow_partial_execution
            and self.settings.paper_intrabar_enabled
            and candle.interval == self.settings.execution_interval
        )
        if not candle.is_closed and not is_live_execution_update:
            return

        pair = candle.pair
        if pair not in self.series:
            return

        # Determine which stream this belongs to
        is_strategy = (candle.interval == self._current_interval or self._current_interval == "unknown")
        is_execution = (self.settings.paper_intrabar_enabled and candle.interval == self.settings.execution_interval)

        if not self.settings.paper_intrabar_enabled:
            is_strategy = True
            is_execution = True

        if is_strategy:
            self._handle_strategy_candle(candle)

        if is_execution:
            self._handle_execution_candle(
                candle,
                live_partial_update=is_live_execution_update,
            )
            
        self.broker.update_mark_prices({candle.pair: candle.close})
        self._publish_live_snapshot(last_updated=datetime.now(timezone.utc).isoformat())

    def _handle_strategy_candle(self, candle: OHLCVCandle) -> None:
        pair = candle.pair
        series = self.series.get(pair)
        if series is None:
            return

        prev = series.latest()
        if prev is not None:
            if (
                prev.pair == candle.pair
                and prev.interval == candle.interval
                and prev.open_time_ms == candle.open_time_ms
            ):
                series.add(candle)
                return

            gap_result = self.gap_guards[pair].check(prev, candle)
            if gap_result.has_gap:
                self.logger.warning("[%s] Gap detected! Re-fetching...", pair)
                self._warm_up(pair, candle.interval)
                return

            if candle.close_time_ms <= prev.close_time_ms:
                return

        series.add(candle)
        
        if self.settings.paper_intrabar_enabled:
            if candle.open_time_ms > self._current_parent_open_ms.get(pair, 0):
                self._current_parent_open_ms[pair] = candle.open_time_ms
                self._entries_this_parent_candle[pair] = 0

    def _handle_execution_candle(
        self,
        candle: OHLCVCandle,
        *,
        live_partial_update: bool = False,
    ) -> None:
        pair = candle.pair
        if not self.settings.paper_intrabar_enabled:
            self.candle_count += 1
            self._record_live_candle(candle)
            self._process_trading_candle(candle)
            return

        series = self.execution_series.get(pair)
        if series is None:
            return
        
        prev = series.latest()
        is_same_candle_update = False
        if prev:
            if (
                prev.pair == candle.pair
                and prev.interval == candle.interval
                and prev.open_time_ms == candle.open_time_ms
            ):
                is_same_candle_update = True
            elif candle.close_time_ms <= prev.close_time_ms:
                return

        if is_same_candle_update and prev == candle and live_partial_update:
            return
            
        series.add(candle)
        self._record_live_candle(candle)
        if not is_same_candle_update:
            self.candle_count += 1
        
        from app.data.candle_builder import floor_time_ms
        parent_open_ms = floor_time_ms(candle.open_time_ms, self.settings.strategy_interval)
        if parent_open_ms > self._current_parent_open_ms.get(pair, 0):
            self._current_parent_open_ms[pair] = parent_open_ms
            self._entries_this_parent_candle[pair] = 0

        self._process_trading_candle(
            candle,
            live_partial_update=live_partial_update,
        )

    def _process_trading_candle(
        self,
        candle: OHLCVCandle,
        *,
        live_partial_update: bool = False,
    ) -> None:
        pair = candle.pair
        main_series = self.execution_series.get(pair) if self.settings.paper_intrabar_enabled else self.series.get(pair)
        if main_series is None: return
        
        if self.settings.paper_intrabar_enabled:
            htf_series = self.series[pair]
            if self.settings.use_partial_parent_candle:
                htf_series = self._build_provisional_htf_series(pair)
            
            indicators = latest_indicator_snapshot(htf_series)
            context_interval = self.settings.strategy_interval
        else:
            indicators = latest_indicator_snapshot(self.series[pair])
            htf_series = self.series[pair]
            context_interval = candle.interval
        
        atr_val = indicators.atr

        self._handle_broker_reports(self.broker.process_candle(candle), candle)

        self._handle_broker_reports(self.broker.process_candle(candle), candle)


        if self.broker.starting_equity is not None and self.broker.starting_equity > 0:
            snapshot_cb = self.broker.snapshot({candle.pair: candle.close})
            drawdown = self.broker.starting_equity - snapshot_cb.equity
            loss_limit = self.broker.starting_equity * (
                self.settings.risk.max_daily_loss_pct / Decimal("100")
            )
            if drawdown >= loss_limit:
                self.logger.warning(
                    "[circuit_breaker] Daily loss limit hit: drawdown=%.2f limit=%.2f — "
                    "force-closing all positions and halting.",
                    drawdown,
                    loss_limit,
                )
                for pos in list(self.broker.open_positions()):
                    action = (
                        SignalAction.EXIT_LONG
                        if pos.direction == SignalDirection.LONG
                        else SignalAction.EXIT_SHORT
                    )
                    exit_signal = StrategySignal(
                        pair=pos.pair,
                        action=action,
                        direction=pos.direction,
                        entry_price=candle.close,
                        reason="daily_loss_circuit_breaker",
                    )
                    exit_decision = RiskDecision(
                        approved=True,
                        reason="daily_loss_circuit_breaker",
                        signal=exit_signal,
                    )
                    report = self.broker.execute_decision(
                        exit_decision,
                        market_price=candle.close,
                        timestamp_ms=candle.close_time_ms,
                    )
                    if report.accepted and report.fill is not None:
                        self._observe_closed_fill(report.fill, candle, position=report.position)
                        trade_dict = self._map_fill_to_trade_dict(
                            fill=report.fill,
                            candle=candle,
                            position=report.position,
                        )
                        self.summary_logger.on_trade_closed(trade_dict)
                        self._closed_count += 1
                self._stop_requested = True
                return   


        self.broker.update_dynamic_atr_exits(
            candle,
            atr=atr_val,
            stop_multiple=self.settings.risk.atr_stop_multiple,
            take_profit_multiple=self.settings.risk.atr_take_profit_multiple,
            trailing_multiple=self.settings.risk.atr_trailing_multiple,
            stop_enabled=self.settings.risk.atr_stop_enabled,
            take_profit_enabled=self.settings.risk.atr_take_profit_enabled,
            trailing_enabled=self.settings.risk.atr_trailing_enabled,
            take_profit_mode=self.settings.risk.atr_take_profit_mode,
            breakeven_enabled=self.settings.risk.breakeven_enabled,
            breakeven_activation_r=self.settings.risk.breakeven_activation_r,
            breakeven_offset_r=self.settings.risk.breakeven_offset_r,
            profit_lock_enabled=self.settings.risk.profit_lock_enabled,
            profit_lock_activation_r=self.settings.risk.profit_lock_activation_r,
            profit_lock_r=self.settings.risk.profit_lock_r,
            atr_trail_after_r_enabled=self.settings.risk.atr_trail_after_r_enabled,
            atr_trail_activation_r=self.settings.risk.atr_trail_activation_r,
        )

        context = StrategyContext(
            pair=pair,
            interval=context_interval,
            candles=htf_series,
            indicators=indicators,
            features=self._strategy_features(pair),
        )
        signals = self.strategy_engine.evaluate(context)
        
        # Task 4: Audit logging prep
        breakout_rejection = "no_candidate"
        breakout_side = None
        
        # Check if breakout was even attempted
        for s in signals:
            if s.metadata.get("intrabar_reversal_breakout"):
                breakout_side = "long"
                breakout_rejection = s.metadata.get("breakout_rejection", "entered")
                break
            if s.metadata.get("breakout_rejection"):
                breakout_side = "long"
                breakout_rejection = s.metadata.get("breakout_rejection", "rejected")
        
        # If no breakout signal was returned, it might have been rejected inside
        if breakout_side is None:
            # We can check the parent metadata if we modify the strategy to return it, 
            # or just look at what the strategy updated. 
            # In our case, the strategy updates parent_metadata which is usually part of some signal or context.
            # But the signals list might be empty.
            pass

        for signal in signals:
            if signal.action == SignalAction.HOLD:
                continue

            if (
                live_partial_update
                and is_entry_signal(signal)
                and self.settings.enter_on_execution_close
            ):
                continue

            if (
                live_partial_update
                and is_entry_signal(signal)
                and signal.metadata.get("entry_type") != "intrabar_reversal_breakout"
            ):
                continue

            if is_entry_signal(signal):
                signal = self._with_entry_candle_metadata(signal, candle)
                signal = self._with_market_microstructure_metadata(signal)

            safety_rejection = self._paper_entry_safety_rejection(signal, candle)
            if safety_rejection is not None:
                if signal.metadata.get("intrabar_reversal_breakout"):
                    breakout_rejection = safety_rejection
                    breakout_side = "long"
                continue

            if is_entry_signal(signal):
                signal = self._apply_paper_loss_throttle(signal)
                signal = self._apply_pair_recent_loss_throttle(signal)
            
            if self.settings.paper_intrabar_enabled:
                if self._entries_this_parent_candle.get(pair, 0) >= self.settings.max_entries_per_parent_candle:
                    continue

            snapshot = self.broker.snapshot({candle.pair: candle.close})
            open_positions_tuple = tuple(
                OpenPosition(
                    pair=p.pair,
                    direction=p.direction,
                    quantity=p.quantity,
                    entry_price=p.entry_price,
                    leverage=p.leverage,
                    stop_loss=p.stop_loss,
                    quote_to_margin_rate=p.quote_to_margin_rate,
                    unit_contract_value=p.unit_contract_value,
                )
                for p in self.broker.open_positions()
            )
            decision = self.risk_manager.evaluate_signal(
                signal,
                account_equity=self.broker.starting_equity,
                available_equity=snapshot.equity,
                risk_base_mode="initial_equity",
                open_positions=open_positions_tuple,
                daily_realized_pnl=self.broker.realized_pnl,
                daily_loss_limit_equity=self.broker.starting_equity,
                trading_mode=self.settings.trading_mode,
                live_trading_enabled=self.settings.live_trading_allowed,
                requested_leverage=self.settings.paper_leverage,
                quote_to_margin_rate=self.settings.quote_to_margin_rate,
                unit_contract_value=Decimal("1"),
            )
            
            # Task 4: Audit Decision
            if signal.metadata.get("intrabar_reversal_breakout"):
                if not decision.approved:
                    breakout_rejection = f"risk_rejected: {decision.reason}"

            if decision.approved:
                report = self.broker.execute_decision(
                    decision, market_price=candle.close, timestamp_ms=candle.close_time_ms
                )
                from app.broker.models import PaperOrderSide
                if getattr(report, 'accepted', False) and report.fill is not None:
                    if is_entry_signal(signal):
                        if report.fill.side == PaperOrderSide.BUY:
                            self._position_entry_candle[candle.pair] = self.candle_count
                            self._position_entry_prices[candle.pair] = candle.close
                        elif report.fill.side == PaperOrderSide.SELL:
                            self._position_entry_candle[candle.pair] = self.candle_count
                            self._position_entry_prices[candle.pair] = candle.close
                        
                        if self.settings.paper_intrabar_enabled:
                            self._entries_this_parent_candle[pair] = self._entries_this_parent_candle.get(pair, 0) + 1
                    elif is_exit_signal(signal) and _is_position_close_fill(report.fill):
                        self._observe_closed_fill(report.fill, candle, position=report.position)
                        trade_dict = self._map_fill_to_trade_dict(
                            fill=report.fill,
                            candle=candle,
                            position=report.position,
                        )
                        self.summary_logger.on_trade_closed(trade_dict)
                        self._closed_count += 1

        # Task 4: Finalize Audit Log for this execution candle
        snapshot = self.broker.snapshot({candle.pair: candle.close})
        open_positions = self.broker.open_positions()
        same_pair = any(p.pair == candle.pair for p in open_positions)
        
        # If we didn't find a breakout signal, let's see if the strategy left a rejection reason in metadata
        # We need to peek into the strategy's evaluation state if possible, but for now 
        # let's assume if no signal, we might need to find where it failed.
        # Actually, let's modify the strategy to ALWAYS return a "signal" with action HOLD if it was a breakout candidate but failed.
        # Or just rely on the strategy updating some shared state.
        
        # Let's use a simpler approach: the strategy evaluate might have multiple signals.
        # If no breakout signal found, check if it was explicitly rejected.
        # We'll need another evaluate pass or the strategy needs to provide this.
        
        features = context.features.get("backtest_config", {})
        prev_h = features.get("previous_parent_high", Decimal("0"))
        prev_l = features.get("previous_parent_low", Decimal("0"))

        self._log_audit([
            datetime.now(timezone.utc).isoformat(),
            candle.pair,
            self.settings.strategy_interval,
            candle.interval,
            str(candle.open),
            str(candle.high),
            str(candle.low),
            str(candle.close),
            str(candle.volume),
            str(prev_h),
            str(prev_l),
            breakout_side or "none",
            "entered" if breakout_rejection == "entered" else "rejected" if breakout_side else "no_candidate",
            breakout_rejection,
            len(open_positions),
            "yes" if same_pair else "no",
            len(self.broker.positions),
            "yes" if breakout_rejection == "entered" else "no"
        ])

        snapshot = self.broker.snapshot({candle.pair: candle.close})
        
        current_eq_dict = {"t": candle.close_time_ms, "equity": str(snapshot.equity)}
        if not self.equity_history or self.equity_history[-1]["equity"] != current_eq_dict["equity"]:
            self.equity_history.append(current_eq_dict)
            if len(self.equity_history) > 1000:
                self.equity_history.pop(0)

        self.summary_logger.on_candle(
            self.candle_count, 
            snapshot.equity, 
            snapshot.open_position_count,
            self.broker.realized_pnl
        )
        self.summary_logger.check_drawdown(snapshot.equity)
        
        display_interval = f"{self.settings.strategy_interval}/{self.settings.execution_interval}" if self.settings.paper_intrabar_enabled else self._current_interval

        self._publish_live_snapshot(
            interval=display_interval,
            last_updated=datetime.now(timezone.utc).isoformat(),
        )

        self._save_session()

    def _handle_broker_reports(
        self,
        reports: list[PaperExecutionReport],
        candle: OHLCVCandle,
    ) -> None:
        for report in reports:
            if not report.accepted or report.fill is None:
                continue
            if not _is_position_close_fill(report.fill):
                continue
            self._observe_closed_fill(report.fill, candle, position=report.position)
            trade_dict = self._map_fill_to_trade_dict(
                fill=report.fill,
                candle=candle,
                position=report.position,
            )
            self.summary_logger.on_trade_closed(trade_dict)
            self._closed_count += 1

    def _with_entry_candle_metadata(
        self,
        signal: Any,
        candle: OHLCVCandle,
    ) -> Any:
        metadata = {
            **signal.metadata,
            "entry_execution_interval": candle.interval,
            "entry_execution_candle_open_time_ms": candle.open_time_ms,
            "entry_execution_candle_high": candle.high,
            "entry_execution_candle_low": candle.low,
            "entry_execution_candle_close": candle.close,
            "entry_execution_volume": candle.volume,
            "entry_execution_quote_volume": candle.quote_volume,
            "entry_execution_quote_volume_margin": (
                candle.quote_volume * self.settings.quote_to_margin_rate
            ),
        }
        return replace(signal, metadata=metadata)

    def _with_market_microstructure_metadata(self, signal: Any) -> Any:
        summary = self.broker.orderbook_summary(signal.pair)
        if not summary:
            return signal

        quote_to_margin_rate = self.settings.quote_to_margin_rate
        direction = signal.direction
        side_depth_quote = (
            summary.get("ask_depth_quote")
            if direction == SignalDirection.LONG
            else summary.get("bid_depth_quote")
        )
        side_depth_margin = (
            side_depth_quote * quote_to_margin_rate
            if isinstance(side_depth_quote, Decimal)
            else None
        )
        metadata = {
            **signal.metadata,
            "entry_orderbook_best_bid": summary.get("best_bid"),
            "entry_orderbook_best_ask": summary.get("best_ask"),
            "entry_orderbook_spread_pct": summary.get("spread_pct"),
            "entry_orderbook_bid_depth_quote": summary.get("bid_depth_quote"),
            "entry_orderbook_ask_depth_quote": summary.get("ask_depth_quote"),
            "entry_orderbook_side_depth_quote": side_depth_quote,
            "entry_orderbook_side_depth_margin": side_depth_margin,
            "entry_orderbook_timestamp_ms": summary.get("timestamp_ms"),
        }
        return replace(signal, metadata=metadata)

    def _paper_entry_safety_rejection(
        self,
        signal: Any,
        candle: OHLCVCandle,
    ) -> str | None:
        if not is_entry_signal(signal):
            return None
        if candle.close_time_ms < self._global_loss_cooldown_until_ms:
            if _paper_reentry_override(signal, strict=True):
                return None
            return "paper_loss_streak_cooldown"
        pair_cooldown = self._pair_cooldown_until_ms.get(signal.pair, 0)
        if candle.close_time_ms < pair_cooldown:
            if _paper_reentry_override(signal, strict=True):
                return None
            return "paper_pair_stop_loss_cooldown"
        if signal.direction is not None:
            key = (signal.pair, signal.direction.value)
            reversal_cooldown = self._same_direction_reversal_cooldown_until_ms.get(key, 0)
            if candle.close_time_ms < reversal_cooldown:
                return "paper_reversal_same_direction_cooldown"
            direction_cooldown = self._same_direction_cooldown_until_ms.get(key, 0)
            if candle.close_time_ms < direction_cooldown:
                if _paper_reentry_override(signal, strict=False):
                    return None
                return "paper_same_direction_cooldown"
        return None

    def _apply_paper_loss_throttle(self, signal: Any) -> Any:
        if self._consecutive_losing_trades <= 0:
            return signal

        existing = _decimal_metadata(signal.metadata.get("risk_multiplier"), Decimal("1"))
        throttle = Decimal("0.50")
        metadata = {
            **signal.metadata,
            "risk_multiplier": min(existing, throttle),
            "risk_multiplier_applies": True,
            "paper_loss_throttle_active": True,
            "paper_consecutive_losing_trades": self._consecutive_losing_trades,
        }
        return replace(signal, metadata=metadata)

    def _apply_pair_recent_loss_throttle(self, signal: Any) -> Any:
        if not is_entry_signal(signal):
            return signal
        recent = self._pair_recent_net_pnls.get(signal.pair, [])
        profile = pair_recent_risk_profile(recent, self.settings.risk)
        if profile.multiplier >= 1:
            if not profile.metadata:
                return signal
            return replace(
                signal,
                metadata={**signal.metadata, **profile.metadata},
            )

        existing = _decimal_metadata(signal.metadata.get("risk_multiplier"), Decimal("1"))
        metadata = {
            **signal.metadata,
            **profile.metadata,
            "risk_multiplier": min(existing, profile.multiplier),
            "risk_multiplier_applies": True,
        }
        return replace(signal, metadata=metadata)

    def _observe_closed_fill(
        self,
        fill: PaperFill,
        candle: OHLCVCandle,
        *,
        position: PaperPosition | None = None,
    ) -> None:
        metadata = fill.metadata if isinstance(fill.metadata, dict) else {}
        position_metadata = (
            position.metadata
            if position is not None and isinstance(position.metadata, dict)
            else {}
        )
        exit_reason = _paper_exit_reason(fill).lower()
        entry_fee = _decimal_metadata(
            position_metadata.get("entry_fees_total")
            or position_metadata.get("entry_fee"),
            Decimal("0"),
        )
        net_pnl = fill.realized_pnl - fill.fee - entry_fee
        self._record_pair_net_pnl(fill.pair, net_pnl)
        interval_ms = self._paper_safety_interval_ms()

        if net_pnl < 0:
            self._consecutive_losing_trades += 1
            if _paper_stop_style_exit(exit_reason):
                until_ms = candle.close_time_ms + (10 * interval_ms)
                self._pair_cooldown_until_ms[fill.pair] = max(
                    self._pair_cooldown_until_ms.get(fill.pair, 0),
                    until_ms,
                )
                self.logger.info(
                    "[%s] Paper safety: stop-loss cooldown active for 10 execution candles.",
                    fill.pair,
                )
            if self._consecutive_losing_trades >= 2:
                self._global_loss_cooldown_until_ms = max(
                    self._global_loss_cooldown_until_ms,
                    candle.close_time_ms + (20 * interval_ms),
                )
                self.logger.info(
                    "Paper safety: global loss-streak cooldown active after %d losses.",
                    self._consecutive_losing_trades,
                )
            return

        if net_pnl > 0:
            self._consecutive_losing_trades = 0
            direction = _closed_position_direction(fill)
            if direction is None:
                return
            key = (fill.pair, direction.value)
            if _paper_stop_style_exit(exit_reason):
                self._same_direction_reversal_cooldown_until_ms[key] = max(
                    self._same_direction_reversal_cooldown_until_ms.get(key, 0),
                    candle.close_time_ms + (12 * interval_ms),
                )
                self.logger.info(
                    "[%s] Paper safety: same-direction cooldown after reversal stop.",
                    fill.pair,
                )
            elif _paper_take_profit_style_exit(exit_reason):
                self._same_direction_cooldown_until_ms[key] = max(
                    self._same_direction_cooldown_until_ms.get(key, 0),
                    candle.close_time_ms + (12 * interval_ms),
                )
                self.logger.info(
                    "[%s] Paper safety: same-direction cooldown after take-profit.",
                    fill.pair,
                )

    def _record_pair_net_pnl(self, pair: str, net_pnl: Decimal) -> None:
        lookback = max(self.settings.risk.pair_loss_lookback, 1)
        values = self._pair_recent_net_pnls.setdefault(pair, [])
        values.append(net_pnl)
        if len(values) > lookback:
            del values[:-lookback]

    def _paper_safety_interval_ms(self) -> int:
        interval = (
            self.settings.execution_interval
            if self.settings.paper_intrabar_enabled
            else self._current_interval
        )
        try:
            return interval_to_ms(interval)
        except Exception:
            return 60_000

    def _strategy_features(self, pair: str) -> dict[str, Any]:
        execution_candles: list[OHLCVCandle] = []
        exec_series = self.execution_series.get(pair)
        if exec_series is not None:
            execution_candles = list(exec_series)[-30:]

        # Get the high/low of the last fully closed parent candle for breakout checks
        previous_parent_high = Decimal("0")
        previous_parent_low = Decimal("0")
        htf_series = self.series.get(pair)
        if htf_series is not None and len(htf_series) > 0:
            last_closed = htf_series.latest()
            if last_closed is not None:
                previous_parent_high = last_closed.high
                previous_parent_low = last_closed.low

        strategy_name = (
            self.strategy_engine.strategies[0].name
            if self.strategy_engine.strategies
            else ""
        )
        features: dict[str, Any] = {
            "backtest_config": {
                "risk_per_trade_pct": self.settings.risk.max_risk_per_trade_pct,
                "trade_quality_mode": "strict",
                "controlled_shorts_enabled": False,
                "a_setup_score_threshold": Decimal("0.50"),
                "a_setup_agreement_threshold": Decimal("0.65"),
                "b_setup_score_threshold": Decimal("0.40"),
                "b_setup_agreement_threshold": Decimal("0.55"),
                "b_setup_risk_multiplier": Decimal("0.50"),
                "minimum_visual_score": Decimal("0"),
                "long_entry_threshold": Decimal("0.40"),
                "short_entry_threshold": Decimal("0.55"),
                "short_agreement_threshold": Decimal("0.60"),
                "trailing_stop_enabled": self.settings.risk.trailing_stop_enabled,
                "trailing_stop_activation_pct": self.settings.risk.trailing_stop_activation_pct,
                "trailing_stop_distance_pct": self.settings.risk.trailing_stop_distance_pct,
                "atr_dynamic_exits_enabled": (
                    self.settings.risk.atr_stop_enabled
                    or self.settings.risk.atr_take_profit_enabled
                    or self.settings.risk.atr_trailing_enabled
                ),
                "atr_stop_enabled": self.settings.risk.atr_stop_enabled,
                "atr_take_profit_enabled": self.settings.risk.atr_take_profit_enabled,
                "atr_trailing_enabled": self.settings.risk.atr_trailing_enabled,
                "atr_entry_filter_enabled": True,
                "atr_policy_mode": "router",
                "intrabar_reversal_breakout_enabled": (
                    self.settings.paper_intrabar_enabled
                    and strategy_name == "hybrid_meta_v2"
                ),
                "previous_parent_high": previous_parent_high,
                "previous_parent_low": previous_parent_low,
                "reversal_breakout_min_execution_candles": 2,
                "reversal_breakout_volume_ratio": Decimal("2.0"),
                "reversal_breakout_body_ratio": Decimal("0.65"),
                "reversal_breakout_close_position_ratio": Decimal("0.70"),
                "reversal_breakout_risk_multiplier": Decimal("0.50"),
                "reversal_breakout_max_extension_atr": Decimal("2.2"),
                "reversal_breakout_ignition_volume_ratio": Decimal("3.0"),
                "reversal_breakout_ignition_body_ratio": Decimal("0.70"),
                "reversal_breakout_ignition_close_position_ratio": Decimal("0.75"),
                "reversal_breakout_ignition_max_extension_atr": Decimal("5.0"),
                "reversal_breakout_ignition_risk_multiplier": Decimal("0.25"),
                "reversal_breakout_breakeven_activation_r": Decimal("0.70"),
                "reversal_breakout_profit_lock_activation_r": Decimal("1.20"),
                "reversal_breakout_profit_lock_r": Decimal("0.35"),
                "reversal_breakout_time_stop_candles": 8,
            }
        }
        if execution_candles:
            features["execution_candles"] = execution_candles
            features["execution_interval"] = self.settings.execution_interval
        for position in self.broker.open_positions():
            if position.pair != pair:
                continue
            features["open_position"] = {
                "pair": position.pair,
                "direction": position.direction.value,
                "quantity": position.quantity,
                "entry_price": position.entry_price,
                "opened_at_ms": position.opened_at_ms,
                "strategy_name": position.strategy_name,
                "stop_loss": position.stop_loss,
                "take_profit": position.take_profit,
                "metadata": position.metadata,
            }
            break
        return features

    def _build_provisional_htf_series(self, pair: str) -> CandleSeries:
        from app.data.candle_builder import floor_time_ms
        exec_series = self.execution_series.get(pair)
        base_series = self.series.get(pair)
        if exec_series is None or base_series is None:
            return base_series or CandleSeries()
            
        latest_exec = exec_series.latest()
        if latest_exec is None:
            return base_series
            
        parent_open_ms = floor_time_ms(latest_exec.open_time_ms, self.settings.strategy_interval)
        intrabar_candles = [c for c in exec_series if c.open_time_ms >= parent_open_ms]
        if not intrabar_candles:
            return base_series
            
        from app.data.candle_builder import interval_to_ms
        provisional = OHLCVCandle(
            pair=latest_exec.pair,
            interval=self.settings.strategy_interval,
            open_time_ms=parent_open_ms,
            close_time_ms=parent_open_ms + interval_to_ms(self.settings.strategy_interval) - 1,
            open=intrabar_candles[0].open,
            high=max(c.high for c in intrabar_candles),
            low=min(c.low for c in intrabar_candles),
            close=intrabar_candles[-1].close,
            volume=sum(c.volume for c in intrabar_candles),
            quote_volume=sum(c.quote_volume for c in intrabar_candles),
            trade_count=sum(c.trade_count for c in intrabar_candles)
        )
        
        new_series = CandleSeries(list(base_series), maxlen=base_series.maxlen)
        new_series.add(provisional)
        return new_series

    def _map_fill_to_trade_dict(
        self,
        fill: Any,
        candle: OHLCVCandle,
        *,
        position: PaperPosition | None = None,
    ) -> dict[str, Any]:
        position_metadata = (
            position.metadata
            if position is not None and isinstance(position.metadata, dict)
            else {}
        )
        fill_metadata = fill.metadata if isinstance(fill.metadata, dict) else {}
        entry_price = (
            position.entry_price
            if position is not None
            else self._position_entry_prices.get(fill.pair, fill.price)
        )
        entry_candle = self._position_entry_candle.get(fill.pair, self.candle_count)
        if position is not None:
            interval_ms = self._paper_safety_interval_ms()
            hold = max(0, int((fill.timestamp_ms - position.opened_at_ms) // interval_ms))
        else:
            hold = self.candle_count - entry_candle
        gross = fill.realized_pnl
        entry_fee = _decimal_metadata(
            position_metadata.get("entry_fees_total")
            or position_metadata.get("entry_fee"),
            Decimal("0"),
        )
        exit_fee = fill.fee
        total_fees = entry_fee + exit_fee
        net = gross - total_fees
        quote_to_margin_rate = _decimal_metadata(
            position_metadata.get("quote_to_margin_rate")
            or fill_metadata.get("quote_to_margin_rate"),
            self.settings.quote_to_margin_rate,
        )
        unit_contract_value = _decimal_metadata(
            position_metadata.get("unit_contract_value")
            or fill_metadata.get("unit_contract_value"),
            Decimal("1"),
        )
        notional = entry_price * fill.quantity * quote_to_margin_rate * unit_contract_value
        net_pct = (net / notional * Decimal("100")) if notional > 0 else Decimal("0")
        legacy_currency_math = (
            position is not None
            and _decimal_metadata(position_metadata.get("quote_to_margin_rate"), quote_to_margin_rate)
            != self.settings.quote_to_margin_rate
        ) or (
            "quote_to_margin_rate" not in fill_metadata
            and "quote_to_margin_rate" not in position_metadata
            and self.settings.quote_to_margin_rate != Decimal("1")
        )
        
        # direction: if fill.side is SELL, position was LONG (we sold to close); vice versa
        from app.broker.models import PaperOrderSide
        direction = "long" if fill.side == PaperOrderSide.SELL else "short"
        
        # Cleanup entry state after mapping
        self._position_entry_candle.pop(fill.pair, None)
        self._position_entry_prices.pop(fill.pair, None)

        return {
            "fill_id": str(getattr(fill, "fill_id", "") or ""),
            "order_id": str(getattr(fill, "order_id", "") or ""),
            "timestamp": time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(fill.timestamp_ms / 1000)),
            "pair": fill.pair,
            "interval": candle.interval,
            "strategy": self.strategy_engine.strategies[0].name
                        if self.strategy_engine.strategies else "unknown",
            "direction": direction,
            "entry_price": str(entry_price),
            "exit_price": str(fill.price),
            "stop_loss": str(
                fill_metadata.get("atr_stop_loss")
                or position_metadata.get("atr_stop_loss")
                or fill_metadata.get("initial_stop_loss")
                or position_metadata.get("initial_stop_loss")
                or ""
            ),
            "take_profit": str(
                fill_metadata.get("atr_take_profit")
                or position_metadata.get("atr_take_profit")
                or fill_metadata.get("take_profit")
                or position_metadata.get("take_profit")
                or ""
            ),
            "position_size": str(fill.quantity),
            "quantity_unit": _quantity_unit(fill.pair),
            "position_notional": str(notional),
            "notional_currency": self.settings.futures_margin_currency,
            "price_quote_currency": self.settings.price_quote_currency,
            "quote_to_margin_rate": str(quote_to_margin_rate),
            "unit_contract_value": str(unit_contract_value),
            "risk_percent_used": str(position_metadata.get("risk_percent_used") or fill_metadata.get("risk_percent_used") or ""),
            "risk_multiplier": str(position_metadata.get("risk_multiplier") or fill_metadata.get("risk_multiplier") or ""),
            "risk_base_mode": str(position_metadata.get("risk_base_mode") or fill_metadata.get("risk_base_mode") or ""),
            "risk_base_amount": str(position_metadata.get("risk_base_amount") or fill_metadata.get("risk_base_amount") or ""),
            "planned_risk_amount": str(position_metadata.get("planned_risk_amount") or fill_metadata.get("planned_risk_amount") or ""),
            "required_margin": str(position_metadata.get("required_margin") or fill_metadata.get("required_margin") or ""),
            "available_equity": str(position_metadata.get("available_equity") or fill_metadata.get("available_equity") or ""),
            "margin_ok": str(position_metadata.get("margin_ok") or fill_metadata.get("margin_ok") or ""),
            "account_blown": str(position_metadata.get("account_blown") or fill_metadata.get("account_blown") or ""),
            "fee_type": str(fill_metadata.get("fee_type") or ""),
            "fee_rate": str(fill_metadata.get("fee_rate") or ""),
            "fee_gst_rate": str(fill_metadata.get("fee_gst_rate") or ""),
            "effective_fee_rate": str(fill_metadata.get("effective_fee_rate") or ""),
            "entry_fee": str(entry_fee),
            "exit_fee": str(exit_fee),
            "total_fees": str(total_fees),
            "gross_pnl": str(gross),
            "fees": str(total_fees),
            "net_pnl": str(net),
            "net_pnl_pct": str(net_pct.quantize(Decimal("0.0001"))),
            "equity_after": str(self.broker.snapshot({candle.pair: candle.close}).equity),
            "exit_reason": _paper_exit_reason(fill),
            "hold_duration_candles": hold,
            "legacy_currency_math": str(legacy_currency_math).lower(),
        }
