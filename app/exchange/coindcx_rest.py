from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from app.config import Settings, load_settings
from app.exchange.errors import (
    CoinDCXAPIError,
    CoinDCXAuthError,
    CoinDCXNetworkError,
    CoinDCXRateLimitError,
    CoinDCXServerError,
    LiveTradingDisabledError,
)
from app.exchange.models import FuturesOrderRequest
from app.exchange.signing import auth_headers, serialize_body
from app.utils.rate_limit import MinIntervalRateLimiter
from app.utils.time import utc_timestamp_ms


RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


@dataclass(frozen=True)
class HTTPResponse:
    status_code: int
    text: str
    headers: Mapping[str, str]

    def json(self) -> Any:
        if not self.text:
            return None
        return json.loads(self.text)


class UrllibTransport:
    def __init__(self, timeout_seconds: float = 15.0) -> None:
        self.timeout_seconds = timeout_seconds

    def request(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        data: bytes | None = None,
    ) -> HTTPResponse:
        request = Request(
            url=url,
            data=data,
            headers=dict(headers),
            method=method.upper(),
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                body = response.read().decode("utf-8")
                return HTTPResponse(
                    status_code=response.status,
                    text=body,
                    headers=dict(response.headers.items()),
                )
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            return HTTPResponse(
                status_code=exc.code,
                text=body,
                headers=dict(exc.headers.items()) if exc.headers else {},
            )
        except URLError as exc:
            raise CoinDCXNetworkError(str(exc.reason)) from exc


class CoinDCXFuturesClient:
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        transport: UrllibTransport | None = None,
        rate_limiter: MinIntervalRateLimiter | None = None,
        clock_ms: Callable[[], int] = utc_timestamp_ms,
        logger: logging.Logger | None = None,
        allow_trading: bool | None = None,
    ) -> None:
        self.settings = settings or load_settings()
        self.transport = transport or UrllibTransport(self.settings.http_timeout_seconds)
        self.rate_limiter = rate_limiter or MinIntervalRateLimiter(
            self.settings.api_rate_limit_per_second
        )
        self.clock_ms = clock_ms
        self.logger = logger or logging.getLogger(__name__)
        self.allow_trading = (
            self.settings.live_trading_allowed if allow_trading is None else allow_trading
        )

    def get_active_instruments(self, margin_currency: str = "INR") -> list[str]:
        return self._request_json(
            "GET",
            self.settings.coindcx_api_base_url,
            "/exchange/v1/derivatives/futures/data/active_instruments",
            query={"margin_currency_short_name[]": [margin_currency.upper()]},
        )

    def get_instrument(self, pair: str, margin_currency: str = "INR") -> dict[str, Any]:
        return self._request_json(
            "GET",
            self.settings.coindcx_api_base_url,
            "/exchange/v1/derivatives/futures/data/instrument",
            query={"pair": pair, "margin_currency_short_name": margin_currency.upper()},
        )

    def get_trades(self, pair: str) -> list[dict[str, Any]]:
        return self._request_json(
            "GET",
            self.settings.coindcx_api_base_url,
            "/exchange/v1/derivatives/futures/data/trades",
            query={"pair": pair},
        )

    def get_orderbook(self, pair: str, depth: int = 50) -> dict[str, Any]:
        if depth not in {10, 20, 50}:
            raise ValueError("CoinDCX futures orderbook depth must be 10, 20, or 50.")
        return self._request_json(
            "GET",
            self.settings.coindcx_public_base_url,
            f"/market_data/v3/orderbook/{pair}-futures/{depth}",
        )

    def get_candles(
        self,
        *,
        pair: str,
        from_ts: int,
        to_ts: int,
        resolution: str,
    ) -> dict[str, Any]:
        if from_ts >= to_ts:
            raise ValueError("from_ts must be earlier than to_ts.")
        return self._request_json(
            "GET",
            self.settings.coindcx_public_base_url,
            "/market_data/candlesticks",
            query={
                "pair": pair,
                "from": from_ts,
                "to": to_ts,
                "resolution": resolution,
                "pcode": "f",
            },
        )

    def get_user_info(self) -> dict[str, Any]:
        return self._request_json(
            "POST",
            self.settings.coindcx_api_base_url,
            "/exchange/v1/users/info",
            auth=True,
        )

    def get_wallets(self) -> list[dict[str, Any]]:
        return self._request_json(
            "GET",
            self.settings.coindcx_api_base_url,
            "/exchange/v1/derivatives/futures/wallets",
            auth=True,
        )

    def list_orders(
        self,
        *,
        status: str,
        side: str,
        page: int = 1,
        size: int = 100,
        margin_currencies: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        body = {
            "status": status,
            "side": side,
            "page": page,
            "size": size,
            "margin_currency_short_name": margin_currencies
            or [self.settings.futures_margin_currency],
        }
        return self._request_json(
            "POST",
            self.settings.coindcx_api_base_url,
            "/exchange/v1/derivatives/futures/orders",
            body=body,
            auth=True,
        )

    def list_positions(
        self,
        *,
        page: int = 1,
        size: int = 100,
        margin_currencies: list[str] | None = None,
        pairs: list[str] | None = None,
        position_ids: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        body = {
            "page": page,
            "size": size,
            "margin_currency_short_name": margin_currencies
            or [self.settings.futures_margin_currency],
        }
        if pairs:
            body["pairs"] = ",".join(pairs)
        if position_ids:
            body["position_ids"] = ",".join(position_ids)
        return self._request_json(
            "POST",
            self.settings.coindcx_api_base_url,
            "/exchange/v1/derivatives/futures/positions",
            body=body,
            auth=True,
        )

    def list_position_transactions(
        self,
        *,
        stage: str,
        page: int = 1,
        size: int = 100,
        margin_currencies: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Return futures position transactions in the margin currency."""

        body = {
            "stage": stage,
            "page": page,
            "size": size,
            "margin_currency_short_name": margin_currencies
            or [self.settings.futures_margin_currency],
        }
        return self._request_json(
            "POST",
            self.settings.coindcx_api_base_url,
            "/exchange/v1/derivatives/futures/positions/transactions",
            body=body,
            auth=True,
        )

    def place_order(self, order: FuturesOrderRequest) -> list[dict[str, Any]]:
        self._require_live_trading("place futures orders")
        return self._request_json(
            "POST",
            self.settings.coindcx_api_base_url,
            "/exchange/v1/derivatives/futures/orders/create",
            body={"order": order.to_api_order()},
            auth=True,
        )

    def cancel_order(self, order_id: str) -> dict[str, Any]:
        self._require_live_trading("cancel futures orders")
        return self._request_json(
            "POST",
            self.settings.coindcx_api_base_url,
            "/exchange/v1/derivatives/futures/orders/cancel",
            body={"id": order_id},
            auth=True,
        )

    def cancel_all_open_orders(
        self,
        *,
        margin_currencies: list[str] | None = None,
    ) -> dict[str, Any]:
        self._require_live_trading("cancel all futures open orders")
        return self._request_json(
            "POST",
            self.settings.coindcx_api_base_url,
            "/exchange/v1/derivatives/futures/positions/cancel_all_open_orders",
            body={
                "margin_currency_short_name": margin_currencies
                or [self.settings.futures_margin_currency]
            },
            auth=True,
        )

    def cancel_all_open_orders_for_position(self, position_id: str) -> dict[str, Any]:
        self._require_live_trading("cancel futures open orders for a position")
        return self._request_json(
            "POST",
            self.settings.coindcx_api_base_url,
            "/exchange/v1/derivatives/futures/positions/cancel_all_open_orders_for_position",
            body={"id": position_id},
            auth=True,
        )

    def exit_position(self, position_id: str) -> dict[str, Any]:
        self._require_live_trading("exit a futures position")
        return self._request_json(
            "POST",
            self.settings.coindcx_api_base_url,
            "/exchange/v1/derivatives/futures/positions/exit",
            body={"id": position_id},
            auth=True,
        )

    def create_position_tpsl(
        self,
        *,
        position_id: str,
        take_profit_stop_price: Decimal | int | float | str | None = None,
        stop_loss_stop_price: Decimal | int | float | str | None = None,
    ) -> dict[str, Any]:
        self._require_live_trading("create futures take-profit/stop-loss orders")
        body: dict[str, Any] = {"id": position_id}
        if take_profit_stop_price is not None:
            body["take_profit"] = {
                "stop_price": str(take_profit_stop_price),
                "order_type": "take_profit_market",
            }
        if stop_loss_stop_price is not None:
            body["stop_loss"] = {
                "stop_price": str(stop_loss_stop_price),
                "order_type": "stop_market",
            }
        if "take_profit" not in body and "stop_loss" not in body:
            raise ValueError("At least one take-profit or stop-loss price is required.")
        return self._request_json(
            "POST",
            self.settings.coindcx_api_base_url,
            "/exchange/v1/derivatives/futures/positions/create_tpsl",
            body=body,
            auth=True,
        )

    def edit_order(
        self,
        *,
        order_id: str,
        total_quantity: int | float,
        price: int | float,
        take_profit_price: int | float | None = None,
        stop_loss_price: int | float | None = None,
    ) -> list[dict[str, Any]]:
        self._require_live_trading("edit futures orders")
        body: dict[str, Any] = {
            "id": order_id,
            "total_quantity": total_quantity,
            "price": price,
        }
        if take_profit_price is not None:
            body["take_profit_price"] = take_profit_price
        if stop_loss_price is not None:
            body["stop_loss_price"] = stop_loss_price
        return self._request_json(
            "POST",
            self.settings.coindcx_api_base_url,
            "/exchange/v1/derivatives/futures/orders/edit",
            body=body,
            auth=True,
        )

    def update_leverage(
        self,
        *,
        pair: str,
        leverage: int,
        margin_currency: str | None = None,
    ) -> dict[str, Any]:
        self._require_live_trading("update futures leverage")
        body: dict[str, Any] = {"pair": pair, "leverage": str(leverage)}
        if margin_currency is not None:
            body["margin_currency_short_name"] = margin_currency.upper()
        return self._request_json(
            "POST",
            self.settings.coindcx_api_base_url,
            "/exchange/v1/derivatives/futures/positions/update_leverage",
            body=body,
            auth=True,
        )

    def change_margin_type(self, *, pair: str, margin_type: str) -> list[dict[str, Any]]:
        self._require_live_trading("change futures margin type")
        return self._request_json(
            "POST",
            self.settings.coindcx_api_base_url,
            "/exchange/v1/derivatives/futures/positions/margin_type",
            body={"pair": pair, "margin_type": margin_type},
            auth=True,
        )

    def _request_json(
        self,
        method: str,
        base_url: str,
        path: str,
        *,
        query: Mapping[str, Any] | None = None,
        body: Mapping[str, Any] | None = None,
        auth: bool = False,
    ) -> Any:
        response = self._request(method, base_url, path, query=query, body=body, auth=auth)
        try:
            return response.json()
        except json.JSONDecodeError as exc:
            raise CoinDCXAPIError(
                response.status_code,
                "CoinDCX returned a non-JSON response.",
                response.text,
            ) from exc

    def _request(
        self,
        method: str,
        base_url: str,
        path: str,
        *,
        query: Mapping[str, Any] | None = None,
        body: Mapping[str, Any] | None = None,
        auth: bool = False,
    ) -> HTTPResponse:
        url = self._build_url(base_url, path, query)
        base_headers: dict[str, str] = {
            "Accept": "application/json",
            "User-Agent": self.settings.http_user_agent,
        }
        if auth:
            self.settings.require_private_credentials()
        request_body = dict(body or {})
        public_data: bytes | None = None
        public_headers = dict(base_headers)
        if not auth and request_body:
            public_headers["Content-Type"] = "application/json"
            public_data = serialize_body(request_body).encode("utf-8")

        last_response: HTTPResponse | None = None
        last_network_error: CoinDCXNetworkError | None = None
        for attempt in range(self.settings.api_max_retries + 1):
            if auth:
                attempt_body = dict(body or {})
                attempt_body["timestamp"] = self.clock_ms()
                headers = dict(base_headers)
                headers.update(
                    auth_headers(
                        self.settings.coindcx_api_key,
                        self.settings.coindcx_api_secret,
                        attempt_body,
                    )
                )
                data = serialize_body(attempt_body).encode("utf-8")
            else:
                headers = public_headers
                data = public_data

            self.rate_limiter.acquire()
            try:
                response = self.transport.request(method, url, headers, data)
            except CoinDCXNetworkError as exc:
                last_network_error = exc
                if attempt < self.settings.api_max_retries:
                    delay = self.settings.api_retry_base_delay_seconds * (2**attempt)
                    self.logger.warning(
                        "Network error for CoinDCX %s %s; retrying in %.2fs: %s",
                        method.upper(),
                        path,
                        delay,
                        exc,
                    )
                    time.sleep(delay)
                    continue
                raise
            if response.status_code not in RETRYABLE_STATUS_CODES:
                return self._raise_for_status(response)

            last_response = response
            if attempt < self.settings.api_max_retries:
                delay = self.settings.api_retry_base_delay_seconds * (2**attempt)
                self.logger.warning(
                    "CoinDCX returned retryable status %s for %s %s; retrying in %.2fs",
                    response.status_code,
                    method.upper(),
                    path,
                    delay,
                )
                time.sleep(delay)

        if last_network_error is not None and last_response is None:
            raise last_network_error
        assert last_response is not None
        return self._raise_for_status(last_response)

    def _raise_for_status(self, response: HTTPResponse) -> HTTPResponse:
        if 200 <= response.status_code < 300:
            return response

        message = self._error_message(response)
        if response.status_code == 401:
            raise CoinDCXAuthError(response.status_code, message, response.text)
        if response.status_code == 429:
            raise CoinDCXRateLimitError(response.status_code, message, response.text)
        if 500 <= response.status_code:
            raise CoinDCXServerError(response.status_code, message, response.text)
        raise CoinDCXAPIError(response.status_code, message, response.text)

    @staticmethod
    def _error_message(response: HTTPResponse) -> str:
        try:
            data = response.json()
        except json.JSONDecodeError:
            return response.text[:500] or "Unknown CoinDCX error"
        if isinstance(data, dict):
            for key in ("message", "error", "display_message", "code"):
                if key in data and data[key] not in (None, ""):
                    return str(data[key])
        return str(data)

    @staticmethod
    def _build_url(
        base_url: str, path: str, query: Mapping[str, Any] | None = None
    ) -> str:
        url = f"{base_url.rstrip('/')}/{path.lstrip('/')}"
        if query:
            url = f"{url}?{urlencode(query, doseq=True)}"
        return url

    def _require_live_trading(self, action: str) -> None:
        if not self.allow_trading:
            raise LiveTradingDisabledError(
                f"Refusing to {action}: live trading requires TRADING_MODE=live and "
                "LIVE_TRADING_ENABLED=true. Keep this disabled until paper trading "
                "and risk controls are verified."
            )
