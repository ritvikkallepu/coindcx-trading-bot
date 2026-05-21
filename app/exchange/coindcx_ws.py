from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Any

from app.config import Settings, load_settings
from app.data.pipeline import MarketDataPipeline
from app.exchange.coindcx_channels import (
    futures_candle_channel,
    futures_current_prices_channel,
    futures_orderbook_channel,
    futures_price_channel,
    futures_trade_channel,
)


class WebSocketDependencyError(RuntimeError):
    """Raised when the Socket.IO client dependency is not installed."""


@dataclass(frozen=True)
class MarketSubscription:
    channel_name: str
    event_name: str


def default_market_subscriptions(
    *,
    pair: str,
    candle_interval: str = "1m",
    orderbook_depth: int = 50,
    include_current_prices: bool = False,
) -> list[MarketSubscription]:
    subscriptions = [
        MarketSubscription(futures_trade_channel(pair), "new-trade"),
        MarketSubscription(futures_price_channel(pair), "price-change"),
        MarketSubscription(futures_candle_channel(pair, candle_interval), "candlestick"),
        MarketSubscription(
            futures_orderbook_channel(pair, orderbook_depth),
            "depth-snapshot",
        ),
        MarketSubscription(
            futures_orderbook_channel(pair, orderbook_depth),
            "depth-update",
        ),
    ]
    if include_current_prices:
        subscriptions.append(
            MarketSubscription(
                futures_current_prices_channel(),
                "currentPrices@futures#update",
            )
        )
    return subscriptions


class CoinDCXFuturesWebSocketClient:
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        pipeline: MarketDataPipeline,
        logger: logging.Logger | None = None,
    ) -> None:
        self.settings = settings or load_settings()
        self.pipeline = pipeline
        self.logger = logger or logging.getLogger(__name__)
        self._received_events = 0
        self._max_events: int | None = None
        self._stop_requested = threading.Event()
        self._sio: Any | None = None

    def run(
        self,
        subscriptions: list[MarketSubscription],
        *,
        max_events: int | None = None,
    ) -> None:
        socketio = self._import_socketio()
        self._sio = socketio.Client(
            reconnection=True,
            logger=False,
            engineio_logger=False,
        )
        sio = self._sio
        self._received_events = 0
        self._max_events = max_events
        self._stop_requested.clear()

        @sio.event
        def connect() -> None:
            self.logger.info("Connected to CoinDCX websocket.")
            joined_channels: set[str] = set()
            for subscription in subscriptions:
                if subscription.channel_name in joined_channels:
                    continue
                joined_channels.add(subscription.channel_name)
                self.logger.info("Joining CoinDCX channel %s", subscription.channel_name)
                sio.emit("join", {"channelName": subscription.channel_name})
            sio.start_background_task(self._ping_loop, sio)

        @sio.event
        def disconnect() -> None:
            self.logger.info("Disconnected from CoinDCX websocket.")

        @sio.event
        def connect_error(data: Any) -> None:
            self.logger.error("CoinDCX websocket connection error: %s", data)

        for event_name in sorted({item.event_name for item in subscriptions}):
            sio.on(event_name, self._handler_for(event_name, sio))

        sio.connect(self.settings.coindcx_ws_url, transports=["websocket"])
        sio.wait()

    def stop(self) -> None:
        self._stop_requested.set()
        if self._sio and self._sio.connected:
            self._sio.disconnect()

    def _handler_for(self, event_name: str, sio: Any):
        def handle(payload: Any) -> None:
            try:
                events = self.pipeline.handle_raw(event_name, payload)
            except Exception:
                self.logger.exception("Failed to normalize CoinDCX websocket event %s", event_name)
                return

            self._received_events += len(events)
            if self._max_events is not None and self._received_events >= self._max_events:
                self._stop_requested.set()
                sio.disconnect()

        return handle

    def _ping_loop(self, sio: Any) -> None:
        while not self._stop_requested.is_set() and sio.connected:
            sio.sleep(self.settings.ws_ping_interval_seconds)
            if self._stop_requested.is_set() or not sio.connected:
                break
            try:
                sio.emit("ping", {"data": "Ping message"})
            except Exception:
                self.logger.exception("Failed to send CoinDCX websocket ping.")

    @staticmethod
    def _import_socketio():
        try:
            import socketio  # type: ignore[import-not-found]
        except ImportError as exc:
            raise WebSocketDependencyError(
                "python-socketio is required for live websockets. "
                'Install it with: python -m pip install -e ".[ws]"'
            ) from exc
        return socketio
