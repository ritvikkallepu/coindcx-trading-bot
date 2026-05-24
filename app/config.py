from __future__ import annotations

import os
from dataclasses import asdict, dataclass
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
    return value


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


# These are conservative paper-trading defaults.
# They should be overridden via .env for live trading.
@dataclass(frozen=True)
class RiskSettings:
    max_risk_per_trade_pct: Decimal = Decimal("5")
    max_daily_loss_pct: Decimal = Decimal("10")
    max_open_positions: int = 3
    max_open_positions_per_pair: int = 1
    allow_multi_pair_positions: bool = True
    allow_same_pair_pyramiding: bool = False
    max_leverage: int = 30
    max_total_open_notional_pct: Decimal = Decimal("0")
    max_total_risk_pct: Decimal = Decimal("20")
    max_margin_usage_pct: Decimal = Decimal("100.0")
    max_margin_per_trade_pct: Decimal = Decimal("0")
    liquidation_buffer_pct: Decimal = Decimal("2")
    entry_safety_enabled: bool = True
    min_stop_distance_pct: Decimal = Decimal("0.50")
    min_entry_atr_pct: Decimal = Decimal("0.05")
    min_stop_atr_multiple: Decimal = Decimal("0.75")
    min_entry_volume_ratio: Decimal = Decimal("0.50")
    max_entry_spread_pct: Decimal = Decimal("0.30")
    min_entry_side_depth_margin: Decimal = Decimal("0")
    pair_loss_throttle_enabled: bool = True
    pair_loss_lookback: int = 4
    pair_loss_limit: int = 2
    pair_loss_risk_multiplier: Decimal = Decimal("0.50")
    pair_loss_severe_limit: int = 3
    pair_loss_severe_risk_multiplier: Decimal = Decimal("0.25")
    trailing_stop_enabled: bool = False
    trailing_stop_activation_pct: Decimal = Decimal("1")
    trailing_stop_distance_pct: Decimal = Decimal("2")
    # Fees and Slippage
    maker_fee_rate: Decimal = Decimal("0.0002")
    taker_fee_rate: Decimal = Decimal("0.0005")
    fee_gst_rate: Decimal = Decimal("0.18")
    slippage_pct: Decimal = Decimal("0.05")
    stop_slippage_pct: Decimal = Decimal("0.1")
    # ATR Policy
    atr_stop_enabled: bool = True
    atr_take_profit_enabled: bool = True
    atr_trailing_enabled: bool = True
    atr_take_profit_mode: str = "fixed"
    atr_stop_multiple: Decimal = Decimal("1.5")
    atr_take_profit_multiple: Decimal = Decimal("2.4")
    atr_trailing_multiple: Decimal = Decimal("2.0")
    # Management
    breakeven_enabled: bool = False
    breakeven_activation_r: Decimal = Decimal("1.0")
    breakeven_offset_r: Decimal = Decimal("0")
    profit_lock_enabled: bool = False
    profit_lock_activation_r: Decimal = Decimal("1.5")
    profit_lock_r: Decimal = Decimal("0.5")
    atr_trail_after_r_enabled: bool = False
    atr_trail_activation_r: Decimal = Decimal("2.0")


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
    quote_to_margin_rate: Decimal = Decimal("98")
    paper_starting_equity: Decimal = Decimal("10000")
    paper_starting_equity_currency: str = "INR"
    paper_leverage: Decimal = Decimal("1")
    paper_intrabar_enabled: bool = False
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
    merged = {**dotenv_values, **os.environ}

    risk = RiskSettings(
        max_risk_per_trade_pct=_decimal(_get(merged, "MAX_RISK_PER_TRADE_PCT", "5")),
        max_daily_loss_pct=_decimal(_get(merged, "MAX_DAILY_LOSS_PCT", "10")),
        max_open_positions=int(_get(merged, "MAX_OPEN_POSITIONS", "3")),
        max_open_positions_per_pair=int(_get(merged, "MAX_OPEN_POSITIONS_PER_PAIR", "1")),
        allow_multi_pair_positions=_bool(_get(merged, "ALLOW_MULTI_PAIR_POSITIONS", "true")),
        allow_same_pair_pyramiding=_bool(_get(merged, "ALLOW_SAME_PAIR_PYRAMIDING", "false")),
        max_leverage=int(_get(merged, "MAX_LEVERAGE", "30")),
        max_total_open_notional_pct=_decimal(
            _get(merged, "MAX_TOTAL_OPEN_NOTIONAL_PCT", "0")
        ),
        max_total_risk_pct=_decimal(_get(merged, "MAX_TOTAL_RISK_PCT", "20")),
        max_margin_usage_pct=_decimal(_get(merged, "MAX_MARGIN_USAGE_PCT", "100.0")),
        max_margin_per_trade_pct=_decimal(
            _get(merged, "MAX_MARGIN_PER_TRADE_PCT", "0")
        ),
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
        atr_trailing_multiple=_decimal(_get(merged, "ATR_TRAILING_MULTIPLE", "2.0")),
        breakeven_enabled=_bool(_get(merged, "BREAKEVEN_ENABLED", "false")),
        breakeven_activation_r=_decimal(_get(merged, "BREAKEVEN_ACTIVATION_R", "1.0")),
        breakeven_offset_r=_decimal(_get(merged, "BREAKEVEN_OFFSET_R", "0")),
        profit_lock_enabled=_bool(_get(merged, "PROFIT_LOCK_ENABLED", "false")),
        profit_lock_activation_r=_decimal(_get(merged, "PROFIT_LOCK_ACTIVATION_R", "1.5")),
        profit_lock_r=_decimal(_get(merged, "PROFIT_LOCK_R", "0.5")),
        atr_trail_after_r_enabled=_bool(_get(merged, "ATR_TRAIL_AFTER_R_ENABLED", "false")),
        atr_trail_activation_r=_decimal(_get(merged, "ATR_TRAIL_ACTIVATION_R", "2.0")),
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
        quote_to_margin_rate=_decimal(_get(merged, "QUOTE_TO_MARGIN_RATE", "98")),
        paper_starting_equity=_decimal(_get(merged, "PAPER_STARTING_EQUITY", "10000")),
        paper_starting_equity_currency=_get(merged, "PAPER_STARTING_EQUITY_CURRENCY", "INR"),
        paper_leverage=_decimal(_get(merged, "PAPER_LEVERAGE", "1")),
        paper_intrabar_enabled=_bool(_get(merged, "PAPER_INTRABAR_ENABLED", "false")),
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
