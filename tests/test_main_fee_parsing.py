from __future__ import annotations

import unittest
from decimal import Decimal

from app.main import _fee_rate_from_args


class MainFeeParsingTests(unittest.TestCase):
    def test_percent_like_fee_rate_is_normalized(self) -> None:
        self.assertEqual(
            _fee_rate_from_args(
                fee_rate=Decimal("0.02"),
                fee_pct=None,
                default=Decimal("0"),
            ),
            Decimal("0.0002"),
        )

    def test_fractional_fee_rate_is_kept_as_rate(self) -> None:
        self.assertEqual(
            _fee_rate_from_args(
                fee_rate=Decimal("0.0002"),
                fee_pct=None,
                default=Decimal("0"),
            ),
            Decimal("0.0002"),
        )

    def test_explicit_fee_pct_is_normalized(self) -> None:
        self.assertEqual(
            _fee_rate_from_args(
                fee_rate=None,
                fee_pct=Decimal("0.02"),
                default=Decimal("0"),
            ),
            Decimal("0.0002"),
        )


if __name__ == "__main__":
    unittest.main()
