from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping

from app.config import RiskSettings, Settings
from app.data.candle_builder import CandleSeries, OHLCVCandle, interval_to_ms, rest_rows_to_series
from app.data.gap_guard import CandleGapGuard
from app.data.indicators import latest_indicator_snapshot
from app.data.pipeline import MarketDataPipeline
from app.execution.live import (
    LiveExecutionEngine, 
    LiveExecutionReport, 
    LivePositionSnapshot, 
    LiveOrderSnapshot,
    LiveSyncResult
)
from app.exchange.coindcx_rest import CoinDCXFuturesClient
from app.exchange.coindcx_ws import CoinDCXFuturesWebSocketClient, MarketSubscription
from app.exchange.coindcx_channels import futures_candle_channel, futures_orderbook_channel
from app.risk.manager import RiskManager
from app.risk.models import RiskContext, OpenPosition
from app.risk.limits import is_entry_signal, is_exit_signal
from app.strategies.base import StrategyEngine, StrategyContext, SignalAction, SignalDirection, StrategySignal
from app.strategies.defaults import strategy_engine_for_name
from app.utils.json import to_jsonable
from app.live.state import _update_live_state, reset_live_state
from app.alerts.console import ConsoleAlert
from app.alerts.base import AlertInterface
from app.alerts.telegram import TelegramAlert

logger = logging.getLogger(__name__)

class LiveTradingLoop:
    def __init__(
        self,
        settings: Settings,
        strategy_name: str = "hybrid_meta_v2",
        pairs: list[str] = ["B-BTC_USDT"],
        interval: str = "5m",
        starting_equity: Decimal = Decimal("100000"),
        leverage: Decimal = Decimal("3"),
        alert_interface: AlertInterface | None = None,
    ) -> None:
        self.settings = settings
        self.strategy_name = strategy_name
        self.pairs = pairs
        self.interval = interval
        self.starting_equity = starting_equity
        self.leverage = leverage
        
        # Alerts
        self.alert = alert_interface or ConsoleAlert()
             
        self.client = CoinDCXFuturesClient(settings)
        self.execution_engine = LiveExecutionEngine(self.client, settings)
        self.risk_manager = RiskManager(settings.risk)
        self.strategy_engine = strategy_engine_for_name(strategy_name)
        
        self.series_by_pair: dict[str, CandleSeries] = {p: CandleSeries(maxlen=500) for p in pairs}
        self.execution_series_by_pair: dict[str, CandleSeries] = {p: CandleSeries(maxlen=500) for p in pairs}
        self.gap_guard_by_pair: dict[str, CandleGapGuard] = {p: CandleGapGuard(interval) for p in pairs}
        
        self.state_path = Path("data/live_state.json")
        self.local_state: dict[str, Any] = {
            "positions": {},
            "orders": [],
            "locked_profit": "0",
            "tradable_base": str(starting_equity),
            "daily_loss_from_tradable_base": "0",
            "kill_switch_active": settings.risk.live_kill_switch
        }
        self._last_reconcile_ms = 0
        self._last_ws_message_ms = int(time.time() * 1000)
        self._last_ws_message_ms_by_pair: dict[str, int] = {p: int(time.time() * 1000) for p in pairs}
        self._last_processed_candle_ms: dict[str, int] = {} # pair: close_time_ms
        self._last_skipped_candle_ms: dict[str, int] = {} # pair: close_time_ms
        self._dry_run_positions: dict[str, dict[str, Any]] = {} # pair: pos_dict
        self._is_warming_up: dict[str, bool] = {p: True for p in pairs}
        
        self.load_local_state()
        self.kill_switch_active = self.local_state.get("kill_switch_active", settings.risk.live_kill_switch)
        
        self._stop_requested = False
        self._reconcile_thread = None
        self._ws_client = None
        
        self.candle_count = 0
        
    def load_local_state(self) -> None:
        if self.state_path.exists():
            try:
                saved = json.loads(self.state_path.read_text())
                self.local_state.update(saved)
                
                # Recover last processed candle times if present
                saved_last_candles = self.local_state.get("last_processed_strategy_candle_close_time", {})
                if isinstance(saved_last_candles, dict):
                    self._last_processed_candle_ms.update({k: int(v) for k, v in saved_last_candles.items()})

                # Recover dry run positions from state if any
                for pair, pos in self.local_state.get("positions", {}).items():
                     if self.settings.live_pilot_dry_run:
                          self._dry_run_positions[pair] = pos
                logger.info("Loaded local live state from %s", self.state_path)
            except Exception as exc:
                logger.error("Failed to load local state: %s", exc)

    def save_local_state(self) -> None:
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text(json.dumps(to_jsonable(self.local_state), indent=2))
        except Exception as exc:
            logger.error("Failed to save local state: %s", exc)

    def run(self) -> None:
        mode_str = "DRY-RUN" if self.settings.live_pilot_dry_run else "REAL-TIME LIVE"
        logger.info("Starting %s trading loop for %s @ %s", mode_str, ", ".join(self.pairs), self.interval)
        self.alert.alert(f"Starting {mode_str} trading loop for {len(self.pairs)} pairs @ {self.interval}")
        
        if not self.settings.live_pilot_dry_run and self.settings.live_trading_enabled:
             self.alert.alert("!!! REAL LIVE TRADING IS ENABLED !!!", level="CRITICAL")

        # Initial sync
        self.reconcile()
        
        # Warmup
        self._warm_up()
        
        # Start reconciliation thread
        self._reconcile_thread = threading.Thread(target=self._reconcile_loop, daemon=True, name="live-reconcile")
        self._reconcile_thread.start()
        
        # Build pipeline
        pipeline = MarketDataPipeline(
            on_event=self._on_market_event
        )
        
        # Subscriptions for ALL pairs
        subscriptions = []
        for pair in self.pairs:
            logger.info("[%s] Preparing WebSocket subscriptions...", pair)
            subscriptions.extend([
                MarketSubscription(futures_candle_channel(pair, self.interval), "candlestick"),
                MarketSubscription(futures_candle_channel(pair, self.settings.execution_interval), "candlestick"),
                MarketSubscription(futures_orderbook_channel(pair, 50), "depth-snapshot"),
                MarketSubscription(futures_orderbook_channel(pair, 50), "depth-update"),
            ])
        
        self._ws_client = CoinDCXFuturesWebSocketClient(self.settings, pipeline=pipeline)
        self._last_ws_message_ms = int(time.time() * 1000)
        for pair in self.pairs:
             self._last_ws_message_ms_by_pair[pair] = self._last_ws_message_ms
        
        try:
            self._ws_client.run(subscriptions)
        except Exception as exc:
            logger.exception("Live loop WebSocket error: %s", exc)
            self.alert.alert(f"Live loop WebSocket error: {exc}", level="ERROR")
        finally:
            self.stop()

    def stop(self) -> None:
        self._stop_requested = True
        if self._ws_client:
            self._ws_client.stop()
        logger.info("Live trading loop stop requested.")

    def _reconcile_loop(self) -> None:
        while not self._stop_requested:
            try:
                now_ms = int(time.time() * 1000)
                
                # Check for WS silence and fallback to polling PER PAIR
                for pair in self.pairs:
                     last_ms = self._last_ws_message_ms_by_pair.get(pair, 0)
                     silence_duration = (now_ms - last_ms) // 1000
                     
                     if silence_duration > self.settings.live_rest_poll_seconds:
                          logger.warning("[%s] WebSocket silent for %d seconds. Polling REST fallback...", pair, silence_duration)
                          self._poll_rest_candles_for_pair(pair)
                          # Reset timer to avoid spamming polls
                          self._last_ws_message_ms_by_pair[pair] = now_ms

                     # Check for full interval silence
                     interval_ms = interval_to_ms(self.interval)
                     if silence_duration >= (interval_ms // 1000):
                          logger.info("[%s] No WebSocket activity for one full interval (%s). Check connection.", pair, self.interval)
                          # We don't reset timer here to avoid suppressing the INFO log if it persists? 
                          # Actually INFO log should probably be once per interval.
                
                if now_ms - self._last_reconcile_ms > self.settings.live_reconcile_seconds * 1000:
                    self.reconcile()
                    self._last_reconcile_ms = now_ms
            except Exception as exc:
                logger.error("Reconciliation loop error: %s", exc)
            time.sleep(1)

    def reconcile(self) -> None:
        for pair in self.pairs:
            self._reconcile_pair(pair)
        
        self._reconcile_daily_loss()
        self.save_local_state()
        self._update_dashboard_state()

    def _reconcile_pair(self, pair: str) -> None:
        # 1. Skip exchange sync for pure dry-run positions
        last_known_pos = self.local_state["positions"].get(pair)
        if last_known_pos and last_known_pos.get("source") == "dry_run":
             return

        if not self.settings.live_trading_allowed and self.settings.live_pilot_dry_run:
             return

        logger.debug("[%s] Reconciling state with exchange...", pair)
        try:
            # 2. Sync Position
            pos = self.execution_engine.sync.fetch_position(pair)
            exchange_pos_exists = pos and pos.is_open
            
            last_known_exists = last_known_pos and abs(Decimal(str(last_known_pos.get("active_pos", 0)))) > 0
            
            if exchange_pos_exists:
                if not last_known_exists:
                     logger.warning("[%s] Exchange has position but local state does not. Syncing.", pair)
                     self.alert.alert(f"Exchange position mismatch for {pair}: Exchange has position, local does not.", level="WARNING")
                
                pos_dict = pos.to_dict()
                pos_dict["source"] = "exchange"
                self.local_state["positions"][pair] = pos_dict
                
                # Safety rule 7: No stop loss?
                if not pos.has_stop_loss and self.settings.risk.live_require_stop_loss:
                     self.alert.alert(f"CRITICAL: Position open for {pair} but STOP LOSS IS MISSING!", level="CRITICAL")
                     self.kill_switch_active = True
                     self.local_state["kill_switch_active"] = True
            else:
                # Position is closed or doesn't exist on exchange
                if last_known_exists:
                     # Only Mark as closed if it was an exchange position
                     if last_known_pos.get("source") == "exchange":
                          logger.info("[%s] Local state position closed on exchange. Processing accounting...", pair)
                          self._process_closed_position(pair, last_known_pos)
                          self.local_state["positions"].pop(pair, None)
                          self.local_state.get("position_metadata", {}).pop(pair, None)
            
            # 3. Handle Kill Switch for this position
            if self.kill_switch_active or self.local_state.get("kill_switch_active"):
                 if self.settings.risk.live_close_on_kill_switch and exchange_pos_exists:
                      logger.warning("Kill switch: Flattening position for %s", pair)
                      self.client.exit_position(pos.position_id)
                 if self.settings.risk.live_cancel_orders_on_kill_switch:
                      if exchange_pos_exists:
                           self.client.cancel_all_open_orders_for_position(pos.position_id)

        except Exception as exc:
            logger.error("[%s] Reconciliation failed: %s", pair, exc)

    def _process_closed_position(self, pair: str, local_pos: dict[str, Any]) -> None:
        """Fetch results from exchange and update virtual accounting."""
        if local_pos.get("source") == "dry_run":
             return

        try:
             # Fetch last filled orders to find PnL. We check both buy and sell.
             raw_orders = []
             for side in ["buy", "sell"]:
                 try:
                     raw_orders.extend(self.client.list_orders(
                          status="filled",
                          side=side,
                          margin_currencies=[self.settings.futures_margin_currency],
                          size=10
                     ))
                 except Exception:
                     pass
             
             orders = [o for o in raw_orders if o.get("pair") == pair]
             # Sort by descending timestamp if possible
             orders.sort(key=lambda x: x.get("updated_at", 0), reverse=True)
             
             if not orders:
                  logger.warning("[%s] No filled orders found to process closed position accounting. Real exchange fills might be delayed.", pair)
                  return

             self.alert.alert(f"Position closed for {pair}. Reconciling PnL from orders...", level="INFO")
             
             last_order = orders[0]
             pnl = Decimal(str(last_order.get("realized_pnl", 0)))
             fee = Decimal(str(last_order.get("fee_amount", 0)))
             
             self._update_accounting_balances(pnl - fee)
             
        except Exception as exc:
             logger.error("[%s] Failed to process closed position accounting: %s", pair, exc)
             self.alert.alert(f"Accounting reconciliation FAILED for {pair}: {exc}", level="ERROR")

    def _update_accounting_balances(self, net_trade_pnl: Decimal) -> None:
        locked = Decimal(self.local_state.get("locked_profit", "0"))
        base = Decimal(self.local_state.get("tradable_base", str(self.starting_equity)))
        daily_loss = Decimal(self.local_state.get("daily_loss_from_tradable_base", "0"))

        if net_trade_pnl > 0:
             locked += net_trade_pnl
             logger.info("Profit locked: +%.2f INR", net_trade_pnl)
             self.alert.alert(f"Profit locked: +{net_trade_pnl:.2f} INR")
        else:
             base += net_trade_pnl
             daily_loss += abs(net_trade_pnl)
             logger.warning("Loss from tradable base: %.2f INR", net_trade_pnl)
             self.alert.alert(f"Loss from tradable base: {net_trade_pnl:.2f} INR", level="WARNING")

        self.local_state["locked_profit"] = str(locked)
        self.local_state["tradable_base"] = str(base)
        self.local_state["daily_loss_from_tradable_base"] = str(daily_loss)
        
        # Check daily loss limit
        if daily_loss >= self.settings.risk.live_max_daily_loss_inr:
             self.alert.alert(f"DAILY LOSS LIMIT REACHED ({daily_loss:.2f} >= {self.settings.risk.live_max_daily_loss_inr:.2f}). Blocking trading.", level="CRITICAL")
             self.kill_switch_active = True
             self.local_state["kill_switch_active"] = True

    def _reconcile_daily_loss(self) -> None:
        from datetime import timedelta
        now = datetime.now(timezone.utc)
        last_reset = self.local_state.get("last_pnl_reset_day", "")
        today = now.date().isoformat()
        
        if last_reset != today:
             logger.info("New day detected (%s). Resetting daily loss tracking.", today)
             self.local_state["daily_loss_from_tradable_base"] = "0"
             self.local_state["last_pnl_reset_day"] = today

    def _poll_rest_candles(self) -> None:
        for pair in self.pairs:
            self._poll_rest_candles_for_pair(pair)

    def _poll_rest_candles_for_pair(self, pair: str) -> None:
        try:
             from datetime import timedelta
             from app.backtest.data_loader import rows_from_candles_response
             now = datetime.now(timezone.utc)
             res = self.client.get_candles(
                  pair=pair,
                  from_ts=int((now - timedelta(seconds=interval_to_ms(self.interval)*5/1000)).timestamp()),
                  to_ts=int(now.timestamp()),
                  resolution="60" if self.interval == "1h" else "1" 
             )
             
             rows = rows_from_candles_response(res)
             if rows:
                  series = rest_rows_to_series(pair=pair, interval=self.interval, rows=rows)
                  for candle in series:
                       self._on_candle(candle, source="rest_fallback")
             
             logger.debug("[%s] Polled REST candles for fallback.", pair)
        except Exception as exc:
             logger.error("[%s] REST poll fallback failed: %s", pair, exc)

    def _warm_up(self) -> None:
        from app.backtest.data_loader import load_historical_candle_series
        for pair in self.pairs:
            logger.info("[%s] Warming up indicators...", pair)
            try:
                warmup_series = load_historical_candle_series(
                    client=self.client, 
                    pair=pair, 
                    interval=self.interval, 
                    lookback=100
                )
                for c in warmup_series:
                    # Warmup candles are loaded into series but do NOT trigger evaluation
                    self.series_by_pair[pair].add(c)
                    self._last_processed_candle_ms[pair] = max(self._last_processed_candle_ms.get(pair, 0), c.close_time_ms)
                    logger.debug("[%s] Warmup candle loaded, no trading: close=%d", pair, c.close_time_ms)
                
                self._is_warming_up[pair] = False
                logger.info("[%s] Warmup complete. Indicators ready.", pair)
            except Exception as exc:
                logger.error("[%s] Warmup failed: %s", pair, exc)

    def _on_market_event(self, event: Any) -> None:
        self._last_ws_message_ms = int(time.time() * 1000)
        
        if event.event_type == "candle":
             # Mark activity for this pair
             if event.pair in self._last_ws_message_ms_by_pair:
                  self._last_ws_message_ms_by_pair[event.pair] = self._last_ws_message_ms
                  
             if event.pair in self.pairs:
                  candle = OHLCVCandle.from_candle_event(event)
                  self._on_candle(candle, source="websocket")
        
        elif event.event_type == "trade":
             # "Soften" the loop: evaluate on every trade tick as well
             if event.pair in self.pairs:
                  # Update activity
                  self._last_ws_message_ms_by_pair[event.pair] = self._last_ws_message_ms
                  # Feed trade to series (this updates the current partial candle)
                  self.series_by_pair[event.pair].apply_trade(event)
                  # Trigger an intrabar evaluation
                  current_partial = self.series_by_pair[event.pair].current
                  if current_partial:
                       self._on_candle(current_partial, source="websocket_tick")

    def _on_candle(self, candle: OHLCVCandle, source: str = "unknown") -> None:
        if candle.pair not in self.pairs:
            return
            
        now_ms = int(time.time() * 1000)
        open_time_str = datetime.fromtimestamp(candle.open_time_ms / 1000, timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
        close_time_str = datetime.fromtimestamp(candle.close_time_ms / 1000, timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
        now_str = datetime.fromtimestamp(now_ms / 1000, timezone.utc).strftime('%Y-%m-%d %H:%M:%S')

        log_prefix = f"[{candle.pair} {candle.interval}] source={source} is_closed={candle.is_closed} open={open_time_str} close={close_time_str} now={now_str}"

        # Add verbose debug log for every raw candle event
        logger.debug("%s: raw candle event received", log_prefix)

        # 1. Check Dry-Run Stops for any accepted candle (regardless of interval)
        self._check_dry_run_stops(candle)

        if candle.interval == self.interval:
            # Boundary alignment (e.g. 5m candles must start at :00, :05, etc.)
            interval_ms = interval_to_ms(self.interval)
            if candle.open_time_ms % interval_ms != 0:
                 return

            # INTRABAR SOFTENING:
            # We evaluate both closed AND partial candles for entries.
            # But we still deduplicate closed candles to avoid redundant logging/history.
            
            if candle.is_closed:
                last_ts = self._last_processed_candle_ms.get(candle.pair, 0)
                if candle.close_time_ms <= last_ts:
                    return
                    
                logger.info("%s: accepted closed strategy candle", log_prefix)
                self.series_by_pair[candle.pair].add(candle)
                self.candle_count += 1
                self._last_processed_candle_ms[candle.pair] = candle.close_time_ms
                self.local_state["last_processed_strategy_candle_close_time"] = self._last_processed_candle_ms
                
                # Full evaluation on close (always allowed)
                self._process_trading_candle(candle, allow_entries=True)
            
            else:
                # Intrabar partial candle evaluation
                # Stale signal guard: only evaluate if the candle is very fresh
                is_fresh = (now_ms - candle.open_time_ms) < (interval_ms + 10000) # Open time within current interval window
                if is_fresh:
                     # Only log at debug to avoid console spam, unless a trade happens
                     logger.debug("%s: intrabar strategy evaluation", log_prefix)
                     self._process_trading_candle(candle, allow_entries=True)

        elif candle.interval == self.settings.execution_interval:
            self.execution_series_by_pair[candle.pair].add(candle)
            self._apply_profit_protection(candle)

        elif candle.interval == self.settings.execution_interval:
            self.execution_series_by_pair[candle.pair].add(candle)
            self._apply_profit_protection(candle)

    def _check_dry_run_stops(self, candle: OHLCVCandle) -> None:
        pos = self.local_state["positions"].get(candle.pair)
        if not pos or pos.get("source") != "dry_run" or pos.get("status") != "active":
             return
             
        stop_price = Decimal(str(pos.get("stop_loss_trigger", 0)))
        if stop_price <= 0:
             return
             
        direction = pos.get("direction", "").upper()
        # Fallback to pos size if direction is missing
        if not direction:
             direction = "LONG" if Decimal(str(pos["active_pos"])) > 0 else "SHORT"

        stop_hit = False
        if direction == "LONG" and candle.low <= stop_price:
             stop_hit = True
        elif direction == "SHORT" and candle.high >= stop_price:
             stop_hit = True
             
        if stop_hit:
             entry_price = Decimal(str(pos["avg_price"]))
             qty = abs(Decimal(str(pos["active_pos"])))
             exit_price = stop_price # Execute at stop price
             side_multiplier = 1 if direction == "LONG" else -1
             
             logger.warning(
                 "DRY-RUN STOP HIT: pair=%s, direction=%s, entry=%.2f, stop=%.2f, candle_low/high=%.2f, exit_price=%.2f",
                 candle.pair, direction, entry_price, stop_price, 
                 candle.low if direction == "LONG" else candle.high,
                 exit_price
             )
             
             raw_pnl = (exit_price - entry_price) * qty * side_multiplier
             simulated_fee = (entry_price * qty * Decimal("0.0005")) + (exit_price * qty * Decimal("0.0005"))
             
             self.alert.alert(f"DRY-RUN STOP LOSS HIT for {candle.pair} @ {exit_price}")
             self._update_accounting_balances(raw_pnl - simulated_fee)
             
             # Close position
             pos["status"] = "closed"
             pos["active_pos"] = "0"
             self._dry_run_positions.pop(candle.pair, None)
             self.local_state["positions"].pop(candle.pair, None)
             self.local_state.get("position_metadata", {}).pop(candle.pair, None)
             self.save_local_state()

    def _process_trading_candle(self, candle: OHLCVCandle, allow_entries: bool = True) -> None:
        if self.kill_switch_active:
            return

        series = self.series_by_pair[candle.pair]
        indicators = latest_indicator_snapshot(series)
        context = StrategyContext(
            pair=candle.pair,
            interval=self.interval,
            candles=series,
            indicators=indicators,
            features={}
        )
        
        signals = self.strategy_engine.evaluate(context)
        entry_processed = False
        
        if not any(is_entry_signal(s) for s in signals):
             logger.debug("[%s] strategy evaluation: no entry signals emitted for candle at %d", candle.pair, candle.close_time_ms)
        
        for signal in signals:
            if signal.action == SignalAction.HOLD:
                continue
            
            if is_entry_signal(signal):
                 if entry_processed:
                      logger.info("[%s] Skipping secondary entry signal in same candle.", candle.pair)
                      continue
                 self._handle_signal(signal, candle)
                 entry_processed = True
            else:
                 self._handle_signal(signal, candle)

    def _handle_signal(self, signal: StrategySignal, candle: OHLCVCandle) -> None:
        if signal.confidence < self.settings.risk.live_min_confidence:
             return

        tradable_equity = Decimal(self.local_state.get("tradable_base", "100000"))
        
        risk_context = RiskContext(
            signal=signal,
            account_equity=tradable_equity,
            available_equity=tradable_equity, 
            open_positions=self._get_open_positions(),
            trading_mode="live" if not self.settings.live_pilot_dry_run else "paper",
            live_trading_enabled=self.settings.live_trading_allowed,
            requested_leverage=self.leverage
        )
        
        decision = self.risk_manager.evaluate(risk_context)
        
        if decision.approved:
            self.alert.alert(f"Signal approved: {signal.action.value} {signal.pair} @ {candle.close}")
            report = self.execution_engine.process_decision(decision)
            self.alert.alert(f"Execution report: {report.reason}")
            
            if report.accepted:
                 if is_entry_signal(signal):
                      if signal.stop_loss and signal.entry_price:
                           r_val = abs(signal.entry_price - signal.stop_loss)
                           self.local_state.setdefault("position_metadata", {})[signal.pair] = {
                                "initial_stop_loss": str(signal.stop_loss),
                                "initial_entry_price": str(signal.entry_price),
                                "initial_r": str(r_val),
                                "peak_price": str(signal.entry_price),
                                "max_r_hit": "0"
                           }
                      
                      if self.settings.live_pilot_dry_run:
                           logger.info("[%s] dry-run entry creates simulated fill", signal.pair)
                           qty = decision.position_size or Decimal("0")
                           side_multiplier = 1 if signal.direction == SignalDirection.LONG else -1
                           virtual_pos = {
                                "id": f"dry_run_{int(time.time())}",
                                "pair": signal.pair,
                                "active_pos": str(qty * side_multiplier),
                                "avg_price": str(candle.close),
                                "leverage": str(decision.leverage),
                                "stop_loss_trigger": str(signal.stop_loss),
                                "take_profit_trigger": str(signal.take_profit),
                                "margin_type": self.settings.live_position_margin_type,
                                "status": "active",
                                "source": "dry_run"
                           }
                           self._dry_run_positions[signal.pair] = virtual_pos
                           self.local_state["positions"][signal.pair] = virtual_pos
                 
                 elif is_exit_signal(signal):
                      if self.settings.live_pilot_dry_run:
                           logger.info("[%s] dry-run exit creates simulated fill", signal.pair)
                           pos = self.local_state["positions"].get(signal.pair)
                           if pos:
                                entry_price = Decimal(str(pos["avg_price"]))
                                qty = abs(Decimal(str(pos["active_pos"])))
                                exit_price = candle.close
                                side_multiplier = 1 if Decimal(str(pos["active_pos"])) > 0 else -1
                                
                                raw_pnl = (exit_price - entry_price) * qty * side_multiplier
                                simulated_fee = (entry_price * qty * Decimal("0.0005")) + (exit_price * qty * Decimal("0.0005"))
                                
                                logger.info("[%s] dry-run closed accounting uses simulated fills: PnL=%.2f, Fee=%.2f", signal.pair, raw_pnl, simulated_fee)
                                self._update_accounting_balances(raw_pnl - simulated_fee)

                           self._dry_run_positions.pop(signal.pair, None)
                           self.local_state["positions"].pop(signal.pair, None)
                           self.local_state.get("position_metadata", {}).pop(signal.pair, None)

            self.reconcile()
        else:
            if "already has an open position" in decision.reason and self.settings.live_pilot_dry_run:
                 logger.info("[%s] entry blocked because dry-run position already exists", signal.pair)
            logger.info("Signal rejected: %s", decision.reason)

    def _apply_profit_protection(self, candle: OHLCVCandle) -> None:
        pos_data = self.local_state["positions"].get(candle.pair)
        if not pos_data or Decimal(str(pos_data.get("active_pos", 0))) == 0:
             return
             
        meta = self.local_state.get("position_metadata", {}).get(candle.pair)
        if not meta:
             return
             
        direction = SignalDirection.LONG if Decimal(str(pos_data["active_pos"])) > 0 else SignalDirection.SHORT
        entry_price = Decimal(meta["initial_entry_price"])
        initial_stop = Decimal(meta["initial_stop_loss"])
        r_val = Decimal(meta["initial_r"])
        if r_val <= 0: return

        current_price = candle.close
        pnl_points = (current_price - entry_price) if direction == SignalDirection.LONG else (entry_price - current_price)
        current_r = pnl_points / r_val
        
        max_r = max(Decimal(meta.get("max_r_hit", "0")), current_r)
        meta["max_r_hit"] = str(max_r)
        
        # Track peak price for ATR trailing
        peak_price = Decimal(meta.get("peak_price", str(entry_price)))
        if direction == SignalDirection.LONG:
             peak_price = max(peak_price, candle.high)
        else:
             peak_price = min(peak_price, candle.low)
        meta["peak_price"] = str(peak_price)

        current_stop = Decimal(str(pos_data.get("stop_loss_trigger") or initial_stop))
        new_stop = current_stop
        
        # 1. R-Based Tightening (Phase 5)
        if direction == SignalDirection.LONG:
            if max_r >= Decimal("0.8") and current_stop < entry_price:
                 new_stop = entry_price + (r_val * Decimal("0.05"))
            if max_r >= Decimal("1.2"):
                 target_lock = entry_price + (r_val * Decimal("0.25"))
                 new_stop = max(new_stop, target_lock)
            if max_r >= Decimal("1.8"):
                 target_lock = entry_price + (r_val * Decimal("0.75"))
                 new_stop = max(new_stop, target_lock)
        else:
            if max_r >= Decimal("0.8") and current_stop > entry_price:
                 new_stop = entry_price - (r_val * Decimal("0.05"))
            if max_r >= Decimal("1.2"):
                 target_lock = entry_price - (r_val * Decimal("0.25"))
                 new_stop = min(new_stop, target_lock)
            if max_r >= Decimal("1.8"):
                 target_lock = entry_price - (r_val * Decimal("0.75"))
                 new_stop = min(new_stop, target_lock)

        # 2. ATR Trailing (after 2.0R)
        if max_r >= Decimal("2.0"):
             # Get ATR from latest snapshot
             indicators = latest_indicator_snapshot(self.series_by_pair[candle.pair])
             atr = indicators.get("atr", Decimal("0"))
             if atr > 0:
                  trail_dist = atr * self.settings.risk.atr_trailing_multiple
                  if direction == SignalDirection.LONG:
                       atr_stop = peak_price - trail_dist
                       new_stop = max(new_stop, atr_stop)
                  else:
                       atr_stop = peak_price + trail_dist
                       new_stop = min(new_stop, atr_stop)

        if new_stop != current_stop:
             if direction == SignalDirection.LONG and new_stop < current_stop:
                  return
             if direction == SignalDirection.SHORT and new_stop > current_stop:
                  return
                  
             logger.info("[%s] Tightening live stop: %.2f -> %.2f (%.2fR hit)", candle.pair, current_stop, new_stop, max_r)
             
             # Update local trigger first so _check_dry_run_stops sees it
             pos_data["stop_loss_trigger"] = str(new_stop)

             if pos_data.get("source") == "dry_run":
                  self.alert.alert(f"Dry-run stop tightened for {candle.pair}: {new_stop} ({max_r:.2f}R hit)")
                  return

             report = self.execution_engine.update_tpsl(candle.pair, stop_loss=new_stop, reason=f"Profit protection: {max_r:.2f}R hit")
             if report.accepted:
                  self.alert.alert(f"Live stop tightened for {candle.pair}: {new_stop} ({max_r:.2f}R hit)")
             else:
                  self.alert.alert(f"FAILED to tighten live stop for {candle.pair}: {report.reason}", level="ERROR")
                  self.kill_switch_active = True
                  
        self.save_local_state()

    def _get_open_positions(self) -> Any:
        found = []
        for pair, pos in self.local_state["positions"].items():
            if pos and abs(Decimal(str(pos.get("active_pos", 0)))) > 0:
                found.append(OpenPosition(
                    pair=pos.get("pair", pair),
                    direction=SignalDirection.LONG if Decimal(str(pos.get("active_pos", 0))) > 0 else SignalDirection.SHORT,
                    quantity=abs(Decimal(str(pos.get("active_pos", 0)))),
                    entry_price=Decimal(str(pos.get("avg_price", 0))),
                    leverage=Decimal(str(pos.get("leverage", 1)))
                ))
        return tuple(found)

    def _update_dashboard_state(self) -> None:
        # For dashboard, we send all positions
        all_pos = [pos for pair, pos in self.local_state["positions"].items() if abs(Decimal(str(pos.get("active_pos", 0)))) > 0]
        
        # Use first pair as primary for dashboard metadata if needed, or just a list
        primary_pair = self.pairs[0] if self.pairs else "B-BTC_USDT"
        
        _update_live_state(
            running=not self._stop_requested,
            execution_mode="live" if not self.settings.live_pilot_dry_run else "live_dry_run",
            live_dry_run=self.settings.live_pilot_dry_run,
            pair=primary_pair, # Legacy single pair field
            interval=self.interval,
            strategy=self.strategy_name,
            candle_count=self.candle_count,
            live_positions_json=json.dumps(all_pos),
            initial_equity=str(self.starting_equity),
            tradable_base=str(self.local_state.get("tradable_base", "0")),
            locked_profit=str(self.local_state.get("locked_profit", "0")),
            last_updated=datetime.now(timezone.utc).isoformat()
        )
