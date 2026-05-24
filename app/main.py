from __future__ import annotations

import argparse
import json
import logging
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from app.backtest.data_loader import (
    REST_RESOLUTION_BY_INTERVAL,
    load_historical_candle_series,
    load_historical_candle_series_between,
)
from app.backtest.engine import BacktestEngine
from app.backtest.models import BacktestConfig
from app.broker.paper import PaperBroker
from app.config import load_settings
from app.dashboard import run_dashboard
from app.dashboard.state import DashboardDefaults
from app.data.candle_builder import interval_to_ms, rest_rows_to_series
from app.data.indicators import latest_indicator_snapshot
from app.data.market_events import event_to_dict
from app.data.open_interest import (
    OpenInterestFeatureSeries,
    load_binance_open_interest_proxy,
)
from app.data.pipeline import MarketDataPipeline
from app.data.replay import ReplayMarketDataSource
from app.execution.engine import PaperExecutionEngine
from app.exchange.coindcx_rest import CoinDCXFuturesClient
from app.exchange.coindcx_ws import (
    CoinDCXFuturesWebSocketClient,
    WebSocketDependencyError,
    default_market_subscriptions,
)
from app.exchange.errors import CoinDCXAuthError, CoinDCXError
from app.exchange.signing import sign_payload
from app.fees import (
    COINDCX_FEE_GST_RATE,
    COINDCX_INR_M_MAKER_FEE_RATE,
    COINDCX_INR_M_TAKER_FEE_RATE,
)
from app.risk.manager import RiskManager
from app.risk.models import InstrumentMetadata
from app.research.history import record_backtest_result
from app.research.quick_validator import QuickValidationConfig, run_quick_validator
from app.research.sweep import (
    DEFAULT_INTERVALS,
    DEFAULT_SWEEP_PAIRS,
    SweepConfig,
    parse_csv_list,
    parse_bool_grid,
    parse_decimal_list,
    parse_optional_decimal_list,
    parse_trailing_profiles,
    parse_until,
    run_research_sweep,
    strategy_variants_for_names,
)
from app.strategies.base import (
    SignalAction,
    SignalDirection,
    StrategyContext,
    StrategySignal,
)
from app.strategies.defaults import (
    STRATEGY_CHOICES,
    default_strategy_engine,
    strategy_engine_for_name,
)
from app.utils.logging import configure_logging
from app.utils.time import utc_timestamp_ms


ATR_TAKE_PROFIT_MODES = ("fixed", "entry_atr", "ratchet", "trailing_atr", "none")
OPEN_INTEREST_STRATEGIES = {"hybrid_meta", "hybrid_meta_v2", "adaptive_hybrid"}


def _print_json(value: object) -> None:
    print(json.dumps(value, indent=2, sort_keys=True, default=str))


def _uses_open_interest(strategy_name: str) -> bool:
    normalized = strategy_name.strip().lower()
    return normalized in OPEN_INTEREST_STRATEGIES


def _load_open_interest_proxy_safe(
    *,
    pair: str,
    interval: str,
    series,
    strategy_name: str,
) -> OpenInterestFeatureSeries:
    if not _uses_open_interest(strategy_name):
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


def _decimal_arg(value: str) -> Decimal:
    try:
        return Decimal(value)
    except InvalidOperation as exc:
        raise argparse.ArgumentTypeError(f"Invalid decimal value: {value}") from exc


def _fee_rate_from_args(
    *,
    fee_rate: Decimal | None,
    fee_pct: Decimal | None,
    default: Decimal,
    allow_negative: bool = False,
) -> Decimal:
    if fee_pct is not None:
        if fee_pct < 0 and not allow_negative:
            raise SystemExit("Fee percentage cannot be negative.")
        return fee_pct / Decimal("100")
    if fee_rate is None:
        return default
    if fee_rate < 0 and not allow_negative:
        raise SystemExit("Fee rate cannot be negative.")
    if abs(fee_rate) >= Decimal("0.01"):
        return fee_rate / Decimal("100")
    return fee_rate


def _fee_rates_from_args(
    *,
    maker_fee_rate: Decimal | None,
    maker_fee_pct: Decimal | None,
    taker_fee_rate: Decimal | None,
    taker_fee_pct: Decimal | None,
    fee_rate: Decimal | None,
    fee_pct: Decimal | None,
    default_maker_fee_rate: Decimal = COINDCX_INR_M_MAKER_FEE_RATE,
    default_taker_fee_rate: Decimal = COINDCX_INR_M_TAKER_FEE_RATE,
) -> tuple[Decimal, Decimal]:
    legacy_fee = _fee_rate_from_args(
        fee_rate=fee_rate,
        fee_pct=fee_pct,
        default=Decimal("-1"),
    )
    if legacy_fee >= 0:
        return legacy_fee, legacy_fee
    return (
        _fee_rate_from_args(
            fee_rate=maker_fee_rate,
            fee_pct=maker_fee_pct,
            default=default_maker_fee_rate,
        ),
        _fee_rate_from_args(
            fee_rate=taker_fee_rate,
            fee_pct=taker_fee_pct,
            default=default_taker_fee_rate,
        ),
    )


def _fee_gst_rate_from_args(
    *,
    fee_gst_rate: Decimal | None,
    fee_gst_pct: Decimal | None,
    default: Decimal = COINDCX_FEE_GST_RATE,
) -> Decimal:
    if fee_gst_pct is not None:
        if fee_gst_pct < 0:
            raise SystemExit("Fee GST percentage cannot be negative.")
        return fee_gst_pct / Decimal("100")
    if fee_gst_rate is None:
        return default
    if fee_gst_rate < 0:
        raise SystemExit("Fee GST rate cannot be negative.")
    return fee_gst_rate


def _funding_fee_rate_from_args(
    *,
    funding_fee_rate: Decimal | None,
    funding_fee_pct: Decimal | None,
) -> Decimal:
    return _fee_rate_from_args(
        fee_rate=funding_fee_rate,
        fee_pct=funding_fee_pct,
        default=Decimal("0"),
        allow_negative=True,
    )


def _positive_optional_pct(value: Decimal | None, name: str) -> Decimal | None:
    if value is not None and value <= 0:
        raise SystemExit(f"{name} must be positive when provided.")
    return value


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


def config_check() -> None:
    settings = load_settings()
    configure_logging(settings)
    _print_json(settings.safe_dict())


def public_smoke(pair: str) -> None:
    settings = load_settings()
    configure_logging(settings)
    client = CoinDCXFuturesClient(settings)

    instruments = client.get_active_instruments(settings.futures_margin_currency)
    instrument = client.get_instrument(pair, settings.futures_margin_currency)

    now = datetime.now(timezone.utc)
    candles = client.get_candles(
        pair=pair,
        from_ts=int((now - timedelta(hours=3)).timestamp()),
        to_ts=int(now.timestamp()),
        resolution="60",
    )

    _print_json(
        {
            "active_instrument_count": len(instruments),
            "first_active_instruments": instruments[:5],
            "instrument": instrument,
            "candles_sample": candles.get("data", [])[:3]
            if isinstance(candles, dict)
            else candles,
        }
    )


def auth_smoke() -> None:
    settings = load_settings()
    configure_logging(settings)
    settings.require_private_credentials()
    client = CoinDCXFuturesClient(settings)

    user_info = client.get_user_info()
    _print_json(user_info)


def auth_diagnose() -> None:
    settings = load_settings()
    configure_logging(settings)
    settings.require_private_credentials()

    timestamp = utc_timestamp_ms()
    body = {"timestamp": timestamp}
    signature = sign_payload(settings.coindcx_api_secret, body)

    report: dict[str, object] = {
        "local_utc_time": datetime.now(timezone.utc).isoformat(),
        "timestamp_ms": timestamp,
        "api_key_loaded": bool(settings.coindcx_api_key),
        "api_secret_loaded": bool(settings.coindcx_api_secret),
        "signature_generated": bool(signature),
        "endpoint_tested": "/exchange/v1/users/info",
    }

    client = CoinDCXFuturesClient(settings)
    try:
        user_info = client.get_user_info()
    except CoinDCXAuthError as exc:
        report["auth_status"] = "failed"
        report["status_code"] = exc.status_code
        report["message"] = str(exc)
        report["likely_next_steps"] = [
            "Confirm the .env key prefix/suffix match the CoinDCX API key exactly.",
            "Confirm API key and API secret are not swapped.",
            "If IP whitelist is enabled, add your current public IP or temporarily disable whitelist for local testing.",
            "Regenerate a fresh API key/secret if the secret was copied once and cannot be viewed again.",
            "Confirm API access is enabled on the CoinDCX account.",
        ]
    except CoinDCXError as exc:
        report["auth_status"] = "error"
        report["message"] = str(exc)
    else:
        report["auth_status"] = "passed"
        report["user_info_shape"] = type(user_info).__name__

    _print_json(report)


def private_read_smoke() -> None:
    settings = load_settings()
    configure_logging(settings)
    settings.require_private_credentials()
    client = CoinDCXFuturesClient(settings)

    wallets = client.get_wallets()
    positions = client.list_positions(
        page=1,
        size=10,
        margin_currencies=[settings.futures_margin_currency],
    )
    _print_json({"wallets": wallets, "positions": positions})


def ws_replay_smoke(pair: str) -> None:
    settings = load_settings()
    configure_logging(settings)
    emitted: list[dict[str, object]] = []
    pipeline = MarketDataPipeline(
        default_pair=pair,
        on_event=lambda event: emitted.append(event_to_dict(event)),
    )
    replay = ReplayMarketDataSource(pair=pair)
    count = replay.run(pipeline)
    _print_json(
        {
            "events_replayed": count,
            "events": emitted,
            "store_summary": pipeline.store.summary(),
        }
    )


def market_stream(
    *,
    pair: str,
    interval: str,
    depth: int,
    max_events: int,
    include_current_prices: bool,
) -> None:
    settings = load_settings()
    configure_logging(settings)
    pipeline = MarketDataPipeline(
        default_pair=pair,
        on_event=lambda event: print(
            json.dumps(event_to_dict(event), sort_keys=True),
            flush=True,
        ),
    )
    subscriptions = default_market_subscriptions(
        pair=pair,
        candle_interval=interval,
        orderbook_depth=depth,
        include_current_prices=include_current_prices,
    )
    print(
        json.dumps(
            {
                "ws_url": settings.coindcx_ws_url,
                "subscriptions": [
                    {
                        "channel_name": item.channel_name,
                        "event_name": item.event_name,
                    }
                    for item in subscriptions
                ],
                "max_events": max_events,
            },
            indent=2,
        )
    )
    try:
        client = CoinDCXFuturesWebSocketClient(settings, pipeline=pipeline)
        client.run(subscriptions, max_events=max_events)
    except WebSocketDependencyError as exc:
        raise SystemExit(str(exc)) from exc


def indicator_smoke(*, pair: str, interval: str, lookback: int) -> None:
    settings = load_settings()
    configure_logging(settings)
    if interval not in REST_RESOLUTION_BY_INTERVAL:
        raise SystemExit(
            f"Unsupported REST smoke interval {interval}. "
            f"Choose one of: {sorted(REST_RESOLUTION_BY_INTERVAL)}"
        )

    client = CoinDCXFuturesClient(settings)
    now = datetime.now(timezone.utc)
    lookback_seconds = int((interval_to_ms(interval) * lookback) / 1000)
    candles_response = client.get_candles(
        pair=pair,
        from_ts=int((now - timedelta(seconds=lookback_seconds)).timestamp()),
        to_ts=int(now.timestamp()),
        resolution=REST_RESOLUTION_BY_INTERVAL[interval],
    )

    rows = (
        candles_response.get("data", [])
        if isinstance(candles_response, dict)
        else candles_response
    )
    series = rest_rows_to_series(pair=pair, interval=interval, rows=rows, maxlen=lookback)
    snapshot = latest_indicator_snapshot(series)
    latest = series.latest()
    _print_json(
        {
            "pair": pair,
            "interval": interval,
            "candles_loaded": len(series),
            "latest_candle": latest,
            "indicators": snapshot.to_dict(),
        }
    )


def strategy_smoke(*, pair: str, interval: str, lookback: int) -> None:
    settings = load_settings()
    configure_logging(settings)
    if interval not in REST_RESOLUTION_BY_INTERVAL:
        raise SystemExit(
            f"Unsupported REST smoke interval {interval}. "
            f"Choose one of: {sorted(REST_RESOLUTION_BY_INTERVAL)}"
        )

    client = CoinDCXFuturesClient(settings)
    now = datetime.now(timezone.utc)
    lookback_seconds = int((interval_to_ms(interval) * lookback) / 1000)
    candles_response = client.get_candles(
        pair=pair,
        from_ts=int((now - timedelta(seconds=lookback_seconds)).timestamp()),
        to_ts=int(now.timestamp()),
        resolution=REST_RESOLUTION_BY_INTERVAL[interval],
    )
    rows = (
        candles_response.get("data", [])
        if isinstance(candles_response, dict)
        else candles_response
    )
    series = rest_rows_to_series(pair=pair, interval=interval, rows=rows, maxlen=lookback)
    indicators = latest_indicator_snapshot(series)
    context = StrategyContext(
        pair=pair,
        interval=interval,
        candles=series,
        indicators=indicators,
    )
    signals = default_strategy_engine().evaluate(context)
    _print_json(
        {
            "pair": pair,
            "interval": interval,
            "candles_loaded": len(series),
            "signals": [signal.to_dict() for signal in signals],
        }
    )


def risk_smoke(
    *,
    pair: str,
    interval: str,
    lookback: int,
    account_equity: Decimal,
    daily_realized_pnl: Decimal,
    open_positions: int,
    leverage: Decimal,
) -> None:
    settings = load_settings()
    configure_logging(settings)
    if interval not in REST_RESOLUTION_BY_INTERVAL:
        raise SystemExit(
            f"Unsupported REST smoke interval {interval}. "
            f"Choose one of: {sorted(REST_RESOLUTION_BY_INTERVAL)}"
        )

    client = CoinDCXFuturesClient(settings)
    now = datetime.now(timezone.utc)
    lookback_seconds = int((interval_to_ms(interval) * lookback) / 1000)
    candles_response = client.get_candles(
        pair=pair,
        from_ts=int((now - timedelta(seconds=lookback_seconds)).timestamp()),
        to_ts=int(now.timestamp()),
        resolution=REST_RESOLUTION_BY_INTERVAL[interval],
    )
    rows = (
        candles_response.get("data", [])
        if isinstance(candles_response, dict)
        else candles_response
    )
    series = rest_rows_to_series(pair=pair, interval=interval, rows=rows, maxlen=lookback)
    indicators = latest_indicator_snapshot(series)
    context = StrategyContext(
        pair=pair,
        interval=interval,
        candles=series,
        indicators=indicators,
    )
    signals = default_strategy_engine().evaluate(context)
    instrument = _instrument_metadata(
        client,
        pair=pair,
        margin_currency=settings.futures_margin_currency,
    )

    manager = RiskManager(settings.risk)
    decisions = [
        manager.evaluate_signal(
            signal,
            account_equity=account_equity,
            daily_realized_pnl=daily_realized_pnl,
            open_positions=open_positions,
            instrument=instrument,
            requested_leverage=leverage,
            trading_mode=settings.trading_mode,
            live_trading_enabled=settings.live_trading_enabled,
        )
        for signal in signals
    ]

    _print_json(
        {
            "pair": pair,
            "interval": interval,
            "candles_loaded": len(series),
            "account_equity": account_equity,
            "daily_realized_pnl": daily_realized_pnl,
            "open_positions": open_positions,
            "requested_leverage": leverage,
            "live_trading_allowed": settings.live_trading_allowed,
            "decisions": [decision.to_dict() for decision in decisions],
        }
    )


def _demo_entry_signal(
    *,
    pair: str,
    interval: str,
    price: Decimal,
    timestamp_ms: int,
    stop_pct: Decimal,
    take_profit_pct: Decimal,
) -> StrategySignal:
    if stop_pct <= 0:
        raise SystemExit("--demo-stop-pct must be positive.")
    if take_profit_pct <= 0:
        raise SystemExit("--demo-take-profit-pct must be positive.")
    return StrategySignal(
        strategy_name="paper_demo",
        pair=pair,
        interval=interval,
        action=SignalAction.ENTER_LONG,
        direction=SignalDirection.LONG,
        confidence=Decimal("1"),
        reason="Synthetic paper execution demo entry.",
        timestamp_ms=timestamp_ms,
        entry_price=price,
        stop_loss=price * (Decimal("1") - (stop_pct / Decimal("100"))),
        take_profit=price * (Decimal("1") + (take_profit_pct / Decimal("100"))),
        metadata={"demo": True},
    )


def paper_execution_smoke(
    *,
    pair: str,
    interval: str,
    lookback: int,
    account_equity: Decimal,
    leverage: Decimal,
    maker_fee_rate: Decimal,
    taker_fee_rate: Decimal,
    fee_gst_rate: Decimal,
    entry_fee_type: str,
    exit_fee_type: str,
    slippage_pct: Decimal,
    stop_slippage_pct: Decimal | None,
    demo_entry: bool,
    demo_stop_pct: Decimal,
    demo_take_profit_pct: Decimal,
) -> None:
    settings = load_settings()
    configure_logging(settings)
    if interval not in REST_RESOLUTION_BY_INTERVAL:
        raise SystemExit(
            f"Unsupported REST smoke interval {interval}. "
            f"Choose one of: {sorted(REST_RESOLUTION_BY_INTERVAL)}"
        )

    client = CoinDCXFuturesClient(settings)
    now = datetime.now(timezone.utc)
    lookback_seconds = int((interval_to_ms(interval) * lookback) / 1000)
    candles_response = client.get_candles(
        pair=pair,
        from_ts=int((now - timedelta(seconds=lookback_seconds)).timestamp()),
        to_ts=int(now.timestamp()),
        resolution=REST_RESOLUTION_BY_INTERVAL[interval],
    )
    rows = (
        candles_response.get("data", [])
        if isinstance(candles_response, dict)
        else candles_response
    )
    series = rest_rows_to_series(pair=pair, interval=interval, rows=rows, maxlen=lookback)
    latest = series.latest()
    if latest is None:
        raise SystemExit("No candles loaded; cannot run paper execution smoke.")

    indicators = latest_indicator_snapshot(series)
    context = StrategyContext(
        pair=pair,
        interval=interval,
        candles=series,
        indicators=indicators,
    )
    signals = default_strategy_engine().evaluate(context)
    if demo_entry:
        signals.append(
            _demo_entry_signal(
                pair=pair,
                interval=interval,
                price=latest.close,
                timestamp_ms=latest.close_time_ms,
                stop_pct=demo_stop_pct,
                take_profit_pct=demo_take_profit_pct,
            )
        )

    instrument = _instrument_metadata(
        client,
        pair=pair,
        margin_currency=settings.futures_margin_currency,
    )
    risk_manager = RiskManager(settings.risk)
    broker = PaperBroker(
        starting_equity=account_equity,
        maker_fee_rate=maker_fee_rate,
        taker_fee_rate=taker_fee_rate,
        fee_gst_rate=fee_gst_rate,
        entry_fee_type=entry_fee_type,
        exit_fee_type=exit_fee_type,
        slippage_pct=slippage_pct,
        stop_slippage_pct=stop_slippage_pct,
        quote_to_margin_rate=settings.quote_to_margin_rate,
        account_currency=settings.futures_margin_currency,
        price_quote_currency=settings.price_quote_currency,
    )
    engine = PaperExecutionEngine(broker)

    results = []
    for signal in signals:
        snapshot = broker.snapshot({pair: latest.close})
        decision = risk_manager.evaluate_signal(
            signal,
            account_equity=snapshot.equity,
            daily_realized_pnl=broker.realized_pnl,
            open_positions=snapshot.open_position_count,
            instrument=instrument,
            requested_leverage=leverage,
            quote_to_margin_rate=settings.quote_to_margin_rate,
            unit_contract_value=Decimal("1"),
            trading_mode=settings.trading_mode,
            live_trading_enabled=settings.live_trading_enabled,
        )
        report = engine.process_decision(
            decision,
            market_price=latest.close,
            timestamp_ms=latest.close_time_ms,
        )
        results.append(
            {
                "signal": signal.to_dict(),
                "risk_decision": decision.to_dict(),
                "execution_report": report.to_dict(),
            }
        )

    final_snapshot = broker.snapshot({pair: latest.close})
    _print_json(
        {
            "pair": pair,
            "interval": interval,
            "candles_loaded": len(series),
            "latest_close": latest.close,
            "demo_entry": demo_entry,
            "live_trading_allowed": settings.live_trading_allowed,
            "results": results,
            "final_account": final_snapshot.to_dict(),
            "open_positions": [
                position.to_dict() for position in broker.open_positions()
            ],
            "orders": [order.to_dict() for order in broker.orders],
            "fills": [fill.to_dict() for fill in broker.fills],
        }
    )


def backtest_command(
    *,
    pair: str,
    interval: str,
    lookback: int,
    account_equity: Decimal,
    leverage: Decimal,
    risk_per_trade_pct: Decimal | None,
    compound_risk_equity: bool,
    stop_loss_pct: Decimal | None,
    take_profit_pct: Decimal | None,
    maker_fee_rate: Decimal,
    taker_fee_rate: Decimal,
    fee_gst_rate: Decimal,
    entry_fee_type: str,
    exit_fee_type: str,
    slippage_pct: Decimal,
    stop_slippage_pct: Decimal | None,
    funding_fee_rate: Decimal,
    funding_interval_hours: int,
    trailing_stop_enabled: bool | None,
    trailing_stop_activation_pct: Decimal | None,
    trailing_stop_distance_pct: Decimal | None,
    atr_dynamic_exits_enabled: bool,
    atr_period: int,
    atr_stop_multiple: Decimal,
    atr_take_profit_multiple: Decimal,
    atr_take_profit_mode: str,
    execution_interval: str | None,
    intrabar_reentry_enabled: bool,
    max_reentries_per_candle: int,
    reentry_cooldown_candles: int,
    stop_loss_cooldown_candles: int,
    max_consecutive_losses: int,
    loss_cooldown_candles: int,
    strategy_name: str,
    recent_count: int,
    equity_giveback_guard_enabled: bool,
    equity_giveback_threshold_pct: Decimal,
    equity_giveback_cooldown_candles: int,
    loss_streak_cooldown_enabled: bool,
    consecutive_loss_limit: int,
    loss_streak_cooldown_candles: int,
    rolling_loss_window: int,
    rolling_loss_limit: int,
    rolling_loss_cooldown_candles: int,
    post_spike_cooldown_enabled: bool,
    post_spike_lookback_candles: int,
    post_spike_gain_threshold_pct: Decimal,
    post_spike_cooldown_candles: int,
    breakeven_enabled: bool,
    breakeven_activation_r: Decimal,
    breakeven_offset_r: Decimal,
    profit_lock_enabled: bool,
    profit_lock_activation_r: Decimal,
    profit_lock_r: Decimal,
    atr_trail_after_r_enabled: bool,
    atr_trail_activation_r: Decimal,
    chop_filter_enabled: bool,
    min_ema_gap_pct: Decimal,
    min_atr_pct: Decimal,
    block_flat_ema_enabled: bool,
    block_low_atr_enabled: bool,
    atr_trailing_multiple: Decimal,
) -> None:
    settings = load_settings()
    configure_logging(settings)
    logging.getLogger("app.risk.manager").setLevel(logging.WARNING)
    logging.getLogger("app.broker.paper").setLevel(logging.WARNING)
    if interval not in REST_RESOLUTION_BY_INTERVAL:
        raise SystemExit(
            f"Unsupported REST backtest interval {interval}. "
            f"Choose one of: {sorted(REST_RESOLUTION_BY_INTERVAL)}"
        )

    try:
        strategy_engine = strategy_engine_for_name(strategy_name)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc

    client = CoinDCXFuturesClient(settings)
    series = load_historical_candle_series(
        client=client,
        pair=pair,
        interval=interval,
        lookback=lookback,
    )
    if len(series) == 0:
        raise SystemExit("No candles loaded; cannot run backtest.")
    open_interest_features = _load_open_interest_proxy_safe(
        pair=pair,
        interval=interval,
        series=series,
        strategy_name=strategy_name,
    )
    execution_series = None
    if execution_interval:
        if execution_interval not in REST_RESOLUTION_BY_INTERVAL:
            raise SystemExit(
                f"Unsupported REST execution interval {execution_interval}. "
                f"Choose one of: {sorted(REST_RESOLUTION_BY_INTERVAL)}"
            )
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
            raise SystemExit("No execution candles loaded; cannot run intrabar backtest.")

    stop_loss_pct = _positive_optional_pct(stop_loss_pct, "--stop-loss-pct")
    take_profit_pct = _positive_optional_pct(take_profit_pct, "--take-profit-pct")

    instrument = _instrument_metadata(
        client,
        pair=pair,
        margin_currency=settings.futures_margin_currency,
    )
    config = BacktestConfig(
        pair=pair,
        interval=interval,
        starting_equity=account_equity,
        leverage=leverage,
        strategy_name=strategy_name,
        margin_currency=settings.futures_margin_currency,
        price_quote_currency=settings.price_quote_currency,
        quote_to_margin_rate=settings.quote_to_margin_rate,
        risk_per_trade_pct=(
            settings.risk.max_risk_per_trade_pct
            if risk_per_trade_pct is None
            else risk_per_trade_pct
        ),
        compound_risk_equity=compound_risk_equity,
        stop_loss_pct=stop_loss_pct,
        take_profit_pct=take_profit_pct,
        maker_fee_rate=maker_fee_rate,
        taker_fee_rate=taker_fee_rate,
        fee_gst_rate=fee_gst_rate,
        entry_fee_type=entry_fee_type,
        exit_fee_type=exit_fee_type,
        slippage_pct=slippage_pct,
        stop_slippage_pct=stop_slippage_pct,
        funding_fee_rate=funding_fee_rate,
        funding_interval_hours=funding_interval_hours,
        trailing_stop_enabled=(
            settings.risk.trailing_stop_enabled
            if trailing_stop_enabled is None
            else trailing_stop_enabled
        ),
        trailing_stop_activation_pct=(
            settings.risk.trailing_stop_activation_pct
            if trailing_stop_activation_pct is None
            else trailing_stop_activation_pct
        ),
        trailing_stop_distance_pct=(
            settings.risk.trailing_stop_distance_pct
            if trailing_stop_distance_pct is None
            else trailing_stop_distance_pct
        ),
        atr_dynamic_exits_enabled=atr_dynamic_exits_enabled,
        atr_take_profit_enabled=atr_take_profit_mode != "none",
        atr_period=atr_period,
        atr_stop_multiple=atr_stop_multiple,
        atr_take_profit_multiple=atr_take_profit_multiple,
        atr_take_profit_mode=atr_take_profit_mode,
        execution_interval=execution_interval,
        paper_intrabar_enabled=execution_interval is not None,
        strategy_interval=interval,
        intrabar_reentry_enabled=intrabar_reentry_enabled,
        max_reentries_per_candle=max_reentries_per_candle,
        reentry_cooldown_candles=reentry_cooldown_candles,
        stop_loss_cooldown_candles=stop_loss_cooldown_candles,
        max_consecutive_losses=max_consecutive_losses,
        loss_cooldown_candles=loss_cooldown_candles,
        equity_giveback_guard_enabled=equity_giveback_guard_enabled,
        equity_giveback_threshold_pct=equity_giveback_threshold_pct / Decimal("100"),
        equity_giveback_cooldown_candles=equity_giveback_cooldown_candles,
        loss_streak_cooldown_enabled=loss_streak_cooldown_enabled,
        consecutive_loss_limit=consecutive_loss_limit,
        loss_streak_cooldown_candles=loss_streak_cooldown_candles,
        rolling_loss_window=rolling_loss_window,
        rolling_loss_limit=rolling_loss_limit,
        rolling_loss_cooldown_candles=rolling_loss_cooldown_candles,
        post_spike_cooldown_enabled=post_spike_cooldown_enabled,
        post_spike_lookback_candles=post_spike_lookback_candles,
        post_spike_gain_threshold_pct=post_spike_gain_threshold_pct / Decimal("100"),
        post_spike_cooldown_candles=post_spike_cooldown_candles,
        breakeven_enabled=breakeven_enabled,
        breakeven_activation_r=breakeven_activation_r,
        breakeven_offset_r=breakeven_offset_r,
        profit_lock_enabled=profit_lock_enabled,
        profit_lock_activation_r=profit_lock_activation_r,
        profit_lock_r=profit_lock_r,
        atr_trail_after_r_enabled=atr_trail_after_r_enabled,
        atr_trail_activation_r=atr_trail_activation_r,
        chop_filter_enabled=chop_filter_enabled,
        min_ema_gap_pct=min_ema_gap_pct / Decimal("100"),
        min_atr_pct=min_atr_pct / Decimal("100"),
        block_flat_ema_enabled=block_flat_ema_enabled,
        block_low_atr_enabled=block_low_atr_enabled,
        atr_trailing_multiple=atr_trailing_multiple,
    )
    engine = BacktestEngine(
        config=config,
        strategy_engine=strategy_engine,
        risk_manager=RiskManager(
            replace(
                settings.risk,
                max_risk_per_trade_pct=config.risk_per_trade_pct
                or settings.risk.max_risk_per_trade_pct,
            )
        ),
        instrument=instrument,
        open_interest_features=open_interest_features,
    )
    result = engine.run(series, execution_candles=execution_series)
    payload = result.to_dict(recent_count=recent_count)
    payload["history"] = record_backtest_result(result, source="cli")
    _print_json(payload)


def research_sweep_command(
    *,
    pairs: str,
    intervals: str,
    variant_names: str | None,
    lookback: int,
    account_equity: Decimal,
    leverage: Decimal,
    risk_per_trade_pct: Decimal | None,
    risk_grid: str | None,
    leverage_grid: str | None,
    compound_risk_equity: bool,
    stop_loss_pct: Decimal | None,
    take_profit_pct: Decimal | None,
    take_profit_grid: str | None,
    maker_fee_rate: Decimal,
    taker_fee_rate: Decimal,
    fee_gst_rate: Decimal,
    entry_fee_type: str,
    exit_fee_type: str,
    slippage_pct: Decimal,
    stop_slippage_pct: Decimal | None,
    funding_fee_rate: Decimal,
    funding_interval_hours: int,
    trailing_stop_enabled: bool | None,
    trailing_stop_activation_pct: Decimal | None,
    trailing_stop_distance_pct: Decimal | None,
    trailing_grid: str | None,
    atr_dynamic_exits_enabled: bool,
    atr_grid: str | None,
    atr_period: int,
    atr_stop_multiple: Decimal,
    atr_take_profit_multiple: Decimal,
    atr_take_profit_mode: str,
    equity_giveback_guard_enabled: bool,
    equity_giveback_threshold_pct: Decimal,
    equity_giveback_cooldown_candles: int,
    loss_streak_cooldown_enabled: bool,
    consecutive_loss_limit: int,
    loss_streak_cooldown_candles: int,
    rolling_loss_window: int,
    rolling_loss_limit: int,
    rolling_loss_cooldown_candles: int,
    post_spike_cooldown_enabled: bool,
    post_spike_lookback_candles: int,
    post_spike_gain_threshold_pct: Decimal,
    post_spike_cooldown_candles: int,
    breakeven_enabled: bool,
    breakeven_activation_r: Decimal,
    breakeven_offset_r: Decimal,
    profit_lock_enabled: bool,
    profit_lock_activation_r: Decimal,
    profit_lock_r: Decimal,
    atr_trail_after_r_enabled: bool,
    atr_trail_activation_r: Decimal,
    chop_filter_enabled: bool,
    min_ema_gap_pct: Decimal,
    min_atr_pct: Decimal,
    block_flat_ema_enabled: bool,
    block_low_atr_enabled: bool,
    atr_trailing_multiple: Decimal,
    until: str | None,
    output_dir: str,
    max_runs: int | None,
) -> None:
    settings = load_settings()
    configure_logging(settings)
    logging.getLogger("app.risk.manager").setLevel(logging.WARNING)
    logging.getLogger("app.broker.paper").setLevel(logging.WARNING)
    stop_loss_pct = _positive_optional_pct(stop_loss_pct, "--stop-loss-pct")
    take_profit_pct = _positive_optional_pct(take_profit_pct, "--take-profit-pct")
    take_profit_pct_values = tuple(
        _positive_optional_pct(value, "--take-profit-grid")
        for value in parse_optional_decimal_list(take_profit_grid, ())
    )
    risk_per_trade_pct_values = parse_decimal_list(risk_grid, ())
    leverage_values = parse_decimal_list(leverage_grid, ())
    for value in risk_per_trade_pct_values:
        _positive_optional_pct(value, "--risk-grid")
    for value in leverage_values:
        _positive_optional_pct(value, "--leverage-grid")
    variants = (
        strategy_variants_for_names(parse_csv_list(variant_names, ()))
        if variant_names
        else None
    )

    result = run_research_sweep(
        settings=settings,
        config=SweepConfig(
            pairs=parse_csv_list(pairs, DEFAULT_SWEEP_PAIRS),
            intervals=parse_csv_list(intervals, DEFAULT_INTERVALS),
            lookback=lookback,
            starting_equity=account_equity,
            leverage=leverage,
            margin_currency=settings.futures_margin_currency,
            price_quote_currency=settings.price_quote_currency,
            quote_to_margin_rate=settings.quote_to_margin_rate,
            risk_per_trade_pct=(
                settings.risk.max_risk_per_trade_pct
                if risk_per_trade_pct is None
                else risk_per_trade_pct
            ),
            risk_per_trade_pct_values=risk_per_trade_pct_values,
            leverage_values=leverage_values,
            compound_risk_equity=compound_risk_equity,
            stop_loss_pct=stop_loss_pct,
            take_profit_pct=take_profit_pct,
            take_profit_pct_values=take_profit_pct_values,
            maker_fee_rate=maker_fee_rate,
            taker_fee_rate=taker_fee_rate,
            fee_gst_rate=fee_gst_rate,
            entry_fee_type=entry_fee_type,
            exit_fee_type=exit_fee_type,
            slippage_pct=slippage_pct,
            stop_slippage_pct=stop_slippage_pct,
            funding_fee_rate=funding_fee_rate,
            funding_interval_hours=funding_interval_hours,
            trailing_stop_enabled=(
                settings.risk.trailing_stop_enabled
                if trailing_stop_enabled is None
                else trailing_stop_enabled
            ),
            trailing_stop_activation_pct=(
                settings.risk.trailing_stop_activation_pct
                if trailing_stop_activation_pct is None
                else trailing_stop_activation_pct
            ),
            trailing_stop_distance_pct=(
                settings.risk.trailing_stop_distance_pct
                if trailing_stop_distance_pct is None
                else trailing_stop_distance_pct
            ),
            trailing_profiles=parse_trailing_profiles(trailing_grid),
            atr_dynamic_exits_enabled=atr_dynamic_exits_enabled,
            atr_dynamic_exits_values=parse_bool_grid(atr_grid, ()),
            atr_take_profit_enabled=atr_take_profit_mode != "none",
            atr_period=atr_period,
            atr_stop_multiple=atr_stop_multiple,
            atr_take_profit_multiple=atr_take_profit_multiple,
            atr_take_profit_mode=atr_take_profit_mode,
            equity_giveback_guard_enabled=equity_giveback_guard_enabled,
            equity_giveback_threshold_pct=equity_giveback_threshold_pct,
            equity_giveback_cooldown_candles=equity_giveback_cooldown_candles,
            loss_streak_cooldown_enabled=loss_streak_cooldown_enabled,
            consecutive_loss_limit=consecutive_loss_limit,
            loss_streak_cooldown_candles=loss_streak_cooldown_candles,
            rolling_loss_window=rolling_loss_window,
            rolling_loss_limit=rolling_loss_limit,
            rolling_loss_cooldown_candles=rolling_loss_cooldown_candles,
            post_spike_cooldown_enabled=post_spike_cooldown_enabled,
            post_spike_lookback_candles=post_spike_lookback_candles,
            post_spike_gain_threshold_pct=post_spike_gain_threshold_pct,
            post_spike_cooldown_candles=post_spike_cooldown_candles,
            breakeven_enabled=breakeven_enabled,
            breakeven_activation_r=breakeven_activation_r,
            breakeven_offset_r=breakeven_offset_r,
            profit_lock_enabled=profit_lock_enabled,
            profit_lock_activation_r=profit_lock_activation_r,
            profit_lock_r=profit_lock_r,
            atr_trail_after_r_enabled=atr_trail_after_r_enabled,
            atr_trail_activation_r=atr_trail_activation_r,
            chop_filter_enabled=chop_filter_enabled,
            min_ema_gap_pct=min_ema_gap_pct,
            min_atr_pct=min_atr_pct,
            block_flat_ema_enabled=block_flat_ema_enabled,
            block_low_atr_enabled=block_low_atr_enabled,
            atr_trailing_multiple=atr_trailing_multiple,
            until=parse_until(until),
            output_dir=Path(output_dir),
            max_runs=max_runs,
        ),
        variants=variants,
    )
    _print_json(result)


def quick_validate_command(
    *,
    pairs: str,
    intervals: str,
    variant_names: str,
    quick_lookback: int,
    validation_lookback: int,
    final_lookback: int,
    quick_max_runs: int,
    top_validation: int,
    top_final: int,
    quick_only: bool,
    account_equity: Decimal,
    leverage: Decimal,
    risk_per_trade_pct: Decimal | None,
    risk_grid: str | None,
    leverage_grid: str | None,
    compound_risk_equity: bool,
    stop_loss_pct: Decimal | None,
    take_profit_pct: Decimal | None,
    take_profit_grid: str | None,
    maker_fee_rate: Decimal,
    taker_fee_rate: Decimal,
    fee_gst_rate: Decimal,
    entry_fee_type: str,
    exit_fee_type: str,
    slippage_pct: Decimal,
    stop_slippage_pct: Decimal | None,
    funding_fee_rate: Decimal,
    funding_interval_hours: int,
    trailing_grid: str,
    atr_grid: str,
    atr_period: int,
    atr_stop_multiple: Decimal,
    atr_take_profit_multiple: Decimal,
    atr_take_profit_mode: str,
    output_dir: str,
) -> None:
    settings = load_settings()
    configure_logging(settings)
    logging.getLogger("app.risk.manager").setLevel(logging.WARNING)
    logging.getLogger("app.broker.paper").setLevel(logging.WARNING)

    stop_loss_pct = _positive_optional_pct(stop_loss_pct, "--stop-loss-pct")
    take_profit_pct = _positive_optional_pct(take_profit_pct, "--take-profit-pct")
    risk_per_trade_pct_values = parse_decimal_list(
        risk_grid,
        (Decimal("1"), Decimal("2"), Decimal("3")),
    )
    leverage_values = parse_decimal_list(
        leverage_grid,
        (Decimal("1"), Decimal("3"), Decimal("5")),
    )
    take_profit_pct_values = tuple(
        _positive_optional_pct(value, "--take-profit-grid")
        for value in parse_optional_decimal_list(
            take_profit_grid,
            (None, Decimal("3"), Decimal("5")),
        )
    )
    for value in risk_per_trade_pct_values:
        _positive_optional_pct(value, "--risk-grid")
    for value in leverage_values:
        _positive_optional_pct(value, "--leverage-grid")

    variants = strategy_variants_for_names(parse_csv_list(variant_names, ()))
    trailing_profiles = parse_trailing_profiles(trailing_grid)
    atr_dynamic_exits_values = parse_bool_grid(atr_grid, (False, True))

    base_config = SweepConfig(
        pairs=parse_csv_list(pairs, DEFAULT_SWEEP_PAIRS),
        intervals=parse_csv_list(intervals, ("5m", "15m", "30m", "1h")),
        lookback=quick_lookback,
        starting_equity=account_equity,
        leverage=leverage,
        margin_currency=settings.futures_margin_currency,
        price_quote_currency=settings.price_quote_currency,
        quote_to_margin_rate=settings.quote_to_margin_rate,
        risk_per_trade_pct=(
            settings.risk.max_risk_per_trade_pct
            if risk_per_trade_pct is None
            else risk_per_trade_pct
        ),
        risk_per_trade_pct_values=risk_per_trade_pct_values,
        leverage_values=leverage_values,
        compound_risk_equity=compound_risk_equity,
        stop_loss_pct=stop_loss_pct,
        take_profit_pct=take_profit_pct,
        take_profit_pct_values=take_profit_pct_values,
        maker_fee_rate=maker_fee_rate,
        taker_fee_rate=taker_fee_rate,
        fee_gst_rate=fee_gst_rate,
        entry_fee_type=entry_fee_type,
        exit_fee_type=exit_fee_type,
        slippage_pct=slippage_pct,
        stop_slippage_pct=stop_slippage_pct,
        funding_fee_rate=funding_fee_rate,
        funding_interval_hours=funding_interval_hours,
        trailing_profiles=trailing_profiles,
        atr_dynamic_exits_values=atr_dynamic_exits_values,
        atr_take_profit_enabled=atr_take_profit_mode != "none",
        atr_period=atr_period,
        atr_stop_multiple=atr_stop_multiple,
        atr_take_profit_multiple=atr_take_profit_multiple,
        atr_take_profit_mode=atr_take_profit_mode,
        output_dir=Path(output_dir),
        max_runs=quick_max_runs,
    )
    result = run_quick_validator(
        settings=settings,
        config=QuickValidationConfig(
            base_sweep=base_config,
            output_dir=Path(output_dir),
            quick_lookback=quick_lookback,
            validation_lookback=validation_lookback,
            final_lookback=final_lookback,
            quick_max_runs=quick_max_runs,
            top_validation=top_validation,
            top_final=top_final,
            quick_only=quick_only,
        ),
        variants=variants,
    )
    _print_json(result)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="CoinDCX futures bot utilities")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("config-check", help="Print redacted local settings")
    subparsers.add_parser(
        "auth-smoke", help="Read basic CoinDCX user info with signed auth"
    )
    subparsers.add_parser(
        "auth-diagnose", help="Show safe auth diagnostics without printing secrets"
    )

    public_parser = subparsers.add_parser(
        "public-smoke", help="Read public futures market data"
    )
    public_parser.add_argument("--pair", default="B-BTC_USDT")

    subparsers.add_parser(
        "private-read-smoke", help="Read private futures wallets and positions"
    )

    replay_parser = subparsers.add_parser(
        "ws-replay-smoke", help="Replay sample CoinDCX websocket events offline"
    )
    replay_parser.add_argument("--pair", default="B-BTC_USDT")

    stream_parser = subparsers.add_parser(
        "market-stream", help="Stream live CoinDCX futures market data"
    )
    stream_parser.add_argument("--pair", default="B-BTC_USDT")
    stream_parser.add_argument("--interval", default="1m")
    stream_parser.add_argument("--depth", type=int, default=50)
    stream_parser.add_argument("--max-events", type=int, default=10)
    stream_parser.add_argument(
        "--include-current-prices",
        action="store_true",
        help="Also subscribe to the all-pairs futures current-prices channel",
    )

    indicator_parser = subparsers.add_parser(
        "indicator-smoke",
        help="Fetch recent public futures candles and compute indicator snapshot",
    )
    indicator_parser.add_argument("--pair", default="B-BTC_USDT")
    indicator_parser.add_argument("--interval", default="1h")
    indicator_parser.add_argument("--lookback", type=int, default=80)

    strategy_parser = subparsers.add_parser(
        "strategy-smoke",
        help="Fetch recent public futures candles and evaluate starter strategies",
    )
    strategy_parser.add_argument("--pair", default="B-BTC_USDT")
    strategy_parser.add_argument("--interval", default="1h")
    strategy_parser.add_argument("--lookback", type=int, default=120)

    risk_parser = subparsers.add_parser(
        "risk-smoke",
        help="Fetch strategy signals and evaluate paper risk decisions",
    )
    risk_parser.add_argument("--pair", default="B-BTC_USDT")
    risk_parser.add_argument("--interval", default="1h")
    risk_parser.add_argument("--lookback", type=int, default=120)
    risk_parser.add_argument("--equity", type=_decimal_arg, default=None)
    risk_parser.add_argument("--daily-pnl", type=_decimal_arg, default=Decimal("0"))
    risk_parser.add_argument("--open-positions", type=int, default=0)
    risk_parser.add_argument("--leverage", type=_decimal_arg, default=Decimal("1"))

    paper_parser = subparsers.add_parser(
        "paper-execution-smoke",
        help="Evaluate signals, risk-check them, and simulate paper fills",
    )
    paper_parser.add_argument("--pair", default="B-BTC_USDT")
    paper_parser.add_argument("--interval", default="1h")
    paper_parser.add_argument("--lookback", type=int, default=120)
    paper_parser.add_argument("--equity", type=_decimal_arg, default=None)
    paper_parser.add_argument("--leverage", type=_decimal_arg, default=Decimal("1"))
    paper_parser.add_argument("--fee-rate", type=_decimal_arg, default=None)
    paper_parser.add_argument(
        "--fee-pct",
        type=_decimal_arg,
        default=None,
        help="Legacy single fee percentage for every fill.",
    )
    paper_parser.add_argument("--maker-fee-rate", type=_decimal_arg, default=None)
    paper_parser.add_argument("--maker-fee-pct", type=_decimal_arg, default=None)
    paper_parser.add_argument("--taker-fee-rate", type=_decimal_arg, default=None)
    paper_parser.add_argument("--taker-fee-pct", type=_decimal_arg, default=None)
    paper_parser.add_argument("--fee-gst-rate", type=_decimal_arg, default=None)
    paper_parser.add_argument(
        "--fee-gst-pct",
        type=_decimal_arg,
        default=None,
        help="GST percentage applied on trading fees, e.g. 18 for CoinDCX INR-M.",
    )
    paper_parser.add_argument("--entry-fee-type", choices=("maker", "taker"), default="maker")
    paper_parser.add_argument("--exit-fee-type", choices=("maker", "taker"), default="taker")
    paper_parser.add_argument("--slippage-pct", type=_decimal_arg, default=Decimal("0"))
    paper_parser.add_argument(
        "--stop-slippage-pct",
        type=_decimal_arg,
        default=None,
        help="Separate stop-exit slippage percentage. Defaults to normal slippage.",
    )
    paper_parser.add_argument(
        "--demo-entry",
        action="store_true",
        help="Append a synthetic long entry so the paper fill path is visible.",
    )
    paper_parser.add_argument(
        "--demo-stop-pct",
        type=_decimal_arg,
        default=Decimal("1"),
    )
    paper_parser.add_argument(
        "--demo-take-profit-pct",
        type=_decimal_arg,
        default=Decimal("2"),
    )

    backtest_parser = subparsers.add_parser(
        "backtest",
        help="Run a paper-only historical backtest through strategy, risk, and execution",
    )
    backtest_parser.add_argument("--pair", default="B-BTC_USDT")
    backtest_parser.add_argument("--interval", default="1h")
    backtest_parser.add_argument("--lookback", type=int, default=500)
    backtest_parser.add_argument("--equity", type=_decimal_arg, default=None)
    backtest_parser.add_argument("--leverage", type=_decimal_arg, default=Decimal("1"))
    backtest_parser.add_argument(
        "--risk-per-trade-pct",
        type=_decimal_arg,
        default=None,
        help="Override max risk per trade percentage for this backtest run.",
    )
    backtest_parser.add_argument(
        "--compound-risk-equity",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Use current account equity for risk sizing. Default is fixed initial equity.",
    )
    backtest_parser.add_argument(
        "--stop-loss-pct",
        type=_decimal_arg,
        default=None,
        help="Override strategy stop-loss with a fixed percentage from entry.",
    )
    backtest_parser.add_argument(
        "--take-profit-pct",
        type=_decimal_arg,
        default=None,
        help="Override strategy take-profit with a fixed percentage from entry.",
    )
    backtest_parser.add_argument("--fee-rate", type=_decimal_arg, default=None)
    backtest_parser.add_argument(
        "--fee-pct",
        type=_decimal_arg,
        default=None,
        help="Legacy single fee percentage for every fill.",
    )
    backtest_parser.add_argument("--maker-fee-rate", type=_decimal_arg, default=None)
    backtest_parser.add_argument(
        "--maker-fee-pct",
        type=_decimal_arg,
        default=None,
        help="Maker fee percentage, e.g. 0.02 for CoinDCX INR-M futures.",
    )
    backtest_parser.add_argument("--taker-fee-rate", type=_decimal_arg, default=None)
    backtest_parser.add_argument(
        "--taker-fee-pct",
        type=_decimal_arg,
        default=None,
        help="Taker fee percentage, e.g. 0.05 for CoinDCX INR-M futures.",
    )
    backtest_parser.add_argument("--fee-gst-rate", type=_decimal_arg, default=None)
    backtest_parser.add_argument(
        "--fee-gst-pct",
        type=_decimal_arg,
        default=None,
        help="GST percentage applied on trading fees, e.g. 18 for CoinDCX INR-M.",
    )
    backtest_parser.add_argument("--entry-fee-type", choices=("maker", "taker"), default="maker")
    backtest_parser.add_argument("--exit-fee-type", choices=("maker", "taker"), default="taker")
    backtest_parser.add_argument("--slippage-pct", type=_decimal_arg, default=Decimal("0"))
    backtest_parser.add_argument(
        "--stop-slippage-pct",
        type=_decimal_arg,
        default=None,
        help="Separate stop-exit slippage percentage. Defaults to normal slippage.",
    )
    backtest_parser.add_argument("--funding-fee-rate", type=_decimal_arg, default=None)
    backtest_parser.add_argument(
        "--funding-fee-pct",
        type=_decimal_arg,
        default=None,
        help="Signed funding percentage per funding event. Positive means longs pay shorts.",
    )
    backtest_parser.add_argument("--funding-interval-hours", type=int, default=8)
    backtest_parser.add_argument(
        "--trailing-stop",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Enable candle-close trailing stop simulation for this backtest.",
    )
    backtest_parser.add_argument(
        "--trailing-stop-activation-pct",
        type=_decimal_arg,
        default=None,
        help="Favorable move needed before trailing starts, in percent.",
    )
    backtest_parser.add_argument(
        "--trailing-stop-distance-pct",
        type=_decimal_arg,
        default=None,
        help="Distance between close price and trailed stop, in percent.",
    )
    backtest_parser.add_argument(
        "--atr-dynamic-exits",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Recalculate ATR-based stop/target levels after each closed candle.",
    )
    backtest_parser.add_argument("--atr-period", type=int, default=14)
    backtest_parser.add_argument(
        "--atr-stop-multiple",
        type=_decimal_arg,
        default=Decimal("1.5"),
    )
    backtest_parser.add_argument(
        "--atr-take-profit-multiple",
        type=_decimal_arg,
        default=Decimal("3"),
    )
    backtest_parser.add_argument(
        "--atr-take-profit-mode",
        choices=ATR_TAKE_PROFIT_MODES,
        default="none",
        help="How dynamic ATR exits manage take profit levels.",
    )
    backtest_parser.add_argument(
        "--execution-interval",
        default=None,
        help="Optional lower timeframe for intrabar fills/re-entries, e.g. 5m under 1h.",
    )
    backtest_parser.add_argument(
        "--intrabar-reentry",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Allow lower-timeframe re-entry inside a parent candle after an exit.",
    )
    backtest_parser.add_argument("--max-reentries-per-candle", type=int, default=0)
    backtest_parser.add_argument("--reentry-cooldown-candles", type=int, default=1)
    backtest_parser.add_argument("--stop-loss-cooldown-candles", type=int, default=1)
    backtest_parser.add_argument("--max-consecutive-losses", type=int, default=2)
    backtest_parser.add_argument("--loss-cooldown-candles", type=int, default=4)
    backtest_parser.add_argument(
        "--strategy",
        choices=STRATEGY_CHOICES,
        default="all",
        help="Strategy set to run",
    )
    backtest_parser.add_argument(
        "--equity-giveback-guard",
        action=argparse.BooleanOptionalAction,
        default=False,
    )
    backtest_parser.add_argument(
        "--equity-giveback-threshold-pct",
        type=_decimal_arg,
        default=Decimal("3.5"),
    )
    backtest_parser.add_argument(
        "--equity-giveback-cooldown-candles",
        type=int,
        default=72,
    )
    backtest_parser.add_argument(
        "--loss-streak-cooldown",
        action=argparse.BooleanOptionalAction,
        default=False,
    )
    backtest_parser.add_argument(
        "--consecutive-loss-limit",
        type=int,
        default=3,
    )
    backtest_parser.add_argument(
        "--loss-streak-cooldown-candles",
        type=int,
        default=12,
    )
    backtest_parser.add_argument(
        "--rolling-loss-window",
        type=int,
        default=8,
    )
    backtest_parser.add_argument(
        "--rolling-loss-limit",
        type=int,
        default=5,
    )
    backtest_parser.add_argument(
        "--rolling-loss-cooldown-candles",
        type=int,
        default=36,
    )
    backtest_parser.add_argument(
        "--post-spike-cooldown",
        action=argparse.BooleanOptionalAction,
        default=False,
    )
    backtest_parser.add_argument(
        "--post-spike-lookback-candles",
        type=int,
        default=50,
    )
    backtest_parser.add_argument(
        "--post-spike-gain-threshold-pct",
        type=_decimal_arg,
        default=Decimal("5.0"),
    )
    backtest_parser.add_argument(
        "--post-spike-cooldown-candles",
        type=int,
        default=24,
    )
    backtest_parser.add_argument(
        "--breakeven-enabled",
        action=argparse.BooleanOptionalAction,
        default=False,
    )
    backtest_parser.add_argument(
        "--breakeven-activation-r",
        type=_decimal_arg,
        default=Decimal("1.0"),
    )
    backtest_parser.add_argument(
        "--breakeven-offset-r",
        type=_decimal_arg,
        default=Decimal("0.0"),
    )
    backtest_parser.add_argument(
        "--profit-lock-enabled",
        action=argparse.BooleanOptionalAction,
        default=False,
    )
    backtest_parser.add_argument(
        "--profit-lock-activation-r",
        type=_decimal_arg,
        default=Decimal("1.5"),
    )
    backtest_parser.add_argument(
        "--profit-lock-r",
        type=_decimal_arg,
        default=Decimal("0.5"),
    )
    backtest_parser.add_argument(
        "--atr-trail-after-r",
        action=argparse.BooleanOptionalAction,
        default=False,
    )
    backtest_parser.add_argument(
        "--atr-trail-activation-r",
        type=_decimal_arg,
        default=Decimal("2.0"),
    )
    backtest_parser.add_argument(
        "--chop-filter",
        action=argparse.BooleanOptionalAction,
        default=False,
    )
    backtest_parser.add_argument(
        "--min-ema-gap-pct",
        type=_decimal_arg,
        default=Decimal("0.15"),
    )
    backtest_parser.add_argument(
        "--min-atr-pct",
        type=_decimal_arg,
        default=Decimal("0.2"),
    )
    backtest_parser.add_argument(
        "--block-flat-ema",
        action=argparse.BooleanOptionalAction,
        default=False,
    )
    backtest_parser.add_argument(
        "--block-low-atr",
        action=argparse.BooleanOptionalAction,
        default=False,
    )
    backtest_parser.add_argument(
        "--atr-trailing-multiple",
        type=_decimal_arg,
        default=Decimal("2.0"),
    )
    backtest_parser.add_argument(
        "--recent-count",
        type=int,
        default=10,
        help="Number of recent trades/orders/fills/reports to include in JSON output",
    )

    sweep_parser = subparsers.add_parser(
        "research-sweep",
        help="Run a batch paper backtest sweep across pairs, intervals, and strategy variants",
    )
    sweep_parser.add_argument(
        "--pairs",
        default=",".join(DEFAULT_SWEEP_PAIRS),
        help="Comma-separated futures pairs to test",
    )
    sweep_parser.add_argument(
        "--intervals",
        default=",".join(DEFAULT_INTERVALS),
        help="Comma-separated candle intervals to test. Defaults to 5m, 15m, and 1h.",
    )
    sweep_parser.add_argument(
        "--variant-names",
        default=None,
        help="Comma-separated strategy variants to test, e.g. hybrid_meta,adaptive_hybrid.",
    )
    sweep_parser.add_argument("--lookback", type=int, default=1000)
    sweep_parser.add_argument("--equity", type=_decimal_arg, default=None)
    sweep_parser.add_argument("--leverage", type=_decimal_arg, default=Decimal("3"))
    sweep_parser.add_argument(
        "--leverage-grid",
        default=None,
        help="Comma-separated leverage values to vary, e.g. 1,2,3,4,5,6,7,8.",
    )
    sweep_parser.add_argument(
        "--risk-per-trade-pct",
        type=_decimal_arg,
        default=None,
        help="Override max risk per trade percentage for this sweep.",
    )
    sweep_parser.add_argument(
        "--risk-grid",
        default=None,
        help="Comma-separated risk-per-trade percentages to vary, e.g. 1,2,3.",
    )
    sweep_parser.add_argument(
        "--compound-risk-equity",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Use current account equity for risk sizing. Default is fixed initial equity.",
    )
    sweep_parser.add_argument(
        "--stop-loss-pct",
        type=_decimal_arg,
        default=None,
        help="Override strategy stop-loss with a fixed percentage from entry.",
    )
    sweep_parser.add_argument(
        "--take-profit-pct",
        type=_decimal_arg,
        default=None,
        help="Override strategy take-profit with a fixed percentage from entry.",
    )
    sweep_parser.add_argument(
        "--take-profit-grid",
        default=None,
        help="Comma-separated fixed take-profit percentages to vary. Use default/none for strategy exits.",
    )
    sweep_parser.add_argument("--fee-rate", type=_decimal_arg, default=None)
    sweep_parser.add_argument(
        "--fee-pct",
        type=_decimal_arg,
        default=None,
        help="Legacy single fee percentage for every fill.",
    )
    sweep_parser.add_argument("--maker-fee-rate", type=_decimal_arg, default=None)
    sweep_parser.add_argument("--maker-fee-pct", type=_decimal_arg, default=None)
    sweep_parser.add_argument("--taker-fee-rate", type=_decimal_arg, default=None)
    sweep_parser.add_argument("--taker-fee-pct", type=_decimal_arg, default=None)
    sweep_parser.add_argument("--fee-gst-rate", type=_decimal_arg, default=None)
    sweep_parser.add_argument(
        "--fee-gst-pct",
        type=_decimal_arg,
        default=None,
        help="GST percentage applied on trading fees, e.g. 18 for CoinDCX INR-M.",
    )
    sweep_parser.add_argument("--entry-fee-type", choices=("maker", "taker"), default="maker")
    sweep_parser.add_argument("--exit-fee-type", choices=("maker", "taker"), default="taker")
    sweep_parser.add_argument("--slippage-pct", type=_decimal_arg, default=Decimal("0.02"))
    sweep_parser.add_argument(
        "--stop-slippage-pct",
        type=_decimal_arg,
        default=None,
        help="Separate stop-exit slippage percentage. Defaults to normal slippage.",
    )
    sweep_parser.add_argument("--funding-fee-rate", type=_decimal_arg, default=None)
    sweep_parser.add_argument(
        "--funding-fee-pct",
        type=_decimal_arg,
        default=None,
        help="Signed funding percentage per funding event. Positive means longs pay shorts.",
    )
    sweep_parser.add_argument("--funding-interval-hours", type=int, default=8)
    sweep_parser.add_argument(
        "--trailing-stop",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Enable candle-close trailing stop simulation for the sweep.",
    )
    sweep_parser.add_argument(
        "--trailing-stop-activation-pct",
        type=_decimal_arg,
        default=None,
    )
    sweep_parser.add_argument(
        "--trailing-stop-distance-pct",
        type=_decimal_arg,
        default=None,
    )
    sweep_parser.add_argument(
        "--trailing-grid",
        default=None,
        help="Comma-separated trailing profiles: off or activation:distance, e.g. off,2:3,3:5.",
    )
    sweep_parser.add_argument(
        "--atr-dynamic-exits",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Recalculate ATR-based stop/target levels after each closed candle.",
    )
    sweep_parser.add_argument(
        "--atr-grid",
        default=None,
        help="Comma-separated ATR dynamic exit states to vary, e.g. off,on.",
    )
    sweep_parser.add_argument("--atr-period", type=int, default=14)
    sweep_parser.add_argument(
        "--atr-stop-multiple",
        type=_decimal_arg,
        default=Decimal("1.5"),
    )
    sweep_parser.add_argument(
        "--atr-take-profit-multiple",
        type=_decimal_arg,
        default=Decimal("3"),
    )
    sweep_parser.add_argument(
        "--atr-take-profit-mode",
        choices=ATR_TAKE_PROFIT_MODES,
        default="none",
        help="How dynamic ATR exits manage take profit levels.",
    )
    sweep_parser.add_argument(
        "--equity-giveback-guard",
        action=argparse.BooleanOptionalAction,
        default=False,
    )
    sweep_parser.add_argument(
        "--equity-giveback-threshold-pct",
        type=_decimal_arg,
        default=Decimal("3.5"),
    )
    sweep_parser.add_argument(
        "--equity-giveback-cooldown-candles",
        type=int,
        default=72,
    )
    sweep_parser.add_argument(
        "--loss-streak-cooldown",
        action=argparse.BooleanOptionalAction,
        default=False,
    )
    sweep_parser.add_argument(
        "--consecutive-loss-limit",
        type=int,
        default=3,
    )
    sweep_parser.add_argument(
        "--loss-streak-cooldown-candles",
        type=int,
        default=12,
    )
    sweep_parser.add_argument(
        "--rolling-loss-window",
        type=int,
        default=8,
    )
    sweep_parser.add_argument(
        "--rolling-loss-limit",
        type=int,
        default=5,
    )
    sweep_parser.add_argument(
        "--rolling-loss-cooldown-candles",
        type=int,
        default=36,
    )
    sweep_parser.add_argument(
        "--post-spike-cooldown",
        action=argparse.BooleanOptionalAction,
        default=False,
    )
    sweep_parser.add_argument(
        "--post-spike-lookback-candles",
        type=int,
        default=50,
    )
    sweep_parser.add_argument(
        "--post-spike-gain-threshold-pct",
        type=_decimal_arg,
        default=Decimal("5.0"),
    )
    sweep_parser.add_argument(
        "--post-spike-cooldown-candles",
        type=int,
        default=24,
    )
    sweep_parser.add_argument(
        "--breakeven-enabled",
        action=argparse.BooleanOptionalAction,
        default=False,
    )
    sweep_parser.add_argument(
        "--breakeven-activation-r",
        type=_decimal_arg,
        default=Decimal("1.0"),
    )
    sweep_parser.add_argument(
        "--breakeven-offset-r",
        type=_decimal_arg,
        default=Decimal("0.0"),
    )
    sweep_parser.add_argument(
        "--profit-lock-enabled",
        action=argparse.BooleanOptionalAction,
        default=False,
    )
    sweep_parser.add_argument(
        "--profit-lock-activation-r",
        type=_decimal_arg,
        default=Decimal("1.5"),
    )
    sweep_parser.add_argument(
        "--profit-lock-r",
        type=_decimal_arg,
        default=Decimal("0.5"),
    )
    sweep_parser.add_argument(
        "--atr-trail-after-r",
        action=argparse.BooleanOptionalAction,
        default=False,
    )
    sweep_parser.add_argument(
        "--atr-trail-activation-r",
        type=_decimal_arg,
        default=Decimal("2.0"),
    )
    sweep_parser.add_argument(
        "--chop-filter",
        action=argparse.BooleanOptionalAction,
        default=False,
    )
    sweep_parser.add_argument(
        "--min-ema-gap-pct",
        type=_decimal_arg,
        default=Decimal("0.15"),
    )
    sweep_parser.add_argument(
        "--min-atr-pct",
        type=_decimal_arg,
        default=Decimal("0.2"),
    )
    sweep_parser.add_argument(
        "--block-flat-ema",
        action=argparse.BooleanOptionalAction,
        default=False,
    )
    sweep_parser.add_argument(
        "--block-low-atr",
        action=argparse.BooleanOptionalAction,
        default=False,
    )
    sweep_parser.add_argument(
        "--atr-trailing-multiple",
        type=_decimal_arg,
        default=Decimal("2.0"),
    )
    sweep_parser.add_argument(
        "--until",
        default=None,
        help="Local cutoff time like 08:30, or ISO datetime. Stops before starting the next run.",
    )
    sweep_parser.add_argument(
        "--output-dir",
        default="research/backtests",
        help="Directory where timestamped sweep outputs are written",
    )
    sweep_parser.add_argument(
        "--max-runs",
        type=int,
        default=None,
        help="Optional cap for smoke testing or short sweeps",
    )

    quick_parser = subparsers.add_parser(
        "quick-validate",
        help="Fast staged strategy validator: quick screen first, then deeper tests of top candidates",
    )
    quick_parser.add_argument(
        "--pairs",
        default=",".join(DEFAULT_SWEEP_PAIRS),
        help="Comma-separated futures pairs to screen",
    )
    quick_parser.add_argument(
        "--intervals",
        default="5m,15m,30m,1h",
        help="Comma-separated intervals. Default skips 2h and focuses on 5m, 15m, 30m, 1h.",
    )
    quick_parser.add_argument(
        "--variant-names",
        default="hybrid_meta_v2",
        help="Comma-separated variants. Default focuses on Hybrid Meta V2.",
    )
    quick_parser.add_argument("--quick-lookback", type=int, default=240)
    quick_parser.add_argument("--validation-lookback", type=int, default=500)
    quick_parser.add_argument("--final-lookback", type=int, default=1000)
    quick_parser.add_argument(
        "--quick-max-runs",
        type=int,
        default=60,
        help="Maximum broad-screen runs before selecting top candidates.",
    )
    quick_parser.add_argument("--top-validation", type=int, default=8)
    quick_parser.add_argument("--top-final", type=int, default=3)
    quick_parser.add_argument(
        "--quick-only",
        action="store_true",
        help="Run only the fast broad screen and skip validation/final stages.",
    )
    quick_parser.add_argument("--equity", type=_decimal_arg, default=None)
    quick_parser.add_argument("--leverage", type=_decimal_arg, default=Decimal("3"))
    quick_parser.add_argument(
        "--leverage-grid",
        default="1,3,5",
        help="Coarse leverage grid. Keep this coarse to avoid slow overfitting.",
    )
    quick_parser.add_argument("--risk-per-trade-pct", type=_decimal_arg, default=None)
    quick_parser.add_argument(
        "--risk-grid",
        default="1,2,3",
        help="Coarse risk-per-trade percentage grid.",
    )
    quick_parser.add_argument(
        "--compound-risk-equity",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Use current equity for sizing. Default keeps risk based on initial equity.",
    )
    quick_parser.add_argument("--stop-loss-pct", type=_decimal_arg, default=None)
    quick_parser.add_argument("--take-profit-pct", type=_decimal_arg, default=None)
    quick_parser.add_argument(
        "--take-profit-grid",
        default="default,3,5",
        help="Coarse take-profit grid. Use default/none for strategy exits.",
    )
    quick_parser.add_argument("--fee-rate", type=_decimal_arg, default=None)
    quick_parser.add_argument("--fee-pct", type=_decimal_arg, default=None)
    quick_parser.add_argument("--maker-fee-rate", type=_decimal_arg, default=None)
    quick_parser.add_argument("--maker-fee-pct", type=_decimal_arg, default=None)
    quick_parser.add_argument("--taker-fee-rate", type=_decimal_arg, default=None)
    quick_parser.add_argument("--taker-fee-pct", type=_decimal_arg, default=None)
    quick_parser.add_argument("--fee-gst-rate", type=_decimal_arg, default=None)
    quick_parser.add_argument("--fee-gst-pct", type=_decimal_arg, default=None)
    quick_parser.add_argument("--entry-fee-type", choices=("maker", "taker"), default="maker")
    quick_parser.add_argument("--exit-fee-type", choices=("maker", "taker"), default="taker")
    quick_parser.add_argument("--slippage-pct", type=_decimal_arg, default=Decimal("0.02"))
    quick_parser.add_argument("--stop-slippage-pct", type=_decimal_arg, default=Decimal("0.02"))
    quick_parser.add_argument("--funding-fee-rate", type=_decimal_arg, default=None)
    quick_parser.add_argument("--funding-fee-pct", type=_decimal_arg, default=None)
    quick_parser.add_argument("--funding-interval-hours", type=int, default=8)
    quick_parser.add_argument(
        "--trailing-grid",
        default="off,2:3,3:5",
        help="Coarse trailing profiles: off or activation:distance.",
    )
    quick_parser.add_argument(
        "--atr-grid",
        default="off,on",
        help="ATR dynamic exit states to test: off,on.",
    )
    quick_parser.add_argument("--atr-period", type=int, default=14)
    quick_parser.add_argument("--atr-stop-multiple", type=_decimal_arg, default=Decimal("1.5"))
    quick_parser.add_argument("--atr-take-profit-multiple", type=_decimal_arg, default=Decimal("3"))
    quick_parser.add_argument(
        "--atr-take-profit-mode",
        choices=ATR_TAKE_PROFIT_MODES,
        default="none",
    )
    quick_parser.add_argument(
        "--output-dir",
        default="research/backtests/quick_validator",
        help="Directory where staged validator outputs are written.",
    )

    dashboard_parser = subparsers.add_parser(
        "dashboard",
        help="Run the local paper/backtest monitoring dashboard",
    )
    dashboard_parser.add_argument("--host", default="127.0.0.1")
    dashboard_parser.add_argument("--port", type=int, default=8000)
    dashboard_parser.add_argument("--pair", default="B-SOL_USDT")
    dashboard_parser.add_argument("--interval", default="1h")
    dashboard_parser.add_argument("--lookback", type=int, default=1000)
    dashboard_parser.add_argument("--equity", type=_decimal_arg, default=None)
    dashboard_parser.add_argument("--leverage", type=_decimal_arg, default=Decimal("3"))
    dashboard_parser.add_argument("--risk-per-trade-pct", type=_decimal_arg, default=None)
    dashboard_parser.add_argument(
        "--compound-risk-equity",
        action=argparse.BooleanOptionalAction,
        default=False,
    )
    dashboard_parser.add_argument("--stop-loss-pct", type=_decimal_arg, default=None)
    dashboard_parser.add_argument("--take-profit-pct", type=_decimal_arg, default=None)
    dashboard_parser.add_argument("--fee-rate", type=_decimal_arg, default=None)
    dashboard_parser.add_argument(
        "--fee-pct",
        type=_decimal_arg,
        default=None,
        help="Legacy default dashboard fee percentage per fill.",
    )
    dashboard_parser.add_argument("--maker-fee-rate", type=_decimal_arg, default=None)
    dashboard_parser.add_argument("--maker-fee-pct", type=_decimal_arg, default=None)
    dashboard_parser.add_argument("--taker-fee-rate", type=_decimal_arg, default=None)
    dashboard_parser.add_argument("--taker-fee-pct", type=_decimal_arg, default=None)
    dashboard_parser.add_argument("--fee-gst-rate", type=_decimal_arg, default=None)
    dashboard_parser.add_argument(
        "--fee-gst-pct",
        type=_decimal_arg,
        default=None,
        help="GST percentage applied on trading fees, e.g. 18 for CoinDCX INR-M.",
    )
    dashboard_parser.add_argument("--entry-fee-type", choices=("maker", "taker"), default="maker")
    dashboard_parser.add_argument("--exit-fee-type", choices=("maker", "taker"), default="taker")
    dashboard_parser.add_argument("--slippage-pct", type=_decimal_arg, default=Decimal("0.02"))
    dashboard_parser.add_argument(
        "--stop-slippage-pct",
        type=_decimal_arg,
        default=Decimal("0.02"),
    )
    dashboard_parser.add_argument("--funding-fee-rate", type=_decimal_arg, default=None)
    dashboard_parser.add_argument("--funding-fee-pct", type=_decimal_arg, default=None)
    dashboard_parser.add_argument("--funding-interval-hours", type=int, default=8)
    dashboard_parser.add_argument(
        "--atr-dynamic-exits",
        action=argparse.BooleanOptionalAction,
        default=False,
    )
    dashboard_parser.add_argument("--atr-period", type=int, default=14)
    dashboard_parser.add_argument("--atr-stop-multiple", type=_decimal_arg, default=Decimal("1.5"))
    dashboard_parser.add_argument(
        "--atr-take-profit-multiple",
        type=_decimal_arg,
        default=Decimal("3"),
    )
    dashboard_parser.add_argument(
        "--atr-take-profit-mode",
        choices=ATR_TAKE_PROFIT_MODES,
        default="none",
    )
    dashboard_parser.add_argument(
        "--equity-giveback-guard",
        action=argparse.BooleanOptionalAction,
        default=False,
    )
    dashboard_parser.add_argument(
        "--equity-giveback-threshold-pct",
        type=_decimal_arg,
        default=Decimal("3.5"),
    )
    dashboard_parser.add_argument(
        "--equity-giveback-cooldown-candles",
        type=int,
        default=72,
    )
    dashboard_parser.add_argument(
        "--loss-streak-cooldown",
        action=argparse.BooleanOptionalAction,
        default=False,
    )
    dashboard_parser.add_argument(
        "--consecutive-loss-limit",
        type=int,
        default=3,
    )
    dashboard_parser.add_argument(
        "--loss-streak-cooldown-candles",
        type=int,
        default=12,
    )
    dashboard_parser.add_argument(
        "--rolling-loss-window",
        type=int,
        default=8,
    )
    dashboard_parser.add_argument(
        "--rolling-loss-limit",
        type=int,
        default=5,
    )
    dashboard_parser.add_argument(
        "--rolling-loss-cooldown-candles",
        type=int,
        default=36,
    )
    dashboard_parser.add_argument(
        "--post-spike-cooldown",
        action=argparse.BooleanOptionalAction,
        default=False,
    )
    dashboard_parser.add_argument(
        "--post-spike-lookback-candles",
        type=int,
        default=50,
    )
    dashboard_parser.add_argument(
        "--post-spike-gain-threshold-pct",
        type=_decimal_arg,
        default=Decimal("5.0"),
    )
    dashboard_parser.add_argument(
        "--post-spike-cooldown-candles",
        type=int,
        default=24,
    )
    dashboard_parser.add_argument(
        "--breakeven-enabled",
        action=argparse.BooleanOptionalAction,
        default=False,
    )
    dashboard_parser.add_argument(
        "--breakeven-activation-r",
        type=_decimal_arg,
        default=Decimal("1.0"),
    )
    dashboard_parser.add_argument(
        "--breakeven-offset-r",
        type=_decimal_arg,
        default=Decimal("0.0"),
    )
    dashboard_parser.add_argument(
        "--profit-lock-enabled",
        action=argparse.BooleanOptionalAction,
        default=False,
    )
    dashboard_parser.add_argument(
        "--profit-lock-activation-r",
        type=_decimal_arg,
        default=Decimal("1.5"),
    )
    dashboard_parser.add_argument(
        "--profit-lock-r",
        type=_decimal_arg,
        default=Decimal("0.5"),
    )
    dashboard_parser.add_argument(
        "--atr-trail-after-r",
        action=argparse.BooleanOptionalAction,
        default=False,
    )
    dashboard_parser.add_argument(
        "--atr-trail-activation-r",
        type=_decimal_arg,
        default=Decimal("2.0"),
    )
    dashboard_parser.add_argument(
        "--chop-filter",
        action=argparse.BooleanOptionalAction,
        default=False,
    )
    dashboard_parser.add_argument(
        "--min-ema-gap-pct",
        type=_decimal_arg,
        default=Decimal("0.15"),
    )
    dashboard_parser.add_argument(
        "--min-atr-pct",
        type=_decimal_arg,
        default=Decimal("0.2"),
    )
    dashboard_parser.add_argument(
        "--block-flat-ema",
        action=argparse.BooleanOptionalAction,
        default=False,
    )
    dashboard_parser.add_argument(
        "--block-low-atr",
        action=argparse.BooleanOptionalAction,
        default=False,
    )
    dashboard_parser.add_argument(
        "--atr-trailing-multiple",
        type=_decimal_arg,
        default=Decimal("2.0"),
    )
    dashboard_parser.add_argument(
        "--strategy",
        choices=STRATEGY_CHOICES,
        default="bb_dynamic_grid",
    )
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    settings = load_settings()

    if args.command == "config-check":
        config_check()
    elif args.command == "auth-smoke":
        auth_smoke()
    elif args.command == "auth-diagnose":
        auth_diagnose()
    elif args.command == "public-smoke":
        public_smoke(args.pair)
    elif args.command == "private-read-smoke":
        private_read_smoke()
    elif args.command == "ws-replay-smoke":
        ws_replay_smoke(args.pair)
    elif args.command == "market-stream":
        market_stream(
            pair=args.pair,
            interval=args.interval,
            depth=args.depth,
            max_events=args.max_events,
            include_current_prices=args.include_current_prices,
        )
    elif args.command == "indicator-smoke":
        indicator_smoke(pair=args.pair, interval=args.interval, lookback=args.lookback)
    elif args.command == "strategy-smoke":
        strategy_smoke(pair=args.pair, interval=args.interval, lookback=args.lookback)
    elif args.command == "risk-smoke":
        risk_smoke(
            pair=args.pair,
            interval=args.interval,
            lookback=args.lookback,
            account_equity=args.equity if args.equity is not None else settings.paper_starting_equity,
            daily_realized_pnl=args.daily_pnl,
            open_positions=args.open_positions,
            leverage=args.leverage,
        )
    elif args.command == "paper-execution-smoke":
        maker_fee_rate, taker_fee_rate = _fee_rates_from_args(
            maker_fee_rate=args.maker_fee_rate,
            maker_fee_pct=args.maker_fee_pct,
            taker_fee_rate=args.taker_fee_rate,
            taker_fee_pct=args.taker_fee_pct,
            fee_rate=args.fee_rate,
            fee_pct=args.fee_pct,
            default_maker_fee_rate=settings.risk.maker_fee_rate,
            default_taker_fee_rate=settings.risk.taker_fee_rate,
        )
        fee_gst_rate = _fee_gst_rate_from_args(
            fee_gst_rate=args.fee_gst_rate,
            fee_gst_pct=args.fee_gst_pct,
            default=settings.risk.fee_gst_rate,
        )
        paper_execution_smoke(
            pair=args.pair,
            interval=args.interval,
            lookback=args.lookback,
            account_equity=args.equity if args.equity is not None else settings.paper_starting_equity,
            leverage=args.leverage,
            maker_fee_rate=maker_fee_rate,
            taker_fee_rate=taker_fee_rate,
            fee_gst_rate=fee_gst_rate,
            entry_fee_type=args.entry_fee_type,
            exit_fee_type=args.exit_fee_type,
            slippage_pct=args.slippage_pct,
            stop_slippage_pct=args.stop_slippage_pct,
            demo_entry=args.demo_entry,
            demo_stop_pct=args.demo_stop_pct,
            demo_take_profit_pct=args.demo_take_profit_pct,
        )
    elif args.command == "backtest":
        maker_fee_rate, taker_fee_rate = _fee_rates_from_args(
            maker_fee_rate=args.maker_fee_rate,
            maker_fee_pct=args.maker_fee_pct,
            taker_fee_rate=args.taker_fee_rate,
            taker_fee_pct=args.taker_fee_pct,
            fee_rate=args.fee_rate,
            fee_pct=args.fee_pct,
            default_maker_fee_rate=settings.risk.maker_fee_rate,
            default_taker_fee_rate=settings.risk.taker_fee_rate,
        )
        fee_gst_rate = _fee_gst_rate_from_args(
            fee_gst_rate=args.fee_gst_rate,
            fee_gst_pct=args.fee_gst_pct,
            default=settings.risk.fee_gst_rate,
        )
        funding_fee_rate = _funding_fee_rate_from_args(
            funding_fee_rate=args.funding_fee_rate,
            funding_fee_pct=args.funding_fee_pct,
        )
        backtest_command(
            pair=args.pair,
            interval=args.interval,
            lookback=args.lookback,
            account_equity=args.equity if args.equity is not None else settings.paper_starting_equity,
            leverage=args.leverage,
            risk_per_trade_pct=args.risk_per_trade_pct,
            compound_risk_equity=args.compound_risk_equity,
            stop_loss_pct=args.stop_loss_pct,
            take_profit_pct=args.take_profit_pct,
            maker_fee_rate=maker_fee_rate,
            taker_fee_rate=taker_fee_rate,
            fee_gst_rate=fee_gst_rate,
            entry_fee_type=args.entry_fee_type,
            exit_fee_type=args.exit_fee_type,
            slippage_pct=args.slippage_pct,
            stop_slippage_pct=args.stop_slippage_pct,
            funding_fee_rate=funding_fee_rate,
            funding_interval_hours=args.funding_interval_hours,
            trailing_stop_enabled=args.trailing_stop,
            trailing_stop_activation_pct=args.trailing_stop_activation_pct,
            trailing_stop_distance_pct=args.trailing_stop_distance_pct,
            atr_dynamic_exits_enabled=args.atr_dynamic_exits,
            atr_period=args.atr_period,
            atr_stop_multiple=args.atr_stop_multiple,
            atr_take_profit_multiple=args.atr_take_profit_multiple,
            atr_take_profit_mode=args.atr_take_profit_mode,
            execution_interval=args.execution_interval,
            intrabar_reentry_enabled=args.intrabar_reentry,
            max_reentries_per_candle=args.max_reentries_per_candle,
            reentry_cooldown_candles=args.reentry_cooldown_candles,
            stop_loss_cooldown_candles=args.stop_loss_cooldown_candles,
            max_consecutive_losses=args.max_consecutive_losses,
            loss_cooldown_candles=args.loss_cooldown_candles,
            strategy_name=args.strategy,
            recent_count=args.recent_count,
            equity_giveback_guard_enabled=args.equity_giveback_guard,
            equity_giveback_threshold_pct=args.equity_giveback_threshold_pct / Decimal("100"),
            equity_giveback_cooldown_candles=args.equity_giveback_cooldown_candles,
            loss_streak_cooldown_enabled=args.loss_streak_cooldown,
            consecutive_loss_limit=args.consecutive_loss_limit,
            loss_streak_cooldown_candles=args.loss_streak_cooldown_candles,
            rolling_loss_window=args.rolling_loss_window,
            rolling_loss_limit=args.rolling_loss_limit,
            rolling_loss_cooldown_candles=args.rolling_loss_cooldown_candles,
            post_spike_cooldown_enabled=args.post_spike_cooldown,
            post_spike_lookback_candles=args.post_spike_lookback_candles,
            post_spike_gain_threshold_pct=args.post_spike_gain_threshold_pct / Decimal("100"),
            post_spike_cooldown_candles=args.post_spike_cooldown_candles,
            breakeven_enabled=args.breakeven_enabled,
            breakeven_activation_r=args.breakeven_activation_r,
            breakeven_offset_r=args.breakeven_offset_r,
            profit_lock_enabled=args.profit_lock_enabled,
            profit_lock_activation_r=args.profit_lock_activation_r,
            profit_lock_r=args.profit_lock_r,
            atr_trail_after_r_enabled=args.atr_trail_after_r,
            atr_trail_activation_r=args.atr_trail_activation_r,
            chop_filter_enabled=args.chop_filter,
            min_ema_gap_pct=args.min_ema_gap_pct / Decimal("100"),
            min_atr_pct=args.min_atr_pct / Decimal("100"),
            block_flat_ema_enabled=args.block_flat_ema,
            block_low_atr_enabled=args.block_low_atr,
            atr_trailing_multiple=args.atr_trailing_multiple,
        )
    elif args.command == "research-sweep":
        maker_fee_rate, taker_fee_rate = _fee_rates_from_args(
            maker_fee_rate=args.maker_fee_rate,
            maker_fee_pct=args.maker_fee_pct,
            taker_fee_rate=args.taker_fee_rate,
            taker_fee_pct=args.taker_fee_pct,
            fee_rate=args.fee_rate,
            fee_pct=args.fee_pct,
            default_maker_fee_rate=settings.risk.maker_fee_rate,
            default_taker_fee_rate=settings.risk.taker_fee_rate,
        )
        fee_gst_rate = _fee_gst_rate_from_args(
            fee_gst_rate=args.fee_gst_rate,
            fee_gst_pct=args.fee_gst_pct,
            default=settings.risk.fee_gst_rate,
        )
        funding_fee_rate = _funding_fee_rate_from_args(
            funding_fee_rate=args.funding_fee_rate,
            funding_fee_pct=args.funding_fee_pct,
        )
        research_sweep_command(
            pairs=args.pairs,
            intervals=args.intervals,
            variant_names=args.variant_names,
            lookback=args.lookback,
            account_equity=args.equity if args.equity is not None else settings.paper_starting_equity,
            leverage=args.leverage,
            risk_per_trade_pct=args.risk_per_trade_pct,
            risk_grid=args.risk_grid,
            leverage_grid=args.leverage_grid,
            compound_risk_equity=args.compound_risk_equity,
            stop_loss_pct=args.stop_loss_pct,
            take_profit_pct=args.take_profit_pct,
            take_profit_grid=args.take_profit_grid,
            maker_fee_rate=maker_fee_rate,
            taker_fee_rate=taker_fee_rate,
            fee_gst_rate=fee_gst_rate,
            entry_fee_type=args.entry_fee_type,
            exit_fee_type=args.exit_fee_type,
            slippage_pct=args.slippage_pct,
            stop_slippage_pct=args.stop_slippage_pct,
            funding_fee_rate=funding_fee_rate,
            funding_interval_hours=args.funding_interval_hours,
            trailing_stop_enabled=args.trailing_stop,
            trailing_stop_activation_pct=args.trailing_stop_activation_pct,
            trailing_stop_distance_pct=args.trailing_stop_distance_pct,
            trailing_grid=args.trailing_grid,
            atr_dynamic_exits_enabled=args.atr_dynamic_exits,
            atr_grid=args.atr_grid,
            atr_period=args.atr_period,
            atr_stop_multiple=args.atr_stop_multiple,
            atr_take_profit_multiple=args.atr_take_profit_multiple,
            atr_take_profit_mode=args.atr_take_profit_mode,
            equity_giveback_guard_enabled=args.equity_giveback_guard,
            equity_giveback_threshold_pct=args.equity_giveback_threshold_pct / Decimal("100"),
            equity_giveback_cooldown_candles=args.equity_giveback_cooldown_candles,
            loss_streak_cooldown_enabled=args.loss_streak_cooldown,
            consecutive_loss_limit=args.consecutive_loss_limit,
            loss_streak_cooldown_candles=args.loss_streak_cooldown_candles,
            rolling_loss_window=args.rolling_loss_window,
            rolling_loss_limit=args.rolling_loss_limit,
            rolling_loss_cooldown_candles=args.rolling_loss_cooldown_candles,
            post_spike_cooldown_enabled=args.post_spike_cooldown,
            post_spike_lookback_candles=args.post_spike_lookback_candles,
            post_spike_gain_threshold_pct=args.post_spike_gain_threshold_pct / Decimal("100"),
            post_spike_cooldown_candles=args.post_spike_cooldown_candles,
            breakeven_enabled=args.breakeven_enabled,
            breakeven_activation_r=args.breakeven_activation_r,
            breakeven_offset_r=args.breakeven_offset_r,
            profit_lock_enabled=args.profit_lock_enabled,
            profit_lock_activation_r=args.profit_lock_activation_r,
            profit_lock_r=args.profit_lock_r,
            atr_trail_after_r_enabled=args.atr_trail_after_r,
            atr_trail_activation_r=args.atr_trail_activation_r,
            chop_filter_enabled=args.chop_filter,
            min_ema_gap_pct=args.min_ema_gap_pct / Decimal("100"),
            min_atr_pct=args.min_atr_pct / Decimal("100"),
            block_flat_ema_enabled=args.block_flat_ema,
            block_low_atr_enabled=args.block_low_atr,
            atr_trailing_multiple=args.atr_trailing_multiple,
            until=args.until,
            output_dir=args.output_dir,
            max_runs=args.max_runs,
        )
    elif args.command == "quick-validate":
        maker_fee_rate, taker_fee_rate = _fee_rates_from_args(
            maker_fee_rate=args.maker_fee_rate,
            maker_fee_pct=args.maker_fee_pct,
            taker_fee_rate=args.taker_fee_rate,
            taker_fee_pct=args.taker_fee_pct,
            fee_rate=args.fee_rate,
            fee_pct=args.fee_pct,
            default_maker_fee_rate=settings.risk.maker_fee_rate,
            default_taker_fee_rate=settings.risk.taker_fee_rate,
        )
        fee_gst_rate = _fee_gst_rate_from_args(
            fee_gst_rate=args.fee_gst_rate,
            fee_gst_pct=args.fee_gst_pct,
            default=settings.risk.fee_gst_rate,
        )
        funding_fee_rate = _funding_fee_rate_from_args(
            funding_fee_rate=args.funding_fee_rate,
            funding_fee_pct=args.funding_fee_pct,
        )
        quick_validate_command(
            pairs=args.pairs,
            intervals=args.intervals,
            variant_names=args.variant_names,
            quick_lookback=args.quick_lookback,
            validation_lookback=args.validation_lookback,
            final_lookback=args.final_lookback,
            quick_max_runs=args.quick_max_runs,
            top_validation=args.top_validation,
            top_final=args.top_final,
            quick_only=args.quick_only,
            account_equity=args.equity if args.equity is not None else settings.paper_starting_equity,
            leverage=args.leverage,
            risk_per_trade_pct=args.risk_per_trade_pct,
            risk_grid=args.risk_grid,
            leverage_grid=args.leverage_grid,
            compound_risk_equity=args.compound_risk_equity,
            stop_loss_pct=args.stop_loss_pct,
            take_profit_pct=args.take_profit_pct,
            take_profit_grid=args.take_profit_grid,
            maker_fee_rate=maker_fee_rate,
            taker_fee_rate=taker_fee_rate,
            fee_gst_rate=fee_gst_rate,
            entry_fee_type=args.entry_fee_type,
            exit_fee_type=args.exit_fee_type,
            slippage_pct=args.slippage_pct,
            stop_slippage_pct=args.stop_slippage_pct,
            funding_fee_rate=funding_fee_rate,
            funding_interval_hours=args.funding_interval_hours,
            trailing_grid=args.trailing_grid,
            atr_grid=args.atr_grid,
            atr_period=args.atr_period,
            atr_stop_multiple=args.atr_stop_multiple,
            atr_take_profit_multiple=args.atr_take_profit_multiple,
            atr_take_profit_mode=args.atr_take_profit_mode,
            output_dir=args.output_dir,
        )
    elif args.command == "dashboard":
        maker_fee_rate, taker_fee_rate = _fee_rates_from_args(
            maker_fee_rate=args.maker_fee_rate,
            maker_fee_pct=args.maker_fee_pct,
            taker_fee_rate=args.taker_fee_rate,
            taker_fee_pct=args.taker_fee_pct,
            fee_rate=args.fee_rate,
            fee_pct=args.fee_pct,
            default_maker_fee_rate=settings.risk.maker_fee_rate,
            default_taker_fee_rate=settings.risk.taker_fee_rate,
        )
        fee_gst_rate = _fee_gst_rate_from_args(
            fee_gst_rate=args.fee_gst_rate,
            fee_gst_pct=args.fee_gst_pct,
            default=settings.risk.fee_gst_rate,
        )
        funding_fee_rate = _funding_fee_rate_from_args(
            funding_fee_rate=args.funding_fee_rate,
            funding_fee_pct=args.funding_fee_pct,
        )
        run_dashboard(
            host=args.host,
            port=args.port,
            defaults=DashboardDefaults(
                pair=args.pair,
                interval=args.interval,
                lookback=args.lookback,
                equity=args.equity if args.equity is not None else settings.paper_starting_equity,
                leverage=args.leverage,
                risk_per_trade_pct=(
                    settings.risk.max_risk_per_trade_pct
                    if args.risk_per_trade_pct is None
                    else args.risk_per_trade_pct
                ),
                compound_risk_equity=args.compound_risk_equity,
                stop_loss_pct=_positive_optional_pct(args.stop_loss_pct, "--stop-loss-pct"),
                take_profit_pct=_positive_optional_pct(args.take_profit_pct, "--take-profit-pct"),
                maker_fee_rate=maker_fee_rate,
                taker_fee_rate=taker_fee_rate,
                fee_gst_rate=fee_gst_rate,
                entry_fee_type=args.entry_fee_type,
                exit_fee_type=args.exit_fee_type,
                slippage_pct=args.slippage_pct,
                stop_slippage_pct=args.stop_slippage_pct,
                funding_fee_rate=funding_fee_rate,
                funding_interval_hours=args.funding_interval_hours,
                atr_dynamic_exits_enabled=args.atr_dynamic_exits,
                atr_period=args.atr_period,
                atr_stop_multiple=args.atr_stop_multiple,
                atr_take_profit_multiple=args.atr_take_profit_multiple,
                atr_take_profit_mode=args.atr_take_profit_mode,
                equity_giveback_guard_enabled=args.equity_giveback_guard,
                equity_giveback_threshold_pct=args.equity_giveback_threshold_pct / Decimal("100"),
                equity_giveback_cooldown_candles=args.equity_giveback_cooldown_candles,
                loss_streak_cooldown_enabled=args.loss_streak_cooldown,
                consecutive_loss_limit=args.consecutive_loss_limit,
                loss_streak_cooldown_candles=args.loss_streak_cooldown_candles,
                rolling_loss_window=args.rolling_loss_window,
                rolling_loss_limit=args.rolling_loss_limit,
                rolling_loss_cooldown_candles=args.rolling_loss_cooldown_candles,
                post_spike_cooldown_enabled=args.post_spike_cooldown,
                post_spike_lookback_candles=args.post_spike_lookback_candles,
                post_spike_gain_threshold_pct=args.post_spike_gain_threshold_pct / Decimal("100"),
                post_spike_cooldown_candles=args.post_spike_cooldown_candles,
                breakeven_enabled=args.breakeven_enabled,
                breakeven_activation_r=args.breakeven_activation_r,
                breakeven_offset_r=args.breakeven_offset_r,
                profit_lock_enabled=args.profit_lock_enabled,
                profit_lock_activation_r=args.profit_lock_activation_r,
                profit_lock_r=args.profit_lock_r,
                atr_trail_after_r_enabled=args.atr_trail_after_r,
                atr_trail_activation_r=args.atr_trail_activation_r,
                chop_filter_enabled=args.chop_filter,
                min_ema_gap_pct=args.min_ema_gap_pct / Decimal("100"),
                min_atr_pct=args.min_atr_pct / Decimal("100"),
                block_flat_ema_enabled=args.block_flat_ema,
                block_low_atr_enabled=args.block_low_atr,
                atr_trailing_multiple=args.atr_trailing_multiple,
                strategy=args.strategy,
            ),
        )
    else:
        parser.error(f"Unknown command: {args.command}")


if __name__ == "__main__":
    main()
