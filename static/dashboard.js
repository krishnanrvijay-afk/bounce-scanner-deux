/* ── Bounce Scanner II — dashboard.js ──────────────────────────────────────── */
let STATE        = null;
let activeFilter = 'ALL';
let activeTab    = 'grid';   // 'grid' | 'alerts' | 'pos' | 'log'
let lastScanAt   = null;
let marketOpen   = false;

const ADX_FADE_MAX = 60;   // mirrors config — used for card pill display

// ── Fetch state every 2s ──────────────────────────────────────────────────────
async function fetchState() {
  try {
    const r = await fetch('/api/state');
    if (!r.ok) return;
    STATE = await r.json();
    render();
  } catch (e) { /* network blip */ }
}

setInterval(fetchState, 2000);
fetchState();

// Dismiss market popover on outside click
document.addEventListener('click', e => {
  if (marketOpen && !e.target.closest('.mkt-btn-wrap')) closeMarket();
});

// ── Navigation ────────────────────────────────────────────────────────────────
function setNav(el) {
  document.querySelectorAll('.fp').forEach(f => f.classList.remove('active'));
  el.classList.add('active');
  activeTab = el.dataset.tab;
  if (activeTab === 'grid' && el.dataset.filter) activeFilter = el.dataset.filter;

  document.getElementById('view-grid').style.display    = activeTab === 'grid'   ? '' : 'none';
  document.getElementById('tab-alerts').style.display   = activeTab === 'alerts' ? 'block' : 'none';
  document.getElementById('tab-positions').style.display= activeTab === 'pos'    ? 'block' : 'none';
  document.getElementById('tab-log').style.display      = activeTab === 'log'    ? 'block' : 'none';

  if (STATE) render();
}

// ── Market popover ────────────────────────────────────────────────────────────
function toggleMarket(e) {
  e.stopPropagation();
  marketOpen ? closeMarket() : openMarket();
}
function openMarket() {
  marketOpen = true;
  document.getElementById('mkt-btn').classList.add('open');
  document.getElementById('mkt-popover').classList.add('open');
}
function closeMarket() {
  marketOpen = false;
  document.getElementById('mkt-btn').classList.remove('open');
  document.getElementById('mkt-popover').classList.remove('open');
}

// ── Master render ─────────────────────────────────────────────────────────────
function render() {
  renderHeader();
  updateNavCounts();
  if (activeTab === 'grid')   renderCards();
  if (activeTab === 'alerts') renderAlertsTab();
  if (activeTab === 'pos')    renderPositionsTab();
  if (activeTab === 'log')    renderLogTab();
  if (marketOpen) updateMarketPopover();
}

// ── Nav counts ────────────────────────────────────────────────────────────────
function updateNavCounts() {
  const alerts = STATE?.alerts      || [];
  const trades = STATE?.open_trades || {};
  const log    = STATE?.trade_log   || [];
  document.getElementById('nav-alert-count').textContent = alerts.length;
  document.getElementById('nav-pos-count').textContent   = Object.keys(trades).length;
  document.getElementById('nav-log-count').textContent   = log.length;
}

// ── Header ────────────────────────────────────────────────────────────────────
function renderHeader() {
  const { daily, account, circuit_breaker, scan_count, last_scan_at } = STATE;

  const pnlEl = document.getElementById('h-pnl');
  pnlEl.textContent = `$${(daily?.pnl || 0).toFixed(2)}`;
  pnlEl.className   = 'hstat-value ' + ((daily?.pnl || 0) >= 0 ? 'green' : 'red');

  document.getElementById('h-margin').textContent    = `$${Math.round(account?.margin_deployed || 0).toLocaleString()}`;
  document.getElementById('h-positions').textContent = account?.slots_used || 0;
  document.getElementById('h-scans').textContent     = scan_count || 0;

  document.getElementById('paper-badge').style.display = account?.paper_mode ? 'block' : 'none';
  document.getElementById('cb-badge').style.display    = circuit_breaker?.active ? 'block' : 'none';

  if (last_scan_at && last_scan_at !== lastScanAt) {
    lastScanAt = last_scan_at;
    const d = new Date(last_scan_at * 1000);
    document.getElementById('scan-status').innerHTML =
      `last scan <span>${d.toLocaleTimeString()}</span> · #${scan_count}`;
  }
}

// ── Market popover content ────────────────────────────────────────────────────
function updateMarketPopover() {
  const pairs = STATE?.pair_states || [];
  const bulls = pairs.filter(p => p.trend === 'Strong Bull').map(p => p.symbol);
  const bears = pairs.filter(p => p.trend === 'Strong Bear').map(p => p.symbol);
  const ob    = pairs.filter(p => p.j15m >= 80).map(p => p.symbol);
  const os    = pairs.filter(p => p.j15m <= 20).map(p => p.symbol);

  const chips = (arr, color) => arr.length
    ? arr.map(s => `<span class="mkt-chip" style="color:${color}">${s}</span>`).join('')
    : `<span style="color:#333;font-size:9px;">none</span>`;

  document.getElementById('mkt-bull').innerHTML = chips(bulls, '#00ff88');
  document.getElementById('mkt-bear').innerHTML = chips(bears, '#ff4444');
  document.getElementById('mkt-ob').innerHTML   = chips(ob,    '#ff4444');
  document.getElementById('mkt-os').innerHTML   = chips(os,    '#00ff88');
}

// ── Pair cards (full width, 4-col, single direction) ─────────────────────────
function renderCards() {
  const grid   = document.getElementById('card-grid');
  const pairs  = STATE.pair_states || [];
  const alerts = STATE.alerts || [];
  const trades = STATE.open_trades || {};

  const filtered = pairs.filter(p => {
    if (activeFilter === 'ALL')          return true;
    if (activeFilter === 'ALERTS')       return alerts.some(a => a.symbol === p.symbol);
    if (activeFilter === 'BOUNCE_SHORT') return p.short_score === 4;
    if (activeFilter === 'BOUNCE_LONG')  return p.long_score  === 4;
    if (activeFilter === 'COOLDOWN')     return p.cooldown_short > 0 || p.cooldown_long > 0;
    return true;
  });

  grid.innerHTML = filtered.map(p => buildCard(p, alerts, trades)).join('')
    || '<div style="padding:40px;color:#333;text-align:center;grid-column:1/-1;">No pairs match filter</div>';
}

function buildCard(p, alerts, trades) {
  const sym    = p.symbol;
  const price  = p.price   || 0;
  const j15m   = p.j15m    || 0;
  const j1h    = p.j1h     || 0;
  const j5m    = p.j5m     || 0;
  const rsi15m = p.rsi15m  || 0;
  const bidPct = p.bid_pct || 0;
  const askPct = p.ask_pct || 0;
  const adx1h  = p.adx1h   || 0;
  const cdS    = p.cooldown_short || 0;
  const cdL    = p.cooldown_long  || 0;
  const inTrade = p.in_trade;

  // Gate pass count per direction
  const shortGates = [j15m > 80, j1h > 60, rsi15m > 65, askPct >= 55];
  const longGates  = [j15m < 20, j1h < 40, rsi15m < 35, bidPct >= 55];
  const shortCount = shortGates.filter(Boolean).length;
  const longCount  = longGates.filter(Boolean).length;

  const shortFull = shortCount === 4;
  const longFull  = longCount  === 4;
  const diverge   = shortCount === longCount && !shortFull;
  const showShort = shortCount >= longCount || diverge;
  const showLong  = longCount  >= shortCount || diverge;
  const leadCount = Math.max(shortCount, longCount);
  const nearTrig  = !shortFull && !longFull && leadCount === 3;

  const hasAlert  = alerts.some(a => a.symbol === sym);
  const adxFade   = adx1h > ADX_FADE_MAX;

  // Direction rows
  let rows = '';
  if (showShort) {
    rows += dirRow('SHORT', j15m, j1h, rsi15m, askPct, adx1h, shortGates);
  }
  if (showLong) {
    rows += dirRow('LONG', j15m, j1h, rsi15m, bidPct, adx1h, longGates);
  }

  // Footer pills
  let pills = '';
  if (inTrade)   pills += `<span class="pill pill-intrade">IN TRADE</span>`;
  if (cdS > 0)   pills += `<span class="pill pill-cd">CD-S ${fmtCd(cdS)}</span>`;
  if (cdL > 0)   pills += `<span class="pill pill-cd">CD-L ${fmtCd(cdL)}</span>`;
  if (diverge)   pills += `<span class="pill pill-diverge">DIVERGENCE</span>`;
  if (nearTrig)  pills += `<span class="pill pill-near">NEAR TRIGGER</span>`;
  if (adxFade)   pills += `<span class="pill pill-adxmax">ADX ${adx1h.toFixed(0)} FADE MAX</span>`;
  if (shortFull && hasAlert) pills += `<span class="pill pill-alert-s">▼ ALERT</span>`;
  if (longFull  && hasAlert) pills += `<span class="pill pill-alert">▲ ALERT</span>`;

  return `<div class="pair-card">
    <div class="card-top">
      <div class="card-sym">${sym}</div>
      <div class="card-price-block">
        <div class="card-price">${fmtPrice(price)}</div>
        <div class="card-adx ${adxFade ? 'red' : adx1h >= 25 ? 'white' : 'grey'}">ADX ${adx1h.toFixed(1)}</div>
      </div>
    </div>
    ${rows}
    <div class="card-footer">${pills || `<span class="pill pill-scanning">SCANNING</span>`}</div>
  </div>`;
}

function dirRow(direction, j15m, j1h, rsi15m, depthPct, adx, gates) {
  const isLong = direction === 'LONG';
  const rowCls = isLong ? 'long-row' : 'short-row';
  const arrow  = isLong ? '▲' : '▼';
  const arCls  = isLong ? 'arrow-long' : 'arrow-short';
  const depthLabel = isLong ? 'BID%' : 'ASK%';

  const j15mColor  = isLong ? (j15m  < 20 ? 'green' : 'grey') : (j15m  > 80 ? 'red' : 'grey');
  const j1hColor   = isLong ? (j1h   < 40 ? 'green' : 'grey') : (j1h   > 60 ? 'red' : 'grey');
  const rsiColor   = isLong ? (rsi15m < 35 ? 'green' : 'grey') : (rsi15m > 65 ? 'red' : 'grey');
  const depthColor = depthPct >= 55 ? (isLong ? 'green' : 'red') : 'grey';
  const adxColor   = adx > ADX_FADE_MAX ? 'red' : adx >= 25 ? 'white' : 'grey';

  const [g1, g2, g3, g4] = gates;

  return `<div class="dir-row ${rowCls}">
    <span class="dir-arrow ${arCls}">${arrow}</span>
    <div class="dir-vals">
      <div class="dv-item">
        <span class="dv-label">J15M</span>
        <span class="dv-val ${j15mColor}"><span class="gate-dot ${g1?'pass':'fail'}"></span>${j15m.toFixed(0)}</span>
      </div>
      <div class="dv-item">
        <span class="dv-label">J1H</span>
        <span class="dv-val ${j1hColor}"><span class="gate-dot ${g2?'pass':'fail'}"></span>${j1h.toFixed(0)}</span>
      </div>
      <div class="dv-item">
        <span class="dv-label">RSI15</span>
        <span class="dv-val ${rsiColor}"><span class="gate-dot ${g3?'pass':'fail'}"></span>${rsi15m.toFixed(0)}</span>
      </div>
      <div class="dv-item">
        <span class="dv-label">${depthLabel}</span>
        <span class="dv-val ${depthColor}"><span class="gate-dot ${g4?'pass':'fail'}"></span>${depthPct.toFixed(0)}%</span>
      </div>
    </div>
  </div>`;
}

// ── Alerts tab ────────────────────────────────────────────────────────────────
function renderAlertsTab() {
  const alerts = STATE.alerts || [];
  const trades = STATE.open_trades || {};
  const count  = alerts.length;
  document.getElementById('alert-count').textContent = count;
  updateNavCounts();

  if (!count) {
    document.getElementById('alert-grid').innerHTML = '<div class="no-content">No alerts yet</div>';
    return;
  }
  document.getElementById('alert-grid').innerHTML = alerts.map(a => buildAlertCard(a, trades)).join('');
}

function buildAlertCard(a, trades) {
  const isShort  = a.direction === 'SHORT';
  const dirClass = isShort ? 'short-card' : 'long-card';
  const dirPill  = isShort
    ? '<span class="ac-dir dir-short">BOUNCE SHORT</span>'
    : '<span class="ac-dir dir-long">BOUNCE LONG</span>';
  const tierCls = a.tier === 'HIGH_PROB' ? 'tp-high' : a.tier === 'STRONG' ? 'tp-strong' : 'tp-regular';
  const key     = `${a.symbol}${a.direction}`;
  const inTrade = a.is_in_trade || (key in trades);
  const stamp   = inTrade ? '<div class="in-trade-stamp">IN TRADE</div>' : '';
  const dis     = inTrade ? 'disabled' : '';
  const elapsed = a.fired_at ? Math.floor(Date.now()/1000 - a.fired_at) : 0;
  const elpStr  = elapsed < 60 ? `${elapsed}s ago` : `${Math.floor(elapsed/60)}m ago`;

  return `<div class="alert-card ${dirClass}">
    ${stamp}
    <div class="ac-top">
      <div class="ac-sym">${a.symbol}</div>
      <div style="display:flex;gap:4px;align-items:center;">
        ${dirPill}
        <span class="tier-pill ${tierCls}">${a.tier} ${a.leverage}x</span>
      </div>
    </div>
    <div class="ac-prices">
      <div class="ac-px"><div class="ac-px-label">ENTRY</div><div class="ac-px-val white">${fmtPrice(a.entry_price)}</div></div>
      <div class="ac-px"><div class="ac-px-label">SL (ATR)</div><div class="ac-px-val red">${fmtPrice(a.sl_price)}</div></div>
      <div class="ac-px"><div class="ac-px-label">TP1</div><div class="ac-px-val green">${fmtPrice(a.tp1_price)}</div></div>
    </div>
    <div class="ac-meta">
      <div><span class="ac-meta-label">TP2 </span><span class="ac-meta-val">${fmtPrice(a.tp2_price)}</span></div>
      <div><span class="ac-meta-label">J15M </span><span class="ac-meta-val">${(a.j15m||0).toFixed(1)}</span></div>
      <div><span class="ac-meta-label">RSI </span><span class="ac-meta-val">${(a.rsi15m||0).toFixed(1)}</span></div>
      <div><span class="ac-meta-label">ADX </span><span class="ac-meta-val amber">${(a.adx1h||0).toFixed(1)}</span></div>
      <div><span class="ac-meta-label">ATR </span><span class="ac-meta-val amber">${(a.atr15m||0).toFixed(4)}</span></div>
      <div><span style="color:#555;font-size:9px;">${elpStr}</span></div>
    </div>
    <div class="ac-btns">
      <button class="ac-btn btn-hl"   ${dis} onclick="openTrade('${a.symbol}','${a.direction}','HL',${a.leverage})">OPEN HL</button>
      <button class="ac-btn btn-mexc" ${dis} onclick="openTrade('${a.symbol}','${a.direction}','MEXC',${a.leverage})">OPEN MEXC</button>
    </div>
  </div>`;
}

// ── Positions tab ─────────────────────────────────────────────────────────────
function renderPositionsTab() {
  const trades = STATE.open_trades || {};
  const prices = STATE.prices      || {};
  const keys   = Object.keys(trades);

  if (!keys.length) {
    document.getElementById('pos-grid').innerHTML = '<div class="no-content">No open positions</div>';
    return;
  }
  document.getElementById('pos-grid').innerHTML = keys.map(k => buildPosCard(trades[k], prices)).join('');
}

function buildPosCard(t, prices) {
  const isLong   = t.direction === 'LONG';
  const cls      = isLong ? 'long-card' : 'short-card';
  const dirBadge = isLong ? 'pb-long' : 'pb-short';
  const tierCls  = t.tier === 'HIGH_PROB' ? 'tp-high' : t.tier === 'STRONG' ? 'tp-strong' : 'tp-regular';
  const current  = t.current_price || prices[t.symbol] || t.entry_price;
  const sl       = t.sl_price   || 0;
  const tp1      = t.tp1_price  || 0;
  const entry    = t.entry_price || 0;
  const pnl      = t.unrealized_pnl || 0;
  const r        = t.r || 0;
  const pnlCls   = pnl >= 0 ? 'pos' : 'neg';
  const liveColor= isLong ? (current >= entry ? '#00ff88' : '#ff4444')
                          : (current <= entry ? '#00ff88' : '#ff4444');

  let pct = 0;
  if (isLong  && tp1 > sl) pct = Math.min(100, Math.max(0, (current - sl) / (tp1 - sl) * 100));
  if (!isLong && sl > tp1) pct = Math.min(100, Math.max(0, (sl - current) / (sl - tp1) * 100));

  const elapsed    = t.elapsed_s || 0;
  const elapsedStr = elapsed < 3600
    ? `${Math.floor(elapsed/60)}m ${elapsed%60}s`
    : `${Math.floor(elapsed/3600)}h ${Math.floor((elapsed%3600)/60)}m`;

  return `<div class="pos-card ${cls}">
    <div class="pos-card-hdr">
      <span class="pos-sym">${t.symbol}</span>
      <div class="pos-badges">
        <span class="pos-badge ${dirBadge}">${t.direction}</span>
        <span class="tier-pill ${tierCls}">${t.tier||'REGULAR'}</span>
        <span class="pos-badge pb-exch">${t.leverage||5}x · ${t.exchange||'HL'}</span>
        ${t.paper ? '<span class="pos-badge pb-paper">PAPER</span>' : ''}
      </div>
    </div>
    <div class="pos-bar-wrap">
      <div class="pos-bar-labels">
        <span class="pos-bar-label sl">SL ${fmtPrice(sl)}</span>
        <span class="pos-bar-label tp">TP1 ${fmtPrice(tp1)}</span>
      </div>
      <div class="pos-bar-track">
        <div class="pos-bar-fill ${isLong?'fill-long':'fill-short'}" style="width:${pct.toFixed(1)}%"></div>
      </div>
      <div class="pos-bar-prices">
        <div class="pos-bar-entry" style="color:#555;">entry ${fmtPrice(entry)}</div>
        <div class="pos-bar-live" style="color:${liveColor};">${fmtPrice(current)}</div>
      </div>
    </div>
    <div class="pos-pnl-row">
      <span class="pos-pnl ${pnlCls}">${pnl>=0?'+':''}$${pnl.toFixed(2)}</span>
      <span class="pos-r" style="color:${r>=0?'#555':'#ff4444'}">${r>=0?'+':''}${r.toFixed(2)}R</span>
    </div>
    <div class="pos-meta-row">
      <div class="pos-meta-item"><span class="pos-meta-label">TP2 </span><span class="pos-meta-val">${fmtPrice(t.tp2_price)}</span></div>
      <div class="pos-meta-item"><span class="pos-meta-label">AGE </span><span class="pos-meta-val">${elapsedStr}</span></div>
      <div class="pos-meta-item"><span class="pos-meta-label">ADX </span><span class="pos-meta-val">${(t.adx1h||0).toFixed(1)}</span></div>
    </div>
    <button class="pos-close-btn" onclick="closeTrade('${t.symbol}','${t.direction}')">FORCE CLOSE</button>
  </div>`;
}

// ── Log tab ───────────────────────────────────────────────────────────────────
function renderLogTab() {
  const log = STATE.trade_log || [];
  document.getElementById('log-count').textContent = `${log.length} trade${log.length!==1?'s':''}`;

  if (!log.length) {
    document.getElementById('log-body').className = 'log-empty';
    document.getElementById('log-body').innerHTML = 'No closed trades yet';
    return;
  }

  const rows = [...log].reverse().map(r => {
    const reasonCls = r.exit_reason === 'TP1'  ? 'reason-tp1'
                    : r.exit_reason === 'TP2'  ? 'reason-tp2'
                    : r.exit_reason === 'SL'   ? 'reason-sl' : 'reason-manual';
    const pnlColor = (r.pnl_usd||0) >= 0 ? '#00ff88' : '#ff4444';
    const rColor   = (r.r_value||0) >= 0 ? '#555'    : '#ff4444';
    const dur      = r.duration_seconds || 0;
    const durStr   = dur < 3600 ? `${Math.floor(dur/60)}m` : `${Math.floor(dur/3600)}h${Math.floor((dur%3600)/60)}m`;
    const openTime = r.timestamp_opened ? new Date(r.timestamp_opened*1000).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'}) : '—';
    const closeTime= r.timestamp_closed ? new Date(r.timestamp_closed*1000).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'}) : '—';
    const isLong   = r.direction === 'LONG';
    return `<tr>
      <td style="font-weight:700;font-size:12px;">${r.symbol}</td>
      <td style="color:${isLong?'#00ff88':'#ff4444'};font-weight:700;">${r.direction}</td>
      <td style="color:#888;">${r.tier||'—'}</td>
      <td style="color:#aaa;">${r.leverage||'—'}x</td>
      <td>${fmtPrice(r.entry_price)}</td>
      <td>${fmtPrice(r.exit_price)}</td>
      <td style="color:#ff4444;">${fmtPrice(r.sl_price)}</td>
      <td style="color:#00ff88;">${fmtPrice(r.tp1_price)}</td>
      <td class="${reasonCls}">${r.exit_reason||'—'}</td>
      <td style="color:${pnlColor};font-weight:700;">${(r.pnl_usd||0)>=0?'+':''}$${(r.pnl_usd||0).toFixed(2)}</td>
      <td style="color:${rColor};font-weight:700;">${(r.r_value||0)>=0?'+':''}${(r.r_value||0).toFixed(2)}R</td>
      <td style="color:#555;">${openTime}</td>
      <td style="color:#555;">${closeTime}</td>
      <td style="color:#555;">${durStr}</td>
    </tr>`;
  }).join('');

  document.getElementById('log-body').className = '';
  document.getElementById('log-body').innerHTML = `
    <table class="log-table">
      <thead><tr>
        <th>PAIR</th><th>DIR</th><th>TIER</th><th>LEV</th>
        <th>ENTRY</th><th>EXIT</th><th>SL</th><th>TP1</th>
        <th>REASON</th><th>P&L</th><th>R</th>
        <th>OPEN</th><th>CLOSE</th><th>DUR</th>
      </tr></thead>
      <tbody>${rows}</tbody>
    </table>`;
}

// ── Trade actions ─────────────────────────────────────────────────────────────
async function openTrade(symbol, direction, exchange, leverage) {
  try {
    const r = await fetch('/api/trade/open', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ symbol, direction, exchange, leverage }),
    });
    const d = await r.json();
    if (!r.ok) { alert(`Open failed: ${d.detail || d.msg}`); return; }
    fetchState();
  } catch (e) { alert('Request failed'); }
}

async function closeTrade(symbol, direction) {
  if (!confirm(`Force close ${symbol} ${direction}?`)) return;
  try {
    const r = await fetch('/api/trade/close', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ symbol, direction }),
    });
    const d = await r.json();
    if (!r.ok) { alert(`Close failed: ${d.detail || d.msg}`); return; }
    fetchState();
  } catch (e) { alert('Request failed'); }
}

async function clearAlerts() {
  try {
    const r = await fetch('/api/alerts', { method: 'DELETE' });
    if (!r.ok) { alert('Clear failed'); return; }
    fetchState();
  } catch (e) { alert('Request failed'); }
}

async function exportCsv() {
  window.location.href = '/api/tradelog/csv';
}

async function clearLog() {
  const trades  = STATE?.open_trades || {};
  const hasOpen = Object.keys(trades).length > 0;
  const msg = hasOpen
    ? `${Object.keys(trades).length} open position(s) will be force-closed first. Clear everything?`
    : 'Clear all trade log entries?';
  if (!confirm(msg)) return;
  try {
    const r = await fetch('/api/tradelog', { method: 'DELETE' });
    if (!r.ok) { alert('Clear failed'); return; }
    fetchState();
  } catch (e) { alert('Request failed'); }
}

// ── Helpers ───────────────────────────────────────────────────────────────────
function fmtPrice(p) {
  if (!p) return '—';
  if (p >= 1000) return p.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  if (p >= 1)    return p.toFixed(4);
  return p.toFixed(6);
}

function fmtCd(seconds) {
  if (seconds < 60) return `${seconds}s`;
  return `${Math.floor(seconds/60)}m`;
}
