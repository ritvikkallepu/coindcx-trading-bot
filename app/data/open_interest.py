from __future__ import annotations

import json
import time
from bisect import bisect_right
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Iterable, Mapping
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from app.data.candle_builder import OHLCVCandle


BINANCE_USDM_BASE_URL = "https://fapi.binance.com"
BINANCE_OI_MAX_HISTORY_MS = 30 * 24 * 60 * 60_000
BINANCE_OI_PERIOD_BY_INTERVAL = {
    "1m": "5m",
    "5m": "5m",
    "15m": "15m",
    "30m": "30m",
    "30min": "30m",
    "1h": "1h",
    "2h": "2h",
    "2hr": "2h",
    "4h": "4h",
    "8h": "12h",
    "24h": "1d",
    "1d": "1d",
    "3d": "1d",
    "1w": "1d",
}
BINANCE_OI_PERIOD_MS = {
    "5m": 5 * 60_000,
    "15m": 15 * 60_000,
    "30m": 30 * 60_000,
    "1h": 60 * 60_000,
    "2h": 2 * 60 * 60_000,
    "4h": 4 * 60 * 60_000,
    "6h": 6 * 60 * 60_000,
    "12h": 12 * 60 * 60_000,
    "1d": 24 * 60 * 60_000,
}


@dataclass(frozen=True)
class OpenInterestPoint:
    timestamp_ms: int
    symbol: str
    open_interest: Decimal
    open_interest_value: Decimal | None = None


class OpenInterestFeatureSeries:
    def __init__(self, features: Iterable[tuple[int, Mapping[str, Any]]]) -> None:
        ordered = sorted((int(timestamp), dict(feature)) for timestamp, feature in features)
        self._timestamps = [timestamp for timestamp, _ in ordered]
        self._features = [feature for _, feature in ordered]

    def __len__(self) -> int:
        return len(self._features)

    def latest_at_or_before(self, timestamp_ms: int) -> dict[str, Any] | None:
        index = bisect_right(self._timestamps, timestamp_ms) - 1
        if index < 0:
            return None
        return dict(self._features[index])


class BinanceOpenInterestClient:
    def __init__(
        self,
        *,
        base_url: str = BINANCE_USDM_BASE_URL,
        timeout_seconds: float = 2.5,
        opener: Callable[..., Any] = urlopen,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self._opener = opener

    def get_open_interest_history(
        self,
        *,
        symbol: str,
        period: str,
        start_time_ms: int,
        end_time_ms: int,
        limit: int = 500,
    ) -> list[OpenInterestPoint]:
        if period not in BINANCE_OI_PERIOD_MS:
            raise ValueError(f"Unsupported Binance open-interest period: {period}")
        if start_time_ms >= end_time_ms:
            return []
        limit = min(max(limit, 1), 500)

        params = {
            "symbol": symbol,
            "period": period,
            "startTime": start_time_ms,
            "endTime": end_time_ms,
            "limit": limit,
        }
        request = Request(
            f"{self.base_url}/futures/data/openInterestHist?{urlencode(params)}",
            headers={"Accept": "application/json", "User-Agent": "coindcx-bot/0.1"},
            method="GET",
        )
        try:
            with self._opener(request, timeout=self.timeout_seconds) as response:
                raw = response.read().decode("utf-8")
        except HTTPError as exc:
            if _is_unavailable_binance_oi_response(exc):
                return []
            raise
        data = json.loads(raw)
        if not isinstance(data, list):
            return []
        return [_point_from_binance_row(row) for row in data if isinstance(row, dict)]

    def get_open_interest_history_between(
        self,
        *,
        symbol: str,
        period: str,
        start_time_ms: int,
        end_time_ms: int,
        max_requests: int = 12,
    ) -> list[OpenInterestPoint]:
        period_ms = BINANCE_OI_PERIOD_MS[period]
        points: list[OpenInterestPoint] = []
        cursor = start_time_ms
        seen_timestamps: set[int] = set()
        for _ in range(max(max_requests, 1)):
            if cursor >= end_time_ms:
                break
            chunk = self.get_open_interest_history(
                symbol=symbol,
                period=period,
                start_time_ms=cursor,
                end_time_ms=end_time_ms,
            )
            if not chunk:
                break
            for point in chunk:
                if point.timestamp_ms not in seen_timestamps:
                    points.append(point)
                    seen_timestamps.add(point.timestamp_ms)
            next_cursor = max(point.timestamp_ms for point in chunk) + period_ms
            if next_cursor <= cursor:
                break
            cursor = next_cursor
        return sorted(points, key=lambda point: point.timestamp_ms)


def coindcx_pair_to_binance_symbol(pair: str) -> str | None:
    normalized = pair.strip().upper()
    if normalized.startswith("B-"):
        normalized = normalized[2:]
    if "_" not in normalized:
        return None
    base, quote = normalized.split("_", 1)
    if not base or quote != "USDT":
        return None
    return f"{base}{quote}"


def binance_oi_period_for_interval(interval: str) -> str | None:
    return BINANCE_OI_PERIOD_BY_INTERVAL.get(interval.strip().lower())


def build_open_interest_features(
    *,
    pair: str,
    interval: str,
    candles: Iterable[OHLCVCandle],
    points: Iterable[OpenInterestPoint],
) -> OpenInterestFeatureSeries:
    candle_list = sorted(candles, key=lambda candle: candle.close_time_ms)
    point_list = sorted(points, key=lambda point: point.timestamp_ms)
    if not candle_list or len(point_list) < 2:
        return OpenInterestFeatureSeries([])

    symbol = point_list[-1].symbol
    period = binance_oi_period_for_interval(interval) or ""
    candle_timestamps = [candle.close_time_ms for candle in candle_list]
    features: list[tuple[int, dict[str, Any]]] = []
    for index in range(1, len(point_list)):
        current = point_list[index]
        previous = point_list[index - 1]
        candle_index = bisect_right(candle_timestamps, current.timestamp_ms) - 1
        if candle_index <= 0:
            continue
        current_close = candle_list[candle_index].close
        previous_close = candle_list[candle_index - 1].close
        oi_current = current.open_interest_value or current.open_interest
        oi_previous = previous.open_interest_value or previous.open_interest
        if previous_close <= 0 or oi_previous <= 0:
            continue
        price_change_pct = ((current_close - previous_close) / previous_close) * Decimal("100")
        oi_change_pct = ((oi_current - oi_previous) / oi_previous) * Decimal("100")
        score = open_interest_confirmation_score(
            price_change_pct=price_change_pct,
            open_interest_change_pct=oi_change_pct,
        )
        features.append(
            (
                current.timestamp_ms,
                {
                    "source": "binance_proxy",
                    "symbol": symbol,
                    "period": period,
                    "score": score,
                    "score_used": True,
                    "change_pct": oi_change_pct,
                    "price_change_pct": price_change_pct,
                    "open_interest": current.open_interest,
                    "open_interest_value": current.open_interest_value,
                    "timestamp_ms": current.timestamp_ms,
                    "proxy_for_pair": pair,
                },
            )
        )
    return OpenInterestFeatureSeries(features)


def load_binance_open_interest_proxy(
    *,
    pair: str,
    interval: str,
    candles: Iterable[OHLCVCandle],
    client: BinanceOpenInterestClient | None = None,
) -> OpenInterestFeatureSeries:
    candle_list = sorted(candles, key=lambda candle: candle.close_time_ms)
    symbol = coindcx_pair_to_binance_symbol(pair)
    period = binance_oi_period_for_interval(interval)
    if symbol is None or period is None or not candle_list:
        return OpenInterestFeatureSeries([])

    window = binance_oi_request_window(
        start_time_ms=candle_list[0].open_time_ms,
        end_time_ms=candle_list[-1].close_time_ms + 1,
    )
    if window is None:
        return OpenInterestFeatureSeries([])
    start_time_ms, end_time_ms = window

    oi_client = client or BinanceOpenInterestClient()
    points = oi_client.get_open_interest_history_between(
        symbol=symbol,
        period=period,
        start_time_ms=start_time_ms,
        end_time_ms=end_time_ms,
    )
    return build_open_interest_features(
        pair=pair,
        interval=interval,
        candles=candle_list,
        points=points,
    )


def binance_oi_request_window(
    *,
    start_time_ms: int,
    end_time_ms: int,
    now_ms: int | None = None,
) -> tuple[int, int] | None:
    """Clamp Binance OI requests to the exchange's recent-history window."""
    if start_time_ms >= end_time_ms:
        return None
    current_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
    earliest_available_ms = current_ms - BINANCE_OI_MAX_HISTORY_MS
    if end_time_ms <= earliest_available_ms:
        return None
    return max(start_time_ms, earliest_available_ms), end_time_ms


def open_interest_confirmation_score(
    *,
    price_change_pct: Decimal,
    open_interest_change_pct: Decimal,
) -> Decimal:
    if price_change_pct == 0 or open_interest_change_pct == 0:
        return Decimal("0")

    price_direction = Decimal("1") if price_change_pct > 0 else Decimal("-1")
    oi_strength = min(abs(open_interest_change_pct) / Decimal("5"), Decimal("1"))
    price_strength = min(abs(price_change_pct) / Decimal("2"), Decimal("1"))
    strength = (oi_strength * Decimal("0.70")) + (price_strength * Decimal("0.30"))

    if open_interest_change_pct > 0:
        return _clamp(price_direction * strength, Decimal("-1"), Decimal("1"))
    return _clamp(price_direction * strength * Decimal("0.25"), Decimal("-1"), Decimal("1"))


def _point_from_binance_row(row: Mapping[str, Any]) -> OpenInterestPoint:
    return OpenInterestPoint(
        timestamp_ms=int(row["timestamp"]),
        symbol=str(row["symbol"]),
        open_interest=Decimal(str(row["sumOpenInterest"])),
        open_interest_value=(
            Decimal(str(row["sumOpenInterestValue"]))
            if row.get("sumOpenInterestValue") is not None
            else None
        ),
    )


def _clamp(value: Decimal, low: Decimal, high: Decimal) -> Decimal:
    return min(max(value, low), high)


def _is_unavailable_binance_oi_response(exc: HTTPError) -> bool:
    if exc.code != 400:
        return False

    body = _read_http_error_body(exc)
    if not body:
        return False

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        payload = {}

    code = payload.get("code") if isinstance(payload, Mapping) else None
    message = str(payload.get("msg", "") if isinstance(payload, Mapping) else body).lower()

    if code == -1121 or "invalid symbol" in message:
        return True
    if "latest 1 month" in message or "latest 30 days" in message:
        return True
    if "starttime" in message and ("too old" in message or "invalid" in message):
        return True
    return False


def _read_http_error_body(exc: HTTPError) -> str:
    try:
        return exc.read().decode("utf-8", errors="replace")
    except Exception:
        return ""
