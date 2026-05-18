from __future__ import annotations


class CoinDCXError(Exception):
    """Base error for CoinDCX client failures."""


class CoinDCXNetworkError(CoinDCXError):
    """Raised when the HTTP transport cannot reach CoinDCX."""


class CoinDCXAPIError(CoinDCXError):
    """Raised for non-retryable API responses."""

    def __init__(self, status_code: int, message: str, response_text: str = "") -> None:
        self.status_code = status_code
        self.response_text = response_text
        super().__init__(f"CoinDCX API error {status_code}: {message}")


class CoinDCXAuthError(CoinDCXAPIError):
    """Raised for authentication or authorization failures."""


class CoinDCXRateLimitError(CoinDCXAPIError):
    """Raised when CoinDCX rate limits the client."""


class CoinDCXServerError(CoinDCXAPIError):
    """Raised when CoinDCX returns repeated 5xx responses."""


class LiveTradingDisabledError(CoinDCXError):
    """Raised when a live mutation is attempted while live trading is disabled."""

