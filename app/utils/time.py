from __future__ import annotations

import time


def utc_timestamp_ms() -> int:
    return int(round(time.time() * 1000))

