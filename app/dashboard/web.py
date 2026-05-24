from __future__ import annotations


DASHBOARD_HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>CoinDCX Futures Control</title>
  <style>
    :root {
      color-scheme: dark;
      --bg: #101114;
      --panel: #181a20;
      --panel-2: #20232b;
      --line: #30343d;
      --text: #f2f3f5;
      --muted: #a8adb7;
      --green: #40c98a;
      --red: #f06d6d;
      --amber: #e6b450;
      --cyan: #56b6c2;
      --ink: #0d0f12;
    }

    * { box-sizing: border-box; }

    body {
      margin: 0;
      min-height: 100vh;
      background: var(--bg);
      color: var(--text);
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      letter-spacing: 0;
    }

    button, input, select {
      font: inherit;
      letter-spacing: 0;
    }

    .shell {
      display: grid;
      grid-template-columns: 300px 1fr;
      min-height: 100vh;
    }

    aside {
      border-right: 1px solid var(--line);
      background: #14161b;
      padding: 22px;
    }

    main {
      padding: 22px;
    }

    .brand {
      display: flex;
      align-items: center;
      gap: 12px;
      margin-bottom: 22px;
    }

    .mark {
      width: 36px;
      height: 36px;
      border-radius: 8px;
      background: linear-gradient(135deg, var(--green), var(--cyan));
      color: var(--ink);
      display: grid;
      place-items: center;
      font-weight: 900;
    }

    h1, h2, h3, p { margin: 0; }

    h1 {
      font-size: 18px;
      line-height: 1.2;
    }

    .subtle {
      color: var(--muted);
      font-size: 12px;
    }

    .panel {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 16px;
    }

    .controlStack {
      display: grid;
      gap: 12px;
    }

    label {
      display: grid;
      gap: 7px;
      color: var(--muted);
      font-size: 12px;
    }

    input, select {
      width: 100%;
      min-height: 38px;
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 8px 10px;
      color: var(--text);
      background: #111318;
      outline: none;
    }

    input:focus, select:focus {
      border-color: var(--cyan);
    }

    .checkLine {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      min-height: 38px;
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 8px 10px;
      background: #111318;
      color: var(--text);
      font-size: 13px;
      font-weight: 700;
    }

    .checkLine input {
      width: 18px;
      min-height: 18px;
      accent-color: var(--green);
    }

    .split {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 10px;
    }

    button {
      min-height: 42px;
      border: 0;
      border-radius: 8px;
      padding: 10px 14px;
      color: var(--ink);
      background: var(--green);
      font-weight: 800;
      cursor: pointer;
    }

    button:disabled {
      cursor: wait;
      opacity: 0.68;
    }

    .runButtonTop {
      width: 100%;
      margin-bottom: 16px;
    }

    .statusRows {
      display: grid;
      gap: 8px;
      margin-top: 16px;
    }

    .statusRow {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      font-size: 13px;
    }

    .chip {
      border: 1px solid var(--line);
      border-radius: 999px;
      padding: 4px 9px;
      font-size: 12px;
      color: var(--muted);
      background: #111318;
      white-space: nowrap;
    }

    .chip.good { color: var(--green); border-color: rgba(64, 201, 138, 0.45); }
    .chip.warn { color: var(--amber); border-color: rgba(230, 180, 80, 0.45); }
    .chip.bad { color: var(--red); border-color: rgba(240, 109, 109, 0.45); }

    .modeTabs {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 6px;
      margin-bottom: 18px;
    }
    .modeTab {
      background: #111318;
      color: var(--muted);
      border: 1px solid var(--line);
      border-radius: 8px;
      min-height: 36px;
      font-size: 13px;
      font-weight: 700;
      cursor: pointer;
    }
    .modeTab.active {
      background: var(--cyan);
      color: var(--ink);
      border-color: var(--cyan);
    }
    .paperStatus {
      display: flex;
      align-items: center;
      gap: 10px;
      padding: 8px 10px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #111318;
      font-size: 13px;
    }

    .topbar {
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      gap: 16px;
      margin-bottom: 18px;
    }

    .title h2 {
      font-size: 24px;
      line-height: 1.15;
    }

    .title .subtle {
      margin-top: 6px;
    }

    .chips {
      display: flex;
      flex-wrap: wrap;
      justify-content: flex-end;
      gap: 8px;
    }

    .metrics {
      display: grid;
      grid-template-columns: repeat(6, minmax(130px, 1fr));
      gap: 12px;
      margin-bottom: 12px;
    }

    .strategyBand {
      display: grid;
      grid-template-columns: minmax(220px, 0.85fr) repeat(4, minmax(120px, 1fr));
      gap: 10px;
      margin-bottom: 12px;
      align-items: stretch;
    }

    .miniStat {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 12px;
      min-height: 78px;
    }

    .miniStat span {
      display: block;
      color: var(--muted);
      font-size: 11px;
      margin-bottom: 7px;
    }

    .miniStat strong {
      display: block;
      font-size: 15px;
      line-height: 1.25;
      overflow-wrap: anywhere;
    }

    .qualityDetail {
      color: var(--muted);
      font-size: 12px;
      margin-top: 7px;
      line-height: 1.4;
    }

    .metric {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 14px;
      min-height: 92px;
    }

    .metric span {
      display: block;
      color: var(--muted);
      font-size: 12px;
      margin-bottom: 9px;
    }

    .metric strong {
      display: block;
      font-size: 22px;
      line-height: 1.1;
      overflow-wrap: anywhere;
    }

    details.advanced {
      margin-top: 12px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #111318;
    }

    details.advanced summary {
      padding: 10px;
      cursor: pointer;
      font-size: 13px;
      font-weight: 700;
      color: var(--muted);
      user-select: none;
    }

    details.advanced summary:hover {
      color: var(--text);
    }

    details.advanced .content {
      padding: 0 10px 10px;
      display: grid;
      gap: 12px;
    }

    .grid {
      display: grid;
      grid-template-columns: minmax(0, 1.45fr) minmax(320px, 0.85fr);
      gap: 12px;
    }

    .panelHeader {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 14px;
      margin-bottom: 12px;
    }

    .panelHeader h3 {
      font-size: 15px;
    }

    .chart {
      width: 100%;
      min-height: 320px;
      background: #111318;
      border: 1px solid var(--line);
      border-radius: 8px;
      overflow: hidden;
    }

    .bars {
      display: grid;
      gap: 12px;
    }

    .signalGrid {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 8px;
      margin-top: 14px;
    }

    .signalCell {
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 9px;
      background: #111318;
    }

    .signalCell span {
      display: block;
      color: var(--muted);
      font-size: 11px;
      margin-bottom: 5px;
    }

    .signalCell strong {
      display: block;
      font-size: 16px;
    }

    .barRow {
      display: grid;
      gap: 6px;
    }

    .barLabel {
      display: flex;
      justify-content: space-between;
      color: var(--muted);
      font-size: 12px;
    }

    .barTrack {
      height: 9px;
      background: #111318;
      border-radius: 999px;
      overflow: hidden;
      border: 1px solid var(--line);
    }

    .barFill {
      height: 100%;
      width: 0;
      background: var(--green);
    }

    .barFill.amber { background: var(--amber); }
    .barFill.red { background: var(--red); }
    .barFill.cyan { background: var(--cyan); }

    .tableWrap {
      margin-top: 12px;
      overflow: auto;
      max-height: 560px;
      border: 1px solid var(--line);
      border-radius: 8px;
    }

    table {
      width: 100%;
      border-collapse: collapse;
      min-width: 1180px;
      background: #111318;
    }

    th, td {
      text-align: left;
      border-bottom: 1px solid var(--line);
      padding: 10px 12px;
      font-size: 13px;
      white-space: nowrap;
    }

    th {
      color: var(--muted);
      font-weight: 700;
      background: #151820;
      position: sticky;
      top: 0;
      z-index: 1;
    }

    tr:last-child td {
      border-bottom: 0;
    }

    .message {
      min-height: 20px;
      margin-top: 12px;
      color: var(--muted);
      font-size: 13px;
    }

    .message.error { color: var(--red); }
    .message.warn { color: var(--amber); }
    .message.good { color: var(--green); }

    .paperMetricsGrid {
      display: grid;
      grid-template-columns: repeat(4, 1fr);
      gap: 12px;
      margin-bottom: 12px;
    }
    .metricCard {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 14px;
      display: flex;
      flex-direction: column;
      gap: 6px;
      min-height: 92px;
    }
    .metricLabel {
      color: var(--muted);
      font-size: 12px;
    }
    .metricValue {
      font-size: 20px;
      font-weight: 800;
    }
    .metricSub {
      font-size: 11px;
      color: var(--muted);
    }

    .posCard {
      background: #111318;
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 14px;
      margin-bottom: 12px;
    }
    .posTop {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 12px;
    }
    .posName {
      font-weight: 800;
      font-size: 15px;
      display: flex;
      align-items: center;
      gap: 8px;
    }
    .posGrid {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 10px;
      font-size: 13px;
    }
    .posAudit {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 8px;
      margin-top: 12px;
      padding-top: 10px;
      border-top: 1px solid var(--line);
    }
    .posAuditItem {
      min-width: 0;
      background: #151820;
      border: 1px solid rgba(255,255,255,0.05);
      border-radius: 6px;
      padding: 8px;
    }
    .posAuditItem span {
      display: block;
      color: var(--muted);
      font-size: 10px;
      line-height: 1.2;
      margin-bottom: 4px;
    }
    .posAuditItem strong {
      display: block;
      color: var(--text);
      font-size: 12px;
      line-height: 1.25;
      overflow-wrap: anywhere;
    }
    .posFooter {
      margin-top: 12px;
      padding-top: 10px;
      border-top: 1px solid var(--line);
      display: flex;
      justify-content: space-between;
      align-items: center;
      font-size: 11px;
      color: var(--muted);
    }

    .pmStatsBar {
      display: grid;
      grid-template-columns: repeat(6, 1fr);
      gap: 12px;
    }

    /* Searchable Select */
    .searchableSelect {
      position: relative;
    }
    .searchableSelect input {
      width: 100%;
    }
    .selectDropdown {
      position: absolute;
      top: 100%;
      left: 0;
      right: 0;
      background: var(--panel-2);
      border: 1px solid var(--line);
      border-top: 0;
      border-radius: 0 0 8px 8px;
      max-height: 240px;
      overflow-y: auto;
      z-index: 110; /* Higher than selectionInfo */
      display: none;
    }
    .selectDropdown.open {
      display: block;
    }
    .selectOption {
      padding: 10px 12px;
      cursor: pointer;
      display: flex;
      justify-content: space-between;
      align-items: center;
      border-bottom: 1px solid rgba(255,255,255,0.03);
    }
    .selectOption:hover {
      background: rgba(255,255,255,0.05);
    }
    .selectOption .internal {
      font-size: 10px;
      color: var(--muted);
    }
    .selectionInfo {
      margin-top: 6px;
      padding: 10px 12px;
      background: rgba(0,0,0,0.2);
      border: 1px solid var(--line);
      border-radius: 8px;
      display: flex;
      flex-direction: column;
      gap: 4px;
      font-size: 11px;
    }
    
    .intrabarGroup {
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 12px;
      display: grid;
      gap: 12px;
      background: rgba(255,255,255,0.02);
      margin-top: 4px;
    }

    @media (max-width: 1120px) {
      .shell { grid-template-columns: 1fr; }
      aside { border-right: 0; border-bottom: 1px solid var(--line); }
      .metrics { grid-template-columns: repeat(3, 1fr); }
      .strategyBand { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .paperMetricsGrid { grid-template-columns: repeat(2, 1fr); }
      .pmStatsBar { grid-template-columns: repeat(3, 1fr); }
      .grid { grid-template-columns: 1fr; }
    }

    @media (max-width: 680px) {
      main, aside { padding: 14px; }
      .topbar { display: grid; }
      .chips { justify-content: flex-start; }
      .metrics { grid-template-columns: 1fr 1fr; }
      .strategyBand { grid-template-columns: 1fr; }
      .paperMetricsGrid { grid-template-columns: 1fr; }
      .pmStatsBar { grid-template-columns: 1fr; }
      .split { grid-template-columns: 1fr; }
      .metric strong { font-size: 18px; }
    }
  </style>
</head>
<body>
  <div class="shell">
    <aside>
      <div class="brand">
        <div class="mark">CX</div>
        <div>
          <h1>CoinDCX Futures</h1>
          <p class="subtle">Paper control dashboard</p>
        </div>
      </div>

      <div class="modeTabs">
        <button id="tabBacktest" class="modeTab active" type="button">Backtest</button>
        <button id="tabPaper" class="modeTab" type="button">Paper Trading</button>
      </div>

      <div id="backtestControls">
        <button id="runButton" class="runButtonTop" type="button">Run Backtest</button>

        <div class="controlStack">
          <label>Pair (INR-M)
            <div class="searchableSelect">
              <input id="pairSearchInput" type="text" placeholder="Type BTC, SOL, BSB..." autocomplete="off" />
              <div id="pairDropdownList" class="selectDropdown"></div>
            </div>
          </label>
          <div class="selectionInfo">
            <span>Selected: <strong id="selectedPairDisplay">-</strong></span>
            <span class="subtle">Internal: <span id="selectedPairValue">-</span></span>
          </div>

          <div class="split">
            <label>Interval
              <select id="interval">
                <option>1h</option>
                <option value="2h">2hr</option>
                <option>4h</option>
                <option value="24h">24h</option>
                <option value="30min">30min</option>
                <option>15m</option>
                <option>5m</option>
                <option>1m</option>
              </select>
            </label>
            <label>Strategy
              <select id="strategy">
                <option value="bb_dynamic_grid">Futures Grid</option>
                <option value="adaptive_hybrid">Adaptive Hybrid</option>
                <option value="hybrid_meta_v2">Weighted Hybrid V2</option>
                <option value="hybrid_meta">Weighted Hybrid</option>
                <option value="ema_rsi_trend">EMA-RSI Trend</option>
                <option value="bb_volume_reversion">BB Reversion</option>
                <option value="all">All Baselines</option>
              </select>
            </label>
          </div>

          <div class="split">
            <label>Lookback
              <input id="lookback" type="number" min="50" step="50" value="1000" />
            </label>
            <label>Equity
              <input id="equity" type="number" min="1" step="100" value="1000" />
            </label>
          </div>

          <div class="split">
            <label>Leverage
              <input id="leverage" type="number" min="1" step="1" value="3" />
            </label>
            <label>Risk / Trade %
              <input id="risk_per_trade_pct" type="number" min="0.1" step="0.1" value="5" />
            </label>
            <label>Max Loss / Day %
              <input id="max_daily_loss_pct" type="number" min="0.1" step="0.1" value="10" />
            </label>
          </div>

          <label class="checkLine">
            <span>Trailing Stop</span>
            <input id="trailing_stop_enabled" type="checkbox" />
          </label>

          <label class="checkLine">
            <span>Auto ATR Exits</span>
            <input id="atr_dynamic_exits_enabled" type="checkbox" />
          </label>

          <label>ATR Mode
            <select id="atr_mode">
              <option value="router">Auto Policy Router</option>
              <option value="stop_only">Stop Only</option>
              <option value="stop_tp">Stop + Take Profit</option>
            </select>
          </label>

          <details class="advanced">
            <summary>Advanced Run Settings</summary>
            <div class="content">
              <div class="split">
                <label>Stop Loss %
                  <input id="stop_loss_pct" type="number" min="0.1" step="0.1" placeholder="Strategy" />
                </label>
                <label>Take Profit %
                  <input id="take_profit_pct" type="number" min="0.1" step="0.1" placeholder="Strategy" />
                </label>
              </div>

              <div class="split">
                <label>Maker Fee %
                  <input id="maker_fee_pct" type="number" min="0" step="0.01" value="0.02" />
                </label>
                <label>Taker Fee %
                  <input id="taker_fee_pct" type="number" min="0" step="0.01" value="0.05" />
                </label>
              </div>

              <div class="split">
                <label>GST on Fees %
                  <input id="fee_gst_pct" type="number" min="0" step="0.1" value="18" />
                </label>
                <label>Slippage %
                  <input id="slippage_pct" type="number" min="0" step="0.01" value="0.02" />
                </label>
              </div>

              <div class="split">
                <label>Stop Slippage %
                  <input id="stop_slippage_pct" type="number" min="0" step="0.01" value="0.02" />
                </label>
                <label>Funding Rate %
                  <input id="funding_fee_pct" type="number" step="0.001" value="0" />
                </label>
              </div>

              <label>Funding Every Hours
                <input id="funding_interval_hours" type="number" min="1" step="1" value="8" />
              </label>

              <div class="split">
                <label>Trail Active %
                  <input id="trailing_stop_activation_pct" type="number" min="0" step="0.1" value="1" />
                </label>
                <label>Trail Distance %
                  <input id="trailing_stop_distance_pct" type="number" min="0.1" step="0.1" value="2" />
                </label>
              </div>

              <div class="split">
                <label>ATR Trail Mult
                  <input id="atr_trailing_multiple" type="number" min="0.1" step="0.1" value="2.0" />
                </label>
                <label>ATR Stop Mult
                  <input id="atr_stop_multiple" type="number" min="0.1" step="0.1" value="1.5" />
                </label>
              </div>
              
              <label>Fee Rates Incl. GST
                <input id="fee_rate_preview" type="text" value="M 0.000236 / T 0.00059" readonly />
              </label>
            </div>
          </details>
        </div>

        <div class="statusRows" id="statusRows"></div>
        <div id="message" class="message"></div>
      </div>

      <div id="paperControls" style="display:none">
        <div class="controlStack">

          <div class="paperStatus" id="paperStatusBadge">
            <span class="chip" id="paperRunChip">Stopped</span>
            <span class="subtle" id="paperRunMeta"></span>
          </div>

          <!-- Pair Selector -->
          <label>Pair (INR-M)
            <div class="searchableSelect">
              <input id="p_pairSearchInput" type="text" placeholder="Type BTC, SOL, BSB..." autocomplete="off" />
              <div id="p_pairDropdownList" class="selectDropdown"></div>
            </div>
          </label>
          <div class="selectionInfo">
            <span>Selected next: <strong id="p_selectedPairDisplay">-</strong></span>
            <span class="subtle">Internal: <span id="p_selectedPairValue">-</span></span>
            <span class="subtle" id="runningPairLabelWrap" style="display:none; margin-top:4px; border-top:1px solid rgba(255,255,255,0.05); padding-top:4px">
              Currently Running: <strong id="runningPairLabel" style="color:var(--cyan)">-</strong>
            </span>
          </div>

          <!-- Interval + Strategy -->
          <div class="split">
            <label>Interval
              <select id="p_interval">
                <option value="1m">1m</option>
                <option value="5m">5m</option>
                <option value="15m" selected>15m</option>
                <option value="30min">30min</option>
                <option value="1h">1h</option>
                <option value="4h">4h</option>
                <option value="1d">1d</option>
              </select>
            </label>
            <label>Strategy
              <select id="p_strategy">
                <option value="adaptive_hybrid">Adaptive Hybrid</option>
                <option value="bb_dynamic_grid">Futures Grid</option>
                <option value="hybrid_meta_v2">Weighted Hybrid V2</option>
                <option value="hybrid_meta">Weighted Hybrid</option>
                <option value="ema_rsi_trend">EMA-RSI Trend</option>
                <option value="bb_volume_reversion">BB Reversion</option>
              </select>
            </label>
          </div>

          <label>Watchlist (comma-separated)
            <input id="p_watchlist" type="text" placeholder="B-BTC_USDT, B-SOL_USDT..." />
          </label>

          <label>Starting Equity (INR)
            <input id="p_starting_equity" type="number" min="10" step="100" value="10000" />
          </label>

          <!-- Leverage + Risk -->
          <div class="split">
            <label>Leverage
              <input id="p_leverage" type="number" min="1" max="30" step="1" value="5" />
            </label>
            <label>Risk / Trade %
              <input id="p_risk_pct" type="number" min="0.1" step="0.1" value="1" />
            </label>
          </div>

          <div class="split">
            <label>Max Daily Loss %
              <input id="p_max_daily_loss_pct" type="number" min="0.1" step="0.1" value="3" />
            </label>
            <label>Max Open Pos
              <input id="p_max_open" type="number" min="1" max="10" value="3" />
            </label>
          </div>

          <label>Max Margin %
            <input id="p_max_margin" type="number" min="1" max="100" value="50" />
          </label>

          <div class="split">
            <label class="checkLine">
              <span>Multi-Pair</span>
              <input id="p_multi_pair" type="checkbox" checked />
            </label>
            <label class="checkLine">
              <span>Pyramiding</span>
              <input id="p_pyramiding" type="checkbox" />
            </label>
          </div>

          <div class="split">
            <label class="checkLine">
              <span>Trailing Stop</span>
              <input id="p_trailing_stop" type="checkbox" />
            </label>
            <label class="checkLine">
              <span>ATR Exits</span>
              <input id="p_atr_exits" type="checkbox" />
            </label>
          </div>

          <label class="checkLine">
            <span>Profit Lock</span>
            <input id="p_profit_lock" type="checkbox" checked />
          </label>

          <!-- Intrabar Group -->
          <div class="intrabarGroup">
            <label class="checkLine" style="border:0; padding:0; background:transparent; min-height:0">
              <span>Intrabar Execution</span>
              <input id="p_intrabar" type="checkbox" />
            </label>
            
            <div id="p_intrabar_settings" style="display:none; gap:12px; flex-direction:column">
              <div class="split">
                <label>Exec Interval
                  <select id="p_exec_interval">
                    <option value="1m">1m</option>
                    <option value="5m">5m</option>
                    <option value="15m">15m</option>
                  </select>
                </label>
                <label>Max Entries/Candle
                  <input id="p_max_entries" type="number" min="1" max="10" value="1" />
                </label>
              </div>
              <label class="checkLine" style="border:0; padding:0; background:transparent; min-height:0">
                <span>Partial HTF Candle</span>
                <input id="p_partial_htf" type="checkbox" />
              </label>
            </div>
          </div>

          <div class="split" style="margin-top: 8px">
            <button id="startPaperBtn" type="button" style="background:var(--green)">
              ▶ Start
            </button>
            <button id="stopPaperBtn" type="button" 
                    style="background:var(--red);color:var(--text)" disabled>
              ■ Stop
            </button>
          </div>
          
          <button id="resetPaperBtn" type="button" 
                  style="background:var(--panel-2);color:var(--muted);width:100%; border:1px solid var(--line)">
            Reset Session
          </button>

          <div id="paperMessage" class="message"></div>
        </div>
      </div>
    </aside>

    <main>
      <div id="backtestMain">
        <div class="topbar">
          <div class="title">
            <h2>Strategy Monitor</h2>
            <p class="subtle" id="runMeta">Awaiting run</p>
          </div>
          <div class="chips" id="topChips">
            <span class="chip warn">Paper</span>
            <span class="chip good">INR-M Futures</span>
            <span class="chip bad">Live Locked</span>
          </div>
        </div>

        <section class="metrics">
          <div class="metric"><span>Final Equity</span><strong id="mFinal">-</strong></div>
          <div class="metric"><span>Total Return</span><strong id="mReturn">-</strong></div>
          <div class="metric"><span>Win Rate</span><strong id="mWin">-</strong></div>
          <div class="metric"><span>Profit Factor</span><strong id="mPF">-</strong></div>
          <div class="metric"><span>Max Drawdown</span><strong id="mDD">-</strong></div>
          <div class="metric"><span>Fees Paid</span><strong id="mFees">-</strong></div>
        </section>

        <section class="strategyBand">
          <div class="miniStat">
            <span>Run Quality</span>
            <strong id="qLabel">Awaiting Run</strong>
            <p class="qualityDetail" id="qDetail">-</p>
          </div>
          <div class="miniStat"><span>Strategy Mode</span><strong id="pMode">-</strong></div>
          <div class="miniStat"><span>Primary</span><strong id="pPrimary">-</strong></div>
          <div class="miniStat"><span>Secondary</span><strong id="pSecondary">-</strong></div>
          <div class="miniStat"><span>Filter</span><strong id="pFilter">-</strong></div>
        </section>

        <section class="grid">
          <div class="panel">
            <div class="panelHeader">
              <h3>Equity Curve</h3>
              <span class="chip" id="curveCount">0 points</span>
            </div>
            <div class="chart" id="equityChart"></div>
          </div>

          <div class="panel">
            <div class="panelHeader">
              <h3>Run Quality</h3>
              <span class="chip" id="tradeCount">0 trades</span>
            </div>
            <div class="bars">
              <div class="barRow">
                <div class="barLabel"><span>Win Rate</span><span id="bWin">-</span></div>
                <div class="barTrack"><div class="barFill" id="barWin"></div></div>
              </div>
              <div class="barRow">
                <div class="barLabel"><span>Drawdown</span><span id="bDD">-</span></div>
                <div class="barTrack"><div class="barFill red" id="barDD"></div></div>
              </div>
              <div class="barRow">
                <div class="barLabel"><span>Profit Factor</span><span id="bPF">-</span></div>
                <div class="barTrack"><div class="barFill cyan" id="barPF"></div></div>
              </div>
              <div class="barRow">
                <div class="barLabel"><span>Open Notional</span><span id="bOpen">-</span></div>
                <div class="barTrack"><div class="barFill amber" id="barOpen"></div></div>
              </div>
            </div>
            <div class="signalGrid">
              <div class="signalCell"><span>Long Entries</span><strong id="sLong">0</strong></div>
              <div class="signalCell"><span>Short Entries</span><strong id="sShort">0</strong></div>
              <div class="signalCell"><span>Exits</span><strong id="sExit">0</strong></div>
              <div class="signalCell"><span>Holds</span><strong id="sHold">0</strong></div>
            </div>
            <div id="diagnosticsMessage" class="message"></div>
            <div id="conflictMessage" class="message warn" style="display:none"></div>
            <div class="signalGrid" id="scoreGrid" style="margin-top:12px; display:none">
              <div class="signalCell"><span>EMA Score</span><strong id="vEMA">-</strong></div>
              <div class="signalCell"><span>BB Score</span><strong id="vBB">-</strong></div>
              <div class="signalCell"><span>Visual</span><strong id="vVis">-</strong></div>
              <div class="signalCell"><span>OI Score</span><strong id="vOI">-</strong></div>
            </div>
            <div id="exitDiagnosticsMessage" class="message"></div>
          </div>
        </section>

        <section class="panel" style="margin-top:12px">
          <div class="panelHeader">
            <h3>Signal Funnel</h3>
            <span class="chip" id="fSummary">-</span>
          </div>
          <div id="fWarning" class="message warn" style="display:none; margin-bottom:12px"></div>
          <div id="fAccountingWarning" class="message error" style="display:none; margin-bottom:12px">
            Some candidates were not mapped to a rejection reason. Funnel accounting is incomplete.
          </div>

          <div class="sectionTitle">Candidate Accounting</div>
          <div style="display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; margin-bottom: 12px;">
            <div class="miniStat"><span>Raw Candidates</span><strong id="fRawTot">-</strong></div>
            <div class="miniStat"><span>Executed Entries</span><strong id="fExecTot">-</strong></div>
            <div class="miniStat"><span>Explained Rejections</span><strong id="fExpTot">-</strong></div>
            <div class="miniStat" id="fUnexpWrap"><span>Unexplained</span><strong id="fUnexpTot">-</strong></div>
          </div>

          <div class="sectionTitle">Stage 1: Strategy Candidates</div>
          <div style="display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; margin-bottom: 12px;">
            <div class="miniStat"><span>Requested</span><strong id="fReq">-</strong></div>
            <div class="miniStat"><span>Actual Loaded</span><strong id="fAct">-</strong></div>
            <div class="miniStat"><span>Evaluated</span><strong id="fEval">-</strong></div>
            <div class="miniStat"><span>Coverage</span><strong id="fCov">-</strong></div>
          </div>
          <div style="display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; margin-bottom: 12px;">
            <div class="miniStat"><span>Raw Longs</span><strong id="fRawL">-</strong></div>
            <div class="miniStat"><span>Raw Shorts</span><strong id="fRawS">-</strong></div>
            <div class="miniStat"><span>Trade Rate</span><strong id="fRate">-</strong></div>
            <div class="miniStat"><span>Closed Trades</span><strong id="fClosed">-</strong></div>
          </div>

          <div class="sectionTitle">Stage 2 & 3: Active Blockers / Reasons</div>
          <div class="signalGrid" id="fBlocks">
            <!-- Block reasons sorted by count descending -->
          </div>
        </section>

        <section class="panel" style="margin-top:12px">
          <div class="panelHeader">
            <h3>Executed Trades Timeline</h3>
            <span class="chip" id="lastUpdated">-</span>
          </div>
          <div class="tableWrap">
            <table>
              <thead>
                <tr>
                  <th>#</th>
                  <th>Entry Time</th>
                  <th>Exit Time</th>
                  <th>Duration</th>
                  <th>Pair</th>
                  <th>Strategy</th>
                  <th>Side</th>
                  <th>Entry</th>
                  <th>Exit</th>
                  <th>Stop Loss</th>
                  <th>Take Profit</th>
                  <th>Net PnL</th>
                  <th>ATR Profile</th>
                  <th>MFE %</th>
                  <th>MAE %</th>
                  <th>R</th>
                  <th>Reason</th>
                </tr>
              </thead>
              <tbody id="tradeRows">
                <tr><td colspan="17">No trades loaded</td></tr>
              </tbody>
            </table>
          </div>
        </section>
      </div>

    <div id="paperMain" style="display:none">
      <!-- Top bar -->
      <div class="topbar">
        <div class="title">
          <h2>Paper Trading</h2>
          <p class="subtle" id="paperLoopMeta">Not running</p>
        </div>
        <div class="chips" id="paperTopChips">
          <span class="chip warn">Paper</span>
          <span class="chip good">INR-M Futures</span>
          <span class="chip bad">Live Locked</span>
        </div>
      </div>

        <!-- Live equity metrics row -->
        <section class="paperMetricsGrid">
          <div class="metricCard">
            <div class="metricLabel">Live Equity</div>
            <div class="metricValue" id="pmEquity">-</div>
            <div class="metricSub" id="pmEquityBase">-</div>
          </div>
          <div class="metricCard">
            <div class="metricLabel">Return</div>
            <div class="metricValue" id="pmReturn">-</div>
            <div class="metricSub" id="pmReturnAbs">-</div>
          </div>
          <div class="metricCard">
            <div class="metricLabel">Closed Net PnL</div>
            <div class="metricValue" id="pmPnl">-</div>
            <div class="metricSub" id="pmPnlSub">-</div>
          </div>
          <div class="metricCard">
            <div class="metricLabel">Fees Paid</div>
            <div class="metricValue" id="pmFees">-</div>
            <div class="metricSub" id="pmFeesSub">-</div>
          </div>
          <div class="metricCard">
            <div class="metricLabel">Win Rate</div>
            <div class="metricValue" id="pmWinRate">-</div>
            <div class="metricSub" id="pmWinRateSub">-</div>
          </div>
          <div class="metricCard">
            <div class="metricLabel">Max Drawdown</div>
            <div class="metricValue" id="pmMaxDD">-</div>
          </div>
          <div class="metricCard">
            <div class="metricLabel">Candles</div>
            <div class="metricValue" id="pmCandles">-</div>
            <div class="metricSub" id="pmCandlesSub">-</div>
          </div>
          <div class="metricCard">
            <div class="metricLabel">Total Fills</div>
            <div class="metricValue" id="pmFillCount">-</div>
            <div class="metricSub" id="pmFillSub">-</div>
          </div>
        </section>

        <!-- Watchlist Status -->
        <div id="pWatchlistWarning" class="alert warning" style="display: none; margin-bottom: 12px; padding: 10px; border-radius: 4px; background: rgba(245, 158, 11, 0.1); border: 1px solid var(--amber); color: var(--amber); font-size: 0.85rem;">
          Only selected pair is being scanned. Add pairs to Watchlist to scan multiple coins.
        </div>
        <section class="panel" style="margin-bottom:12px">
          <div class="panelHeader">
            <h3>Active Watchlist & Scanning</h3>
            <span class="chip" id="pScannedCount">0 pairs</span>
          </div>
          <div id="pWatchlistDisplay" style="font-size: 0.85rem; color: #ccc; display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: 10px; margin-top: 8px;">
            Waiting for loop...
          </div>
          <div style="margin-top: 12px; border-top: 1px solid var(--line); padding-top: 12px;">
            <button class="btn btn-secondary btn-sm" onclick="addCurrentToWatchlist()">Add Selected Pair to Watchlist</button>
          </div>
        </section>

        <!-- Live equity chart -->
        <section class="grid" style="margin-bottom:12px">
          <div class="panel">
            <div class="panelHeader">
              <h3>Live Equity Curve</h3>
              <div style="display:flex; gap:8px; align-items:center">
                <span class="chip" id="pmPeakLabel" style="color:var(--amber)">Peak: -</span>
                <span class="chip" id="pmChipStatus">Stopped</span>
              </div>
            </div>
            <div class="chart" id="paperEquityChart"></div>
          </div>

          <!-- Open positions -->
          <div class="panel">
            <div class="panelHeader">
              <h3>Open Positions</h3>
              <span class="chip" id="pmOpenCount">0</span>
            </div>
            <div id="pmPositionsBody" style="font-size:13px; color:var(--muted)">
              No open positions
            </div>
          </div>
        </section>

        <section class="panel" style="margin-bottom:12px">
          <div class="panelHeader">
            <h3>Live Candle Curve</h3>
            <div style="display:flex; gap:8px; align-items:center">
              <span class="chip" id="pmCandlePairChip">-</span>
              <span class="chip" id="pmCandleCountChip">0 candles</span>
            </div>
          </div>
          <div class="chart" id="paperCandleChart"></div>
        </section>

        <!-- Session statistics -->
        <section id="pmStatsBar" class="pmStatsBar" style="display:none; margin-bottom:12px">
          <div class="miniStat"><span>Avg Win</span><strong id="pmAvgWin">-</strong></div>
          <div class="miniStat"><span>Avg Loss</span><strong id="pmAvgLoss">-</strong></div>
          <div class="miniStat"><span>Best Trade</span><strong id="pmBestTrade">-</strong></div>
          <div class="miniStat"><span>Worst Trade</span><strong id="pmWorstTrade">-</strong></div>
          <div class="miniStat"><span>Profit Factor</span><strong id="pmProfitFactor">-</strong></div>
          <div class="miniStat"><span>Total Closed</span><strong id="pmTotalClosed">-</strong></div>
        </section>

        <!-- Recent closed trades -->
        <section class="panel" style="margin-top:0">
          <div class="panelHeader">
            <h3>Closed Trades</h3>
            <div style="display:flex; gap:8px; align-items:center">
              <span class="chip" id="pmWinChip">-</span>
              <span class="chip" id="pmTradeCount">0 trades</span>
            </div>
          </div>
          <p id="pmFillInfo" style="font-size:11px;color:#6b7280;margin:0 0 8px"></p>
          <div class="tableWrap" id="pmTradeTable" style="max-height:480px">
            <table>
              <thead>
                <tr>
                  <th>#</th>
                  <th>Time</th>
                  <th>Pair</th>
                  <th>Dir</th>
                  <th>Entry</th>
                  <th>Exit</th>
                  <th>Qty / Unit</th>
                  <th>Notional</th>
                  <th>Risk Used</th>
                  <th>Gross PnL</th>
                  <th>Fees</th>
                  <th>Net PnL</th>
                  <th>Net %</th>
                  <th>Hold</th>
                  <th>Reason</th>
                </tr>
              </thead>
              <tbody id="pmTradeRows">
                <tr><td colspan="15">No closed trades yet</td></tr>
              </tbody>
            </table>
          </div>
        </section>

        <div id="pmError" class="message error" style="margin-top:12px"></div>
      </div>
    </main>
  </div>

  <script>
    const ids = {
      pair: document.getElementById('pair'),
      interval: document.getElementById('interval'),
      strategy: document.getElementById('strategy'),
      lookback: document.getElementById('lookback'),
      equity: document.getElementById('equity'),
      leverage: document.getElementById('leverage'),
      risk_per_trade_pct: document.getElementById('risk_per_trade_pct'),
      max_daily_loss_pct: document.getElementById('max_daily_loss_pct'),
      stop_loss_pct: document.getElementById('stop_loss_pct'),
      take_profit_pct: document.getElementById('take_profit_pct'),
      maker_fee_pct: document.getElementById('maker_fee_pct'),
      taker_fee_pct: document.getElementById('taker_fee_pct'),
      fee_gst_pct: document.getElementById('fee_gst_pct'),
      fee_rate_preview: document.getElementById('fee_rate_preview'),
      slippage_pct: document.getElementById('slippage_pct'),
      stop_slippage_pct: document.getElementById('stop_slippage_pct'),
      funding_fee_pct: document.getElementById('funding_fee_pct'),
      funding_interval_hours: document.getElementById('funding_interval_hours'),
      trailing_stop_enabled: document.getElementById('trailing_stop_enabled'),
      trailing_stop_activation_pct: document.getElementById('trailing_stop_activation_pct'),
      trailing_stop_distance_pct: document.getElementById('trailing_stop_distance_pct'),
      atr_dynamic_exits_enabled: document.getElementById('atr_dynamic_exits_enabled'),
      atr_mode: document.getElementById('atr_mode'),
      atr_stop_multiple: document.getElementById('atr_stop_multiple'),
      atr_trailing_multiple: document.getElementById('atr_trailing_multiple'),
      runButton: document.getElementById('runButton'),
      message: document.getElementById('message'),
      statusRows: document.getElementById('statusRows'),
      diagnosticsMessage: document.getElementById('diagnosticsMessage'),
      exitDiagnosticsMessage: document.getElementById('exitDiagnosticsMessage'),
      runMeta: document.getElementById('runMeta'),
      topChips: document.getElementById('topChips'),
      equityChart: document.getElementById('equityChart'),
      tradeRows: document.getElementById('tradeRows'),
      qLabel: document.getElementById('qLabel'),
      qDetail: document.getElementById('qDetail'),
      pMode: document.getElementById('pMode'),
      pPrimary: document.getElementById('pPrimary'),
      pSecondary: document.getElementById('pSecondary'),
      pFilter: document.getElementById('pFilter'),
      sLong: document.getElementById('sLong'),
      sShort: document.getElementById('sShort'),
      sExit: document.getElementById('sExit'),
      sHold: document.getElementById('sHold'),
      fReq: document.getElementById('fReq'),
      fAct: document.getElementById('fAct'),
      fEval: document.getElementById('fEval'),
      fCov: document.getElementById('fCov'),
      fRawL: document.getElementById('fRawL'),
      fRawS: document.getElementById('fRawS'),
      fClosed: document.getElementById('fClosed'),
      fRate: document.getElementById('fRate'),
      fRawTot: document.getElementById('fRawTot'),
      fExecTot: document.getElementById('fExecTot'),
      fExpTot: document.getElementById('fExpTot'),
      fUnexpTot: document.getElementById('fUnexpTot'),
      vEMA: document.getElementById('vEMA'),
      vBB: document.getElementById('vBB'),
      vVis: document.getElementById('vVis'),
      vOI: document.getElementById('vOI'),
      scoreGrid: document.getElementById('scoreGrid'),
      conflictMessage: document.getElementById('conflictMessage'),
      pmWinChip: document.getElementById('pmWinChip'),
      pmTradeCount: document.getElementById('pmTradeCount'),
      pmTradeTable: document.getElementById('pmTradeTable'),
      pmStatsBar: document.getElementById('pmStatsBar'),
      pmAvgWin: document.getElementById('pmAvgWin'),
      pmAvgLoss: document.getElementById('pmAvgLoss'),
      pmBestTrade: document.getElementById('pmBestTrade'),
      pmWorstTrade: document.getElementById('pmWorstTrade'),
      pmProfitFactor: document.getElementById('pmProfitFactor'),
      pmTotalClosed: document.getElementById('pmTotalClosed'),
      p_pairSearchInput: document.getElementById('p_pairSearchInput'),
      p_interval: document.getElementById('p_interval'),
      p_strategy: document.getElementById('p_strategy'),
      p_watchlist: document.getElementById('p_watchlist'),
      p_starting_equity: document.getElementById('p_starting_equity'),
      p_max_open: document.getElementById('p_max_open'),
      p_max_margin: document.getElementById('p_max_margin'),
      p_multi_pair: document.getElementById('p_multi_pair'),
      p_pyramiding: document.getElementById('p_pyramiding'),
      p_leverage: document.getElementById('p_leverage'),
      p_risk_pct: document.getElementById('p_risk_pct'),
      p_max_daily_loss_pct: document.getElementById('p_max_daily_loss_pct'),
      p_trailing_stop: document.getElementById('p_trailing_stop'),
      p_atr_exits: document.getElementById('p_atr_exits'),
      p_profit_lock: document.getElementById('p_profit_lock'),
      p_intrabar: document.getElementById('p_intrabar'),
      p_exec_interval: document.getElementById('p_exec_interval'),
      p_max_entries: document.getElementById('p_max_entries'),
      p_partial_htf: document.getElementById('p_partial_htf'),
      startPaperBtn: document.getElementById('startPaperBtn'),
      stopPaperBtn: document.getElementById('stopPaperBtn'),
      runningPairLabel: document.getElementById('runningPairLabel')
    };

    const num = value => {
      const parsed = Number(value);
      return Number.isFinite(parsed) ? parsed : 0;
    };
    const boolVal = value => {
      if (value === true) return true;
      if (value === false || value === null || value === undefined) return false;
      return ['1', 'true', 'yes', 'y', 'on'].includes(String(value).trim().toLowerCase());
    };

    const money = value => num(value).toLocaleString(undefined, {
      minimumFractionDigits: 2,
      maximumFractionDigits: 4
    });

    const pct = value => `${num(value).toFixed(2)}%`;
    const compact = value => num(value).toLocaleString(undefined, { 
      minimumFractionDigits: 2,
      maximumFractionDigits: 6 
    });
    const rateToPct = value => (num(value) * 100).toFixed(4).replace(/0+$/, '').replace(/\.$/, '');
    const pctToRate = value => (num(value) / 100).toFixed(8).replace(/0+$/, '').replace(/\.$/, '');
    const effectiveRateFromPct = (feePct, gstPct) => {
      const feeRate = num(feePct) / 100;
      const gstRate = num(gstPct) / 100;
      return (feeRate * (1 + gstRate)).toFixed(8).replace(/0+$/, '').replace(/\.$/, '');
    };
    const escapeHtml = value => String(value ?? '').replace(/[&<>"']/g, char => ({
      '&': '&amp;',
      '<': '&lt;',
      '>': '&gt;',
      '"': '&quot;',
      "'": '&#39;'
    }[char]));

    function setMessage(text, kind = '') {
      ids.message.textContent = text || '';
      ids.message.className = `message ${kind}`;
    }

    async function fetchJson(url, options) {
      const response = await fetch(url, options);
      const payload = await response.json();
      if (!response.ok) {
        throw new Error(payload.detail || payload.error || 'Request failed');
      }
      return payload;
    }

    function switchMode(mode) {
      document.getElementById('backtestControls').style.display = mode === 'backtest' ? '' : 'none';
      document.getElementById('paperControls').style.display = mode === 'paper' ? '' : 'none';
      document.getElementById('backtestMain').style.display = mode === 'backtest' ? '' : 'none';
      document.getElementById('paperMain').style.display = mode === 'paper' ? '' : 'none';
      document.getElementById('tabBacktest').className = 'modeTab' + (mode === 'backtest' ? ' active' : '');
      document.getElementById('tabPaper').className = 'modeTab' + (mode === 'paper' ? ' active' : '');
    }
    document.getElementById('tabBacktest').addEventListener('click', () => switchMode('backtest'));
    document.getElementById('tabPaper').addEventListener('click', () => switchMode('paper'));

    function renderStatusBadges(config, botMode) {
      ids.statusRows.innerHTML = '';
      [
        ['Mode', botMode || 'paper'],
        ['Risk / Trade', `${config.risk_per_trade_pct}%`],
        ['Max Loss / Day', `${config.max_daily_loss_pct}%`],
        ['Trailing Stop', config.trailing_stop_enabled ? 'On' : 'Off'],
        ['Dynamic ATR', config.atr_dynamic_exits_enabled ? 'On' : 'Off'],
        ['ATR Mode', config.atr_policy_mode === 'router' ? 'Auto Router' : (config.atr_take_profit_enabled ? 'Stop + TP' : 'Stop Only')]
      ].forEach(([label, value]) => {
        const row = document.createElement('div');
        row.className = 'statusRow';
        row.innerHTML = `<span>${escapeHtml(label)}</span><span class="chip">${escapeHtml(value)}</span>`;
        ids.statusRows.appendChild(row);
      });

      // Advanced hidden section
      const advanced = [
        ['Leverage', config.leverage],
        ['Compound Risk', config.compound_risk_equity ? 'Yes' : 'No'],
        ['Slippage', `${config.slippage_pct}%`],
        ['Stop Slippage', `${config.stop_slippage_pct ?? config.slippage_pct}%`],
        ['Funding', `${rateToPct(config.funding_fee_rate)}% / ${config.funding_interval_hours}h`],
        ['ATR Stop Mult', config.atr_stop_multiple],
        ['ATR Trail Mult', config.atr_trailing_multiple],
        ['Loss Guard', `${config.stop_loss_cooldown_candles} stop / ${config.max_consecutive_losses} losses`]
      ];
      
      const details = document.createElement('details');
      details.className = 'advanced';
      details.style.marginTop = '8px';
      details.innerHTML = '<summary style="padding:4px 8px; font-size:11px">Advanced Run Settings</summary>';
      const content = document.createElement('div');
      content.className = 'content';
      content.style.padding = '8px';
      content.style.gap = '4px';
      advanced.forEach(([label, value]) => {
        const row = document.createElement('div');
        row.className = 'statusRow';
        row.style.fontSize = '11px';
        row.innerHTML = `<span>${escapeHtml(label)}</span><span class="chip" style="font-size:10px; padding:2px 6px">${escapeHtml(value)}</span>`;
        content.appendChild(row);
      });
      details.appendChild(content);
      ids.statusRows.appendChild(details);
    }

    let allPairs = []; // Stores {pair: 'B-BTC_USDT', display_name: 'BTC-USDT'}

    function setupSearchableDropdown(inputId, listId, displayId, valueId) {
      const input = document.getElementById(inputId);
      const list = document.getElementById(listId);
      const display = document.getElementById(displayId);
      const valEl = document.getElementById(valueId);

      const render = (filter = '') => {
        const query = filter.toUpperCase();
        const filtered = allPairs.filter(p => 
          p.display_name.toUpperCase().includes(query) || 
          p.pair.toUpperCase().includes(query)
        );
        
        list.innerHTML = filtered.map(p => `
          <div class="selectOption" data-pair="${p.pair}" data-display="${p.display_name}">
            <span>${p.display_name}</span>
            <span class="internal">${p.pair}</span>
          </div>
        `).join('');
      };

      input.addEventListener('focus', () => {
        render(input.value);
        list.classList.add('open');
      });

      input.addEventListener('input', () => {
        render(input.value);
        list.classList.add('open');
      });

      list.addEventListener('click', (e) => {
        const opt = e.target.closest('.selectOption');
        if (!opt) return;
        
        const pair = opt.dataset.pair;
        const disp = opt.dataset.display;
        
        input.value = disp;
        display.textContent = disp;
        valEl.textContent = pair;
        list.classList.remove('open');
      });

      document.addEventListener('click', (e) => {
        if (!input.contains(e.target) && !list.contains(e.target)) {
          list.classList.remove('open');
        }
      });
    }

    async function loadPairs() {
      try {
        const data = await fetchJson('/api/pairs');
        allPairs = data.pairs || [];
        // Setup both dropdowns
        setupSearchableDropdown('pairSearchInput', 'pairDropdownList', 'selectedPairDisplay', 'selectedPairValue');
        setupSearchableDropdown('p_pairSearchInput', 'p_pairDropdownList', 'p_selectedPairDisplay', 'p_selectedPairValue');
      } catch (e) {
        console.error('Failed to load pairs:', e);
      }
    }

    function getSelectedPair(prefix = '') {
      // If we have a value in the internal label, use it
      const valEl = document.getElementById(prefix ? prefix + '_selectedPairValue' : 'selectedPairValue');
      if (valEl && valEl.textContent !== '-') return valEl.textContent;
      
      // Fallback to normalizing whatever is in the search input
      const input = document.getElementById(prefix ? prefix + 'pairSearchInput' : 'pairSearchInput');
      return normalizePair(input.value);
    }

    function normalizePair(val) {
      const input = (val || '').trim();
      if (!input) return '';
      // If already internal, return it
      const foundByInternal = allPairs.find(p => p.pair.toUpperCase() === input.toUpperCase());
      if (foundByInternal) return foundByInternal.pair;
      // If display name, map it
      const foundByDisplay = allPairs.find(p => p.display_name.toUpperCase() === input.toUpperCase());
      if (foundByDisplay) return foundByDisplay.pair;
      // Fallback: if it starts with B- assume user knows what they are doing
      if (input.toUpperCase().startsWith('B-')) return input.toUpperCase();
      // Last resort try to make it CoinDCX style
      return 'B-' + input.toUpperCase().replace('-', '_');
    }

    async function loadStatus() {
      const status = await fetchJson('/api/status');
      const defaults = status.defaults || {};
      const risk = status.risk || {};
      
      const initialConfig = {
        ...defaults,
        risk_per_trade_pct: defaults.risk_per_trade_pct ?? risk.max_risk_per_trade_pct,
        max_daily_loss_pct: defaults.max_daily_loss_pct ?? risk.max_daily_loss_pct,
        trailing_stop_enabled: risk.trailing_stop_enabled,
        trailing_stop_activation_pct: risk.trailing_stop_activation_pct,
        trailing_stop_distance_pct: risk.trailing_stop_distance_pct
      };

      Object.entries({
        interval: defaults.interval,
        strategy: defaults.strategy,
        lookback: defaults.lookback,
        equity: defaults.equity,
        leverage: defaults.leverage,
        risk_per_trade_pct: initialConfig.risk_per_trade_pct,
        max_daily_loss_pct: initialConfig.max_daily_loss_pct,
        stop_loss_pct: defaults.stop_loss_pct ?? '',
        take_profit_pct: defaults.take_profit_pct ?? '',
        maker_fee_pct: rateToPct(defaults.maker_fee_rate),
        taker_fee_pct: rateToPct(defaults.taker_fee_rate),
        fee_gst_pct: rateToPct(defaults.fee_gst_rate),
        slippage_pct: defaults.slippage_pct,
        stop_slippage_pct: defaults.stop_slippage_pct,
        funding_fee_pct: rateToPct(defaults.funding_fee_rate),
        funding_interval_hours: defaults.funding_interval_hours,
        trailing_stop_activation_pct: initialConfig.trailing_stop_activation_pct,
        trailing_stop_distance_pct: initialConfig.trailing_stop_distance_pct,
        atr_stop_multiple: defaults.atr_stop_multiple,
        atr_trailing_multiple: defaults.atr_trailing_multiple
      }).forEach(([key, value]) => {
        if (value !== undefined && ids[key]) ids[key].value = value;
      });
      
      // Handle pair separately to use display name if available
      const dPair = defaults.pair || 'B-SOL_USDT';
      await loadPairs();
      const pMatch = allPairs.find(p => p.pair === dPair);
      
      const setPair = (prefix, pair, disp) => {
        const input = document.getElementById(prefix + 'pairSearchInput');
        const display = document.getElementById(prefix + 'selectedPairDisplay');
        const valEl = document.getElementById(prefix + 'selectedPairValue');
        if (!input || !display || !valEl) return;
        input.value = disp;
        display.textContent = disp;
        valEl.textContent = pair;
      };

      if (pMatch) {
        setPair('', pMatch.pair, pMatch.display_name);
        setPair('p_', pMatch.pair, pMatch.display_name);
      } else {
        setPair('', dPair, dPair);
        setPair('p_', dPair, dPair);
      }
      
      // Initialize Paper sidebar fields
      if (ids.p_interval) ids.p_interval.value = defaults.interval || '15m';
      if (ids.p_strategy) ids.p_strategy.value = defaults.strategy || 'adaptive_hybrid';
      if (ids.p_starting_equity) ids.p_starting_equity.value = defaults.equity || '10000';
      if (ids.p_leverage) ids.p_leverage.value = defaults.leverage || '5';
      if (ids.p_risk_pct) ids.p_risk_pct.value = initialConfig.risk_per_trade_pct || '1';
      if (ids.p_max_daily_loss_pct) ids.p_max_daily_loss_pct.value = initialConfig.max_daily_loss_pct || '3';
      
      ids.p_trailing_stop.checked = Boolean(initialConfig.trailing_stop_enabled);
      ids.p_atr_exits.checked = Boolean(defaults.atr_dynamic_exits_enabled);
      ids.p_profit_lock.checked = true;
      ids.p_intrabar.checked = Boolean(defaults.paper_intrabar_enabled);
      if (ids.p_exec_interval) ids.p_exec_interval.value = defaults.execution_interval || '1m';
      if (ids.p_max_entries) ids.p_max_entries.value = defaults.max_reentries_per_candle || '1';
      ids.p_partial_htf.checked = Boolean(defaults.use_partial_parent_candle);
      
      document.getElementById('p_intrabar_settings').style.display = ids.p_intrabar.checked ? 'flex' : 'none';
      refreshExecIntervalOptions();

      refreshFeeRatePreview();
      ids.trailing_stop_enabled.checked = Boolean(initialConfig.trailing_stop_enabled);
      ids.atr_dynamic_exits_enabled.checked = Boolean(defaults.atr_dynamic_exits_enabled);
      ids.atr_mode.value = defaults.atr_policy_mode === 'router'
        ? 'router'
        : (defaults.atr_take_profit_enabled ? 'stop_tp' : 'stop_only');

      renderStatusBadges(initialConfig, status.bot.mode);
      renderStrategyProfile(status.strategy_profile || {});
    }

    function params() {
      const atrMode = ids.atr_mode.value;
      return {
        pair: getSelectedPair(''),
        interval: ids.interval.value,
        strategy: ids.strategy.value,
        lookback: ids.lookback.value,
        equity: ids.equity.value,
        leverage: ids.leverage.value,
        risk_per_trade_pct: ids.risk_per_trade_pct.value,
        max_daily_loss_pct: ids.max_daily_loss_pct.value,
        stop_loss_pct: ids.stop_loss_pct.value,
        take_profit_pct: ids.take_profit_pct.value,
        maker_fee_pct: ids.maker_fee_pct.value,
        taker_fee_pct: ids.taker_fee_pct.value,
        fee_gst_pct: ids.fee_gst_pct.value,
        slippage_pct: ids.slippage_pct.value,
        stop_slippage_pct: ids.stop_slippage_pct.value,
        funding_fee_pct: ids.funding_fee_pct.value,
        funding_interval_hours: ids.funding_interval_hours.value,
        trailing_stop_enabled: ids.trailing_stop_enabled.checked,
        trailing_stop_activation_pct: ids.trailing_stop_activation_pct.value,
        trailing_stop_distance_pct: ids.trailing_stop_distance_pct.value,
        atr_dynamic_exits_enabled: ids.atr_dynamic_exits_enabled.checked,
        atr_policy_mode: atrMode === 'router' ? 'router' : 'manual',
        atr_stop_enabled: true,
        atr_take_profit_enabled: atrMode === 'stop_tp',
        atr_trailing_enabled: true,
        atr_entry_filter_enabled: true,
        atr_stop_multiple: ids.atr_stop_multiple.value,
        atr_trailing_multiple: ids.atr_trailing_multiple.value,
        recent_count: 12
      };
    }

    function profileFor(strategy, interval) {
      if (strategy === 'adaptive_hybrid') {
        return {
          mode: 'Market Adaptive',
          primary: 'Regime selector',
          secondary: 'EMA-RSI / Bollinger',
          filter: 'Visual + OI'
        };
      }
      if (strategy === 'hybrid_meta') {
        return { mode: 'Score Blend', primary: 'EMA + BB + Visual', secondary: 'OI optional', filter: 'Visual screen' };
      }
      if (strategy === 'hybrid_meta_v2') {
        return { mode: 'Score Blend V2', primary: 'EMA + BB + Visual', secondary: 'OI only when scored', filter: 'Entry-only visual screen' };
      }
      if (strategy === 'ema_rsi_trend') {
        return { mode: 'Trend', primary: 'EMA crossover', secondary: 'RSI filter', filter: 'ATR exits' };
      }
      if (strategy === 'bb_volume_reversion') {
        return { mode: 'Mean Reversion', primary: 'Bollinger squeeze', secondary: 'Volume confirmation', filter: 'ATR stop' };
      }
      if (strategy === 'bb_dynamic_grid') {
        return { mode: 'Futures Grid', primary: 'Bollinger range', secondary: 'Basket scale-ins', filter: 'Close-confirmed trail' };
      }
      return { mode: 'Mixed', primary: 'Multiple', secondary: 'Multiple', filter: 'Risk layer' };
    }

    function refreshFeeRatePreview() {
      ids.fee_rate_preview.value = `M ${effectiveRateFromPct(ids.maker_fee_pct.value, ids.fee_gst_pct.value)} / T ${effectiveRateFromPct(ids.taker_fee_pct.value, ids.fee_gst_pct.value)}`;
    }

    function refreshStrategyPreview() {
      renderStrategyProfile(profileFor(ids.strategy.value, ids.interval.value));
      ids.qLabel.textContent = 'Awaiting Run';
      ids.qDetail.textContent = '-';
      ids.qLabel.style.color = 'var(--amber)';
      ids.vEMA.textContent = '-';
      ids.vBB.textContent = '-';
      ids.vVis.textContent = '-';
      ids.vOI.textContent = '-';
      ids.scoreGrid.style.display = 'none';
      ids.conflictMessage.style.display = 'none';
      ids.fSummary.textContent = '-';
      ids.fReq.textContent = '-';
      ids.fAct.textContent = '-';
      ids.fEval.textContent = '-';
      ids.fCov.textContent = '-';
      ids.fRawL.textContent = '-';
      ids.fRawS.textContent = '-';
      ids.fClosed.textContent = '-';
      ids.fRate.textContent = '-';
      ids.fRawTot.textContent = '-';
      ids.fExecTot.textContent = '-';
      ids.fExpTot.textContent = '-';
      ids.fUnexpTot.textContent = '-';
      const fUnexpWrap = document.getElementById('fUnexpWrap');
      if (fUnexpWrap) fUnexpWrap.classList.remove('error');
      const fAccWarn = document.getElementById('fAccountingWarning');
      if (fAccWarn) fAccWarn.style.display = 'none';
      document.getElementById('fBlocks').innerHTML = '';
      document.getElementById('fWarning').style.display = 'none';
      ids.topChips.innerHTML = `
        <span class="chip warn">Paper</span>
        <span class="chip good">INR-M Futures</span>
        <span class="chip warn">Ready</span>
        <span class="chip bad">Live Locked</span>`;
    }

    async function runBacktest() {
      ids.runButton.disabled = true;
      setMessage('Running backtest...');
      try {
        const payload = await fetchJson('/api/backtest', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(params())
        });
        render(payload);
        const trades = (payload.counts && payload.counts.trades) || 0;
        const candles = (payload.counts && payload.counts.candles_loaded) || 0;
        const riskReason = topReason(payload.diagnostics && payload.diagnostics.entry_rejection_reasons);
        const history = payload.history || {};
        const saved = history.saved ? ` Saved to Excel history (${history.rows} rows).` : '';
        const warning = history.workbook_error ? ` Excel warning: ${history.workbook_error}` : '';
        if (trades > 0) {
          setMessage(`Backtest complete: ${trades} closed trades from ${candles} candles.${saved}${warning}`, 'good');
        } else if (riskReason) {
          setMessage(`No trades: entries were blocked by risk. ${riskReason.reason}${saved}${warning}`, 'error');
        } else {
          setMessage(`Backtest complete: ${candles} candles loaded, but this strategy opened no trades.${saved}${warning}`, 'good');
        }
      } catch (error) {
        setMessage(error.message, 'error');
      } finally {
        ids.runButton.disabled = false;
      }
    }

    function render(payload) {
      const metrics = payload.metrics || {};
      const account = payload.final_account || {};
      const config = payload.config || {};
      const counts = payload.counts || {};

      document.getElementById('mFinal').textContent = money(metrics.final_equity);
      document.getElementById('mReturn').textContent = pct(metrics.total_return_pct);
      document.getElementById('mWin').textContent = metrics.win_rate_pct === null ? '-' : pct(metrics.win_rate_pct);
      document.getElementById('mPF').textContent = metrics.profit_factor === null ? '-' : compact(metrics.profit_factor);
      document.getElementById('mDD').textContent = pct(metrics.max_drawdown_pct);
      document.getElementById('mFees').textContent = money(metrics.fees_paid);

      const trailing = config.trailing_stop_enabled
        ? ` / trailing ${compact(config.trailing_stop_activation_pct)}%/${compact(config.trailing_stop_distance_pct)}%`
        : '';
      const atrDynamic = config.atr_dynamic_exits_enabled
        ? ` / ATR ${compact(config.atr_policy_mode || 'manual')} ${config.atr_take_profit_enabled ? 'stop+TP' : 'stop-only'}`
        : '';
      const intrabar = config.execution_interval
        ? ` / exec ${config.execution_interval}${config.intrabar_reentry_enabled ? ` re-entry ${config.max_reentries_per_candle}/candle` : ''}`
        : '';
      const lossGuard = Number(config.max_consecutive_losses) > 0 || Number(config.stop_loss_cooldown_candles) > 0
        ? ` / loss guard stop ${config.stop_loss_cooldown_candles}c + ${config.max_consecutive_losses}x/${config.loss_cooldown_candles}c`
        : ' / loss guard off';
      const risk = config.risk_per_trade_pct ? ` / risk ${compact(config.risk_per_trade_pct)}%` : '';
      const riskEquity = ` / risk equity ${config.compound_risk_equity ? 'current' : 'initial'}`;
      const manualExits = [
        config.stop_loss_pct ? `SL ${compact(config.stop_loss_pct)}%` : '',
        config.take_profit_pct ? `TP ${compact(config.take_profit_pct)}%` : ''
      ].filter(Boolean).join(' ');
      const fee = config.taker_fee_rate
        ? ` / fees M ${rateToPct(config.maker_fee_rate)}% T ${rateToPct(config.taker_fee_rate)}% + GST ${rateToPct(config.fee_gst_rate)}% (${config.entry_fee_type}/${config.exit_fee_type})`
        : '';
      const stopSlip = config.stop_slippage_pct !== null && config.stop_slippage_pct !== undefined
        ? ` / stop slip ${compact(config.stop_slippage_pct)}%`
        : '';
      const funding = num(config.funding_fee_rate)
        ? ` / funding ${rateToPct(config.funding_fee_rate)}%/${config.funding_interval_hours}h`
        : '';
      const margin = (payload.bot && payload.bot.futures_margin_currency) || 'INR';
      ids.runMeta.textContent = `${config.pair} / ${margin}-M / ${config.interval} / ${config.strategy_name} / ${counts.candles_loaded || 0} candles${intrabar}${risk}${riskEquity}${manualExits ? ` / ${manualExits}` : ''}${fee}${stopSlip}${funding}${trailing}${atrDynamic}${lossGuard}`;
      document.getElementById('curveCount').textContent = `${(payload.equity_curve || []).length} points`;
      document.getElementById('tradeCount').textContent = `${counts.trades || 0} trades`;
      document.getElementById('lastUpdated').textContent = new Date(payload.generated_at).toLocaleString();

      renderStatusBadges(config, payload.bot ? payload.bot.mode : null);
      renderStrategyProfile(payload.strategy_profile || {});
      renderRunQuality(payload.run_quality || {});
      renderBars(metrics, account);
      renderDiagnostics(payload.diagnostics || {});
      renderSignalFunnel(payload.signal_funnel || {});
      renderEquityChart(payload.equity_curve || []);
      renderTrades(payload.trade_timeline || payload.trades || payload.recent_trades || []);
    }

    function renderStrategyProfile(profile) {
      ids.pMode.textContent = profile.mode || '-';
      ids.pPrimary.textContent = profile.primary || '-';
      ids.pSecondary.textContent = profile.secondary || '-';
      ids.pFilter.textContent = profile.filter || '-';
    }

    function renderRunQuality(quality) {
      const label = quality.label || 'Awaiting Run';
      const tone = quality.tone || 'warn';
      ids.qLabel.textContent = label;
      ids.qDetail.textContent = quality.detail || '-';
      ids.qLabel.style.color = tone === 'good' ? 'var(--green)' : tone === 'bad' ? 'var(--red)' : 'var(--amber)';
      ids.topChips.innerHTML = `
        <span class="chip warn">Paper</span>
        <span class="chip good">INR-M Futures</span>
        <span class="chip ${tone}">${escapeHtml(label)}</span>
        <span class="chip bad">Live Locked</span>`;
    }

    function topReason(reasons) {
      const entries = Object.entries(reasons || {});
      if (!entries.length) return null;
      entries.sort((a, b) => b[1] - a[1]);
      return { reason: entries[0][0], count: entries[0][1] };
    }

    function renderDiagnostics(diagnostics) {
      const actions = diagnostics.filled_action_counts || diagnostics.signal_action_counts || {};
      ids.sLong.textContent = actions.enter_long || 0;
      ids.sShort.textContent = actions.enter_short || 0;
      ids.sExit.textContent = Number(actions.exit_long || 0) + Number(actions.exit_short || 0);
      ids.sHold.textContent = diagnostics.hold_count || 0;

      // Handle Scoring Conflicts
      const trades = diagnostics.recent_trades || [];
      const lastTrade = trades.length > 0 ? trades[trades.length - 1] : null;

      const meta = lastTrade ? lastTrade.metadata : {};
      if (meta && meta.ema_score !== undefined) {
        ids.scoreGrid.style.display = 'grid';
        ids.vEMA.textContent = num(meta.ema_score).toFixed(2);
        ids.vBB.textContent = num(meta.bb_score).toFixed(2);
        ids.vVis.textContent = num(meta.visual_score).toFixed(2);
        ids.vOI.textContent = num(meta.open_interest_score).toFixed(2);

        if (meta.ema_opposed_direction) {
          ids.conflictMessage.style.display = 'block';
          ids.conflictMessage.textContent = 'Conflict: BB score opposed EMA direction in the last trade.';
        } else {
          ids.conflictMessage.style.display = 'none';
        }
      } else {
        ids.scoreGrid.style.display = 'none';
        ids.conflictMessage.style.display = 'none';
      }

      ids.diagnosticsMessage.className = 'message';

      const topExit = topReason(diagnostics.exit_reasons);
      if (topExit) {
        const avgByReason = diagnostics.exit_reason_average_net_pnl || {};
        ids.exitDiagnosticsMessage.className = 'message';
        ids.exitDiagnosticsMessage.textContent = `Top exit: ${topExit.reason} (${topExit.count}) / avg PnL ${money(avgByReason[topExit.reason])} / avg MFE ${money(diagnostics.average_mfe)} / avg MAE ${money(diagnostics.average_mae)} / avg R ${compact(diagnostics.average_r_multiple)}`;
      } else {
        ids.exitDiagnosticsMessage.className = 'message';
        ids.exitDiagnosticsMessage.textContent = '';
      }

      const riskReason = topReason(diagnostics.entry_rejection_reasons);
      if (riskReason) {
        ids.diagnosticsMessage.className = 'message error';
        ids.diagnosticsMessage.textContent = `Risk blocked ${riskReason.count} entry signal(s): ${riskReason.reason}`;
        return;
      }
      const ambiguousCount = Number(diagnostics.ambiguous_exit_count || 0);
      const gapCount = Number(diagnostics.gap_exit_count || 0);
      if (ambiguousCount > 0 || gapCount > 0) {
        ids.diagnosticsMessage.className = 'message';
        ids.diagnosticsMessage.textContent = `${ambiguousCount} ambiguous exit(s), ${gapCount} gap exit(s): conservative backtest fill rules applied.`;
        return;
      }
      const holdCount = Number(diagnostics.hold_count || 0);
      if (holdCount > 0) {
        ids.diagnosticsMessage.className = 'message';
        ids.diagnosticsMessage.textContent = `${holdCount} hold signal(s): no eligible setup on those candles.`;
        return;
      }
      ids.diagnosticsMessage.className = 'message';
      ids.diagnosticsMessage.textContent = '';
    }

    function renderSignalFunnel(funnel) {
      if (!funnel) return;
      const req = num(funnel.requested_candles);
      const act = num(funnel.actual_candles_loaded);
      const evalCount = num(funnel.candles_evaluated);
      const coverage = req > 0 ? (act / req) * 100 : 0;
      const rate = evalCount > 0 ? (num(funnel.executed_entries) / evalCount) * 100 : 0;

      ids.fReq.textContent = compact(req);
      ids.fAct.textContent = compact(act);
      ids.fEval.textContent = compact(evalCount);
      ids.fCov.textContent = pct(coverage);
      ids.fRawL.textContent = compact(funnel.raw_long_candidates);
      ids.fRawS.textContent = compact(funnel.raw_short_candidates);
      ids.fClosed.textContent = compact(funnel.closed_trades);
      ids.fRate.textContent = pct(rate);

      ids.fRawTot.textContent = compact(funnel.raw_candidates_total);
      ids.fExecTot.textContent = compact(funnel.executed_entries);
      ids.fExpTot.textContent = compact(funnel.explained_rejections_total);
      ids.fUnexpTot.textContent = compact(funnel.unexplained_candidates_total);

      const fUnexpWrap = document.getElementById('fUnexpWrap');
      const fAccWarn = document.getElementById('fAccountingWarning');
      if (num(funnel.unexplained_candidates_total) > 0) {
        fUnexpWrap.classList.add('error');
        fAccWarn.style.display = 'block';
      } else {
        fUnexpWrap.classList.remove('error');
        fAccWarn.style.display = 'none';
      }

      const fWarning = document.getElementById('fWarning');
      if (req > 0 && coverage < 90) {
        fWarning.textContent = `Requested ${req} candles, but only ${act} were available. Result is limited by exchange history.`;
        fWarning.style.display = 'block';
      } else {
        fWarning.style.display = 'none';
      }

      const blockReasons = funnel.block_reasons || {};
      const blockEntries = Object.entries(blockReasons)
        .map(([key, count]) => ({
          key,
          count: num(count),
          label: key.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase())
        }))
        .filter(b => b.count > 0 && b.key !== 'executed')
        .sort((a, b) => b.count - a.count);

      let topBlock = blockEntries[0] || { label: 'None', count: 0 };
      
      const fBlocks = document.getElementById('fBlocks');
      fBlocks.innerHTML = blockEntries.map(b => {
        return `<div class="signalCell"><span>${escapeHtml(b.label)}</span><strong>${compact(b.count)}</strong></div>`;
      }).join('');

      document.getElementById('fSummary').textContent = `${funnel.explained_rejections_total} explained blocks / Most common: ${topBlock.label} (${topBlock.count})`;
    }

    function renderBars(metrics, account) {
      const win = Math.max(0, Math.min(100, num(metrics.win_rate_pct)));
      const drawdown = Math.max(0, Math.min(100, num(metrics.max_drawdown_pct) * 5));
      const pf = Math.max(0, Math.min(100, num(metrics.profit_factor) * 33.333));
      const openRatio = num(account.open_notional) && num(account.equity)
        ? Math.max(0, Math.min(100, (num(account.open_notional) / num(account.equity)) * 100))
        : 0;

      document.getElementById('barWin').style.width = `${win}%`;
      document.getElementById('barDD').style.width = `${drawdown}%`;
      document.getElementById('barPF').style.width = `${pf}%`;
      document.getElementById('barOpen').style.width = `${openRatio}%`;
      document.getElementById('bWin').textContent = metrics.win_rate_pct === null ? '-' : pct(metrics.win_rate_pct);
      document.getElementById('bDD').textContent = pct(metrics.max_drawdown_pct);
      document.getElementById('bPF').textContent = metrics.profit_factor === null ? '-' : compact(metrics.profit_factor);
      document.getElementById('bOpen').textContent = money(account.open_notional);
    }

    function renderEquityChart(points) {
      if (!points.length) {
        ids.equityChart.innerHTML = '';
        return;
      }
      const values = points.map(point => num(point.equity));
      const min = Math.min(...values);
      const max = Math.max(...values);
      const spread = max - min || 1;
      const width = 960;
      const height = 320;
      const pad = 26;
      const path = points.map((point, index) => {
        const x = pad + (index / Math.max(points.length - 1, 1)) * (width - pad * 2);
        const y = height - pad - ((num(point.equity) - min) / spread) * (height - pad * 2);
        return `${index === 0 ? 'M' : 'L'} ${x.toFixed(2)} ${y.toFixed(2)}`;
      }).join(' ');
      const area = `${path} L ${width - pad} ${height - pad} L ${pad} ${height - pad} Z`;
      ids.equityChart.innerHTML = `
        <svg viewBox="0 0 ${width} ${height}" width="100%" height="320" role="img" aria-label="Equity curve">
          <rect x="0" y="0" width="${width}" height="${height}" fill="#111318"></rect>
          <path d="${area}" fill="rgba(64,201,138,0.12)"></path>
          <path d="${path}" fill="none" stroke="#40c98a" stroke-width="3"></path>
          <line x1="${pad}" y1="${height - pad}" x2="${width - pad}" y2="${height - pad}" stroke="#30343d"></line>
          <text x="${pad}" y="22" fill="#a8adb7" font-size="13">${money(max)}</text>
          <text x="${pad}" y="${height - 8}" fill="#a8adb7" font-size="13">${money(min)}</text>
        </svg>`;
    }

    function renderTrades(trades) {
      if (!trades.length) {
        ids.tradeRows.innerHTML = '<tr><td colspan="15">No closed trades</td></tr>';
        return;
      }
      ids.tradeRows.innerHTML = trades.map(trade => {
        const pnl = num(trade.net_pnl);
        const cls = pnl >= 0 ? 'good' : 'bad';
        const meta = trade.metadata || {};
        return `<tr>
          <td>${escapeHtml(trade.trade_number || '')}</td>
          <td>${escapeHtml(trade.entry_time || formatMs(trade.entry_time_ms))}</td>
          <td>${escapeHtml(trade.exit_time || formatMs(trade.exit_time_ms))}</td>
          <td>${escapeHtml(trade.duration || durationMs(num(trade.exit_time_ms) - num(trade.entry_time_ms)))}</td>
          <td>${escapeHtml(trade.pair)}</td>
          <td>${escapeHtml(trade.strategy_name)}</td>
          <td>${escapeHtml(trade.direction)}</td>
          <td>${compact(trade.entry_price)}</td>
          <td>${compact(trade.exit_price)}</td>
          <td>${compact(meta.initial_stop_loss || meta.atr_stop_loss)}</td>
          <td>${compact(meta.take_profit || meta.atr_take_profit)}</td>
          <td><span class="chip ${cls}">${money(trade.net_pnl)}</span></td>
          <td>${escapeHtml(meta.atr_profile || '-')}</td>
          <td>${compact(meta.mfe_pct)}</td>
          <td>${compact(meta.mae_pct)}</td>
          <td>${compact(meta.r_multiple)}</td>
          <td>${escapeHtml(trade.exit_reason)}</td>
        </tr>`;
      }).join('');
    }

    function formatMs(value) {
      const timestamp = num(value);
      if (!timestamp) return '-';
      return new Date(timestamp).toLocaleString();
    }

    function durationMs(value) {
      const minutesTotal = Math.max(0, Math.floor(num(value) / 60000));
      const days = Math.floor(minutesTotal / 1440);
      const hours = Math.floor((minutesTotal % 1440) / 60);
      const minutes = minutesTotal % 60;
      return [
        days ? `${days}d` : '',
        hours ? `${hours}h` : '',
        minutes || (!days && !hours) ? `${minutes}m` : ''
      ].filter(Boolean).join(' ');
    }

    window.pmEquityHistory = [];  // stores {t, equity} for chart

    function equityPointValue(point) {
      return typeof point === 'number' ? num(point) : num(point && point.equity);
    }

    function safeParseJson(value, fallback) {
      if (!value) return fallback;
      if (typeof value !== 'string') return value;
      try { return JSON.parse(value); } catch(e) { return fallback; }
    }

    function paperTradeSummaryFromRows(rows) {
      const list = rows || [];
      let wins = 0;
      let losses = 0;
      let gross = 0;
      let net = 0;
      let fees = 0;
      let winPnl = 0;
      let lossPnl = 0;
      let best = null;
      let worst = null;
      let notional = 0;

      list.forEach(t => {
        const rowNet = num(t.net_pnl || t.pnl);
        const rowGross = num(t.gross_pnl || t.pnl);
        const rowFees = num(t.total_fees || t.fees || t.fee || 0);
        const rowNotional = Math.abs(num(t.position_notional || t.notional_margin || t.notional || 0));
        gross += rowGross;
        net += rowNet;
        fees += rowFees;
        notional += rowNotional;
        best = best === null ? rowNet : Math.max(best, rowNet);
        worst = worst === null ? rowNet : Math.min(worst, rowNet);
        if (rowNet > 0) {
          wins += 1;
          winPnl += rowNet;
        } else if (rowNet < 0) {
          losses += 1;
          lossPnl += Math.abs(rowNet);
        }
      });

      return {
        closed_trades: list.length,
        wins,
        losses,
        win_rate_pct: list.length ? wins / list.length * 100 : 0,
        gross_pnl: gross,
        net_pnl: net,
        fees,
        avg_win: wins ? winPnl / wins : 0,
        avg_loss: losses ? lossPnl / losses : 0,
        best_trade: best === null ? 0 : best,
        worst_trade: worst === null ? 0 : worst,
        profit_factor: lossPnl > 0 ? winPnl / lossPnl : (winPnl > 0 ? null : 0),
        closed_notional: notional,
        fees_cost_pct: notional > 0 ? fees / notional * 100 : 0,
      };
    }

    function renderPaperEquityChart(history) {
      const chart = document.getElementById('paperEquityChart');
      if (!history || !history.length) { chart.innerHTML = ''; return; }
      
      const values = history.map(equityPointValue).filter(v => Number.isFinite(v) && v > 0);
      if (!values.length) { chart.innerHTML = ''; return; }
      const min = Math.min(...values);
      const max = Math.max(...values);
      const spread = max - min || 1;
      const width = 960; const height = 320; const pad = 26;
      
      const current = values[values.length - 1];
      const start = values[0];
      const isGreen = current >= start;
      const stroke = isGreen ? '#40c98a' : '#f06d6d';
      const fill = isGreen ? 'rgba(64,201,138,0.1)' : 'rgba(240,109,109,0.1)';
      
      const getX = (i) => pad + (i / Math.max(values.length - 1, 1)) * (width - pad * 2);
      const getY = (val) => height - pad - ((val - min) / spread) * (height - pad * 2);
      
      const path = values.map((v, i) => `${i === 0 ? 'M' : 'L'} ${getX(i).toFixed(1)} ${getY(v).toFixed(1)}`).join(' ');
      const area = `${path} L ${width - pad} ${height - pad} L ${pad} ${height - pad} Z`;
      
      // Markers
      let peakIdx = 0;
      let peakVal = -Infinity;
      values.forEach((v, i) => { if (v > peakVal) { peakVal = v; peakIdx = i; } });
      
      const peakX = getX(peakIdx);
      const peakY = getY(peakVal);
      const curX = getX(values.length - 1);
      const curY = getY(current);

      // Grid lines
      const gridVals = [min, min + spread/2, max];
      const gridLines = gridVals.map(v => `<line x1="${pad}" y1="${getY(v)}" x2="${width-pad}" y2="${getY(v)}" stroke="var(--line)" stroke-width="0.5" stroke-dasharray="4 4"/>`).join('');

      chart.innerHTML = `<svg viewBox="0 0 ${width} ${height}" width="100%" height="320">
        <rect width="${width}" height="${height}" fill="#111318"/>
        ${gridLines}
        <path d="${area}" fill="${fill}"/>
        <path d="${path}" fill="none" stroke="${stroke}" stroke-width="2.5"/>
        
        <!-- Peak Marker -->
        <circle cx="${peakX}" cy="${peakY}" r="4" fill="var(--amber)"/>
        <text x="${peakX}" y="${peakY - 8}" fill="var(--amber)" font-size="10" text-anchor="middle">Peak</text>
        
        <!-- Current Marker -->
        <circle cx="${curX}" cy="${curY}" r="4" fill="${stroke}"/>
        
        <text x="${pad}" y="20" fill="#a8adb7" font-size="12" font-weight="700">${money(max)}</text>
        <text x="${pad}" y="${height - 6}" fill="#a8adb7" font-size="12" font-weight="700">${money(min)}</text>
      </svg>`;
    }

    function renderPaperCandleChart(payload) {
      const chart = document.getElementById('paperCandleChart');
      const pairChip = document.getElementById('pmCandlePairChip');
      const countChip = document.getElementById('pmCandleCountChip');
      const data = typeof payload === 'string' ? safeParseJson(payload, {}) : (payload || {});
      const candles = Array.isArray(data.candles) ? data.candles : [];

      if (pairChip) pairChip.textContent = data.pair ? `${data.pair} ${data.interval || ''}` : '-';
      if (countChip) countChip.textContent = `${candles.length} candles`;
      if (!candles.length) {
        chart.innerHTML = '<div style="height:100%;display:flex;align-items:center;justify-content:center;color:var(--muted);font-size:13px">Waiting for live candles...</div>';
        return;
      }

      const visible = candles.slice(-80);
      const highs = visible.map(c => num(c.high));
      const lows = visible.map(c => num(c.low));
      const closes = visible.map(c => num(c.close));
      const min = Math.min(...lows);
      const max = Math.max(...highs);
      const spread = max - min || 1;
      const width = 960; const height = 320; const pad = 28;
      const innerW = width - pad * 2;
      const candleW = Math.max(3, Math.min(10, innerW / Math.max(visible.length, 1) * 0.58));
      const getX = i => pad + (i / Math.max(visible.length - 1, 1)) * innerW;
      const getY = val => height - pad - ((val - min) / spread) * (height - pad * 2);

      const gridVals = [min, min + spread / 2, max];
      const gridLines = gridVals.map(v => `<line x1="${pad}" y1="${getY(v)}" x2="${width-pad}" y2="${getY(v)}" stroke="var(--line)" stroke-width="0.5" stroke-dasharray="4 4"/>`).join('');
      const candleNodes = visible.map((c, i) => {
        const x = getX(i);
        const open = num(c.open);
        const close = num(c.close);
        const high = num(c.high);
        const low = num(c.low);
        const up = close >= open;
        const color = up ? '#40c98a' : '#f06d6d';
        const yOpen = getY(open);
        const yClose = getY(close);
        const yHigh = getY(high);
        const yLow = getY(low);
        const bodyY = Math.min(yOpen, yClose);
        const bodyH = Math.max(1.5, Math.abs(yClose - yOpen));
        return `<g>
          <line x1="${x.toFixed(1)}" y1="${yHigh.toFixed(1)}" x2="${x.toFixed(1)}" y2="${yLow.toFixed(1)}" stroke="${color}" stroke-width="1"/>
          <rect x="${(x - candleW / 2).toFixed(1)}" y="${bodyY.toFixed(1)}" width="${candleW.toFixed(1)}" height="${bodyH.toFixed(1)}" fill="${color}" opacity="0.86"/>
        </g>`;
      }).join('');

      const closePath = closes.map((v, i) => `${i === 0 ? 'M' : 'L'} ${getX(i).toFixed(1)} ${getY(v).toFixed(1)}`).join(' ');
      const last = visible[visible.length - 1];
      const lastClose = num(last.close);
      const lastX = getX(visible.length - 1);
      const lastY = getY(lastClose);

      chart.innerHTML = `<svg viewBox="0 0 ${width} ${height}" width="100%" height="320">
        <rect width="${width}" height="${height}" fill="#111318"/>
        ${gridLines}
        ${candleNodes}
        <path d="${closePath}" fill="none" stroke="#67d7e5" stroke-width="1.4" opacity="0.85"/>
        <circle cx="${lastX.toFixed(1)}" cy="${lastY.toFixed(1)}" r="3.5" fill="#67d7e5"/>
        <text x="${pad}" y="20" fill="#a8adb7" font-size="12" font-weight="700">High ${compact(max)}</text>
        <text x="${pad}" y="${height - 6}" fill="#a8adb7" font-size="12" font-weight="700">Low ${compact(min)}</text>
        <text x="${Math.min(width - pad - 90, lastX + 8).toFixed(1)}" y="${Math.max(18, lastY - 8).toFixed(1)}" fill="#67d7e5" font-size="12" font-weight="800">Close ${compact(lastClose)}</text>
      </svg>`;
    }

    function renderPaperTrades(trades) {
      const rows = trades || [];
      const tbody = document.getElementById('pmTradeRows');
      const countChip = document.getElementById('pmTradeCount');
      const winChip = document.getElementById('pmWinChip');
      
      countChip.textContent = `${rows.length} trades`;

      if (!rows.length) {
        tbody.innerHTML = '<tr><td colspan="15">No closed trades yet - open positions will appear here when they close</td></tr>';
        winChip.textContent = '-';
        winChip.className = 'chip';
        return;
      }

      let wins = 0;
      tbody.innerHTML = rows.map((t, i) => {
        const pnl = num(t.net_pnl || t.pnl);
        const gross = num(t.gross_pnl || t.pnl);
        const isWin = pnl > 0;
        if (isWin) wins++;

        const side = escapeHtml(t.direction || t.side || '');
        const qtyUnit = t.quantity_unit || 'contracts';
        const qty = `${compact(t.position_size || t.quantity || 0)} ${escapeHtml(qtyUnit)}`;
        const notional = num(t.position_notional || t.notional_margin || 0);
        const currency = escapeHtml(t.notional_currency || 'INR');
        const legacy = String(t.legacy_currency_math || '').toLowerCase() === 'true';
        const totalFees = num(t.total_fees || t.fees || t.fee || 0);
        const riskUsed = t.risk_percent_used !== undefined && t.risk_percent_used !== ''
          ? `${num(t.risk_percent_used).toFixed(2)}%`
          : '-';
        const reason = escapeHtml(t.exit_reason || t.reason || '');
        const time = escapeHtml(t.timestamp || t.exit_time || '-');
        const hold = t.hold_duration_candles !== undefined ? `${t.hold_duration_candles}c` : '-';
        const netPct = t.net_pnl_pct !== undefined ? `${num(t.net_pnl_pct).toFixed(2)}%` : '-';

        return `<tr>
          <td>${rows.length - i}</td>
          <td>${time}</td>
          <td>${escapeHtml(t.pair || '')}</td>
          <td style="color:${side.toLowerCase()==='long'?'var(--green)':'var(--red)'}">${side}</td>
          <td>${compact(t.entry_price)}</td>
          <td>${compact(t.exit_price)}</td>
          <td>${qty}</td>
          <td>${money(notional)} <small style="color:${legacy ? 'var(--warn)' : 'var(--muted)'}">${currency}${legacy ? ' legacy' : ''}</small></td>
          <td>${riskUsed}</td>
          <td>${money(gross)}</td>
          <td>${money(totalFees)}</td>
          <td><span class="chip ${isWin?'good':'bad'}">${money(pnl)}</span></td>
          <td style="color:${isWin?'var(--green)':'var(--red)'}">${netPct}</td>
          <td>${hold}</td>
          <td>${reason}</td>
        </tr>`;
      }).join('');

      const wr = (wins / rows.length * 100).toFixed(1);
      winChip.textContent = `${wr}% Win Rate (${wins}W / ${rows.length - wins}L)`;
      winChip.className = `chip ${wr >= 50 ? 'good' : 'warn'}`;
    }

    function renderPaperPositions(positions) {
      const container = document.getElementById('pmPositionsBody');
      const countEl = document.getElementById('pmOpenCount');

      if (!positions || !positions.length) {
        container.innerHTML = '<p style="color:var(--muted);font-size:13px;padding:8px 0">No open positions</p>';
        countEl.textContent = '0';
        return;
      }

      countEl.textContent = positions.length;
      container.innerHTML = positions.map(p => {
        const pnl = num(p.unrealized_pnl);
        const pnlPct = num(p.unrealized_pnl_pct || 0); // fallback if not provided
        const pnlCls = pnl >= 0 ? 'good' : 'bad';
        const dirCls = p.direction === 'long' ? 'good' : 'bad';
        const notional = num(p.notional);
        const riskUsed = p.risk_percent_used !== undefined && p.risk_percent_used !== ''
          ? `${num(p.risk_percent_used).toFixed(2)}%`
          : '-';
        const priceOrDash = value => (value !== undefined && value !== null && String(value) !== '')
          ? compact(value)
          : '-';
        const auditItems = [
          ['Active Trail', priceOrDash(p.active_trailing_stop || p.stop_loss)],
          ['TP Suppressed', boolVal(p.take_profit_suppressed_by_trailing) ? 'Yes' : 'No'],
          ['ATR Best', priceOrDash(p.atr_best_price)],
          ['Stop Type', p.stop_type ? String(p.stop_type) : '-']
        ];

        const footerItems = [
          p.strategy,
          p.stop_loss ? `SL ${compact(p.stop_loss)}` : '',
          p.take_profit ? `TP ${compact(p.take_profit)}` : ''
        ].filter(Boolean);

        return `
          <div class="posCard">
            <div class="posTop">
              <div class="posName">
                <span class="chip ${dirCls}">${p.direction.toUpperCase()}</span>
                ${escapeHtml(p.pair)}
              </div>
              <div class="chip ${pnlCls}" style="font-weight:800">${money(pnl)}</div>
            </div>
            <div class="posGrid">
              <div><span>Qty (${escapeHtml(p.quantity_unit || 'contracts')})</span><strong>${compact(p.quantity)}</strong></div>
              <div><span>Entry</span><strong>${compact(p.entry_price)}</strong></div>
              <div><span>Notional (${escapeHtml(p.notional_currency || 'INR')})</span><strong>${money(notional)}</strong></div>
              <div><span>Risk Used</span><strong>${riskUsed}</strong></div>
            </div>
            <div class="posAudit">
              ${auditItems.map(([label, value]) => `
                <div class="posAuditItem">
                  <span>${escapeHtml(label)}</span>
                  <strong>${escapeHtml(value)}</strong>
                </div>
              `).join('')}
            </div>
            ${footerItems.length > 0 ? `
              <div class="posFooter">
                ${footerItems.map(item => `<span>${escapeHtml(item)}</span>`).join('')}
              </div>
            ` : ''}
          </div>
        `;
      }).join('');
    }
    function renderPaperSessionStats(trades, summary) {
      const rows = trades || [];
      const bar = document.getElementById('pmStatsBar');
      const stats = summary || paperTradeSummaryFromRows(rows);

      if (!num(stats.closed_trades)) {
        bar.style.display = 'none';
        return;
      }
      bar.style.display = 'grid';

      const avgWin = num(stats.avg_win);
      const avgLoss = num(stats.avg_loss);
      const best = num(stats.best_trade);
      const worst = num(stats.worst_trade);
      const pfRaw = stats.profit_factor;
      const pf = pfRaw === null || pfRaw === undefined || pfRaw === ''
        ? (num(stats.net_pnl) > 0 ? 'inf' : '-')
        : num(pfRaw).toFixed(2);

      document.getElementById('pmAvgWin').textContent = money(avgWin);
      document.getElementById('pmAvgWin').style.color = 'var(--green)';

      document.getElementById('pmAvgLoss').textContent = money(avgLoss);
      document.getElementById('pmAvgLoss').style.color = 'var(--red)';

      document.getElementById('pmBestTrade').textContent = money(best);
      document.getElementById('pmBestTrade').style.color = best > 0 ? 'var(--green)' : 'var(--text)';

      document.getElementById('pmWorstTrade').textContent = money(worst);
      document.getElementById('pmWorstTrade').style.color = worst < 0 ? 'var(--red)' : 'var(--text)';

      document.getElementById('pmProfitFactor').textContent = pf;
      if (pf !== '-' && pf !== 'inf') {
        document.getElementById('pmProfitFactor').style.color = num(pf) >= 1.5 ? 'var(--green)' : num(pf) < 1 ? 'var(--red)' : 'var(--text)';
      } else if (pf === 'inf') {
        document.getElementById('pmProfitFactor').style.color = 'var(--green)';
      }

      document.getElementById('pmTotalClosed').textContent = compact(stats.closed_trades);
    }

    async function refreshPaperStatus() {
      try {
        const [data, tradesData] = await Promise.all([
          fetchJson('/api/paper-status'),
          fetchJson('/api/paper-trades').catch(() => ({ trades: [] }))
        ]);

        const running = data.running;
        const trades = tradesData.trades || [];
        const tradeSummary = tradesData.summary || paperTradeSummaryFromRows(trades);
        const set = (id, val) => { const el = document.getElementById(id); if (el) el.textContent = val; };
        const col = (id, good) => { const el = document.getElementById(id); if (el) el.style.color = good ? 'var(--green)' : 'var(--red)'; };

        document.getElementById('pmChipStatus').textContent = running ? 'Live' : 'Stopped';
        document.getElementById('pmChipStatus').className = 'chip ' + (running ? 'good' : '');
        
        if (data.error) {
          document.getElementById('pmError').textContent = 'Error: ' + data.error;
        } else {
          document.getElementById('pmError').textContent = '';
        }

        const equity = num(data.equity);
        const startEq = num(data.starting_equity) || num(document.getElementById('p_starting_equity').value);
        const candleCount = num(data.candle_count);
        
        const hasUsableEquity = equity > 0;
        let displayEquity = hasUsableEquity ? equity : startEq;
        let returnAbs = displayEquity - startEq;
        let returnPct = startEq > 0 ? (returnAbs / startEq * 100) : 0;
        if (data.return_abs !== undefined && data.return_abs !== '') {
          returnAbs = num(data.return_abs);
        }
        if (data.return_pct !== undefined && data.return_pct !== '') {
          returnPct = num(data.return_pct);
        }
        
        const pnl = num(tradeSummary.net_pnl);
        const grossClosedPnl = num(tradeSummary.gross_pnl);
        const fees = num(data.fees_paid);

        // Update Row 1
        set('pmEquity', money(displayEquity));
        set('pmEquityBase', `Initial: ${money(startEq)}`);
        
        set('pmReturn', returnPct.toFixed(2) + '%');
        col('pmReturn', returnPct >= 0);
        
        const absSign = returnAbs >= 0 ? '+' : '';
        set('pmReturnAbs', absSign + money(Math.abs(returnAbs)) + (returnAbs < 0 ? ' loss' : ' gain'));
        col('pmReturnAbs', returnAbs >= 0);
        
        set('pmPnl', money(pnl));
        col('pmPnl', pnl >= 0);
        set('pmPnlSub', `Closed gross ${money(grossClosedPnl)}`);
        
        set('pmFees', money(fees));
        const feeCostPct = tradeSummary.fees_cost_pct !== undefined
          ? num(tradeSummary.fees_cost_pct)
          : (fees / Math.max(1, num(tradeSummary.closed_notional) || displayEquity)) * 100;
        set('pmFeesSub', `${feeCostPct.toFixed(3)}% closed notional`);

        // Update Row 2
        // Win Rate
        const closedTrades = num(tradeSummary.closed_trades);
        const wins = num(tradeSummary.wins);
        const losses = num(tradeSummary.losses);
        if (closedTrades > 0) {
          const wr = num(tradeSummary.win_rate_pct);
          set('pmWinRate', wr.toFixed(1) + '%');
          set('pmWinRateSub', `${wins}W / ${losses}L`);
        } else {
          set('pmWinRate', '0.0%');
          set('pmWinRateSub', 'No closed trades');
        }

        if (!window.pmEquityHistory) window.pmEquityHistory = [];
        const serverEquityHistory = safeParseJson(data.equity_history_json, []);
        if (Array.isArray(serverEquityHistory) && serverEquityHistory.length > 0) {
          window.pmEquityHistory = serverEquityHistory
            .filter(p => equityPointValue(p) > 0)
            .slice(-500);
        } else if (equity > 0) {
          const lastPoint = window.pmEquityHistory[window.pmEquityHistory.length - 1];
          if (!lastPoint || Math.abs(equityPointValue(lastPoint) - equity) > 0.000001) {
            window.pmEquityHistory.push({t: data.last_updated || Date.now(), equity});
            if (window.pmEquityHistory.length > 500) window.pmEquityHistory.shift();
          }
        }
        
        if (window.pmEquityHistory.length > 0) {
          let maxDD = 0;
          let peak = startEq;
          for (const point of window.pmEquityHistory) {
            const val = equityPointValue(point);
            if (val > peak) peak = val;
            if (peak > 0) {
              const dd = ((peak - val) / peak) * 100;
              if (dd > maxDD) maxDD = dd;
            }
          }
          if (data.max_drawdown_pct !== undefined && data.max_drawdown_pct !== '') {
            maxDD = num(data.max_drawdown_pct);
          }
          if (data.peak_equity !== undefined && data.peak_equity !== '') {
            peak = num(data.peak_equity) || peak;
          }
          set('pmMaxDD', maxDD.toFixed(2) + '%');
          const ddEl = document.getElementById('pmMaxDD');
          if (ddEl) ddEl.style.color = maxDD < 3 ? 'var(--green)' : maxDD < 8 ? 'var(--amber, #f59e0b)' : 'var(--red)';
          set('pmPeakLabel', 'Peak: ' + money(peak));
          renderPaperEquityChart(window.pmEquityHistory);
        }
        renderPaperCandleChart(data.candles_json);

        set('pmCandles', data.candle_count);
        set('pmCandlesSub', running ? `Interval: ${data.interval}` : 'Standby');
        
        set('pmFillCount', (data.total_fills || 0));
        set('pmFillCountChip', (data.total_fills || 0) + ' fills');
        set('pmFillSub', `${data.open_positions} open pos`);

        // BUG 3 Fix: Fill Info line
        const fillInfo = document.getElementById('pmFillInfo');
        if (fillInfo) {
          const fills = parseInt(data.total_fills || '0');
          const openPos = parseInt(data.open_positions || '0');
          const entriesOpen = openPos > 0 ? openPos + ' entr' + (openPos > 1 ? 'ies' : 'y') + ' open' : '';
          fillInfo.textContent = fills > 0
            ? fills + ' total fill' + (fills > 1 ? 's' : '') + (entriesOpen ? ' | ' + entriesOpen : '')
            : '';
        }

        if (running) {
          // Fix invalid date rendering
          let lastUpdatedStr = 'pending';
          if (data.last_updated) {
              const d = new Date(data.last_updated);
              if (!isNaN(d.getTime())) {
                  lastUpdatedStr = d.toLocaleTimeString();
              }
          }

          document.getElementById('paperLoopMeta').innerHTML =
            `<strong>Running:</strong> ${data.pair} | INR-M | ${data.interval} | ${data.strategy} | <span class="subtle">updated ${lastUpdatedStr}</span>`;
          document.getElementById('paperTopChips').innerHTML =
            `<span class="chip warn">Paper</span>
             <span class="chip good">INR-M Futures</span>
             <span class="chip good">Live</span>
             <span class="chip bad">Live Orders Locked</span>`;
          
          // Task 3: Separate selected and running pair
          const runningLabelWrap = document.getElementById('runningPairLabelWrap');
          const runningLabel = document.getElementById('runningPairLabel');
          if (runningLabelWrap && runningLabel && data.pair) {
            runningLabelWrap.style.display = 'block';
            runningLabel.textContent = data.pair;
          }

          // Sync UI selection to running pair if it's different and NOT manually changed?
          // For now just keep them separate as requested.
        } else {
          document.getElementById('paperLoopMeta').textContent = 'Standby | Ready to start INR-M session';
          const runningLabelWrap = document.getElementById('runningPairLabelWrap');
          if (runningLabelWrap) runningLabelWrap.style.display = 'none';
        }

        // Open positions
        let positions = [];
        try { positions = JSON.parse(data.positions_json || '[]'); } catch(e) {}
        renderPaperPositions(positions);

        // Sync start/stop button states
        document.getElementById('startPaperBtn').disabled = running;
        document.getElementById('stopPaperBtn').disabled = !running;
        document.getElementById('paperRunChip').textContent = running ? 'Running' : 'Stopped';
        document.getElementById('paperRunChip').className = 'chip ' + (running ? 'good' : '');
        if (running) document.getElementById('paperRunMeta').textContent =
          `${data.pair} ${data.interval}`;

        renderPaperTrades(trades);
        renderPaperSessionStats(trades, tradeSummary);

        // Update Watchlist UI
        const watchlist = data.watchlist || [];
        const scanned = data.scanned_pairs || {};
        const pWatchlistDisplay = document.getElementById('pWatchlistDisplay');
        const pScannedCount = document.getElementById('pScannedCount');
        const pWatchlistWarning = document.getElementById('pWatchlistWarning');

        if (pScannedCount) pScannedCount.textContent = `${watchlist.length} pair${watchlist.length === 1 ? '' : 's'}`;
        
        if (pWatchlistWarning) {
          pWatchlistWarning.style.display = (running && watchlist.length === 1) ? 'block' : 'none';
        }

        if (pWatchlistDisplay) {
          if (watchlist.length === 0) {
            pWatchlistDisplay.textContent = running ? 'No pairs in watchlist.' : 'Waiting for loop...';
          } else {
            pWatchlistDisplay.innerHTML = watchlist.map(pair => {
              const status = scanned[pair] || 'Initializing...';
              return `<div style="padding: 8px; background: #1a1d24; border-radius: 4px; border-left: 3px solid var(--cyan);">
                <div style="font-weight: bold; margin-bottom: 2px;">${escapeHtml(pair)}</div>
                <div style="font-size: 0.75rem; color: #888;">${escapeHtml(status)}</div>
              </div>`;
            }).join('');
          }
        }

      } catch(e) {
        // silently ignore connection errors
      }
    }

    async function startPaper() {
      const selectedPair = getSelectedPair('p_');
      const watchlistRaw = document.getElementById('p_watchlist').value.trim();
      const pairs = watchlistRaw ? watchlistRaw.split(',').map(s => s.trim()) : [selectedPair];
      
      const payload = {
        pairs,
        interval: document.getElementById('p_interval').value,
        strategy: document.getElementById('p_strategy').value,
        starting_equity: document.getElementById('p_starting_equity').value,
        leverage: document.getElementById('p_leverage').value,
        risk_pct: document.getElementById('p_risk_pct').value,
        max_daily_loss_pct: document.getElementById('p_max_daily_loss_pct').value,
        max_open_positions: document.getElementById('p_max_open').value,
        max_margin_usage_pct: document.getElementById('p_max_margin').value,
        allow_multi_pair_positions: document.getElementById('p_multi_pair').checked,
        allow_same_pair_pyramiding: document.getElementById('p_pyramiding').checked,
        trailing_stop_enabled: document.getElementById('p_trailing_stop').checked,
        atr_dynamic_exits_enabled: document.getElementById('p_atr_exits').checked,
        profit_lock_enabled: document.getElementById('p_profit_lock').checked,
        paper_intrabar_enabled: document.getElementById('p_intrabar').checked,
        strategy_interval: document.getElementById('p_interval').value,
        execution_interval: document.getElementById('p_exec_interval').value,
        use_partial_parent_candle: document.getElementById('p_partial_htf').checked,
        max_entries_per_parent_candle: document.getElementById('p_max_entries').value,
      };
      document.getElementById('startPaperBtn').disabled = true;
      document.getElementById('paperMessage').textContent = 'Starting...';
      try {
        await fetchJson('/api/paper-start', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify(payload)
        });
        document.getElementById('stopPaperBtn').disabled = false;
        document.getElementById('paperMessage').textContent = 'Paper loop started. Loading live candles...';
        document.getElementById('paperMessage').className = 'message good';
      } catch(e) {
        document.getElementById('startPaperBtn').disabled = false;
        document.getElementById('paperMessage').textContent = e.message;
        document.getElementById('paperMessage').className = 'message error';
      }
    }

    function refreshExecIntervalOptions() {
      const strategyInterval = document.getElementById('p_interval').value;
      const execSelect = document.getElementById('p_exec_interval');
      const allOptions = ['1m', '5m', '15m'];
      
      const ms = { '1m': 60, '3m': 180, '5m': 300, '15m': 900, '30min': 1800, '1h': 3600, '4h': 14400, '1d': 86400 };
      const strategyMs = ms[strategyInterval] || 900;
      
      const currentVal = execSelect.value;
      execSelect.innerHTML = '';
      allOptions.forEach(opt => {
        if ((ms[opt] || 60) <= strategyMs) {
          const el = document.createElement('option');
          el.value = opt;
          el.textContent = opt;
          execSelect.appendChild(el);
        }
      });
      
      if ([...execSelect.options].some(o => o.value === currentVal)) {
        execSelect.value = currentVal;
      } else {
        execSelect.value = '1m';
      }
    }

    async function addCurrentToWatchlist() {
      const pair = getSelectedPair('p_');
      try {
        await fetchJson('/api/paper-add-pair', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({ pair })
        });
        document.getElementById('paperMessage').textContent = `Added ${pair} to watchlist.`;
        document.getElementById('paperMessage').className = 'message good';
        refreshPaperStatus();
      } catch(e) {
        document.getElementById('paperMessage').textContent = e.message;
        document.getElementById('paperMessage').className = 'message error';
      }
    }

    document.getElementById('p_interval').addEventListener('change', refreshExecIntervalOptions);
    document.getElementById('p_intrabar').addEventListener('change', (e) => {
        document.getElementById('p_intrabar_settings').style.display = e.target.checked ? 'flex' : 'none';
    });
    
    refreshExecIntervalOptions();

    async function stopPaper() {
      document.getElementById('stopPaperBtn').disabled = true;
      try {
        await fetchJson('/api/paper-stop', {method: 'POST', 
                        headers: {'Content-Type': 'application/json'}, body: '{}'});
        document.getElementById('startPaperBtn').disabled = false;
        document.getElementById('paperMessage').textContent = 'Paper loop stopped.';
        document.getElementById('paperMessage').className = 'message warn';
      } catch(e) {
        document.getElementById('stopPaperBtn').disabled = false;
        document.getElementById('paperMessage').textContent = e.message;
        document.getElementById('paperMessage').className = 'message error';
      }
    }

    async function resetPaper() {
      if (!confirm('Are you sure you want to RESET all paper trading progress? This will clear all history and positions.')) return;
      
      try {
        await fetchJson('/api/paper-reset', {method: 'POST', 
                        headers: {'Content-Type': 'application/json'}, body: '{}'});
        window.pmEquityHistory = [];
        document.getElementById('paperMessage').textContent = 'Paper session reset.';
        document.getElementById('paperMessage').className = 'message warn';
        // Refresh UI
        refreshPaperStatus();
      } catch(e) {
        document.getElementById('paperMessage').textContent = e.message;
        document.getElementById('paperMessage').className = 'message error';
      }
    }

    document.getElementById('startPaperBtn').addEventListener('click', startPaper);
    document.getElementById('stopPaperBtn').addEventListener('click', stopPaper);
    document.getElementById('resetPaperBtn').addEventListener('click', resetPaper);

    setInterval(refreshPaperStatus, 4000);
    refreshPaperStatus();

    ids.runButton.addEventListener('click', runBacktest);
    ids.strategy.addEventListener('change', refreshStrategyPreview);
    ids.interval.addEventListener('change', refreshStrategyPreview);
    ids.maker_fee_pct.addEventListener('input', refreshFeeRatePreview);
    ids.taker_fee_pct.addEventListener('input', refreshFeeRatePreview);
    ids.fee_gst_pct.addEventListener('input', refreshFeeRatePreview);
    loadStatus()
      .then(() => setMessage('Ready. Pick a pair and click Run Backtest.'))
      .catch(error => setMessage(error.message, 'error'));
  </script>
</body>
</html>
"""
