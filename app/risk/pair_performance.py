from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.config import RiskSettings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PairRiskProfile:
    multiplier: Decimal
    metadata: dict[str, object] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

class PairPerformanceStore:
    """
    Persists per-pair PnL history to a JSON file so that the loss throttle
    survives bot restarts.

    File format:
        {
            "B-BTC_USDT": ["-150.50", "200.00", "-80.00"],
            ...
        }

    All values are stored as strings to preserve Decimal precision.
    """

    def __init__(self, path: str | os.PathLike = "pair_performance.json") -> None:
        self._path = Path(path)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def load_pnls(self, pair: str) -> list[Decimal]:
        """Return the stored PnL history for *pair* (newest last).  Empty list if none."""
        data = self._read_file()
        raw = data.get(pair, [])
        try:
            return [Decimal(str(v)) for v in raw]
        except Exception:
            logger.warning(
                "PairPerformanceStore: could not parse PnL history for %s; resetting.", pair
            )
            return []

    def append_pnl(self, pair: str, net_pnl: Decimal, *, maxlen: int = 200) -> None:
        """Append *net_pnl* to the history for *pair*, capping at *maxlen* entries."""
        data = self._read_file()
        history: list[str] = [str(v) for v in data.get(pair, [])]
        history.append(str(net_pnl))
        if len(history) > maxlen:
            history = history[-maxlen:]
        data[pair] = history
        self._write_file(data)

    def clear_pair(self, pair: str) -> None:
        """Remove all stored history for *pair*."""
        data = self._read_file()
        data.pop(pair, None)
        self._write_file(data)

    def clear_all(self) -> None:
        """Wipe the entire store (e.g. on a new trading session)."""
        self._write_file({})

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _read_file(self) -> dict[str, Any]:
        if not self._path.exists():
            return {}
        try:
            return json.loads(self._path.read_text(encoding="utf-8"))
        except Exception:
            logger.warning(
                "PairPerformanceStore: could not read %s; treating as empty.", self._path
            )
            return {}

    def _write_file(self, data: dict[str, Any]) -> None:
        try:
            self._path.write_text(
                json.dumps(data, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception:
            logger.exception(
                "PairPerformanceStore: failed to write %s; performance history not saved.",
                self._path,
            )


# ---------------------------------------------------------------------------
# Core throttle logic
# ---------------------------------------------------------------------------

def pair_recent_risk_profile(
    recent_net_pnls: list[Decimal],
    settings: RiskSettings,
) -> PairRiskProfile:
    """
    Return a PairRiskProfile based on recent per-pair PnL history.

    Throttle levels
    ---------------
    * losses >= pair_loss_limit        → apply pair_loss_risk_multiplier
    * losses >= pair_loss_severe_limit → apply pair_loss_severe_risk_multiplier
      (severe always fires alongside normal; min() keeps whichever is stricter)

    The function is pure — it does not touch the store.  The caller is
    responsible for loading and persisting history via PairPerformanceStore.
    """
    if not settings.pair_loss_throttle_enabled:
        return PairRiskProfile(Decimal("1"))

    # --- BUG FIX: guard against invalid settings at call-time ---------------
    # (ideally validated in RiskSettings.__post_init__, but belt-and-suspenders)
    if settings.pair_loss_severe_limit <= settings.pair_loss_limit:
        logger.warning(
            "pair_loss_severe_limit (%d) must be greater than pair_loss_limit (%d); "
            "severe throttle will fire before normal throttle — check RiskSettings.",
            settings.pair_loss_severe_limit,
            settings.pair_loss_limit,
        )

    lookback = max(settings.pair_loss_lookback, 0)
    if lookback == 0:
        return PairRiskProfile(Decimal("1"))

    window = recent_net_pnls[-lookback:]
    if not window:
        return PairRiskProfile(Decimal("1"))

    losses = sum(1 for pnl in window if pnl < 0)
    total = sum(window, Decimal("0"))

    multiplier = Decimal("1")

    if losses >= max(settings.pair_loss_limit, 1):
        multiplier = min(multiplier, _bounded_multiplier(settings.pair_loss_risk_multiplier))

    if losses >= max(settings.pair_loss_severe_limit, 1):
        multiplier = min(
            multiplier,
            _bounded_multiplier(settings.pair_loss_severe_risk_multiplier),
        )

    # --- BUG FIX: was `multiplier >= 1` (int), which is identical to `>= Decimal("1")`
    # in CPython but breaks if _bounded_multiplier ever returns exactly Decimal("1")
    # because a multiplier of exactly 1 should still be flagged as "throttle active"
    # when a throttle threshold WAS crossed (i.e. multiplier was set by a min() call).
    # Solution: track whether any threshold fired independently of the final multiplier.
    # -------------------------------------------------------------------------
    throttle_active = losses >= max(settings.pair_loss_limit, 1)

    base_metadata: dict[str, object] = {
        "pair_recent_trade_count": len(window),
        "pair_recent_loss_count": losses,
        "pair_recent_net_pnl": total,
    }

    if not throttle_active:
        return PairRiskProfile(Decimal("1"), base_metadata)

    return PairRiskProfile(
        multiplier,
        {
            **base_metadata,
            "pair_risk_throttle_active": True,
            "pair_risk_multiplier": multiplier,
            "pair_loss_limit_crossed": losses >= max(settings.pair_loss_limit, 1),
            "pair_severe_limit_crossed": losses >= max(settings.pair_loss_severe_limit, 1),
        },
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _bounded_multiplier(value: Decimal) -> Decimal:
    """
    Clamp a configured risk multiplier to the half-open interval (0, 1).

    BUG FIX: was `min(value, Decimal("1"))` which allowed a return of exactly
    Decimal("1"), making the throttle visually "active" but having no effect and
    then being incorrectly routed to the no-throttle path by the old `>= 1` check.
    Now the upper bound is exclusive: any value >= 1 is treated as misconfigured
    and clamped down to Decimal("0.99") with a warning, keeping the throttle
    semantically active.
    """
    if value <= Decimal("0"):
        logger.warning(
            "_bounded_multiplier received non-positive value %s; clamping to 0.01.", value
        )
        return Decimal("0.01")

    if value >= Decimal("1"):
        logger.warning(
            "_bounded_multiplier received value %s >= 1; a throttle multiplier must be "
            "strictly less than 1 to have any effect.  Clamping to 0.99.",
            value,
        )
        return Decimal("0.99")

    return value