from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Mapping
from typing import Any


def serialize_body(body: Mapping[str, Any]) -> str:
    """Serialize exactly like CoinDCX examples: compact JSON, no added spaces."""
    return json.dumps(body, separators=(",", ":"))


def sign_payload(secret: str, body: Mapping[str, Any]) -> str:
    json_body = serialize_body(body)
    secret_bytes = secret.encode("utf-8")
    return hmac.new(secret_bytes, json_body.encode("utf-8"), hashlib.sha256).hexdigest()


def auth_headers(api_key: str, api_secret: str, body: Mapping[str, Any]) -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "X-AUTH-APIKEY": api_key,
        "X-AUTH-SIGNATURE": sign_payload(api_secret, body),
    }

