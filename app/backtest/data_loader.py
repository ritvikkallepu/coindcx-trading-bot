from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from app.data.candle_builder import CandleSeries, interval_to_ms, rest_rows_to_series
from app.exchange.coindcx_rest import CoinDCXFuturesClient


REST_RESOLUTION_BY_INTERVAL = {
    "1m": "1",
    "5m": "5",
    "15m": "15",
    "30m": "30",
    "30min": "30",
    "1h": "60",
    "2h": "120",
    "2hr": "120",
    "4h": "240",
    "1d": "1D",
    "24h": "1D",
}


def rows_from_candles_response(response: Any) -> list[dict[str, Any]]:
    rows = response.get("data", []) if isinstance(response, dict) else response
    if rows is None:
        return []
    return [row for row in rows if isinstance(row, dict)]


def series_from_rows(
    *,
    pair: str,
    interval: str,
    rows: Iterable[dict[str, Any]],
    maxlen: int,
) -> CandleSeries:
    return rest_rows_to_series(pair=pair, interval=interval, rows=rows, maxlen=maxlen)


def load_historical_candle_series(
    *,
    client: CoinDCXFuturesClient,
    pair: str,
    interval: str,
    lookback: int,
    now: datetime | None = None,
) -> CandleSeries:
    if lookback <= 0:
        raise ValueError("lookback must be positive.")
    if interval not in REST_RESOLUTION_BY_INTERVAL:
        raise ValueError(
            f"Unsupported REST backtest interval {interval}. "
            f"Choose one of: {sorted(REST_RESOLUTION_BY_INTERVAL)}"
        )

    end = now or datetime.now(timezone.utc)
    lookback_seconds = int((interval_to_ms(interval) * lookback) / 1000)
    response = client.get_candles(
        pair=pair,
        from_ts=int((end - timedelta(seconds=lookback_seconds)).timestamp()),
        to_ts=int(end.timestamp()),
        resolution=REST_RESOLUTION_BY_INTERVAL[interval],
    )
    rows = rows_from_candles_response(response)
    return series_from_rows(pair=pair, interval=interval, rows=rows, maxlen=lookback)


def load_historical_candle_series_between(
    *,
    client: CoinDCXFuturesClient,
    pair: str,
    interval: str,
    from_ts: int,
    to_ts: int,
    maxlen: int,
) -> CandleSeries:
    if maxlen <= 0:
        raise ValueError("maxlen must be positive.")
    if from_ts >= to_ts:
        raise ValueError("from_ts must be before to_ts.")
    if interval not in REST_RESOLUTION_BY_INTERVAL:
        raise ValueError(
            f"Unsupported REST backtest interval {interval}. "
            f"Choose one of: {sorted(REST_RESOLUTION_BY_INTERVAL)}"
        )

    response = client.get_candles(
        pair=pair,
        from_ts=from_ts,
        to_ts=to_ts,
        resolution=REST_RESOLUTION_BY_INTERVAL[interval],
    )
    rows = rows_from_candles_response(response)
    return series_from_rows(pair=pair, interval=interval, rows=rows, maxlen=maxlen)
