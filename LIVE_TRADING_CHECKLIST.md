# Live Trading Safety Checklist

Before setting `LIVE_TRADING_ENABLED=true` and `LIVE_CONFIRM_I_UNDERSTAND_RISK=YES` in your `.env` file, you **MUST** complete and verify every item on this checklist. This bot trades real capital on margin; a single misconfiguration can lead to severe financial loss.

## Phase 1: Security & Credentials
- [ ] **Dedicated API Key**: Created a new API key on CoinDCX specifically for this bot.
- [ ] **Minimum Permissions**: The API key only has permissions for "Futures Trading" and "Reading info". It does **NOT** have withdrawal permissions.
- [ ] **IP Whitelisting**: If you are running on a VPS, the API key is restricted to your server's static IP address.
- [ ] **Secret Safety**: `COINDCX_API_SECRET` is saved **only** in the local `.env` file and has never been committed to git or shared in logs.

## Phase 2: Configuration & Caps
- [ ] **Review `.env`**: Double-checked all values in `.env` against `.env.example`.
- [ ] **Max Order Notional**: `LIVE_MAX_ORDER_NOTIONAL` is set to an absolute maximum value in INR that you are comfortable risking per single order.
- [ ] **Max Margin Per Order**: `LIVE_MAX_MARGIN_PER_ORDER` is set correctly.
- [ ] **Max Daily Loss**: `LIVE_MAX_DAILY_LOSS_INR` is set to a strict INR amount. If the bot loses this amount in a single UTC day, the kill switch will activate.
- [ ] **LIVE_RISK_APPROVAL_ENABLED**: Set to true only AFTER `LIVE_MAX_ORDER_NOTIONAL` and `LIVE_MAX_MARGIN_PER_ORDER` are configured to safe values.
- [ ] **Position Sizing**: `MAX_RISK_PER_TRADE_PCT` is set conservatively (e.g., `1.0` or `2.0`).

## Phase 3: Staged Rollout
- [ ] **Backtest Verification**: Ran the strategy in backtest mode and confirmed it has a positive edge and acceptable drawdowns on recent data.
- [ ] **Paper Trading**: Ran the bot in Paper Mode (`TRADING_MODE=paper`) for at least 24 hours to ensure strategy triggers and logic are sound.
- [ ] **Dry-Run Pilot**: Set `TRADING_MODE=live`, `LIVE_TRADING_ENABLED=true`, but kept `LIVE_PILOT_DRY_RUN=true`. Confirmed the bot receives real market data, evaluates signals, and logs the orders it *would* have placed without errors.
- [ ] **Tiny Capital Pilot**: Set `LIVE_PILOT_DRY_RUN=false` but reduced `LIVE_MAX_ORDER_NOTIONAL` to a minimal amount (e.g., ₹500). Let the bot take 2-3 real trades to verify execution, fees, and stop-loss placement.

## Phase 4: Operational Readiness
- [ ] **Kill Switch Knowledge**: I know how to use the CLI (`python -m app.main live-kill-switch --enable`) or the dashboard to immediately halt trading.
- [ ] **Flatten Command**: I know how to manually close a position and cancel orders using `python -m app.main live-flatten --pair B-BTC_USDT --confirm-flatten YES`.
- [ ] **Stop-Loss Enforcement**: `LIVE_REQUIRE_STOP_LOSS=true` is enabled to ensure no "naked" positions are opened.
- [ ] **LIVE_CLOSE_ON_KILL_SWITCH**: Decided and set deliberately. Recommended: true unless you intend to manage open positions manually after a kill switch event.
- [ ] **Dashboard Monitoring**: The dashboard is running and I can see the "LIVE SAFETY GATES" panel.

---
*Once all items are checked, you are ready for full live trading.*
