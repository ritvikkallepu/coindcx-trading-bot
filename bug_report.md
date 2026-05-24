# CoinDCX Trading Bot — Bug & Inconsistency Report

---

## 🔴 Critical Bugs (will cause crashes or silent incorrect behaviour at runtime)

### 1. `app/data` module is entirely missing from the zip
**Files affected:** `strategies/base.py`, `exchange/coindcx_ws.py`, `execution/engine.py`, `broker/paper.py`, `live/paper_loop.py`, `backtest/engine.py`, `backtest/data_loader.py`, `dashboard/server.py`, `main.py`, `research/sweep.py`, and most strategies.

Every single one of these files imports from `app.data.*`:
```
app.data.candle_builder   (CandleSeries, OHLCVCandle, interval_to_ms, rest_rows_to_series, floor_time_ms)
app.data.indicators       (IndicatorSnapshot, latest_indicator_snapshot, bollinger_bands, average_true_range, …)
app.data.pipeline         (MarketDataPipeline)
app.data.market_events    (event_to_dict, OrderBookLevel)
app.data.open_interest    (OpenInterestFeatureSeries, …)
app.data.gap_guard        (CandleGapGuard)
app.data.replay           (ReplayMarketDataSource)
```
**None of these files exist in the repository.** The entire bot is unrunnable — the very first import in `strategies/base.py` will raise `ModuleNotFoundError`. This is the single biggest issue in the codebase.

---

### 2. Global `max_open_positions` check is bypassed when a same-pair position already exists
**File:** `risk/manager.py`, line 106

```python
# 2. Global limit
if total_open >= self.settings.max_open_positions and pair_open == 0:
    return self._reject(...)
```

The `and pair_open == 0` condition means: if you already hold a position in `B-BTC_USDT` and `max_open_positions=3` is already full, a new scale-in signal for `B-BTC_USDT` will bypass the global position cap entirely. The portfolio can accumulate more open positions than `max_open_positions` allows through approved scale-ins, silently.

**Fix:** Remove the `pair_open == 0` guard (or check `total_open + 1 > max_open_positions` independently of `pair_open`).

---

### 3. `_restore_decimals` will corrupt non-numeric string fields on state reload
**File:** `persistence/paper_state.py`, `_restore_decimals()`

```python
def _restore_decimals(self, obj: Any) -> Any:
    if isinstance(obj, str):
        s = obj.lstrip("-")
        if s.isdigit() or ("." in s and s.replace(".", "", 1).isdigit()):
            return Decimal(obj)  # ← converts ANY numeric-looking string
```

The `load()` method calls `_restore_decimals` on the raw `positions` and `fills` dicts **before** `_restore_position` / `_restore_fill` are called. Any field whose stored string value happens to look like a number — e.g. a `pair` whose name contained only digits, or an `order_id` like `"123456"` — will silently be converted to a `Decimal` instead of staying a `str`. The downstream `_restore_position` call then crashes with a type error when it tries to do `d["pair"]` and receives a `Decimal`.

---

### 4. `_serialize_decimal` and `_deserialize_decimal` are dead code, creating a maintenance trap
**File:** `persistence/paper_state.py`, lines 44–73

`save()` uses `to_jsonable()` (from `app.utils.json`) and `load()` uses `_restore_decimals()`. The methods `_serialize_decimal` and `_deserialize_decimal` are never called anywhere in the file or in the broader codebase. They implement slightly different serialisation logic from the live path, which means any future developer who discovers them may use them incorrectly or be confused about which serialiser is canonical.

---

## 🟠 Logic Bugs (wrong behaviour, no crash)

### 5. `"all"` strategy in `strategy_engine_for_name` does NOT include all strategies
**File:** `strategies/defaults.py`, lines 20–28

```python
STRATEGY_CHOICES = ("all", "ema_rsi_trend", "bb_volume_reversion", "hybrid_meta",
                    "hybrid_meta_v2", "adaptive_hybrid", "bb_dynamic_grid")

def default_strategy_engine() -> StrategyEngine:
    return StrategyEngine([EMARSICrossoverStrategy(), BollingerVolumeMeanReversionStrategy()])

def strategy_engine_for_name(name: str) -> StrategyEngine:
    if normalized == "all":
        return default_strategy_engine()   # ← only 2 of 6 strategies!
```

`"all"` maps to `default_strategy_engine()` which only runs `ema_rsi_trend` and `bb_volume_reversion`. `HybridMetaStrategy`, `HybridMetaV2Strategy`, `AdaptiveHybridStrategy`, and `BollingerDynamicFuturesGridStrategy` are silently excluded. A user passing `PAPER_STRATEGY=all` would never know.

---

### 6. `_calculate_position_size` sizes on `account_equity`, not `available_equity`
**File:** `risk/manager.py`, `_calculate_position_size()` (line ~374)

Position sizing uses the full `account_equity` as the risk base, even though the margin sufficiency check further down operates on `available_equity` (which may be lower due to unrealised losses or existing margin commitments). This can produce a position size that the available equity check then rejects — or worse, in edge cases where margin sufficiency passes, can result in a position sized beyond what is genuinely available.

---

### 7. `effective_risk_multiplier` is clamped to `min(..., Decimal("1"))`, making the basket multiplier redundant
**File:** `risk/manager.py`, lines 152–158

```python
effective_risk_multiplier = min(
    signal_risk_multiplier,
    basket_risk_multiplier,
    Decimal("1"),   # ← hard cap at 1
)
```

`_risk_multiplier()` already clamps its output to `min(value, Decimal("1"))`, so every multiplier passed in is already `≤ 1`. The outer `min(..., Decimal("1"))` is a no-op but makes it look like there is intent to allow multipliers > 1 at some point. The code comment says "basket_risk_multiplier" but `_validate_risk_basket` always returns `Decimal("1")` — it is explicitly a placeholder stub. The metadata field `"basket_risk_checked": True` is therefore misleading.

---

### 8. `take_profit` rounding direction is wrong for SHORT entries
**File:** `risk/exchange_rules.py`, `normalize_signal_prices_to_tick()`, line 33

```python
elif signal.direction == SignalDirection.SHORT:
    entry_price = _round_price(signal.entry_price, tick_size, "down")
    stop_loss   = _round_price(signal.stop_loss,   tick_size, "up")
    take_profit = _round_price(signal.take_profit, tick_size, "up")  # ← wrong
```

For a SHORT trade the take profit is *below* the entry price. Rounding it **up** moves it *away* from the entry, making the TP harder to reach (more conservative), which may be intentional, but contradicts what the LONG branch does (rounds LONG TP **down**, closer to entry = more conservative). The convention is inconsistent between LONG and SHORT, meaning the SHORT TP is rounded toward a more favourable price while the LONG TP is rounded toward a worse price. Pick one convention and apply it symmetrically.

---

## 🟡 Inconsistencies & Code Quality Issues

### 9. Duplicate `from enum import Enum` and unused `Mapping` import in `strategies/base.py`
**File:** `strategies/base.py`, lines 7 and 14

```python
# Line 7
from enum import Enum
from typing import Any

from app.data.candle_builder import CandleSeries, OHLCVCandle
from app.data.indicators import IndicatorSnapshot

# Line 14 — duplicate block pasted in
from enum import Enum
from typing import Any, Mapping    # ← Mapping is never used in this file
```

`Enum` is imported twice. `Mapping` is imported but never used. This looks like a copy-paste artifact from a module merge.

---

### 10. Fee constants in `fees.py` are duplicated in `config.py` but used from hardcoded source in sweep/dashboard/main
**Files:** `fees.py`, `config.py`, `research/sweep.py`, `dashboard/state.py`, `main.py`

`fees.py` exports three hardcoded constants (`COINDCX_INR_M_MAKER_FEE_RATE`, `COINDCX_INR_M_TAKER_FEE_RATE`, `COINDCX_FEE_GST_RATE`). `config.py`'s `RiskSettings` has the same three values independently, overridable via `.env`. However, the backtest sweep and dashboard read from `fees.py` directly — meaning if a user sets custom fee rates in `.env`, the backtest and dashboard UI still use the hardcoded defaults. Only the live paper broker picks up the configured values. This causes a discrepancy between backtest results and live paper results when fees are customised.

---

### 11. Mixed CRLF/LF line endings in 5 files
**Files:** `live/paper_loop.py`, `live/run_paper.py`, `live/summary_logger.py`, `persistence/paper_state.py`, `utils/json.py`

These files have mixed Windows (`\r\n`) and Unix (`\n`) line endings within the same file. While Python handles this transparently at runtime, it causes noisy diffs, confuses linters, and can break shell scripts or tools that read the files raw. All files in the repo should be normalised to LF (add a `.gitattributes` with `* text=auto eol=lf`).

---

### 12. `metadata` field `"planned_risk_amount"` and `"risk_budget"` are redundant duplicates
**File:** `risk/manager.py`, lines 239–258

```python
metadata = {
    "risk_budget":          sizing.risk_budget,   # ← same value
    ...
    "planned_risk_amount":  sizing.risk_budget,   # ← exact duplicate
```

Both keys are set to `sizing.risk_budget`. One of them should be removed or they should carry different semantics (e.g. `risk_budget` = full budget before scale-in deduction, `planned_risk_amount` = effective budget after deduction).

---

### 13. `is_adjusted` metadata flag is semantically wrong
**File:** `risk/manager.py`, line 236

```python
is_adjusted = (pair_open > 0)
...
"position_size_adjusted_for_basket_risk": is_adjusted,
```

This flag is `True` whenever there is an existing position in the same pair, regardless of whether the basket risk calculation actually changed the size. The field name says "adjusted for basket risk" but `_validate_risk_basket` always returns a multiplier of `1.0` (it's a stub). The adjustment is actually for same-pair risk budget deduction, not basket risk. The label is misleading.

---

### 14. `PaperSessionStore` is a second persistence class with its own serialisation, never tested for consistency with `PaperStateStore`
**File:** `persistence/paper_state.py`, lines 242+

`PaperStateStore` uses SQLite + `to_jsonable`. `PaperSessionStore` uses a plain JSON file with its own `_serialize` / `_deserialize` methods. They store overlapping data (equity, positions, fills) in different formats, and there is no mechanism to keep them in sync or migrate between them. If one is loaded and the other is stale, state will be inconsistent.

---

### 15. `run_paper.py` configures `logging.basicConfig` then immediately imports a module that may call `configure_logging` again
**File:** `live/run_paper.py`

`run_paper.py` calls `logging.basicConfig(...)` before `PaperTradingLoop` is instantiated. If `PaperTradingLoop.__init__` or any import it triggers calls `configure_logging(settings)` from `utils/logging.py`, the second call does `root.handlers.clear()` which silently discards the `basicConfig` handler. The live log output format becomes inconsistent or handlers get doubled. The two logging setup paths need to be unified into a single call.

---

## Summary Table

| # | Severity | File | Issue |
|---|----------|------|-------|
| 1 | 🔴 Critical | entire `app/data/` | Module does not exist — bot won't start |
| 2 | 🔴 Critical | `risk/manager.py:106` | Global max position cap bypassed on scale-in |
| 3 | 🔴 Critical | `persistence/paper_state.py` | `_restore_decimals` corrupts string fields |
| 4 | 🟠 Logic | `persistence/paper_state.py` | Dead serialisation methods create maintenance trap |
| 5 | 🟠 Logic | `strategies/defaults.py` | `"all"` strategy only loads 2 of 6 strategies |
| 6 | 🟠 Logic | `risk/manager.py` | Sizing uses `account_equity`, not `available_equity` |
| 7 | 🟠 Logic | `risk/manager.py` | Basket risk multiplier is a no-op stub but metadata claims it's checked |
| 8 | 🟠 Logic | `risk/exchange_rules.py` | SHORT take-profit tick rounding direction inconsistent with LONG |
| 9 | 🟡 Quality | `strategies/base.py` | Duplicate `from enum import Enum`; unused `Mapping` import |
| 10 | 🟡 Quality | `fees.py` + `config.py` + sweep/dashboard | Fee constants duplicated; backtest ignores `.env` fee overrides |
| 11 | 🟡 Quality | 5 files in `live/`, `persistence/`, `utils/` | Mixed CRLF/LF line endings |
| 12 | 🟡 Quality | `risk/manager.py` | `risk_budget` and `planned_risk_amount` are identical in metadata |
| 13 | 🟡 Quality | `risk/manager.py` | `is_adjusted` / `position_size_adjusted_for_basket_risk` is mislabelled |
| 14 | 🟡 Quality | `persistence/paper_state.py` | Two persistence classes with divergent serialisation, no sync mechanism |
| 15 | 🟡 Quality | `live/run_paper.py` | Double logging setup — `basicConfig` then `configure_logging` conflict |
