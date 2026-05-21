from __future__ import annotations

import logging
import threading
import json
import time
from dataclasses import asdict, dataclass
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
from app.exchange.coindcx_channels import futures_candle_channel
from app.persistence.paper_state import PaperStateStore
from app.broker.paper import PaperBroker
from app.broker.models import PaperFill, PaperPosition, PaperExecutionReport
from app.risk.manager import RiskManager
from app.risk.models import OpenPosition
from app.strategies.base import StrategyEngine, StrategyContext, SignalAction
from app.strategies.defaults import strategy_engine_for_name
from app.live.summary_logger import PaperTradingSummaryLogger


@dataclass
class LivePaperState:
    running: bool = False
    pair: str = ""
    interval: str = ""
    strategy: str = ""
    candle_count: int = 0
    equity: str = "0"
    starting_equity: str = "0"
    realized_pnl: str = "0"
    fees_paid: str = "0"
    open_positions: int = 0
    total_fills: int = 0
    positions_json: str = "[]"
    last_updated: str = ""
    error: str = ""


_state_lock = threading.Lock()
_live_state = LivePaperState()


def get_live_state() -> dict[str, Any]:
    with _state_lock:
        return asdict(_live_state)


def _update_live_state(**kwargs: Any) -> None:
    with _state_lock:
        for k, v in kwargs.items():
            if hasattr(_live_state, k):
                setattr(_live_state, k, v)


_active_loop: PaperTradingLoop | None = None


def get_active_loop() -> PaperTradingLoop | None:
    return _active_loop


def set_active_loop(loop: PaperTradingLoop | None) -> None:
    global _active_loop
    _active_loop = loop


class PaperTradingLoop:
    def __init__(self, settings: Settings, strategy_name: str = "adaptive_hybrid") -> None:
        self.settings = settings
        self.logger = logging.getLogger(__name__)
        
        self.strategy_engine = strategy_engine_for_name(strategy_name)
        self.risk_manager = RiskManager(settings.risk)
        self.state_store = PaperStateStore()
        
        self.broker = PaperBroker(
            starting_equity=settings.paper_starting_equity,
            state_store=self.state_store,
            logger=logging.getLogger("app.broker.paper")
        )
        
        self.summary_logger = PaperTradingSummaryLogger()
        self.gap_guard = None # Initialized in run() when interval is known
        self.client = CoinDCXFuturesClient(settings)
        self.series = None
        self.execution_series = None # Task 9: Second buffer
        self.candle_count = 0
        self._position_entry_candle: dict[str, int] = {}
        self._current_pair = "unknown"
        self._current_interval = "unknown"
        self._ws_client = None
        self._stop_requested = False
        self._closed_count: int = 0
        self._entries_this_parent_candle: int = 0
        self._current_parent_open_ms: int = 0

    def stop(self) -> None:
        self._stop_requested = True
        if self._ws_client:
            self._ws_client.stop()

    def run(self, pair: str, interval: str) -> None:
        self._current_pair = pair
        self._current_interval = interval
        self._stop_requested = False
        
        # Task 9: If intrabar is enabled, interval is the strategy_interval
        strategy_interval = self.settings.strategy_interval if self.settings.paper_intrabar_enabled else interval
        execution_interval = self.settings.execution_interval if self.settings.paper_intrabar_enabled else interval
        
        _update_live_state(
            running=True,
            pair=pair,
            interval=strategy_interval,
            strategy=self.strategy_engine.strategies[0].name if self.strategy_engine.strategies else "unknown",
            starting_equity=str(self.broker.starting_equity),
            error="",
        )
        
        set_active_loop(self)
        try:
            self.gap_guard = CandleGapGuard(strategy_interval)
            self._warm_up(pair, strategy_interval)
            
            if self.settings.paper_intrabar_enabled:
                self.logger.info("Intrabar execution enabled: %s -> %s", strategy_interval, execution_interval)
                # Warm up execution series too
                self._warm_up_execution(pair, execution_interval)

            pipeline = MarketDataPipeline()
            self._ws_client = CoinDCXFuturesWebSocketClient(self.settings, pipeline=pipeline)
            
            subscriptions = [MarketSubscription(futures_candle_channel(pair, strategy_interval), "candlestick")]
            if self.settings.paper_intrabar_enabled and strategy_interval != execution_interval:
                subscriptions.append(MarketSubscription(futures_candle_channel(pair, execution_interval), "candlestick"))
            
            self.logger.info("Starting live paper loop for %s %s", pair, strategy_interval)
            
            original_handle_raw = pipeline.handle_raw
            def hooked_handle_raw(event_name: str, payload: Any):
                events = original_handle_raw(event_name, payload)
                for event in events:
                    if getattr(event, "event_type", None) == "candle":
                        from app.data.candle_builder import OHLCVCandle
                        candle = OHLCVCandle.from_candle_event(event)
                        self._on_candle(candle)
                return events
            
            pipeline.handle_raw = hooked_handle_raw

            try:
                self._ws_client.run(subscriptions)
            except KeyboardInterrupt:
                self.logger.info("Graceful shutdown requested.")
            
            snapshot = self.broker.snapshot()
            self.logger.info(
                "Final Paper State: equity=%.2f, open_positions=%d", 
                snapshot.equity, snapshot.open_position_count
            )
        finally:
            set_active_loop(None)
            self._ws_client = None
            _update_live_state(running=False, error="")

    def _warm_up(self, pair: str, interval: str, lookback: int = 200) -> None:
        from app.backtest.data_loader import (
            load_historical_candle_series,
            REST_RESOLUTION_BY_INTERVAL,
        )
        _update_live_state(last_updated="warming up...")
        self.logger.info("Warming up with %d REST candles for %s %s...", lookback, pair, interval)

        if interval not in REST_RESOLUTION_BY_INTERVAL:
            raise ValueError(
                f"Interval {interval!r} not supported for REST warmup. "
                f"Choose one of: {sorted(REST_RESOLUTION_BY_INTERVAL)}"
            )

        self.series = load_historical_candle_series(
            client=self.client,
            pair=pair,
            interval=interval,
            lookback=lookback,
        )
        self.logger.info("Warmed up with %d candles", len(self.series))
        _update_live_state(candle_count=0)  # reset candle count to 0 (warmup doesn't count)

    def _warm_up_execution(self, pair: str, interval: str, lookback: int = 200) -> None:
        from app.backtest.data_loader import load_historical_candle_series
        self.logger.info("Warming up execution series (%s)...", interval)
        self.execution_series = load_historical_candle_series(
            client=self.client,
            pair=pair,
            interval=interval,
            lookback=lookback,
        )

    def _on_candle(self, candle: OHLCVCandle) -> None:
        if self._stop_requested:
            if self._ws_client:
                self._ws_client.stop()
            return

        if self.series is None or self.series.latest() is None:
            return

        # Determine which stream this belongs to
        is_strategy = (candle.interval == self._current_interval or self._current_interval == "unknown")
        is_execution = (self.settings.paper_intrabar_enabled and candle.interval == self.settings.execution_interval)

        if not self.settings.paper_intrabar_enabled:
            # Single stream mode: every candle is both strategy and execution
            is_strategy = True
            is_execution = True

        if is_strategy:
            self._handle_strategy_candle(candle)

        if is_execution:
            self._handle_execution_candle(candle)
    def _handle_strategy_candle(self, candle: OHLCVCandle) -> None:
        if self.series is None or self.series.latest() is None:
            return

        prev = self.series.latest()
        
        # Gap check (only for strategy series for now)
        gap_result = self.gap_guard.check(prev, candle)
        if gap_result.has_gap:
            self.logger.warning("Gap detected in strategy series! Re-fetching...")
            self._warm_up(candle.pair, candle.interval)
            return

        if candle.close_time_ms <= prev.close_time_ms:
            return

        self.series.add(candle)
        
        # Task 9: New strategy candle resets entry counter
        if self.settings.paper_intrabar_enabled:
            if candle.open_time_ms > self._current_parent_open_ms:
                self._current_parent_open_ms = candle.open_time_ms
                self._entries_this_parent_candle = 0

    def _handle_execution_candle(self, candle: OHLCVCandle) -> None:
        if not self.settings.paper_intrabar_enabled:
            # If not intrabar, we just use the main logic flow
            self.candle_count += 1
            self._process_trading_candle(candle)
            return

        if self.execution_series is None:
            return
        
        prev = self.execution_series.latest()
        if prev and candle.close_time_ms <= prev.close_time_ms:
            return
            
        self.execution_series.add(candle)
        self.candle_count += 1
        
        # Task 9: Detect parent candle boundary
        from app.data.candle_builder import floor_time_ms
        parent_open_ms = floor_time_ms(candle.open_time_ms, self.settings.strategy_interval)
        if parent_open_ms > self._current_parent_open_ms:
            self._current_parent_open_ms = parent_open_ms
            self._entries_this_parent_candle = 0

        self._process_trading_candle(candle)

    def _process_trading_candle(self, candle: OHLCVCandle) -> None:
        # 1. Indicators
        # In intrabar mode, we use execution_series for trailing but strategy_series for context
        main_series = self.execution_series if self.settings.paper_intrabar_enabled else self.series
        if main_series is None: return
        
        # 2. Strategy Context
        if self.settings.paper_intrabar_enabled:
            # HTF Context: Either last confirmed closed or provisional
            htf_series = self.series
            if self.settings.use_partial_parent_candle:
                # Build provisional candle from execution series
                htf_series = self._build_provisional_htf_series()
            
            indicators = latest_indicator_snapshot(htf_series)
            context = StrategyContext(
                pair=candle.pair,
                interval=self.settings.strategy_interval,
                candles=htf_series,
                indicators=indicators,
                features={}
            )
        else:
            indicators = latest_indicator_snapshot(self.series)
            context = StrategyContext(
                pair=candle.pair,
                interval=candle.interval,
                candles=self.series,
                indicators=indicators,
                features={}
            )
        
        signals = self.strategy_engine.evaluate(context)
        
        # 3. Risk & Execution
        for signal in signals:
            if signal.action == SignalAction.HOLD:
                continue
            
            # Entry Limit
            if self.settings.paper_intrabar_enabled:
                if self._entries_this_parent_candle >= self.settings.max_entries_per_parent_candle:
                    continue

            snapshot = self.broker.snapshot({candle.pair: candle.close})
            open_positions_tuple = tuple(
                OpenPosition(
                    pair=p.pair,
                    direction=p.direction,
                    strategy_name=p.strategy_name,
                    notional=p.quantity * candle.close,
                )
                for p in self.broker.open_positions()
            )
            decision = self.risk_manager.evaluate_signal(
                signal,
                account_equity=snapshot.equity,
                available_equity=snapshot.equity,
                open_positions=open_positions_tuple,
                daily_realized_pnl=self.broker.realized_pnl,
                trading_mode=self.settings.trading_mode,
                live_trading_enabled=self.settings.live_trading_allowed,
            )
            
            if decision.approved:
                report = self.broker.execute_decision(
                    decision, market_price=candle.close, timestamp_ms=candle.close_time_ms
                )
                if report.accepted:
                    self._position_entry_candle[candle.pair] = self.candle_count
                    if self.settings.paper_intrabar_enabled:
                        self._entries_this_parent_candle += 1

        open_before = {p.pair: p for p in self.broker.open_positions()}
        
        # Task 9: Stop/TP checks happen on the current candle (which is the execution candle in intrabar mode)
        reports = self.broker.process_candle(candle)
        for report in reports:
            if report.accepted and report.position is not None:
                closed_pair = report.position.pair
                prev_pos = open_before.get(closed_pair)
                if prev_pos is not None and report.fill is not None:
                    trade_dict = self._map_fill_to_trade_dict(
                        fill=report.fill,
                        position=prev_pos,
                        candle=candle,
                    )
                    self.summary_logger.on_trade_closed(trade_dict)
                    self._position_entry_candle.pop(closed_pair, None)
                    self._closed_count += 1

        snapshot = self.broker.snapshot({candle.pair: candle.close})
        self.summary_logger.on_candle(
            self.candle_count, 
            snapshot.equity, 
            snapshot.open_position_count,
            self.broker.realized_pnl
        )
        self.summary_logger.check_drawdown(snapshot.equity)
        
        # Update live state
        positions_list = [
            {
                "pair": p.pair,
                "direction": p.direction.value,
                "quantity": str(p.quantity),
                "entry_price": str(p.entry_price),
                "unrealized_pnl": str(p.unrealized_pnl(candle.close)),
                "strategy": p.strategy_name,
            }
            for p in self.broker.open_positions()
        ]
        
        # In intrabar mode, display BOTH intervals or just strategy
        display_interval = f"{self.settings.strategy_interval}/{self.settings.execution_interval}" if self.settings.paper_intrabar_enabled else self._current_interval
        
        _update_live_state(
            interval=display_interval,
            candle_count=self.candle_count,
            equity=str(snapshot.equity),
            realized_pnl=str(self.broker.realized_pnl),
            fees_paid=str(self.broker.fees_paid),
            open_positions=len(self.broker.positions),
            total_fills=len(self.broker.fills),
            positions_json=json.dumps(positions_list),
            last_updated=datetime.now(timezone.utc).isoformat(),
        )

    def _build_provisional_htf_series(self) -> CandleSeries:
        # Build a provisional parent candle from execution series and append it to a copy of strategy series
        from app.data.candle_builder import floor_time_ms
        if self.execution_series is None or self.series is None:
            return self.series
            
        latest_exec = self.execution_series.latest()
        if latest_exec is None:
            return self.series
            
        parent_open_ms = floor_time_ms(latest_exec.open_time_ms, self.settings.strategy_interval)
        
        # Find all execution candles belonging to this parent
        intrabar_candles = [c for c in self.execution_series if c.open_time_ms >= parent_open_ms]
        if not intrabar_candles:
            return self.series
            
        # Aggregate
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
        
        # Return a copy with the provisional candle added
        new_series = CandleSeries(list(self.series), maxlen=self.series.maxlen)
        new_series.add(provisional)
        return new_series

    def _map_fill_to_trade_dict(
        self,
        fill: PaperFill,
        position: PaperPosition,
        candle: OHLCVCandle,
    ) -> dict[str, Any]:
        import time
        gross_pnl = fill.realized_pnl
        net_pnl = gross_pnl - fill.fee
        entry_notional = position.entry_price * position.quantity
        net_pnl_pct = (net_pnl / entry_notional * Decimal("100")) if entry_notional > 0 else Decimal("0")
        entry_candle_num = self._position_entry_candle.get(position.pair, self.candle_count)
        hold_candles = self.candle_count - entry_candle_num
        return {
            "timestamp": time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(fill.timestamp_ms / 1000)),
            "pair": position.pair,
            "interval": candle.interval,
            "strategy": position.strategy_name,
            "direction": position.direction.value,
            "entry_price": str(position.entry_price),
            "exit_price": str(fill.price),
            "stop_loss": str(position.stop_loss) if position.stop_loss else "",
            "take_profit": str(position.take_profit) if position.take_profit else "",
            "position_size": str(position.quantity),
            "gross_pnl": str(gross_pnl),
            "fees": str(fill.fee),
            "net_pnl": str(net_pnl),
            "net_pnl_pct": str(net_pnl_pct.quantize(Decimal("0.0001"))),
            "equity_after": str(self.broker.snapshot({candle.pair: candle.close}).equity),
            "exit_reason": fill.metadata.get("exit_trigger_type", "signal"),
            "hold_duration_candles": hold_candles,
        }
