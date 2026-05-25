from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from app.exchange.coindcx_ws import (
    CoinDCXFuturesWebSocketClient,
    MarketSubscription,
    default_market_subscriptions,
)


class WebSocketConfigTests(unittest.TestCase):
    def test_orderbook_registers_snapshot_and_update_events(self) -> None:
        subscriptions = default_market_subscriptions(pair="B-BTC_USDT")
        orderbook_events = {
            item.event_name
            for item in subscriptions
            if item.channel_name == "B-BTC_USDT@orderbook@50-futures"
        }

        self.assertEqual(orderbook_events, {"depth-snapshot", "depth-update"})

    def test_running_client_can_add_subscriptions_without_duplicate_joins(self) -> None:
        class FakeSocket:
            connected = True

            def __init__(self) -> None:
                self.handlers: list[str] = []
                self.emits: list[tuple[str, dict[str, str]]] = []

            def on(self, event_name, handler) -> None:
                self.handlers.append(event_name)

            def emit(self, event_name, payload) -> None:
                self.emits.append((event_name, payload))

        fake_socket = FakeSocket()
        client = CoinDCXFuturesWebSocketClient(pipeline=MagicMock())
        client._sio = fake_socket

        added = client.subscribe(
            [
                MarketSubscription("B-BTC_USDT@candlestick_5m-futures", "candlestick"),
                MarketSubscription("B-BTC_USDT@candlestick_5m-futures", "candlestick"),
                MarketSubscription("B-BTC_USDT@orderbook@50-futures", "depth-update"),
                MarketSubscription("B-BTC_USDT@orderbook@50-futures", "depth-snapshot"),
            ]
        )

        joined_channels = [payload["channelName"] for _, payload in fake_socket.emits]
        self.assertEqual(added, 3)
        self.assertEqual(
            joined_channels,
            [
                "B-BTC_USDT@candlestick_5m-futures",
                "B-BTC_USDT@orderbook@50-futures",
            ],
        )
        self.assertEqual(set(fake_socket.handlers), {"candlestick", "depth-snapshot", "depth-update"})


if __name__ == "__main__":
    unittest.main()
