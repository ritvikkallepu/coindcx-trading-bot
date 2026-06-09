# Live Dashboard Checklist

Before relying on the dashboard during a live pilot, verify the following:

- [ ] **Dashboard Starts**: Running `python -m app.main dashboard` starts the server without errors.
- [ ] **Status Endpoint Works**: Opening `http://localhost:8000/api/status` returns a valid JSON response.
- [ ] **Live State Loads**: Opening `http://localhost:8000/api/live-state` returns the current JSON state (or `{}` if no state exists yet).
- [ ] **Secrets Redacted**: The "Live Configuration (Redacted)" section under the Live Monitor tab DOES NOT show API keys, secrets, or Telegram tokens.
- [ ] **Kill Switch Button Tested**: 
  - Clicking "ENABLE KILL SWITCH" updates the state to Active.
  - Clicking "Disable" requires the exact text `DISABLE` to turn the switch off.
- [ ] **Dry-Run Position Visible**: A running dry-run position shows up in the "Open Positions" table with source `dry_run`.
- [ ] **Stop-Loss Visible**: The Stop Loss and Take Profit columns populate correctly for active positions.
- [ ] **No Real Order Buttons Enabled**: The dashboard does not provide any buttons to manually enter a trade.
- [ ] **Real Live Warning Visible**: When `LIVE_TRADING_ENABLED=true` and `LIVE_PILOT_DRY_RUN=false` are set, a large red warning banner appears.
- [ ] **Refresh Works**: The live dashboard auto-refreshes every 3 seconds without freezing the browser.
