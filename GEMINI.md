# Project Instructions: CoinDCX Trading Bot

## Strategy Architecture: HybridMeta

### Scoring & Damping
- **BB Conflict Damping**: When Bollinger Band (BB) signals oppose the EMA trend, the BB influence is reduced to **15%** rather than fully canceling the signal. This ensures trend persistence while still acknowledging mean-reversion pressure.
- **Visual Screen**: 
  - Softened rejection: Critically low volume (ratio < 0.35) results in a hard block.
  - Mildly low volume (ratio between 0.35 and `min_volume_ratio`) is penalized via scaling rather than immediate rejection.
  - Large spikes (candle range > 3.5x ATR) are hard-blocked.

### HybridMetaV2 Defaults
- `exit_threshold`: 0.42
- `min_volume_ratio`: 0.60
- `allow_short_without_open_interest`: True

## Risk Management & Trailing Stops

### ATR Trailing
- **Volatility Capping**: Trailing stops are capped using the ATR at the time of **entry**. This prevents stops from loosening if volatility expands after a position is opened.
- **Confirmation**: Trailing stops must be confirmed by a **candle close**. Wick-only movements (intrabar highs/lows) do not ratchet the trailing stop.
- **Conflict Resolution**: Fixed trailing and ATR trailing logic are mutually exclusive; ATR trailing takes precedence or suppresses fixed trailing to prevent "fighting" between stop logic.

## Technical Conventions

### Dataclasses
- **Immutability**: Many strategy and signal objects are `frozen=True`. 
- **Metadata Mutation**: Never attempt to mutate metadata or fields directly. Use `dataclasses.replace(obj, metadata=...)` for updates to maintain immutability and state integrity.
