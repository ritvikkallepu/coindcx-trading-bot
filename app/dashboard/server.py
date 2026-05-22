from __future__ import annotations

import json
import logging
from dataclasses import replace
from decimal import Decimal, InvalidOperation
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from app.backtest.data_loader import (
    REST_RESOLUTION_BY_INTERVAL,
    load_historical_candle_series,
    load_historical_candle_series_between,
)
from app.backtest.engine import BacktestEngine
from app.backtest.models import BacktestConfig
from app.config import Settings, load_settings
from app.dashboard.state import (
    DashboardDefaults,
    build_backtest_dashboard_payload,
    build_status_payload,
)
from app.dashboard.web import DASHBOARD_HTML
from app.data.candle_builder import interval_to_ms
from app.data.open_interest import (
    OpenInterestFeatureSeries,
    load_binance_open_interest_proxy,
)
from app.exchange.coindcx_rest import CoinDCXFuturesClient
from app.exchange.errors import CoinDCXError
from app.fees import COINDCX_INR_M_MAKER_FEE_RATE, COINDCX_INR_M_TAKER_FEE_RATE
from app.risk.manager import RiskManager
from app.risk.models import InstrumentMetadata
from app.research.history import record_backtest_result
from app.strategies.defaults import STRATEGY_CHOICES, strategy_engine_for_name
from app.utils.json import to_jsonable
from app.utils.logging import configure_logging


OPEN_INTEREST_STRATEGIES = {"hybrid_meta", "hybrid_meta_v2", "adaptive_hybrid"}


def _normalize_dashboard_pair(value: object) -> str:
    """Accept dashboard display pairs and convert them to CoinDCX internal pairs.

    The searchable dropdown stores values like B-BSB_USDT, but the free-text
    watchlist is easy to fill with display labels like BSB-USDT or B-BSB-USDT.
    REST candles use the internal underscore form.
    """
    text = str(value or "").strip().upper().replace(" ", "")
    if not text:
        return text

    if text.startswith("B-"):
        body = text[2:]
    else:
        body = text

    if "_" in body:
        base, quote = body.split("_", 1)
    elif "-" in body:
        base, quote = body.rsplit("-", 1)
    else:
        return text if text.startswith("B-") else f"B-{text}"

    return f"B-{base}_{quote}"


class DashboardHTTPServer(ThreadingHTTPServer):
    def __init__(
        self,
        server_address: tuple[str, int],
        *,
        settings: Settings,
        defaults: DashboardDefaults,
    ) -> None:
        import threading
        super().__init__(server_address, DashboardRequestHandler)
        self.settings = settings
        self.defaults = defaults
        self._paper_thread: threading.Thread | None = None
        self._paper_loop: Any = None  # PaperTradingLoop instance
        self._paper_lock = threading.Lock()


class DashboardRequestHandler(BaseHTTPRequestHandler):
    server: DashboardHTTPServer

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path in {"/", "/index.html"}:
            self._send_html(DASHBOARD_HTML)
            return
        if parsed.path == "/api/health":
            self._send_json({"ok": True})
            return
        if parsed.path == "/api/status":
            self._send_json(
                build_status_payload(
                    settings=self.server.settings,
                    defaults=self.server.defaults,
                )
            )
            return
        if parsed.path == "/api/pairs":
            self._send_pairs()
            return
        if parsed.path == "/api/backtest":
            params = _flatten_query(parse_qs(parsed.query))
            self._send_backtest(params)
            return
        if parsed.path == "/api/paper-status":
            from app.live.paper_loop import get_live_state
            self._send_json(get_live_state())
            return
        if parsed.path == "/api/paper-trades":
            self._send_paper_trades()
            return
        self._send_json({"error": "Not found"}, status=HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/backtest":
            self._send_backtest(self._read_json_body())
            return
        if parsed.path == "/api/paper-start":
            self._handle_paper_start(self._read_json_body())
            return
        if parsed.path == "/api/paper-stop":
            self._handle_paper_stop()
            return
        if parsed.path == "/api/paper-reset":
            self._handle_paper_reset()
            return
        self._send_json({"error": "Not found"}, status=HTTPStatus.NOT_FOUND)

    def log_message(self, format: str, *args: object) -> None:
        logging.getLogger("app.dashboard").debug(format, *args)

    def _handle_paper_start(self, params: dict[str, Any]) -> None:
        import threading
        from app.live.paper_loop import PaperTradingLoop
        from app.live.paper_loop import get_live_state
        from app.strategies.defaults import STRATEGY_CHOICES
        from app.backtest.data_loader import REST_RESOLUTION_BY_INTERVAL

        state = get_live_state()
        if state["running"]:
            self._send_json({"error": "Paper loop is already running."}, 
                            status=HTTPStatus.CONFLICT)
            return

        pair_val = params.get("pair") or params.get("pairs") or self.server.settings.default_pair
        if isinstance(pair_val, str):
            pairs = [_normalize_dashboard_pair(p) for p in pair_val.split(",") if p.strip()]
        elif isinstance(pair_val, list):
            pairs = [_normalize_dashboard_pair(p) for p in pair_val if str(p).strip()]
        else:
            pairs = [_normalize_dashboard_pair(self.server.settings.default_pair)]

        interval = str(params.get("interval") or "15m")
        strategy = str(params.get("strategy") or "adaptive_hybrid")
        
        if interval not in REST_RESOLUTION_BY_INTERVAL:
            self._send_json({"error": f"Unsupported interval: {interval}"}, 
                            status=HTTPStatus.BAD_REQUEST)
            return
        if strategy not in STRATEGY_CHOICES:
            self._send_json({"error": f"Unknown strategy: {strategy}"}, 
                            status=HTTPStatus.BAD_REQUEST)
            return

        settings = self.server.settings
        from decimal import Decimal, InvalidOperation
        starting_equity = settings.paper_starting_equity
        if params.get("starting_equity"):
            try:
                starting_equity = Decimal(str(params["starting_equity"]))
            except InvalidOperation:
                pass

        try:
            leverage = Decimal(str(params.get("leverage", settings.paper_leverage)))
            risk_pct = Decimal(str(params.get("risk_pct", settings.risk.max_risk_per_trade_pct)))
            max_daily_loss_pct = Decimal(
                str(params.get("max_daily_loss_pct", settings.risk.max_daily_loss_pct))
            )
            max_open = int(params.get("max_open_positions", settings.risk.max_open_positions))
            allow_multi = _bool_param(
                params.get("allow_multi_pair_positions"),
                settings.risk.allow_multi_pair_positions,
            )
            allow_pyramid = _bool_param(
                params.get("allow_same_pair_pyramiding"),
                settings.risk.allow_same_pair_pyramiding,
            )
            max_margin_usage = Decimal(
                str(params.get("max_margin_usage_pct", settings.risk.max_margin_usage_pct))
            )
            paper_intrabar_enabled = _bool_param(
                params.get("paper_intrabar_enabled"),
                settings.paper_intrabar_enabled,
            )
            use_partial_parent_candle = _bool_param(
                params.get("use_partial_parent_candle"),
                settings.use_partial_parent_candle,
            )
            max_entries_per_parent_candle = int(
                params.get(
                    "max_entries_per_parent_candle",
                    settings.max_entries_per_parent_candle,
                )
            )
            trailing_stop_enabled = _bool_param(
                params.get("trailing_stop_enabled"),
                settings.risk.trailing_stop_enabled,
            )
            atr_exits_enabled = _bool_param(
                params.get("atr_dynamic_exits_enabled"),
                False,
            )
            profit_lock_enabled = _bool_param(
                params.get("profit_lock_enabled"),
                True,
            )
        except (ValueError, InvalidOperation) as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return

        strategy_interval = str(params.get("strategy_interval") or interval)
        execution_interval = str(params.get("execution_interval") or "1m")

        # Build modified risk settings
        new_risk = replace(
            settings.risk,
            max_risk_per_trade_pct=risk_pct,
            max_daily_loss_pct=max_daily_loss_pct,
            max_open_positions=max_open,
            allow_multi_pair_positions=allow_multi,
            allow_same_pair_pyramiding=allow_pyramid,
            max_margin_usage_pct=max_margin_usage,
            trailing_stop_enabled=trailing_stop_enabled,
            atr_stop_enabled=atr_exits_enabled,
            atr_take_profit_enabled=atr_exits_enabled,
            atr_trailing_enabled=atr_exits_enabled,
            profit_lock_enabled=profit_lock_enabled,
        )

        # Build a modified settings
        from dataclasses import replace as dc_replace
        effective_settings = dc_replace(
            settings, 
            paper_starting_equity=starting_equity,
            paper_leverage=leverage,
            paper_intrabar_enabled=paper_intrabar_enabled,
            strategy_interval=strategy_interval,
            execution_interval=execution_interval,
            use_partial_parent_candle=use_partial_parent_candle,
            max_entries_per_parent_candle=max_entries_per_parent_candle,
            risk=new_risk
        )

        loop = PaperTradingLoop(effective_settings, strategy_name=strategy)
        
        with self.server._paper_lock:
            self.server._paper_loop = loop

        def run_loop():
            try:
                loop.run(pairs, interval)
            except Exception as exc:
                from app.live.paper_loop import _update_live_state
                _update_live_state(running=False, error=str(exc))
                logging.getLogger(__name__).exception("Paper loop crashed: %s", exc)

        t = threading.Thread(target=run_loop, daemon=True, name="paper-loop")
        with self.server._paper_lock:
            self.server._paper_thread = t
        t.start()
        self._send_json({"started": True, "pairs": pairs, "interval": interval, 
                         "strategy": strategy})

    def _handle_paper_stop(self) -> None:
        with self.server._paper_lock:
            loop = self.server._paper_loop
        if loop is None:
            self._send_json({"error": "No paper loop is running."}, 
                            status=HTTPStatus.CONFLICT)
            return
        loop.stop()
        self._send_json({"stopped": True})

    def _handle_paper_reset(self) -> None:
        from app.live.paper_loop import get_active_loop, _update_live_state
        from app.persistence.paper_state import PaperStateStore, PaperSessionStore
        import os
        
        loop = get_active_loop()
        if loop:
            loop.stop()
        
        # Clear persistent stores
        PaperStateStore().clear()
        PaperSessionStore().clear()
        
        # Clear summary csv
        if os.path.exists("paper_trades.csv"):
            os.remove("paper_trades.csv")
            
        # Reset live state
        _update_live_state(
            candle_count=0,
            equity="0",
            realized_pnl="0",
            fees_paid="0",
            open_positions=0,
            total_fills=0,
            positions_json="[]",
            last_updated=None,
        )
        
        self._send_json({"reset": True})

    def _send_pairs(self) -> None:
        from pathlib import Path
        import time
        cache_path = Path("data/coindcx_inr_futures_pairs_cache.json")
        now = time.time()
        
        # Try load cache
        if cache_path.exists():
            try:
                with open(cache_path, "r", encoding="utf-8") as f:
                    cached = json.load(f)
                # Cache valid for 1 hour
                if now - cached.get("timestamp", 0) < 3600:
                    self._send_json(cached["data"])
                    return
            except Exception:
                pass
        
        # Fetch live
        client = CoinDCXFuturesClient(self.server.settings)
        try:
            # CoinDCX active_instruments returns list of strings like "B-BTC_USDT"
            # for a given margin currency.
            margin = "INR"
            raw_pairs = client.get_active_instruments(margin_currency=margin)
            
            pairs = []
            for p in sorted(raw_pairs):
                # "B-BTC_USDT" -> "BTC-USDT"
                display = p
                if p.startswith("B-"):
                    display = p[2:].replace("_", "-")
                
                pairs.append({
                    "pair": p,
                    "display_name": display,
                    "margin_currency": margin,
                    "active": True
                })
            
            data = {
                "market": "futures",
                "margin": margin,
                "source": "live",
                "pairs": pairs
            }
            
            # Save to cache
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            with open(cache_path, "w", encoding="utf-8") as f:
                json.dump({"timestamp": now, "data": data}, f, indent=2)
                
            self._send_json(data)
        except Exception as exc:
            logging.getLogger("app.dashboard").error("Failed to fetch pairs: %s", exc)
            # Fallback to empty or previous cache if available
            self._send_json({
                "market": "futures",
                "margin": "INR",
                "source": "fallback",
                "pairs": []
            })

    def _send_paper_trades(self) -> None:
        import csv
        from pathlib import Path
        path = Path("paper_trades.csv")
        if not path.exists():
            self._send_json({"trades": []})
            return
        trades = []
        with open(path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                trades.append(dict(row))
        self._send_json({"trades": list(reversed(trades[-100:]))})

    def _send_backtest(self, params: dict[str, Any]) -> None:
        try:
            payload = run_backtest_for_dashboard(
                settings=self.server.settings,
                defaults=self.server.defaults,
                params=params,
            )
        except ValueError as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
        except CoinDCXError as exc:
            self._send_json(
                {"error": "CoinDCX public data request failed.", "detail": str(exc)},
                status=HTTPStatus.BAD_GATEWAY,
            )
        except Exception as exc:
            logging.getLogger(__name__).exception("Dashboard backtest failed")
            self._send_json(
                {"error": "Dashboard backtest failed.", "detail": str(exc)},
                status=HTTPStatus.INTERNAL_SERVER_ERROR,
            )
        else:
            self._send_json(payload)

    def _read_json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        if not raw:
            return {}
        value = json.loads(raw.decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("JSON body must be an object.")
        return value

    def _send_html(self, html: str) -> None:
        encoded = html.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(encoded)

    def _send_json(
        self,
        payload: dict[str, Any],
        *,
        status: HTTPStatus = HTTPStatus.OK,
    ) -> None:
        # Pre-process payload to be JSON-safe (handles Decimals, datetimes, dataclasses)
        jsonable_payload = to_jsonable(payload)
        encoded = json.dumps(
            jsonable_payload, 
            indent=2, 
            sort_keys=True, 
            default=str
        ).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(encoded)


def run_dashboard(
    *,
    host: str,
    port: int,
    defaults: DashboardDefaults,
) -> None:
    settings = load_settings()
    configure_logging(settings)
    logging.getLogger("app.risk.manager").setLevel(logging.WARNING)
    logging.getLogger("app.broker.paper").setLevel(logging.WARNING)

    server = DashboardHTTPServer((host, port), settings=settings, defaults=defaults)
    print(f"Dashboard running at http://{host}:{server.server_port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Dashboard stopped.", flush=True)
    finally:
        server.server_close()


def run_backtest_for_dashboard(
    *,
    settings: Settings,
    defaults: DashboardDefaults,
    params: dict[str, Any],
) -> dict[str, Any]:
    pair = _normalize_dashboard_pair(params.get("pair") or defaults.pair)
    interval = str(params.get("interval") or defaults.interval)
    strategy = str(params.get("strategy") or defaults.strategy)
    lookback = _int_param(params.get("lookback"), defaults.lookback)
    equity = _decimal_param(params.get("equity"), defaults.equity)
    leverage = _decimal_param(params.get("leverage"), defaults.leverage)
    risk_per_trade_pct = _decimal_param(
        params.get("risk_per_trade_pct"),
        settings.risk.max_risk_per_trade_pct,
    )
    max_daily_loss_pct = _decimal_param(
        params.get("max_daily_loss_pct"),
        settings.risk.max_daily_loss_pct,
    )
    compound_risk_equity = _bool_param(
        params.get("compound_risk_equity"),
        defaults.compound_risk_equity,
    )
    stop_loss_pct = _optional_decimal_param(
        params.get("stop_loss_pct"),
        name="stop_loss_pct",
    )
    take_profit_pct = _optional_decimal_param(
        params.get("take_profit_pct"),
        name="take_profit_pct",
    )
    fee_config = _fee_config_params(params, defaults)
    slippage_pct = _decimal_param(params.get("slippage_pct"), defaults.slippage_pct)
    stop_slippage_pct = _nonnegative_decimal_param(
        params.get("stop_slippage_pct"),
        defaults.stop_slippage_pct,
        name="stop_slippage_pct",
    )
    funding_fee_rate = _rate_from_pct_or_rate(
        pct=params.get("funding_fee_pct"),
        rate=params.get("funding_fee_rate"),
        default=defaults.funding_fee_rate,
        allow_negative=True,
    )
    funding_interval_hours = _positive_int_param(
        params.get("funding_interval_hours"),
        defaults.funding_interval_hours,
        name="funding_interval_hours",
    )
    trailing_stop_enabled = _bool_param(
        params.get("trailing_stop_enabled"),
        settings.risk.trailing_stop_enabled,
    )
    trailing_stop_activation_pct = _decimal_param(
        params.get("trailing_stop_activation_pct"),
        settings.risk.trailing_stop_activation_pct,
    )
    trailing_stop_distance_pct = _decimal_param(
        params.get("trailing_stop_distance_pct"),
        settings.risk.trailing_stop_distance_pct,
    )
    atr_dynamic_exits_enabled = _bool_param(
        params.get("atr_dynamic_exits_enabled"),
        defaults.atr_dynamic_exits_enabled,
    )
    atr_stop_enabled = _bool_param(
        params.get("atr_stop_enabled"),
        defaults.atr_stop_enabled,
    )
    atr_take_profit_enabled = _bool_param(
        params.get("atr_take_profit_enabled"),
        defaults.atr_take_profit_enabled,
    )
    atr_trailing_enabled = _bool_param(
        params.get("atr_trailing_enabled"),
        defaults.atr_trailing_enabled,
    )
    atr_entry_filter_enabled = _bool_param(
        params.get("atr_entry_filter_enabled"),
        defaults.atr_entry_filter_enabled,
    )
    atr_policy_mode = _atr_policy_mode_param(
        params.get("atr_policy_mode"),
        defaults.atr_policy_mode,
    )
    atr_period = _positive_int_param(
        params.get("atr_period"),
        defaults.atr_period,
        name="atr_period",
    )
    atr_stop_multiple = _optional_decimal_param(
        params.get("atr_stop_multiple"),
        name="atr_stop_multiple",
    ) or defaults.atr_stop_multiple
    atr_take_profit_multiple = _optional_decimal_param(
        params.get("atr_take_profit_multiple"),
        name="atr_take_profit_multiple",
    ) or defaults.atr_take_profit_multiple
    atr_trailing_multiple = _optional_decimal_param(
        params.get("atr_trailing_multiple"),
        name="atr_trailing_multiple",
    ) or defaults.atr_trailing_multiple
    atr_take_profit_mode = _atr_take_profit_mode_param(
        params.get("atr_take_profit_mode"),
        defaults.atr_take_profit_mode,
    )
    execution_interval = str(
        params.get("execution_interval") or defaults.execution_interval or ""
    ).strip()
    intrabar_reentry_enabled = _bool_param(
        params.get("intrabar_reentry_enabled"),
        defaults.intrabar_reentry_enabled,
    )
    max_reentries_per_candle = _nonnegative_int_param(
        params.get("max_reentries_per_candle"),
        defaults.max_reentries_per_candle,
        name="max_reentries_per_candle",
    )
    reentry_cooldown_candles = _nonnegative_int_param(
        params.get("reentry_cooldown_candles"),
        defaults.reentry_cooldown_candles,
        name="reentry_cooldown_candles",
    )
    stop_loss_cooldown_candles = _nonnegative_int_param(
        params.get("stop_loss_cooldown_candles"),
        defaults.stop_loss_cooldown_candles,
        name="stop_loss_cooldown_candles",
    )
    max_consecutive_losses = _nonnegative_int_param(
        params.get("max_consecutive_losses"),
        defaults.max_consecutive_losses,
        name="max_consecutive_losses",
    )
    loss_cooldown_candles = _nonnegative_int_param(
        params.get("loss_cooldown_candles"),
        defaults.loss_cooldown_candles,
        name="loss_cooldown_candles",
    )
    recent_count = _int_param(params.get("recent_count"), 20)

    if interval not in REST_RESOLUTION_BY_INTERVAL:
        raise ValueError(
            f"Unsupported interval {interval}. Choose one of: "
            f"{', '.join(sorted(REST_RESOLUTION_BY_INTERVAL))}."
        )
    if intrabar_reentry_enabled and not execution_interval:
        execution_interval = _auto_execution_interval(interval)
        if not execution_interval:
            intrabar_reentry_enabled = False
    if execution_interval:
        if execution_interval not in REST_RESOLUTION_BY_INTERVAL:
            raise ValueError(
                f"Unsupported execution interval {execution_interval}. Choose one of: "
                f"{', '.join(sorted(REST_RESOLUTION_BY_INTERVAL))}."
            )
        if interval_to_ms(execution_interval) >= interval_to_ms(interval):
            raise ValueError("Execution interval must be smaller than the strategy interval.")
        if interval_to_ms(interval) % interval_to_ms(execution_interval) != 0:
            raise ValueError(
                "Execution interval must divide the strategy interval cleanly."
            )
    if intrabar_reentry_enabled and max_reentries_per_candle <= 0:
        raise ValueError("Max Re-entries / Candle must be at least 1 when Intrabar Re-entry is on.")
    if strategy not in STRATEGY_CHOICES:
        raise ValueError(
            f"Unsupported strategy {strategy}. Choose one of: {', '.join(STRATEGY_CHOICES)}."
        )
    if lookback <= 0:
        raise ValueError("lookback must be positive.")

    client = CoinDCXFuturesClient(settings)
    series = load_historical_candle_series(
        client=client,
        pair=pair,
        interval=interval,
        lookback=lookback,
    )
    if len(series) == 0:
        raise ValueError("No candles loaded; cannot run dashboard backtest.")
    open_interest_features = _load_open_interest_proxy_safe(
        pair=pair,
        interval=interval,
        series=series,
        strategy=strategy,
    )
    execution_series = None
    if execution_interval:
        first = series[0]
        last = series[-1]
        execution_lookback = max(
            int((last.close_time_ms - first.open_time_ms + 1) / interval_to_ms(execution_interval))
            + 5,
            1,
        )
        execution_series = load_historical_candle_series_between(
            client=client,
            pair=pair,
            interval=execution_interval,
            from_ts=int(first.open_time_ms / 1000),
            to_ts=int(last.close_time_ms / 1000) + 1,
            maxlen=execution_lookback,
        )
        if len(execution_series) == 0:
            raise ValueError("No execution candles loaded; cannot run intrabar backtest.")

    config = BacktestConfig(
        pair=pair,
        interval=interval,
        starting_equity=equity,
        leverage=leverage,
        strategy_name=strategy,
        risk_per_trade_pct=risk_per_trade_pct,
        compound_risk_equity=compound_risk_equity,
        stop_loss_pct=stop_loss_pct,
        take_profit_pct=take_profit_pct,
        maker_fee_rate=fee_config["maker_fee_rate"],
        taker_fee_rate=fee_config["taker_fee_rate"],
        fee_gst_rate=fee_config["fee_gst_rate"],
        entry_fee_type=fee_config["entry_fee_type"],
        exit_fee_type=fee_config["exit_fee_type"],
        slippage_pct=slippage_pct,
        stop_slippage_pct=stop_slippage_pct,
        funding_fee_rate=funding_fee_rate,
        funding_interval_hours=funding_interval_hours,
        trailing_stop_enabled=trailing_stop_enabled,
        trailing_stop_activation_pct=trailing_stop_activation_pct,
        trailing_stop_distance_pct=trailing_stop_distance_pct,
        atr_dynamic_exits_enabled=atr_dynamic_exits_enabled,
        atr_stop_enabled=atr_stop_enabled,
        atr_take_profit_enabled=atr_take_profit_enabled,
        atr_trailing_enabled=atr_trailing_enabled,
        atr_entry_filter_enabled=atr_entry_filter_enabled,
        atr_policy_mode=atr_policy_mode,
        atr_period=atr_period,
        atr_stop_multiple=atr_stop_multiple,
        atr_take_profit_multiple=atr_take_profit_multiple,
        atr_trailing_multiple=atr_trailing_multiple,
        atr_take_profit_mode=atr_take_profit_mode,
        execution_interval=execution_interval or None,
        paper_intrabar_enabled=bool(execution_interval),
        strategy_interval=interval,
        intrabar_reentry_enabled=intrabar_reentry_enabled,
        max_reentries_per_candle=max_reentries_per_candle,
        reentry_cooldown_candles=reentry_cooldown_candles,
        stop_loss_cooldown_candles=stop_loss_cooldown_candles,
        max_consecutive_losses=max_consecutive_losses,
        loss_cooldown_candles=loss_cooldown_candles,
        max_daily_loss_pct=max_daily_loss_pct,
    )
    engine = BacktestEngine(
        config=config,
        strategy_engine=strategy_engine_for_name(strategy),
        risk_manager=RiskManager(
            replace(
                settings.risk,
                max_risk_per_trade_pct=risk_per_trade_pct,
                max_daily_loss_pct=max_daily_loss_pct,
                trailing_stop_enabled=trailing_stop_enabled,
                trailing_stop_activation_pct=trailing_stop_activation_pct,
                trailing_stop_distance_pct=trailing_stop_distance_pct,
            )
        ),
        instrument=_instrument_metadata(client, pair=pair, margin_currency=settings.futures_margin_currency),
        open_interest_features=open_interest_features,
    )
    result = engine.run(series, execution_candles=execution_series)
    payload = build_backtest_dashboard_payload(result, recent_count=recent_count)
    payload["history"] = record_backtest_result(result, source="dashboard")
    return payload


def _instrument_metadata(
    client: CoinDCXFuturesClient,
    *,
    pair: str,
    margin_currency: str,
) -> InstrumentMetadata:
    try:
        instrument_data = client.get_instrument(pair, margin_currency)
    except CoinDCXError:
        return InstrumentMetadata(pair=pair)
    if isinstance(instrument_data, dict):
        return InstrumentMetadata.from_mapping(pair, instrument_data)
    return InstrumentMetadata(pair=pair)


def _load_open_interest_proxy_safe(
    *,
    pair: str,
    interval: str,
    series,
    strategy: str,
) -> OpenInterestFeatureSeries:
    if strategy.strip().lower() not in OPEN_INTEREST_STRATEGIES:
        return OpenInterestFeatureSeries([])
    try:
        return load_binance_open_interest_proxy(
            pair=pair,
            interval=interval,
            candles=series,
        )
    except Exception as exc:
        logging.getLogger(__name__).warning(
            "Binance open-interest proxy unavailable for %s %s: %s",
            pair,
            interval,
            exc,
        )
        return OpenInterestFeatureSeries([])


def _auto_execution_interval(interval: str) -> str:
    return {
        "24h": "4h",
        "1d": "4h",
        "4h": "1h",
        "2h": "1h",
        "2hr": "1h",
        "1h": "15m",
        "30m": "15m",
        "30min": "15m",
        "15m": "5m",
        "5m": "1m",
    }.get(interval, "")


def _flatten_query(values: dict[str, list[str]]) -> dict[str, Any]:
    return {key: item[-1] for key, item in values.items() if item}


def _decimal_param(value: Any, default: Decimal) -> Decimal:
    if value is None or value == "":
        return default
    try:
        return Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError(f"Invalid decimal value: {value}") from exc


def _optional_decimal_param(value: Any, *, name: str) -> Decimal | None:
    if value is None or value == "":
        return None
    parsed = _decimal_param(value, Decimal("0"))
    if parsed <= 0:
        raise ValueError(f"{name} must be positive when provided.")
    return parsed


def _nonnegative_decimal_param(value: Any, default: Decimal, *, name: str) -> Decimal:
    parsed = _decimal_param(value, default)
    if parsed < 0:
        raise ValueError(f"{name} cannot be negative.")
    return parsed


def _fee_rate_param(params: dict[str, Any], default: Decimal) -> Decimal:
    fee_type = str(params.get("fee_type") or "").strip().lower()
    if fee_type == "inr_maker":
        return COINDCX_INR_M_MAKER_FEE_RATE
    if fee_type == "inr_taker":
        return COINDCX_INR_M_TAKER_FEE_RATE
    if params.get("fee_pct") not in {None, ""}:
        return _fee_pct_to_rate(params.get("fee_pct"))
    return _normalize_fee_rate(_decimal_param(params.get("fee_rate"), default))


def _fee_config_params(params: dict[str, Any], defaults: DashboardDefaults) -> dict[str, object]:
    maker_fee_rate = _rate_from_pct_or_rate(
        pct=params.get("maker_fee_pct"),
        rate=params.get("maker_fee_rate"),
        default=defaults.maker_fee_rate,
    )
    taker_fee_rate = _rate_from_pct_or_rate(
        pct=params.get("taker_fee_pct"),
        rate=params.get("taker_fee_rate"),
        default=defaults.taker_fee_rate,
    )
    legacy_fee_rate = _legacy_fee_rate(params)
    if legacy_fee_rate is not None:
        maker_fee_rate = legacy_fee_rate
        taker_fee_rate = legacy_fee_rate

    fee_gst_rate = _gst_rate_param(params, defaults.fee_gst_rate)
    entry_fee_type = _fee_type_param(params.get("entry_fee_type"), defaults.entry_fee_type)
    exit_fee_type = _fee_type_param(params.get("exit_fee_type"), defaults.exit_fee_type)
    return {
        "maker_fee_rate": maker_fee_rate,
        "taker_fee_rate": taker_fee_rate,
        "fee_gst_rate": fee_gst_rate,
        "entry_fee_type": entry_fee_type,
        "exit_fee_type": exit_fee_type,
    }


def _rate_from_pct_or_rate(
    *,
    pct: Any,
    rate: Any,
    default: Decimal,
    allow_negative: bool = False,
) -> Decimal:
    if pct not in {None, ""}:
        return _pct_to_rate(pct, allow_negative=allow_negative)
    return _normalize_rate(_decimal_param(rate, default), allow_negative=allow_negative)


def _legacy_fee_rate(params: dict[str, Any]) -> Decimal | None:
    if params.get("fee_type") in {"inr_maker", "inr_taker"}:
        return _fee_rate_param(params, Decimal("0"))
    if params.get("fee_pct") not in {None, ""}:
        return _fee_pct_to_rate(params.get("fee_pct"))
    if params.get("fee_rate") not in {None, ""}:
        return _normalize_fee_rate(_decimal_param(params.get("fee_rate"), Decimal("0")))
    return None


def _fee_type_param(value: Any, default: str) -> str:
    if value is None or value == "":
        value = default
    normalized = str(value).strip().lower()
    if normalized not in {"maker", "taker"}:
        raise ValueError("fee type must be maker or taker.")
    return normalized


def _gst_rate_param(params: dict[str, Any], default: Decimal) -> Decimal:
    if params.get("fee_gst_pct") not in {None, ""}:
        return _fee_pct_to_rate(params.get("fee_gst_pct"))
    value = _decimal_param(params.get("fee_gst_rate"), default)
    if value < 0:
        raise ValueError("fee_gst_rate cannot be negative.")
    return value


def _atr_take_profit_mode_param(value: Any, default: str) -> str:
    if value is None or value == "":
        value = default
    normalized = str(value).strip().lower()
    if normalized not in {"fixed", "entry_atr", "ratchet", "trailing_atr", "none"}:
        raise ValueError(
            "atr_take_profit_mode must be fixed, entry_atr, ratchet, trailing_atr, or none."
        )
    return normalized


def _atr_policy_mode_param(value: Any, default: str) -> str:
    if value is None or value == "":
        value = default
    normalized = str(value).strip().lower()
    if normalized not in {"manual", "router"}:
        raise ValueError("atr_policy_mode must be manual or router.")
    return normalized


def _fee_pct_to_rate(value: Any) -> Decimal:
    return _pct_to_rate(value, allow_negative=False)


def _pct_to_rate(value: Any, *, allow_negative: bool) -> Decimal:
    fee_pct = _decimal_param(value, Decimal("0"))
    if fee_pct < 0 and not allow_negative:
        raise ValueError("fee_pct cannot be negative.")
    return fee_pct / Decimal("100")


def _normalize_fee_rate(value: Decimal) -> Decimal:
    return _normalize_rate(value, allow_negative=False)


def _normalize_rate(value: Decimal, *, allow_negative: bool) -> Decimal:
    if value < 0:
        if allow_negative:
            return value
        raise ValueError("fee_rate cannot be negative.")
    if value >= Decimal("0.01"):
        return value / Decimal("100")
    return value


def _int_param(value: Any, default: int) -> int:
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid integer value: {value}") from exc


def _positive_int_param(value: Any, default: int, *, name: str) -> int:
    parsed = _int_param(value, default)
    if parsed <= 0:
        raise ValueError(f"{name} must be positive.")
    return parsed


def _nonnegative_int_param(value: Any, default: int, *, name: str) -> int:
    parsed = _int_param(value, default)
    if parsed < 0:
        raise ValueError(f"{name} cannot be negative.")
    return parsed


def _bool_param(value: Any, default: bool) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    raise ValueError(f"Invalid boolean value: {value}")
