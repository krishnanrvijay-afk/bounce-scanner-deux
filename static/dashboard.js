/* ── Bounce Scanner II — dashboard.js ──────────────────────────────────────── */
let STATE        = null;
let activeFilter = 'ALL';
let activeTab    = 'grid';
let lastScanAt   = null;
let marketOpen   = false;
let posTimers    = {};

const ADX_FADE_MAX = 60;

// ── Fetch + countdown state ───────────────────────────────────────────────────
let _scanCdSec   = 0;   // counts down to next scan
let _priceCdSec  = 0;   // counts down to next price update

// Tick every second — scan countdown, per-card price countdown
setInterval(() => {
  _scanCdSec  = Math.max(0, _scanCdSec  - 1);
  _priceCdSec = Math.max(0, _priceCdSec - 1);
  updateScanStatus();
  // Update all per-card price countdown spans in-place (no re-render)
  document.querySelectorAll('.price-cd-val').forEach(el => {
    el.textContent = `${_priceCdSec}s`;
  });
}, 1000);

// Fetch state every 2s
async function fetchState() {
  try {
    const r = await fetch('/api/state');
    if (!r.ok) return;
    STATE = await r.json();

    // Reset price countdown whenever we get fresh prices
    _priceCdSec = PRICE_INTERVAL;

    // Reset scan countdown when scan_at changes
    if (STATE.last_scan_at && STATE.last_scan_at !== lastScanAt) {
      lastScanAt  = STATE.last_scan_at;
      _scanCdSec  = SCAN_INTERVAL;
    }

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

  document.getElementById('view-grid').style.display     = activeTab === 'grid'   ? '' : 'none';
  document.getElementById('tab-alerts').style.display    = activeTab === 'alerts' ? 'block' : 'none';
  document.getElementById('tab-positions').style.display = activeTab === 'pos'    ? 'block' : 'none';
  document.getElementById('tab-log').style.display       = activeTab === 'log'    ? 'block' : 'none';

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

// ── Scan status text (updated by ticker and by render) ────────────────────────
function updateScanStatus() {
  const el = document.getElementById('scan-status');
  if (!el) return;
  if (!lastScanAt) { el.innerHTML = 'waiting for scan…'; return; }
  const d = new Date(lastScanAt * 1000);
  const ts = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
  el.innerHTML = `last scan <span class="ts">${ts}</span> · #${STATE?.scan_count||0} · <span class="cd">next in ${_scanCdSec}s</span>`;
}

// ── Master render ─────────────────────────────────────────────────────────────
function render() {
  renderHeader();
  updateNavCounts();
  updateScanStatus();
  if (activeTab === 'grid')   renderCards();
  if (activeTab === 'alerts') renderAlertsTab();
  if (activeTab === 'pos')    renderPositionsTab();
  if (activeTab === 'log')    renderLogTab();
  if (marketOpen)             updateMarketPopover();
  renderCockpit();
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
  const { daily, account, circuit_breaker, scan_count } = STATE;

  const pnlEl = document.getElementById('h-pnl');
  pnlEl.textContent = `$${(daily?.pnl || 0).toFixed(2)}`;
  pnlEl.className   = 'hstat-value ' + ((daily?.pnl || 0) >= 0 ? 'green' : 'red');

  document.getElementById('h-margin').textContent    = `$${Math.round(account?.margin_deployed || 0).toLocaleString()}`;
  document.getElementById('h-positions').textContent = account?.slots_used || 0;
  document.getElementById('h-scans').textContent     = scan_count || 0;

  document.getElementById('paper-badge').style.display = account?.paper_mode ? 'block' : 'none';
  document.getElementById('cb-badge').style.display    = circuit_breaker?.active ? 'block' : 'none';
}

// ── Market popover ────────────────────────────────────────────────────────────
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

// ── Pair cards ────────────────────────────────────────────────────────────────
function renderCards() {
  const grid    = document.getElementById('card-grid');
  const pairs   = STATE.pair_states || [];
  const alerts  = STATE.alerts || [];
  const trades  = STATE.open_trades || {};
  const changes = STATE.price_changes || {};

  const filtered = pairs.filter(p => {
    if (activeFilter === 'ALL')          return true;
    if (activeFilter === 'ALERTS')       return alerts.some(a => a.symbol === p.symbol);
    if (activeFilter === 'BOUNCE_SHORT') return p.short_score === 4;
    if (activeFilter === 'BOUNCE_LONG')  return p.long_score  === 4;
    if (activeFilter === 'COOLDOWN')     return p.cooldown_short > 0 || p.cooldown_long > 0;
    return true;
  });

  grid.innerHTML = filtered.map(p => buildCard(p, alerts, trades, changes)).join('')
    || '<div style="padding:40px;color:#333;text-align:center;grid-column:1/-1;">No pairs match filter</div>';
}

function buildCard(p, alerts, trades, changes) {
  const sym    = p.symbol;
  const price  = p.price   || 0;
  const j15m   = p.j15m    || 0;
  const j1h    = p.j1h     || 0;
  const rsi15m = p.rsi15m  || 0;
  const bidPct = p.bid_pct || 0;
  const askPct = p.ask_pct || 0;
  const adx1h  = p.adx1h   || 0;
  const cdS    = p.cooldown_short || 0;
  const cdL    = p.cooldown_long  || 0;
  const inTrade = p.in_trade;
  const chg    = changes[sym] ?? null;

  // Price change display
  let chgHtml = '';
  if (chg !== null) {
    const chgColor = chg >= 0 ? '#00ff88' : '#ff4444';
    chgHtml = `<span class="card-chg" style="color:${chgColor}">${chg >= 0 ? '+' : ''}${chg.toFixed(2)}%</span>`;
  }

  // ADX value color: >= 50 green, 25-49 amber, < 25 white
  const adxFade  = adx1h > ADX_FADE_MAX;
  const adxColor = adxFade   ? '#ff4444'
                 : adx1h >= 50 ? '#00ff88'
                 : adx1h >= 25 ? '#ffaa00'
                 : '#ffffff';

  // Gate counts per direction
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

  let rows = '';
  if (showShort) rows += dirRow('SHORT', j15m, j1h, rsi15m, askPct, shortGates);
  if (showLong)  rows += dirRow('LONG',  j15m, j1h, rsi15m, bidPct, longGates);

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
      <div class="card-right">
        <div class="card-price-line">
          <span class="card-price">${fmtPrice(price)}</span>${chgHtml}<span class="card-price-cd price-cd-val">${_priceCdSec}s</span>
        </div>
        <div class="card-adx-block">
          <span class="card-adx-label">ADX</span>
          <span class="card-adx-val" style="color:${adxColor}">${adx1h.toFixed(1)}</span>
        </div>
      </div>
    </div>
    ${rows}
    <div class="card-footer">${pills || `<span class="pill pill-scanning">SCANNING</span>`}</div>
  </div>`;
}

function dirRow(direction, j15m, j1h, rsi15m, depthPct, gates) {
  const isLong     = direction === 'LONG';
  const rowCls     = isLong ? 'long-row' : 'short-row';
  const arrow      = isLong ? '▲' : '▼';
  const arCls      = isLong ? 'arrow-long' : 'arrow-short';
  const depthLabel = isLong ? 'BID%' : 'ASK%';
  const dotPfx     = isLong ? 'long' : 'short';

  // Dot cluster: 4 dots, green for LONG, red for SHORT
  const dotCluster = `<div class="gate-cluster">${gates.map(g =>
    `<span class="gc-dot ${dotPfx}-${g ? 'pass' : 'fail'}"></span>`
  ).join('')}</div>`;

  // Value colors
  const j15mColor  = isLong ? (j15m  < 20 ? 'green' : 'grey') : (j15m  > 80 ? 'red' : 'grey');
  const j1hColor   = isLong ? (j1h   < 40 ? 'green' : 'grey') : (j1h   > 60 ? 'red' : 'grey');
  const rsiColor   = isLong ? (rsi15m < 35 ? 'green' : 'grey') : (rsi15m > 65 ? 'red' : 'grey');
  const depthColor = depthPct >= 55 ? (isLong ? 'green' : 'red') : 'grey';

  return `<div class="dir-row ${rowCls}">
    <span class="dir-arrow ${arCls}">${arrow}</span>
    ${dotCluster}
    <div class="dir-vals">
      <div class="dv-item">
        <span class="dv-label">J15M</span>
        <span class="dv-val ${j15mColor}">${j15m.toFixed(0)}</span>
      </div>
      <div class="dv-item">
        <span class="dv-label">J1H</span>
        <span class="dv-val ${j1hColor}">${j1h.toFixed(0)}</span>
      </div>
      <div class="dv-item">
        <span class="dv-label">RSI15</span>
        <span class="dv-val ${rsiColor}">${rsi15m.toFixed(0)}</span>
      </div>
      <div class="dv-item">
        <span class="dv-label">${depthLabel}</span>
        <span class="dv-val ${depthColor}">${depthPct.toFixed(0)}%</span>
      </div>
    </div>
  </div>`;
}

// ── Cockpit bar ───────────────────────────────────────────────────────────────
function renderCockpit() {
  const pairs   = STATE?.pair_states || [];
  const changes = STATE?.price_changes || {};

  // Section 1: pair-name labels below the bar, stacked to avoid overlap
  const labelRow = document.getElementById('ck-label-row');
  const sorted = [...pairs]
    .map(p => ({ sym: p.symbol, j: Math.min(99, Math.max(1, p.j15m || 50)) }))
    .sort((a, b) => a.j - b.j);

  // Anti-overlap: track rightmost extent per row (label ~5% wide at this font size)
  const rowEdge = [];   // rowEdge[rowIndex] = last placed right-edge percent
  const placed  = sorted.map(({ sym, j }) => {
    let row = 0;
    while (rowEdge[row] !== undefined && rowEdge[row] > j - 3) row++;
    rowEdge[row] = j + 5;
    return { sym, j, row };
  });

  const rowH   = 14; // px — matches 10px label font-size
  const maxRow = placed.reduce((m, p) => Math.max(m, p.row), 0);
  labelRow.style.height = `${(maxRow + 1) * rowH}px`;
  labelRow.innerHTML = placed.map(({ sym, j, row }) => {
    const col = j < 20 ? '#00ff88'
              : j < 35 ? '#4d8a4d'
              : j < 65 ? '#cccccc'
              : j < 80 ? '#8a4d4d'
              : '#ff4444';
    return `<div class="ck-pair-label" style="left:${j}%;top:${row * rowH}px;color:${col};">${sym}</div>`;
  }).join('');

  // Section 2: Oversold (J15M <= 35)
  const osEl = document.getElementById('ck-os');
  const osPairs = pairs.filter(p => p.j15m <= 35);
  osEl.innerHTML = osPairs.length
    ? osPairs.map(p => {
        const col  = p.j15m <= 20 ? '#00ff88' : '#006633';
        const bord = p.j15m <= 20 ? 'rgba(0,255,136,0.25)' : 'rgba(0,100,50,0.25)';
        return `<span class="ck-chip" style="color:${col};border-color:${bord}">${p.symbol}</span>`;
      }).join('')
    : `<span style="color:#333;font-size:9px;">none</span>`;

  // Section 3: Overbought (J15M >= 65)
  const obEl = document.getElementById('ck-ob');
  const obPairs = pairs.filter(p => p.j15m >= 65);
  obEl.innerHTML = obPairs.length
    ? obPairs.map(p => {
        const col  = p.j15m >= 80 ? '#ff4444' : '#662200';
        const bord = p.j15m >= 80 ? 'rgba(255,68,68,0.25)' : 'rgba(100,34,0,0.25)';
        return `<span class="ck-chip" style="color:${col};border-color:${bord}">${p.symbol}</span>`;
      }).join('')
    : `<span style="color:#333;font-size:9px;">none</span>`;

  // Section 4: Near trigger (exactly 3/4 gates on leading direction)
  const nearList = [];
  for (const p of pairs) {
    const sg = [p.j15m > 80, p.j1h > 60, p.rsi15m > 65, p.ask_pct >= 55].filter(Boolean).length;
    const lg = [p.j15m < 20, p.j1h < 40, p.rsi15m < 35, p.bid_pct >= 55].filter(Boolean).length;
    if (sg === 3 && sg > lg) nearList.push(`<span style="color:#ff4444">${p.symbol}</span> SHORT`);
    if (lg === 3 && lg > sg) nearList.push(`<span style="color:#00ff88">${p.symbol}</span> LONG`);
  }
  const nearEl = document.getElementById('ck-near');
  nearEl.innerHTML = nearList.length
    ? nearList.join('<span style="color:#333"> · </span>')
    : `<span style="color:#333">none</span>`;

}

// ── Alerts tab ────────────────────────────────────────────────────────────────
function dismissAlert(symbol, direction) {
  fetch('/api/alert/dismiss', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ symbol, direction }),
  }).then(() => fetchState()).catch(() => {});
}

function renderAlertsTab() {
  const alerts  = STATE.alerts || [];
  const trades  = STATE.open_trades || {};
  const pairMap = {};
  (STATE.pair_states || []).forEach(p => { pairMap[p.symbol] = p; });
  document.getElementById('alert-count').textContent = alerts.length;

  // Auto-dismiss alerts older than 15 minutes
  const nowSec = Date.now() / 1000;
  alerts.filter(a => a.fired_at && (nowSec - a.fired_at) > 900)
        .forEach(a => dismissAlert(a.symbol, a.direction));

  if (!alerts.length) {
    document.getElementById('alert-grid').innerHTML = '<div class="no-content">No alerts yet</div>';
    return;
  }
  document.getElementById('alert-grid').innerHTML = alerts.map(a => buildAlertCard(a, trades, pairMap)).join('');
}

function buildAlertCard(a, trades, pairMap) {
  const sym      = a.symbol;
  const isShort  = a.direction === 'SHORT';
  const dirClass = isShort ? 'short-card' : 'long-card';
  const key      = `${sym}${a.direction}`;
  const inTrade  = a.is_in_trade || (key in trades);
  const isPaper  = STATE.account?.paper_mode;

  // ── Snap data (frozen at alert fire) ──────────────────────────────────────
  const snapJ15m = +(a.j15m   || 0);
  const snapRsi  = +(a.rsi15m || 0);
  const snapAdx  = +(a.adx1h  || 0);
  const snapAtr  = +(a.atr15m || 0);

  // ── NOW data (live from pair_states) ──────────────────────────────────────
  const ps      = (pairMap || {})[sym] || {};
  const nowJ15m = ps.j15m   != null ? +ps.j15m   : snapJ15m;
  const nowRsi  = ps.rsi15m != null ? +ps.rsi15m : snapRsi;
  const nowAdx  = ps.adx1h  != null ? +ps.adx1h  : snapAdx;
  const nowAtr  = ps.atr15m != null ? +ps.atr15m : snapAtr;

  // ── Live price + 24h change ───────────────────────────────────────────────
  const livePrice     = (STATE.prices || {})[sym] || a.entry_price || 0;
  const chg24h        = ((STATE.price_changes || {})[sym]) ?? null;
  const priceDriftPct = a.entry_price ? Math.abs(livePrice - a.entry_price) / a.entry_price * 100 : 0;

  // ── Staleness ─────────────────────────────────────────────────────────────
  const elapsed    = a.fired_at ? Math.floor(Date.now() / 1000 - a.fired_at) : 0;
  const j15mDrift  = Math.abs(nowJ15m - snapJ15m);
  const isStale    = elapsed > 480 || j15mDrift > 30 || priceDriftPct > 1.5;
  const isAging    = !isStale && (elapsed > 180 || j15mDrift > 15 || priceDriftPct > 0.5);
  const staleness  = isStale ? 'STALE' : isAging ? 'AGING' : 'FRESH';
  const staleColor = staleness === 'STALE' ? '#ff4444' : staleness === 'AGING' ? '#ffaa00' : '#00ff88';
  const barPct     = Math.max(0, Math.min(100, 100 - (elapsed / 600 * 100)));
  const cdSec      = Math.max(0, 600 - elapsed);
  const cdStr      = cdSec >= 60
    ? `${Math.floor(cdSec/60)}m${String(cdSec % 60).padStart(2,'0')}s`
    : `${cdSec}s`;
  const elStr      = elapsed < 60
    ? `${elapsed}s`
    : `${Math.floor(elapsed/60)}m${String(elapsed % 60).padStart(2,'0')}s`;

  // ── Header badges ─────────────────────────────────────────────────────────
  const dirPill = isShort
    ? '<span class="ac-dir dir-short">BOUNCE SHORT</span>'
    : '<span class="ac-dir dir-long">BOUNCE LONG</span>';
  const tierCls = a.tier === 'HIGH_PROB' ? 'tp-high' : a.tier === 'STRONG' ? 'tp-strong' : 'tp-regular';
  const tierLbl = a.tier === 'HIGH_PROB' ? 'HIGH PROB' : a.tier === 'STRONG' ? 'STRONG' : 'REGULAR';

  // ── Live price row ────────────────────────────────────────────────────────
  const chgHtml  = chg24h !== null
    ? `<span class="ac2-chg" style="color:${chg24h >= 0 ? '#00ff88' : '#ff4444'}">${chg24h >= 0 ? '+' : ''}${chg24h.toFixed(2)}%</span>`
    : '';
  const warnHtml = priceDriftPct > 1 ? '<span class="ac2-warn">⚠</span>' : '';

  // ── Metric color helpers ──────────────────────────────────────────────────
  const j15mClr = v => v > 80 ? '#ff4444' : v < 20 ? '#00ff88' : '#ffaa00';
  const rsiClr  = v => v > 65 ? '#ff4444' : v < 35 ? '#00ff88' : '#fff';
  const adxClr  = v => v >= 50 ? '#00ff88' : v >= 25 ? '#ffaa00' : '#fff';

  const mkMetric = (lbl, val, clr, dec) =>
    `<div class="ac2-metric">
      <div class="ac2-metric-label">${lbl}</div>
      <div class="ac2-metric-val" style="color:${clr(val)}">${val.toFixed(dec)}</div>
    </div>`;

  const snapRow = mkMetric('J15M', snapJ15m, j15mClr, 1)
    + mkMetric('RSI',  snapRsi,  rsiClr,  1)
    + mkMetric('ADX',  snapAdx,  adxClr,  1)
    + mkMetric('ATR',  snapAtr,  () => '#fff', 4);

  const nowRow  = mkMetric('J15M', nowJ15m, j15mClr, 1)
    + mkMetric('RSI',  nowRsi,  rsiClr,  1)
    + mkMetric('ADX',  nowAdx,  adxClr,  1)
    + mkMetric('ATR',  nowAtr,  () => '#fff', 4);

  // ── Buttons ───────────────────────────────────────────────────────────────
  const dis      = inTrade ? 'disabled' : '';
  const btnsHtml = isStale
    ? `<button class="ac-btn ac-btn-dismiss" onclick="dismissAlert('${sym}','${a.direction}')">DISMISS</button>`
    : `<button class="ac-btn btn-hl"   ${dis} onclick="openTrade('${sym}','${a.direction}','HL',${a.leverage})">OPEN HL</button>
       <button class="ac-btn btn-mexc" ${dis} onclick="openTrade('${sym}','${a.direction}','MEXC',${a.leverage})">OPEN MEXC</button>
       <button class="ac-btn ac-btn-dismiss" onclick="dismissAlert('${sym}','${a.direction}')">DISMISS</button>`;

  return `<div class="alert-card ${dirClass}" style="${isStale ? 'opacity:0.6;' : ''}">
    ${isStale ? '<div class="ac2-stale-overlay">STALE</div>' : ''}

    <div class="ac-top">
      <div class="ac-sym">${sym}</div>
      <div style="display:flex;gap:4px;align-items:center;flex-wrap:wrap;justify-content:flex-end;">
        ${dirPill}
        <span class="tier-pill ${tierCls}">${tierLbl} ${a.leverage}x</span>
        ${inTrade ? '<span class="in-trade-badge">IN TRADE</span>' : ''}
        ${isPaper ? '<span class="ac-paper-badge">PAPER</span>' : ''}
      </div>
    </div>

    <div class="ac2-prices">
      <div class="ac2-px"><div class="ac2-px-label">ENTRY</div><div class="ac2-px-val white">${fmtPrice(a.entry_price)}</div></div>
      <div class="ac2-px"><div class="ac2-px-label">SL</div><div class="ac2-px-val red">${fmtPrice(a.sl_price)}</div></div>
      <div class="ac2-px"><div class="ac2-px-label">TP1</div><div class="ac2-px-val green">${fmtPrice(a.tp1_price)}</div></div>
      <div class="ac2-px"><div class="ac2-px-label">TP2</div><div class="ac2-px-val" style="color:rgba(0,255,136,0.6)">${fmtPrice(a.tp2_price)}</div></div>
    </div>

    <div class="ac2-live-row">
      <span class="ac2-live-label">LIVE</span>
      <span class="ac2-live-val">${fmtPrice(livePrice)}</span>
      ${chgHtml}${warnHtml}
    </div>

    <div class="ac2-metric-row">
      <span class="ac2-row-pill ac2-pill-snap">SNAP</span>
      <div class="ac2-metrics">${snapRow}</div>
      <span class="ac2-elapsed">${elStr}</span>
    </div>

    <div class="ac2-metric-row">
      <span class="ac2-row-pill ac2-pill-now">NOW</span>
      <div class="ac2-metrics">${nowRow}</div>
      <span class="ac2-live-tag">LIVE</span>
    </div>

    <div class="ac2-stale-row">
      <span class="ac2-stale-label" style="color:${staleColor}">${staleness}</span>
      <div class="ac2-bar-track">
        <div class="ac2-bar-fill" style="width:${barPct.toFixed(1)}%;background:${staleColor}"></div>
      </div>
      <span class="ac2-stale-cd" style="color:${staleColor}">${cdStr}</span>
    </div>

    <div class="ac-btns">${btnsHtml}</div>
  </div>`;
}

// ── Positions tab ─────────────────────────────────────────────────────────────
function renderPositionsTab() {
  const trades     = STATE.open_trades || {};
  const prices     = STATE.prices      || {};
  const pairStates = STATE.pair_states || [];
  const keys       = Object.keys(trades);

  for (const id of Object.keys(posTimers)) { clearInterval(posTimers[id]); }
  posTimers = {};

  if (!keys.length) {
    document.getElementById('pos-grid').innerHTML = '<div class="no-content">No open positions</div>';
    return;
  }
  document.getElementById('pos-grid').innerHTML = keys.map(k => buildPosCard(trades[k], prices, pairStates)).join('');
  setTimeout(startPosTimers, 0);
}

function startPosTimers() {
  const trades = STATE?.open_trades || {};
  for (const trade of Object.values(trades)) {
    const tid = `pct-${trade.symbol}-${trade.direction}`;
    const el  = document.getElementById(tid);
    if (!el) continue;
    const ts = trade.opened_at || 0;
    function makeTick(element, openTs) {
      return function() {
        const sec = Math.max(0, Math.floor(Date.now() / 1000 - openTs));
        const h = Math.floor(sec / 3600);
        const m = Math.floor((sec % 3600) / 60);
        const s = sec % 60;
        element.textContent =
          String(h).padStart(2,'0') + ':' +
          String(m).padStart(2,'0') + ':' +
          String(s).padStart(2,'0');
      };
    }
    const tick = makeTick(el, ts);
    tick();
    posTimers[tid] = setInterval(tick, 1000);
  }
}

function buildPosCard(t, prices, pairStates) {
  const sym      = t.symbol;
  const isLong   = t.direction === 'LONG';
  const current  = t.current_price || prices[sym] || t.entry_price || 0;
  const entry    = t.entry_price   || 0;
  const sl       = t.sl_price      || 0;
  const tp1      = t.tp1_price     || 0;
  const tp2      = t.tp2_price     || 0;
  const be       = t.be_price      || (isLong ? entry * 1.001 : entry * 0.999);
  const tp1Hit   = !!t.tp1_hit;
  const pnl      = t.unrealized_pnl || 0;
  const r        = t.r              || 0;
  const score    = t.score          || 0;
  const margin   = t.margin         || 0;
  const lev      = t.leverage       || 5;
  const paper    = !!t.paper;
  const exch     = t.exchange       || 'HL';
  const openedAt = t.opened_at      || 0;
  const size     = t.size           || 0;

  const ps = (pairStates || []).find(p => p.symbol === sym) || {};

  // Colors
  const dirCol = isLong ? '#00ff88' : '#ff4444';
  const pnlCol = pnl >= 0 ? '#00ff88' : '#ff4444';
  const rCol   = r   >= 0 ? '#00ff88' : '#ff4444';
  const winning = isLong ? current >= entry : current <= entry;
  const arrow   = winning ? '▲' : '▼';
  const arrCol  = winning ? '#00ff88' : '#ff4444';
  const delta   = current - entry;
  const dltCol  = isLong ? (delta >= 0 ? '#00ff88' : '#ff4444') : (delta <= 0 ? '#00ff88' : '#ff4444');
  const absDlt  = Math.abs(delta);
  const dltStr  = (delta >= 0 ? '+' : '-') + (absDlt >= 1000
    ? absDlt.toLocaleString('en-US', {minimumFractionDigits:2, maximumFractionDigits:2})
    : absDlt >= 1 ? absDlt.toFixed(4) : absDlt.toFixed(6));

  // Price ruler: spans SL (0%) → 2R (100%)
  // Works for both LONG and SHORT: pct = (price−sl)/(2R−sl)×100
  // For SHORT sl>entry>2R so denominator is negative — ratios still correct
  const oneR     = Math.abs(entry - sl);
  const twoR     = isLong ? entry + 2 * oneR : entry - 2 * oneR;
  const barRange = twoR - sl;

  function bp(price) {
    if (!barRange || !sl) return 50;
    return Math.min(100, Math.max(0, (price - sl) / barRange * 100));
  }

  const pSl  = bp(sl);
  const pEn  = bp(entry);
  const pBe  = bp(be);
  const pTp1 = bp(tp1);
  const pTp2 = bp(tp2);
  const p2R  = bp(twoR);
  const pCur = bp(current);

  // Zone widths (loss zone: 0%→entry%, gain zone: entry%→100%)
  const gainLeft = Math.min(pEn, p2R).toFixed(1);
  const gainW    = Math.abs(p2R - pEn).toFixed(1);
  const tp1SL    = Math.min(pTp1, pCur).toFixed(1);
  const tp1SW    = Math.abs(pCur - pTp1).toFixed(1);

  // Dollar P&L at levels (full original size)
  const dollarAt = tgt => isLong ? (tgt - entry) * size : (entry - tgt) * size;
  const pnlSl    = dollarAt(sl);
  const pnlTp1   = dollarAt(tp1);
  const pnlTp2   = dollarAt(tp2);

  // Subheader
  const openFmt   = openedAt ? new Date(openedAt*1000).toISOString().replace('T',' ').slice(0,19) : '—';
  const marginFmt = margin >= 1000 ? `$${(margin/1000).toFixed(1)}k` : `$${Math.round(margin)}`;

  // Metrics (live from pair state, fallback to trade snapshot)
  const adx   = ps.adx1h  ?? t.adx1h  ?? 0;
  const rsi   = ps.rsi15m ?? t.rsi15m ?? 0;
  const j15m  = ps.j15m   ?? t.j15m   ?? 0;
  const bidPc = ps.bid_pct ?? t.bid_pct ?? 0;
  const askPc = ps.ask_pct ?? t.ask_pct ?? 0;
  const dPct  = isLong ? bidPc : askPc;
  const dLbl  = isLong ? 'BID%' : 'ASK%';

  const adxCl = v => v >= 50 ? '#00ff88' : v >= 25 ? '#ffaa00' : '#aaa';
  const rsiCl = v => v > 65  ? '#ff4444' : v < 35  ? '#00ff88' : '#aaa';
  const jCl   = v => v > 80  ? '#ff4444' : v < 20  ? '#00ff88' : '#aaa';
  const dCol  = isLong ? (bidPc >= 60 ? '#00ff88' : '#ff4444') : (askPc >= 60 ? '#00ff88' : '#ff4444');

  // Scan narrative
  const jTr  = j15m > 60 ? 'rising' : j15m < 40 ? 'falling' : 'flat';
  const narr = ps.symbol
    ? `SCAN  J ${(+j15m).toFixed(1)}  ${dLbl} ${(+dPct).toFixed(1)}%  ADX ${(+adx).toFixed(1)}  RSI ${(+rsi).toFixed(1)}  J ${jTr}`
    : 'SCAN  awaiting next scan…';

  const tid      = `pct-${sym}-${t.direction}`;
  const closeLbl = `${paper ? 'PAPER ' : ''}CLOSE ${exch}`;
  const closeCls = exch === 'MEXC' ? 'pcv2-btn-mexc' : 'pcv2-btn-hl';
  const cond     = isLong ? 'Bullish' : 'Bearish';

  return `<div class="pcv2" style="border-left:3px solid ${dirCol}">

  <div class="pcv2-hdr">
    <div class="pcv2-hdr-l">
      <span class="pcv2-sym">${sym}</span>
      <span class="pcv2-dir" style="color:${dirCol};border-color:${dirCol}">${t.direction}</span>
      <span style="color:#ffaa00;font-size:13px;line-height:1">★</span>
      <span class="pcv2-sig">Bounce</span>
      <span style="font-size:11px;font-weight:700;color:${dirCol}">${cond}</span>
      ${score ? `<span class="pcv2-sc">${score}pts</span>` : ''}
    </div>
    <span class="pcv2-timer" id="${tid}">00:00:00</span>
  </div>

  <div class="pcv2-sub">${lev}x · ${marginFmt} · ${openFmt}</div>

  <div class="pcv2-live">
    <span style="font-size:20px;color:${arrCol};line-height:1">${arrow}</span>
    <span class="pcv2-price">${fmtPrice(current)}</span>
    <span style="font-family:'JetBrains Mono',monospace;font-size:12px;font-weight:700;color:${dltCol}">${dltStr}</span>
    <span class="pcv2-pnl" style="color:${pnlCol};margin-left:auto">${pnl>=0?'+':''}$${pnl.toFixed(2)}</span>
    <span class="pcv2-r" style="color:${rCol}">${r>=0?'+':''}${r.toFixed(2)}R</span>
  </div>

  <div class="pcv2-ruler-wrap">
    <div class="pcv2-ruler-bar">
      <div class="pcv2-z pcv2-zr" style="left:0%;width:${pEn.toFixed(1)}%"></div>
      <div class="pcv2-z pcv2-zg" style="left:${gainLeft}%;width:${gainW}%"></div>
      ${tp1Hit ? `<div class="pcv2-z pcv2-ztp1" style="left:${tp1SL}%;width:${tp1SW}%"></div>` : ''}
      <div class="pcv2-mk" style="left:${pSl.toFixed(1)}%">
        <span class="pcv2-mkt" style="color:#ff4444">SL<br>${fmtPrice(sl)}</span>
        <span class="pcv2-mck" style="background:#ff4444"></span>
        <span class="pcv2-mkb" style="color:#ff4444">−$${Math.abs(pnlSl).toFixed(0)}</span>
      </div>
      <div class="pcv2-mk" style="left:${pEn.toFixed(1)}%">
        <span class="pcv2-mkt" style="color:#ccc">ENTRY<br>${fmtPrice(entry)}</span>
        <span class="pcv2-mck" style="background:#888"></span>
        <span class="pcv2-mkb"></span>
      </div>
      <div class="pcv2-mk" style="left:${pBe.toFixed(1)}%">
        <span class="pcv2-mkt pcv2-mkt-be" style="color:#ffaa00">BE<br>${fmtPrice(be)}</span>
        <span class="pcv2-mck" style="background:#ffaa00"></span>
        <span class="pcv2-mkb" style="color:#ffaa00">≈$0</span>
      </div>
      ${tp1 ? `<div class="pcv2-mk" style="left:${pTp1.toFixed(1)}%">
        <span class="pcv2-mkt" style="color:#4488ff">TP1<br>${fmtPrice(tp1)}</span>
        <span class="pcv2-mck" style="background:#4488ff"></span>
        <span class="pcv2-mkb" style="color:#4488ff">+$${pnlTp1.toFixed(0)}</span>
      </div>` : ''}
      ${tp2 ? `<div class="pcv2-mk" style="left:${pTp2.toFixed(1)}%">
        <span class="pcv2-mkt" style="color:#00ff88">TP2 1.5R<br>${fmtPrice(tp2)}</span>
        <span class="pcv2-mck" style="background:#00ff88"></span>
        <span class="pcv2-mkb" style="color:#00ff88">+$${pnlTp2.toFixed(0)}</span>
      </div>` : ''}
      <div class="pcv2-mk" style="left:${p2R.toFixed(1)}%">
        <span class="pcv2-mkt" style="color:#3a6644">2.0R<br>${fmtPrice(twoR)}</span>
        <span class="pcv2-mck" style="background:#3a6644"></span>
        <span class="pcv2-mkb"></span>
      </div>
      <div class="pcv2-dot" style="left:${pCur.toFixed(1)}%;background:${pnlCol}"></div>
    </div>
  </div>

  <div class="pcv2-metrics">
    <div class="pcv2-metric"><span class="pcv2-ml">ADX</span><span class="pcv2-mv" style="color:${adxCl(adx)}">${(+adx).toFixed(1)}</span></div>
    <div class="pcv2-metric"><span class="pcv2-ml">RSI15M</span><span class="pcv2-mv" style="color:${rsiCl(rsi)}">${(+rsi).toFixed(1)}</span></div>
    <div class="pcv2-metric"><span class="pcv2-ml">J15M</span><span class="pcv2-mv" style="color:${jCl(j15m)}">${(+j15m).toFixed(1)}</span></div>
    <div class="pcv2-metric"><span class="pcv2-ml">${dLbl}</span><span class="pcv2-mv" style="color:${dCol}">${(+dPct).toFixed(1)}%</span></div>
  </div>

  <div class="pcv2-narr">${narr}</div>

  <div class="pcv2-actions">
    <button class="pcv2-btn ${closeCls}" onclick="closeTrade('${sym}','${t.direction}')">${closeLbl}</button>
    <button class="pcv2-btn pcv2-btn-force" onclick="closeTrade('${sym}','${t.direction}')">FORCE CLOSE</button>
  </div>
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

async function exportCsv() { window.location.href = '/api/tradelog/csv'; }

async function clearLog() {
  const trades  = STATE?.open_trades || {};
  const hasOpen = Object.keys(trades).length > 0;
  const msg = hasOpen
    ? `${Object.keys(trades).length} open position(s) will be force-closed. Clear everything?`
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
