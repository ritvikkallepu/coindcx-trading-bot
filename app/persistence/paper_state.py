from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any


class PaperStateStore:
    def __init__(self, db_path: str = "paper_state.db") -> None:
        self.db_path = db_path
        self._init_db()

    def _init_db(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            # Paper trading: drop and recreate on schema change to ensure columns exist.
            conn.execute("DROP TABLE IF EXISTS broker_state")
            conn.execute(
                """
                CREATE TABLE broker_state (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    equity TEXT,
                    positions TEXT,
                    fills TEXT,
                    daily_pnl TEXT,
                    daily_limit_equity TEXT,
                    fees_paid TEXT,
                    funding_paid TEXT,
                    saved_at TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS strategy_state (
                    key TEXT PRIMARY KEY,
                    state TEXT
                )
                """
            )
            conn.commit()

    def _serialize_decimal(self, obj: Any) -> Any:
        from dataclasses import asdict, is_dataclass

        if is_dataclass(obj):
            obj = asdict(obj)
        if isinstance(obj, Decimal):
            return str(obj)
        if isinstance(obj, dict):
            return {k: self._serialize_decimal(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self._serialize_decimal(i) for i in obj]
        return obj

    def _deserialize_decimal(self, obj: Any) -> Any:
        if isinstance(obj, str):
            s = obj.lstrip("-")
            if s.isdigit() or ("." in s and s.replace(".", "", 1).isdigit()):
                try:
                    return Decimal(obj)
                except Exception:
                    pass
        if isinstance(obj, dict):
            return {k: self._deserialize_decimal(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self._deserialize_decimal(i) for i in obj]
        return obj

    def save(self, snapshot: dict[str, Any]) -> None:
        equity = str(snapshot.get("equity", "0"))
        positions = json.dumps(self._serialize_decimal(snapshot.get("positions", {})))
        fills = json.dumps(self._serialize_decimal(snapshot.get("fills", [])))
        daily_pnl = str(snapshot.get("realized_pnl", snapshot.get("daily_pnl", "0")))
        daily_limit_equity = str(snapshot.get("daily_limit_equity", "0"))
        fees_paid = str(snapshot.get("fees_paid", "0"))
        funding_paid = str(snapshot.get("funding_paid", "0"))
        saved_at = datetime.now(timezone.utc).isoformat()

        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO broker_state (
                    id, equity, positions, fills, daily_pnl, 
                    daily_limit_equity, fees_paid, funding_paid, saved_at
                )
                VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    equity=excluded.equity,
                    positions=excluded.positions,
                    fills=excluded.fills,
                    daily_pnl=excluded.daily_pnl,
                    daily_limit_equity=excluded.daily_limit_equity,
                    fees_paid=excluded.fees_paid,
                    funding_paid=excluded.funding_paid,
                    saved_at=excluded.saved_at
                """,
                (
                    equity,
                    positions,
                    fills,
                    daily_pnl,
                    daily_limit_equity,
                    fees_paid,
                    funding_paid,
                    saved_at,
                ),
            )
            conn.commit()

    def load(self) -> dict[str, Any] | None:
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                """
                SELECT equity, positions, fills, daily_pnl, daily_limit_equity, 
                       fees_paid, funding_paid 
                FROM broker_state WHERE id = 1
                """
            ).fetchone()

        if not row:
            return None

        (
            equity,
            positions_json,
            fills_json,
            daily_pnl,
            daily_limit_equity,
            fees_paid,
            funding_paid,
        ) = row

        return {
            "equity": Decimal(equity),
            "positions": json.loads(positions_json),
            "fills": json.loads(fills_json),
            "daily_pnl": Decimal(daily_pnl),
            "realized_pnl": Decimal(daily_pnl),
            "daily_limit_equity": Decimal(daily_limit_equity),
            "fees_paid": Decimal(fees_paid),
            "funding_paid": Decimal(funding_paid),
        }

    def _restore_decimals(self, obj: Any) -> Any:
        if isinstance(obj, str):
            s = obj.lstrip("-")
            if s.isdigit() or ("." in s and s.replace(".", "", 1).isdigit()):
                try:
                    return Decimal(obj)
                except Exception:
                    pass
            return obj
        if isinstance(obj, dict):
            return {k: self._restore_decimals(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self._restore_decimals(i) for i in obj]
        return obj

    def _restore_position(self, d: dict[str, Any]) -> PaperPosition:
        from app.broker.models import PaperPosition
        from app.strategies.base import SignalDirection

        d = self._restore_decimals(d)
        # Handle Enum
        direction = SignalDirection(d["direction"])
        return PaperPosition(
            pair=d["pair"],
            direction=direction,
            quantity=d["quantity"],
            entry_price=d["entry_price"],
            leverage=d["leverage"],
            opened_at_ms=d["opened_at_ms"],
            updated_at_ms=d["updated_at_ms"],
            strategy_name=d["strategy_name"],
            stop_loss=d.get("stop_loss"),
            take_profit=d.get("take_profit"),
            metadata=d.get("metadata", {}),
        )

    def _restore_fill(self, d: dict[str, Any]) -> PaperFill:
        from app.broker.models import PaperFill, PaperOrderSide

        d = self._restore_decimals(d)
        return PaperFill(
            fill_id=d["fill_id"],
            order_id=d["order_id"],
            pair=d["pair"],
            side=PaperOrderSide(d["side"]),
            quantity=d["quantity"],
            price=d["price"],
            fee=d["fee"],
            timestamp_ms=d["timestamp_ms"],
            realized_pnl=d.get("realized_pnl", Decimal("0")),
            metadata=d.get("metadata", {}),
        )

    def clear(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("DELETE FROM broker_state")
            conn.execute("DELETE FROM strategy_state")
            conn.commit()

    def save_strategy_state(self, key: str, state: dict[str, Any]) -> None:
        state_json = json.dumps(self._serialize_decimal(state))
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO strategy_state (key, state)
                VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET state=excluded.state
                """,
                (key, state_json),
            )
            conn.commit()

    def load_strategy_state(self, key: str) -> dict[str, Any] | None:
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT state FROM strategy_state WHERE key = ?", (key,)
            ).fetchone()
            
        if not row:
            return None
            
        return self._restore_decimals(json.loads(row[0]))

    def close(self) -> None:
        # On some OSs (like Windows), open sqlite handles can prevent file deletion.
        # Since we use 'with sqlite3.connect' inside methods, we don't store a long-lived conn.
        # But we can force a GC or just wait. 
        import gc
        gc.collect()
