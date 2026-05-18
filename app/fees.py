from __future__ import annotations

from decimal import Decimal


COINDCX_INR_M_MAKER_FEE_RATE = Decimal("0.0002")
COINDCX_INR_M_TAKER_FEE_RATE = Decimal("0.0005")
COINDCX_FEE_GST_RATE = Decimal("0.18")


def effective_fee_rate(base_rate: Decimal, gst_rate: Decimal) -> Decimal:
    return base_rate * (Decimal("1") + gst_rate)
