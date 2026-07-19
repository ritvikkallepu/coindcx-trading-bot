from __future__ import annotations

import hashlib
import json
import logging
import queue
import re
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
from app.exchange.errors import CoinDCXAPIError
from app.exchange.coindcx_ws import CoinDCXFuturesWebSocketClient, MarketSubscription
from app.exchange.coindcx_channels import futures_candle_channel, futures_orderbook_channel
from app.fees import effective_fee_rate
from app.live.accounting import (
    calculate_closed_position_accounting,
    matching_transaction_totals,
    normalize_position_direction,
    select_exit_orders,
    total_order_fees_in_margin_currency,
    transaction_identity,
    transaction_net_amount,
)
from app.live.portfolio import (
    FuturesPortfolioSnapshot,
    build_futures_portfolio_snapshot,
    risk_capacity_before_reservations,
)
from app.risk.profit_protection import (
    compute_profit_giveback_stop,
    post_profit_pullback_seen,
)
from app.risk.manager import RiskManager
from app.risk.models import RiskContext, OpenPosition, get_unit_contract_value, InstrumentMetadata
from app.risk.exchange_rules import round_price
from app.risk.limits import is_entry_signal, is_exit_signal
from app.risk.pair_profiles import pair_profile_for
from app.strategies.base import StrategyEngine, StrategyContext, SignalAction, SignalDirection, StrategySignal
from app.strategies.defaults import strategy_engine_for_name
from app.utils.json import to_jsonable
from app.live.state import _update_live_state, reset_live_state
from app.alerts.console import ConsoleAlert
from app.alerts.base import AlertInterface
from app.alerts.telegram import TelegramAlert

logger = logging.getLogger(__name__)

_LIVE_DIAGNOSTICS_MAX_ROWS = 5000


def _live_state_slug(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-")
    return cleaned[:96] or "run"


def _live_state_run_id(
    pairs: list[str],
    strategy_name: str,
    interval: str,
    execution_interval: str,
) -> str:
    normalized_pairs = [str(pair).strip().upper() for pair in pairs if str(pair).strip()]
    identity = "|".join(
        [
            ",".join(sorted(normalized_pairs)),
            strategy_name,
            interval,
            execution_interval,
        ]
    )
    digest = hashlib.sha1(identity.encode("utf-8")).hexdigest()[:10]
    pair_label = "multi" if len(normalized_pairs) > 1 else (normalized_pairs[0] if normalized_pairs else "pair")
    return _live_state_slug(f"{pair_label}-{strategy_name}-{interval}-{execution_interval}-{digest}").lower()


class LiveTradingLoop:
    def __init__(
        self,
        settings: Settings,
        strategy_name: str = "hybrid_meta_v2",
        pairs: list[str] = ["B-BTC_USDT"],
        interval: str = "5m",
        execution_interval: str | None = None,
        starting_equity: Decimal = Decimal("2000"),
        leverage: Decimal | dict[str, Decimal] = Decimal("1"),
        alert_interface: AlertInterface | None = None,
        take_profit_pct: Decimal | None = None,
        stop_loss_pct: Decimal | None = None,
    ) -> None:
        self.settings = settings
        self.strategy_name = strategy_name
        self.interval = interval
        self.execution_interval = execution_interval or settings.execution_interval
        _validate_live_intervals(self.interval, self.execution_interval)
        self.starting_equity = starting_equity
        self.allocated_capital = starting_equity
        self.take_profit_pct = take_profit_pct
        self.stop_loss_pct = stop_loss_pct
        
        # Handle pair-specific leverage
        self._pair_leverage: dict[str, Decimal] = {}
        if isinstance(leverage, dict):
             self._pair_leverage = leverage
        
        self.pairs = []
        for p in pairs:
             if ":" in p:
                  parts = p.split(":")
                  pair_name = parts[0].strip()
                  self.pairs.append(pair_name)
                  self._pair_leverage[pair_name] = Decimal(parts[1].strip())
             else:
                  self.pairs.append(p)
                  if p not in self._pair_leverage:
                       self._pair_leverage[p] = leverage if isinstance(leverage, Decimal) else Decimal("1")

        self.leverage = leverage # Store for legacy compat if needed, but use _pair_leverage mostly
        
        # Alerts
        self.alert = alert_interface or ConsoleAlert()
             
        self.client = CoinDCXFuturesClient(settings)
        self.execution_engine = LiveExecutionEngine(self.client, settings)
        self.risk_manager = RiskManager(settings.risk)
        self.strategy_engine = strategy_engine_for_name(strategy_name)
        
        self.series_by_pair: dict[str, CandleSeries] = {}
        self.execution_series_by_pair: dict[str, CandleSeries] = {}
        self.gap_guard_by_pair: dict[str, CandleGapGuard] = {}
        
        # ALL-pairs support
        if "ALL" in [p.upper() for p in self.pairs]:
             if settings.live_trading_allowed and not settings.risk.live_allow_all_pairs_for_real_trading:
                  raise ValueError("LIVE_ALLOWED_PAIRS=ALL is not allowed for real live trading. Use an explicit pair allowlist.")
             
             logger.info("Discovering and filtering ALL available pairs...")
             self.pairs = self.discover_and_filter_pairs()
             logger.info("Selected top %d pairs for dry-run: %s", len(self.pairs), ", ".join(self.pairs))
             # Apply global leverage to discovered pairs if not already set
             for p in self.pairs:
                  if p not in self._pair_leverage:
                       self._pair_leverage[p] = leverage if isinstance(leverage, Decimal) else Decimal("1")
        
        for p in self.pairs:
             self.series_by_pair[p] = CandleSeries(maxlen=500)
             self.execution_series_by_pair[p] = CandleSeries(maxlen=500)
             self.gap_guard_by_pair[p] = CandleGapGuard(interval)
        
        self.state_dir = Path("data/live_state")
        self.global_state_path = Path("data/live_state.json")
        self.run_id = _live_state_run_id(self.pairs, self.strategy_name, self.interval, self.execution_interval)
        self.state_path = self.state_dir / f"{self.run_id}.json"
        self.local_state: dict[str, Any] = {
            "run_id": self.run_id,
            "state_path": str(self.state_path),
            "pair": ", ".join(self.pairs),
            "watchlist": list(self.pairs),
            "interval": self.interval,
            "strategy": self.strategy_name,
            "positions": {},
            "orders": [],
            "locked_profit": "0",
            "tradable_base": self._format_decimal(starting_equity),
            "allocated_capital": self._format_decimal(starting_equity),
            "execution_interval": self.execution_interval,
            "daily_loss_from_tradable_base": "0",
            "processed_pnl_order_ids": [],
            "processed_funding_transaction_ids": [],
            "funding_reconciliation_initialized": False,
            "kill_switch_active": settings.risk.live_kill_switch
        }
        self._last_reconcile_ms = 0
        self._last_ws_message_ms = int(time.time() * 1000)
        self._last_ws_message_ms_by_pair: dict[str, int] = {p: int(time.time() * 1000) for p in self.pairs}
        
        # Trackers (Keyed by (pair, interval) tuple)
        self._last_evaluated_strategy_candle_ms: dict[tuple[str, str], int] = {} 
        self._last_processed_candle_ms: dict[tuple[str, str], int] = {} 
        self._current_forming_candle_ms: dict[tuple[str, str], int] = {} 
        
        self._dry_run_positions: dict[str, dict[str, Any]] = {} # pair: pos_dict
        self._is_warming_up: dict[str, bool] = {p: True for p in self.pairs}
        self._stopped_out_candles: dict[str, int] = {} # pair: close_time_ms
        self._processed_signals: set[str] = set() # pair|interval|close_time|side
        self._same_direction_profit_cooldown_until_ms: dict[tuple[str, str], int] = {}
        self._post_profit_reentry_state: dict[str, dict[str, Any]] = {}
        self._pending_entry_pairs: set[str] = set()
        self._portfolio_snapshot: FuturesPortfolioSnapshot | None = None
        self._portfolio_positions: dict[str, dict[str, Any]] = {}
        self._missing_stop_alerted_pairs: set[str] = set()
        self._session_started_at = datetime.now(timezone.utc).isoformat()
        
        self.load_local_state()
        self.local_state["run_id"] = self.run_id
        self.local_state["state_path"] = str(self.state_path)
        self.local_state["pair"] = ", ".join(self.pairs)
        self.local_state["watchlist"] = list(self.pairs)
        self.local_state["interval"] = self.interval
        self.local_state["strategy"] = self.strategy_name
        self.local_state["allocated_capital"] = self._format_decimal(self.allocated_capital)
        self.local_state["execution_interval"] = self.execution_interval
        self.local_state["session_started_at"] = self._session_started_at
        self.kill_switch_active = self.local_state.get("kill_switch_active", settings.risk.live_kill_switch)
        
        self._stop_requested = False
        self._reconcile_thread = None
        self._ws_client = None
        self._processing_lock = threading.Lock()
        self._pending_closed_candles: dict[
            tuple[str, str, int], tuple[OHLCVCandle, str]
        ] = {}
        
        # Event worker
        self._event_queue = queue.Queue()
        self._worker_thread = threading.Thread(target=self._worker_loop, daemon=True, name="live-worker")
        self._worker_thread.start()
        
        # Instrument Metadata Cache
        self._instruments: dict[str, InstrumentMetadata] = {}
        self._fetch_instrument_metadata()

        self.candle_count = 0

    def _fetch_instrument_metadata(self) -> None:
        """Fetch and cache instrument metadata for all traded pairs."""
        for pair in self.pairs:
             try:
                  raw = self.client.get_instrument(pair, self.settings.futures_margin_currency)
                  self._instruments[pair] = InstrumentMetadata.from_mapping(pair, raw)
                  logger.info("[%s] Cached instrument metadata: step=%s, tick=%s", 
                              pair, self._instruments[pair].quantity_step, self._instruments[pair].tick_size)
             except Exception as exc:
                  logger.error("[%s] Failed to fetch instrument metadata: %s", pair, exc)

    def discover_and_filter_pairs(self) -> list[str]:
        """Discover available futures pairs and filter based on liquidity, spread, and activity."""
        try:
             # 1. Get all active futures instruments
             active_instruments = self.client.get_active_instruments() # Returns list of pair names
             
             # 2. Get tickers for volume and spread
             import urllib.request
             req = urllib.request.Request("https://api.coindcx.com/exchange/ticker", headers={"User-Agent": "Mozilla/5.0"})
             with urllib.request.urlopen(req, timeout=10) as response:
                  ticker_data = json.loads(response.read().decode())
             
             # Map ticker data by market name (e.g., BTCUSDT)
             tickers_by_market = {t["market"].upper(): t for t in ticker_data}
             
             filtered_pairs = []
             blocked = {p.upper() for p in self.settings.live_blocked_pairs}
             
             # print(f"DEBUG: active_instruments={active_instruments}")
             # print(f"DEBUG: tickers_by_market keys={list(tickers_by_market.keys())}")

             for pair_name in active_instruments:
                  # Manual block
                  if pair_name.upper() in blocked:
                       logger.debug("[%s] Rejected: Manually blocked", pair_name)
                       continue
                  
                  # Quote must be USDT (all futures are USDT settled but let's be sure)
                  if "_USDT" not in pair_name.upper():
                       logger.debug("[%s] Rejected: Non-USDT pair", pair_name)
                       continue
                  
                  # Get ticker match (Futures pair B-BTC_USDT matches BTCUSDT in spot ticker)
                  market_name = pair_name.upper().replace("B-", "").replace("_", "").replace("-", "")
                  ticker = tickers_by_market.get(market_name)
                  
                  # print(f"DEBUG: pair={pair_name} market={market_name} found={ticker is not None}")

                  if not ticker:
                       logger.debug("[%s] Rejected: No ticker data found for %s", pair_name, market_name)
                       continue
                  
                  # Volume filter
                  volume = Decimal(str(ticker.get("volume", 0)))
                  last_price = Decimal(str(ticker.get("last_price", 0)))
                  volume_usdt = volume * last_price
                  
                  if volume_usdt < self.settings.risk.live_min_24h_volume_usdt:
                       logger.debug("[%s] Rejected: Low 24h volume (%.2f USDT)", pair_name, volume_usdt)
                       continue
                  
                  # Spread filter
                  bid = Decimal(str(ticker.get("bid", 0)))
                  ask = Decimal(str(ticker.get("ask", 0)))
                  if bid > 0 and ask > 0:
                       spread_pct = ((ask - bid) / bid) * 100
                       if spread_pct > self.settings.risk.live_max_spread_pct:
                            logger.debug("[%s] Rejected: High spread (%.4f%%)", pair_name, spread_pct)
                            continue
                  else:
                       logger.debug("[%s] Rejected: Missing bid/ask", pair_name)
                       continue
                  
                  # If we reached here, it's a good candidate
                  filtered_pairs.append({
                       "pair": pair_name,
                       "volume_usdt": volume_usdt
                  })
             
             # Rank by volume and take top N
             filtered_pairs.sort(key=lambda x: x["volume_usdt"], reverse=True)
             selected = [p["pair"] for p in filtered_pairs[:self.settings.risk.live_max_auto_pairs]]
             
             if not selected:
                  logger.warning("ALL-pairs discovery found 0 suitable pairs. Falling back to default.")
                  return ["B-BTC_USDT"]
                  
             return selected

        except Exception as exc:
             logger.error("Failed to discover and filter pairs: %s", exc)
             return ["B-BTC_USDT"]

    def _format_decimal(self, val: Decimal) -> str:
        """Format Decimal as a clean string without trailing zeros or scientific notation."""
        return "{:f}".format(val.normalize())

    def _read_state_file(self, path: Path) -> dict[str, Any]:
        if not path.exists():
            return {}
        try:
            state = json.loads(path.read_text())
            return state if isinstance(state, dict) else {}
        except Exception:
            return {}

    def _state_matches_current_run(self, state: dict[str, Any]) -> bool:
        state_pairs = state.get("watchlist")
        if isinstance(state_pairs, str):
            pairs = [p.strip() for p in state_pairs.split(",") if p.strip()]
        elif isinstance(state_pairs, list):
            pairs = [str(p).strip() for p in state_pairs if str(p).strip()]
        else:
            pair = str(state.get("pair") or "").strip()
            pairs = [pair] if pair else []

        if set(pairs) != set(self.pairs):
            return False
        if state.get("strategy") and state.get("strategy") != self.strategy_name:
            return False
        if state.get("interval") and state.get("interval") != self.interval:
            return False
        execution_interval = state.get("execution_interval")
        if execution_interval and execution_interval != self.execution_interval:
            return False
        return True

    def _read_saved_state_for_run(self) -> dict[str, Any]:
        saved = self._read_state_file(self.state_path)
        if saved:
            return saved

        legacy = self._read_state_file(self.global_state_path)
        if legacy and self._state_matches_current_run(legacy):
            logger.info("Migrating matching legacy live state from %s to %s", self.global_state_path, self.state_path)
            return legacy
        return {}

    def _read_external_control_state(self) -> dict[str, Any]:
        global_state = self._read_state_file(self.global_state_path)
        if global_state.get("live_state_control") and "kill_switch_active" in global_state:
            return {"kill_switch_active": bool(global_state.get("kill_switch_active"))}
        if global_state.get("kill_switch_active") is True:
            return {"kill_switch_active": True}

        run_state = self._read_state_file(self.state_path)
        if "kill_switch_active" in run_state:
            return {"kill_switch_active": bool(run_state.get("kill_switch_active"))}
        return {}

    def _position_dict_is_open(self, position: Mapping[str, Any] | None) -> bool:
        if not position:
            return False
        return abs(_decimal_or_default(position.get("active_pos"), Decimal("0"))) > 0

    def _state_file_sort_value(self, state: Mapping[str, Any], path: Path) -> float:
        raw_value = str(state.get("last_updated") or state.get("updated_at") or "").strip()
        if raw_value.endswith("Z"):
            raw_value = f"{raw_value[:-1]}+00:00"
        if raw_value:
            try:
                updated = datetime.fromisoformat(raw_value)
                if updated.tzinfo is None:
                    updated = updated.replace(tzinfo=timezone.utc)
                return updated.timestamp()
            except ValueError:
                pass
        try:
            return path.stat().st_mtime
        except OSError:
            return 0.0

    def _recover_pair_metadata_from_prior_runs(self, pair: str) -> dict[str, Any] | None:
        """Return the newest saved bot metadata for a pair from older run-state files."""

        candidates: list[tuple[float, dict[str, Any]]] = []
        state_dir = getattr(self, "state_dir", None)
        if not isinstance(state_dir, Path):
            return None
        state_path = getattr(self, "state_path", None)
        try:
            paths = list(state_dir.glob("*.json"))
        except OSError:
            return None

        for path in paths:
            if state_path is not None and path == state_path:
                continue
            state = self._read_state_file(path)
            metadata_by_pair = state.get("position_metadata")
            if not isinstance(metadata_by_pair, dict):
                continue
            metadata = metadata_by_pair.get(pair)
            if not isinstance(metadata, dict) or not metadata:
                continue
            candidates.append((self._state_file_sort_value(state, path), dict(metadata)))

        if not candidates:
            return None
        candidates.sort(key=lambda item: item[0], reverse=True)
        return candidates[0][1]

    def _ensure_exchange_position_metadata(
        self,
        pair: str,
        position: Mapping[str, Any],
        recovered_metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        metadata_by_pair = self.local_state.setdefault("position_metadata", {})
        existing = metadata_by_pair.get(pair)
        if isinstance(existing, dict) and existing:
            return existing

        if recovered_metadata:
            metadata_by_pair[pair] = dict(recovered_metadata)
            logger.info("[%s] Recovered live position metadata from previous run state.", pair)
            return metadata_by_pair[pair]

        entry_price = _positive_decimal_or_none(position.get("avg_price"))
        if entry_price is None:
            return {}
        stop_loss = _positive_decimal_or_none(
            position.get("stop_loss_trigger")
            or position.get("stop_loss")
            or position.get("sl_trigger_price")
            or position.get("stop_price")
        )
        initial_r = abs(entry_price - stop_loss) if stop_loss is not None else Decimal("0")
        opened_at_ms = _int_or_zero(position.get("updated_at")) or int(time.time() * 1000)
        metadata = {
            "initial_entry_price": str(entry_price),
            "initial_stop_loss": str(stop_loss) if stop_loss is not None else "",
            "initial_r": str(initial_r),
            "peak_price": str(entry_price),
            "max_r_hit": "0",
            "opened_at_ms": opened_at_ms,
            "strategy_name": getattr(self, "strategy_name", ""),
            "recovered_from_exchange": True,
        }
        metadata_by_pair[pair] = metadata
        logger.warning(
            "[%s] Built minimal live metadata from exchange position; prior run metadata was unavailable.",
            pair,
        )
        return metadata

    def _adopt_exchange_position(
        self,
        pair: str,
        position: LivePositionSnapshot,
        *,
        last_known_pos: Mapping[str, Any] | None = None,
        pending_bot_entry: bool = False,
    ) -> tuple[dict[str, Any], bool]:
        metadata_by_pair = self.local_state.setdefault("position_metadata", {})
        existing_metadata = metadata_by_pair.get(pair)
        has_existing_metadata = isinstance(existing_metadata, dict) and bool(existing_metadata)
        recovered_metadata = None if has_existing_metadata else self._recover_pair_metadata_from_prior_runs(pair)
        last_known_exists = self._position_dict_is_open(last_known_pos)
        known_bot_owned = (
            last_known_exists
            or pending_bot_entry
            or has_existing_metadata
            or bool(recovered_metadata)
        )

        position_dict = position.to_dict()
        position_dict["source"] = "exchange"
        if last_known_pos and last_known_pos.get("manual_entry"):
            position_dict["manual_entry"] = True
        else:
            position_dict["manual_entry"] = not known_bot_owned

        self.local_state["positions"][pair] = position_dict
        self._ensure_exchange_position_metadata(pair, position_dict, recovered_metadata)
        return position_dict, known_bot_owned

    def _sync_pair_position_before_entry(
        self,
        pair: str,
    ) -> tuple[bool, str, dict[str, Any]]:
        """Before real entries, prove the exchange is not already holding this pair."""

        if not self.settings.live_trading_allowed or self.settings.live_pilot_dry_run:
            return False, "", {}

        try:
            position = self.execution_engine.sync.fetch_position(pair)
        except Exception as exc:
            logger.error("[%s] Exchange position pre-entry sync failed: %s", pair, exc)
            return True, "exchange_position_sync_failed", {"error": str(exc)}

        if not position or not position.is_open:
            return False, "", {}

        position_dict, known_bot_owned = self._adopt_exchange_position(
            pair,
            position,
            last_known_pos=self.local_state["positions"].get(pair),
            pending_bot_entry=pair in self._pending_entry_pairs,
        )
        self.save_local_state()
        logger.warning(
            "[%s] Entry blocked because exchange already has an open position for this pair.",
            pair,
        )
        return (
            True,
            "exchange_position_already_open",
            {
                "active_pos": position_dict.get("active_pos"),
                "avg_price": position_dict.get("avg_price"),
                "stop_loss_trigger": position_dict.get("stop_loss_trigger"),
                "known_bot_owned": known_bot_owned,
            },
        )

    def load_local_state(self) -> None:
        saved = self._read_saved_state_for_run()
        if saved:
            try:
                # Check if we should align tradable_base to CLI starting_equity
                # 1. No active positions in state
                # 2. No locked profit (clean state)
                # 3. Explicit CLI equity differs from persisted tradable_base
                persisted_base = Decimal(str(saved.get("tradable_base", "0")))
                persisted_locked = Decimal(str(saved.get("locked_profit", "0")))
                has_positions = any(abs(Decimal(str(p.get("active_pos", 0)))) > 0 for p in saved.get("positions", {}).values())
                
                if persisted_locked == 0 and not has_positions:
                     if persisted_base != self.starting_equity:
                          logger.info("Aligning Tradable Base to CLI starting equity: %s -> %s", persisted_base, self.starting_equity)
                          saved["tradable_base"] = self._format_decimal(self.starting_equity)
                
                self.local_state.update(saved)
                self.local_state["recent_diagnostics"] = []
                
                # Recover last evaluated candle times if present
                saved_evals = self.local_state.get("last_evaluated_strategy_candle_close_time", {})
                if isinstance(saved_evals, dict):
                    for key, val in saved_evals.items():
                         if "_" in key:
                              parts = key.rsplit("_", 1)
                              pair = parts[0]
                              interval = parts[1]
                              self._last_evaluated_strategy_candle_ms[(pair, interval)] = int(val)
                
                # Recover processed signals
                processed_list = self.local_state.get("processed_signals", [])
                if isinstance(processed_list, list):
                     self._processed_signals = set(processed_list)

                # Recover stopped out candles for cooldown persistence
                saved_stops = self.local_state.get("stopped_out_candles", {})
                if isinstance(saved_stops, dict):
                    for pair, ts in saved_stops.items():
                        self._stopped_out_candles[pair] = int(ts)

                saved_profit_cooldowns = self.local_state.get("same_direction_profit_cooldown_until_ms", {})
                if isinstance(saved_profit_cooldowns, dict):
                    for key, ts in saved_profit_cooldowns.items():
                        pair, _, direction = str(key).partition("|")
                        if pair and direction:
                            self._same_direction_profit_cooldown_until_ms[(pair, direction)] = int(ts)

                saved_reentry_state = self.local_state.get("post_profit_reentry_state", {})
                if isinstance(saved_reentry_state, dict):
                    self._post_profit_reentry_state = {
                        str(pair): dict(state)
                        for pair, state in saved_reentry_state.items()
                        if isinstance(state, dict)
                    }

                # Recover dry run positions from state if any
                for pair, pos in self.local_state.get("positions", {}).items():
                     if self.settings.live_pilot_dry_run:
                          self._dry_run_positions[pair] = pos
                logger.info("Loaded local live state from %s (processed_signals=%d)", self.state_path, len(self._processed_signals))
            except Exception as exc:
                logger.error("Failed to load local state: %s", exc)

        control_state = self._read_external_control_state()
        if control_state.get("kill_switch_active"):
            self.local_state["kill_switch_active"] = True

    def save_local_state(self) -> None:
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            
            # External kill switch wins. A false value is applied in reconcile()
            # so live safety trips are not accidentally cleared during a save.
            control_state = self._read_external_control_state()
            if control_state.get("kill_switch_active"):
                 self.kill_switch_active = True
             
            self.local_state["run_id"] = self.run_id
            self.local_state["state_path"] = str(self.state_path)
            self.local_state["kill_switch_active"] = self.kill_switch_active
             
            # Persist evaluation tracker in serializable format
            eval_persistence = {
                 f"{p}_{i}": ts for (p, i), ts in self._last_evaluated_strategy_candle_ms.items()
            }
            self.local_state["last_evaluated_strategy_candle_close_time"] = eval_persistence
            
            # Persist stopped out candles for cooldown persistence
            self.local_state["stopped_out_candles"] = {
                 pair: str(ts) for pair, ts in self._stopped_out_candles.items()
            }

            self.local_state["same_direction_profit_cooldown_until_ms"] = {
                 f"{pair}|{direction}": str(ts)
                 for (pair, direction), ts in self._same_direction_profit_cooldown_until_ms.items()
            }
            self.local_state["post_profit_reentry_state"] = self._post_profit_reentry_state

            # Persist processed signals (limit to last 200 for sanity)
            processed_list = sorted(list(self._processed_signals))
            if len(processed_list) > 200:
                 processed_list = processed_list[-200:]
            self.local_state["processed_signals"] = processed_list
            
            # Merge logic: preserve PnL/positions from memory, but kill_switch from either
            self.state_path.write_text(json.dumps(to_jsonable(self.local_state), indent=2))
        except Exception as exc:
            logger.error("Failed to save local state: %s", exc)

    def run(self) -> None:
        mode_str = "DRY-RUN" if self.settings.live_pilot_dry_run else "REAL-TIME LIVE"
        logger.info(
            "Starting %s trading loop for %s strategy_interval=%s execution_interval=%s allocated_capital=%s",
            mode_str,
            ", ".join(self.pairs),
            self.interval,
            self.execution_interval,
            self.allocated_capital,
        )
        self.alert.alert(
            f"Starting {mode_str} trading loop for {len(self.pairs)} pairs "
            f"@ {self.interval}/{self.execution_interval}"
        )
        
        if self.kill_switch_active:
             logger.warning("STARTUP: KILL SWITCH IS ACTIVE. New entries will be blocked.")
             self.alert.alert("KILL SWITCH IS ACTIVE. New entries blocked.", level="WARNING")

        # Log existing local positions
        local_positions = [p for p in self.local_state["positions"].values() if abs(Decimal(str(p.get("active_pos", 0)))) > 0]
        if local_positions:
             logger.info("STARTUP: Found %d existing positions in local state:", len(local_positions))
             for p in local_positions:
                  logger.info("  - %s: %s %s @ %s (source=%s)", p.get("pair"), p.get("direction") or ("LONG" if Decimal(str(p.get("active_pos"))) > 0 else "SHORT"), p.get("active_pos"), p.get("avg_price"), p.get("source"))

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
                MarketSubscription(futures_candle_channel(pair, self.execution_interval), "candlestick"),
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
        self.local_state["running"] = False
        self.local_state["last_updated"] = datetime.now(timezone.utc).isoformat()
        self.save_local_state()
        logger.info("Live trading loop stop requested.")

    def _reconcile_loop(self) -> None:
        while not self._stop_requested:
            try:
                now_ms = int(time.time() * 1000)
                interval_ms = interval_to_ms(self.interval)
                
                # Check for WS silence and fallback to polling PER PAIR
                for pair in self.pairs:
                     last_ms = self._last_ws_message_ms_by_pair.get(pair, 0)
                     silence_duration = (now_ms - last_ms) // 1000
                     
                     if silence_duration > self.settings.live_rest_poll_seconds:
                          logger.warning("[%s] WebSocket silent for %d seconds. Polling REST fallback...", pair, silence_duration)
                          self._poll_rest_candles_for_pair(pair)
                          # Reset timer to avoid spamming polls
                          self._last_ws_message_ms_by_pair[pair] = now_ms

                     # 2. Strategy Grace Period Fallback (Point 4)
                     # Calculate when the last strategy candle should have closed
                     expected_close = (now_ms // interval_ms) * interval_ms
                     last_eval = self._last_evaluated_strategy_candle_ms.get((pair, self.interval), 0)
                     
                     # Grace period (30s)
                     grace = 30000
                     if last_eval < expected_close - 1000: # We haven't evaluated the candle that just closed
                          if now_ms > expected_close + grace:
                               logger.warning("[%s] Missed closed candle from websocket; fetching REST fallback interval=%s expected_close_time_ms=%d", 
                                              pair, self.interval, expected_close)
                               self._poll_rest_candles_for_pair(pair)
                               # Prevent immediate re-poll by bumping the timer or relying on last_eval update
                               # _on_candle will update last_eval if the rest poll returns a closed candle
                
                if now_ms - self._last_reconcile_ms > self.settings.live_reconcile_seconds * 1000:
                    self.reconcile()
                    self._last_reconcile_ms = now_ms
            except Exception as exc:
                logger.error("Reconciliation loop error: %s", exc)
            time.sleep(1)

    def reconcile(self) -> None:
        # Check if kill switch was activated externally (e.g. by dashboard)
        external_state = self._read_external_control_state()
        if "kill_switch_active" in external_state:
             external_kill = bool(external_state.get("kill_switch_active"))
             if external_kill and not self.kill_switch_active:
                  logger.warning("Kill switch activated externally.")
                  self.kill_switch_active = True
                  self.local_state["kill_switch_active"] = True
             elif not external_kill and self.kill_switch_active:
                  logger.warning("Kill switch deactivated externally.")
                  self.kill_switch_active = False
                  self.local_state["kill_switch_active"] = False
                  
        # Update scanned_pairs for dashboard
        now_ms = int(time.time() * 1000)
        self.local_state["scanned_pairs"] = {
             pair: {
                  "pair": pair,
                  "interval": self.interval,
                  "strategy": self.strategy_name,
                  "leverage": str(self._pair_leverage.get(pair, Decimal("1"))),
                  "last_ws_event": self._last_ws_message_ms_by_pair.get(pair, 0),
                  "last_strat_candle": self._last_evaluated_strategy_candle_ms.get((pair, self.interval), 0),
                  "forming_candle": self._current_forming_candle_ms.get((pair, self.interval), 0),
                  "ws_health": "connected" if (now_ms - self._last_ws_message_ms_by_pair.get(pair, 0)) < self.settings.live_rest_poll_seconds * 1000 else "silent",
             }
             for pair in self.pairs
        }

        self._refresh_portfolio_snapshot()

        # Check the watchlist, local bot state, and account-wide exchange positions.
        # This catches positions that survived a restart/watchlist change before
        # the current run had a chance to write them into its own state file.
        all_tracked_pairs = (
            set(self.pairs)
            | set(self.local_state["positions"].keys())
            | set(self._portfolio_positions.keys())
        )

        for pair in all_tracked_pairs:
            self._reconcile_pair(pair)
        
        self._reconcile_funding_transactions()
        self._reconcile_daily_loss()
        self._update_dashboard_state()
        self.save_local_state()

    def _refresh_portfolio_snapshot(self) -> None:
        """Refresh account-wide wallet and position data for risk and monitoring."""

        try:
            wallets = self.client.get_wallets()
            positions = self.client.list_positions(
                size=100,
                margin_currencies=[self.settings.futures_margin_currency],
            )
            snapshot = build_futures_portfolio_snapshot(
                wallets=wallets,
                positions=positions,
                margin_currency=self.settings.futures_margin_currency,
                fallback_quote_to_margin_rate=self.settings.quote_to_margin_rate,
            )
        except Exception as exc:
            logger.warning("Portfolio equity refresh failed; hiding stale portfolio positions: %s", exc)
            self._portfolio_positions = {}
            self.local_state["portfolio_positions"] = {}
            self.local_state["portfolio_positions_stale"] = True
            return

        self._portfolio_snapshot = snapshot
        self._portfolio_positions = {}
        updated_at = datetime.now(timezone.utc).isoformat()
        for position in positions:
            pair = str(position.get("pair") or "")
            active_pos = Decimal(str(position.get("active_pos") or "0"))
            if not pair or active_pos == 0:
                continue
            normalized = dict(position)
            normalized["source"] = "exchange"
            normalized["status"] = "open"
            normalized["direction"] = "long" if active_pos > 0 else "short"
            self._portfolio_positions[pair] = normalized
        self.local_state.update(
            {
                "wallet_balance": self._format_decimal(snapshot.wallet_balance),
                "wallet_locked_collateral": self._format_decimal(
                    snapshot.locked_collateral
                ),
                "wallet_free_collateral": self._format_decimal(snapshot.free_collateral),
                "portfolio_unrealized_pnl": self._format_decimal(
                    snapshot.unrealized_pnl
                ),
                "portfolio_equity": self._format_decimal(snapshot.portfolio_equity),
                "portfolio_open_positions": snapshot.open_position_count,
                "portfolio_positions": self._portfolio_positions,
                "portfolio_positions_stale": False,
                "portfolio_positions_updated_at": updated_at,
            }
        )

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
                pending_bot_entry = pair in self._pending_entry_pairs
                pos_dict, known_bot_owned = self._adopt_exchange_position(
                    pair,
                    pos,
                    last_known_pos=last_known_pos,
                    pending_bot_entry=pending_bot_entry,
                )
                if not last_known_exists and not pending_bot_entry and not known_bot_owned:
                      # This is a manual or external position
                      logger.warning("[%s] Manual/External position detected on exchange (not in local state).", pair)
                      self.alert.alert(f"Manual position detected for {pair}. The bot will track it but will NOT close it unless the strategy fires an explicit exit.", level="WARNING")
                elif not last_known_exists:
                      logger.info("[%s] Adopting exchange position from reconciliation.", pair)
                 
                # Safety rule 7: No stop loss?
                if not pos.has_stop_loss and self.settings.risk.live_require_stop_loss:
                     self._report_missing_protective_stop(pair)
                     # Manual positions are high risk if no SL
                     if not pos_dict.get("manual_entry"):
                          self.kill_switch_active = True
                          self.local_state["kill_switch_active"] = True
                else:
                     self._clear_missing_protective_stop_alert(pair)
            else:
                self._clear_missing_protective_stop_alert(pair)
                # Position is closed or doesn't exist on exchange
                if last_known_exists:
                     # Only Mark as closed if it was an exchange position
                     if last_known_pos.get("source") == "exchange":
                          logger.info("[%s] Local state position closed on exchange. Processing accounting...", pair)
                          
                          # Point 6: Apply re-entry cooldown for real live stops
                          now_ms = int(time.time() * 1000)
                          self._stopped_out_candles.setdefault(pair, now_ms)
                          
                          if self._process_closed_position(pair, last_known_pos):
                               self.local_state["positions"].pop(pair, None)
                               self.local_state.get("position_metadata", {}).pop(pair, None)
                          else:
                               last_known_pos["accounting_pending"] = True
            
            # 3. Handle Kill Switch for this position
            if self.kill_switch_active or self.local_state.get("kill_switch_active"):
                 # Manual positions should be respected even under kill switch, or at least not closed by force unless configured
                 if exchange_pos_exists:
                      is_manual = self.local_state["positions"].get(pair, {}).get("manual_entry", False)
                      
                      if self.settings.risk.live_close_on_kill_switch:
                           if is_manual:
                                logger.warning("Kill switch active but NOT closing manual position for %s. Manual intervention required.", pair)
                                self.alert.alert(f"Kill switch active. Manual position {pair} remains open!", level="WARNING")
                           else:
                                try:
                                     logger.warning("Kill switch: Flattening bot-owned position for %s", pair)
                                     self.client.exit_position(pos.position_id)
                                except CoinDCXAPIError as exc:
                                     if exc.status_code == 422 and "already in progress" in str(exc):
                                          logger.info("[%s] Kill switch: Exit already in progress on exchange.", pair)
                                     else:
                                          logger.error("[%s] Kill switch flatten FAILED: %s", pair, exc)
                                except Exception as exc:
                                     logger.error("[%s] Kill switch flatten FAILED: %s", pair, exc)
                      
                      if self.settings.risk.live_cancel_orders_on_kill_switch:
                           try:
                                self.client.cancel_all_open_orders_for_position(pos.position_id)
                           except Exception as exc:
                                logger.error("[%s] Kill switch cancel orders FAILED: %s", pair, exc)

        except Exception as exc:
            logger.error("[%s] Reconciliation failed: %s", pair, exc)

    def _process_closed_position(self, pair: str, local_pos: dict[str, Any]) -> bool:
        """Fetch results from exchange and update virtual accounting."""
        if local_pos.get("source") == "dry_run":
             return True

        try:
             # Fetch both sides, then select only the side that can close this position.
             raw_orders = []
             for side in ["buy", "sell"]:
                 try:
                     raw_orders.extend(self.client.list_orders(
                          status="filled",
                          side=side,
                          margin_currencies=[self.settings.futures_margin_currency],
                          size=100
                     ))
                 except Exception:
                     pass
             
             position = {**local_pos, "pair": pair}
             position_metadata = self.local_state.get("position_metadata", {}).get(pair, {})
             opened_at_ms = _int_or_zero(position_metadata.get("opened_at_ms"))
             if opened_at_ms > 0:
                  raw_orders = [
                       order
                       for order in raw_orders
                       if _int_or_zero(order.get("updated_at") or order.get("created_at"))
                       >= opened_at_ms
                  ]
             direction = normalize_position_direction(position)
             exit_orders = select_exit_orders(raw_orders, pair=pair, direction=direction)
             exit_order_ids = tuple(
                  str(order.get("id") or order.get("order_id") or "")
                  for order in exit_orders
                  if order.get("id") or order.get("order_id")
             )
             if not exit_order_ids:
                  logger.warning("[%s] No filled orders found to process closed position accounting. Real exchange fills might be delayed.", pair)
                  return False

             processed_ids = self.local_state.get("processed_pnl_order_ids", [])
             already_processed = [order_id for order_id in exit_order_ids if order_id in processed_ids]
             if already_processed:
                  logger.info(
                       "[%s] Closing order(s) already processed for accounting: %s. Skipping.",
                       pair,
                       already_processed,
                  )
                  return True
             
             self.alert.alert(f"Position closed for {pair}. Reconciling PnL from orders...", level="INFO")

             transaction_gross, transaction_fee, transaction_count = (
                  self._matching_close_transaction_totals(pair, exit_orders)
             )
             entry_fee = self._entry_fee_for_position(pair, position_metadata)
             instrument = self._instruments.get(pair)
             accounting = calculate_closed_position_accounting(
                  position=position,
                  exit_orders=exit_orders,
                  quote_to_margin_rate=self.settings.quote_to_margin_rate,
                  unit_contract_value=(
                       instrument.unit_contract_value
                       if instrument
                       else get_unit_contract_value(pair)
                  ),
                  margin_currency=self.settings.futures_margin_currency,
                  entry_fee=entry_fee,
                  transaction_gross_pnl=(
                       transaction_gross if transaction_count > 0 else None
                  ),
                  transaction_exit_fee=(
                       transaction_fee if transaction_count > 0 else None
                  ),
             )
             closed_at_ms = max(
                  (
                       _int_or_zero(order.get("updated_at") or order.get("created_at"))
                       for order in exit_orders
                  ),
                  default=int(time.time() * 1000),
             )
             self._record_post_profit_exit(
                  pair=pair,
                  direction=accounting.direction,
                  exit_price=accounting.exit_price,
                  net_pnl=accounting.net_pnl,
                  closed_at_ms=closed_at_ms,
             )
             logger.info(
                  "[%s] Closed position accounting: source=%s direction=%s entry=%s "
                  "exit=%s qty=%s gross_pnl=%.2f entry_fee=%.2f exit_fee=%.2f net_pnl=%.2f",
                  pair,
                  accounting.source,
                  accounting.direction.value,
                  position.get("avg_price"),
                  accounting.exit_price,
                  accounting.quantity,
                  accounting.gross_pnl,
                  accounting.entry_fee,
                  accounting.exit_fee,
                  accounting.net_pnl,
             )
             self.alert.alert(
                  f"Position closed for {pair}. Gross PnL: {accounting.gross_pnl:.2f}, "
                  f"Fees: {(accounting.entry_fee + accounting.exit_fee):.2f}, "
                  f"Net: {accounting.net_pnl:.2f} INR"
             )

             self._update_accounting_balances(accounting.net_pnl)

             processed_ids = self.local_state.setdefault("processed_pnl_order_ids", [])
             processed_ids.extend(accounting.exit_order_ids)
             self.local_state["processed_pnl_order_ids"] = list(dict.fromkeys(processed_ids))[-100:]
             return True
             
        except Exception as exc:
             logger.error("[%s] Failed to process closed position accounting: %s", pair, exc)
             self.alert.alert(f"Accounting reconciliation FAILED for {pair}: {exc}", level="ERROR")
             return False

    def _matching_close_transaction_totals(
        self,
        pair: str,
        exit_orders: list[Mapping[str, Any]],
    ) -> tuple[Decimal, Decimal, int]:
        """Fetch exact INR PnL and fees for the selected closing order group."""

        exit_order_ids = {
             str(order.get("id") or order.get("order_id") or "")
             for order in exit_orders
             if order.get("id") or order.get("order_id")
        }
        stage_aliases = {"liquidate": "liquidation"}
        stages = {
             stage_aliases.get(
                  str(order.get("stage") or "").strip().lower(),
                  str(order.get("stage") or "").strip().lower(),
             )
             for order in exit_orders
             if str(order.get("stage") or "").strip()
        }
        if not stages:
             stages = {"default", "exit", "tpsl_exit", "liquidation"}

        for attempt in range(3):
             transactions: list[Mapping[str, Any]] = []
             for stage in stages:
                  try:
                       transactions.extend(
                            self.client.list_position_transactions(
                                 stage=stage,
                                 margin_currencies=[self.settings.futures_margin_currency],
                                 size=100,
                            )
                       )
                  except Exception as exc:
                       logger.debug(
                            "[%s] Position transactions unavailable for stage=%s: %s",
                            pair,
                            stage,
                            exc,
                       )
             totals = matching_transaction_totals(
                  transactions,
                  pair=pair,
                  exit_order_ids=exit_order_ids,
             )
             if totals[2] > 0 or attempt == 2:
                  return totals
             time.sleep(0.5)
        return Decimal("0"), Decimal("0"), 0

    def _entry_fee_for_position(
        self,
        pair: str,
        position_metadata: Mapping[str, Any],
    ) -> Decimal:
        stored_fee = Decimal(str(position_metadata.get("entry_fee_margin_currency", "0")))
        order_ids = {
             str(order_id)
             for order_id in position_metadata.get("entry_order_ids", [])
             if str(order_id)
        }
        if not order_ids:
             return stored_fee
        try:
             transactions = self.client.list_position_transactions(
                  stage="default",
                  margin_currencies=[self.settings.futures_margin_currency],
                  size=100,
             )
             _, exact_fee, matched = matching_transaction_totals(
                  transactions,
                  pair=pair,
                  exit_order_ids=order_ids,
             )
             return exact_fee if matched > 0 else stored_fee
        except Exception as exc:
             logger.debug("[%s] Exact entry fee transaction unavailable: %s", pair, exc)
             return stored_fee

    def _entry_fee_from_report(self, report: LiveExecutionReport, pair: str) -> Decimal:
        metadata = report.metadata if isinstance(report.metadata, dict) else {}
        live_sync = metadata.get("live_sync")
        if not isinstance(live_sync, dict):
             return Decimal("0")
        orders = live_sync.get("orders")
        if not isinstance(orders, list):
             return Decimal("0")
        order_ids = {
             str(order.get("order_id") or order.get("id") or "")
             for order in orders
             if isinstance(order, Mapping)
             and (order.get("order_id") or order.get("id"))
        }
        if order_ids:
             try:
                  transactions = self.client.list_position_transactions(
                       stage="default",
                       margin_currencies=[self.settings.futures_margin_currency],
                       size=100,
                  )
                  _, exact_fee, matched = matching_transaction_totals(
                       transactions,
                       pair=pair,
                       exit_order_ids=order_ids,
                  )
                  if matched > 0:
                       return exact_fee
             except Exception as exc:
                  logger.debug("[%s] Entry transaction fee unavailable: %s", pair, exc)
        return total_order_fees_in_margin_currency(
             [order for order in orders if isinstance(order, Mapping)],
             quote_to_margin_rate=self.settings.quote_to_margin_rate,
             margin_currency=self.settings.futures_margin_currency,
        )

    def _entry_order_ids_from_report(self, report: LiveExecutionReport) -> list[str]:
        metadata = report.metadata if isinstance(report.metadata, dict) else {}
        live_sync = metadata.get("live_sync")
        orders = live_sync.get("orders") if isinstance(live_sync, dict) else None
        if not isinstance(orders, list):
             return []
        return [
             str(order.get("order_id") or order.get("id"))
             for order in orders
             if isinstance(order, Mapping)
             and (order.get("order_id") or order.get("id"))
        ]

    def _reconcile_funding_transactions(self) -> None:
        """Apply new CoinDCX funding cash flows without replaying old history."""

        if self.settings.live_pilot_dry_run or not self.settings.live_trading_allowed:
             return
        try:
             transactions = self.client.list_position_transactions(
                  stage="funding",
                  margin_currencies=[self.settings.futures_margin_currency],
                  size=100,
             )
        except Exception as exc:
             logger.debug("Funding transaction reconciliation unavailable: %s", exc)
             return

        watched = {pair.upper() for pair in self.pairs}
        relevant = [
             transaction
             for transaction in transactions
             if str(transaction.get("pair") or "").upper() in watched
        ]
        identities = [transaction_identity(transaction) for transaction in relevant]
        if not self.local_state.get("funding_reconciliation_initialized", False):
             self.local_state["processed_funding_transaction_ids"] = identities[-300:]
             self.local_state["funding_reconciliation_initialized"] = True
             logger.info(
                  "Funding reconciliation baseline initialized with %d existing transaction(s).",
                  len(identities),
             )
             return

        processed_ordered = list(
             dict.fromkeys(self.local_state.get("processed_funding_transaction_ids", []))
        )
        processed = set(processed_ordered)
        relevant.sort(
             key=lambda transaction: _int_or_zero(
                  transaction.get("updated_at") or transaction.get("created_at")
             )
        )
        for transaction in relevant:
             transaction_id = transaction_identity(transaction)
             if transaction_id in processed:
                  continue
             net_amount = transaction_net_amount(transaction)
             if net_amount != 0:
                  pair = str(transaction.get("pair") or "")
                  logger.info(
                       "[%s] Funding transaction reconciled: net=%s %s",
                       pair,
                       net_amount,
                       self.settings.futures_margin_currency,
                  )
                  self._update_accounting_balances(net_amount)
             processed.add(transaction_id)
             processed_ordered.append(transaction_id)
        self.local_state["processed_funding_transaction_ids"] = list(
             dict.fromkeys(processed_ordered)
        )[-300:]

    def _simulated_close_accounting(
        self,
        pair: str,
        position: Mapping[str, Any],
        exit_price: Decimal,
    ) -> tuple[Decimal, Decimal]:
        """Return dry-run gross PnL and round-trip fees in margin currency."""

        direction = normalize_position_direction(position)
        entry_price = Decimal(str(position.get("avg_price", 0)))
        quantity = abs(Decimal(str(position.get("active_pos", 0))))
        instrument = self._instruments.get(pair)
        unit_contract_value = (
             instrument.unit_contract_value
             if instrument
             else get_unit_contract_value(pair)
        )
        quote_to_margin_rate = self.settings.quote_to_margin_rate
        side_multiplier = (
             Decimal("1") if direction == SignalDirection.LONG else Decimal("-1")
        )
        gross_pnl = (
             (exit_price - entry_price)
             * quantity
             * side_multiplier
             * unit_contract_value
             * quote_to_margin_rate
        )
        fee_rate = effective_fee_rate(
             self.settings.risk.taker_fee_rate,
             self.settings.risk.fee_gst_rate,
        )
        round_trip_notional = (
             (entry_price + exit_price)
             * quantity
             * unit_contract_value
             * quote_to_margin_rate
        )
        return gross_pnl, round_trip_notional * fee_rate

    def _update_accounting_balances(self, net_trade_pnl: Decimal) -> None:
        locked = Decimal(self.local_state.get("locked_profit", "0"))
        base = Decimal(self.local_state.get("tradable_base", str(self.starting_equity)))
        daily_loss = Decimal(self.local_state.get("daily_loss_from_tradable_base", "0"))

        if net_trade_pnl > 0:
             if getattr(self.settings.risk, "compound_profits", False):
                 base += net_trade_pnl
                 logger.info("Profit added to tradable base: +%.2f INR", net_trade_pnl)
                 self.alert.alert(f"Profit added to tradable base: +{net_trade_pnl:.2f} INR")
             else:
                 locked += net_trade_pnl
                 logger.info("Profit locked: +%.2f INR", net_trade_pnl)
                 self.alert.alert(f"Profit locked: +{net_trade_pnl:.2f} INR")
        else:
             loss = abs(net_trade_pnl)
             base = max(Decimal("0"), base - loss)
             daily_loss += loss
             logger.warning("Loss from tradable base: %.2f INR", net_trade_pnl)
             self.alert.alert(f"Loss from tradable base: {net_trade_pnl:.2f} INR", level="WARNING")

        self.local_state["locked_profit"] = self._format_decimal(locked)
        self.local_state["tradable_base"] = self._format_decimal(base)
        self.local_state["daily_loss_from_tradable_base"] = self._format_decimal(daily_loss)
        
        # Check daily loss limit
        if daily_loss >= self.settings.risk.live_max_daily_loss_inr:
             self.alert.alert(f"DAILY LOSS LIMIT REACHED ({daily_loss:.2f} >= {self.settings.risk.live_max_daily_loss_inr:.2f}). Blocking trading.", level="CRITICAL")
             self.kill_switch_active = True
             self.local_state["kill_switch_active"] = True


    def _risk_decimal(self, name: str, default: Decimal) -> Decimal:
        return _decimal_or_default(getattr(self.settings.risk, name, default), default)

    def _risk_int(self, name: str, default: int) -> int:
        try:
            return int(getattr(self.settings.risk, name, default))
        except (TypeError, ValueError):
            return default

    def _risk_bool(self, name: str, default: bool) -> bool:
        value = getattr(self.settings.risk, name, default)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "y", "on"}
        return bool(value)

    def _record_post_profit_exit(
        self,
        *,
        pair: str,
        direction: SignalDirection,
        exit_price: Decimal,
        net_pnl: Decimal,
        closed_at_ms: int,
    ) -> None:
        if net_pnl <= 0 or not self._risk_bool("post_profit_reentry_guard_enabled", False):
            return
        interval_ms = interval_to_ms(self.execution_interval or self.interval)
        cooldown_candles = max(self._risk_int("post_profit_reentry_cooldown_candles", 12), 0)
        until_ms = closed_at_ms + (cooldown_candles * interval_ms)
        key = (pair, direction.value)
        self._same_direction_profit_cooldown_until_ms[key] = max(
            self._same_direction_profit_cooldown_until_ms.get(key, 0),
            until_ms,
        )
        self._post_profit_reentry_state[pair] = {
            "direction": direction.value,
            "exit_price": str(exit_price),
            "closed_at_ms": str(closed_at_ms),
            "cooldown_until_ms": str(until_ms),
            "pullback_seen": False,
            "net_pnl": str(net_pnl),
        }
        logger.info(
            "[%s] Post-profit same-direction guard active for %s until %s.",
            pair,
            direction.value,
            datetime.fromtimestamp(until_ms / 1000, timezone.utc).isoformat(),
        )

    def _post_profit_reentry_block(
        self,
        signal: StrategySignal,
        candle: OHLCVCandle,
    ) -> tuple[str, dict[str, Any]] | None:
        if not self._risk_bool("post_profit_reentry_guard_enabled", False):
            return None
        if not is_entry_signal(signal) or signal.direction is None:
            return None
        key = (signal.pair, signal.direction.value)
        until_ms = self._same_direction_profit_cooldown_until_ms.get(key, 0)
        candle_close_ms = _int_or_zero(getattr(candle, "close_time_ms", 0))
        if candle_close_ms < until_ms:
            return (
                "post_profit_reentry_cooldown",
                {
                    "cooldown_until_ms": until_ms,
                    "direction": signal.direction.value,
                },
            )

        state = self._post_profit_reentry_state.get(signal.pair)
        if not state or state.get("direction") != signal.direction.value:
            return None
        if bool(state.get("pullback_seen")):
            return None

        exit_price = _decimal_or_default(state.get("exit_price"), Decimal("0"))
        if exit_price <= 0:
            return None
        indicators = latest_indicator_snapshot(self.series_by_pair.get(signal.pair, CandleSeries(maxlen=1)))
        atr = indicators.atr if indicators else None
        if post_profit_pullback_seen(
            direction=signal.direction,
            exit_price=exit_price,
            candle_high=candle.high,
            candle_low=candle.low,
            atr=atr,
            pullback_atr=self._risk_decimal("post_profit_reentry_pullback_atr", Decimal("0.75")),
            pullback_pct=self._risk_decimal("post_profit_reentry_pullback_pct", Decimal("1.0")),
        ):
            state["pullback_seen"] = True
            self._post_profit_reentry_state[signal.pair] = state
            return None

        return (
            "post_profit_pullback_required",
            {
                "direction": signal.direction.value,
                "last_profit_exit_price": str(exit_price),
                "pullback_atr": str(self._risk_decimal("post_profit_reentry_pullback_atr", Decimal("0.75"))),
                "pullback_pct": str(self._risk_decimal("post_profit_reentry_pullback_pct", Decimal("1.0"))),
            },
        )

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
             from app.backtest.data_loader import rows_from_candles_response, REST_RESOLUTION_BY_INTERVAL
             now = datetime.now(timezone.utc)

             for interval in dict.fromkeys((self.interval, self.execution_interval)):
                  if interval not in REST_RESOLUTION_BY_INTERVAL:
                       logger.error("[%s] Interval %s not supported for REST fallback.", pair, interval)
                       continue

                  res = self.client.get_candles(
                       pair=pair,
                       from_ts=int((now - timedelta(seconds=interval_to_ms(interval)*5/1000)).timestamp()),
                       to_ts=int(now.timestamp()),
                       resolution=REST_RESOLUTION_BY_INTERVAL[interval]
                  )

                  rows = rows_from_candles_response(res)
                  if rows:
                       series = rest_rows_to_series(pair=pair, interval=interval, rows=rows)
                       for candle in series:
                            self._on_candle(candle, source="rest_fallback")

             logger.debug("[%s] Polled REST candles for fallback.", pair)
        except Exception as exc:
             logger.error("[%s] REST poll fallback failed: %s", pair, exc)

    def _warm_up(self) -> None:
        from app.backtest.data_loader import load_historical_candle_series
        now_ms = int(time.time() * 1000)
        strategy_lookback = _strategy_warmup_lookback(self.strategy_engine.strategies)
        
        for pair in self.pairs:
            logger.info(
                "[%s] Live schedule strategy_interval=%s next_strategy_close=%s "
                "execution_interval=%s next_execution_close=%s",
                pair,
                self.interval,
                _format_interval_boundary(
                    _next_interval_boundary_ms(now_ms, self.interval)
                ),
                self.execution_interval,
                _format_interval_boundary(
                    _next_interval_boundary_ms(now_ms, self.execution_interval)
                ),
            )
            logger.info("[%s] Warming up indicators...", pair)
            try:
                warmup_series = load_historical_candle_series(
                    client=self.client, 
                    pair=pair, 
                    interval=self.interval, 
                    lookback=strategy_lookback
                )
                for c in warmup_series:
                    # Warmup candles are loaded into series but do NOT trigger evaluation
                    self.series_by_pair[pair].add(c)
                    
                    if c.is_closed and c.close_time_ms <= now_ms:
                         # Track only truly closed past candles
                         self._last_evaluated_strategy_candle_ms[(pair, self.interval)] = max(
                              self._last_evaluated_strategy_candle_ms.get((pair, self.interval), 0), 
                              c.close_time_ms
                         )

                    logger.debug("[%s] Warmup candle loaded: close=%d is_closed=%s", pair, c.close_time_ms, c.is_closed)

                try:
                    execution_warmup = load_historical_candle_series(
                        client=self.client,
                        pair=pair,
                        interval=self.execution_interval,
                        lookback=100,
                    )
                    for c in execution_warmup:
                        self.execution_series_by_pair[pair].add(c)
                        if c.is_closed and c.close_time_ms <= now_ms:
                            self._last_evaluated_strategy_candle_ms[
                                (pair, self.execution_interval)
                            ] = max(
                                self._last_evaluated_strategy_candle_ms.get(
                                    (pair, self.execution_interval),
                                    0,
                                ),
                                c.close_time_ms,
                            )
                    logger.info(
                        "[%s] Execution warmup complete: %d candles @ %s.",
                        pair,
                        len(execution_warmup),
                        self.execution_interval,
                    )
                except Exception as exc:
                    logger.warning(
                        "[%s] Execution warmup failed for %s; momentum ignition waits for live history: %s",
                        pair,
                        self.execution_interval,
                        exc,
                    )

                self._is_warming_up[pair] = False
                logger.info("[%s] Warmup complete. Indicators ready.", pair)
            except Exception as exc:
                logger.error("[%s] Warmup failed: %s", pair, exc)

    def _worker_loop(self) -> None:
        logger.info("Event worker thread started.")
        while not self._stop_requested:
            try:
                # Wait for event from queue
                try:
                    event = self._event_queue.get(timeout=1.0)
                except queue.Empty:
                    with self._processing_lock:
                        self._flush_matured_candles()
                    continue

                # Use the processing lock to ensure thread safety
                with self._processing_lock:
                     self._process_event_sync(event)
                     self._flush_matured_candles()
                
                self._event_queue.task_done()
            except Exception as exc:
                logger.error("Event worker error: %s", exc)

    def _process_event_sync(self, event: Any) -> None:
        # Mark activity for this pair whenever ANY event arrives
        if hasattr(event, 'pair') and event.pair in self._last_ws_message_ms_by_pair:
             self._last_ws_message_ms_by_pair[event.pair] = int(time.time() * 1000)

        if event.event_type == "candle":
             if event.pair in self.pairs:
                  candle = OHLCVCandle.from_candle_event(event)
                  self._on_candle(candle, source="websocket")
        
        elif event.event_type == "trade":
             if event.pair in self.pairs:
                  # Feed trade to series (this updates the current partial candle)
                  self.series_by_pair[event.pair].apply_trade(event)

    def _on_market_event(self, event: Any) -> None:
        self._last_ws_message_ms = int(time.time() * 1000)
        
        # Offload to worker to keep WebSocket thread responsive
        self._event_queue.put(event)

    def _candle_past_close_buffer(self, candle: OHLCVCandle, now_ms: int) -> bool:
        settings = getattr(self, "settings", None)
        buffer_ms = max(
            0,
            int(getattr(settings, "live_closed_candle_buffer_ms", 0)),
        )
        return now_ms >= candle.close_time_ms + 1 + buffer_ms

    def _candle_ready_for_entry(self, candle: OHLCVCandle, now_ms: int) -> bool:
        """Return whether a closed candle is past the configured safety buffer."""

        return candle.is_closed and self._candle_past_close_buffer(candle, now_ms)

    def _defer_candle_until_closed(self, candle: OHLCVCandle, source: str) -> None:
        key = (candle.pair, candle.interval, candle.open_time_ms)
        pending = getattr(self, "_pending_closed_candles", None)
        if pending is None:
            pending = {}
            self._pending_closed_candles = pending
        pending[key] = (candle, source)

    def _flush_matured_candles(self) -> None:
        """Process the latest update only after its candle is indisputably closed."""

        now_ms = int(time.time() * 1000)
        pending_candles = getattr(self, "_pending_closed_candles", {})
        ready_keys = sorted(
            (
                key
                for key, (candle, _) in list(pending_candles.items())
                if self._candle_past_close_buffer(candle, now_ms)
            ),
            key=lambda key: (key[2], key[0], key[1]),
        )
        for key in ready_keys:
            pending = pending_candles.pop(key, None)
            if pending is None:
                continue
            candle, source = pending
            self._on_candle(
                replace(candle, is_closed=True),
                source=f"{source}_deferred",
            )

    def _on_candle(self, candle: OHLCVCandle, source: str = "unknown") -> None:
        # Synchronous candle handler called by worker or rest poll
        if candle.pair not in self.pairs:
            return
            
        now_ms = int(time.time() * 1000)
        
        # 1. Check Dry-Run Stops (High priority, any interval)
        self._check_dry_run_stops(candle)

        # 2. Strategy Evaluation (Strategy Interval Only)
        if candle.interval == self.interval:
            interval_ms = interval_to_ms(self.interval)
            # Boundary alignment check
            if candle.open_time_ms % interval_ms != 0:
                 return

            # Candle timestamps are authoritative. Some websocket payloads mark a
            # forming candle as closed, so entries wait until the configured
            # post-close buffer has elapsed.
            is_closed_confirmed = self._candle_ready_for_entry(candle, now_ms)

            if is_closed_confirmed:
                last_eval = self._last_evaluated_strategy_candle_ms.get((candle.pair, candle.interval), 0)
                if candle.close_time_ms <= last_eval:
                    logger.debug("[%s %s] Skipping duplicate closed candle close_time_ms=%d (last_eval=%d)", 
                                 candle.pair, candle.interval, candle.close_time_ms, last_eval)
                    return

                # Check cooldown/stopped out prevention
                last_stop_ms = self._stopped_out_candles.get(candle.pair, 0)
                cooldown_candles = self.settings.risk.reentry_cooldown_candles
                if cooldown_candles > 0:
                     interval_ms = interval_to_ms(self.interval)
                     # cooldown_until_ms blocks candles for N full intervals after the stop
                     # If stop at 14:08 on 5m, and cooldown=1: wait until 14:13.
                     # The 14:10 candle (open_time=14:10) will be <= 14:13 and thus blocked.
                     cooldown_until_ms = last_stop_ms + (cooldown_candles * interval_ms)
                     if candle.open_time_ms <= cooldown_until_ms:
                          logger.info("[%s %s] Cooldown blocked entry evaluation after recent position exit (cooldown_candles=%d)", 
                                      candle.pair, candle.interval, cooldown_candles)
                          # We still mark it as processed/evaluated so we don't try again
                          self._last_evaluated_strategy_candle_ms[(candle.pair, candle.interval)] = candle.close_time_ms
                          return

                # Valid new closed candle: Evaluate Strategy
                self.series_by_pair[candle.pair].add(candle)
                
                # Robust evaluation: ensure tracker is updated even if order placement fails
                try:
                     self._process_trading_candle(candle, allow_entries=True, source=source)
                except Exception as exc:
                     logger.error("[%s %s] Strategy evaluation/execution CRASHED: %s", candle.pair, candle.interval, exc)
                finally:
                     # Update trackers to prevent infinite loop on the same candle
                     self._last_evaluated_strategy_candle_ms[(candle.pair, candle.interval)] = candle.close_time_ms
                     self._last_processed_candle_ms[(candle.pair, candle.interval)] = candle.close_time_ms
                     self.candle_count += 1
                     # Update current forming to 0 since it just closed
                     self._current_forming_candle_ms[(candle.pair, candle.interval)] = 0
            else:
                # Still forming
                self._defer_candle_until_closed(candle, source)
                previous_forming_close = self._current_forming_candle_ms.get(
                    (candle.pair, candle.interval),
                    0,
                )
                self._current_forming_candle_ms[(candle.pair, candle.interval)] = candle.close_time_ms
                if previous_forming_close != candle.close_time_ms:
                    logger.info(
                        "[%s %s] Forming strategy candle observed; scheduled_close=%s",
                        candle.pair,
                        candle.interval,
                        _format_interval_boundary(candle.close_time_ms + 1),
                    )

        # 3. Profit Protection (Execution Interval)
        if candle.interval == self.execution_interval:
            self.execution_series_by_pair[candle.pair].add(candle)
            self._apply_profit_protection(candle)

            is_closed_confirmed = self._candle_ready_for_entry(candle, now_ms)
            if is_closed_confirmed:
                key = (candle.pair, candle.interval)
                last_eval = self._last_evaluated_strategy_candle_ms.get(key, 0)
                if candle.close_time_ms > last_eval:
                    try:
                        self._process_execution_momentum_candle(candle, source=source)
                    except Exception as exc:
                        logger.error(
                            "[%s %s] Execution momentum evaluation CRASHED: %s",
                            candle.pair,
                            candle.interval,
                            exc,
                        )
                    finally:
                        self._last_evaluated_strategy_candle_ms[key] = candle.close_time_ms
                        self._last_processed_candle_ms[key] = candle.close_time_ms
                        self.save_local_state()
            else:
                self._defer_candle_until_closed(candle, source)

    def _check_dry_run_stops(self, candle: OHLCVCandle) -> bool:
        pos = self.local_state["positions"].get(candle.pair)
        if not pos or pos.get("source") != "dry_run" or pos.get("status") != "active":
             return False
             
        stop_price = Decimal(str(pos.get("stop_loss_trigger", 0)))
        if stop_price <= 0:
             return False
             
        direction = normalize_position_direction(pos)

        stop_hit = False
        if direction == SignalDirection.LONG and candle.low <= stop_price:
             stop_hit = True
        elif direction == SignalDirection.SHORT and candle.high >= stop_price:
             stop_hit = True
             
        if stop_hit:
             entry_price = Decimal(str(pos["avg_price"]))
             exit_price = stop_price # Execute at stop price
             
             logger.warning(
                 "DRY-RUN STOP HIT: pair=%s, direction=%s, entry=%.2f, stop=%.2f, candle_low/high=%.2f, exit_price=%.2f",
                 candle.pair, direction.value, entry_price, stop_price,
                 candle.low if direction == SignalDirection.LONG else candle.high,
                 exit_price
             )
             
             raw_pnl, simulated_fee = self._simulated_close_accounting(
                  candle.pair,
                  pos,
                  exit_price,
             )
             
             net_pnl = raw_pnl - simulated_fee
             self.alert.alert(f"DRY-RUN STOP LOSS HIT for {candle.pair} @ {exit_price}")
             self._update_accounting_balances(net_pnl)
             self._record_post_profit_exit(
                  pair=candle.pair,
                  direction=direction,
                  exit_price=exit_price,
                  net_pnl=net_pnl,
                  closed_at_ms=candle.close_time_ms,
             )

             # Point 9: Track stopped out candle to prevent re-entry
             self._stopped_out_candles[candle.pair] = candle.close_time_ms

             # Close position
             pos["status"] = "closed"
             pos["active_pos"] = "0"
             self._dry_run_positions.pop(candle.pair, None)
             self.local_state["positions"].pop(candle.pair, None)
             self.local_state.get("position_metadata", {}).pop(candle.pair, None)
             self.save_local_state()
             return True
             
        return False

    def _add_diagnostic(self, data: dict[str, Any] | StrategySignal | RiskDecision, pair: str, candle: OHLCVCandle) -> None:
        from app.utils.formatting import format_decision_reason
        diags = self.local_state.setdefault("recent_diagnostics", [])
        now_iso = datetime.now(timezone.utc).isoformat()
        
        # 1. Determine action and metadata
        if isinstance(data, StrategySignal):
             action = data.action.value
             reason = data.reason
             confidence = str(data.confidence)
             metadata = data.metadata
             sig_dir = data.direction.value if data.direction else None
             trading_mode = "live"
        elif hasattr(data, 'approved'): # RiskDecision
             action = "APPROVED" if data.approved else "REJECTED"
             reason = data.reason
             confidence = str(data.signal.confidence)
             metadata = {**data.metadata, "risk_reason": data.reason}
             sig_dir = data.signal.direction.value if data.signal.direction else None
             # Derive from decision if possible, else default to live
             trading_mode = "live"
        else: # dict
             action = data.get("action", "unknown")
             reason = data.get("reason", "unknown")
             confidence = data.get("confidence", "0")
             metadata = data.get("metadata", {})
             sig_dir = metadata.get("direction")
             trading_mode = data.get("trading_mode", "live")

        # Use dry_run suffix if enabled
        if self.settings.live_pilot_dry_run:
             trading_mode = "live_dry_run"

        # 2. Determine relation (position context)
        relation = "no position"
        existing_pos = self.local_state["positions"].get(pair)
        if existing_pos and abs(Decimal(str(existing_pos.get("active_pos", 0)))) > 0:
             pos_dir = normalize_position_direction(existing_pos).value
             if sig_dir:
                  relation = "same direction" if sig_dir == pos_dir else f"opposing {pos_dir}"
             else:
                  relation = f"has {pos_dir}"

        # 3. Deduplicate identical HOLD/SKIPPED reasons for the same pair
        if action.lower() in ("hold", "skipped") and diags:
             last = diags[-1]
             if last.get("pair") == pair and last.get("action").lower() == action.lower() and last.get("reason") == reason:
                  last["time"] = now_iso
                  return

        diags.append({
             "time": now_iso,
             "candle_time": datetime.fromtimestamp(candle.close_time_ms / 1000, timezone.utc).isoformat(),
             "pair": pair,
             "action": action.upper(),
             "confidence": confidence,
             "reason": reason,
             "relation": relation,
             "user_reason": format_decision_reason(data),
             "trading_mode": trading_mode,
             "metadata": to_jsonable(metadata)
        })
        
        if len(diags) > _LIVE_DIAGNOSTICS_MAX_ROWS:
            self.local_state["recent_diagnostics"] = diags[-_LIVE_DIAGNOSTICS_MAX_ROWS:]

    def _process_trading_candle(self, candle: OHLCVCandle, allow_entries: bool = True, source: str = "unknown") -> None:
        if self.kill_switch_active:
            logger.info("[%s] Strategy evaluation skipped: KILL SWITCH ACTIVE", candle.pair)
            return

        series = self.series_by_pair[candle.pair]
        if len(series) < 20: # Minimum candles for indicators
             logger.debug("[%s] Strategy evaluation skipped: Not enough candles (%d < 20)", candle.pair, len(series))
             return

        indicators = latest_indicator_snapshot(series)
        context = StrategyContext(
            pair=candle.pair,
            interval=self.interval,
            candles=series,
            indicators=indicators,
            features=self._strategy_features(candle.pair),
        )
        
        signals = self.strategy_engine.evaluate(context)
        
        if candle.interval == self.interval and candle.is_closed:
             logger.info("[%s] Strategy evaluated closed candle interval=%s open_time_ms=%d close_time_ms=%d source=%s signals=%d", 
                         candle.pair, candle.interval, candle.open_time_ms, candle.close_time_ms, source, len(signals))

        logger.debug("[%s] Strategy evaluated. Signals emitted: %d, allow_entries=%s", candle.pair, len(signals), allow_entries)
        
        entry_processed = False
        
        if not any(is_entry_signal(s) for s in signals):
             logger.debug("[%s] strategy evaluation: no entry signals emitted for candle at %d", candle.pair, candle.close_time_ms)
        
        for signal in signals:
            if signal.action == SignalAction.HOLD:
                 self._add_diagnostic(signal, candle.pair, candle)
                 continue
            
            if is_entry_signal(signal):
                 if not allow_entries:
                      logger.debug("[%s] Entry signal ignored: allow_entries=False (intrabar)", candle.pair)
                      self._add_diagnostic({
                           "action": "skipped",
                           "reason": "intrabar_skip",
                           "confidence": str(signal.confidence),
                           "metadata": {**signal.metadata, "user_reason": "Skipped: Entry not allowed intrabar"}
                      }, candle.pair, candle)
                      continue
                 if entry_processed:
                      logger.info("[%s] Skipping secondary entry signal in same candle.", candle.pair)
                      self._add_diagnostic({
                           "action": "skipped",
                           "reason": "secondary_entry_skip",
                           "confidence": str(signal.confidence),
                           "metadata": {**signal.metadata, "user_reason": "Skipped: Secondary entry in same candle"}
                      }, candle.pair, candle)
                      continue
                 self._handle_signal(signal, candle)
                 entry_processed = True
            else:
                 self._handle_signal(signal, candle)

    def _process_execution_momentum_candle(self, candle: OHLCVCandle, *, source: str) -> None:
        """Evaluate closed execution candles for strategies that support intrabar entries."""

        if self.kill_switch_active:
            return
        has_hybrid = any(
            getattr(strategy, "name", "") == "hybrid_meta_v2"
            for strategy in self.strategy_engine.strategies
        )
        has_fib = any(
            getattr(strategy, "name", "") == "fib_ma_pullback"
            for strategy in self.strategy_engine.strategies
        )
        if not has_hybrid and not has_fib:
            return
        if candle.pair in self._pending_entry_pairs:
            return

        position = self.local_state.get("positions", {}).get(candle.pair)
        if position and abs(Decimal(str(position.get("active_pos", 0)))) > 0:
            return

        last_stop_ms = self._stopped_out_candles.get(candle.pair, 0)
        cooldown_candles = self.settings.risk.reentry_cooldown_candles
        if cooldown_candles > 0:
             interval_ms = interval_to_ms(self.interval)
             cooldown_until_ms = last_stop_ms + (cooldown_candles * interval_ms)
             if candle.open_time_ms <= cooldown_until_ms:
                  logger.info(
                       "[%s %s] Cooldown blocked momentum ignition evaluation after recent position exit (cooldown_candles=%d)",
                       candle.pair,
                       candle.interval,
                       cooldown_candles
                  )
                  return

        series = self.series_by_pair[candle.pair]
        if len(series) < 20:
            return

        context = StrategyContext(
            pair=candle.pair,
            interval=self.interval,
            candles=series,
            indicators=latest_indicator_snapshot(series),
            features=self._strategy_features(
                candle.pair,
                enable_momentum_ignition=has_hybrid,
            ),
        )
        signals = self.strategy_engine.evaluate(context)
        for signal in signals:
            if signal.strategy_name == "fib_ma_pullback":
                if not is_entry_signal(signal):
                    continue
                if signal.metadata.get("entry_type") != "fib_ma_intrabar_pullback":
                    continue
                live_signal = replace(
                    signal,
                    metadata={
                        **signal.metadata,
                        "live_execution_fib_entry": True,
                        "context_interval": self.interval,
                        "execution_interval": self.execution_interval,
                        "evaluation_source": source,
                    },
                )
                logger.info(
                    "[%s %s] Closed execution candle confirmed Fib MA pullback trigger; submitting to live risk checks",
                    candle.pair,
                    candle.interval,
                )
                self._handle_signal(live_signal, candle)
                return

            if signal.strategy_name != "hybrid_meta_v2":
                continue
            if not is_entry_signal(signal) or not signal.metadata.get("momentum_ignition"):
                continue

            final_score = _decimal_or_default(signal.metadata.get("final_score"), Decimal("0"))
            ema_score = _decimal_or_default(signal.metadata.get("ema_score"), Decimal("0"))
            aligned = (
                signal.direction == SignalDirection.LONG
                and final_score > 0
                and ema_score > 0
            ) or (
                signal.direction == SignalDirection.SHORT
                and final_score < 0
                and ema_score < 0
            )
            if not aligned:
                logger.info(
                    "[%s %s] Momentum ignition rejected: 1h context misaligned direction=%s final_score=%s ema_score=%s",
                    candle.pair,
                    candle.interval,
                    signal.direction.value if signal.direction else "none",
                    final_score,
                    ema_score,
                )
                self._add_diagnostic(
                    {
                        "action": "rejected",
                        "reason": "momentum_ignition_context_misaligned",
                        "confidence": str(signal.confidence),
                        "metadata": {
                            **signal.metadata,
                            "context_interval": self.interval,
                            "execution_interval": self.execution_interval,
                        },
                    },
                    candle.pair,
                    candle,
                )
                return

            live_signal = replace(
                signal,
                metadata={
                    **signal.metadata,
                    "live_execution_momentum_entry": True,
                    "momentum_context_aligned": True,
                    "context_interval": self.interval,
                    "execution_interval": self.execution_interval,
                    "evaluation_source": source,
                },
            )
            logger.info(
                "[%s %s] Closed execution candle confirmed aligned momentum ignition; submitting to live risk checks",
                candle.pair,
                candle.interval,
            )
            self._handle_signal(live_signal, candle)
            return

    def _handle_signal(self, signal: StrategySignal, candle: OHLCVCandle) -> None:
        # 0. Idempotency Check: Prevent duplicate execution for same candle + side
        # key = pair|interval|close_time|side
        signal_key = f"{signal.pair}|{signal.interval}|{candle.close_time_ms}|{signal.direction.value if signal.direction else 'unknown'}"
        if signal_key in self._processed_signals:
             logger.debug("[%s] Skipping already processed signal: %s", signal.pair, signal_key)
             return

        if signal.confidence < self.settings.risk.live_min_confidence:
             logger.info("[%s] Signal rejected: Low confidence (%.2f < %.2f)", 
                         signal.pair, signal.confidence, self.settings.risk.live_min_confidence)
             self._add_diagnostic({
                  "action": "rejected",
                  "reason": "confidence_below_threshold",
                  "confidence": str(signal.confidence),
                  "metadata": {
                       **signal.metadata,
                       "min_confidence": str(self.settings.risk.live_min_confidence)
                  }
             }, signal.pair, candle)
             # Mark as processed even if rejected by confidence
             self._processed_signals.add(signal_key)
             return

        if is_entry_signal(signal):
             blocked, reason, metadata = self._sync_pair_position_before_entry(signal.pair)
             if blocked:
                  diagnostic_metadata = {
                       **signal.metadata,
                       **metadata,
                       "direction": signal.direction.value if signal.direction else None,
                  }
                  self._add_diagnostic({
                       "action": "rejected",
                       "reason": reason,
                       "confidence": str(signal.confidence),
                       "metadata": diagnostic_metadata,
                  }, signal.pair, candle)
                  self._processed_signals.add(signal_key)
                  self.save_local_state()
                  return

             post_profit_block = self._post_profit_reentry_block(signal, candle)
             if post_profit_block:
                  reason, block_metadata = post_profit_block
                  diagnostic_metadata = {
                       **signal.metadata,
                       **block_metadata,
                       "direction": signal.direction.value if signal.direction else None,
                  }
                  self._add_diagnostic({
                       "action": "rejected",
                       "reason": reason,
                       "confidence": str(signal.confidence),
                       "metadata": diagnostic_metadata,
                  }, signal.pair, candle)
                  self._processed_signals.add(signal_key)
                  self.save_local_state()
                  return

        tradable_equity = Decimal(self.local_state.get("tradable_base", str(self.starting_equity)))
        open_positions = self._get_open_positions()
        existing_required_margin = sum(
            (
                position.notional / position.leverage
                for position in open_positions
                if position.leverage > 0
            ),
            Decimal("0"),
        )
        available_equity = risk_capacity_before_reservations(
            allocated_capital=tradable_equity,
            existing_required_margin=existing_required_margin,
            portfolio=self._portfolio_snapshot,
        )
        
        # Force live risk semantics inside live loop
        effective_trading_mode = "live"

        # Get effective leverage for this pair
        requested_leverage = self._pair_leverage.get(signal.pair, self.leverage if isinstance(self.leverage, Decimal) else Decimal("1"))
        pair_existing_required_margin = sum(
            (
                position.notional / position.leverage
                for position in open_positions
                if position.pair == signal.pair and position.leverage > 0
            ),
            Decimal("0"),
        )
        pair_sizing_equity = None
        if self.settings.risk.max_margin_per_pair > 0:
            pair_sizing_equity = max(
                Decimal("0"),
                self.settings.risk.max_margin_per_pair - pair_existing_required_margin,
            )

        risk_context = RiskContext(
            signal=signal,
            account_equity=tradable_equity,
            available_equity=available_equity,
            sizing_equity=pair_sizing_equity,
            risk_base_mode="tradable_equity",
            open_positions=open_positions,
            trading_mode=effective_trading_mode,
            live_trading_enabled=self.settings.live_trading_enabled,
            instrument=self._instruments.get(signal.pair),
            requested_leverage=requested_leverage,
            quote_to_margin_rate=self.settings.quote_to_margin_rate,
            unit_contract_value=get_unit_contract_value(signal.pair)
        )

        # HARD GUARD: Never approve non-live risk in live loop
        if str(risk_context.trading_mode or "").lower() != "live":
             logger.critical("[%s] CRITICAL: Live loop attempted to use non-live risk mode: %s", signal.pair, risk_context.trading_mode)
             self._add_diagnostic({
                  "action": "blocked",
                  "reason": "invalid_risk_mode",
                  "metadata": {"mode": risk_context.trading_mode, "user_reason": "Blocked: live loop attempted to use non-live risk mode"}
             }, signal.pair, candle)
             return
        
        decision = self.risk_manager.evaluate(risk_context)
        
        # Add risk decision to diagnostics
        self._add_diagnostic(decision, signal.pair, candle)

        if decision.approved:
            # User requested wording: "Execution approved" or "Live order ready"
            self.alert.alert(f"Execution approved: {signal.action.value} {signal.pair} @ {candle.close} qty={decision.position_size}")
            pending_real_entry = (
                 is_entry_signal(signal) and not self.settings.live_pilot_dry_run
            )
            if pending_real_entry:
                 self._pending_entry_pairs.add(signal.pair)
            try:
                 report = self.execution_engine.process_decision(decision)
            except Exception:
                 self._pending_entry_pairs.discard(signal.pair)
                 raise
            self.alert.alert(f"Execution report: {report.reason}")
            
            # Mark as processed after execution attempt
            self._processed_signals.add(signal_key)

            # Point 4: Use structured flags instead of string matching
            if report.safety_failure:
                 logger.critical("[%s] SAFETY FAILURE DETECTED: %s. ACTIVATING PERSISTENT KILL SWITCH.", signal.pair, report.reason)
                 self.alert.alert(f"SAFETY FAILURE for {signal.pair}: {report.reason}. KILL SWITCH ACTIVATED.", level="CRITICAL")
                 self.kill_switch_active = True
                 self.local_state["kill_switch_active"] = True
                 self.save_local_state() # Persist immediately

            if report.accepted or report.submitted_to_exchange:
                 # Point 5: Preserve position state if order may have been submitted
                 if is_entry_signal(signal):
                      entry_fee = self._entry_fee_from_report(report, signal.pair)
                      entry_order_ids = self._entry_order_ids_from_report(report)
                      metadata_by_pair = self.local_state.setdefault("position_metadata", {})
                      if signal.pair not in metadata_by_pair and signal.stop_loss and signal.entry_price:
                           r_val = abs(signal.entry_price - signal.stop_loss)
                           metadata_by_pair[signal.pair] = {
                                "initial_stop_loss": str(signal.stop_loss),
                                "initial_entry_price": str(signal.entry_price),
                                "initial_r": str(r_val),
                                "peak_price": str(signal.entry_price),
                                "max_r_hit": "0",
                                "entry_fee_margin_currency": str(entry_fee),
                                "entry_order_ids": entry_order_ids,
                                "opened_at_ms": candle.close_time_ms,
                                "strategy_name": signal.strategy_name,
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
                      else:
                           # Real entry (even if TPSL failed)
                           # Try to get best known data
                           pos_snapshot = None
                           # If ExecutionReport doesn't have it, try reconciliation
                           if hasattr(report, 'exchange_response') and report.exchange_response:
                                # Map from exchange response if possible
                                pass
                           
                           # Re-fetch from exchange to be sure
                           try:
                                pos_snapshot = self.execution_engine.sync.fetch_position(signal.pair)
                           except Exception:
                                pass
                           
                           if pos_snapshot and pos_snapshot.is_open:
                                pos_dict = pos_snapshot.to_dict()
                                pos_dict["source"] = "exchange"
                                # Point 5: Mark for manual inspection if safety failure
                                if report.requires_manual_reconciliation:
                                     pos_dict["requires_manual_reconciliation"] = True
                                     pos_dict["safety_failure_reason"] = report.reason
                                
                                self.local_state["positions"][signal.pair] = pos_dict
                           elif report.submitted_to_exchange:
                                # Order submitted but no position snapshot? 
                                # Save what we have for manual inspection
                                self.local_state["positions"][signal.pair] = {
                                     "pair": signal.pair,
                                     "status": "submitted_unknown_fill",
                                     "requires_manual_reconciliation": True,
                                     "safety_failure": report.safety_failure,
                                     "reason": report.reason,
                                     "direction": signal.direction.value if signal.direction else "unknown",
                                     "source": "exchange"
                                }
                 
                 elif is_exit_signal(signal):
                      if self.settings.live_pilot_dry_run:
                           logger.info("[%s] dry-run exit creates simulated fill", signal.pair)
                           pos = self.local_state["positions"].get(signal.pair)
                           if pos:
                                exit_price = candle.close
                                raw_pnl, simulated_fee = self._simulated_close_accounting(
                                     signal.pair,
                                     pos,
                                     exit_price,
                                )
                                
                                net_pnl = raw_pnl - simulated_fee
                                logger.info("[%s] dry-run closed accounting uses simulated fills: PnL=%.2f, Fee=%.2f", signal.pair, raw_pnl, simulated_fee)
                                self._update_accounting_balances(net_pnl)
                                self._record_post_profit_exit(
                                     pair=signal.pair,
                                     direction=normalize_position_direction(pos),
                                     exit_price=exit_price,
                                     net_pnl=net_pnl,
                                     closed_at_ms=candle.close_time_ms,
                                )
                           self._dry_run_positions.pop(signal.pair, None)
                           self.local_state["positions"].pop(signal.pair, None)
                           self.local_state.get("position_metadata", {}).pop(signal.pair, None)
                      else:
                           # Real exit reconciliation happens in reconcile()
                           pass

            if pending_real_entry and not (
                 report.accepted or report.submitted_to_exchange
            ):
                 self._pending_entry_pairs.discard(signal.pair)
            try:
                 self.reconcile()
            finally:
                 if pending_real_entry:
                      self._pending_entry_pairs.discard(signal.pair)
        else:
            if "already has an open position" in decision.reason and self.settings.live_pilot_dry_run:
                 logger.info("[%s] entry blocked because dry-run position already exists", signal.pair)
            logger.info("Signal rejected: %s", decision.reason)

    def _apply_profit_protection(self, candle: OHLCVCandle) -> None:
        if self.kill_switch_active:
             return

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

        favorable_price = candle.high if direction == SignalDirection.LONG else candle.low
        favorable_points = (
            favorable_price - entry_price
            if direction == SignalDirection.LONG
            else entry_price - favorable_price
        )
        favorable_r = favorable_points / r_val

        max_r = max(Decimal(meta.get("max_r_hit", "0")), current_r, favorable_r)
        meta["current_r"] = str(current_r)
        meta["favorable_r"] = str(favorable_r)
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
        
        # 1. Profit giveback guard: protect a configurable fraction of best R seen.
        giveback = compute_profit_giveback_stop(
             entry_price=entry_price,
             initial_r=r_val,
             direction=direction,
             current_stop=current_stop,
             current_price=current_price,
             favorable_price=favorable_price,
             previous_max_r=max_r,
             activation_r=self._risk_decimal("profit_giveback_activation_r", Decimal("1.0")),
             lock_fraction=self._risk_decimal("profit_giveback_lock_fraction", Decimal("0.50")),
             min_lock_r=self._risk_decimal("profit_giveback_min_lock_r", Decimal("0.25")),
             tighten_after_r=self._risk_decimal("profit_giveback_tighten_after_r", Decimal("3.0")),
             tighten_fraction=self._risk_decimal("profit_giveback_tighten_fraction", Decimal("0.70")),
        )
        max_r = giveback.max_r
        meta["current_r"] = str(giveback.current_r)
        meta["favorable_r"] = str(giveback.favorable_r)
        meta["max_r_hit"] = str(giveback.max_r)
        meta["profit_giveback_active"] = str(self._risk_bool("profit_giveback_guard_enabled", False) and giveback.active).lower()
        meta["profit_giveback_stop"] = str(giveback.stop) if giveback.stop is not None else None
        meta["profit_giveback_lock_r"] = str(giveback.lock_r)
        meta["profit_giveback_max_r_hit"] = str(giveback.max_r)
        if self._risk_bool("profit_giveback_guard_enabled", False) and giveback.active and giveback.stop is not None:
             if direction == SignalDirection.LONG:
                  new_stop = max(new_stop, giveback.stop)
             else:
                  new_stop = min(new_stop, giveback.stop)
        # 2. ATR trailing can add extra protection after the configured R threshold.
        atr_trail_activation_r = self._risk_decimal("atr_trail_activation_r", Decimal("2.0"))
        if max_r >= atr_trail_activation_r:
             indicators = latest_indicator_snapshot(self.series_by_pair[candle.pair])
             atr = indicators.atr or Decimal("0")
             if atr > 0:
                  trail_dist = atr * self._risk_decimal("atr_trailing_multiple", Decimal("1.2"))
                  if direction == SignalDirection.LONG:
                       atr_stop = peak_price - trail_dist
                       new_stop = max(new_stop, atr_stop)
                  else:
                       atr_stop = peak_price + trail_dist
                       new_stop = min(new_stop, atr_stop)
        # 3. Keep a late-arriving spike update executable relative to current price.
        instrument = self._instruments.get(candle.pair)
        tick_buffer = (
            instrument.tick_size
            if instrument and instrument.tick_size and instrument.tick_size > 0
            else Decimal("0")
        )
        if direction == SignalDirection.LONG and new_stop >= current_price:
             new_stop = max(current_stop, current_price - tick_buffer)
             meta["profit_protection_crossed_market"] = True
        elif direction == SignalDirection.SHORT and new_stop <= current_price:
             new_stop = min(current_stop, current_price + tick_buffer)
             meta["profit_protection_crossed_market"] = True

        # 4. Final Exchange Tick Normalization
        raw_new_stop = new_stop
        if instrument and instrument.tick_size:
             # Long stop: round DOWN (safer/conservative for Long)
             # Short stop: round UP (safer/conservative for Short)
             mode = "down" if direction == SignalDirection.LONG else "up"
             new_stop = round_price(new_stop, instrument.tick_size, mode)

        if new_stop != current_stop:
             if direction == SignalDirection.LONG and new_stop < current_stop:
                  return
             if direction == SignalDirection.SHORT and new_stop > current_stop:
                  return
                  
             logger.info("[%s] Tightening live stop raw_old=%.5f raw_new=%.5f rounded_new=%.5f tick=%s R=%.2f", 
                         candle.pair, current_stop, raw_new_stop, new_stop, 
                         instrument.tick_size if instrument else "unknown", max_r)
             
             if pos_data.get("source") == "dry_run":
                  pos_data["stop_loss_trigger"] = str(new_stop)
                  self.alert.alert(f"Dry-run stop tightened for {candle.pair}: {new_stop} ({max_r:.2f}R hit)")
                  self.save_local_state()
                  return

             report = self.execution_engine.update_tpsl(candle.pair, stop_loss=new_stop, reason=f"Profit protection: {max_r:.2f}R hit")
             if report.accepted:
                  pos_data["stop_loss_trigger"] = str(new_stop)
                  self.alert.alert(f"Live stop tightened for {candle.pair}: {new_stop} ({max_r:.2f}R hit)")
             else:
                  # Non-retryable check: if 400 validation error, don't try this EXACT price again
                  # By NOT updating pos_data["stop_loss_trigger"], current_stop remains the old value.
                  # But next tick we might calculate a DIFFERENT new_stop and try again.
                  self.alert.alert(f"FAILED to tighten live stop for {candle.pair}: {report.reason}", level="ERROR")
                  if "Price should be divisible by" in report.reason:
                       logger.error("[%s] Tick size violation for stop update: price=%s tick=%s", 
                                    candle.pair, new_stop, instrument.tick_size if instrument else "unknown")
                  self.kill_switch_active = True
                  
        self.save_local_state()

    def _strategy_features(
        self,
        pair: str,
        *,
        enable_momentum_ignition: bool = False,
    ) -> dict[str, Any]:
        """Expose live execution history and open-position state to strategies."""

        features: dict[str, Any] = {}
        execution_series = self.execution_series_by_pair.get(pair)
        if execution_series is not None:
             execution_candles = list(execution_series)[-30:]
             if execution_candles:
                  features["execution_candles"] = execution_candles
                  features["execution_interval"] = getattr(
                        self,
                        "execution_interval",
                        self.settings.execution_interval,
                   )

        # Global overrides for strategies (backtest_config is used for this)
        config: dict[str, Any] = {}
        
        # Determine leverage for ROE -> Price % conversion
        leverage = getattr(self, "_pair_leverage", {}).get(pair, Decimal("1"))
        if leverage <= 0:
             leverage = Decimal("1")

        take_profit_pct = getattr(self, "take_profit_pct", None)
        if take_profit_pct is not None:
             # Convert ROE % to Price Move %
             # Price % = ROE % / Leverage
             config["take_profit_pct"] = take_profit_pct / leverage
             
        stop_loss_pct = getattr(self, "stop_loss_pct", None)
        if stop_loss_pct is not None:
             # Convert ROE % to Price Move %
             config["stop_loss_pct"] = stop_loss_pct / leverage

        if enable_momentum_ignition:
             parent = self.series_by_pair[pair].latest()
             profile = pair_profile_for(pair)
             ignition_config = {
                  "intrabar_reversal_breakout_enabled": True,
                  "previous_parent_high": parent.high if parent else Decimal("0"),
                  "previous_parent_low": parent.low if parent else Decimal("0"),
                  "reversal_breakout_ignition_volume_ratio": Decimal("3.0"),
                  "reversal_breakout_ignition_body_ratio": Decimal("0.70"),
                  "reversal_breakout_ignition_close_position_ratio": Decimal("0.75"),
                  "reversal_breakout_ignition_max_extension_atr": Decimal("5.0"),
                  "reversal_breakout_ignition_risk_multiplier": Decimal("0.25"),
             }
             for key, val in ignition_config.items():
                  config.setdefault(key, val)
             
             for key in ignition_config:
                  if key in profile.config_overrides:
                       config[key] = profile.config_overrides[key]
             features["pair_profile"] = profile.metadata()
        
        if config:
             features["backtest_config"] = config

        position = self.local_state.get("positions", {}).get(pair)
        if not position or abs(Decimal(str(position.get("active_pos", 0)))) <= 0:
             return features
        metadata = self.local_state.get("position_metadata", {}).get(pair, {})
        direction = normalize_position_direction(position)
        features["open_position"] = {
             "pair": pair,
             "direction": direction.value,
             "quantity": abs(Decimal(str(position.get("active_pos", 0)))),
             "entry_price": Decimal(str(position.get("avg_price", 0))),
             "opened_at_ms": int(
                  metadata.get("opened_at_ms")
                  or position.get("updated_at")
                  or 0
             ),
             "strategy_name": metadata.get("strategy_name") or self.strategy_name,
             "stop_loss": _positive_decimal_or_none(
                  position.get("stop_loss_trigger") or metadata.get("initial_stop_loss")
             ),
             "take_profit": _positive_decimal_or_none(
                  position.get("take_profit_trigger")
             ),
             "metadata": metadata,
        }
        return features

    def _get_open_positions(self) -> Any:
        found = []
        combined_positions = dict(self.local_state["positions"])
        combined_positions.update(self._portfolio_positions)
        for pair, pos in combined_positions.items():
            signed_qty = _decimal_or_default(pos.get("active_pos"), Decimal("0"))
            active_qty = abs(signed_qty)
            if pos and active_qty > 0:
                entry_price = _positive_decimal_or_none(pos.get("avg_price"))
                if entry_price is None:
                     logger.warning("[%s] Skipping malformed open position with no entry price.", pair)
                     continue
                # Point 1: Extract latest known protective stop
                sl = _positive_decimal_or_none(
                    pos.get("stop_loss_trigger") or 
                    pos.get("stop_loss") or 
                    pos.get("sl_trigger_price") or 
                    pos.get("stop_price") or
                    pos.get("trailing_stop") or
                    pos.get("latest_trailing_stop") or
                    pos.get("breakeven_stop") or
                    pos.get("profit_lock_stop") or
                    pos.get("dynamic_atr_stop")
                )
                
                # Point 4: Live position without stop-loss is unsafe.
                if sl is None and pos.get("source") == "exchange":
                     self._report_missing_protective_stop(pair)
                elif sl is not None:
                     self._clear_missing_protective_stop_alert(pair)

                from app.risk.models import get_unit_contract_value
                found.append(OpenPosition(
                    pair=pos.get("pair", pair),
                    direction=SignalDirection.LONG if signed_qty > 0 else SignalDirection.SHORT,
                    quantity=active_qty,
                    entry_price=entry_price,
                    leverage=(
                        _positive_decimal_or_none(pos.get("leverage"))
                        or Decimal("1")
                    ),
                    stop_loss=sl,
                    quote_to_margin_rate=(
                        _positive_decimal_or_none(
                            pos.get("settlement_currency_avg_price")
                        )
                        or self.settings.quote_to_margin_rate
                    ),
                    unit_contract_value=get_unit_contract_value(pair)
                ))
        return tuple(found)

    def _report_missing_protective_stop(self, pair: str) -> None:
        """Alert once while an exchange position remains without a stop."""

        alerted_pairs = getattr(self, "_missing_stop_alerted_pairs", None)
        if alerted_pairs is None:
            alerted_pairs = set()
            self._missing_stop_alerted_pairs = alerted_pairs
        if pair in alerted_pairs:
            return

        alerted_pairs.add(pair)
        self.alert.alert(
            f"UNSAFE LIVE STATE: {pair} has NO STOP-LOSS!",
            level="CRITICAL",
        )

    def _clear_missing_protective_stop_alert(self, pair: str) -> None:
        """Re-arm the one-shot alert after the unsafe condition is resolved."""

        alerted_pairs = getattr(self, "_missing_stop_alerted_pairs", None)
        if alerted_pairs is None or pair not in alerted_pairs:
            return

        alerted_pairs.remove(pair)
        logger.info(
            "[%s] Protective stop detected or position closed; "
            "missing-stop alert re-armed.",
            pair,
        )

    def _update_dashboard_state(self) -> None:
        # Portfolio positions are account-wide; local positions remain bot-owned state.
        displayed_positions = (
            self._portfolio_positions
            if not self.local_state.get("portfolio_positions_stale")
            else self.local_state["positions"]
        )
        all_pos = [
            pos
            for pos in displayed_positions.values()
            if abs(Decimal(str(pos.get("active_pos", 0)))) > 0
        ]
        
        # Use first pair as primary for dashboard metadata if needed, or just a list
        primary_pair = self.pairs[0] if self.pairs else "B-BTC_USDT"
        
        tradable_base = Decimal(self.local_state.get("tradable_base", "0"))
        open_positions = self._get_open_positions()
        existing_required_margin = sum(
            (
                position.notional / position.leverage
                for position in open_positions
                if position.leverage > 0
            ),
            Decimal("0"),
        )
        capacity_before_reservations = risk_capacity_before_reservations(
            allocated_capital=tradable_base,
            existing_required_margin=existing_required_margin,
            portfolio=self._portfolio_snapshot,
        )
        usable_capital = max(
            Decimal("0"),
            capacity_before_reservations - existing_required_margin,
        )
        
        # Use maximum configured leverage for global "Max Notional" display
        effective_leverage = max(self._pair_leverage.values()) if self._pair_leverage else Decimal("1")
        max_notional = tradable_base * effective_leverage
        
        dashboard_state = {
            "run_id": self.run_id,
            "state_path": str(self.state_path),
            "running": not self._stop_requested,
            "execution_mode": (
                "live" if not self.settings.live_pilot_dry_run else "live_dry_run"
            ),
            "live_dry_run": self.settings.live_pilot_dry_run,
            "pair": primary_pair,
            "watchlist": list(self.pairs),
            "interval": self.interval,
            "execution_interval": self.execution_interval,
            "strategy": self.strategy_name,
            "candle_count": self.candle_count,
            "session_started_at": self._session_started_at,
            "live_positions_json": json.dumps(all_pos),
            "initial_equity": str(self.allocated_capital),
            "allocated_capital": str(self.allocated_capital),
            "tradable_base": str(tradable_base),
            "usable_capital": str(usable_capital),
            "max_leveraged_notional": str(max_notional),
            "effective_leverage": str(effective_leverage),
            "locked_profit": str(self.local_state.get("locked_profit", "0")),
            "last_updated": datetime.now(timezone.utc).isoformat(),
        }
        self.local_state.update(dashboard_state)
        _update_live_state(**dashboard_state)

def _positive_decimal_or_none(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        val = Decimal(str(value))
        return val if val > 0 else None
    except Exception:
        return None


def _decimal_or_default(value: Any, default: Decimal) -> Decimal:
    try:
        return Decimal(str(value))
    except Exception:
        return default


def _int_or_zero(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _strategy_warmup_lookback(strategies: list[Any]) -> int:
    """Return enough parent candles for every selected strategy to initialize."""

    lookback = 100
    for strategy in strategies:
        try:
            lookback = max(lookback, int(getattr(strategy, "warmup_lookback", 100)))
        except (TypeError, ValueError):
            continue
    return lookback


def _next_interval_boundary_ms(now_ms: int, interval: str) -> int:
    """Return the next UTC-aligned interval boundary after now."""

    interval_ms = interval_to_ms(interval)
    return ((now_ms // interval_ms) + 1) * interval_ms


def _format_interval_boundary(boundary_ms: int) -> str:
    """Show an exchange candle boundary in both local time and UTC."""

    utc_dt = datetime.fromtimestamp(boundary_ms / 1000, timezone.utc)
    local_dt = utc_dt.astimezone()
    return (
        f"{local_dt.strftime('%Y-%m-%d %H:%M:%S %Z')} "
        f"({utc_dt.strftime('%Y-%m-%d %H:%M:%S UTC')})"
    )


def _validate_live_intervals(strategy_interval: str, execution_interval: str) -> None:
    """Require a real intrabar execution interval that divides the strategy bar."""

    strategy_ms = interval_to_ms(strategy_interval)
    execution_ms = interval_to_ms(execution_interval)
    if execution_ms >= strategy_ms:
        raise ValueError(
            "Live execution interval must be shorter than the strategy interval: "
            f"{execution_interval} is not shorter than {strategy_interval}."
        )
    if strategy_ms % execution_ms != 0:
        raise ValueError(
            "Live execution interval must divide the strategy interval exactly: "
            f"{strategy_interval}/{execution_interval}."
        )
