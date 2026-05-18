from __future__ import annotations

from typing import Any

from app.data.pipeline import MarketDataPipeline


def sample_futures_events(pair: str = "B-BTC_USDT") -> list[tuple[str, dict[str, Any]]]:
    return [
        (
            "new-trade",
            {
                "data": {
                    "T": 1705516361108,
                    "RT": 1705516416271.6133,
                    "p": "43000.5",
                    "q": "0.012",
                    "m": 1,
                    "s": pair,
                    "pr": "f",
                }
            },
        ),
        (
            "price-change",
            {"data": {"T": 1705516361200, "p": "43001.0", "pr": "f"}},
        ),
        (
            "candlestick",
            {
                "data": {
                    "data": [
                        {
                            "open": "42900",
                            "close": "43010",
                            "high": "43100",
                            "low": "42880",
                            "volume": "125.5",
                            "open_time": 1705514400,
                            "close_time": 1705517999.999,
                            "pair": pair,
                            "duration": "1h",
                            "symbol": "BTCUSDT",
                            "quote_volume": "5390000",
                        }
                    ],
                    "Ets": 1705516366626,
                    "i": "1h",
                    "channel": f"{pair}_1h-futures",
                    "pr": "futures",
                }
            },
        ),
        (
            "depth-update",
            {
                "data": {
                    "ts": 1705913767265,
                    "vs": 53727235,
                    "asks": {"43002": "0.5", "43001.5": "0.2"},
                    "bids": {"43000.9": "0.7", "43000.4": "1.1"},
                    "pr": "futures",
                }
            },
        ),
    ]


class ReplayMarketDataSource:
    def __init__(
        self,
        events: list[tuple[str, dict[str, Any]]] | None = None,
        *,
        pair: str = "B-BTC_USDT",
    ) -> None:
        self.events = events or sample_futures_events(pair)

    def run(self, pipeline: MarketDataPipeline) -> int:
        count = 0
        for event_name, payload in self.events:
            count += len(pipeline.handle_raw(event_name, payload))
        return count
