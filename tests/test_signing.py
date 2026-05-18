from __future__ import annotations

import hashlib
import hmac
import unittest

from app.exchange.signing import auth_headers, serialize_body, sign_payload


class SigningTests(unittest.TestCase):
    def test_serialize_body_is_compact_json(self) -> None:
        self.assertEqual(
            serialize_body({"timestamp": 1700000000000, "page": "1"}),
            '{"timestamp":1700000000000,"page":"1"}',
        )

    def test_sign_payload_matches_hmac_sha256_hex(self) -> None:
        body = {"timestamp": 1700000000000}
        expected = hmac.new(
            b"secret",
            b'{"timestamp":1700000000000}',
            hashlib.sha256,
        ).hexdigest()
        self.assertEqual(sign_payload("secret", body), expected)

    def test_auth_headers_include_coin_dcx_auth_keys(self) -> None:
        headers = auth_headers("api-key", "secret", {"timestamp": 1700000000000})
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertEqual(headers["X-AUTH-APIKEY"], "api-key")
        self.assertTrue(headers["X-AUTH-SIGNATURE"])


if __name__ == "__main__":
    unittest.main()

