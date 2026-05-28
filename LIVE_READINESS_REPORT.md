# LIVE_READINESS_REPORT.md

## Current State Audit
- **Exchange Integration**: REST and WebSocket clients for CoinDCX futures are implemented.
- **Execution Engine**: `LiveExecutionEngine` exists with basic entry/exit logic and dry-run support.
- **Risk Management**: `RiskManager` handles sizing and daily loss, but lacks live-specific caps (INR-based).
- **Loop**: `PaperTradingLoop` is well-implemented. A corresponding `LiveTradingLoop` is missing.
- **Reconciliation**: `LiveExchangeSynchronizer` has basic sync methods but lacks continuous background reconciliation and local-vs-exchange state verification.
- **Dashboard**: Dashboard exists for paper trading but lacks live-specific safety indicators and kill-switch controls.
- **Configuration**: Basic live flags exist in `Settings`, but strict staged-rollout validation is missing.

## Identified Gaps
1. **Live Trading Loop**: No long-running loop for real-time live trading.
2. **Safety Guards**: Missing `LIVE_MAX_ORDER_NOTIONAL` and `LIVE_MAX_MARGIN_PER_ORDER` strict enforcement in the live execution path.
3. **Reconciliation**: No periodic background sync to ensure local state matches exchange state.
4. **Kill Switch**: No mechanism to block entries or flatten positions across the whole bot via a single flag/command.
5. **Profit Protection**: Paper-only profit locking logic needs to be verified and enabled for live trading.
6. **Alerting**: No system for Telegram/Console notifications on critical live events.

## Roadmap to Live
- **PHASE 2**: Harden configuration and environment validation.
- **PHASE 3**: Implement the real-time live trading loop with reconciliation.
- **PHASE 4**: Add CLI commands for status, kill-switch, and manual flattening.
- **PHASE 5/6**: Port and harden profit protection and virtual accounting to live mode.
- **PHASE 7**: Extend dashboard with live safety features.
- **PHASE 8**: Implement alerting.
- **PHASE 9**: Extensive safety-block testing.
