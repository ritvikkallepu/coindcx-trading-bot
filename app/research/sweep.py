from __future__ import annotations

import csv
import json
import logging
from dataclasses import dataclass, replace
from datetime import datetime, time, timedelta
from decimal import Decimal
from math import gcd
from pathlib import Path
from typing import Callable, Iterable

from app.backtest.data_loader import load_historical_candle_series
from app.backtest.engine import BacktestEngine
from app.backtest.models import BacktestConfig, BacktestResult
from app.config import Settings
from app.data.open_interest import (
    OpenInterestFeatureSeries,
    load_binance_open_interest_proxy,
)
from app.exchange.coindcx_rest import CoinDCXFuturesClient
from app.exchange.errors import CoinDCXError
from app.fees import (
    COINDCX_FEE_GST_RATE,
    COINDCX_INR_M_MAKER_FEE_RATE,
    COINDCX_INR_M_TAKER_FEE_RATE,
    effective_fee_rate,
)
from app.risk.manager import RiskManager
from app.risk.models import InstrumentMetadata, convert_for_json
from app.research.excel import write_sweep_workbook
from app.strategies.adaptive_hybrid import AdaptiveHybridStrategy
from app.strategies.base import Strategy, StrategyEngine
from app.strategies.bb_dynamic_grid import BollingerDynamicFuturesGridStrategy
from app.strategies.bb_volume_reversion import BollingerVolumeMeanReversionStrategy
from app.strategies.ema_rsi_trend import EMARSICrossoverStrategy
from app.strategies.hybrid_meta import HybridMetaStrategy, HybridMetaV2Strategy
from app.strategies.rsi_macd_momentum import RSIMACDMomentumStrategy


DEFAULT_SWEEP_PAIRS = (
    "B-SOL_USDT",
    "B-ETH_USDT",
    "B-BTC_USDT",
    "B-ZEC_USDT",
    "B-BNB_USDT",
    "B-AIGENSYN_USDT",
    "B-RAVE_USDT",
    "B-BSB_USDT",
    "B-SAGA_USDT",
    "B-SAHARA_USDT",
    "B-STABLE_USDT",
    "B-CHIP_USDT",
    "B-KITE_USDT",
    "B-MEGA_USDT",
    "B-BASED_USDT",
    "B-STO_USDT",
    "B-KAITO_USDT",
    "B-SKY_USDT",
    "B-PIXEL_USDT",
    "B-ALICE_USDT",
)

DEFAULT_INTERVALS = ("5m", "15m", "1h")
OPEN_INTEREST_FAMILIES = {"hybrid_meta", "adaptive_hybrid"}


@dataclass(frozen=True)
class StrategyVariant:
    name: str
    family: str
    factory: Callable[[], Strategy]
    intervals: tuple[str, ...] = ()


@dataclass(frozen=True)
class TrailingProfile:
    label: str
    enabled: bool
    activation_pct: Decimal
    distance_pct: Decimal


@dataclass(frozen=True)
class SweepConfig:
    pairs: tuple[str, ...] = DEFAULT_SWEEP_PAIRS
    intervals: tuple[str, ...] = DEFAULT_INTERVALS
    lookback: int = 1000
    starting_equity: Decimal = Decimal("1000")
    leverage: Decimal = Decimal("3")
    margin_currency: str = "INR"
    price_quote_currency: str = "USDT"
    quote_to_margin_rate: Decimal = Decimal("98")
    unit_contract_value: Decimal = Decimal("1")
    risk_per_trade_pct: Decimal | None = None
    compound_risk_equity: bool = True
    stop_loss_pct: Decimal | None = None
    take_profit_pct: Decimal | None = None
    maker_fee_rate: Decimal = COINDCX_INR_M_MAKER_FEE_RATE
    taker_fee_rate: Decimal = COINDCX_INR_M_TAKER_FEE_RATE
    fee_gst_rate: Decimal = COINDCX_FEE_GST_RATE
    entry_fee_type: str = "maker"
    exit_fee_type: str = "taker"
    slippage_pct: Decimal = Decimal("0.02")
    stop_slippage_pct: Decimal | None = Decimal("0.02")
    funding_fee_rate: Decimal = Decimal("0")
    funding_interval_hours: int = 8
    trailing_stop_enabled: bool = False
    trailing_stop_activation_pct: Decimal = Decimal("1")
    trailing_stop_distance_pct: Decimal = Decimal("2")
    atr_dynamic_exits_enabled: bool = False
    atr_stop_enabled: bool = True
    atr_take_profit_enabled: bool = False
    atr_trailing_enabled: bool = True
    atr_entry_filter_enabled: bool = True
    atr_policy_mode: str = "router"
    atr_period: int = 14
    atr_stop_multiple: Decimal = Decimal("1.5")
    atr_take_profit_multiple: Decimal = Decimal("3")
    atr_trailing_multiple: Decimal = Decimal("2.0")
    atr_take_profit_mode: str = "none"
    risk_per_trade_pct_values: tuple[Decimal, ...] = ()
    leverage_values: tuple[Decimal, ...] = ()
    take_profit_pct_values: tuple[Decimal | None, ...] = ()
    trailing_profiles: tuple[TrailingProfile, ...] = ()
    atr_dynamic_exits_values: tuple[bool, ...] = ()
    
    # Task 2: Equity giveback guard
    equity_giveback_guard_enabled: bool = False
    equity_giveback_threshold_pct: Decimal = Decimal("0.035")
    equity_giveback_cooldown_candles: int = 72
    
    # Task 3: Loss-streak cooldown enhancements
    loss_streak_cooldown_enabled: bool = False
    consecutive_loss_limit: int = 3
    loss_streak_cooldown_candles: int = 12
    rolling_loss_window: int = 8
    rolling_loss_limit: int = 5
    rolling_loss_cooldown_candles: int = 36
    
    # Task 4: Post-spike cooldown
    post_spike_cooldown_enabled: bool = False
    post_spike_lookback_candles: int = 50
    post_spike_gain_threshold_pct: Decimal = Decimal("0.05")
    post_spike_cooldown_candles: int = 24
    
    # Task 5: Breakeven and profit-lock
    breakeven_enabled: bool = False
    breakeven_activation_r: Decimal = Decimal("1.0")
    breakeven_offset_r: Decimal = Decimal("0")
    profit_lock_enabled: bool = False
    profit_lock_activation_r: Decimal = Decimal("1.5")
    profit_lock_r: Decimal = Decimal("0.5")
    atr_trail_after_r_enabled: bool = False
    atr_trail_activation_r: Decimal = Decimal("2.0")
    
    # Task 6: Chop/regime filter
    chop_filter_enabled: bool = False
    min_ema_gap_pct: Decimal = Decimal("0.0015")
    min_atr_pct: Decimal = Decimal("0.002")
    block_flat_ema_enabled: bool = False
    block_low_atr_enabled: bool = False

    until: datetime | None = None
    output_dir: Path = Path("research/backtests")
    max_runs: int | None = None


SUMMARY_FIELDS = (
    "completed_at",
    "pair",
    "interval",
    "variant",
    "family",
    "execution_interval",
    "intrabar_reentry_enabled",
    "max_reentries_per_candle",
    "reentry_cooldown_candles",
    "stop_loss_cooldown_candles",
    "max_consecutive_losses",
    "loss_cooldown_candles",
    "candles_loaded",
    "total_return_pct",
    "rank_score",
    "rank_grade",
    "final_equity",
    "trade_count",
    "win_rate_pct",
    "profit_factor",
    "max_drawdown_pct",
    "fees_paid",
    "maker_fee_rate",
    "taker_fee_rate",
    "fee_gst_rate",
    "maker_fee_effective_rate",
    "taker_fee_effective_rate",
    "entry_fee_type",
    "exit_fee_type",
    "leverage",
    "risk_per_trade_pct",
    "compound_risk_equity",
    "stop_loss_pct",
    "take_profit_pct",
    "stop_slippage_pct",
    "funding_fee_rate",
    "funding_interval_hours",
    "funding_paid",
    "ambiguous_exit_count",
    "gap_exit_count",
    "trailing_stop_enabled",
    "trailing_profile",
    "trailing_stop_activation_pct",
    "trailing_stop_distance_pct",
    "atr_dynamic_exits_enabled",
    "atr_stop_enabled",
    "atr_take_profit_enabled",
    "atr_trailing_enabled",
    "atr_entry_filter_enabled",
    "atr_policy_mode",
    "atr_period",
    "atr_stop_multiple",
    "atr_take_profit_multiple",
    "atr_trailing_multiple",
    "atr_take_profit_mode",
    "accepted_reports",
    "rejected_reports",
    "enter_long",
    "enter_short",
    "exit_long",
    "exit_short",
    "hold",
    "error",
)


def default_strategy_variants() -> tuple[StrategyVariant, ...]:
    return (
        StrategyVariant(
            name="hybrid_meta",
            family="hybrid_meta",
            factory=lambda: HybridMetaStrategy(),
        ),
        StrategyVariant(
            name="hybrid_meta_v2",
            family="hybrid_meta",
            factory=lambda: HybridMetaV2Strategy(),
        ),
        StrategyVariant(
            name="adaptive_default",
            family="adaptive_hybrid",
            factory=lambda: AdaptiveHybridStrategy(),
        ),
        StrategyVariant(
            name="ema_default",
            family="ema_rsi_trend",
            factory=lambda: EMARSICrossoverStrategy(),
        ),
        StrategyVariant(
            name="rsi_macd_momentum",
            family="rsi_macd_momentum",
            factory=lambda: RSIMACDMomentumStrategy(),
        ),
        StrategyVariant(
            name="ema_fast",
            family="ema_rsi_trend",
            factory=lambda: EMARSICrossoverStrategy(
                fast_period=7,
                slow_period=18,
                stop_atr_multiple=Decimal("1.35"),
                take_profit_atr_multiple=Decimal("2.7"),
            ),
        ),
        StrategyVariant(
            name="ema_slow",
            family="ema_rsi_trend",
            factory=lambda: EMARSICrossoverStrategy(
                fast_period=12,
                slow_period=26,
                stop_atr_multiple=Decimal("1.6"),
                take_profit_atr_multiple=Decimal("3.2"),
            ),
        ),
        StrategyVariant(
            name="ema_conservative_rsi",
            family="ema_rsi_trend",
            factory=lambda: EMARSICrossoverStrategy(
                long_rsi_min=Decimal("52"),
                long_rsi_max=Decimal("68"),
                short_rsi_min=Decimal("32"),
                short_rsi_max=Decimal("48"),
            ),
        ),
        StrategyVariant(
            name="bb_default",
            family="bb_volume_reversion",
            factory=lambda: BollingerVolumeMeanReversionStrategy(),
        ),
        StrategyVariant(
            name="grid_bb_dynamic",
            family="bb_dynamic_grid",
            factory=lambda: BollingerDynamicFuturesGridStrategy(),
        ),
        StrategyVariant(
            name="adaptive_reverse_exit",
            family="adaptive_hybrid",
            factory=lambda: AdaptiveHybridStrategy(exit_on_opposite_entry=True),
        ),
        StrategyVariant(
            name="bb_lenient_volume",
            family="bb_volume_reversion",
            factory=lambda: BollingerVolumeMeanReversionStrategy(
                squeeze_rank_threshold=Decimal("0.45"),
                volume_multiplier=Decimal("1.05"),
            ),
        ),
        StrategyVariant(
            name="bb_strict_volume",
            family="bb_volume_reversion",
            factory=lambda: BollingerVolumeMeanReversionStrategy(
                squeeze_rank_threshold=Decimal("0.25"),
                volume_multiplier=Decimal("1.25"),
                stop_atr_multiple=Decimal("1.0"),
            ),
        ),
    )


def strategy_variants_for_names(names: Iterable[str]) -> tuple[StrategyVariant, ...]:
    requested = tuple(name.strip().lower() for name in names if name.strip())
    variants_by_name = {variant.name: variant for variant in default_strategy_variants()}
    aliases = {
        "adaptive_hybrid": "adaptive_default",
        "adaptive": "adaptive_default",
        "hybrid": "hybrid_meta",
        "weighted_hybrid_v2": "hybrid_meta_v2",
        "rsi_macd": "rsi_macd_momentum",
    }
    selected: list[StrategyVariant] = []
    for name in requested:
        resolved = aliases.get(name, name)
        variant = variants_by_name.get(resolved)
        if variant is None:
            raise ValueError(
                f"Unknown sweep variant {name}. Choose one of: "
                f"{', '.join(sorted(variants_by_name))}."
            )
        selected.append(variant)
    return tuple(selected)


def parse_until(value: str | None, *, now: datetime | None = None) -> datetime | None:
    if value is None or value.strip() == "":
        return None

    current = now or datetime.now().astimezone()
    text = value.strip()
    try:
        parsed_time = time.fromisoformat(text)
    except ValueError:
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=current.tzinfo)
        return parsed

    target = datetime.combine(current.date(), parsed_time, tzinfo=current.tzinfo)
    if target <= current:
        target += timedelta(days=1)
    return target


def parse_csv_list(value: str | None, default: Iterable[str]) -> tuple[str, ...]:
    if value is None or value.strip() == "":
        return tuple(default)
    return tuple(item.strip() for item in value.split(",") if item.strip())


def parse_decimal_list(value: str | None, default: Iterable[Decimal]) -> tuple[Decimal, ...]:
    if value is None or value.strip() == "":
        return tuple(default)
    return tuple(Decimal(item.strip()) for item in value.split(",") if item.strip())


def parse_optional_decimal_list(
    value: str | None,
    default: Iterable[Decimal | None],
) -> tuple[Decimal | None, ...]:
    if value is None or value.strip() == "":
        return tuple(default)
    parsed: list[Decimal | None] = []
    for item in value.split(","):
        text = item.strip().lower()
        if not text:
            continue
        if text in {"none", "strategy", "default"}:
            parsed.append(None)
        else:
            parsed.append(Decimal(text))
    return tuple(parsed)


def parse_bool_grid(value: str | None, default: Iterable[bool]) -> tuple[bool, ...]:
    if value is None or value.strip() == "":
        return tuple(default)
    parsed: list[bool] = []
    for item in value.split(","):
        text = item.strip().lower()
        if text in {"1", "true", "yes", "y", "on"}:
            parsed.append(True)
        elif text in {"0", "false", "no", "n", "off"}:
            parsed.append(False)
        elif text:
            raise ValueError(f"Invalid boolean grid value: {item}")
    return tuple(parsed)


def parse_trailing_profiles(value: str | None) -> tuple[TrailingProfile, ...]:
    if value is None or value.strip() == "":
        return ()
    profiles: list[TrailingProfile] = []
    for item in value.split(","):
        text = item.strip().lower()
        if not text:
            continue
        if text in {"off", "none", "false", "0"}:
            profiles.append(
                TrailingProfile(
                    label="off",
                    enabled=False,
                    activation_pct=Decimal("0"),
                    distance_pct=Decimal("0"),
                )
            )
            continue
        if ":" not in text:
            raise ValueError(
                "Trailing profiles must be off or activation:distance, e.g. off,2:3,3:5."
            )
        activation, distance = text.split(":", 1)
        activation_pct = Decimal(activation.strip())
        distance_pct = Decimal(distance.strip())
        profiles.append(
            TrailingProfile(
                label=f"{activation_pct}:{distance_pct}",
                enabled=True,
                activation_pct=activation_pct,
                distance_pct=distance_pct,
            )
        )
    return tuple(profiles)


def _parameter_configs(config: SweepConfig) -> Iterable[SweepConfig]:
    risk_values = config.risk_per_trade_pct_values or (config.risk_per_trade_pct,)
    leverage_values = config.leverage_values or (config.leverage,)
    take_profit_values = config.take_profit_pct_values or (config.take_profit_pct,)
    trailing_profiles = config.trailing_profiles or (
        TrailingProfile(
            label=_trailing_profile_label(config),
            enabled=config.trailing_stop_enabled,
            activation_pct=config.trailing_stop_activation_pct,
            distance_pct=config.trailing_stop_distance_pct,
        ),
    )
    atr_values = config.atr_dynamic_exits_values or (config.atr_dynamic_exits_enabled,)

    configs = [
        replace(
            config,
            risk_per_trade_pct=risk_per_trade_pct,
            leverage=leverage,
            take_profit_pct=take_profit_pct,
            trailing_stop_enabled=trailing_profile.enabled,
            trailing_stop_activation_pct=trailing_profile.activation_pct,
            trailing_stop_distance_pct=trailing_profile.distance_pct,
            atr_dynamic_exits_enabled=atr_dynamic_exits_enabled,
        )
        for risk_per_trade_pct in risk_values
        for leverage in leverage_values
        for take_profit_pct in take_profit_values
        for trailing_profile in trailing_profiles
        for atr_dynamic_exits_enabled in atr_values
    ]
    if not configs:
        return

    stride = _coprime_stride(len(configs))
    for index in range(len(configs)):
        yield configs[(index * stride) % len(configs)]


def _market_tasks(
    *,
    variants: Iterable[StrategyVariant],
    pairs: Iterable[str],
    intervals: Iterable[str],
) -> Iterable[tuple[StrategyVariant, str, str]]:
    for variant in variants:
        for pair in pairs:
            for interval in intervals:
                if variant.intervals and interval not in variant.intervals:
                    continue
                yield variant, pair, interval


def _scheduled_runs(
    *,
    parameter_configs: tuple[SweepConfig, ...],
    market_tasks: tuple[tuple[StrategyVariant, str, str], ...],
) -> Iterable[tuple[SweepConfig, StrategyVariant, str, str]]:
    if not parameter_configs or not market_tasks:
        return

    for pass_index in range(len(parameter_configs)):
        for market_index, (variant, pair, interval) in enumerate(market_tasks):
            config_index = (pass_index + market_index) % len(parameter_configs)
            yield parameter_configs[config_index], variant, pair, interval


def _coprime_stride(total: int) -> int:
    for candidate in (997, 389, 193, 97, 53, 37, 17, 11, 7, 5, 3):
        if candidate < total and gcd(candidate, total) == 1:
            return candidate
    for candidate in range(max(total - 1, 1), 0, -1):
        if gcd(candidate, total) == 1:
            return candidate
    return 1


def _trailing_profile_label(config: SweepConfig | BacktestConfig) -> str:
    if not config.trailing_stop_enabled:
        return "off"
    return (
        f"{config.trailing_stop_activation_pct}:"
        f"{config.trailing_stop_distance_pct}"
    )


def run_research_sweep(
    *,
    settings: Settings,
    config: SweepConfig,
    variants: tuple[StrategyVariant, ...] | None = None,
    logger: logging.Logger | None = None,
) -> dict[str, object]:
    log = logger or logging.getLogger(__name__)
    strategy_variants = variants or default_strategy_variants()
    started_at = datetime.now().astimezone()
    output_dir = _prepare_output_dir(config.output_dir, started_at)
    results_path = output_dir / "results.jsonl"
    errors_path = output_dir / "errors.jsonl"
    summary_path = output_dir / "summary.csv"
    workbook_path = output_dir / "backtest_results.xlsx"
    progress_path = output_dir / "progress.json"

    client = CoinDCXFuturesClient(settings)
    parameter_configs = tuple(_parameter_configs(config))
    market_tasks = tuple(
        _market_tasks(
            variants=strategy_variants,
            pairs=config.pairs,
            intervals=config.intervals,
        )
    )
    series_cache: dict[tuple[str, str], object] = {}
    open_interest_cache: dict[tuple[str, str], OpenInterestFeatureSeries] = {}
    instrument_cache: dict[str, InstrumentMetadata] = {}
    completed = 0
    errors = 0
    attempted = 0
    rows: list[dict[str, object]] = []
    stop_reason = "completed"
    assumptions = _assumptions(config, status="running")

    _write_summary(summary_path, rows)
    write_sweep_workbook(workbook_path, rows=rows, assumptions=assumptions)
    _write_progress(
        progress_path,
        {
            "status": "running",
            "started_at": started_at.isoformat(),
            "until": config.until.isoformat() if config.until else None,
            "output_dir": str(output_dir),
            "completed": completed,
            "errors": errors,
            "attempted": attempted,
        },
    )

    failed_series_keys: set[tuple[str, str]] = set()

    for run_config, variant, pair, interval in _scheduled_runs(
        parameter_configs=parameter_configs,
        market_tasks=market_tasks,
    ):
        if _should_stop(config, attempted):
            stop_reason = "cutoff_reached"
            break

        instrument = instrument_cache.get(pair)
        if instrument is None:
            instrument = _instrument_metadata(
                client,
                pair=pair,
                margin_currency=settings.futures_margin_currency,
            )
            instrument_cache[pair] = instrument

        series_key = (pair, interval)
        if series_key in failed_series_keys:
            continue

        try:
            series = _load_series_cached(
                client=client,
                pair=pair,
                interval=interval,
                lookback=config.lookback,
                cache=series_cache,
            )
            if len(series) == 0:  # type: ignore[arg-type]
                raise ValueError("No candles loaded.")
        except Exception as exc:
            failed_series_keys.add(series_key)
            errors += 1
            row = _error_row(
                pair=pair,
                interval=interval,
                variant=variant.name,
                family=variant.family,
                error=exc,
            )
            _append_jsonl(errors_path, row)
            log.warning("Failed %s %s %s: %s", pair, interval, variant.name, exc)
            rows.append(row)
            _write_summary(summary_path, rows)
            _write_running_workbook(workbook_path, rows, config)
            continue

        open_interest_features = _load_open_interest_proxy_cached(
            pair=pair,
            interval=interval,
            series=series,
            variant=variant,
            cache=open_interest_cache,
            logger=log,
        )
        attempted += 1
        risk_per_trade_pct = (
            run_config.risk_per_trade_pct
            or settings.risk.max_risk_per_trade_pct
        )
        risk_manager = RiskManager(
            replace(
                settings.risk,
                max_risk_per_trade_pct=risk_per_trade_pct,
            )
        )
        try:
            result = _run_one(
                pair=pair,
                interval=interval,
                variant=variant,
                series=series,
                config=run_config,
                risk_manager=risk_manager,
                instrument=instrument,
                open_interest_features=open_interest_features,
            )
        except Exception as exc:
            errors += 1
            row = _error_row(
                pair=pair,
                interval=interval,
                variant=variant.name,
                family=variant.family,
                error=exc,
            )
            _append_jsonl(errors_path, row)
            log.warning(
                "Failed %s %s %s: %s",
                pair,
                interval,
                variant.name,
                exc,
            )
        else:
            completed += 1
            row = _summary_row(result, variant)
            _append_jsonl(results_path, result.to_dict(recent_count=0))
            log.info(
                "Completed %s %s %s return=%s%% trades=%s",
                pair,
                interval,
                variant.name,
                row["total_return_pct"],
                row["trade_count"],
            )
        rows.append(row)
        _write_summary(summary_path, rows)
        _write_running_workbook(workbook_path, rows, config)
        _write_progress(
            progress_path,
            {
                "status": "running",
                "started_at": started_at.isoformat(),
                "until": config.until.isoformat() if config.until else None,
                "output_dir": str(output_dir),
                "completed": completed,
                "errors": errors,
                "attempted": attempted,
                "last_pair": pair,
                "last_interval": interval,
                "last_variant": variant.name,
                "last_risk_per_trade_pct": run_config.risk_per_trade_pct,
                "last_leverage": run_config.leverage,
                "last_take_profit_pct": run_config.take_profit_pct,
                "last_trailing_profile": _trailing_profile_label(run_config),
                "last_atr_dynamic_exits_enabled": run_config.atr_dynamic_exits_enabled,
            },
        )

    ended_at = datetime.now().astimezone()
    _write_progress(
        progress_path,
        {
            "status": "finished",
            "stop_reason": stop_reason,
            "started_at": started_at.isoformat(),
            "ended_at": ended_at.isoformat(),
            "until": config.until.isoformat() if config.until else None,
            "output_dir": str(output_dir),
            "completed": completed,
            "errors": errors,
            "attempted": attempted,
            "top_results": _top_rows(rows, limit=10),
        },
    )
    write_sweep_workbook(
        workbook_path,
        rows=rows,
        assumptions=_assumptions(config, status="finished"),
    )

    return {
        "status": "finished",
        "stop_reason": stop_reason,
        "output_dir": str(output_dir),
        "summary_csv": str(summary_path),
        "workbook_xlsx": str(workbook_path),
        "results_jsonl": str(results_path),
        "errors_jsonl": str(errors_path),
        "progress_json": str(progress_path),
        "completed": completed,
        "errors": errors,
        "attempted": attempted,
        "top_results": _top_rows(rows, limit=10),
    }


def _run_one(
    *,
    pair: str,
    interval: str,
    variant: StrategyVariant,
    series: object,
    config: SweepConfig,
    risk_manager: RiskManager,
    instrument: InstrumentMetadata,
    open_interest_features: OpenInterestFeatureSeries | None = None,
) -> BacktestResult:
    backtest_config = BacktestConfig(
        pair=pair,
        interval=interval,
        starting_equity=config.starting_equity,
        leverage=config.leverage,
        strategy_name=variant.name,
        margin_currency=config.margin_currency,
        price_quote_currency=config.price_quote_currency,
        quote_to_margin_rate=config.quote_to_margin_rate,
        unit_contract_value=config.unit_contract_value,
        risk_per_trade_pct=config.risk_per_trade_pct,
        compound_risk_equity=config.compound_risk_equity,
        stop_loss_pct=config.stop_loss_pct,
        take_profit_pct=config.take_profit_pct,
        taker_fee_rate=config.taker_fee_rate,
        maker_fee_rate=config.maker_fee_rate,
        fee_gst_rate=config.fee_gst_rate,
        entry_fee_type=config.entry_fee_type,
        exit_fee_type=config.exit_fee_type,
        slippage_pct=config.slippage_pct,
        stop_slippage_pct=config.stop_slippage_pct,
        funding_fee_rate=config.funding_fee_rate,
        funding_interval_hours=config.funding_interval_hours,
        trailing_stop_enabled=config.trailing_stop_enabled,
        trailing_stop_activation_pct=config.trailing_stop_activation_pct,
        trailing_stop_distance_pct=config.trailing_stop_distance_pct,
        atr_dynamic_exits_enabled=config.atr_dynamic_exits_enabled,
        atr_stop_enabled=config.atr_stop_enabled,
        atr_take_profit_enabled=config.atr_take_profit_enabled,
        atr_trailing_enabled=config.atr_trailing_enabled,
        atr_entry_filter_enabled=config.atr_entry_filter_enabled,
        atr_policy_mode=config.atr_policy_mode,
        atr_period=config.atr_period,
        atr_stop_multiple=config.atr_stop_multiple,
        atr_take_profit_multiple=config.atr_take_profit_multiple,
        atr_trailing_multiple=config.atr_trailing_multiple,
        atr_take_profit_mode=config.atr_take_profit_mode,
        equity_giveback_guard_enabled=config.equity_giveback_guard_enabled,
        equity_giveback_threshold_pct=config.equity_giveback_threshold_pct,
        equity_giveback_cooldown_candles=config.equity_giveback_cooldown_candles,
        loss_streak_cooldown_enabled=config.loss_streak_cooldown_enabled,
        consecutive_loss_limit=config.consecutive_loss_limit,
        loss_streak_cooldown_candles=config.loss_streak_cooldown_candles,
        rolling_loss_window=config.rolling_loss_window,
        rolling_loss_limit=config.rolling_loss_limit,
        rolling_loss_cooldown_candles=config.rolling_loss_cooldown_candles,
        post_spike_cooldown_enabled=config.post_spike_cooldown_enabled,
        post_spike_lookback_candles=config.post_spike_lookback_candles,
        post_spike_gain_threshold_pct=config.post_spike_gain_threshold_pct,
        post_spike_cooldown_candles=config.post_spike_cooldown_candles,
        breakeven_enabled=config.breakeven_enabled,
        breakeven_activation_r=config.breakeven_activation_r,
        breakeven_offset_r=config.breakeven_offset_r,
        profit_lock_enabled=config.profit_lock_enabled,
        profit_lock_activation_r=config.profit_lock_activation_r,
        profit_lock_r=config.profit_lock_r,
        atr_trail_after_r_enabled=config.atr_trail_after_r_enabled,
        atr_trail_activation_r=config.atr_trail_activation_r,
        chop_filter_enabled=config.chop_filter_enabled,
        min_ema_gap_pct=config.min_ema_gap_pct,
        min_atr_pct=config.min_atr_pct,
        block_flat_ema_enabled=config.block_flat_ema_enabled,
        block_low_atr_enabled=config.block_low_atr_enabled,
    )
    engine = BacktestEngine(
        config=backtest_config,
        strategy_engine=StrategyEngine([variant.factory()]),
        risk_manager=risk_manager,
        instrument=instrument,
        open_interest_features=open_interest_features,
    )
    return engine.run(series)  # type: ignore[arg-type]


def _load_open_interest_proxy_cached(
    *,
    pair: str,
    interval: str,
    series: object,
    variant: StrategyVariant,
    cache: dict[tuple[str, str], OpenInterestFeatureSeries],
    logger: logging.Logger,
) -> OpenInterestFeatureSeries:
    if variant.family not in OPEN_INTEREST_FAMILIES:
        return OpenInterestFeatureSeries([])
    key = (pair, interval)
    if key in cache:
        return cache[key]
    try:
        cache[key] = load_binance_open_interest_proxy(
            pair=pair,
            interval=interval,
            candles=series,  # type: ignore[arg-type]
        )
    except Exception as exc:
        logger.warning(
            "Binance open-interest proxy unavailable for %s %s: %s",
            pair,
            interval,
            exc,
        )
        cache[key] = OpenInterestFeatureSeries([])
    return cache[key]


def _load_series_cached(
    *,
    client: CoinDCXFuturesClient,
    pair: str,
    interval: str,
    lookback: int,
    cache: dict[tuple[str, str], object],
) -> object:
    key = (pair, interval)
    if key not in cache:
        cache[key] = load_historical_candle_series(
            client=client,
            pair=pair,
            interval=interval,
            lookback=lookback,
        )
    return cache[key]


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


def _prepare_output_dir(base_dir: Path, started_at: datetime) -> Path:
    run_dir = base_dir / started_at.strftime("sweep_%Y%m%d_%H%M%S")
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir


def _assumptions(config: SweepConfig, *, status: str) -> dict[str, object]:
    return {
        "status": status,
        "pairs": ", ".join(config.pairs),
        "intervals": ", ".join(config.intervals),
        "lookback": config.lookback,
        "starting_equity": config.starting_equity,
        "leverage": config.leverage,
        "risk_per_trade_pct": config.risk_per_trade_pct,
        "compound_risk_equity": config.compound_risk_equity,
        "stop_loss_pct": config.stop_loss_pct,
        "take_profit_pct": config.take_profit_pct,
        "taker_fee_rate": config.taker_fee_rate,
        "maker_fee_rate": config.maker_fee_rate,
        "fee_gst_rate": config.fee_gst_rate,
        "maker_fee_effective_rate": effective_fee_rate(
            config.maker_fee_rate, config.fee_gst_rate
        ),
        "taker_fee_effective_rate": effective_fee_rate(
            config.taker_fee_rate, config.fee_gst_rate
        ),
        "entry_fee_type": config.entry_fee_type,
        "exit_fee_type": config.exit_fee_type,
        "slippage_pct": config.slippage_pct,
        "stop_slippage_pct": config.stop_slippage_pct,
        "funding_fee_rate": config.funding_fee_rate,
        "funding_interval_hours": config.funding_interval_hours,
        "trailing_stop_enabled": config.trailing_stop_enabled,
        "trailing_stop_activation_pct": config.trailing_stop_activation_pct,
        "trailing_stop_distance_pct": config.trailing_stop_distance_pct,
        "atr_dynamic_exits_enabled": config.atr_dynamic_exits_enabled,
        "atr_stop_enabled": config.atr_stop_enabled,
        "atr_take_profit_enabled": config.atr_take_profit_enabled,
        "atr_trailing_enabled": config.atr_trailing_enabled,
        "atr_entry_filter_enabled": config.atr_entry_filter_enabled,
        "atr_policy_mode": config.atr_policy_mode,
        "atr_period": config.atr_period,
        "atr_stop_multiple": config.atr_stop_multiple,
        "atr_take_profit_multiple": config.atr_take_profit_multiple,
        "atr_trailing_multiple": config.atr_trailing_multiple,
        "atr_take_profit_mode": config.atr_take_profit_mode,
        "risk_grid": _grid_label(config.risk_per_trade_pct_values),
        "leverage_grid": _grid_label(config.leverage_values),
        "take_profit_grid": _grid_label(config.take_profit_pct_values),
        "trailing_grid": ", ".join(profile.label for profile in config.trailing_profiles),
        "atr_grid": _grid_label(config.atr_dynamic_exits_values),
        "parameter_run_count": len(tuple(_parameter_configs(config))),
        "until": config.until.isoformat() if config.until else "",
        "notes": "Paper-only historical research sweep. Results are not live-trading approval.",
    }


def _grid_label(values: Iterable[object]) -> str:
    return ", ".join("" if value is None else str(value) for value in values)


def _rank_score(metrics: object) -> Decimal:
    total_return_pct = _decimal_value(getattr(metrics, "total_return_pct", None))
    max_drawdown_pct = _decimal_value(getattr(metrics, "max_drawdown_pct", None))
    profit_factor = _decimal_value(getattr(metrics, "profit_factor", None))
    trade_count = _decimal_value(getattr(metrics, "trade_count", None))

    if trade_count <= 0:
        return Decimal("-999999")

    capped_profit_factor = min(max(profit_factor, Decimal("0")), Decimal("5"))
    capped_trade_bonus = min(trade_count, Decimal("50")) * Decimal("0.05")
    return (
        total_return_pct
        - (max_drawdown_pct * Decimal("1.25"))
        + (capped_profit_factor * Decimal("2"))
        + capped_trade_bonus
    )


def _rank_grade(metrics: object) -> str:
    total_return_pct = _decimal_value(getattr(metrics, "total_return_pct", None))
    max_drawdown_pct = _decimal_value(getattr(metrics, "max_drawdown_pct", None))
    profit_factor = _decimal_value(getattr(metrics, "profit_factor", None))
    trade_count = _decimal_value(getattr(metrics, "trade_count", None))

    if trade_count < 3:
        return "too_few_trades"
    if total_return_pct <= 0:
        return "reject"
    if profit_factor >= Decimal("1.5") and max_drawdown_pct <= Decimal("15"):
        return "A"
    if profit_factor >= Decimal("1.1") and max_drawdown_pct <= Decimal("25"):
        return "B"
    return "watchlist"


def _decimal_value(value: object) -> Decimal:
    if value is None:
        return Decimal("0")
    try:
        return Decimal(str(value))
    except Exception:
        return Decimal("0")


def _summary_row(result: BacktestResult, variant: StrategyVariant) -> dict[str, object]:
    summary = result.to_dict(recent_count=0)
    metrics = result.metrics
    actions = summary["diagnostics"]["signal_action_counts"]
    diagnostics = summary["diagnostics"]
    return {
        "completed_at": datetime.now().astimezone().isoformat(),
        "pair": result.config.pair,
        "interval": result.config.interval,
        "variant": variant.name,
        "family": variant.family,
        "execution_interval": result.config.execution_interval,
        "intrabar_reentry_enabled": result.config.intrabar_reentry_enabled,
        "max_reentries_per_candle": result.config.max_reentries_per_candle,
        "reentry_cooldown_candles": result.config.reentry_cooldown_candles,
        "stop_loss_cooldown_candles": result.config.stop_loss_cooldown_candles,
        "max_consecutive_losses": result.config.max_consecutive_losses,
        "loss_cooldown_candles": result.config.loss_cooldown_candles,
        "candles_loaded": result.candles_loaded,
        "total_return_pct": metrics.total_return_pct,
        "rank_score": _rank_score(metrics),
        "rank_grade": _rank_grade(metrics),
        "final_equity": metrics.final_equity,
        "trade_count": metrics.trade_count,
        "win_rate_pct": metrics.win_rate_pct,
        "profit_factor": metrics.profit_factor,
        "max_drawdown_pct": metrics.max_drawdown_pct,
        "fees_paid": metrics.fees_paid,
        "funding_paid": metrics.funding_paid,
        "maker_fee_rate": result.config.maker_fee_rate,
        "taker_fee_rate": result.config.taker_fee_rate,
        "fee_gst_rate": result.config.fee_gst_rate,
        "maker_fee_effective_rate": effective_fee_rate(
            result.config.maker_fee_rate, result.config.fee_gst_rate
        ),
        "taker_fee_effective_rate": effective_fee_rate(
            result.config.taker_fee_rate, result.config.fee_gst_rate
        ),
        "entry_fee_type": result.config.entry_fee_type,
        "exit_fee_type": result.config.exit_fee_type,
        "leverage": result.config.leverage,
        "risk_per_trade_pct": result.config.risk_per_trade_pct,
        "compound_risk_equity": result.config.compound_risk_equity,
        "stop_loss_pct": result.config.stop_loss_pct,
        "take_profit_pct": result.config.take_profit_pct,
        "stop_slippage_pct": result.config.stop_slippage_pct,
        "funding_fee_rate": result.config.funding_fee_rate,
        "funding_interval_hours": result.config.funding_interval_hours,
        "ambiguous_exit_count": diagnostics.get("ambiguous_exit_count", 0),
        "gap_exit_count": diagnostics.get("gap_exit_count", 0),
        "trailing_stop_enabled": result.config.trailing_stop_enabled,
        "trailing_profile": _trailing_profile_label(result.config),
        "trailing_stop_activation_pct": result.config.trailing_stop_activation_pct,
        "trailing_stop_distance_pct": result.config.trailing_stop_distance_pct,
        "atr_dynamic_exits_enabled": result.config.atr_dynamic_exits_enabled,
        "atr_stop_enabled": result.config.atr_stop_enabled,
        "atr_take_profit_enabled": result.config.atr_take_profit_enabled,
        "atr_trailing_enabled": result.config.atr_trailing_enabled,
        "atr_entry_filter_enabled": result.config.atr_entry_filter_enabled,
        "atr_policy_mode": result.config.atr_policy_mode,
        "atr_period": result.config.atr_period,
        "atr_stop_multiple": result.config.atr_stop_multiple,
        "atr_take_profit_multiple": result.config.atr_take_profit_multiple,
        "atr_trailing_multiple": result.config.atr_trailing_multiple,
        "atr_take_profit_mode": result.config.atr_take_profit_mode,
        "accepted_reports": summary["accepted_report_count"],
        "rejected_reports": summary["rejected_report_count"],
        "enter_long": actions.get("enter_long", 0),
        "enter_short": actions.get("enter_short", 0),
        "exit_long": actions.get("exit_long", 0),
        "exit_short": actions.get("exit_short", 0),
        "hold": actions.get("hold", 0),
        "error": "",
    }


def _error_row(
    *,
    pair: str,
    interval: str,
    variant: str,
    family: str,
    error: Exception,
) -> dict[str, object]:
    return {
        "completed_at": datetime.now().astimezone().isoformat(),
        "pair": pair,
        "interval": interval,
        "variant": variant,
        "family": family,
        "execution_interval": "",
        "intrabar_reentry_enabled": "",
        "max_reentries_per_candle": "",
        "reentry_cooldown_candles": "",
        "stop_loss_cooldown_candles": "",
        "max_consecutive_losses": "",
        "loss_cooldown_candles": "",
        "candles_loaded": 0,
        "total_return_pct": "",
        "rank_score": "",
        "rank_grade": "",
        "final_equity": "",
        "trade_count": "",
        "win_rate_pct": "",
        "profit_factor": "",
        "max_drawdown_pct": "",
        "fees_paid": "",
        "maker_fee_rate": "",
        "taker_fee_rate": "",
        "fee_gst_rate": "",
        "maker_fee_effective_rate": "",
        "taker_fee_effective_rate": "",
        "entry_fee_type": "",
        "exit_fee_type": "",
        "leverage": "",
        "risk_per_trade_pct": "",
        "compound_risk_equity": "",
        "stop_loss_pct": "",
        "take_profit_pct": "",
        "stop_slippage_pct": "",
        "funding_fee_rate": "",
        "funding_interval_hours": "",
        "funding_paid": "",
        "ambiguous_exit_count": "",
        "gap_exit_count": "",
        "trailing_stop_enabled": "",
        "trailing_profile": "",
        "trailing_stop_activation_pct": "",
        "trailing_stop_distance_pct": "",
        "atr_dynamic_exits_enabled": "",
        "atr_stop_enabled": "",
        "atr_take_profit_enabled": "",
        "atr_trailing_enabled": "",
        "atr_entry_filter_enabled": "",
        "atr_policy_mode": "",
        "atr_period": "",
        "atr_stop_multiple": "",
        "atr_take_profit_multiple": "",
        "atr_trailing_multiple": "",
        "atr_take_profit_mode": "",
        "accepted_reports": "",
        "rejected_reports": "",
        "enter_long": "",
        "enter_short": "",
        "exit_long": "",
        "exit_short": "",
        "hold": "",
        "error": f"{type(error).__name__}: {error}",
    }


def _write_summary(path: Path, rows: list[dict[str, object]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=SUMMARY_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow(convert_for_json(row))


def _write_running_workbook(
    path: Path,
    rows: list[dict[str, object]],
    config: SweepConfig,
) -> None:
    if len(rows) <= 1 or len(rows) % 25 == 0:
        write_sweep_workbook(
            path,
            rows=rows,
            assumptions=_assumptions(config, status="running"),
        )


def _append_jsonl(path: Path, value: object) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(convert_for_json(value), sort_keys=True) + "\n")


def _write_progress(path: Path, value: dict[str, object]) -> None:
    path.write_text(
        json.dumps(convert_for_json(value), indent=2, sort_keys=True),
        encoding="utf-8",
    )


def _should_stop(config: SweepConfig, attempted: int) -> bool:
    if config.max_runs is not None and attempted >= config.max_runs:
        return True
    if config.until is not None and datetime.now().astimezone() >= config.until:
        return True
    return False


def _top_rows(rows: list[dict[str, object]], *, limit: int) -> list[dict[str, object]]:
    valid = [row for row in rows if row.get("error") == "" and row.get("total_return_pct") != ""]

    def key(row: dict[str, object]) -> Decimal:
        value = row.get("rank_score")
        if value not in {"", None}:
            return Decimal(str(value))
        return Decimal(str(row["total_return_pct"]))

    valid.sort(key=key, reverse=True)
    return valid[:limit]
