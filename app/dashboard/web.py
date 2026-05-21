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

    @media (max-width: 1120px) {
      .shell { grid-template-columns: 1fr; }
      aside { border-right: 0; border-bottom: 1px solid var(--line); }
      .metrics { grid-template-columns: repeat(3, 1fr); }
      .strategyBand { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .grid { grid-template-columns: 1fr; }
    }

    @media (max-width: 680px) {
      main, aside { padding: 14px; }
      .topbar { display: grid; }
      .chips { justify-content: flex-start; }
      .metrics { grid-template-columns: 1fr 1fr; }
      .strategyBand { grid-template-columns: 1fr; }
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
          <label>Pair
            <select id="pair">
              <option value="B-SOL_USDT">SOL-USDT</option>
              <option value="B-ETH_USDT">ETH-USDT</option>
              <option value="B-BTC_USDT">BTC-USDT</option>
              <option value="B-ZEC_USDT">ZEC-USDT</option>
              <option value="B-BNB_USDT">BNB-USDT</option>
              <option value="B-AIGENSYN_USDT">AIGENSYN-USDT</option>
              <option value="B-RAVE_USDT">RAVE-USDT</option>
              <option value="B-BSB_USDT">BSB-USDT</option>
              <option value="B-SAGA_USDT">SAGA-USDT</option>
              <option value="B-SAHARA_USDT">SAHARA-USDT</option>
              <option value="B-STABLE_USDT">STABLE-USDT</option>
              <option value="B-CHIP_USDT">CHIP-USDT</option>
              <option value="B-KITE_USDT">KITE-USDT</option>
              <option value="B-MEGA_USDT">MEGA-USDT</option>
              <option value="B-BASED_USDT">BASED-USDT</option>
              <option value="B-STO_USDT">STO-USDT</option>
              <option value="B-KAITO_USDT">KAITO-USDT</option>
              <option value="B-SKY_USDT">SKY-USDT</option>
              <option value="B-PIXEL_USDT">PIXEL-USDT</option>
              <option value="B-ALICE_USDT">ALICE-USDT</option>
            </select>
          </label>

          <label>Custom Pair
            <input id="custom_pair" type="text" placeholder="B-ZEC_USDT" />
          </label>

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

          <label>Pair
            <select id="p_pair">
              <option value="B-SOL_USDT">SOL-USDT</option>
              <option value="B-ETH_USDT">ETH-USDT</option>
              <option value="B-BTC_USDT">BTC-USDT</option>
              <option value="B-ZEC_USDT">ZEC-USDT</option>
              <option value="B-BNB_USDT">BNB-USDT</option>
              <option value="B-AIGENSYN_USDT">AIGENSYN-USDT</option>
              <option value="B-RAVE_USDT">RAVE-USDT</option>
              <option value="B-BSB_USDT">BSB-USDT</option>
              <option value="B-SAGA_USDT">SAGA-USDT</option>
              <option value="B-SAHARA_USDT">SAHARA-USDT</option>
              <option value="B-STABLE_USDT">STABLE-USDT</option>
              <option value="B-CHIP_USDT">CHIP-USDT</option>
              <option value="B-KITE_USDT">KITE-USDT</option>
              <option value="B-MEGA_USDT">MEGA-USDT</option>
              <option value="B-BASED_USDT">BASED-USDT</option>
              <option value="B-STO_USDT">STO-USDT</option>
              <option value="B-KAITO_USDT">KAITO-USDT</option>
              <option value="B-SKY_USDT">SKY-USDT</option>
              <option value="B-PIXEL_USDT">PIXEL-USDT</option>
              <option value="B-ALICE_USDT">ALICE-USDT</option>
            </select>
          </label>
          <label>Custom Pair
            <input id="p_custom_pair" type="text" placeholder="B-ZEC_USDT" />
          </label>
          <div class="split">
            <label>Interval
              <select id="p_interval">
                <option>1h</option><option value="2h">2hr</option>
                <option>4h</option><option>15m</option><option>5m</option>
                <option value="30min">30min</option><option>1m</option>
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

          <label>Starting Equity (USDT)
            <input id="p_starting_equity" type="number" min="10" step="100" value="10000" />
          </label>

          <div class="split">
            <label>Leverage
              <input id="p_leverage" type="number" min="1" max="20" step="1" value="5" />
            </label>
            <label>Risk / Trade %
              <input id="p_risk_pct" type="number" min="0.1" step="0.1" value="1" />
            </label>
          </div>
          <label>Max Daily Loss %
            <input id="p_max_daily_loss_pct" type="number" min="0.1" step="0.1" value="3" />
          </label>

          <label class="checkLine">
            <span>Trailing Stop</span>
            <input id="p_trailing_stop" type="checkbox" />
          </label>
          <label class="checkLine">
            <span>ATR Dynamic Exits</span>
            <input id="p_atr_exits" type="checkbox" />
          </label>

          <hr style="border:0; border-top:1px solid var(--line); margin:8px 0" />
          
          <label class="checkLine">
            <span>Intrabar Execution</span>
            <input id="p_intrabar" type="checkbox" />
          </label>
          
          <div id="p_intrabar_settings" style="display:none; gap:12px; flex-direction:column">
            <div class="split">
              <label>Exec Interval
                <select id="p_exec_interval">
                  <option value="1m">1m</option>
                  <option value="5m">5m</option>
                </select>
              </label>
              <label>Max Entries/Candle
                <input id="p_max_entries" type="number" min="1" max="10" value="1" />
              </label>
            </div>
            <label class="checkLine">
              <span>Partial HTF Candle</span>
              <input id="p_partial_htf" type="checkbox" />
            </label>
          </div>

          <div class="split">
            <button id="startPaperBtn" type="button" style="background:var(--green)">
              ▶ Start
            </button>
            <button id="stopPaperBtn" type="button" 
                    style="background:var(--red);color:var(--text)" disabled>
              ■ Stop
            </button>
          </div>

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
            <span class="chip good">Futures</span>
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
            <span class="chip good">Futures</span>
            <span class="chip bad">Live Locked</span>
          </div>
        </div>

        <!-- Live equity metrics row -->
        <section class="metrics" style="grid-template-columns: repeat(5, minmax(120px, 1fr))">
          <div class="metric"><span>Live Equity</span><strong id="pmEquity">-</strong></div>
          <div class="metric"><span>Return</span><strong id="pmReturn">-</strong></div>
          <div class="metric"><span>Realized PnL</span><strong id="pmPnl">-</strong></div>
          <div class="metric"><span>Fees Paid</span><strong id="pmFees">-</strong></div>
          <div class="metric"><span>Candles</span><strong id="pmCandles">-</strong></div>
        </section>

        <!-- Live equity chart -->
        <section class="grid" style="margin-bottom:12px">
          <div class="panel">
            <div class="panelHeader">
              <h3>Live Equity Curve</h3>
              <span class="chip" id="pmChipStatus">Stopped</span>
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

        <!-- Recent closed trades -->
        <section class="panel" style="margin-top:0">
          <div class="panelHeader">
            <h3>Closed Trades</h3>
            <span class="chip" id="pmFillCount">0 fills</span>
          </div>
          <div class="tableWrap" style="max-height:320px">
            <table>
              <thead>
                <tr>
                  <th>Pair</th><th>Dir</th><th>Entry</th>
                  <th>Exit</th><th>SL</th><th>TP</th><th>Qty</th><th>Net PnL</th><th>Reason</th>
                </tr>
              </thead>
              <tbody id="pmTradeRows">
                <tr><td colspan="9">No closed trades yet</td></tr>
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
      custom_pair: document.getElementById('custom_pair'),
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
      conflictMessage: document.getElementById('conflictMessage')
    };

    const num = value => {
      const parsed = Number(value);
      return Number.isFinite(parsed) ? parsed : 0;
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
        pair: defaults.pair,
        custom_pair: '',
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
        pair: ids.custom_pair.value.trim() || ids.pair.value,
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
        <span class="chip good">Futures</span>
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
      ids.runMeta.textContent = `${config.pair} / ${config.interval} / ${config.strategy_name} / ${counts.candles_loaded || 0} candles${intrabar}${risk}${riskEquity}${manualExits ? ` / ${manualExits}` : ''}${fee}${stopSlip}${funding}${trailing}${atrDynamic}${lossGuard}`;
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
        <span class="chip good">Futures</span>
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

    const pmEquityHistory = [];  // stores {t, equity} for chart

    function renderPaperEquityChart(history) {
      const chart = document.getElementById('paperEquityChart');
      if (!history.length) { chart.innerHTML = ''; return; }
      const values = history.map(p => num(p.equity));
      const min = Math.min(...values);
      const max = Math.max(...values);
      const spread = max - min || 1;
      const width = 960; const height = 320; const pad = 26;
      const isGreen = values[values.length - 1] >= values[0];
      const stroke = isGreen ? '#40c98a' : '#f06d6d';
      const fill = isGreen ? 'rgba(64,201,138,0.1)' : 'rgba(240,109,109,0.1)';
      const path = history.map((p, i) => {
        const x = pad + (i / Math.max(history.length - 1, 1)) * (width - pad * 2);
        const y = height - pad - ((num(p.equity) - min) / spread) * (height - pad * 2);
        return `${i === 0 ? 'M' : 'L'} ${x.toFixed(1)} ${y.toFixed(1)}`;
      }).join(' ');
      const area = `${path} L ${width - pad} ${height - pad} L ${pad} ${height - pad} Z`;
      chart.innerHTML = `<svg viewBox="0 0 ${width} ${height}" width="100%" height="320">
        <rect width="${width}" height="${height}" fill="#111318"/>
        <path d="${area}" fill="${fill}"/>
        <path d="${path}" fill="none" stroke="${stroke}" stroke-width="2.5"/>
        <text x="${pad}" y="20" fill="#a8adb7" font-size="12">${money(max)}</text>
        <text x="${pad}" y="${height - 6}" fill="#a8adb7" font-size="12">${money(min)}</text>
      </svg>`;
    }

    async function maybeRefreshTrades() {
      const now = Date.now();
      if (now - _lastTradeRefresh < 10000) return;
      _lastTradeRefresh = now;
      try {
        const data = await fetchJson('/api/paper-trades');
        const rows = (data.trades || []).reverse();
        const tbody = document.getElementById('pmTradeRows');
        if (!rows.length) {
          tbody.innerHTML = '<tr><td colspan="9">No closed trades yet</td></tr>';
          return;
        }
        tbody.innerHTML = rows.map(t => {
          const pnl = num(t.net_pnl);
          return `<tr>
            <td>${escapeHtml(t.pair || '')}</td>
            <td style="color:${t.direction==='long'?'var(--green)':'var(--red)'}">${escapeHtml(t.direction || '')}</td>
            <td>${compact(t.entry_price)}</td>
            <td>${compact(t.exit_price)}</td>
            <td>${compact(t.stop_loss || t.atr_stop_loss)}</td>
            <td>${compact(t.take_profit || t.atr_take_profit)}</td>
            <td>${compact(t.position_size)}</td>
            <td><span class="chip ${pnl>=0?'good':'bad'}">${money(pnl)}</span></td>
            <td>${escapeHtml(t.exit_reason || '')}</td>
          </tr>`;
        }).join('');
      } catch(e) {}
    }

    async function refreshPaperStatus() {
      try {
        const data = await fetchJson('/api/paper-status');
        const running = data.running;

        document.getElementById('pmChipStatus').textContent = running ? 'Live' : 'Stopped';
        document.getElementById('pmChipStatus').className = 'chip ' + (running ? 'good' : '');
        
        if (data.error) {
          document.getElementById('pmError').textContent = 'Error: ' + data.error;
        } else {
          document.getElementById('pmError').textContent = '';
        }

        const equity = num(data.equity);
        const startEq = num(data.starting_equity) || 1;
        const returnPct = ((equity - startEq) / startEq * 100);
        const pnl = num(data.realized_pnl);

        document.getElementById('pmEquity').textContent = money(equity) || '-';
        document.getElementById('pmReturn').textContent = running
          ? returnPct.toFixed(2) + '%' : '-';
        document.getElementById('pmReturn').style.color = returnPct >= 0 
          ? 'var(--green)' : 'var(--red)';
        document.getElementById('pmPnl').textContent = running ? money(pnl) : '-';
        document.getElementById('pmPnl').style.color = pnl >= 0 ? 'var(--green)' : 'var(--red)';
        document.getElementById('pmFees').textContent = running 
          ? money(num(data.fees_paid)) : '-';
        document.getElementById('pmCandles').textContent = running ? data.candle_count : '-';
        document.getElementById('pmOpenCount').textContent = data.open_positions || 0;
        document.getElementById('pmFillCount').textContent = (data.total_fills || 0) + ' fills';

        if (running) {
          document.getElementById('paperLoopMeta').textContent =
            `${data.pair} · ${data.interval} · ${data.strategy} · last update ${
              data.last_updated ? new Date(data.last_updated).toLocaleTimeString() : 'pending'
            }`;
          document.getElementById('paperTopChips').innerHTML =
            `<span class="chip warn">Paper</span>
             <span class="chip good">Futures</span>
             <span class="chip good">Live</span>
             <span class="chip bad">Live Orders Locked</span>`;
        } else {
          document.getElementById('paperLoopMeta').textContent = 'Not running';
        }

        // Equity history for chart
        if (running && equity > 0) {
          pmEquityHistory.push({ equity });
          if (pmEquityHistory.length > 500) pmEquityHistory.shift();
        }
        renderPaperEquityChart(pmEquityHistory);

        // Open positions table
        let positions = [];
        try { positions = JSON.parse(data.positions_json || '[]'); } catch(e) {}
        const posBody = document.getElementById('pmPositionsBody');
        if (!positions.length) {
          posBody.innerHTML = '<p style="color:var(--muted);font-size:13px;padding:8px 0">No open positions</p>';
        } else {
          posBody.innerHTML = `<table style="width:100%;font-size:12px;border-collapse:collapse">
            <tr style="color:var(--muted)">
              <th style="text-align:left;padding:4px 0">Pair</th>
              <th style="text-align:left;padding:4px 0">Dir</th>
              <th style="text-align:right;padding:4px 0">Qty</th>
              <th style="text-align:right;padding:4px 0">Entry</th>
              <th style="text-align:right;padding:4px 0">Unrealized</th>
            </tr>` + positions.map(p => {
              const upnl = num(p.unrealized_pnl);
              return `<tr>
                <td style="padding:4px 0">${escapeHtml(p.pair)}</td>
                <td style="padding:4px 0;color:${p.direction==='long'?'var(--green)':'var(--red)'}">${p.direction}</td>
                <td style="text-align:right;padding:4px 0">${compact(p.quantity)}</td>
                <td style="text-align:right;padding:4px 0">${compact(p.entry_price)}</td>
                <td style="text-align:right;padding:4px 0;color:${upnl>=0?'var(--green)':'var(--red)'}">${money(upnl)}</td>
              </tr>`;
            }).join('') + '</table>';
        }

        // Sync start/stop button states
        document.getElementById('startPaperBtn').disabled = running;
        document.getElementById('stopPaperBtn').disabled = !running;
        document.getElementById('paperRunChip').textContent = running ? 'Running' : 'Stopped';
        document.getElementById('paperRunChip').className = 'chip ' + (running ? 'good' : '');
        if (running) document.getElementById('paperRunMeta').textContent =
          `${data.pair} ${data.interval}`;

        await maybeRefreshTrades();

      } catch(e) {
        // silently ignore connection errors
      }
    }

    async function startPaper() {
      const pair = document.getElementById('p_custom_pair').value.trim() 
                   || document.getElementById('p_pair').value;
      const payload = {
        pair,
        interval: document.getElementById('p_interval').value,
        strategy: document.getElementById('p_strategy').value,
        starting_equity: document.getElementById('p_starting_equity').value,
        leverage: document.getElementById('p_leverage').value,
        risk_pct: document.getElementById('p_risk_pct').value,
        max_daily_loss_pct: document.getElementById('p_max_daily_loss_pct').value,
        trailing_stop_enabled: document.getElementById('p_trailing_stop').checked,
        atr_dynamic_exits_enabled: document.getElementById('p_atr_exits').checked,
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

    document.getElementById('p_intrabar').addEventListener('change', (e) => {
        document.getElementById('p_intrabar_settings').style.display = e.target.checked ? 'flex' : 'none';
    });

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

    document.getElementById('startPaperBtn').addEventListener('click', startPaper);
    document.getElementById('stopPaperBtn').addEventListener('click', stopPaper);

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
