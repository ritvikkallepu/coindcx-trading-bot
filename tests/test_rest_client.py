from __future__ import annotations

import json
import logging
import unittest

from app.config import Settings
from app.exchange.coindcx_rest import CoinDCXFuturesClient, HTTPResponse
from app.exchange.errors import CoinDCXNetworkError, LiveTradingDisabledError
from app.exchange.models import FuturesOrderRequest
from app.exchange.signing import sign_payload
from decimal import Decimal


class FakeTransport:
    def __init__(self, response: HTTPResponse | None = None) -> None:
        self.calls: list[dict[str, object]] = []
        self.response = response or HTTPResponse(200, "[]", {})

    def request(self, method: str, url: str, headers: dict[str, str], data: bytes | None):
        self.calls.append(
            {"method": method, "url": url, "headers": headers, "data": data}
        )
        return self.response


class FlakyTransport:
    def __init__(self) -> None:
        self.calls = 0

    def request(self, method: str, url: str, headers: dict[str, str], data: bytes | None):
        self.calls += 1
        if self.calls == 1:
            raise CoinDCXNetworkError("temporary network failure")
        return HTTPResponse(200, '["B-BTC_USDT"]', {})


class RetryablePrivateTransport:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def request(self, method: str, url: str, headers: dict[str, str], data: bytes | None):
        self.calls.append(
            {"method": method, "url": url, "headers": headers, "data": data}
        )
        if len(self.calls) == 1:
            return HTTPResponse(429, '{"message":"retry"}', {})
        return HTTPResponse(200, "[]", {})


class NoSleepLimiter:
    def acquire(self) -> None:
        return None


class CoinDCXFuturesClientTests(unittest.TestCase):
    def test_public_active_instruments_uses_futures_endpoint(self) -> None:
        transport = FakeTransport(HTTPResponse(200, '["B-BTC_USDT"]', {}))
        client = CoinDCXFuturesClient(
            Settings(), transport=transport, rate_limiter=NoSleepLimiter()
        )

        self.assertEqual(client.get_active_instruments("USDT"), ["B-BTC_USDT"])
        call = transport.calls[0]
        self.assertEqual(call["method"], "GET")
        self.assertEqual(call["headers"]["User-Agent"], "CoinDCXFuturesBot/0.1")  # type: ignore[index]
        self.assertIn(
            "/exchange/v1/derivatives/futures/data/active_instruments",
            str(call["url"]),
        )
        self.assertIn("margin_currency_short_name%5B%5D=USDT", str(call["url"]))

    def test_private_request_adds_timestamp_and_signature(self) -> None:
        settings = Settings(coindcx_api_key="key", coindcx_api_secret="secret")
        transport = FakeTransport(HTTPResponse(200, '[{"currency_short_name":"USDT"}]', {}))
        client = CoinDCXFuturesClient(
            settings,
            transport=transport,
            rate_limiter=NoSleepLimiter(),
            clock_ms=lambda: 1700000000000,
        )

        client.get_wallets()
        call = transport.calls[0]
        body = json.loads(call["data"].decode("utf-8"))  # type: ignore[union-attr]
        self.assertEqual(body, {"timestamp": 1700000000000})
        headers = call["headers"]
        self.assertEqual(headers["X-AUTH-APIKEY"], "key")  # type: ignore[index]
        self.assertEqual(
            headers["X-AUTH-SIGNATURE"],  # type: ignore[index]
            sign_payload("secret", {"timestamp": 1700000000000}),
        )

    def test_auth_smoke_uses_user_info_endpoint(self) -> None:
        settings = Settings(coindcx_api_key="key", coindcx_api_secret="secret")
        transport = FakeTransport(HTTPResponse(200, '{"email":"masked"}', {}))
        client = CoinDCXFuturesClient(
            settings,
            transport=transport,
            rate_limiter=NoSleepLimiter(),
            clock_ms=lambda: 1700000000000,
        )

        self.assertEqual(client.get_user_info(), {"email": "masked"})
        call = transport.calls[0]
        self.assertEqual(call["method"], "POST")
        self.assertIn("/exchange/v1/users/info", str(call["url"]))

    def test_private_pagination_uses_numbers(self) -> None:
        settings = Settings(coindcx_api_key="key", coindcx_api_secret="secret")
        transport = FakeTransport(HTTPResponse(200, "[]", {}))
        client = CoinDCXFuturesClient(
            settings,
            transport=transport,
            rate_limiter=NoSleepLimiter(),
            clock_ms=lambda: 1700000000000,
        )

        client.list_positions(page=2, size=25, margin_currencies=["USDT"])
        call = transport.calls[0]
        body = json.loads(call["data"].decode("utf-8"))  # type: ignore[union-attr]
        self.assertEqual(body["page"], 2)
        self.assertEqual(body["size"], 25)

    def test_list_positions_can_filter_by_pairs_or_position_ids(self) -> None:
        settings = Settings(coindcx_api_key="key", coindcx_api_secret="secret")
        transport = FakeTransport(HTTPResponse(200, "[]", {}))
        client = CoinDCXFuturesClient(
            settings,
            transport=transport,
            rate_limiter=NoSleepLimiter(),
            clock_ms=lambda: 1700000000000,
        )

        client.list_positions(
            pairs=["B-BTC_USDT", "B-ETH_USDT"],
            position_ids=["pos-1"],
        )
        body = json.loads(transport.calls[0]["data"].decode("utf-8"))  # type: ignore[union-attr]
        self.assertEqual(body["pairs"], "B-BTC_USDT,B-ETH_USDT")
        self.assertEqual(body["position_ids"], "pos-1")

    def test_live_order_is_blocked_by_default(self) -> None:
        settings = Settings(
            trading_mode="paper",
            live_trading_enabled=True,
            coindcx_api_key="key",
            coindcx_api_secret="secret",
        )
        client = CoinDCXFuturesClient(
            settings,
            transport=FakeTransport(),
            rate_limiter=NoSleepLimiter(),
        )
        order = FuturesOrderRequest(
            side="buy",
            pair="B-BTC_USDT",
            order_type="market_order",
            total_quantity=Decimal("0.01"),
            time_in_force=None,
        )

        with self.assertRaises(LiveTradingDisabledError):
            client.place_order(order)

    def test_live_order_allowed_only_when_mode_is_live_and_flag_enabled(self) -> None:
        settings = Settings(
            trading_mode="live",
            live_trading_enabled=True,
            coindcx_api_key="key",
            coindcx_api_secret="secret",
        )
        transport = FakeTransport(HTTPResponse(200, '[{"id":"order-1"}]', {}))
        client = CoinDCXFuturesClient(
            settings,
            transport=transport,
            rate_limiter=NoSleepLimiter(),
            clock_ms=lambda: 1700000000000,
        )
        order = FuturesOrderRequest(
            side="buy",
            pair="B-BTC_USDT",
            order_type="market_order",
            total_quantity=Decimal("0.01"),
            time_in_force=None,
        )

        self.assertEqual(client.place_order(order), [{"id": "order-1"}])

    def test_cancel_all_open_orders_uses_inr_margin_currency(self) -> None:
        settings = Settings(
            trading_mode="live",
            live_trading_enabled=True,
            coindcx_api_key="key",
            coindcx_api_secret="secret",
            futures_margin_currency="INR",
        )
        transport = FakeTransport(HTTPResponse(200, '{"message":"success"}', {}))
        client = CoinDCXFuturesClient(
            settings,
            transport=transport,
            rate_limiter=NoSleepLimiter(),
            clock_ms=lambda: 1700000000000,
        )

        self.assertEqual(client.cancel_all_open_orders(), {"message": "success"})
        body = json.loads(transport.calls[0]["data"].decode("utf-8"))  # type: ignore[union-attr]
        self.assertEqual(body["margin_currency_short_name"], ["INR"])

    def test_create_position_tpsl_uses_market_stop_payloads(self) -> None:
        settings = Settings(
            trading_mode="live",
            live_trading_enabled=True,
            coindcx_api_key="key",
            coindcx_api_secret="secret",
        )
        transport = FakeTransport(HTTPResponse(200, '{"success":true}', {}))
        client = CoinDCXFuturesClient(
            settings,
            transport=transport,
            rate_limiter=NoSleepLimiter(),
            clock_ms=lambda: 1700000000000,
        )

        self.assertEqual(
            client.create_position_tpsl(
                position_id="pos-1",
                take_profit_stop_price=Decimal("110"),
                stop_loss_stop_price=Decimal("95"),
            ),
            {"success": True},
        )
        body = json.loads(transport.calls[0]["data"].decode("utf-8"))  # type: ignore[union-attr]
        self.assertEqual(body["id"], "pos-1")
        self.assertEqual(body["take_profit"]["order_type"], "take_profit_market")
        self.assertEqual(body["take_profit"]["stop_price"], "110")
        self.assertEqual(body["stop_loss"]["order_type"], "stop_market")
        self.assertEqual(body["stop_loss"]["stop_price"], "95")

    def test_network_errors_are_retried(self) -> None:
        settings = Settings(api_max_retries=1, api_retry_base_delay_seconds=0)
        transport = FlakyTransport()
        logger = logging.getLogger("test.rest.retry")
        logger.disabled = True
        client = CoinDCXFuturesClient(
            settings,
            transport=transport,
            rate_limiter=NoSleepLimiter(),
            logger=logger,
        )

        self.assertEqual(client.get_active_instruments("USDT"), ["B-BTC_USDT"])
        self.assertEqual(transport.calls, 2)

    def test_private_retries_regenerate_timestamp_and_signature(self) -> None:
        timestamps = iter([1700000000000, 1700000001000])
        settings = Settings(
            api_max_retries=1,
            api_retry_base_delay_seconds=0,
            coindcx_api_key="key",
            coindcx_api_secret="secret",
        )
        transport = RetryablePrivateTransport()
        logger = logging.getLogger("test.rest.private.retry")
        logger.disabled = True
        client = CoinDCXFuturesClient(
            settings,
            transport=transport,
            rate_limiter=NoSleepLimiter(),
            clock_ms=lambda: next(timestamps),
            logger=logger,
        )

        self.assertEqual(client.get_wallets(), [])
        first_body = json.loads(transport.calls[0]["data"].decode("utf-8"))  # type: ignore[union-attr]
        second_body = json.loads(transport.calls[1]["data"].decode("utf-8"))  # type: ignore[union-attr]

        self.assertEqual(first_body["timestamp"], 1700000000000)
        self.assertEqual(second_body["timestamp"], 1700000001000)
        self.assertNotEqual(
            transport.calls[0]["headers"]["X-AUTH-SIGNATURE"],  # type: ignore[index]
            transport.calls[1]["headers"]["X-AUTH-SIGNATURE"],  # type: ignore[index]
        )


if __name__ == "__main__":
    unittest.main()
