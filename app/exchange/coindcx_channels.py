from __future__ import annotations


VALID_CANDLE_INTERVALS = {"1m", "5m", "15m", "30m", "1h", "4h", "8h", "1d", "3d", "1w", "1M"}
VALID_ORDERBOOK_DEPTHS = {10, 20, 50}


def futures_trade_channel(pair: str) -> str:
    return f"{pair}@trades-futures"


def futures_price_channel(pair: str) -> str:
    return f"{pair}@prices-futures"


def futures_candle_channel(pair: str, interval: str) -> str:
    if interval not in VALID_CANDLE_INTERVALS:
        raise ValueError(
            f"Invalid candle interval {interval}. Valid intervals: {sorted(VALID_CANDLE_INTERVALS)}"
        )
    return f"{pair}_{interval}-futures"


def futures_orderbook_channel(pair: str, depth: int = 50) -> str:
    if depth not in VALID_ORDERBOOK_DEPTHS:
        raise ValueError("CoinDCX futures orderbook depth must be 10, 20, or 50.")
    return f"{pair}@orderbook@{depth}-futures"


def futures_current_prices_channel() -> str:
    return "currentPrices@futures@rt"

