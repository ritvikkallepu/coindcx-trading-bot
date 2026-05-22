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
from app.broker.models import PaperFill, PaperPosition, PaperExecutionReport
from app.risk.manager import RiskManager
from app.risk.models import OpenPosition
from app.risk.limits import is_entry_signal, is_exit_signal
from app.strategies.base import StrategyEngine, StrategyContext, SignalAction
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
    fees_paid: str = "0"
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
        
        self._init_audit_log()

        # Task 4: Restore state
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
        return [
            {
                "pair": p.pair,
                "direction": p.direction.value,
                "quantity": str(p.quantity),
                "entry_price": str(p.entry_price),
                "unrealized_pnl": str(p.unrealized_pnl(self.broker.mark_price_for(p.pair, p.entry_price))),
                "notional": str(abs(p.quantity * self.broker.mark_price_for(p.pair, p.entry_price))),
                "strategy": p.strategy_name,
                "stop_loss": str(p.stop_loss) if p.stop_loss else None,
                "take_profit": str(p.take_profit) if p.take_profit else None,
            }
            for p in self.broker.open_positions()
        ]

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
            "fees_paid": str(self.broker.fees_paid),
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
            "last_updated": datetime.now(timezone.utc).isoformat(),
            # Mirror some broker state for easy JSON access if needed
            "realized_pnl": str(self.broker.realized_pnl),
            "fees_paid": str(self.broker.fees_paid),
            "equity": str(snapshot.equity),
        }
        self.session_store.save_session(state)

    def stop(self) -> None:
        self._stop_requested = True
        if self._ws_client:
            self._ws_client.stop()
        self._save_session()

    def run(self, pairs: str | list[str], interval: str) -> None:
        if isinstance(pairs, str):
            pairs = [pairs]
        
        self._watchlist = pairs
        self._current_interval = interval
        self._stop_requested = False
        
        # Task 9: If intrabar is enabled, interval is the strategy_interval
        strategy_interval = self.settings.strategy_interval if self.settings.paper_intrabar_enabled else interval
        
        _update_live_state(
            running=True,
            pair=", ".join(pairs),
            interval=interval,
            strategy=self.strategy_engine.strategies[0].name if self.strategy_engine.strategies else "unknown",
            starting_equity=str(self.broker.starting_equity),
            error="",
        )
        self._publish_live_snapshot(interval=interval, last_updated="starting")
        
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
            self._ws_client = CoinDCXFuturesWebSocketClient(self.settings, pipeline=pipeline)
            
            self.logger.info("Starting live multi-pair paper loop for %s @ %s", pairs, strategy_interval)
            
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
                        for closed_candle in self._closed_candles_from_stream(candle):
                            self._on_candle(closed_candle)
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

    def _on_candle(self, candle: OHLCVCandle) -> None:
        if self._stop_requested:
            if self._ws_client:
                self._ws_client.stop()
            return

        if not candle.is_closed:
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
            self._handle_execution_candle(candle)
            
        self.broker.update_mark_prices({candle.pair: candle.close})
        self._publish_live_snapshot(last_updated=datetime.now(timezone.utc).isoformat())

    def _handle_strategy_candle(self, candle: OHLCVCandle) -> None:
        pair = candle.pair
        series = self.series.get(pair)
        if series is None:
            return

        prev = series.latest()
        if prev is not None:
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

    def _handle_execution_candle(self, candle: OHLCVCandle) -> None:
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
        if prev and candle.close_time_ms <= prev.close_time_ms:
            return
            
        series.add(candle)
        self._record_live_candle(candle)
        self.candle_count += 1
        
        from app.data.candle_builder import floor_time_ms
        parent_open_ms = floor_time_ms(candle.open_time_ms, self.settings.strategy_interval)
        if parent_open_ms > self._current_parent_open_ms.get(pair, 0):
            self._current_parent_open_ms[pair] = parent_open_ms
            self._entries_this_parent_candle[pair] = 0

        self._process_trading_candle(candle)

    def _process_trading_candle(self, candle: OHLCVCandle) -> None:
        pair = candle.pair
        main_series = self.execution_series.get(pair) if self.settings.paper_intrabar_enabled else self.series.get(pair)
        if main_series is None: return
        
        if self.settings.paper_intrabar_enabled:
            htf_series = self.series[pair]
            if self.settings.use_partial_parent_candle:
                htf_series = self._build_provisional_htf_series(pair)
            
            indicators = latest_indicator_snapshot(htf_series)
            context = StrategyContext(
                pair=pair,
                interval=self.settings.strategy_interval,
                candles=htf_series,
                indicators=indicators,
                features=self._strategy_features(pair),
            )
        else:
            indicators = latest_indicator_snapshot(self.series[pair])
            context = StrategyContext(
                pair=pair,
                interval=candle.interval,
                candles=self.series[pair],
                indicators=indicators,
                features=self._strategy_features(pair),
            )
        
        atr_val = indicators.atr
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
                requested_leverage=self.settings.paper_leverage,
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
                    elif is_exit_signal(signal):
                        trade_dict = self._map_fill_to_trade_dict(
                            fill=report.fill,
                            candle=candle,
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

        reports = self.broker.process_candle(candle)
        for report in reports:
            if report.accepted and report.fill is not None:
                if report.fill.realized_pnl != 0:
                    trade_dict = self._map_fill_to_trade_dict(
                        fill=report.fill,
                        candle=candle,
                    )
                    self.summary_logger.on_trade_closed(trade_dict)
                    self._closed_count += 1

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
                "reversal_breakout_volume_ratio": Decimal("2.0"),
                "reversal_breakout_body_ratio": Decimal("0.65"),
                "reversal_breakout_close_position_ratio": Decimal("0.70"),
                "reversal_breakout_risk_multiplier": Decimal("0.50"),
                "reversal_breakout_max_extension_atr": Decimal("2.2"),
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

    def _map_fill_to_trade_dict(self, fill: Any, candle: OHLCVCandle) -> dict[str, Any]:
        entry_price = self._position_entry_prices.get(fill.pair, fill.price)
        entry_candle = self._position_entry_candle.get(fill.pair, self.candle_count)
        hold = self.candle_count - entry_candle
        gross = fill.realized_pnl
        net = gross - fill.fee
        notional = entry_price * fill.quantity
        net_pct = (net / notional * Decimal("100")) if notional > 0 else Decimal("0")
        
        # direction: if fill.side is SELL, position was LONG (we sold to close); vice versa
        from app.broker.models import PaperOrderSide
        direction = "long" if fill.side == PaperOrderSide.SELL else "short"
        
        # Cleanup entry state after mapping
        self._position_entry_candle.pop(fill.pair, None)
        self._position_entry_prices.pop(fill.pair, None)

        return {
            "timestamp": time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(fill.timestamp_ms / 1000)),
            "pair": fill.pair,
            "interval": candle.interval,
            "strategy": self.strategy_engine.strategies[0].name
                        if self.strategy_engine.strategies else "unknown",
            "direction": direction,
            "entry_price": str(entry_price),
            "exit_price": str(fill.price),
            "stop_loss": str(fill.metadata.get("atr_stop_loss") or fill.metadata.get("initial_stop_loss") or ""),
            "take_profit": str(fill.metadata.get("atr_take_profit") or fill.metadata.get("take_profit") or ""),
            "position_size": str(fill.quantity),
            "gross_pnl": str(gross),
            "fees": str(fill.fee),
            "net_pnl": str(net),
            "net_pnl_pct": str(net_pct.quantize(Decimal("0.0001"))),
            "equity_after": str(self.broker.snapshot({candle.pair: candle.close}).equity),
            "exit_reason": fill.metadata.get("exit_trigger_type") or fill.metadata.get("reason") or "signal",
            "hold_duration_candles": hold,
        }
