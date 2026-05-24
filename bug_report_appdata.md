# `app/data` Module — Bug & Inconsistency Report

*Companion to the original bot bug report. Covers the newly provided `app/data` package only.*

---

## 🔴 Critical Bugs (crash at runtime)

### 1. `"1M"` (monthly) interval crashes `interval_to_ms()` — but is a valid WS subscription
**Files:** `data/candle_builder.py` (`FIXED_INTERVAL_MS`), `exchange/coindcx_channels.py` (`VALID_CANDLE_INTERVALS`)

`coindcx_channels.py` explicitly lists `"1M"` as a valid WS candle interval. If a user subscribes to it, the WS layer receives `CandleEvent` objects with `interval="1M"`. Any downstream call to `interval_to_ms("1M")` — used in `paper_loop.py`, `gap_guard.py`, and `floor_time_ms()` — raises `ValueError` and crashes the loop.

```python
# exchange/coindcx_channels.py
VALID_CANDLE_INTERVALS = {"1m", "5m", "15m", "30m", "1h", "4h", "8h", "1d", "3d", "1w", "1M"}

# candle_builder.py — "1M" is completely absent
FIXED_INTERVAL_MS = {"1m": 60_000, "5m": ..., "1w": ...}  # no "1M"
```

**Fix:** Add `"1M": 30 * 24 * 60 * 60_000` (approximate) or remove `"1M"` from `VALID_CANDLE_INTERVALS`.

---

### 2. `CandleBuilder` silently drops all intermediate candles during multi-bar gaps
**File:** `data/candle_builder.py`, `CandleBuilder.update_trade()`

When a new trade arrives after a gap of N candle periods (e.g. 5 minutes of silence on a 1m chart), `update_trade()` closes exactly one candle and starts fresh. The 4 empty intermediate bars are permanently lost with no log warning.

```python
# A trade 5 mins later only produces 1 candle, not 5
closed.append(self._current.freeze())   # only the one open candle
self._current = _WorkingCandle.start(trade, self.interval)
```

`CandleGapGuard` exists to detect this, but it only detects — it doesn't fill. Strategies that rely on exactly N candles of history will silently have a compressed series with fewer bars than expected. The RSI, EMA, and ATR calculations will return incorrect values for the period immediately after a gap.

---

### 3. `MarketDataPipeline.handle_raw()` propagates `ValueError` for unknown WS event names
**File:** `data/pipeline.py`, `data/normalizer.py`

`normalize_coindcx_event()` raises `ValueError` for any unrecognised event name. `handle_raw()` does **not** wrap the normaliser call in a try/except — the exception propagates uncaught to the caller. In the WS client (`coindcx_ws.py`) the `_handler_for` wrapper *does* catch it, but any other caller of `pipeline.handle_raw()` (e.g. in tests or replay) will crash on an unexpected event type rather than skipping it gracefully.

```python
# pipeline.py — no try/except around normalize_coindcx_event
events = normalize_coindcx_event(event_name, payload, default_pair=self.default_pair)
```

---

## 🟠 Logic Bugs (wrong behaviour, no crash)

### 4. `price-change` (and other events without `"s"` key) get the wrong pair in multi-pair setups
**File:** `data/normalizer.py`, `_normalize_price()` / `_pair_from()`

CoinDCX `price-change` WS payloads do not include a `"s"` (symbol) field — only `"p"` (price) and `"T"` (timestamp). `_pair_from()` falls back to `default_pair`, which is a single value set at `MarketDataPipeline` construction time.

In a multi-pair setup (e.g. simultaneously tracking BTC and ETH), all `price-change` events — regardless of which pair's channel they arrived from — are tagged with the single `default_pair`. `InMemoryMarketStore.prices[pair]` then overwrites the correct price entry with misattributed data.

```python
# price-change for ETH channel → gets labeled as BTC if default_pair='B-BTC_USDT'
PriceEvent(pair='B-BTC_USDT', price=Decimal('2500'), ...)   # ← should be B-ETH_USDT
```

The same issue affects `depth-snapshot`, `depth-update`, and `candlestick` rows that lack an explicit `"s"` or `"pair"` field and don't embed the pair in the channel name.

---

### 5. OI confirmation score is asymmetric and undocumented — OI-decreasing signals are 4× weaker regardless of direction
**File:** `data/open_interest.py`, `open_interest_confirmation_score()`

```python
if open_interest_change_pct > 0:
    return _clamp(price_direction * strength, ...)         # Full weight (1×)
return _clamp(price_direction * strength * Decimal("0.25"), ...)  # 4× dampened
```

When OI falls with rising price (shorts covering = short squeeze), the score is dampened to 25% of what OI-increasing + rising price would produce, even though a short squeeze is often a *stronger* directional signal than fresh longs. The asymmetry is unexplained and may cause strategies to systematically under-weight highly relevant OI regimes. There is no docstring or comment explaining the `0.25` constant.

---

### 6. Binance OI period map is missing `"8h"`, `"3d"`, and `"1w"` intervals
**File:** `data/open_interest.py`, `BINANCE_OI_PERIOD_BY_INTERVAL`

```python
BINANCE_OI_PERIOD_BY_INTERVAL = {
    "1m": "5m", "5m": "5m", "15m": "15m", "30m": "30m",
    "1h": "1h", "2h": "2h", "4h": "4h", "1d": "1d",
    # "8h", "3d", "1w" are absent
}
```

`binance_oi_period_for_interval("8h")` returns `None`. In `load_binance_open_interest_proxy()` a `None` period causes an early return of an empty `OpenInterestFeatureSeries`. Strategies running on `8h`, `3d`, or `1w` candles silently receive zero OI features and degrade to trading without OI confirmation, with no warning logged.

---

### 7. Interval coverage is fragmented and inconsistent across modules

Three interval sets exist with overlapping but non-identical members:

| Interval | `candle_builder` | REST loader | WS channels |
|---|---|---|---|
| `"1M"` | ❌ | ❌ | ✅ |
| `"8h"` | ✅ | ❌ | ✅ |
| `"3d"` | ✅ | ❌ | ✅ |
| `"1w"` | ✅ | ❌ | ✅ |
| `"2h"` / `"2hr"` | ✅ | ✅ | ❌ |
| `"30min"` / `"24h"` | ✅ aliases | ✅ aliases | ❌ |

**Impact:** A user who configures `strategy_interval = "8h"` can receive live candles via WS (WS supports it), but cannot load historical data via REST (data_loader rejects it), and OI features will be empty (OI map is missing it). There is no validation at startup to catch this combination.

---

## 🟡 Code Quality / Inconsistencies

### 8. `CandleGapGuard.check()` uses `next` as a parameter name, shadowing the built-in
**File:** `data/gap_guard.py`

```python
def check(self, prev: OHLCVCandle, next: OHLCVCandle) -> GapCheckResult:
```

`next` is a Python built-in. While harmless at runtime, it shadows the built-in inside the method, can confuse linters, and is flagged by `flake8 A002`. Rename to `current` or `next_candle`.

---

### 9. `gap_guard.py` has CRLF line endings; inconsistent with the rest of `app/data/`
**File:** `data/gap_guard.py`

All other files in `app/data/` use LF. `gap_guard.py` uses CRLF throughout, consistent with the same problem found in `live/`, `persistence/`, and `utils/`. Normalise to LF.

---

### 10. `IndicatorSnapshot.to_dict()` double-converts nested dataclasses via `asdict` then recursion
**File:** `data/indicators.py`, `IndicatorSnapshot.to_dict()`

```python
if hasattr(value, "__dataclass_fields__"):
    return {key: convert(item) for key, item in asdict(value).items()}
```

`asdict()` from the standard library recursively converts all nested dataclasses to dicts. The outer `convert()` recursion then walks those already-converted dicts again. This is not incorrect (it handles `Decimal` serialisation), but it's redundant work and the mixing of `asdict`-with-manual-recursion makes the intent unclear. The method should either use `asdict` with a custom type hook or recurse manually — not both.

---

### 11. `CandleSeries.add()` is O(n) on every insert due to linear search + full sort
**File:** `data/candle_builder.py`, `CandleSeries.add()`

```python
for index, existing in enumerate(self._candles):   # O(n) search
    if existing.open_time_ms == candle.open_time_ms:
        ...
self._candles.sort(key=lambda item: item.open_time_ms)  # O(n log n) every add
```

Every call to `add()` does a full list scan plus a full sort. For a series with `maxlen=1000`, this is fine in practice. But `extend()` (called at startup with hundreds of historical candles) calls `add()` in a loop, making initialisation O(n² log n). For the default `maxlen=1000` this is ~10M operations at startup. Replace with `bisect.insort` for the replace-or-insert logic.

---

### 12. `InMemoryMarketStore._trim_dict()` evicts the oldest-inserted pair, not the least-recently-used
**File:** `data/store.py`

```python
def _trim_dict(values: dict, max_items: int) -> None:
    while len(values) > max_items:
        oldest_key = next(iter(values))   # evicts first-inserted, not LRU
        del values[oldest_key]
```

In a multi-pair bot, a pair that was subscribed first but is actively receiving data could be evicted while a newer but inactive pair is retained. The correct policy for a live market store is LRU, not FIFO. The current implementation may silently drop live price/orderbook data for long-running pairs.

---

### 13. `ReplayMarketDataSource` sample `price-change` event omits the `"s"` pair field, masking Bug #4 in tests
**File:** `data/replay.py`

The sample data used for replaying events in tests/demos does not include the `"s"` key in the `price-change` payload — exactly the scenario that triggers the multi-pair pair-misattribution bug (#4). Tests using `ReplayMarketDataSource` with a single pair will pass, silently hiding the bug that only surfaces in multi-pair production use.

---

## Summary Table (app/data module)

| # | Severity | File | Issue |
|---|----------|------|-------|
| 1 | 🔴 Critical | `candle_builder.py` | `"1M"` interval accepted by WS but crashes `interval_to_ms` |
| 2 | 🔴 Critical | `candle_builder.py` | Multi-bar gaps silently drop intermediate candles — strategy history corrupted |
| 3 | 🔴 Critical | `pipeline.py` | `ValueError` on unknown WS events propagates uncaught from `handle_raw` |
| 4 | 🟠 Logic | `normalizer.py` | `price-change` (and other no-`"s"` events) get wrong pair in multi-pair setups |
| 5 | 🟠 Logic | `open_interest.py` | OI score 4× asymmetry for OI-decreasing regime is undocumented and may be incorrect |
| 6 | 🟠 Logic | `open_interest.py` | `"8h"`, `"3d"`, `"1w"` intervals return empty OI — silent degradation, no warning |
| 7 | 🟠 Logic | Cross-module | Interval coverage fragmented: `"1M"` in WS only, `"8h"/"3d"/"1w"` missing from REST/OI |
| 8 | 🟡 Quality | `gap_guard.py` | `next` shadows Python built-in — rename to `next_candle` |
| 9 | 🟡 Quality | `gap_guard.py` | CRLF line endings, inconsistent with all other `app/data/` files |
| 10 | 🟡 Quality | `indicators.py` | `to_dict` double-processes nested dataclasses via `asdict` + manual recursion |
| 11 | 🟡 Quality | `candle_builder.py` | `CandleSeries.add()` is O(n²) at startup for large historical series |
| 12 | 🟡 Quality | `store.py` | `_trim_dict` uses FIFO eviction instead of LRU — can drop live pair data |
| 13 | 🟡 Quality | `replay.py` | Sample events mask Bug #4 — no `"s"` in `price-change` hides multi-pair attribution failure |
