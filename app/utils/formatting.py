from __future__ import annotations

from typing import Any, Mapping
from decimal import Decimal


def format_decision_reason(data: dict[str, Any] | Any) -> str:
    """
    Converts a raw decision or signal metadata into a concise human-readable string.
    Supports both dicts (from JSON/Dashboard state) and objects.
    """
    if data is None:
        return "Unknown"

    # Convert object to dict if needed
    if not isinstance(data, dict):
        if hasattr(data, "to_dict"):
            data = data.to_dict()
        elif hasattr(data, "__dict__"):
            data = vars(data)
        else:
            return str(data)

    action = str(data.get("action", "")).upper()
    if not action and "approved" in data:
        action = "APPROVED" if data["approved"] else "REJECTED"
        
    reason_code = str(data.get("reason", ""))
    confidence = data.get("confidence")
    metadata = data.get("metadata", {}) or {}

    # 1. Map known reason codes to friendly labels
    REASON_MAP = {
        "kill_switch_active": "Kill switch active",
        "live_trading_disabled": "Live trading disabled",
        "confidence_below_threshold": "Confidence below threshold",
        "visual_rejected": "Visual confirmation failed",
        "low_volume": "Volume below requirement",
        "atr_stop_invalid": "ATR stop invalid",
        "risk_rejected": "Risk manager rejected trade",
        "duplicate_candle": "Duplicate candle already evaluated",
        "unsafe_open_position_missing_stop": "Existing live position missing protective stop",
        "position_already_open": "Position already open",
        "same_direction_position": "Same-direction position already open",
        "opposite_signal_blocked": "Opposite signal blocked by current position",
        "tpsl_sync_failed": "Exchange TPSL sync failed; manual inspection required",
        "entry_stop_distance_too_small": "Stop loss too close to entry",
        "daily_loss_limit_reached": "Daily loss limit reached",
        "insufficient_equity": "Insufficient equity for position",
    }

    friendly_reason = REASON_MAP.get(reason_code, reason_code or "Processed")

    # 2. Add context based on action
    prefix = ""
    if action == "APPROVED":
        prefix = "Approved"
    elif action in ("REJECTED", "SIGNAL_REJECTED"):
        prefix = "Rejected"
    elif action == "BLOCKED":
        prefix = "Blocked"
    elif action == "SKIPPED":
        prefix = "Skipped"

    # 3. Build detailed string
    parts = []
    if prefix:
        parts.append(prefix)
    
    if friendly_reason and friendly_reason.lower() not in prefix.lower():
        parts.append(friendly_reason)

    # 4. Add specific metadata highlights
    if confidence is not None:
        try:
            conf_val = float(str(confidence))
            parts.append(f"conf {conf_val:.2f}")
        except (ValueError, TypeError):
            pass

    if "min_confidence" in metadata:
        parts.append(f"min {metadata['min_confidence']}")

    if "side" in data:
        parts.append(str(data["side"]).upper())
    elif "direction" in data:
        parts.append(str(data["direction"]).upper())

    # Risk manager specific reasons often come in 'reason' but might be in metadata
    if reason_code == "risk_rejected" and "risk_reason" in metadata:
        parts.append(f"({metadata['risk_reason']})")
    
    if "stop_distance_pct" in metadata:
        parts.append(f"stop {metadata['stop_distance_pct']}%")

    result = ": ".join(parts) if prefix else " ".join(parts)
    
    # 5. Clean up string (remove redundant separators)
    result = result.replace(": :", ":").strip(": ")
    
    return result


def truncate_reason(reason: str, max_len: int = 120) -> str:
    """Truncates a string with ellipsis if it exceeds max_len."""
    if len(reason) <= max_len:
        return reason
    return reason[:max_len - 3] + "..."
