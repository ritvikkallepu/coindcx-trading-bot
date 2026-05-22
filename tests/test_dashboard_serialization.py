from __future__ import annotations

import json
from decimal import Decimal
from datetime import datetime, timezone
from dataclasses import dataclass
import pytest
from app.utils.json import to_jsonable, safe_json_dumps

@dataclass
class MockData:
    name: str
    value: Decimal
    timestamp: datetime

def test_to_jsonable_handles_decimal():
    data = {"price": Decimal("100.50")}
    result = to_jsonable(data)
    assert result == {"price": "100.50"}

def test_to_jsonable_handles_datetime():
    ts = datetime(2026, 5, 22, 10, 0, 0, tzinfo=timezone.utc)
    data = {"timestamp": ts}
    result = to_jsonable(data)
    assert result == {"timestamp": "2026-05-22T10:00:00+00:00"}

def test_to_jsonable_handles_dataclass():
    ts = datetime(2026, 5, 22, 10, 0, 0, tzinfo=timezone.utc)
    data = MockData(name="test", value=Decimal("1.23"), timestamp=ts)
    result = to_jsonable(data)
    assert result == {
        "name": "test",
        "value": "1.23",
        "timestamp": "2026-05-22T10:00:00+00:00"
    }

def test_safe_json_dumps():
    ts = datetime(2026, 5, 22, 10, 0, 0, tzinfo=timezone.utc)
    data = {
        "price": Decimal("100.50"),
        "timestamp": ts,
        "nested": {"val": Decimal("0.1")}
    }
    dumped = safe_json_dumps(data)
    loaded = json.loads(dumped)
    assert loaded["price"] == "100.50"
    assert loaded["timestamp"] == "2026-05-22T10:00:00+00:00"
    assert loaded["nested"]["val"] == "0.1"

def test_paper_status_payload_serialization():
    # Simulate the kind of payload get_live_state returns
    payload = {
        "candle_count": 10,
        "equity": Decimal("1050.25"),
        "positions": [
            {
                "pair": "B-BTC_USDT",
                "unrealized_pnl": Decimal("50.25")
            }
        ],
        "last_updated": datetime.now(timezone.utc)
    }
    # This should not raise TypeError
    dumped = safe_json_dumps(payload)
    assert "1050.25" in dumped
    assert "50.25" in dumped

@pytest.mark.parametrize("value", [
    Decimal("1.0"),
    datetime.now(timezone.utc),
    [Decimal("1.0"), Decimal("2.0")],
    {"a": Decimal("1.0")},
    (Decimal("1.0"),),
])
def test_various_types_serialization(value):
    # This should not raise TypeError
    safe_json_dumps({"val": value})
