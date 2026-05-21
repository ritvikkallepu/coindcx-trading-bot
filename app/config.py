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
    max_open_positions: int = 1
    max_leverage: int = 30
    max_total_open_notional_pct: Decimal = Decimal("0")
    max_total_risk_pct: Decimal = Decimal("20")
    liquidation_buffer_pct: Decimal = Decimal("2")
    trailing_stop_enabled: bool = False
    trailing_stop_activation_pct: Decimal = Decimal("1")
    trailing_stop_distance_pct: Decimal = Decimal("2")


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
    paper_starting_equity: Decimal = Decimal("10000")
    paper_starting_equity_currency: str = "USDT"
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
        max_open_positions=int(_get(merged, "MAX_OPEN_POSITIONS", "1")),
        max_leverage=int(_get(merged, "MAX_LEVERAGE", "30")),
        max_total_open_notional_pct=_decimal(
            _get(merged, "MAX_TOTAL_OPEN_NOTIONAL_PCT", "0")
        ),
        max_total_risk_pct=_decimal(_get(merged, "MAX_TOTAL_RISK_PCT", "20")),
        liquidation_buffer_pct=_decimal(_get(merged, "LIQUIDATION_BUFFER_PCT", "2")),
        trailing_stop_enabled=_bool(_get(merged, "TRAILING_STOP_ENABLED", "false")),
        trailing_stop_activation_pct=_decimal(
            _get(merged, "TRAILING_STOP_ACTIVATION_PCT", "1")
        ),
        trailing_stop_distance_pct=_decimal(
            _get(merged, "TRAILING_STOP_DISTANCE_PCT", "2")
        ),
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
        paper_starting_equity=_decimal(_get(merged, "PAPER_STARTING_EQUITY", "10000")),
        paper_starting_equity_currency=_get(merged, "PAPER_STARTING_EQUITY_CURRENCY", "USDT"),
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
