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
      margin: -8px 0 16px;
      position: sticky;
      top: 12px;
      z-index: 3;
      box-shadow: 0 10px 24px rgba(13, 15, 18, 0.35);
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
          <h1>CoinDCX INR-M Futures</h1>
          <p class="subtle">Paper control dashboard v10.1</p>
        </div>
      </div>

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
        </div>

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

        <label>Fee Rates Incl. GST
          <input id="fee_rate_preview" type="text" value="M 0.000236 / T 0.00059" readonly />
        </label>

        <div class="split">
          <label>Trail Active %
            <input id="trailing_stop_activation_pct" type="number" min="0" step="0.1" value="1" />
          </label>
          <label>Trail Distance %
            <input id="trailing_stop_distance_pct" type="number" min="0.1" step="0.1" value="2" />
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
      </div>

      <div class="statusRows" id="statusRows"></div>
      <div id="message" class="message"></div>
    </aside>

    <main>
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
          <div id="exitDiagnosticsMessage" class="message"></div>
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
                <th>Net PnL</th>
                <th>ATR Profile</th>
                <th>MFE %</th>
                <th>MAE %</th>
                <th>R</th>
                <th>Reason</th>
              </tr>
            </thead>
            <tbody id="tradeRows">
              <tr><td colspan="15">No trades loaded</td></tr>
            </tbody>
          </table>
        </div>
      </section>
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
      sHold: document.getElementById('sHold')
    };

    const num = value => {
      const parsed = Number(value);
      return Number.isFinite(parsed) ? parsed : 0;
    };

    const money = value => num(value).toLocaleString(undefined, {
      maximumFractionDigits: 2
    });

    const pct = value => `${num(value).toFixed(2)}%`;
    const compact = value => num(value).toLocaleString(undefined, { maximumFractionDigits: 4 });
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

    async function loadStatus() {
      const status = await fetchJson('/api/status');
      const defaults = status.defaults || {};
      const risk = status.risk || {};
      Object.entries({
        pair: defaults.pair,
        custom_pair: '',
        interval: defaults.interval,
        strategy: defaults.strategy,
        lookback: defaults.lookback,
        equity: defaults.equity,
        leverage: defaults.leverage,
        risk_per_trade_pct: defaults.risk_per_trade_pct ?? risk.max_risk_per_trade_pct,
        stop_loss_pct: defaults.stop_loss_pct ?? '',
        take_profit_pct: defaults.take_profit_pct ?? '',
        maker_fee_pct: rateToPct(defaults.maker_fee_rate),
        taker_fee_pct: rateToPct(defaults.taker_fee_rate),
        fee_gst_pct: rateToPct(defaults.fee_gst_rate),
        slippage_pct: defaults.slippage_pct,
        stop_slippage_pct: defaults.stop_slippage_pct,
        funding_fee_pct: rateToPct(defaults.funding_fee_rate),
        funding_interval_hours: defaults.funding_interval_hours,
        trailing_stop_activation_pct: risk.trailing_stop_activation_pct,
        trailing_stop_distance_pct: risk.trailing_stop_distance_pct
      }).forEach(([key, value]) => {
        if (value !== undefined && ids[key]) ids[key].value = value;
      });
      refreshFeeRatePreview();
      ids.trailing_stop_enabled.checked = Boolean(risk.trailing_stop_enabled);
      ids.atr_dynamic_exits_enabled.checked = Boolean(defaults.atr_dynamic_exits_enabled);
      ids.atr_mode.value = defaults.atr_policy_mode === 'router'
        ? 'router'
        : (defaults.atr_take_profit_enabled ? 'stop_tp' : 'stop_only');

      ids.statusRows.innerHTML = '';
      [
        ['Mode', status.bot.mode],
        ['Margin', `${status.bot.futures_margin_currency}-M`],
        ['Live Orders', status.safety.live_orders_locked ? 'Locked' : 'Unlocked'],
        ['Risk', `${status.risk.max_risk_per_trade_pct}% / trade`],
        ['Risk Equity', defaults.compound_risk_equity ? 'Compounding' : 'Initial'],
        ['Max Loss', `${status.risk.max_daily_loss_pct}% / day`],
        ['Max Leverage', status.risk.max_leverage],
        ['Max Positions', status.risk.max_open_positions],
        ['Fee GST', `${rateToPct(defaults.fee_gst_rate)}%`],
        ['Stop Slip', `${defaults.stop_slippage_pct ?? defaults.slippage_pct}%`],
        ['Funding', `${rateToPct(defaults.funding_fee_rate)}% / ${defaults.funding_interval_hours}h`],
        ['Trailing Stop', status.risk.trailing_stop_enabled ? 'On' : 'Off'],
        ['Dynamic ATR', defaults.atr_dynamic_exits_enabled ? 'On' : 'Off'],
        ['ATR Mode', ids.atr_mode.options[ids.atr_mode.selectedIndex].text],
        ['Re-entry', defaults.intrabar_reentry_enabled ? 'Auto' : 'Off'],
        ['Loss Guard', `${defaults.stop_loss_cooldown_candles} stop / ${defaults.max_consecutive_losses} losses`]
      ].forEach(([label, value]) => {
        const row = document.createElement('div');
        row.className = 'statusRow';
        row.innerHTML = `<span>${escapeHtml(label)}</span><span class="chip">${escapeHtml(value)}</span>`;
        ids.statusRows.appendChild(row);
      });
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

      renderStrategyProfile(payload.strategy_profile || {});
      renderRunQuality(payload.run_quality || {});
      renderBars(metrics, account);
      renderDiagnostics(payload.diagnostics || {});
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
      const topExit = topReason(diagnostics.exit_reason_counts);
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
