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
        self._subscription_lock = threading.RLock()
        self._subscriptions: list[MarketSubscription] = []
        self._joined_channels: set[str] = set()
        self._registered_event_names: set[str] = set()

    def run(
        self,
        subscriptions: list[MarketSubscription],
        *,
        max_events: int | None = None,
    ) -> None:
        socketio = self._import_socketio()
        with self._subscription_lock:
            self._subscriptions = list(subscriptions)
            self._joined_channels = set()
            self._registered_event_names = set()
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
            with self._subscription_lock:
                self._joined_channels.clear()
                current_subscriptions = list(self._subscriptions)
            for subscription in current_subscriptions:
                self._join_subscription(sio, subscription)
            sio.start_background_task(self._ping_loop, sio)

        @sio.event
        def disconnect() -> None:
            self.logger.info("Disconnected from CoinDCX websocket.")

        @sio.event
        def connect_error(data: Any) -> None:
            self.logger.error("CoinDCX websocket connection error: %s", data)

        for event_name in sorted({item.event_name for item in subscriptions}):
            self._register_event_handler(sio, event_name)

        sio.connect(self.settings.coindcx_ws_url, transports=["websocket"])
        sio.wait()

    def subscribe(self, subscriptions: list[MarketSubscription]) -> int:
        """Add websocket subscriptions to a running client.

        CoinDCX channels are joined by channel name while payload handlers are
        registered by event name. This method keeps both deduplicated so the
        dashboard can expand the paper watchlist without restarting the loop.
        """
        if not subscriptions:
            return 0

        with self._subscription_lock:
            known = set(self._subscriptions)
            new_subscriptions: list[MarketSubscription] = []
            for item in subscriptions:
                if item in known:
                    continue
                known.add(item)
                new_subscriptions.append(item)
            self._subscriptions.extend(new_subscriptions)
            sio = self._sio

        if sio is None:
            return len(new_subscriptions)

        for event_name in sorted({item.event_name for item in subscriptions}):
            self._register_event_handler(sio, event_name)

        if getattr(sio, "connected", False):
            for subscription in subscriptions:
                self._join_subscription(sio, subscription)

        return len(new_subscriptions)

    def stop(self) -> None:
        self._stop_requested.set()
        if self._sio and self._sio.connected:
            self._sio.disconnect()

    def _register_event_handler(self, sio: Any, event_name: str) -> None:
        with self._subscription_lock:
            if event_name in self._registered_event_names:
                return
            self._registered_event_names.add(event_name)
        sio.on(event_name, self._handler_for(event_name, sio))

    def _join_subscription(self, sio: Any, subscription: MarketSubscription) -> bool:
        with self._subscription_lock:
            if subscription.channel_name in self._joined_channels:
                return False
            self._joined_channels.add(subscription.channel_name)
        self.logger.info("Joining CoinDCX channel %s", subscription.channel_name)
        sio.emit("join", {"channelName": subscription.channel_name})
        return True

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
