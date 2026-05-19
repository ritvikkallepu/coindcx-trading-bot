from __future__ import annotations

import unittest

from app.exchange.coindcx_ws import default_market_subscriptions


class WebSocketConfigTests(unittest.TestCase):
    def test_orderbook_registers_snapshot_and_update_events(self) -> None:
        subscriptions = default_market_subscriptions(pair="B-BTC_USDT")
        orderbook_events = {
            item.event_name
            for item in subscriptions
            if item.channel_name == "B-BTC_USDT@orderbook@50-futures"
        }

        self.assertEqual(orderbook_events, {"depth-snapshot", "depth-update"})


if __name__ == "__main__":
    unittest.main()
