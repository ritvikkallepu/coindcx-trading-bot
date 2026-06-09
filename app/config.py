from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Mapping


def _read_dotenv(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}

    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _get(env: Mapping[str, str], key: str, default: str) -> str:
    value = env.get(key)
    if value is None or value == "":
        return default
    return str(value).strip()


def _bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _decimal(value: str) -> Decimal:
    return Decimal(value.strip())


def _redact(secret: str | None) -> str:
    if not secret:
        return ""
    if len(secret) <= 8:
        return "***"
    return f"{secret[:4]}...{secret[-4:]}"


def parse_csv_list(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


# These are conservative paper-trading defaults.
# They should be overridden via .env for live trading.
@dataclass(frozen=True)
class RiskSettings:
    max_risk_per_trade_pct: Decimal = Decimal("25")
    max_daily_loss_pct: Decimal = Decimal("10")
    max_open_positions: int = 2
    max_open_positions_per_pair: int = 1
    allow_multi_pair_positions: bool = True
    allow_same_pair_pyramiding: bool = False
    max_leverage: int = 125
    max_total_open_notional_pct: Decimal = Decimal("0")
    max_total_risk_pct: Decimal = Decimal("50")
    max_margin_usage_pct: Decimal = Decimal("100.0")
    max_margin_per_trade_pct: Decimal = Decimal("0")
    max_margin_per_pair: Decimal = Decimal("0")
    liquidation_buffer_pct: Decimal = Decimal("2")
    entry_safety_enabled: bool = True
    min_stop_distance_pct: Decimal = Decimal("0.50")
    min_entry_atr_pct: Decimal = Decimal("0.05")
    min_stop_atr_multiple: Decimal = Decimal("0.75")
    min_entry_volume_ratio: Decimal = Decimal("0.50")
    max_entry_spread_pct: Decimal = Decimal("0.30")
    min_entry_side_depth_margin: Decimal = Decimal("0")
    
    # Live safety caps
    # LIVE_RISK_APPROVAL_ENABLED MUST be set to true in .env for live entries to be approved.
    # It defaults to false as a safety guard to prevent naked entries before caps are tuned.
    live_risk_approval_enabled: bool = False
    
    live_max_order_notional: Decimal = Decimal("5000")
    live_max_margin_per_order: Decimal = Decimal("1000")
    live_max_daily_loss_inr: Decimal = Decimal("1000")
    live_max_orders_per_day: int = 99999
    live_min_confidence: Decimal = Decimal("0.60")
    live_require_stop_loss: bool = True
    live_require_exchange_stop_sync: bool = True
    live_kill_switch: bool = False
    
    # live_close_on_kill_switch defaults to False. If a kill switch fires (e.g., max daily loss),
    # new entries are blocked but existing positions REMAIN OPEN and could bleed.
    # Recommended: set to True in .env unless you intend to manage exits manually.
    live_close_on_kill_switch: bool = False
    
    live_cancel_orders_on_kill_switch: bool = True
    live_min_24h_volume_usdt: Decimal = Decimal("1000000")
    live_max_spread_pct: Decimal = Decimal("0.30")
    live_max_auto_pairs: int = 10
    live_auto_pair_refresh_seconds: int = 3600
    live_allow_all_pairs_for_real_trading: bool = False
    
    # Short Strictness
    short_strictness_enabled: bool = False
    short_confidence_bonus: Decimal = Decimal("0.05")
    short_min_agreement_bonus: Decimal = Decimal("0.05")
    short_require_trend_confirmation: bool = True
    short_require_price_below_ema: bool = True
    short_require_bearish_structure: bool = False
    short_require_volume_confirmation: bool = False
    
    pair_loss_throttle_enabled: bool = True
    pair_loss_lookback: int = 4
    pair_loss_limit: int = 2
    pair_loss_risk_multiplier: Decimal = Decimal("0.50")
    pair_loss_severe_limit: int = 3
    pair_loss_severe_risk_multiplier: Decimal = Decimal("0.25")
    trailing_stop_enabled: bool = True
    trailing_stop_activation_pct: Decimal = Decimal("1")
    trailing_stop_distance_pct: Decimal = Decimal("2")
    # Fees and Slippage
    maker_fee_rate: Decimal = Decimal("0.0002")
    taker_fee_rate: Decimal = Decimal("0.0005")
    fee_gst_rate: Decimal = Decimal("0.18")
    slippage_pct: Decimal = Decimal("0.05")
    stop_slippage_pct: Decimal = Decimal("0.1")
    # ATR Policy
    atr_stop_enabled: bool = False
    atr_take_profit_enabled: bool = False
    atr_trailing_enabled: bool = False
    atr_take_profit_mode: str = "fixed"
    atr_stop_multiple: Decimal = Decimal("1.5")
    atr_take_profit_multiple: Decimal = Decimal("2.4")
    atr_trailing_multiple: Decimal = Decimal("1.2")
    # Bollinger Band hybrid trail
    bb_trail_enabled: bool = False
    bb_trail_buffer_multiplier: Decimal = Decimal("1.0")
    bb_trail_activation_r: Decimal = Decimal("0.5")
    bb_trail_stage2_r: Decimal = Decimal("0.5")
    bb_trail_stage3_r: Decimal = Decimal("1.0")
    bb_trail_force_close_r: Decimal = Decimal("4.0")
    bb_trail_partial_close_at_tp: bool = True
    bb_trail_partial_close_pct: Decimal = Decimal("0.60")
    bb_trail_observe_only: bool = True
    # Management
    compound_profits: bool = True
    breakeven_enabled: bool = False
    breakeven_activation_r: Decimal = Decimal("1.0")
    breakeven_offset_r: Decimal = Decimal("0")
    profit_lock_enabled: bool = False
    profit_lock_activation_r: Decimal = Decimal("1.2")
    profit_lock_r: Decimal = Decimal("0.25")
    atr_trail_after_r_enabled: bool = False
    atr_trail_activation_r: Decimal = Decimal("2.0")
    # Entry Timing and Quality
    balanced_breakout_enabled: bool = True
    balanced_breakout_volume_ratio_min: Decimal = Decimal("1.8")
    balanced_breakout_body_ratio_min: Decimal = Decimal("0.60")
    balanced_breakout_close_position_min: Decimal = Decimal("0.70")
    balanced_breakout_max_extension_atr: Decimal = Decimal("2.0")
    balanced_breakout_max_age_candles: int = 2
    balanced_breakout_risk_multiplier: Decimal = Decimal("0.50")
    false_breakout_filter_enabled: bool = True
    false_breakout_max_wick_ratio: Decimal = Decimal("0.45")
    false_breakout_require_close_outside_parent: bool = True
    late_chase_block_enabled: bool = True
    late_chase_max_consecutive_impulse_candles: int = 3
    late_chase_volume_fade_ratio: Decimal = Decimal("0.75")
    late_chase_max_extension_atr: Decimal = Decimal("2.2")
    pullback_entry_enabled: bool = True
    pullback_max_age_candles: int = 8
    pullback_max_distance_from_ema_atr: Decimal = Decimal("0.6")
    pullback_resume_body_ratio_min: Decimal = Decimal("0.45")
    pullback_risk_multiplier: Decimal = Decimal("0.50")
    signal_flip_grace_candles: int = 2
    signal_flip_confirm_candles: int = 2
    time_stop_extend_if_momentum_strong: bool = True


@dataclass(frozen=True)
class Settings:
    trading_mode: str = "paper"
    live_trading_enabled: bool = False
    coindcx_api_key: str = ""
    coindcx_api_secret: str = ""
    coindcx_api_base_url: str = "https://api.coindcx.com"
    coindcx_public_base_url: str = "https://public.coindcx.com"
    coindcx_ws_url: str = "wss://stream.coindcx.com"
    futures_margin_currency: str = "INR"
    price_quote_currency: str = "USDT"
    quote_to_margin_rate: Decimal = Decimal("102")
    paper_starting_equity: Decimal = Decimal("100000")
    paper_starting_equity_currency: str = "INR"
    paper_leverage: Decimal = Decimal("1")
    paper_intrabar_enabled: bool = False
    live_confirm_i_understand_risk: str = "NO"
    live_pilot_dry_run: bool = True
    live_max_signal_age_seconds: int = 60
    live_position_margin_type: str = "isolated"
    live_allowed_pairs: list[str] = field(default_factory=lambda: ["B-BTC_USDT"])
    live_blocked_pairs: list[str] = field(default_factory=list)
    live_allowed_strategies: list[str] = field(
        default_factory=lambda: ["hybrid_meta_v2", "rsi_macd_momentum"]
    )
    live_reconcile_seconds: int = 30
    live_rest_poll_seconds: int = 10
    live_closed_candle_buffer_ms: int = 2000
    audit_max_file_mb: int = 100 # Applies only to the paper intrabar diagnostic audit file, not real trade records
    strategy_interval: str = "15m"
    execution_interval: str = "1m"
    use_partial_parent_candle: bool = False
    max_entries_per_parent_candle: int = 1
    enter_on_execution_close: bool = True
    default_pair: str = "B-BTC_USDT"
    http_timeout_seconds: float = 15.0
    api_max_retries: int = 3
    api_retry_base_delay_seconds: float = 0.5
    api_rate_limit_per_second: float = 4.0
    http_user_agent: str = "CoinDCXFuturesBot/0.1"
    ws_ping_interval_seconds: float = 25.0
    log_level: str = "INFO"
    log_dir: str = "logs"
    risk: RiskSettings = RiskSettings()

    @property
    def is_paper_trading(self) -> bool:
        return not self.live_trading_allowed

    @property
    def live_trading_allowed(self) -> bool:
        return self.trading_mode.lower() == "live" and self.live_trading_enabled

    def require_private_credentials(self) -> None:
        if not self.coindcx_api_key or not self.coindcx_api_secret:
            raise ValueError(
                "COINDCX_API_KEY and COINDCX_API_SECRET are required for private API calls."
            )

    def safe_dict(self) -> dict[str, object]:
        data = asdict(self)
        data["coindcx_api_key"] = _redact(self.coindcx_api_key)
        data["coindcx_api_secret"] = _redact(self.coindcx_api_secret)
        return data


def load_settings(env_file: str | Path = ".env") -> Settings:
    dotenv_values = _read_dotenv(Path(env_file))
    
    # Check for personal secret overrides
    secret_file = Path(".env.local.secret")
    if secret_file.exists() and str(env_file) == ".env":
        secret_values = _read_dotenv(secret_file)
        dotenv_values = {**dotenv_values, **secret_values}
        
    merged = {**dotenv_values, **os.environ}

    risk = RiskSettings(
        max_risk_per_trade_pct=_decimal(_get(merged, "MAX_RISK_PER_TRADE_PCT", "25")),
        max_daily_loss_pct=_decimal(_get(merged, "MAX_DAILY_LOSS_PCT", "10")),
        max_open_positions=int(_get(merged, "MAX_OPEN_POSITIONS", "2")),
        max_open_positions_per_pair=int(_get(merged, "MAX_OPEN_POSITIONS_PER_PAIR", "1")),
        allow_multi_pair_positions=_bool(_get(merged, "ALLOW_MULTI_PAIR_POSITIONS", "true")),
        allow_same_pair_pyramiding=_bool(_get(merged, "ALLOW_SAME_PAIR_PYRAMIDING", "false")),
        max_leverage=int(_get(merged, "MAX_LEVERAGE", "125")),
        max_total_open_notional_pct=_decimal(
            _get(merged, "MAX_TOTAL_OPEN_NOTIONAL_PCT", "0")
        ),
        max_total_risk_pct=_decimal(_get(merged, "MAX_TOTAL_RISK_PCT", "50")),
        max_margin_usage_pct=_decimal(_get(merged, "MAX_MARGIN_USAGE_PCT", "100.0")),
        max_margin_per_trade_pct=_decimal(
            _get(merged, "MAX_MARGIN_PER_TRADE_PCT", "0")
        ),
        max_margin_per_pair=_decimal(_get(merged, "MAX_MARGIN_PER_PAIR", "0")),
        liquidation_buffer_pct=_decimal(_get(merged, "LIQUIDATION_BUFFER_PCT", "2")),
        entry_safety_enabled=_bool(_get(merged, "ENTRY_SAFETY_ENABLED", "true")),
        min_stop_distance_pct=_decimal(_get(merged, "MIN_STOP_DISTANCE_PCT", "0.50")),
        min_entry_atr_pct=_decimal(_get(merged, "MIN_ENTRY_ATR_PCT", "0.05")),
        min_stop_atr_multiple=_decimal(_get(merged, "MIN_STOP_ATR_MULTIPLE", "0.75")),
        min_entry_volume_ratio=_decimal(_get(merged, "MIN_ENTRY_VOLUME_RATIO", "0.50")),
        max_entry_spread_pct=_decimal(_get(merged, "MAX_ENTRY_SPREAD_PCT", "0.30")),
        min_entry_side_depth_margin=_decimal(
            _get(merged, "MIN_ENTRY_SIDE_DEPTH_MARGIN", "0")
        ),
        live_risk_approval_enabled=_bool(
            _get(merged, "LIVE_RISK_APPROVAL_ENABLED", "false")
        ),
        live_max_order_notional=_decimal(
            _get(merged, "LIVE_MAX_ORDER_NOTIONAL", "5000")
        ),
        live_max_margin_per_order=_decimal(
            _get(merged, "LIVE_MAX_MARGIN_PER_ORDER", "1000")
        ),
        live_max_daily_loss_inr=_decimal(
            _get(merged, "LIVE_MAX_DAILY_LOSS_INR", "1000")
        ),
        live_max_orders_per_day=int(_get(merged, "LIVE_MAX_ORDERS_PER_DAY", "99999")),
        live_min_confidence=_decimal(_get(merged, "LIVE_MIN_CONFIDENCE", "0.60")),
        live_require_stop_loss=_bool(_get(merged, "LIVE_REQUIRE_STOP_LOSS", "true")),
        live_require_exchange_stop_sync=_bool(
            _get(merged, "LIVE_REQUIRE_EXCHANGE_STOP_SYNC", "true")
        ),
        live_kill_switch=_bool(_get(merged, "LIVE_KILL_SWITCH", "false")),
        live_close_on_kill_switch=_bool(
            _get(merged, "LIVE_CLOSE_ON_KILL_SWITCH", "false")
        ),
        live_cancel_orders_on_kill_switch=_bool(
            _get(merged, "LIVE_CANCEL_ORDERS_ON_KILL_SWITCH", "true")
        ),
        live_min_24h_volume_usdt=_decimal(_get(merged, "LIVE_MIN_24H_VOLUME_USDT", "1000000")),
        live_max_spread_pct=_decimal(_get(merged, "LIVE_MAX_SPREAD_PCT", "0.30")),
        live_max_auto_pairs=int(_get(merged, "LIVE_MAX_AUTO_PAIRS", "10")),
        live_auto_pair_refresh_seconds=int(_get(merged, "LIVE_AUTO_PAIR_REFRESH_SECONDS", "3600")),
        live_allow_all_pairs_for_real_trading=_bool(
            _get(merged, "LIVE_ALLOW_ALL_PAIRS_FOR_REAL_TRADING", "false")
        ),
        short_strictness_enabled=_bool(_get(merged, "SHORT_STRICTNESS_ENABLED", "true")),
        short_confidence_bonus=_decimal(_get(merged, "SHORT_CONFIDENCE_BONUS", "0.05")),
        short_min_agreement_bonus=_decimal(_get(merged, "SHORT_MIN_AGREEMENT_BONUS", "0.05")),
        short_require_trend_confirmation=_bool(_get(merged, "SHORT_REQUIRE_TREND_CONFIRMATION", "true")),
        short_require_price_below_ema=_bool(_get(merged, "SHORT_REQUIRE_PRICE_BELOW_EMA", "true")),
        short_require_bearish_structure=_bool(_get(merged, "SHORT_REQUIRE_BEARISH_STRUCTURE", "false")),
        short_require_volume_confirmation=_bool(_get(merged, "SHORT_REQUIRE_VOLUME_CONFIRMATION", "false")),
        pair_loss_throttle_enabled=_bool(

            _get(merged, "PAIR_LOSS_THROTTLE_ENABLED", "true")
        ),
        pair_loss_lookback=int(_get(merged, "PAIR_LOSS_LOOKBACK", "4")),
        pair_loss_limit=int(_get(merged, "PAIR_LOSS_LIMIT", "2")),
        pair_loss_risk_multiplier=_decimal(
            _get(merged, "PAIR_LOSS_RISK_MULTIPLIER", "0.50")
        ),
        pair_loss_severe_limit=int(_get(merged, "PAIR_LOSS_SEVERE_LIMIT", "3")),
        pair_loss_severe_risk_multiplier=_decimal(
            _get(merged, "PAIR_LOSS_SEVERE_RISK_MULTIPLIER", "0.25")
        ),
        trailing_stop_enabled=_bool(_get(merged, "TRAILING_STOP_ENABLED", "false")),
        trailing_stop_activation_pct=_decimal(
            _get(merged, "TRAILING_STOP_ACTIVATION_PCT", "1")
        ),
        trailing_stop_distance_pct=_decimal(
            _get(merged, "TRAILING_STOP_DISTANCE_PCT", "2")
        ),
        maker_fee_rate=_decimal(_get(merged, "MAKER_FEE_RATE", "0.0002")),
        taker_fee_rate=_decimal(_get(merged, "TAKER_FEE_RATE", "0.0005")),
        fee_gst_rate=_decimal(_get(merged, "FEE_GST_RATE", "0.18")),
        slippage_pct=_decimal(_get(merged, "SLIPPAGE_PCT", "0.05")),
        stop_slippage_pct=_decimal(_get(merged, "STOP_SLIPPAGE_PCT", "0.1")),
        atr_stop_enabled=_bool(_get(merged, "ATR_STOP_ENABLED", "true")),
        atr_take_profit_enabled=_bool(_get(merged, "ATR_TAKE_PROFIT_ENABLED", "true")),
        atr_trailing_enabled=_bool(_get(merged, "ATR_TRAILING_ENABLED", "true")),
        atr_take_profit_mode=_get(merged, "ATR_TAKE_PROFIT_MODE", "fixed"),
        atr_stop_multiple=_decimal(_get(merged, "ATR_STOP_MULTIPLE", "1.5")),
        atr_take_profit_multiple=_decimal(_get(merged, "ATR_TAKE_PROFIT_MULTIPLE", "2.4")),
        atr_trailing_multiple=_decimal(_get(merged, "ATR_TRAILING_MULTIPLE", "1.2")),
        bb_trail_enabled=_bool(_get(merged, "BB_TRAIL_ENABLED", "false")),
        bb_trail_buffer_multiplier=_decimal(
            _get(merged, "BB_TRAIL_BUFFER_MULTIPLIER", "1.0")
        ),
        bb_trail_activation_r=_decimal(_get(merged, "BB_TRAIL_ACTIVATION_R", "0.5")),
        bb_trail_stage2_r=_decimal(_get(merged, "BB_TRAIL_STAGE2_R", "0.5")),
        bb_trail_stage3_r=_decimal(_get(merged, "BB_TRAIL_STAGE3_R", "1.0")),
        bb_trail_force_close_r=_decimal(_get(merged, "BB_TRAIL_FORCE_CLOSE_R", "4.0")),
        bb_trail_partial_close_at_tp=_bool(
            _get(merged, "BB_TRAIL_PARTIAL_CLOSE_AT_TP", "true")
        ),
        bb_trail_partial_close_pct=_decimal(
            _get(merged, "BB_TRAIL_PARTIAL_CLOSE_PCT", "0.60")
        ),
        bb_trail_observe_only=_bool(_get(merged, "BB_TRAIL_OBSERVE_ONLY", "true")),
        compound_profits=_bool(_get(merged, "COMPOUND_PROFITS", "true")),
        breakeven_enabled=_bool(_get(merged, "BREAKEVEN_ENABLED", "false")),
        breakeven_activation_r=_decimal(_get(merged, "BREAKEVEN_ACTIVATION_R", "1.0")),
        breakeven_offset_r=_decimal(_get(merged, "BREAKEVEN_OFFSET_R", "0")),
        profit_lock_enabled=_bool(_get(merged, "PROFIT_LOCK_ENABLED", "false")),
        profit_lock_activation_r=_decimal(_get(merged, "PROFIT_LOCK_ACTIVATION_R", "1.2")),
        profit_lock_r=_decimal(_get(merged, "PROFIT_LOCK_R", "0.25")),
        atr_trail_after_r_enabled=_bool(_get(merged, "ATR_TRAIL_AFTER_R_ENABLED", "false")),
        atr_trail_activation_r=_decimal(_get(merged, "ATR_TRAIL_ACTIVATION_R", "2.0")),
        balanced_breakout_enabled=_bool(_get(merged, "BALANCED_BREAKOUT_ENABLED", "true")),
        balanced_breakout_volume_ratio_min=_decimal(_get(merged, "BALANCED_BREAKOUT_VOLUME_RATIO_MIN", "1.8")),
        balanced_breakout_body_ratio_min=_decimal(_get(merged, "BALANCED_BREAKOUT_BODY_RATIO_MIN", "0.60")),
        balanced_breakout_close_position_min=_decimal(_get(merged, "BALANCED_BREAKOUT_CLOSE_POSITION_MIN", "0.70")),
        balanced_breakout_max_extension_atr=_decimal(_get(merged, "BALANCED_BREAKOUT_MAX_EXTENSION_ATR", "2.0")),
        balanced_breakout_max_age_candles=int(_get(merged, "BALANCED_BREAKOUT_MAX_AGE_CANDLES", "2")),
        balanced_breakout_risk_multiplier=_decimal(_get(merged, "BALANCED_BREAKOUT_RISK_MULTIPLIER", "0.50")),
        false_breakout_filter_enabled=_bool(_get(merged, "FALSE_BREAKOUT_FILTER_ENABLED", "true")),
        false_breakout_max_wick_ratio=_decimal(_get(merged, "FALSE_BREAKOUT_MAX_WICK_RATIO", "0.45")),
        false_breakout_require_close_outside_parent=_bool(_get(merged, "FALSE_BREAKOUT_REQUIRE_CLOSE_OUTSIDE_PARENT", "true")),
        late_chase_block_enabled=_bool(_get(merged, "LATE_CHASE_BLOCK_ENABLED", "true")),
        late_chase_max_consecutive_impulse_candles=int(_get(merged, "LATE_CHASE_MAX_CONSECUTIVE_IMPULSE_CANDLES", "3")),
        late_chase_volume_fade_ratio=_decimal(_get(merged, "LATE_CHASE_VOLUME_FADE_RATIO", "0.75")),
        late_chase_max_extension_atr=_decimal(_get(merged, "LATE_CHASE_MAX_EXTENSION_ATR", "2.2")),
        pullback_entry_enabled=_bool(_get(merged, "PULLBACK_ENTRY_ENABLED", "true")),
        pullback_max_age_candles=int(_get(merged, "PULLBACK_MAX_AGE_CANDLES", "8")),
        pullback_max_distance_from_ema_atr=_decimal(_get(merged, "PULLBACK_MAX_DISTANCE_FROM_EMA_ATR", "0.6")),
        pullback_resume_body_ratio_min=_decimal(_get(merged, "PULLBACK_RESUME_BODY_RATIO_MIN", "0.45")),
        pullback_risk_multiplier=_decimal(_get(merged, "PULLBACK_RISK_MULTIPLIER", "0.50")),
        signal_flip_grace_candles=int(_get(merged, "SIGNAL_FLIP_GRACE_CANDLES", "2")),
        signal_flip_confirm_candles=int(_get(merged, "SIGNAL_FLIP_CONFIRM_CANDLES", "2")),
        time_stop_extend_if_momentum_strong=_bool(_get(merged, "TIME_STOP_EXTEND_IF_MOMENTUM_STRONG", "true")),
    )

    return Settings(
        trading_mode=_get(merged, "TRADING_MODE", "paper").lower(),
        live_trading_enabled=_bool(_get(merged, "LIVE_TRADING_ENABLED", "false")),
        coindcx_api_key=_get(merged, "COINDCX_API_KEY", ""),
        coindcx_api_secret=_get(merged, "COINDCX_API_SECRET", ""),
        coindcx_api_base_url=_get(
            merged, "COINDCX_API_BASE_URL", "https://api.coindcx.com"
        ).rstrip("/"),
        coindcx_public_base_url=_get(
            merged, "COINDCX_PUBLIC_BASE_URL", "https://public.coindcx.com"
        ).rstrip("/"),
        coindcx_ws_url=_get(merged, "COINDCX_WS_URL", "wss://stream.coindcx.com"),
        futures_margin_currency=_get(merged, "FUTURES_MARGIN_CURRENCY", "INR").upper(),
        price_quote_currency=_get(merged, "PRICE_QUOTE_CURRENCY", "USDT").upper(),
        quote_to_margin_rate=_decimal(_get(merged, "QUOTE_TO_MARGIN_RATE", "102")),
        paper_starting_equity=_decimal(_get(merged, "PAPER_STARTING_EQUITY", "100000")),
        paper_starting_equity_currency=_get(merged, "PAPER_STARTING_EQUITY_CURRENCY", "INR"),
        paper_leverage=_decimal(_get(merged, "PAPER_LEVERAGE", "1")),
        paper_intrabar_enabled=_bool(_get(merged, "PAPER_INTRABAR_ENABLED", "false")),
        live_confirm_i_understand_risk=_get(
            merged, "LIVE_CONFIRM_I_UNDERSTAND_RISK", "NO"
        ),
        live_pilot_dry_run=_bool(_get(merged, "LIVE_PILOT_DRY_RUN", "true")),
        live_position_margin_type=_get(
            merged, "LIVE_POSITION_MARGIN_TYPE", "isolated"
        ).lower(),
        live_allowed_pairs=parse_csv_list(
            _get(merged, "LIVE_ALLOWED_PAIRS", "B-BTC_USDT")
        ),
        live_blocked_pairs=parse_csv_list(
            _get(merged, "LIVE_BLOCKED_PAIRS", "")
        ),
        live_allowed_strategies=parse_csv_list(
            _get(merged, "LIVE_ALLOWED_STRATEGIES", "hybrid_meta_v2,rsi_macd_momentum")
        ),
        live_reconcile_seconds=int(_get(merged, "LIVE_RECONCILE_SECONDS", "30")),
        live_rest_poll_seconds=int(_get(merged, "LIVE_REST_POLL_SECONDS", "60")),
        live_closed_candle_buffer_ms=int(_get(merged, "LIVE_CLOSED_CANDLE_BUFFER_MS", "2000")),
        audit_max_file_mb=int(_get(merged, "AUDIT_MAX_FILE_MB", "100")),
        strategy_interval=_get(merged, "STRATEGY_INTERVAL", "15m"),
        execution_interval=_get(merged, "EXECUTION_INTERVAL", "1m"),
        use_partial_parent_candle=_bool(_get(merged, "USE_PARTIAL_PARENT_CANDLE", "false")),
        max_entries_per_parent_candle=int(_get(merged, "MAX_ENTRIES_PER_PARENT_CANDLE", "1")),
        enter_on_execution_close=_bool(_get(merged, "ENTER_ON_EXECUTION_CLOSE", "true")),
        default_pair=_get(merged, "DEFAULT_PAIR", "B-BTC_USDT"),
        http_timeout_seconds=float(_get(merged, "HTTP_TIMEOUT_SECONDS", "15")),
        api_max_retries=int(_get(merged, "API_MAX_RETRIES", "3")),
        api_retry_base_delay_seconds=float(
            _get(merged, "API_RETRY_BASE_DELAY_SECONDS", "0.5")
        ),
        api_rate_limit_per_second=float(_get(merged, "API_RATE_LIMIT_PER_SECOND", "4")),
        http_user_agent=_get(merged, "HTTP_USER_AGENT", "CoinDCXFuturesBot/0.1"),
        ws_ping_interval_seconds=float(_get(merged, "WS_PING_INTERVAL_SECONDS", "25")),
        log_level=_get(merged, "LOG_LEVEL", "INFO").upper(),
        log_dir=_get(merged, "LOG_DIR", "logs"),
        risk=risk,
    )

    # Final Safety Validation
    if settings.trading_mode == "live" and settings.live_trading_enabled:
        if settings.live_pilot_dry_run:
             # Dry run in live mode is okay, but let's warn if it's not expected
             pass
        else:
            # REAL LIVE TRADING
            if settings.live_confirm_i_understand_risk.strip().upper() != "YES":
                raise ValueError(
                    "REAL LIVE TRADING BLOCKED: LIVE_CONFIRM_I_UNDERSTAND_RISK must be set to 'YES' in .env"
                )
            if not settings.coindcx_api_key or not settings.coindcx_api_secret:
                 raise ValueError(
                    "REAL LIVE TRADING BLOCKED: COINDCX_API_KEY and COINDCX_API_SECRET are required."
                )
    
    return settings
