from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from contextlib import closing
from datetime import datetime, timezone
from decimal import Decimal
from tempfile import NamedTemporaryFile
from typing import Any
from app.utils.json import to_jsonable


_SESSION_SAVE_LOCK = threading.Lock()
_STRING_METADATA_KEY_HINTS = ("id", "pair", "symbol", "strategy", "reason", "type", "mode", "currency", "unit")


def _decimal_field(values: dict[str, Any], key: str) -> Decimal:
    return Decimal(str(values[key]))


def _optional_decimal_field(values: dict[str, Any], key: str) -> Decimal | None:
    value = values.get(key)
    if value is None or value == "":
        return None
    return Decimal(str(value))


class PaperStateStore:
    def __init__(self, db_path: str = "paper_state.db") -> None:
        self.db_path = db_path
        self._init_db()

    def _init_db(self) -> None:
        # Task: Ensure schema is stable but allows updates
        with closing(sqlite3.connect(self.db_path)) as conn:
            # We don't DROP if we want persistence across restarts
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS broker_state (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    equity TEXT,
                    positions TEXT,
                    fills TEXT,
                    daily_pnl TEXT,
                    daily_limit_equity TEXT,
                    fees_paid TEXT,
                    funding_paid TEXT,
                    initial_equity TEXT,
                    locked_profit TEXT,
                    unlocked_profit TEXT,
                    tradable_base TEXT,
                    daily_tradable_base_start TEXT,
                    daily_loss_from_tradable_base TEXT,
                    profit_lock_enabled INTEGER,
                    auto_lock_profit_pct TEXT,
                    protected_profit_override_enabled INTEGER,
                    last_pnl_reset_day TEXT,
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
            # Migration: add missing columns if table already existed
            cursor = conn.execute("PRAGMA table_info(broker_state)")
            columns = {row[1] for row in cursor.fetchall()}
            
            new_cols = {
                "initial_equity": "TEXT",
                "locked_profit": "TEXT",
                "unlocked_profit": "TEXT",
                "tradable_base": "TEXT",
                "daily_tradable_base_start": "TEXT",
                "daily_loss_from_tradable_base": "TEXT",
                "profit_lock_enabled": "INTEGER",
                "auto_lock_profit_pct": "TEXT",
                "protected_profit_override_enabled": "INTEGER",
                "last_pnl_reset_day": "TEXT"
            }
            for col, col_type in new_cols.items():
                if col not in columns:
                    conn.execute(f"ALTER TABLE broker_state ADD COLUMN {col} {col_type}")
            
            conn.commit()

    def _serialize_decimal(self, obj: Any) -> Any:
        from dataclasses import asdict, is_dataclass
        from enum import Enum

        if is_dataclass(obj):
            obj = asdict(obj)
        if isinstance(obj, Enum):
            return obj.value
        if isinstance(obj, Decimal):
            return str(obj)
        if isinstance(obj, dict):
            return {k: self._serialize_decimal(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self._serialize_decimal(i) for i in obj]
        return obj

    def _deserialize_decimal(self, obj: Any, *, key: str = "") -> Any:
        if isinstance(obj, str):
            normalized_key = key.lower()
            if any(hint in normalized_key for hint in _STRING_METADATA_KEY_HINTS):
                return obj
            s = obj.lstrip("-")
            if s.isdigit() or ("." in s and s.replace(".", "", 1).isdigit()):
                try:
                    return Decimal(obj)
                except Exception:
                    pass
        if isinstance(obj, dict):
            return {k: self._deserialize_decimal(v, key=str(k)) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self._deserialize_decimal(i, key=key) for i in obj]
        return obj

    def save(self, snapshot: dict[str, Any]) -> None:
        equity = str(snapshot.get("equity", "0"))
        positions = json.dumps(to_jsonable(snapshot.get("positions", {})))
        fills = json.dumps(to_jsonable(snapshot.get("fills", [])))
        daily_pnl = str(snapshot.get("realized_pnl", snapshot.get("daily_pnl", "0")))
        daily_limit_equity = str(snapshot.get("daily_limit_equity", "0"))
        fees_paid = str(snapshot.get("fees_paid", "0"))
        funding_paid = str(snapshot.get("funding_paid", "0"))
        
        initial_equity = str(snapshot.get("initial_equity", "0"))
        locked_profit = str(snapshot.get("locked_profit", "0"))
        unlocked_profit = str(snapshot.get("unlocked_profit", "0"))
        tradable_base = str(snapshot.get("tradable_base", "0"))
        daily_tradable_base_start = str(snapshot.get("daily_tradable_base_start", tradable_base))
        daily_loss_from_tradable_base = str(snapshot.get("daily_loss_from_tradable_base", "0"))
        profit_lock_enabled = 1 if snapshot.get("profit_lock_enabled", True) else 0
        auto_lock_profit_pct = str(snapshot.get("auto_lock_profit_pct", "100"))
        protected_profit_override_enabled = 1 if snapshot.get("protected_profit_override_enabled", False) else 0
        last_pnl_reset_day = str(snapshot.get("last_pnl_reset_day", ""))
        
        saved_at = datetime.now(timezone.utc).isoformat()

        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute(
                """
                INSERT INTO broker_state (
                    id, equity, positions, fills, daily_pnl, 
                    daily_limit_equity, fees_paid, funding_paid,
                    initial_equity, locked_profit, unlocked_profit,
                    tradable_base, daily_tradable_base_start, daily_loss_from_tradable_base,
                    profit_lock_enabled, auto_lock_profit_pct,
                    protected_profit_override_enabled, last_pnl_reset_day,
                    saved_at
                )
                VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    equity=excluded.equity,
                    positions=excluded.positions,
                    fills=excluded.fills,
                    daily_pnl=excluded.daily_pnl,
                    daily_limit_equity=excluded.daily_limit_equity,
                    fees_paid=excluded.fees_paid,
                    funding_paid=excluded.funding_paid,
                    initial_equity=excluded.initial_equity,
                    locked_profit=excluded.locked_profit,
                    unlocked_profit=excluded.unlocked_profit,
                    tradable_base=excluded.tradable_base,
                    daily_tradable_base_start=excluded.daily_tradable_base_start,
                    daily_loss_from_tradable_base=excluded.daily_loss_from_tradable_base,
                    profit_lock_enabled=excluded.profit_lock_enabled,
                    auto_lock_profit_pct=excluded.auto_lock_profit_pct,
                    protected_profit_override_enabled=excluded.protected_profit_override_enabled,
                    last_pnl_reset_day=excluded.last_pnl_reset_day,
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
                    initial_equity,
                    locked_profit,
                    unlocked_profit,
                    tradable_base,
                    daily_tradable_base_start,
                    daily_loss_from_tradable_base,
                    profit_lock_enabled,
                    auto_lock_profit_pct,
                    protected_profit_override_enabled,
                    last_pnl_reset_day,
                    saved_at,
                ),
            )
            conn.commit()

    def load(self) -> dict[str, Any] | None:
        with closing(sqlite3.connect(self.db_path)) as conn:
            row = conn.execute(
                """
                SELECT equity, positions, fills, daily_pnl, daily_limit_equity, 
                       fees_paid, funding_paid, initial_equity, locked_profit,
                       unlocked_profit, tradable_base, daily_tradable_base_start,
                       daily_loss_from_tradable_base,
                       profit_lock_enabled, auto_lock_profit_pct,
                       protected_profit_override_enabled, last_pnl_reset_day
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
            initial_equity,
            locked_profit,
            unlocked_profit,
            tradable_base,
            daily_tradable_base_start,
            daily_loss_from_tradable_base,
            profit_lock_enabled,
            auto_lock_profit_pct,
            protected_profit_override_enabled,
            last_pnl_reset_day,
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
            "initial_equity": Decimal(initial_equity) if initial_equity else Decimal("0"),
            "locked_profit": Decimal(locked_profit) if locked_profit else Decimal("0"),
            "unlocked_profit": Decimal(unlocked_profit) if unlocked_profit else Decimal("0"),
            "tradable_base": Decimal(tradable_base) if tradable_base else Decimal("0"),
            "daily_tradable_base_start": Decimal(daily_tradable_base_start) if daily_tradable_base_start else (Decimal(tradable_base) if tradable_base else Decimal("0")),
            "daily_loss_from_tradable_base": Decimal(daily_loss_from_tradable_base) if daily_loss_from_tradable_base else Decimal("0"),
            "profit_lock_enabled": bool(profit_lock_enabled),
            "auto_lock_profit_pct": Decimal(auto_lock_profit_pct) if auto_lock_profit_pct else Decimal("100"),
            "protected_profit_override_enabled": bool(protected_profit_override_enabled),
            "last_pnl_reset_day": str(last_pnl_reset_day) if last_pnl_reset_day else "",
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

    def _restore_metadata(self, obj: Any, *, key: str = "") -> Any:
        if isinstance(obj, str):
            normalized_key = key.lower()
            if any(hint in normalized_key for hint in _STRING_METADATA_KEY_HINTS):
                return obj
            return self._restore_decimals(obj)
        if isinstance(obj, dict):
            return {k: self._restore_metadata(v, key=str(k)) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self._restore_metadata(item, key=key) for item in obj]
        return obj

    def _restore_position(self, d: dict[str, Any]) -> PaperPosition:
        from app.broker.models import PaperPosition
        from app.strategies.base import SignalDirection

        metadata = self._restore_metadata(d.get("metadata", {}))
        # Handle Enum
        direction = SignalDirection(d["direction"])
        return PaperPosition(
            pair=str(d["pair"]),
            direction=direction,
            quantity=_decimal_field(d, "quantity"),
            entry_price=_decimal_field(d, "entry_price"),
            leverage=_decimal_field(d, "leverage"),
            opened_at_ms=int(d["opened_at_ms"]),
            updated_at_ms=int(d["updated_at_ms"]),
            strategy_name=str(d["strategy_name"]),
            stop_loss=_optional_decimal_field(d, "stop_loss"),
            take_profit=_optional_decimal_field(d, "take_profit"),
            quote_to_margin_rate=_optional_decimal_field(d, "quote_to_margin_rate") or Decimal("1"),
            unit_contract_value=_optional_decimal_field(d, "unit_contract_value") or Decimal("1"),
            metadata=metadata if isinstance(metadata, dict) else {},
        )

    def _restore_fill(self, d: dict[str, Any]) -> PaperFill:
        from app.broker.models import PaperFill, PaperOrderSide

        metadata = self._restore_metadata(d.get("metadata", {}))
        return PaperFill(
            fill_id=str(d["fill_id"]),
            order_id=str(d["order_id"]),
            pair=str(d["pair"]),
            side=PaperOrderSide(d["side"]),
            quantity=_decimal_field(d, "quantity"),
            price=_decimal_field(d, "price"),
            fee=_decimal_field(d, "fee"),
            timestamp_ms=int(d["timestamp_ms"]),
            realized_pnl=_optional_decimal_field(d, "realized_pnl") or Decimal("0"),
            metadata=metadata if isinstance(metadata, dict) else {},
        )

    def clear(self) -> None:
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute("DELETE FROM broker_state")
            conn.execute("DELETE FROM strategy_state")
            conn.commit()

    def save_strategy_state(self, key: str, state: dict[str, Any]) -> None:
        state_json = json.dumps(to_jsonable(state))
        with closing(sqlite3.connect(self.db_path)) as conn:
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
        with closing(sqlite3.connect(self.db_path)) as conn:
            row = conn.execute(
                "SELECT state FROM strategy_state WHERE key = ?", (key,)
            ).fetchone()
            
        if not row:
            return None
            
        return self._restore_decimals(json.loads(row[0]))

    def close(self) -> None:
        # Connections are scoped and closed inside each operation.
        return None


class PaperSessionStore:
    def __init__(self, file_path: str = "data/paper_state.json") -> None:
        from pathlib import Path
        self.file_path = Path(file_path)
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        self._recovery_path = self.file_path.with_name(f"{self.file_path.name}.recovery")

    def _serialize(self, obj: Any) -> Any:
        from enum import Enum
        if isinstance(obj, Decimal):
            return str(obj)
        if isinstance(obj, Enum):
            return obj.value
        if isinstance(obj, (datetime)):
            return obj.isoformat()
        if isinstance(obj, dict):
            return {k: self._serialize(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self._serialize(i) for i in obj]
        return obj

    def _deserialize(self, obj: Any) -> Any:
        if isinstance(obj, str):
            # Try decimal
            s = obj.lstrip("-")
            if s.replace(".", "", 1).isdigit() and "." in s:
                try: return Decimal(obj)
                except: pass
            if s.isdigit() and len(s) < 20: # avoid big ints
                try: return Decimal(obj)
                except: pass
            # Try date
            try:
                return datetime.fromisoformat(obj)
            except:
                pass
            return obj
        if isinstance(obj, dict):
            return {k: self._deserialize(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self._deserialize(i) for i in obj]
        return obj

    def save_session(self, state: dict[str, Any]) -> None:
        serializable = to_jsonable(state)
        payload = json.dumps(serializable, indent=2)
        self.file_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = None
        with _SESSION_SAVE_LOCK:
            try:
                with NamedTemporaryFile(
                    mode="w",
                    encoding="utf-8",
                    delete=False,
                    dir=str(self.file_path.parent),
                    prefix=f".{self.file_path.name}.",
                    suffix=".tmp",
                ) as f:
                    tmp_path = f.name
                    f.write(payload)
                    f.flush()
                    os.fsync(f.fileno())

                if self._replace_with_retry(tmp_path, self.file_path):
                    self._cleanup_recovery()
                    return

                # If OneDrive/AV/another process keeps the main file locked,
                # preserve the latest session in a recovery file and let the
                # bot keep running. load_session() prefers this file when newer.
                if self._replace_with_retry(tmp_path, self._recovery_path):
                    return

                raise PermissionError(
                    f"Could not replace {self.file_path} or recovery session file."
                )
            finally:
                if tmp_path and os.path.exists(tmp_path):
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        pass

    def _replace_with_retry(self, source: str, target: Any) -> bool:
        for attempt in range(8):
            try:
                os.replace(source, target)
                return True
            except PermissionError:
                time.sleep(min(0.05 * (2 ** attempt), 1.0))
        return False

    def load_session(self) -> dict[str, Any] | None:
        paths = self._load_candidates()
        if not paths:
            return None
        for path in paths:
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                return self._deserialize(data)
            except Exception:
                continue
        return None

    def _load_candidates(self) -> list[Any]:
        paths = [path for path in [self.file_path, self._recovery_path] if path.exists()]
        return sorted(paths, key=lambda path: path.stat().st_mtime, reverse=True)

    def _cleanup_recovery(self) -> None:
        if not self._recovery_path.exists():
            return
        try:
            self._recovery_path.unlink()
        except OSError:
            pass

    def clear(self) -> None:
        for path in [self.file_path, self._recovery_path]:
            if path.exists():
                try:
                    path.unlink()
                except OSError:
                    pass
        for path in self.file_path.parent.glob(f".{self.file_path.name}.*.tmp"):
            try:
                path.unlink()
            except OSError:
                pass
