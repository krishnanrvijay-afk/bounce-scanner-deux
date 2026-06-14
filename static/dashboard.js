/* ── Bounce Scanner II — dashboard.js ──────────────────────────────────────── */
let STATE        = null;
let activeFilter = 'ALL';
let activeTab    = 'grid';
let lastScanAt   = null;
let marketOpen   = false;
let posTimers    = {};
let bannerTF     = 'BOTH';

const ADX_FADE_MAX = 60;
const BTC_CORRELATION = {
  ETH:0.94, SOL:0.86, XRP:0.84, DOGE:0.87,
  LINK:0.82, AVAX:0.80, SUI:0.82, NEAR:0.78,
  WIF:0.65, HYPE:0.50, ZEC:0.40
};

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

// ── HyperLiquid Account Pill & Overlay ───────────────────────────────────────
let _hlAccFetched = false;
let _hlAccMasked  = false;
let _hlAccData    = null;

function hlAccOpenCard() {
  document.getElementById('hl-acc-backdrop').classList.add('open');
  document.getElementById('hl-acc-card').classList.add('open');
}

function hlAccCloseCard() {
  document.getElementById('hl-acc-backdrop').classList.remove('open');
  document.getElementById('hl-acc-card').classList.remove('open');
}

function hlAccToggleMask(e) {
  e.stopPropagation();
  _hlAccMasked = !_hlAccMasked;
  const icon = _hlAccMasked ? '🚫' : '👁';
  const eye1 = document.getElementById('hl-acc-pill-eye');
  const eye2 = document.getElementById('hl-acc-card-eye');
  if (eye1) eye1.textContent = icon;
  if (eye2) eye2.textContent = icon;
  _hlAccRender();
}

async function hlAccFetch() {
  const btn = document.getElementById('hl-acc-refresh');
  const pv  = document.getElementById('hl-acc-pill-val');
  if (btn) { btn.textContent = '⟳ FETCHING…'; btn.disabled = true; }
  if (pv)  { pv.textContent = '⟳ fetching…'; pv.style.color = '#444'; pv.style.fontSize = '10px'; }
  ['equity','avail','margin','pnl','pos'].forEach(k => {
    const el = document.getElementById('hl-acc-' + k);
    if (el) { el.textContent = '⟳'; el.style.color = '#333'; }
  });
  try {
    const r = await fetch('/api/hl-balance');
    if (!r.ok) throw new Error('HTTP ' + r.status);
    _hlAccData    = await r.json();
    _hlAccFetched = true;
    const ts = document.getElementById('hl-acc-card-ts');
    if (ts) {
      const d = new Date();
      ts.textContent = 'FETCHED ' + d.toLocaleTimeString([], { hour:'2-digit', minute:'2-digit', second:'2-digit', hour12:false });
    }
    const pill = document.getElementById('hl-acc-pill');
    if (pill) pill.classList.add('fetched');
    if (btn) { btn.textContent = '⟳ REFRESH BALANCE'; btn.disabled = false; }
    _hlAccRender();
  } catch(err) {
    if (btn) { btn.textContent = '⟳ FETCH BALANCE'; btn.disabled = false; }
    if (pv)  { pv.textContent = 'TAP FOR BALANCE'; pv.style.color = '#444'; pv.style.fontSize = '10px'; pv.style.fontWeight = '700'; }
    ['equity','avail','margin','pnl','pos'].forEach(k => {
      const el = document.getElementById('hl-acc-' + k);
      if (el) { el.textContent = '—'; el.style.color = '#2a2a2a'; }
    });
  }
}

function _hlAccRender() {
  if (!_hlAccData) return;
  const d   = _hlAccData;
  const msk = _hlAccMasked;
  const fmt = v => msk ? '••••••' : '$' + v.toFixed(2);
  // Pill equity value
  const pv = document.getElementById('hl-acc-pill-val');
  if (pv) {
    pv.style.fontSize   = '12px';
    pv.style.fontWeight = '700';
    pv.style.color      = msk ? '#333' : '#fff';
    pv.textContent      = msk ? '••••••' : '$' + d.equity.toFixed(2);
  }
  // Card values
  const eq = document.getElementById('hl-acc-equity');
  const av = document.getElementById('hl-acc-avail');
  const mg = document.getElementById('hl-acc-margin');
  const pn = document.getElementById('hl-acc-pnl');
  const ps = document.getElementById('hl-acc-pos');
  if (eq) { eq.textContent = fmt(d.equity);      eq.style.color = msk ? '#2a2a2a' : '#ffffff'; }
  if (av) { av.textContent = fmt(d.available);   av.style.color = msk ? '#2a2a2a' : '#00e676'; }
  if (mg) { mg.textContent = fmt(d.margin_used); mg.style.color = msk ? '#2a2a2a' : '#b388ff'; }
  if (pn) {
    if (msk) { pn.textContent = '••••••'; pn.style.color = '#2a2a2a'; }
    else { pn.textContent = (d.unrealized_pnl >= 0 ? '+' : '') + '$' + d.unrealized_pnl.toFixed(2); pn.style.color = d.unrealized_pnl >= 0 ? '#00e676' : '#ff5252'; }
  }
  if (ps) { ps.textContent = d.open_positions; ps.style.color = '#ffffff'; }
}

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

// ── Market Health strip ───────────────────────────────────────────────────────
function renderMarketHealth() {
    const mh = STATE?.market_health;

    const context = (status, side, mh) => {
      if (!mh) return '<div class="mh-ctx-line">Initialising…</div>';
      const isBear = side === 'SHORT';
      const ratio  = isBear ? (mh.bear_ratio ?? 0) : (mh.bull_ratio ?? 0);
      const total  = mh.total || 10;
      const pCount = Math.round(ratio * total);
      const lbl    = isBear ? 'bear' : 'bull';
      const adx    = mh.avg_adx || 0;
      const j5     = mh.avg_j5  || 50;
      const slN    = Math.round((mh.sl_rate || 0) * 6);
      if (status === 'RUN') {
        return '<div class="mh-ctx-line">All conditions met</div>' +
               '<div class="mh-ctx-line">Signals clear — ready to fire</div>';
      }
      if (status === 'HALT') {
        const lines = [];
        if (ratio < 0.3)
          lines.push(`${lbl} pairs ${pCount} of ${total} — need ${Math.ceil(total * 0.3)}+`);
        if ((mh.sl_rate || 0) >= 0.6)
          lines.push(`SL rate ${slN}/6 — too high`);
        if (isBear  && j5 >= 85 && ratio < 0.5)
          lines.push(`Avg J5 ${j5.toFixed(1)} overbought + bears below 50%`);
        if (!isBear && j5 <= 15 && ratio < 0.5)
          lines.push(`Avg J5 ${j5.toFixed(1)} oversold + bulls below 50%`);
        if (!lines.length) lines.push('Market conditions unsafe');
        return lines.slice(0, 2).map(l => `<div class="mh-ctx-line">${l}</div>`).join('');
      }
      const lines = [];
      if (ratio < 0.6)
        lines.push(`Need ${lbl} ratio 0.6 — currently ${ratio.toFixed(2)}`);
      if (adx < 35)
        lines.push(`Need avg ADX 35 — currently ${adx.toFixed(1)}`);
      if (isBear && j5 > 70)
        lines.push(`Need avg J5 ≤70 — currently ${j5.toFixed(1)}`);
      if (!isBear && j5 < 30)
        lines.push(`Need avg J5 ≥30 — currently ${j5.toFixed(1)}`);
      if ((mh.sl_rate || 0) >= 0.4)
        lines.push(`SL rate ${slN}/6 — need below 3`);
      if (!lines.length) lines.push('Near RUN threshold');
      return lines.slice(0, 2).map(l => `<div class="mh-ctx-line">${l}</div>`).join('');
    };

    // Store state for overlay (opened on chip tap)
    window._mhStatusShort = mh?.short_status || 'CAUTION';
    window._mhStatusLong  = mh?.long_status  || 'CAUTION';
    window._mhCtxShort    = context(window._mhStatusShort, 'SHORT', mh);
    window._mhCtxLong     = context(window._mhStatusLong,  'LONG',  mh);

    // Update header chips
    const updateChip = (chipId, dotId, status) => {
      const chip = document.getElementById(chipId);
      const dot  = document.getElementById(dotId);
      if (!chip || !dot) return;
      const st = (status || 'caution').toLowerCase();
      chip.className = `mh-chip mh-chip-${st}`;
      dot.className  = `mhc-dot mhc-dot-${st}`;
    };
    updateChip('mhc-short', 'mhc-short-dot', window._mhStatusShort);
    updateChip('mhc-long',  'mhc-long-dot',  window._mhStatusLong);
  }

  function openMhOverlay() {
    const bd   = document.getElementById('mh-ov-bd');
    const body = document.getElementById('mh-ov-body');
    if (!bd || !body) return;
    const sStatus = window._mhStatusShort || 'CAUTION';
    const lStatus = window._mhStatusLong  || 'CAUTION';
    const sCtx    = (window._mhCtxShort || '<div class="mh-ctx-line">Initialising…</div>').replace(/mh-ctx-line/g, 'mh-ov-ctx-line');
    const lCtx    = (window._mhCtxLong  || '<div class="mh-ctx-line">Initialising…</div>').replace(/mh-ctx-line/g, 'mh-ov-ctx-line');
    const pilCls  = st => `mh-ov-pill mh-ov-pill-${st.toLowerCase()}`;
    body.innerHTML =
      '<div class="mh-ov-section">' +
        '<div class="mh-ov-side-hdr">' +
          '<span class="mh-ov-side-label">SHORT SIDE</span>' +
          `<span class="${pilCls(sStatus)}">${sStatus}</span>` +
        '</div>' +
        sCtx +
      '</div>' +
      '<div class="mh-ov-divider"></div>' +
      '<div class="mh-ov-section">' +
        '<div class="mh-ov-side-hdr">' +
          '<span class="mh-ov-side-label">LONG SIDE</span>' +
          `<span class="${pilCls(lStatus)}">${lStatus}</span>` +
        '</div>' +
        lCtx +
      '</div>';
    bd.classList.add('open');
  }

  function closeMhOverlay() {
    document.getElementById('mh-ov-bd')?.classList.remove('open');
  }
function render() {
  renderHeader();
  updateNavCounts();
  updateScanStatus();
  renderBanner();
  renderMarketHealth();
  if (activeTab === 'grid')   renderCards();
  if (activeTab === 'alerts') renderAlertsTab();
  if (activeTab === 'pos')    renderPositionsTab();
  if (activeTab === 'log')    renderLogTab();
  if (marketOpen)             updateMarketPopover();
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

  const _upnl   = STATE?.unrealized_pnl || 0;
  const _upnlEl = document.getElementById('h-unrealized');
  if (_upnlEl) {
    _upnlEl.textContent = (_upnl > 0 ? '+' : _upnl < 0 ? '-' : '') + '$' + Math.abs(_upnl).toFixed(2);
    _upnlEl.className   = 'hstat-value ' + (_upnl > 0 ? 'green' : _upnl < 0 ? 'red' : '');
  }
  document.getElementById('h-positions').textContent = account?.slots_used || 0;
  document.getElementById('h-scans').textContent     = scan_count || 0;

  const modeBadge = document.getElementById('mode-badge');
  if (modeBadge) {
    if (account?.paper_mode) {
      modeBadge.style.display    = 'block';
      modeBadge.className        = 'mode-badge mode-badge-paper';
      modeBadge.textContent      = 'PAPER';
    } else if (account?.live_manual_entry_only) {
      modeBadge.style.display    = 'block';
      modeBadge.className        = 'mode-badge mode-badge-live-safe';
      modeBadge.textContent      = 'LIVE 🔒';
    } else {
      modeBadge.style.display    = 'block';
      modeBadge.className        = 'mode-badge mode-badge-live-danger';
      modeBadge.textContent      = 'LIVE ⚠';
    }
  }
  document.getElementById('cb-badge').style.display    = circuit_breaker?.active ? 'block' : 'none';
  renderResetSessionBtn();
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
  const rsi15m    = p.rsi15m      || 0;
  const stochK     = p.stoch_k      || 0;
  const stochD     = p.stoch_d      || 0;
  const stochKPrev = p.stoch_k_prev != null ? +p.stoch_k_prev : stochK;
  const stochDPrev = p.stoch_d_prev != null ? +p.stoch_d_prev : stochD;
  const bidPct = p.bid_pct || 0;
  const askPct = p.ask_pct || 0;
  const adx1h  = p.adx1h   || 0;
  const cdS    = p.cooldown_short || 0;
  const cdL    = p.cooldown_long  || 0;
  const inTrade = p.in_trade;
  const chg    = changes[sym] ?? null;

  let chgHtml = '';
  if (chg !== null) {
    const chgColor = chg >= 0 ? '#00ff88' : '#ff4444';
    chgHtml = `<span class="card-chg" style="color:${chgColor}">${chg >= 0 ? '+' : ''}${chg.toFixed(2)}%</span>`;
  }

  const adxFade  = adx1h > ADX_FADE_MAX;
  const adxColor = adxFade     ? '#ff4444'
                 : adx1h >= 50 ? '#00ff88'
                 : adx1h >= 25 ? '#ffaa00'
                 : '#ffffff';

  // Gate counts
  const shortGates = [j15m > 80, j1h > 60, stochK > 75 && stochK < stochD, askPct >= 55];
  const longGates  = [j15m < 20, j1h < 40, stochK < 25 && stochK > stochD, bidPct >= 55];
  const shortCount = shortGates.filter(Boolean).length;
  const longCount  = longGates.filter(Boolean).length;
  const shortFull  = shortCount === 4;
  const longFull   = longCount  === 4;
  const diverge    = shortCount === longCount && !shortFull;
  const showShort  = shortCount >= longCount || diverge;
  const showLong   = longCount  >= shortCount || diverge;
  const leadCount  = Math.max(shortCount, longCount);
  const nearTrig   = !shortFull && !longFull && leadCount === 3;
  const hasAlert   = alerts.some(a => a.symbol === sym);

  // Confluence detection
  const longConf   = j15m < 20 && j1h < 40;
  const shortConf  = j15m > 80 && j1h > 60;
  const isConf     = longConf || shortConf;
  const confIsLong = longConf;

  // ── Card glow — unified trend-based ──────────────────────────────────────────
  const cardCls = 'pair-card';
  let glowStyle;
  const trend = p.trend || 'Neutral';
  if (inTrade) {
    glowStyle = 'border:1px solid rgba(41,121,255,0.6);box-shadow:0 0 20px rgba(41,121,255,0.15),0 2px 8px rgba(0,0,0,0.6)';
  } else if (trend === 'Strong Bull') {
    glowStyle = 'border:1px solid rgba(34,197,94,0.5);box-shadow:0 0 12px 3px rgba(34,197,94,0.7),0 2px 8px rgba(0,0,0,0.6)';
  } else if (trend === 'Bullish') {
    glowStyle = 'border:1px solid rgba(34,197,94,0.3);box-shadow:0 0 8px 2px rgba(34,197,94,0.4),0 2px 8px rgba(0,0,0,0.6)';
  } else if (trend === 'Strong Bear') {
    glowStyle = 'border:1px solid rgba(239,68,68,0.5);box-shadow:0 0 12px 3px rgba(239,68,68,0.7),0 2px 8px rgba(0,0,0,0.6)';
  } else if (trend === 'Bearish') {
    glowStyle = 'border:1px solid rgba(239,68,68,0.3);box-shadow:0 0 8px 2px rgba(239,68,68,0.4),0 2px 8px rgba(0,0,0,0.6)';
  } else {
    glowStyle = 'border:1px solid rgba(255,255,255,0.1);box-shadow:none';
  }
  // ── Symbol class for confluence name glow ────────────────────────────────────
  const symCls = (trend === 'Strong Bull' || trend === 'Bullish') ? 'card-sym card-sym-conf-long'
               : (trend === 'Strong Bear' || trend === 'Bearish') ? 'card-sym card-sym-conf-short'
               : 'card-sym';

  // ── Inline direction rows: arrow + 4 gate dots + J15M/J1H values ─────────────
  function dotRowJ(dir, gateArr) {
    const isL    = dir === 'LONG';
    const arrow  = isL ? '▲' : '▼';
    const arCls  = isL ? 'arrow-long' : 'arrow-short';
    const pfx    = isL ? 'long' : 'short';
    const dots   = gateArr.map(g => `<span class="gc-dot ${pfx}-${g ? 'pass' : 'fail'}"></span>`).join('');
    const j15Col = isL ? (j15m < 20 ? '#00e676' : '#555') : (j15m > 80 ? '#ff3d57' : '#555');
    const j1hCol = isL ? (j1h  < 40 ? '#00e676' : '#555') : (j1h  > 60 ? '#ff3d57' : '#555');
    return `<div class="sym-dir-row">
      <span class="dir-arrow ${arCls}">${arrow}</span>
      <div class="gate-cluster">${dots}</div>
      <span class="j-inline"><span style="color:${j15Col}">${j15m.toFixed(0)}</span><span class="j-slash">/</span><span style="color:${j1hCol}">${j1h.toFixed(0)}</span></span>
    </div>`;
  }

  let inlineDir = '';
  if (diverge && shortCount > 0) {
    inlineDir = `<div class="sym-dir-wrap">${dotRowJ('SHORT', shortGates)}${dotRowJ('LONG', longGates)}</div>`;
  } else if (shortCount > longCount) {
    inlineDir = `<div class="sym-dir-wrap">${dotRowJ('SHORT', shortGates)}</div>`;
  } else if (longCount > shortCount) {
    inlineDir = `<div class="sym-dir-wrap">${dotRowJ('LONG', longGates)}</div>`;
  }

  // ── Gate rows: RSI + DEPTH only (J moved to symbol line) ─────────────────────
  let rows = '';
  if (showShort) rows += dirRow('SHORT', stochK, stochD, rsi15m, askPct);
  if (showLong)  rows += dirRow('LONG',  stochK, stochD, rsi15m, bidPct);

  // ── Confluence mini bars (RSI + Depth) — shown only on confluence cards ───────
  let confBars = '';
  if (isConf) {
    const depthPct   = confIsLong ? bidPct : askPct;
    const depthLabel = confIsLong ? 'BID' : 'ASK';
    const depthPass  = depthPct >= 55;
    const stochPass   = confIsLong ? (stochK < 25 && stochK > stochD) : (stochK > 75 && stochK < stochD);
    const stochPct    = Math.min(100, Math.max(0, stochK));
    const stochCurCol = confIsLong ? (stochK < 25 ? '#00e676' : '#555') : (stochK > 75 ? '#ff3d57' : '#555');
    const stochDotCls = stochPass ? (confIsLong ? 'long-pass' : 'short-pass') : (confIsLong ? 'long-fail' : 'short-fail');
    const dptDotCls  = depthPass ? (confIsLong ? 'long-pass' : 'short-pass') : (confIsLong ? 'long-fail' : 'short-fail');
    const fillPct    = Math.min(100, Math.max(0, depthPct));
    const fillColor  = confIsLong
      ? (depthPass ? 'rgba(0,230,118,0.7)' : 'rgba(0,230,118,0.25)')
      : (depthPass ? 'rgba(255,61,87,0.7)'  : 'rgba(255,61,87,0.25)');
    const fillStyle  = confIsLong
      ? `left:0;width:${fillPct}%;background:${fillColor}`
      : `right:0;width:${fillPct}%;background:${fillColor}`;
    const gateLinePct = confIsLong ? 55 : 45;

    confBars = `<div class="cbar-row">
      <span class="gc-dot cbar-dot ${stochDotCls}"></span>
      <span class="cbar-label">STOCH</span>
      <div class="cbar-track">
        <div class="cbar-zg" style="width:25%"></div>
        <div class="cbar-zr" style="left:75%;width:25%"></div>
        <div class="cbar-thresh cbar-thresh-l" style="left:25%"></div>
        <div class="cbar-thresh cbar-thresh-r" style="left:75%"></div>
        <div class="cbar-cursor" style="left:${stochPct}%;background:${stochCurCol};box-shadow:0 0 5px ${stochCurCol}"></div>
      </div>
    </div>
    <div class="cbar-row">
      <span class="gc-dot cbar-dot ${dptDotCls}"></span>
      <span class="cbar-label">${depthLabel}</span>
      <div class="cbar-track">
        <div class="cbar-fill" style="${fillStyle}"></div>
        <div class="cbar-thresh" style="left:${gateLinePct}%;border-color:rgba(255,170,0,0.5)"></div>
      </div>
      <span class="cbar-val">${depthPct.toFixed(0)}%</span>
    </div>`;
  }

  // ── Pills / readiness ─────────────────────────────────────────────────────────
  let pills = '';
  if (isConf) {
    const gateArr = confIsLong ? longGates : shortGates;
    const passing  = gateArr.filter(Boolean).length;
    const rdyCls   = confIsLong ? 'pill-ready-long' : 'pill-ready-short';
    if (passing === 4) {
      const _cdDir   = confIsLong ? cdL : cdS;
      const _shDir   = confIsLong ? p.session_halted_long : p.session_halted_short;
      const _lgCdDir = confIsLong ? (p.large_sl_cd_long||0) : (p.large_sl_cd_short||0);
      let _veto = null;
      if (_cdDir   > 0) _veto = 'COOLDOWN';
      if (_lgCdDir > 0 && !_veto) _veto = 'COOLDOWN';
      if (adxFade       && !_veto) _veto = `ADX ${adx1h.toFixed(0)} FADE`;
      if (_shDir        && !_veto) _veto = `SESSION ${(STATE.session||'').trim()}`.trim();
      if (STATE.circuit_breaker?.active && !_veto) _veto = 'CIRCUIT BRK';
      pills = _veto
        ? `<span class="pill" style="color:#ff9900;border-color:#ff9900;background:rgba(255,153,0,0.12)">BLOCKED: ${_veto}</span>`
        : `<span class="pill ${rdyCls}">✦ READY</span>`;
    }
    else if (passing === 3) pills = `<span class="pill pill-near-rdy">NEAR 3/4</span>`;
    else                    pills = `<span class="pill pill-partial">PARTIAL ${passing}/4</span>`;
  } else {
    if (inTrade)   pills += `<span class="pill pill-intrade">IN TRADE</span>`;
    if (cdS > 0)   pills += `<span class="pill pill-cd">CD-S ${fmtCd(cdS)}</span>`;
    if (cdL > 0)   pills += `<span class="pill pill-cd">CD-L ${fmtCd(cdL)}</span>`;
    if (diverge)   pills += `<span class="pill pill-diverge">DIVERGENCE</span>`;
    if (nearTrig)  pills += `<span class="pill pill-near">NEAR TRIGGER</span>`;
    if (adxFade)   pills += `<span class="pill pill-adxmax">ADX ${adx1h.toFixed(0)} FADE MAX</span>`;
    // Session halt + large SL CD pills
    const _sess      = STATE.session || '';
    const _sHaltL    = p.session_halted_long;
    const _sHaltS    = p.session_halted_short;
    const _lgCDL     = p.large_sl_cd_long  || 0;
    const _lgCDS     = p.large_sl_cd_short || 0;
    if (_sHaltL) pills += `<span class="pill pill-halted">LONG HALTED — ${_sess}</span>`;
    if (_sHaltS) pills += `<span class="pill pill-halted">SHORT HALTED — ${_sess}</span>`;
    if (!_sHaltL && _lgCDL > 0) pills += `<span class="pill pill-cd-large">COOLDOWN ${fmtCd(_lgCDL)}</span>`;
    if (!_sHaltS && _lgCDS > 0) pills += `<span class="pill pill-cd-large">COOLDOWN ${fmtCd(_lgCDS)}</span>`;
    if (shortFull && hasAlert) pills += `<span class="pill pill-alert-s">▼ ALERT</span>`;
    if (longFull  && hasAlert) pills += `<span class="pill pill-alert">▲ ALERT</span>`;
  }

  return `<div class="${cardCls}" style="${glowStyle}">
    <div class="card-top">
      <div class="card-sym-block">
        <span class="${symCls}" style="cursor:pointer" onclick="openPairOverlay('${sym}')">${sym}</span>
        ${inlineDir}
      </div>
      <div class="card-right">
        <div class="card-price-line">
          <span class="card-price">${fmtPrice(price)}</span>${chgHtml}<span class="card-price-cd price-cd-val">${_priceCdSec}s</span>
        </div>
      </div>
    </div>
    <div class="card-adx-compact"><span class="adx-cl">ADX</span><span class="adx-cv" style="color:${adxColor}">${adx1h.toFixed(1)}</span><span class="card-meta-sep">·</span><span class="adx-cl">J15M</span><span class="adx-cv" style="color:${j15m < 20 ? '#00ff88' : j15m > 80 ? '#ff4444' : '#fff'}">${j15m.toFixed(0)}</span><span class="card-meta-sep">·</span><span class="adx-cl">J1H</span><span class="adx-cv" style="color:${j1h < 40 ? '#00ff88' : j1h > 60 ? '#ff4444' : '#fff'}">${j1h.toFixed(0)}</span></div>
    ${rows}
    ${confBars}
    <div class="card-footer">${pills || `<span class="pill pill-scanning">SCANNING</span>`}</div>
  </div>`;
}

function dirRow(direction, stochK, stochD, rsi15m, depthPct) {
  const isLong      = direction === 'LONG';
  const rowCls      = isLong ? 'long-row' : 'short-row';
  const depthLabel  = isLong ? 'BID%' : 'ASK%';
  const stochColor  = isLong ? (stochK < 25 ? 'green' : 'grey') : (stochK > 75 ? 'red' : 'grey');
  const depthColor  = depthPct >= 55 ? (isLong ? 'green' : 'red') : 'grey';

  return `<div class="dir-row ${rowCls}">
    <div class="dir-vals">
      <div class="dv-item">
        <span class="dv-label">STOCH</span>
        <span class="dv-val ${stochColor}">${stochK.toFixed(1)}/${stochD.toFixed(1)}</span>
        <span style="color:#555;font-size:9px;margin-left:3px">RSI${rsi15m.toFixed(0)}</span>
      </div>
      <div class="dv-item">
        <span class="dv-label">${depthLabel}</span>
        <span class="dv-val ${depthColor}">${depthPct.toFixed(0)}%</span>
      </div>
    </div>
  </div>`;
}

// ── Banner TF switcher ────────────────────────────────────────────────────────
function setBannerTF(tf) {
  bannerTF = tf;
  ['15M', '1H', 'BOTH'].forEach(t => {
    const el = document.getElementById(`jb-tf-${t}`);
    if (!el) return;
    el.className = 'jb-tf-pill' + (bannerTF === t ? ` jb-tf-active-${t}` : '');
  });
  const r15m = document.getElementById('jb-ruler-15m');
  const r1h  = document.getElementById('jb-ruler-1h');
  if (r15m) r15m.style.display = (bannerTF === '1H')  ? 'none' : '';
  if (r1h)  r1h.style.display  = (bannerTF === '15M') ? 'none' : '';
  renderBanner();
}

// ── Compact J Opportunity Banner — chips on bar ───────────────────────────────
function renderBanner() {
  const pairs = STATE?.pair_states || [];
  if (!pairs.length) return;

  function fillRuler(containerId, tfKey) {
    const container = document.getElementById(containerId);
    if (!container || container.style.display === 'none') return;

    const items = [...pairs].map(p => {
      const raw = tfKey === '15m' ? (p.j15m || 50) : (p.j1h || 50);
      const j   = Math.min(97, Math.max(3, +raw));
      const longConf  = (p.j15m || 0) < 20 && (p.j1h || 0) < 40;
      const shortConf = (p.j15m || 0) > 80 && (p.j1h || 0) > 60;
      return { sym: p.symbol, j, longConf, shortConf };
    }).sort((a, b) => a.j - b.j);

    // Anti-overlap: up to 3 stagger rows — pairs within 6 pts try next row
      const NUM_ROWS = 3;
      const rowEdge = new Array(NUM_ROWS).fill(undefined);
      const placed = items.map(item => {
        let row = 0;
        for (let r = 0; r < NUM_ROWS; r++) {
          if (rowEdge[r] === undefined || rowEdge[r] <= item.j - 5) { row = r; break; }
          row = Math.min(r + 1, NUM_ROWS - 1);
        }
        rowEdge[row] = item.j + 5;
        return { ...item, row };
      });

    container.innerHTML = placed.map(({ sym, j, row, longConf, shortConf }) => {
      const isConf = longConf || shortConf;
      const col = tfKey === '15m'
        ? (j < 20 ? '#00e676' : j < 35 ? 'rgba(0,230,118,0.5)' : j < 65 ? 'rgba(255,255,255,0.4)' : j < 80 ? 'rgba(255,61,87,0.5)' : '#ff3d57')
        : (j < 40 ? '#00e676' : j < 50 ? 'rgba(0,230,118,0.5)' : j < 60 ? 'rgba(255,255,255,0.4)' : j < 70 ? 'rgba(255,61,87,0.5)' : '#ff3d57');
      const pulseCls   = isConf ? ' cb-conf' : '';
      const extraBot   = row * 16;
      return `<div class="cb-chip${pulseCls}" style="left:${j.toFixed(1)}%;bottom:${extraBot}px;color:${col}">${sym}${isConf ? '✦' : ''}<div class="cb-tick"></div></div>`;
    }).join('');
  }

  fillRuler('jb-chips-15m', '15m');
  fillRuler('jb-chips-1h',  '1h');
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
  const snapRsi    = +(a.rsi15m  || 0);
  const snapStochK = +(a.stoch_k || 0);
  const snapStochD = +(a.stoch_d || 0);
  const snapAdx    = +(a.adx1h   || 0);
  const snapAtr    = +(a.atr15m  || 0);

  // ── NOW data (live from pair_states) ──────────────────────────────────────
  const ps       = (pairMap || {})[sym] || {};
  const nowJ15m  = ps.j15m    != null ? +ps.j15m    : snapJ15m;
  const nowRsi   = ps.rsi15m  != null ? +ps.rsi15m  : snapRsi;
  const nowStochK= ps.stoch_k != null ? +ps.stoch_k : snapStochK;
  const nowStochD= ps.stoch_d != null ? +ps.stoch_d : snapStochD;
  const nowAdx   = ps.adx1h   != null ? +ps.adx1h   : snapAdx;
  const nowAtr   = ps.atr15m  != null ? +ps.atr15m  : snapAtr;

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
  const rsiClr   = v => v > 65 ? '#ff4444' : v < 35 ? '#00ff88' : '#fff';
  const stochClr = v => v > 75 ? '#ff4444' : v < 25 ? '#00ff88' : '#fff';
  const adxClr  = v => v >= 50 ? '#00ff88' : v >= 25 ? '#ffaa00' : '#fff';

  const mkMetric = (lbl, val, clr, dec) =>
    `<div class="ac2-metric">
      <div class="ac2-metric-label" style="color:#fff;font-weight:700">${lbl}</div>
      <div class="ac2-metric-val" style="color:${clr(val)}">${val.toFixed(dec)}</div>
    </div>`;

  const snapRow = mkMetric('J15M',  snapJ15m,  j15mClr, 1)
    + mkMetric('STOCH', snapStochK, stochClr, 1)
    + mkMetric('RSI',   snapRsi,    rsiClr,   1)
    + mkMetric('ADX',   snapAdx,    adxClr,   1)
    + mkMetric('ATR',   snapAtr,    () => '#fff', 4);

  const nowRow  = mkMetric('J15M',   nowJ15m,   j15mClr, 1)
    + mkMetric('STOCH',  nowStochK,  stochClr, 1)
    + mkMetric('RSI',    nowRsi,     rsiClr,   1)
    + mkMetric('ADX',    nowAdx,     adxClr,   1)
    + mkMetric('ATR',    nowAtr,     () => '#fff', 4);

  // ── Buttons ───────────────────────────────────────────────────────────────
  const dis      = inTrade ? 'disabled' : '';
  const btnsHtml = isStale
    ? `<button class="ac-btn ac-btn-dismiss" onclick="dismissAlert('${sym}','${a.direction}')">DISMISS</button>`
    : `<button class="ac-btn btn-hl"   ${dis} onclick="openTrade('${sym}','${a.direction}','HL',${a.leverage})">OPEN HL</button>
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
      <div class="ac2-px"><div class="ac2-px-label" style="color:#fff;font-weight:700">ENTRY</div><div class="ac2-px-val white" style="font-weight:700">${fmtPrice(a.entry_price)}</div></div>
      <div class="ac2-px"><div class="ac2-px-label" style="color:#fff;font-weight:700">SL</div><div class="ac2-px-val red" style="font-weight:700">${fmtPrice(a.sl_price)}</div></div>
      <div class="ac2-px"><div class="ac2-px-label" style="color:#fff;font-weight:700">TP1</div><div class="ac2-px-val green" style="font-weight:700">${fmtPrice(a.tp1_price)}</div></div>
      <div class="ac2-px"><div class="ac2-px-label" style="color:#fff;font-weight:700">TP2</div><div class="ac2-px-val" style="color:#00ff88;font-weight:700">${fmtPrice(a.tp2_price)}</div></div>
    </div>

    <div class="ac2-live-row">
      <span class="ac2-live-label" style="color:#fff;font-weight:700">LIVE</span>
      <span class="ac2-live-val" style="color:#fff;font-weight:700">${fmtPrice(livePrice)}</span>
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
  const tp2       = t.tp2_price     || 0;
  const trailBest = t.trail_best_price || 0;
  const trailStop = t.trail_stop_price || 0;
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
  const pTp2       = bp(tp2);
  const pTrailBest = bp(trailBest);
  const pTrailStop = bp(trailStop);
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
  const pnlTp2      = dollarAt(tp2);
  const pnlTrailStop = trailStop ? dollarAt(trailStop) : 0;

  // Subheader
  const openFmt   = openedAt ? new Date(openedAt*1000).toISOString().replace('T',' ').slice(0,19) : '—';
  const marginFmt = margin >= 1000 ? `$${(margin/1000).toFixed(1)}k` : `$${Math.round(margin)}`;

  // Metrics (live from pair state, fallback to trade snapshot)
  const adx   = ps.adx1h  ?? t.adx1h  ?? 0;
  const rsi   = ps.rsi15m ?? t.rsi15m ?? 0;
  const j15m  = ps.j15m   ?? t.j15m   ?? 0;
  const sK    = ps.stoch_k ?? t.stoch_k ?? 0;
  const sD    = ps.stoch_d ?? t.stoch_d ?? 0;
  const bidPc = ps.bid_pct ?? t.bid_pct ?? 0;
  const askPc = ps.ask_pct ?? t.ask_pct ?? 0;
  const dPct  = isLong ? bidPc : askPc;
  const dLbl  = isLong ? 'BID%' : 'ASK%';

  const adxCl   = v => v >= 50 ? '#00ff88' : v >= 25 ? '#ffaa00' : '#fff';
  const rsiCl   = v => v > 65  ? '#ff4444' : v < 35  ? '#00ff88' : '#fff';
  const jCl     = v => v > 80  ? '#ff4444' : v < 20  ? '#00ff88' : '#fff';
  const stochCl = v => v > 75  ? '#ff4444' : v < 25  ? '#00ff88' : '#fff';
  const dCol    = isLong ? (bidPc >= 60 ? '#00ff88' : '#ff4444') : (askPc >= 60 ? '#00ff88' : '#ff4444');

  // Scan narrative
  const jTr  = j15m > 60 ? 'rising' : j15m < 40 ? 'falling' : 'flat';
  const narr = ps.symbol
    ? `SCAN  J ${(+j15m).toFixed(1)}  ${dLbl} ${(+dPct).toFixed(1)}%  ADX ${(+adx).toFixed(1)}  RSI ${(+rsi).toFixed(1)}  K/D ${(+sK).toFixed(0)}/${(+sD).toFixed(0)}  J ${jTr}`
    : 'SCAN  awaiting next scan…';

  const tid      = `pct-${sym}-${t.direction}`;
  const closeLbl = `${paper ? 'PAPER ' : ''}CLOSE HL`;
  const closeCls = 'pcv2-btn-hl';
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

  <div class="pcv2-sub">${lev}x · ${marginFmt} · ${openFmt}${t.session ? ' · <span style="color:#aaa;font-size:10px;letter-spacing:1px">' + t.session + '</span>' : ''}</div>

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
        <span class="pcv2-mkt" style="color:#fff;font-weight:700">ENTRY<br>${fmtPrice(entry)}</span>
        <span class="pcv2-mck" style="background:#888"></span>
        <span class="pcv2-mkb"></span>
      </div>
      <div class="pcv2-mk" style="left:${pBe.toFixed(1)}%">
        <span class="pcv2-mkt pcv2-mkt-be" style="color:#ffaa00">BE<br>${fmtPrice(be)}</span>
        <span class="pcv2-mck" style="background:#ffaa00"></span>
        <span class="pcv2-mkb" style="color:#ffaa00">≈$0</span>
      </div>
      ${tp1 ? `<div class="pcv2-mk" style="left:${pTp1.toFixed(1)}%">
        <span class="pcv2-mkt" style="color:#00ff88">TP1<br>${fmtPrice(tp1)}</span>
        <span class="pcv2-mck" style="background:#00ff88"></span>
        <span class="pcv2-mkb" style="color:#00ff88">+$${pnlTp1.toFixed(0)}</span>
      </div>` : ''}
      ${!tp1Hit && tp2 ? `<div class="pcv2-mk" style="left:${pTp2.toFixed(1)}%">
        <span class="pcv2-mkt" style="color:#00ff88">TP2 1.5R<br>${fmtPrice(tp2)}</span>
        <span class="pcv2-mck" style="background:#00ff88"></span>
        <span class="pcv2-mkb" style="color:#00ff88">+$${pnlTp2.toFixed(0)}</span>
      </div>` : ''}
      ${tp1Hit && trailBest ? `<div class="pcv2-mk" style="left:${pTrailBest.toFixed(1)}%">
        <span class="pcv2-mkt" style="color:#ffaa00;text-decoration:underline dotted">BEST<br>${fmtPrice(trailBest)}</span>
        <span class="pcv2-mck" style="background:#ffaa00;opacity:0.6"></span>
        <span class="pcv2-mkb" style="color:#ffaa00"></span>
      </div>` : ''}
      ${tp1Hit && trailStop ? `<div class="pcv2-mk" style="left:${pTrailStop.toFixed(1)}%">
        <span class="pcv2-mkt" style="color:#ff8800">TRAIL<br>${fmtPrice(trailStop)}</span>
        <span class="pcv2-mck" style="background:#ff8800"></span>
        <span class="pcv2-mkb" style="color:#ff8800">${pnlTrailStop>=0?'+':'-'}$${Math.abs(pnlTrailStop).toFixed(0)}</span>
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
    <div class="pcv2-metric"><span class="pcv2-ml" style="color:#fff;font-weight:700">ADX</span><span class="pcv2-mv" style="color:${adxCl(adx)}">${(+adx).toFixed(1)}</span></div>
    <div class="pcv2-metric"><span class="pcv2-ml" style="color:#fff;font-weight:700">STOCH</span><span class="pcv2-mv" style="color:${stochCl(sK)}">${(+sK).toFixed(1)}/${(+sD).toFixed(1)}</span><span style="color:#555;font-size:9px;margin-left:3px">RSI${(+rsi).toFixed(0)}</span></div>
    <div class="pcv2-metric"><span class="pcv2-ml" style="color:#fff;font-weight:700">J15M</span><span class="pcv2-mv" style="color:${jCl(j15m)}">${(+j15m).toFixed(1)}</span></div>
    <div class="pcv2-metric"><span class="pcv2-ml" style="color:#fff;font-weight:700">${dLbl}</span><span class="pcv2-mv" style="color:${dCol}">${(+dPct).toFixed(1)}%</span></div>
  </div>

  <div class="pcv2-narr" style="color:#fff;font-weight:700">${narr}</div>

  <div class="pcv2-actions">
    <button class="pcv2-btn ${closeCls}" onclick="closeTrade('${sym}','${t.direction}')">${closeLbl}</button>
    <button class="pcv2-btn pcv2-btn-force" onclick="closeTrade('${sym}','${t.direction}')">FORCE CLOSE</button>
  </div>
</div>`;
}

// ── CHANGE 1–4: Performance Stats Panel ──────────────────────────────────────

function calcStats(log) {
  if (!log.length) return null;
  var isWin  = function(r) { return r.exit_reason === "TP1" || r.exit_reason === "TP2"; };
  var isSL   = function(r) { return r.exit_reason === "SL"; };
  var wins   = log.filter(isWin);
  var losses = log.filter(isSL);
  var netPnl     = log.reduce(function(s,r){ return s + (r.pnl_usd||0); }, 0);
  var winRate    = (wins.length / log.length) * 100;
  var avgWin     = wins.length   ? wins.reduce(function(s,r){ return s+(r.pnl_usd||0); },0)/wins.length   : null;
  var avgLoss    = losses.length ? losses.reduce(function(s,r){ return s+(r.pnl_usd||0); },0)/losses.length : null;
  var grossWin   = wins.reduce(function(s,r){ return s+(r.pnl_usd||0); }, 0);
  var grossLoss  = Math.abs(losses.reduce(function(s,r){ return s+(r.pnl_usd||0); }, 0));
  var profitFactor = grossLoss === 0 ? null : grossWin / grossLoss;

  var TIERS = [
    { key: "HIGH PROB", label: "HIGH PROB", color: "#00ff88" },
    { key: "STRONG",    label: "STRONG",    color: "#ffaa00" },
    { key: "REGULAR",   label: "REGULAR",   color: "#ffffff" },
  ];
  var byTier = TIERS.map(function(t) {
    var tt = log.filter(function(r){ return r.tier === t.key; });
    var tw = tt.filter(isWin);
    var avgR = tt.length ? tt.reduce(function(s,r){ return s+(r.r_value||0); },0)/tt.length : 0;
    return { label:t.label, color:t.color, count:tt.length, winRate:tt.length?(tw.length/tt.length)*100:0, avgR:avgR };
  });

  var pairMap = {};
  log.forEach(function(r) {
    if (!pairMap[r.symbol]) pairMap[r.symbol] = { trades:0, wins:0, netPnl:0 };
    pairMap[r.symbol].trades++;
    if (isWin(r)) pairMap[r.symbol].wins++;
    pairMap[r.symbol].netPnl += (r.pnl_usd||0);
  });
  var byPair = Object.entries(pairMap)
    .sort(function(a,b){ return b[1].netPnl - a[1].netPnl; }).slice(0,5)
    .map(function(e){ var sym=e[0],d=e[1]; return { sym:sym, trades:d.trades, wins:d.wins, netPnl:d.netPnl, winRate:(d.wins/d.trades)*100 }; });

  var byDir = ["LONG","SHORT"].map(function(dir) {
    var dt = log.filter(function(r){ return r.direction === dir; });
    var dw = dt.filter(isWin);
    var avgR   = dt.length ? dt.reduce(function(s,r){ return s+(r.r_value||0); },0)/dt.length : 0;
    var netPnl = dt.reduce(function(s,r){ return s+(r.pnl_usd||0); },0);
    return { dir:dir, count:dt.length, winRate:dt.length?(dw.length/dt.length)*100:0, avgR:avgR, netPnl:netPnl };
  });

  var slByTier = TIERS.map(function(t) {
    var tl = losses.filter(function(r){ return r.tier === t.key; });
    return { label:t.label, count:tl.length };
  });
  var worstSL   = losses.length ? Math.min.apply(null, losses.map(function(r){ return r.pnl_usd||0; })) : null;
  var avgSLLoss = losses.length ? losses.reduce(function(s,r){ return s+(r.pnl_usd||0); },0)/losses.length : null;

  return {
    netPnl:netPnl, winRate:winRate, total:log.length,
    longCount:  log.filter(function(r){ return r.direction==="LONG";  }).length,
    shortCount: log.filter(function(r){ return r.direction==="SHORT"; }).length,
    avgWin:avgWin, avgLoss:avgLoss, profitFactor:profitFactor, grossLoss:grossLoss,
    byTier:byTier, byPair:byPair, byDir:byDir,
    slCount:losses.length,
    slRate:(losses.length/log.length)*100,
    avgSLLoss:avgSLLoss, worstSL:worstSL, slByTier:slByTier
  };
}
function renderStatsPanel(log) {
  var el = document.getElementById("stats-panel");
  if (!el) return;
  var collapsed = localStorage.getItem("stats-collapsed") === "1";

  if (!log.length) {
    el.innerHTML = '<div class="stats-empty">NO TRADES YET — stats will appear after first closed trade</div>';
    return;
  }

  var s = calcStats(log);
  if (!s) { el.innerHTML = ""; return; }

  function dollar(v) { return (v >= 0 ? "+" : "") + (v < 0 ? "-" : "") + "$" + Math.abs(v).toFixed(2); }
  function pct(v)    { return v.toFixed(1) + "%"; }
  function rFmt(v)   { return (v >= 0 ? "+" : "") + v.toFixed(2) + "R"; }
  function wrColor(v){ return v >= 60 ? "#00ff88" : v >= 40 ? "#ffaa00" : "#ff4444"; }
  function pnlC(v)   { return v >= 0 ? "#00ff88" : "#ff4444"; }

  var pfStr, pfColor;
  if (s.profitFactor === null) { pfStr = "∞"; pfColor = "#00ff88"; }
  else { pfStr = s.profitFactor.toFixed(2); pfColor = s.profitFactor >= 2 ? "#00ff88" : s.profitFactor >= 1 ? "#ffaa00" : "#ff4444"; }

  function card(label, valHtml, subHtml) {
    return '<div class="stat-card">' +
      '<div class="stat-label">' + label + '</div>' +
      '<div class="stat-value">' + valHtml + '</div>' +
      (subHtml ? '<div class="stat-sub">' + subHtml + '</div>' : "") +
      '</div>';
  }

  var row1 =
    card("NET P&L",      '<span style="color:' + pnlC(s.netPnl)    + '">' + dollar(s.netPnl)  + '</span>') +
    card("WIN RATE",     '<span style="color:' + wrColor(s.winRate)  + '">' + pct(s.winRate)   + '</span>') +
    card("TRADES",       '<span style="color:#fff">' + s.total + '</span>', "LONG " + s.longCount + " / SHORT " + s.shortCount) +
    card("AVG WIN",      s.avgWin  !== null ? '<span style="color:#00ff88">' + dollar(s.avgWin)  + '</span>' : '<span style="color:#444">—</span>') +
    card("AVG LOSS",     s.avgLoss !== null ? '<span style="color:#ff4444">' + dollar(s.avgLoss) + '</span>' : '<span style="color:#444">—</span>') +
    card("PROF FACTOR",  '<span style="color:' + pfColor + '">' + pfStr + '</span>');

  function srow(labelHtml, countHtml, wrHtml, rHtml, pnlHtml) {
    return '<div class="srow">' +
      '<span class="srow-label">' + labelHtml + '</span>' +
      (countHtml ? '<span class="srow-count">' + countHtml + '</span>' : "") +
      (wrHtml    ? '<span class="srow-wr" style="color:' + wrColor(parseFloat(wrHtml)||0) + '">' + wrHtml + '</span>' : "") +
      (rHtml     ? '<span class="srow-r">' + rHtml + '</span>' : "") +
      (pnlHtml   ? '<span class="srow-pnl" style="color:' + pnlC(parseFloat((pnlHtml||"0").replace(/[^\d.-]/g,""))||0) + '">' + pnlHtml + '</span>' : "") +
      '</div>';
  }

  var tierRows = s.byTier.map(function(t) {
    return '<div class="srow">' +
      '<span class="srow-label" style="color:' + t.color + '">' + t.label + '</span>' +
      '<span class="srow-count">' + t.count + '</span>' +
      '<span class="srow-wr" style="color:' + wrColor(t.winRate) + '">' + (t.count ? pct(t.winRate) : "—") + '</span>' +
      '<span class="srow-r">' + (t.count ? rFmt(t.avgR) : "—") + '</span>' +
      '</div>';
  }).join("");

  var pairRows = s.byPair.map(function(p) {
    return '<div class="srow">' +
      '<span class="srow-label" style="color:#fff">' + p.sym.replace("USDT","") + '</span>' +
      '<span class="srow-count">' + p.trades + '</span>' +
      '<span class="srow-wr" style="color:' + wrColor(p.winRate) + '">' + pct(p.winRate) + '</span>' +
      '<span class="srow-pnl" style="color:' + pnlC(p.netPnl) + '">' + dollar(p.netPnl) + '</span>' +
      '</div>';
  }).join("");

  var dirRows = s.byDir.map(function(d) {
    var dc = d.dir === "LONG" ? "#00ff88" : "#ff4444";
    return '<div class="srow">' +
      '<span class="srow-label" style="color:' + dc + '">' + d.dir + '</span>' +
      (d.count === 0
        ? '<span style="color:#333;font-size:8px">NO DATA</span>'
        : '<span class="srow-count">' + d.count + '</span>' +
          '<span class="srow-wr" style="color:' + wrColor(d.winRate) + '">' + pct(d.winRate) + '</span>' +
          '<span class="srow-r">' + rFmt(d.avgR) + '</span>' +
          '<span class="srow-pnl" style="color:' + pnlC(d.netPnl) + '">' + dollar(d.netPnl) + '</span>')
      + '</div>';
  }).join("");

  var slTierStr = s.slByTier.filter(function(t){ return t.count > 0; })
    .map(function(t){ return t.label.split(" ")[0] + " " + t.count; }).join(" · ") || "—";
  var slRows =
    '<div class="srow"><span class="srow-label" style="color:#ff4444">SL HITS</span>' +
    '<span style="color:#ff4444;font-weight:700">' + s.slCount + '</span>' +
    '<span class="srow-wr" style="color:#ff4444">' + pct(s.slRate) + '</span></div>' +
    '<div class="srow"><span class="srow-label">AVG LOSS</span>' +
    '<span style="color:#ff4444">' + (s.avgSLLoss !== null ? dollar(s.avgSLLoss) : "—") + '</span></div>' +
    '<div class="srow"><span class="srow-label">WORST SL</span>' +
    '<span style="color:#ff4444">' + (s.worstSL !== null ? dollar(s.worstSL) : "—") + '</span></div>' +
    '<div class="srow"><span style="font-size:7.5px;color:#444">' + slTierStr + '</span></div>';

  function wide(label, body) {
    return '<div class="stat-card">' + '<div class="stat-label">' + label + '</div>' + body + '</div>';
  }
  var row2 = wide("BY TIER", tierRows) + wide("TOP PAIRS", pairRows) +
             wide("LONG vs SHORT", dirRows) + wide("SL ANALYSIS", slRows);

  var inlineSummary = collapsed
    ? '<span class="stats-header-summary">' +
      '<span style="color:' + pnlC(s.netPnl) + ';font-weight:700">' + dollar(s.netPnl) + '</span>' +
      '<span style="color:#444"> · </span>' +
      '<span style="color:' + wrColor(s.winRate) + '">' + pct(s.winRate) + ' WIN</span>' +
      '</span>'
    : "";
  var chevron = collapsed ? "›" : "‹";
  var chevRot = collapsed ? "0" : "90";

  el.className = "stats-panel";
  el.innerHTML =
    '<div class="stats-header" onclick="toggleStatsPanel()">' +
    '<span class="stats-header-title">PERFORMANCE SUMMARY</span>' +
    inlineSummary +
    '<button class="stats-chevron" style="transform:rotate(' + chevRot + 'deg)">' + chevron + '</button>' +
    '</div>' +
    (collapsed ? "" :
      '<div class="stats-body"><div class="stats-rows-wrap">' +
      '<div class="stats-row">' + row1 + '</div>' +
      '<div class="stats-row">' + row2 + '</div>' +
      '</div></div>');
}

function toggleStatsPanel() {
  var was = localStorage.getItem("stats-collapsed") === "1";
  localStorage.setItem("stats-collapsed", was ? "0" : "1");
  renderStatsPanel(STATE.trade_log || []);
}
// ── Per-trade visual row helpers ──────────────────────────────────────────────
function _exitDotPct(r) {
  var sl = r.sl_price, tp1 = r.tp1_price, exit = r.exit_price;
  if (!sl || !tp1 || !exit) return 50;
  var pct;
  if (r.direction === 'LONG') {
    pct = (exit - sl) / (tp1 - sl) * 100;
  } else {
    pct = (sl - exit) / (sl - tp1) * 100;
  }
  return Math.max(0, Math.min(100, pct));
}

function _tradeVisRow(r) {
  var reason   = r.exit_reason || '';
  var isWin    = reason === 'TP1' || reason === 'TP2';
  var isSL     = reason === 'SL';
  var badgeCls = isWin ? 'tl-badge-win' : isSL ? 'tl-badge-loss' : 'tl-badge-force';
  var badgeTxt = isWin ? 'WIN' : isSL ? 'LOSS' : 'FORCE';
  var rv   = r.r_value || 0;
  var rBg  = rv < 0 ? '#7f1d1d' : rv < 0.5 ? '#1f1f1f' : rv < 1 ? '#78350f' : rv < 2 ? '#365314' : '#14532d';
  var rStr = (rv >= 0 ? '+' : '') + rv.toFixed(1) + 'R';
  var dotPct = _exitDotPct(r);
  var dotBg  = dotPct >= 85 ? '#00ff88' : dotPct <= 15 ? '#ff4444' : '#ffffff';
  return '<tr style="background:#050505;"><td colspan="14" style="padding:0 6px 6px;border-top:none;">' +
    '<div class="tl-vis">' +
      '<span class="tl-badge ' + badgeCls + '">' + badgeTxt + '</span>' +
      '<span class="tl-rpill" style="background:' + rBg + '">' + rStr + '</span>' +
      '<div class="tl-pbar-wrap" title="SL ← exit → TP1">' +
        '<div class="tl-pdot" style="left:' + dotPct.toFixed(1) + '%;background:' + dotBg + '"></div>' +
      '</div>' +
    '</div>' +
  '</td></tr>';
}

function _toggleExpand(id) {
  var el = document.getElementById(id);
  if (el) el.style.display = el.style.display === 'none' ? '' : 'none';
}

// ── Streak calculator ─────────────────────────────────────────────────────────
function _calcStreak(log) {
  if (!log.length) return { type: null, count: 0 };
  var isW = function(r) { return r.exit_reason === 'TP1' || r.exit_reason === 'TP2'; };
  var cur = isW(log[log.length - 1]) ? 'W' : 'L';
  var count = 0;
  for (var i = log.length - 1; i >= 0; i--) {
    if ((isW(log[i]) ? 'W' : 'L') === cur) count++;
    else break;
  }
  return { type: cur, count: count };
}

// ── Performance panel ─────────────────────────────────────────────────────────
function renderPerfPanel(log) {
  var el = document.getElementById('perf-panel');
  if (!el) return;
  if (!log.length) { el.innerHTML = ''; return; }

  var isWin = function(r) { return r.exit_reason === 'TP1' || r.exit_reason === 'TP2'; };
  var isSL  = function(r) { return r.exit_reason === 'SL'; };
  var wins  = log.filter(isWin).length;
  var wr    = wins / log.length * 100;
  var netPnl= log.reduce(function(s,r){ return s + (r.pnl_usd||0); }, 0);
  var stk   = _calcStreak(log);

  var wrBg   = wr >= 60 ? '#14532d' : wr >= 40 ? '#78350f' : '#7f1d1d';
  var pnlBg  = netPnl >= 0 ? '#14532d' : '#7f1d1d';
  var stkBg  = stk.type === 'W' ? '#14532d' : '#7f1d1d';
  var stkStr = stk.count > 0 ? (stk.type + stk.count) : '—';
  var pnlStr = (netPnl >= 0 ? '+' : '') + '$' + Math.abs(netPnl).toFixed(2);

  var last20 = log.slice(-20);
  var segs   = last20.map(function(r) {
    var c = isWin(r) ? '#166534' : isSL(r) ? '#991b1b' : '#92400e';
    var tip = r.symbol + ' ' + (r.exit_reason||'') + ' ' + ((r.pnl_usd||0)>=0?'+':'') + '$' + Math.abs(r.pnl_usd||0).toFixed(2);
    return '<div class="perf-seg" style="background:' + c + '" title="' + tip + '"></div>';
  }).join('');

  var best  = Math.max.apply(null, log.map(function(r){ return r.pnl_usd||0; }));
  var worst = Math.min.apply(null, log.map(function(r){ return r.pnl_usd||0; }));
  var avg   = log.reduce(function(s,r){ return s+(r.pnl_usd||0); }, 0) / log.length;
  var pc    = function(v) { return v >= 0 ? '#00ff88' : '#ff4444'; };
  var dp    = function(v) { return (v>=0?'+':'') + '$' + Math.abs(v).toFixed(2); };
  var wrc   = wr >= 60 ? '#00ff88' : wr >= 40 ? '#ffaa00' : '#ff4444';

  el.innerHTML = '<div class="perf-panel">' +
    '<div class="perf-row1">' +
      '<span class="perf-pill" style="background:' + wrBg + '">' + wr.toFixed(0) + '% WIN</span>' +
      '<span class="perf-pill" style="background:' + pnlBg + '">' + pnlStr + '</span>' +
      '<span class="perf-pill" style="background:#1a1a1a;border:1px solid #2a2a2a">' + log.length + ' TRADES</span>' +
      '<span class="perf-pill" style="background:' + stkBg + '">' + stkStr + '</span>' +
    '</div>' +
    '<div class="perf-bar-lbl">LAST ' + last20.length + ' TRADES — OLDEST LEFT · NEWEST RIGHT</div>' +
    '<div class="perf-bar-wrap">' + segs + '</div>' +
    '<div class="perf-at">' +
      '<div class="perf-at-item"><span class="perf-at-label">TOTAL</span><span class="perf-at-val" style="color:#fff">' + log.length + '</span></div>' +
      '<div class="perf-at-item"><span class="perf-at-label">WIN RATE</span><span class="perf-at-val" style="color:' + wrc + '">' + wr.toFixed(1) + '%</span></div>' +
      '<div class="perf-at-item"><span class="perf-at-label">BEST</span><span class="perf-at-val" style="color:' + pc(best) + '">' + dp(best) + '</span></div>' +
      '<div class="perf-at-item"><span class="perf-at-label">WORST</span><span class="perf-at-val" style="color:' + pc(worst) + '">' + dp(worst) + '</span></div>' +
      '<div class="perf-at-item"><span class="perf-at-label">AVG PNL</span><span class="perf-at-val" style="color:' + pc(avg) + '">' + dp(avg) + '</span></div>' +
    '</div>' +
  '</div>';
}

// ── Session breakdown ───────────────────────────────────────────────────────
function renderSessionPanel(log) {
  var el = document.getElementById('session-panel');
  if (!el) return;
  if (!log.length) { el.innerHTML = ''; return; }
  var sessions = ['ASIA', 'EU', 'US', 'OFF'];
  var isWin = function(r) {
    return r.exit_reason === 'TP1' || r.exit_reason === 'TP2' ||
           r.exit_reason === 'TRAILBLAZER' || r.exit_reason === 'HC_PARTIAL_1.5R';
  };
  var isSL  = function(r) { return r.exit_reason === 'SL'; };
  var cols  = { ASIA: '#4488ff', EU: '#00ff88', US: '#ff8800', OFF: '#888' };
  var rows = sessions.map(function(sess) {
    var trades = log.filter(function(r) { return r.session_opened === sess; });
    if (!trades.length) return null;
    var wins      = trades.filter(isWin);
    var losses    = trades.filter(isSL);
    var wr        = (wins.length / trades.length * 100);
    var netPnl    = trades.reduce(function(s,r){ return s+(r.pnl_usd||0); }, 0);
    var grossWin  = wins.reduce(function(s,r){ return s+(r.pnl_usd||0); }, 0);
    var grossLoss = Math.abs(losses.reduce(function(s,r){ return s+(r.pnl_usd||0); }, 0));
    var pf        = grossLoss === 0 ? null : grossWin / grossLoss;
    var avgR      = trades.reduce(function(s,r){ return s+(r.r_value||0); }, 0) / trades.length;
    var wrCol     = wr >= 60 ? '#00ff88' : wr >= 40 ? '#ffaa00' : '#ff4444';
    var pnlCol    = netPnl >= 0 ? '#00ff88' : '#ff4444';
    var pfStr     = pf === null ? '∞' : pf.toFixed(2);
    var pfCol     = (pf === null || pf >= 2) ? '#00ff88' : pf >= 1 ? '#ffaa00' : '#ff4444';
    var avgRCol   = avgR >= 0 ? '#00ff88' : '#ff4444';
    return '<div class="srow">' +
      '<span class="srow-label" style="color:' + (cols[sess]||'#888') + ';font-weight:700">' + sess + '</span>' +
      '<span class="srow-count">' + trades.length + '</span>' +
      '<span class="srow-wr" style="color:' + wrCol + '">' + wr.toFixed(1) + '%</span>' +
      '<span class="srow-r" style="color:' + pfCol + '">' + pfStr + '</span>' +
      '<span class="srow-pnl" style="color:' + pnlCol + '">' + (netPnl>=0?'+':'')+'$'+Math.abs(netPnl).toFixed(2) + '</span>' +
      '<span class="srow-r" style="color:' + avgRCol + '">' + (avgR>=0?'+':'')+avgR.toFixed(2)+'R</span>' +
      '</div>';
  }).filter(Boolean);
  if (!rows.length) { el.innerHTML = ''; return; }
  var hdr = '<div class="srow" style="opacity:0.45;font-size:9px">' +
    '<span class="srow-label">SESSION</span>' +
    '<span class="srow-count">N</span>' +
    '<span class="srow-wr">WR%</span>' +
    '<span class="srow-r">PF</span>' +
    '<span class="srow-pnl">P&L</span>' +
    '<span class="srow-r">AVG R</span>' +
    '</div>';
  var _hasMae = log.filter(function(r) { return r.mae_r != null; });
  var _excLine = '';
  if (_hasMae.length) {
    var _isWin2 = function(r) { return r.exit_reason==='TP1'||r.exit_reason==='TP2'||r.exit_reason==='TRAILBLAZER'; };
    var _isSL2  = function(r) { return r.exit_reason==='SL'; };
    var _wMae = _hasMae.filter(_isWin2);
    var _lMae = _hasMae.filter(_isSL2);
    var _wMfe = _hasMae.filter(function(r){ return r.mfe_r!=null; }).filter(_isWin2);
    var _avg  = function(arr,f){ return arr.length ? arr.reduce(function(s,r){return s+(+r[f]||0);},0)/arr.length : null; };
    var _fR   = function(v){ return v==null?'—':(v>=0?'+':'')+v.toFixed(1)+'R'; };
    _excLine = '<div style="font-size:9px;color:#555;font-family:\'JetBrains Mono\',monospace;padding:5px 8px 3px;letter-spacing:0.4px">' +
      'EXCURSION &nbsp;&nbsp; winners avg MAE <span style="color:#ff8800">' + _fR(_avg(_wMae,'mae_r')) + '</span>' +
      ' · losers avg MAE <span style="color:#ff4444">' + _fR(_avg(_lMae,'mae_r')) + '</span>' +
      ' · winners avg MFE <span style="color:#00ff88">' + _fR(_avg(_wMfe,'mfe_r')) + '</span>' +
      '</div>';
  }
  el.innerHTML =
    '<div class="stat-card" style="margin:6px 0 0">' +
    '<div class="stat-label">BY SESSION</div>' +
    hdr + rows.join('') +
    '</div>' + _excLine;
}

// ── Log tab ───────────────────────────────────────────────────────────────────
// ── Date-filter helpers ─────────────────────────────────────────────────────────────────────────────
function _localDateStr(d) {
  return d.getFullYear() + '-' +
    String(d.getMonth()+1).padStart(2,'0') + '-' +
    String(d.getDate()).padStart(2,'0');
}
function _getYesterday() {
  const d = new Date(); d.setDate(d.getDate()-1); return _localDateStr(d);
}
function _getDateFilter() {
  const fromEl = document.getElementById('log-date-from');
  const toEl   = document.getElementById('log-date-to');
  if (fromEl && !fromEl.dataset.init) {
    const yd = _getYesterday();
    fromEl.value = yd; fromEl.dataset.init = '1';
    toEl.value   = yd; toEl.dataset.init   = '1';
  }
  const fromStr = fromEl ? fromEl.value : '';
  const toStr   = toEl   ? toEl.value   : '';
  const fromMs  = fromStr ? new Date(fromStr + 'T00:00:00').getTime() : null;
  const toMs    = toStr   ? new Date(toStr   + 'T23:59:59').getTime() : null;
  const fromTs  = fromMs ? Math.floor(fromMs / 1000) : null;
  const toTs    = toMs   ? Math.floor(toMs   / 1000) : null;
  return { fromMs, toMs, fromTs, toTs, fromStr, toStr };
}

function renderLogTab() {
  const log = STATE.trade_log || [];
  const { fromMs, toMs } = _getDateFilter();

  const filtered = log.filter(r => {
    const ts = (r.timestamp_closed || 0) * 1000;
    if (fromMs !== null && ts < fromMs) return false;
    if (toMs   !== null && ts > toMs)   return false;
    return true;
  });

  const countTxt = filtered.length === log.length
    ? `${log.length} trade${log.length!==1?'s':''}`
    : `${filtered.length} of ${log.length} trade${log.length!==1?'s':''}`;
  document.getElementById('log-count').textContent = countTxt;
  renderStatsPanel(log);
  renderPerfPanel(log);
  renderSessionPanel(filtered);

  if (!filtered.length) {
    document.getElementById('log-body').className = 'log-empty';
    document.getElementById('log-body').innerHTML = log.length
      ? 'No trades in selected date range'
      : 'No closed trades yet';
    return;
  }

  const rows = [...filtered].reverse().map(r => {
    const reasonCls = r.exit_reason === 'TP1'         ? 'reason-tp1'
                    : r.exit_reason === 'TP2'         ? 'reason-tp2'
                    : r.exit_reason === 'TRAILBLAZER' ? 'reason-tp2'
                    : r.exit_reason === 'SL'          ? 'reason-sl' : 'reason-manual';
    const reasonLbl = r.exit_reason === 'TRAILBLAZER' ? '🏃 TRAILBLAZER' : (r.exit_reason || '—');
    const pnlColor = (r.pnl_usd||0) >= 0 ? '#00ff88' : '#ff4444';
    const rColor   = (r.r_value||0) >= 0 ? '#555'    : '#ff4444';
    const dur      = r.duration_seconds || 0;
    const durStr   = dur < 3600 ? `${Math.floor(dur/60)}m` : `${Math.floor(dur/3600)}h${Math.floor((dur%3600)/60)}m`;
    const openTime = r.timestamp_opened ? new Date(r.timestamp_opened*1000).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'}) : '—';
    const closeTime= r.timestamp_closed ? new Date(r.timestamp_closed*1000).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'}) : '—';
    const isLong   = r.direction === 'LONG';
    const _sessC = { ASIA: '#9966ff', EU: '#4488ff', US: '#00ff88', OFF: '#888' };
    const _sCol  = _sessC[r.session_opened] || '#555';
    const _spill = r.session_opened
      ? `<span style="font-size:8px;font-weight:700;color:${_sCol};background:${_sCol}22;border-radius:3px;padding:1px 4px;margin-top:2px;display:block;letter-spacing:0.5px">${r.session_opened}</span>`
      : '';
    const _maeV = r.mae_r != null ? (+r.mae_r).toFixed(1) : null;
    const _mfeV = r.mfe_r != null ? ((+r.mfe_r >= 0 ? '+' : '') + (+r.mfe_r).toFixed(1)) : null;
    const _excl = (_maeV !== null || _mfeV !== null)
      ? `<span style="font-size:8px;color:#444;display:block;margin-top:1px">${_maeV !== null ? _maeV : '—'}/${_mfeV !== null ? _mfeV : '—'}</span>`
      : '';
    const _rid  = 'tlr-' + (r.timestamp_opened||0) + '-' + (r.timestamp_closed||0);
    const _nd   = v => v != null ? (+v).toFixed(1) : '—';
    const _expR = `<tr id="${_rid}" style="display:none;background:#050505"><td colspan="14" style="padding:2px 14px 8px;border-top:none;font-family:'JetBrains Mono',monospace;font-size:10px;color:#666;letter-spacing:0.3px">J ${_nd(r.j15m_entry)} · K/D ${_nd(r.stoch_k_entry)}/${_nd(r.stoch_d_entry)} · RSI ${_nd(r.rsi_entry)} · depth ${_nd(r.depth_pct_entry)}% · 24h ${_nd(r.chg24h_entry)}% · MAE ${_nd(r.mae_r)}R · MFE ${_nd(r.mfe_r)}R</td></tr>`;
    return `<tr onclick="_toggleExpand('${_rid}')" style="cursor:pointer">
      <td style="font-weight:700;font-size:12px;">${r.symbol}${_spill}</td>
      <td style="color:${isLong?'#00ff88':'#ff4444'};font-weight:700;">${r.direction}</td>
      <td style="color:#fff;">${r.tier||'—'}</td>
      <td style="color:#fff;">${r.leverage||'—'}x</td>
      <td>${fmtPrice(r.entry_price)}</td>
      <td>${fmtPrice(r.exit_price)}</td>
      <td style="color:#ff4444;">${fmtPrice(r.sl_price)}</td>
      <td style="color:#00ff88;">${fmtPrice(r.tp1_price)}</td>
      <td class="${reasonCls}">${reasonLbl}</td>
      <td style="color:${pnlColor};font-weight:700;">${(r.pnl_usd||0)>=0?'+':''}${(r.pnl_usd||0).toFixed(2)}</td>
      <td style="color:${rColor};font-weight:700;">${(r.r_value||0)>=0?'+':''}${(r.r_value||0).toFixed(2)}R${_excl}</td>
      <td style="color:#555;">${openTime}</td>
      <td style="color:#555;">${closeTime}</td>
      <td style="color:#555;">${durStr}</td>
    </tr>` + _tradeVisRow(r) + _expR;
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
  const log = STATE.trade_log || [];
  const { fromMs, toMs, fromStr, toStr } = _getDateFilter();
  const filtered = log.filter(r => {
    const ts = (r.timestamp_closed || 0) * 1000;
    if (fromMs !== null && ts < fromMs) return false;
    if (toMs   !== null && ts > toMs)   return false;
    return true;
  });
  const FIELDS = [
    'timestamp_opened','timestamp_closed','symbol','direction','tier','leverage',
    'entry_price','sl_price','tp1_price','tp2_price','exit_price','exit_reason',
    'pnl_usd','r_value','duration_seconds','exchange','paper','score','adx1h',
    'session_opened','j15m_entry','j1h_entry','stoch_k_entry','stoch_d_entry',
    'rsi_entry','depth_pct_entry','chg24h_entry','mae_r','mfe_r',
  ];
  const csvRows = [[...FIELDS, 'duration_min'].join(',')].concat(
    filtered.map(r => {
      const base   = FIELDS.map(f => JSON.stringify(r[f] ?? ''));
      const durMin = r.duration_seconds != null ? JSON.stringify((r.duration_seconds/60).toFixed(1)) : '""';
      return [...base, durMin].join(',');
    })
  );
  const blob = new Blob([csvRows.join('\n')], { type: 'text/csv' });
  const a    = document.createElement('a');
  a.href     = URL.createObjectURL(blob);
  a.download = `trade_log_${fromStr||'all'}_to_${toStr||'all'}.csv`;
  a.click();
  URL.revokeObjectURL(a.href);
}

async function clearLog() {
  const { fromTs, toTs, fromStr, toStr } = _getDateFilter();
  const rangeLabel = (fromStr && toStr) ? `${fromStr} to ${toStr}` : 'all dates';
  if (!confirm(`Clear trade log entries for ${rangeLabel}?`)) return;
  try {
    let url = '/api/tradelog';
    if (fromTs !== null && toTs !== null) url += `?from_ts=${fromTs}&to_ts=${toTs}`;
    const r = await fetch(url, { method: 'DELETE' });
    if (!r.ok) { alert('Clear failed'); return; }
    fetchState();
  } catch (e) { alert('Request failed'); }
}

// ── Pair Symbol Overlay ───────────────────────────────────────────────────────
let _ovPollId    = null;
let _ovPrevGates = null;

// ── BTC Regime helpers ────────────────────────────────────────────────────────
function _btcRegime(btc) {
  if (!btc) return { state:'EXEMPT', cls:'exempt', color:'#fff', label:'⚪ EXEMPT' };
  const j1h = btc.j1h || 0;
  if (j1h < 20)  return { state:'CONFIRMED_LONG',  cls:'confirmed', color:'#00e676', label:'✅ CONFIRMED' };
  if (j1h < 40)  return { state:'CAUTION_LONG',    cls:'caution',   color:'#ffb300', label:'⚠️ CAUTION'  };
  if (j1h <= 60) return { state:'STOP',            cls:'stop',      color:'#ff4646', label:'🚫 STOP'     };
  if (j1h < 80)  return { state:'CAUTION_SHORT',   cls:'caution',   color:'#ffb300', label:'⚠️ CAUTION'  };
  return           { state:'CONFIRMED_SHORT',  cls:'confirmed', color:'#ff4646', label:'✅ SHORT SAFE' };
}

function _btcRegimeCardHtml(sym, btc, regime, corr) {
    const j15m   = Math.min(100, Math.max(0, btc?.j15m    || 0));
    const j1h    = Math.min(100, Math.max(0, btc?.j1h     || 0));
    const stochK = Math.min(100, Math.max(0, btc?.stoch_k || 0));
    const stochD = Math.min(100, Math.max(0, btc?.stoch_d || 0));
    const adx    = btc?.adx1h || btc?.adx || 0;
    const price  = btc?.price  || 0;
    const isExempt = corr < 0.65;
    const cls    = isExempt ? 'exempt'   : regime.cls;
    const color  = isExempt ? '#fff'     : regime.color;
    const state  = isExempt ? 'EXEMPT'   : regime.state;

    const j1hGlow = cls === 'confirmed' ? '0 0 20px rgba(0,230,118,0.5),0 0 40px rgba(0,230,118,0.2)'
                  : cls === 'caution'   ? '0 0 20px rgba(255,179,0,0.5),0 0 40px rgba(255,179,0,0.2)'
                  : cls === 'stop'      ? '0 0 20px rgba(255,70,70,0.5),0 0 40px rgba(255,70,70,0.2)'
                  :                       'none';

    const heroBg  = cls === 'confirmed' ? '#0a1a0a'
                  : cls === 'caution'   ? '#1a1200'
                  : cls === 'stop'      ? '#1a0808'
                  :                       '#0d0d0d';
    const heroBor = cls === 'confirmed' ? '1px solid #00e67644'
                  : cls === 'caution'   ? '1px solid #ffb30044'
                  : cls === 'stop'      ? '1px solid #ff525244'
                  :                       '1px solid #2a2a2a';

    const stateLabel = state === 'CONFIRMED_LONG'  ? '✅ LONG SAFE ZONE'
                     : state === 'CAUTION_LONG'    ? '⚠️ CAUTION ZONE'
                     : state === 'STOP'            ? '🚫 STOP ZONE'
                     : state === 'CAUTION_SHORT'   ? '⚠️ CAUTION ZONE'
                     : state === 'CONFIRMED_SHORT' ? '✅ SHORT SAFE ZONE'
                     :                              '⚪ NOT APPLIED';

    const threshNote = state === 'CONFIRMED_LONG'  ? 'below 20 threshold'
                     : state === 'CAUTION_LONG'    ? 'below 40 threshold'
                     : state === 'STOP'            ? 'in 40–60 stop zone'
                     : state === 'CAUTION_SHORT'   ? 'above 60 threshold'
                     : state === 'CONFIRMED_SHORT' ? 'above 80 threshold'
                     :                              'exempt';

    const kAboveD   = stochK > stochD;
    const stochLine = 'K=' + stochK.toFixed(0) + (kAboveD ? ' above' : ' below') + ' D=' + stochD.toFixed(0) + (kAboveD ? ' ↑' : ' ↓');

    let narrative = '';
    if      (state === 'CONFIRMED_LONG')  narrative = 'BTC is deeply oversold on the hourly and momentum has turned up — the market is in bounce territory and longs have a green light from the regime.';
    else if (state === 'CAUTION_LONG')    narrative = 'BTC hourly is between oversold and neutral — bounce possible but not confirmed yet. ' + sym + ' pair gates are ready. Wait for BTC J1H to drop below 20 for full conviction, or enter knowing the risk.';
    else if (state === 'STOP')            narrative = "BTC is in no-man's land — not oversold enough to bounce, momentum falling. Every long entered in this regime this week hit its stop loss. Wait for J1H to drop below 20.";
    else if (state === 'CAUTION_SHORT')   narrative = 'BTC hourly is between overbought and neutral on the short side — approaching but not confirmed. Wait for J1H above 80 for full conviction.';
    else if (state === 'CONFIRMED_SHORT') narrative = 'BTC is deeply overbought on the hourly — the market is extended and shorts have a green light from the regime.';
    else                                  narrative = 'BTC regime does not apply to ' + sym + '. Correlation ' + corr.toFixed(2) + ' is below the 0.65 threshold — this pair moves on independent catalysts.';

    const cursorPct = Math.min(99.5, Math.max(0.5, j1h)).toFixed(1);
    const j15mCol   = j15m > 80 ? '#ff4646' : j15m < 20 ? '#00e676' : '#fff';
    const j15mSub   = j15m > 80 ? 'overbought ST' : j15m < 20 ? 'oversold ST ✅' : 'neutral';
    const adxCol    = adx >= 25 ? '#ffb300' : '#fff';
    const adxSub    = adx >= 40 ? 'strong trend' : adx >= 25 ? 'moderate' : 'weak';
    const stochCol  = kAboveD ? '#00e676' : '#ff4646';

    let stochPillBg = '#222', stochPillCol = '#888', stochPillTxt = 'not confirmed';
    if      (kAboveD  && stochK < 25) { stochPillBg = '#00e67622'; stochPillCol = '#00e676'; stochPillTxt = 'K▶D ✅ BULLISH'; }
    else if (!kAboveD && stochK > 75) { stochPillBg = '#ff464622'; stochPillCol = '#ff4646'; stochPillTxt = 'K▼D ❌ BEARISH'; }
    else                               { stochPillBg = '#222';      stochPillCol = '#888';    stochPillTxt = kAboveD ? 'K▶D not in zone' : 'K▼D not confirmed'; }

    const footerNote = state === 'CONFIRMED_LONG'  ? '~78% WR in this zone'
                     : state === 'CAUTION_LONG'    ? '~42% WR · your discretion'
                     : state === 'STOP'            ? '89% SL rate · wait for J1H <20'
                     : state === 'CONFIRMED_SHORT' ? '~78% WR in this zone'
                     : state === 'CAUTION_SHORT'   ? '~42% WR · your discretion'
                     :                              'independent catalysts · no gate';
    const gateDesc  = corr >= 0.75 ? 'regime gate' : corr >= 0.65 ? 'advisory only' : 'no gate';

    const p = [];
    // C) HEADER
    p.push('<div style="padding:10px 12px 8px;border-bottom:1px solid #1a1a1a;display:flex;justify-content:space-between;align-items:center">');
    p.push('<div>');
    p.push('<div style="font-family:\'Bebas Neue\',sans-serif;font-size:20px;color:' + color + ';letter-spacing:0.04em">BTC REGIME</div>');
    p.push('<div style="font-family:\'JetBrains Mono\',monospace;font-size:8px;color:#fff;font-weight:700;margin-top:2px">' + fmtPrice(price) + ' · ADX ' + adx.toFixed(0) + ' · ' + sym + ' corr ' + corr.toFixed(2) + '</div>');
    p.push('</div>');
    p.push('<span style="font-size:9px;font-weight:700;padding:2px 8px;border-radius:3px;border:1px solid ' + color + '66;color:' + color + ';font-family:\'JetBrains Mono\',monospace;background:' + color + '11">' + (isExempt ? '⚪ EXEMPT' : regime.label) + '</span>');
    p.push('</div>');
    // D) HERO
    p.push('<div style="border-radius:6px;padding:14px 14px 10px;margin:8px 12px 0;background:' + heroBg + ';border:' + heroBor + '">');
    p.push('<div style="font-size:8px;font-weight:700;color:#fff;letter-spacing:0.1em;margin-bottom:4px">BTC J 1H — KEY GATE</div>');
    p.push('<div style="display:flex;align-items:flex-end;gap:10px">');
    p.push('<div style="font-family:\'Bebas Neue\',sans-serif;font-size:56px;line-height:1;color:' + color + ';text-shadow:' + j1hGlow + '">' + j1h.toFixed(0) + '</div>');
    p.push('<div style="display:flex;flex-direction:column;gap:4px;padding-bottom:4px">');
    p.push('<div style="font-size:11px;font-weight:700;color:' + color + '">' + stateLabel + '</div>');
    p.push('<div style="font-size:8px;font-weight:700;color:#fff">' + threshNote + '</div>');
    p.push('<div style="font-size:8px;font-weight:700;color:#fff">' + stochLine + '</div>');
    p.push('</div></div>');
    p.push('<div style="border-top:1px solid ' + color + '33;padding-top:8px;margin-top:8px;font-size:11px;font-weight:700;color:#fff;line-height:1.6">' + narrative + '</div>');
    p.push('</div>');
    // E) THRESHOLD BAR
    p.push('<div style="margin:8px 12px 0;border-radius:6px;padding:10px 12px;background:' + heroBg + ';border:' + heroBor + '">');
    p.push('<div style="display:flex;justify-content:space-between;font-family:\'JetBrains Mono\',monospace;font-size:8px;font-weight:700;margin-bottom:4px"><span style="color:#fff">J1H POSITION ON SCALE</span><span style="color:' + color + '">' + j1h.toFixed(0) + ' of 100</span></div>');
    p.push('<div style="display:flex;justify-content:space-between;font-size:7px;font-weight:700;color:#fff;font-family:\'JetBrains Mono\',monospace;margin-bottom:2px"><span>0</span><span>20</span><span>40</span><span>60</span><span>80</span><span>100</span></div>');
    p.push('<div class="tbar-wrap"><div class="tbar"><div class="tz-safe">0–20</div><div class="tz-caut">20–40</div><div class="tz-stop">40–60</div><div class="tz-caut2">60–80</div><div class="tz-safe2">80–100</div></div><div class="tcursor ' + cls + '" style="left:' + cursorPct + '%"></div></div>');
    p.push('<div style="display:flex;justify-content:space-between;font-family:\'JetBrains Mono\',monospace;font-size:7px;font-weight:700;margin-top:4px"><span style="color:#00e676">&lt;20 LONG SAFE</span><span style="color:#ffb300">20–40 CAUTION</span><span style="color:#ff4646">40–60 STOP</span><span style="color:#ffb300">60–80 CAUTION</span><span style="color:#ff4646">&gt;80 SHORT SAFE</span></div>');
    p.push('</div>');
    // F) SUPPORT METRICS
    p.push('<div style="display:flex;gap:6px;margin:8px 12px 0">');
    p.push('<div style="flex:1;background:' + heroBg + ';border:' + heroBor + ';border-radius:6px;padding:8px;text-align:center">');
    p.push('<div style="font-family:\'JetBrains Mono\',monospace;font-size:8px;font-weight:700;color:#fff;margin-bottom:4px">BTC J 15M</div>');
    p.push('<div style="font-family:\'Bebas Neue\',sans-serif;font-size:28px;line-height:1;color:' + j15mCol + '">' + j15m.toFixed(0) + '</div>');
    p.push('<div style="font-family:\'JetBrains Mono\',monospace;font-size:7px;font-weight:700;color:#fff;margin-top:3px">' + j15mSub + '</div>');
    p.push('</div>');
    p.push('<div style="flex:1;background:' + heroBg + ';border:' + heroBor + ';border-radius:6px;padding:8px;text-align:center">');
    p.push('<div style="font-family:\'JetBrains Mono\',monospace;font-size:8px;font-weight:700;color:#fff;margin-bottom:4px">BTC STOCH K/D</div>');
    p.push('<div style="font-family:\'Bebas Neue\',sans-serif;font-size:22px;line-height:1;color:' + stochCol + '">' + stochK.toFixed(0) + '/' + stochD.toFixed(0) + '</div>');
    p.push('<div style="margin-top:4px;font-family:\'JetBrains Mono\',monospace;font-size:7px;font-weight:700;padding:2px 4px;border-radius:3px;display:inline-block;color:' + stochPillCol + ';background:' + stochPillBg + ';border:1px solid ' + stochPillCol + '44">' + stochPillTxt + '</div>');
    p.push('</div>');
    p.push('<div style="flex:1;background:' + heroBg + ';border:' + heroBor + ';border-radius:6px;padding:8px;text-align:center">');
    p.push('<div style="font-family:\'JetBrains Mono\',monospace;font-size:8px;font-weight:700;color:#fff;margin-bottom:4px">BTC ADX</div>');
    p.push('<div style="font-family:\'Bebas Neue\',sans-serif;font-size:28px;line-height:1;color:' + adxCol + '">' + adx.toFixed(0) + '</div>');
    p.push('<div style="font-family:\'JetBrains Mono\',monospace;font-size:7px;font-weight:700;color:#fff;margin-top:3px">' + adxSub + '</div>');
    p.push('</div></div>');
    // G) LIVE BTC ROW
    p.push('<div style="display:flex;align-items:center;gap:8px;margin:8px 12px 0;font-family:\'JetBrains Mono\',monospace;font-size:8px;font-weight:700;flex-wrap:wrap">');
    p.push('<span style="background:#1a1200;border:1px solid #ffb30066;color:#ffb300;font-size:7px;padding:2px 6px;border-radius:3px;flex-shrink:0">LIVE BTC</span>');
    p.push('<span style="color:#fff">PRICE <span style="color:#fff">' + fmtPrice(price) + '</span></span>');
    p.push('<span style="color:#fff">J15M <span style="color:' + j15mCol + '">' + j15m.toFixed(0) + '</span></span>');
    p.push('<span style="color:#fff">J1H <span style="color:' + color + '">' + j1h.toFixed(0) + '</span></span>');
    p.push('<span style="color:#fff">K/D <span style="color:' + stochCol + '">' + stochK.toFixed(0) + '/' + stochD.toFixed(0) + '</span></span>');
    p.push('</div>');
    // H) SPACER
    p.push('<div style="flex:1"></div>');
    // I) FOOTER
    p.push('<div style="padding:8px 12px;border-top:1px solid #1a1a1a;font-family:\'JetBrains Mono\',monospace;font-size:8px;font-weight:700;color:#666;text-align:right">');
    p.push('corr ' + corr.toFixed(2) + ' · ' + gateDesc + ' · ' + footerNote);
    p.push('</div>');

    return p.join('');
  }

function openPairOverlay(sym) {
  if (document.getElementById('pair-ov-bd')) return;
  const bd = document.createElement('div');
  bd.id = 'pair-ov-bd';
  bd.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,0.92);backdrop-filter:blur(8px);display:flex;align-items:stretch;justify-content:center;gap:12px;padding:20px;z-index:9000';
  bd.addEventListener('click', e => { if (e.target === bd) closePairOverlay(); });
  const pn = document.createElement('div');
  pn.id = 'pair-ov-pn';
  pn.style.cssText = 'flex:1;min-width:0';
  pn.dataset.sym   = sym;
  pn.dataset.state = '';
  pn.innerHTML = `<div class="pov-loading">Loading ${sym}…</div>`;
  bd.appendChild(pn);
  const _btcForRegime = (STATE?.pair_states||[]).find(p => p.symbol==='BTC');
  const _corrVal = BTC_CORRELATION[sym] ?? 0.75;
  const _regimeResult = sym === 'BTC' ? null : _btcRegime(_btcForRegime);
  const _showRegime = sym !== 'BTC';
  if (_showRegime) {
    const rn = document.createElement('div');
    rn.id = 'btc-regime-pn';
    rn.style.cssText = 'flex:1;min-width:0';
    const _regimeCorr = BTC_CORRELATION[sym] ?? 0.75;
    const _exemptState = _regimeCorr < 0.65;
    rn.className = _exemptState ? 'exempt' : (_regimeResult?.cls || 'exempt');
    rn.innerHTML = _btcRegimeCardHtml(sym, _btcForRegime, _exemptState ? {state:'EXEMPT',cls:'exempt',color:'#fff',label:'⚪ EXEMPT'} : _regimeResult, _regimeCorr);
    bd.appendChild(rn);
  }
  document.body.appendChild(bd);
  _ovPrevGates = null;
  _ovFetch(sym, true);
  _ovPollId = setInterval(() => _ovFetch(sym, false), 2000);
}

function closePairOverlay() {
  clearInterval(_ovPollId);
  _ovPollId    = null;
  _ovPrevGates = null;
  const bd = document.getElementById('pair-ov-bd');
  if (bd) bd.remove();
}

async function _ovFetch(sym, isFirst) {
  try {
    const r = await fetch(`/api/pair/${encodeURIComponent(sym)}`);
    if (!r.ok) return;
    const d = await r.json();
    const pn = document.getElementById('pair-ov-pn');
    if (!pn) return;
    isFirst ? _ovRender(pn, d) : _ovUpdate(pn, d);
  } catch (e) { /* network blip */ }
}

// ── State helpers ─────────────────────────────────────────────────────────────
  function _ovState(d) {
    if (d.in_trade_long || d.in_trade_short) return 'IN_TRADE';
    if (d.alert && d.alert_state !== 'STALE')  return 'READY';
    const score = Math.max(d.score_long || 0, d.score_short || 0);
    if (score === 3) return 'NEAR';
    if (score >= 1)  return 'SCANNING';
    return 'WATCHING';
  }
  function _ovDir(d) {
    if (d.in_trade_long)    return 'LONG';
    if (d.in_trade_short)   return 'SHORT';
    if (d.alert)            return d.alert.direction;
    if (d.confluence_long)  return 'LONG';
    if (d.confluence_short) return 'SHORT';
    if ((d.score_long || 0)  > (d.score_short || 0)) return 'LONG';
    if ((d.score_short || 0) > (d.score_long  || 0)) return 'SHORT';
    return 'LONG';
  }
  function _ovGates(d, dir) { return dir === 'SHORT' ? d.gate_short : d.gate_long; }
  function _ovBorderCol(state, trend) {
    if (state === 'IN_TRADE')                                   return 'rgba(100,160,255,0.5)';
    if (trend === 'Strong Bull' || trend === 'Bullish')         return 'rgba(0,230,118,0.5)';
    if (trend === 'Strong Bear' || trend === 'Bearish')         return 'rgba(255,61,87,0.5)';
    return '#222';
  }
  function _ovSymCol(state, trend) {
    if (state === 'IN_TRADE')                                   return '#66aaff';
    if (trend === 'Strong Bull' || trend === 'Bullish')         return '#00e676';
    if (trend === 'Strong Bear' || trend === 'Bearish')         return '#ff3d57';
    return '#aaa';
  }

  // ── HTML builders ─────────────────────────────────────────────────────────────
  function _ovStatePillHtml(state, dir) {
    const labels = { IN_TRADE:'IN TRADE', READY:'READY', NEAR:'NEAR', SCANNING:'SCANNING', WATCHING:'WATCHING' };
    const styles = {
      IN_TRADE: 'background:rgba(100,160,255,0.2);color:#66aaff;border:1px solid rgba(100,160,255,0.4)',
      READY:    (dir === 'LONG'
                  ? 'background:rgba(0,230,118,0.15);color:#00e676;border:1px solid rgba(0,230,118,0.4)'
                  : 'background:rgba(255,61,61,0.15);color:#ff3d3d;border:1px solid rgba(255,61,61,0.4)'),
      NEAR:     'background:rgba(255,179,0,0.15);color:#ffb300;border:1px solid rgba(255,179,0,0.4)',
      SCANNING: 'background:rgba(136,136,136,0.1);color:#888;border:1px solid #333',
      WATCHING: 'background:transparent;color:#555;border:1px solid #2a2a2a',
    };
    const label = labels[state] || state;
    const style = styles[state] || styles.WATCHING;
    return `<span style="font-family:'JetBrains Mono',monospace;font-size:10px;font-weight:700;padding:2px 8px;border-radius:3px;letter-spacing:0.06em;${style}">${label}</span>`;
  }

  // ── New gate builders ─────────────────────────────────────────────────────────
  function _ovPassIcon(pass) {
    return pass
      ? '<span style="color:#00e676;font-size:14px;line-height:1">\u2705</span>'
      : '<span style="color:#ff5252;font-size:14px;line-height:1">\u274c</span>';
  }

  function _ovGateLabelHtml(name) {
    return `<span style="font-size:11px;font-weight:700;color:#ffffff;font-family:'JetBrains Mono',monospace;letter-spacing:0.08em">${name}</span>`;
  }

  function _ovVerdictHtml(d, dir) {
    const isL   = dir !== 'SHORT';
    const gates = (isL ? d.gate_long : d.gate_short) || [false, false, false, false];
    const score = gates.filter(Boolean).length;
    const names = ['J 15M', 'J 1H', 'STOCH K/D', isL ? 'BID DEPTH' : 'ASK DEPTH'];
    const failing = names.filter((_, i) => !gates[i]);
    const base = "padding:7px 16px;font-family:'JetBrains Mono',monospace;font-size:11px;font-weight:700;letter-spacing:0.08em;text-align:center";
    const sym = d.symbol;
    const _btcV = (STATE?.pair_states||[]).find(p=>p.symbol==='BTC');
    const _rgV = sym==='BTC'||!_btcV ? null : _btcRegime(_btcV);
    const _corrV = BTC_CORRELATION[sym]??0.75;
    const _exemptV = _corrV < 0.65;
    if (_rgV?.state==='STOP' && _corrV>=0.75 && isL)
      return `<div id="pov-verdict" style="${base};background:rgba(255,82,82,0.12);border-top:1px solid rgba(255,82,82,0.2);border-bottom:1px solid rgba(255,82,82,0.2);color:#ff4646">🚫 LONG BLOCKED — BTC J1H in STOP zone</div>`;
    const _btcSuffix = !_rgV||_exemptV ? '' : _rgV.state==='CAUTION_LONG'||_rgV.state==='CAUTION_SHORT' ? ' · ⚠️ BTC caution' : _rgV.cls==='confirmed' ? ' · ✅ BTC confirmed' : '';
    if (score === 4)
      return `<div id="pov-verdict" style="${base};background:rgba(0,230,118,0.1);border-top:1px solid rgba(0,230,118,0.1);border-bottom:1px solid rgba(0,230,118,0.1);color:#00e676">✅ SIGNAL READY — all ${isL ? 'LONG' : 'SHORT'} gates passing${_btcSuffix}</div>`;
    if (score === 3)
      return `<div id="pov-verdict" style="${base};background:rgba(255,179,0,0.08);border-top:1px solid rgba(255,179,0,0.1);border-bottom:1px solid rgba(255,179,0,0.1);color:#ffb300">⏳ ALMOST READY — waiting for ${failing[0]}${_btcSuffix}</div>`;
    return `<div id="pov-verdict" style="${base};background:rgba(255,82,82,0.07);border-top:1px solid rgba(255,82,82,0.1);border-bottom:1px solid rgba(255,82,82,0.1);color:#ff5252">❌ NOT READY — ${failing.join(', ')}${_btcSuffix}</div>`;
  }

  function _ovGateRowHtml(idPfx, name, passHtml, bodyHtml) {
    return `<div style="padding:10px 16px;border-bottom:1px solid #1a1a1a">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:4px">
        ${_ovGateLabelHtml(name)}
        <span id="pov-${idPfx}-pass">${passHtml}</span>
      </div>
      ${bodyHtml}
    </div>`;
  }

  function _ovJ15Html(d, dir) {
    const isL    = dir !== 'SHORT';
    const v      = d.j15m || 0;
    const pass   = isL ? v < 20 : v > 80;
    const inZone = v <= 20 || v >= 80;
    const jCol   = inZone ? (v <= 20 ? '#00e676' : '#ff3d3d') : '#888';
    const jGlow  = inZone ? `box-shadow:0 0 6px ${jCol};` : '';
    const jLeft  = Math.min(99.5, Math.max(0.5, v)).toFixed(1);
    const txtCol = pass ? '#00e676' : '#ff5252';
    const body   = `
      <div style="position:relative;height:28px;margin:6px 0 0">
        <div style="position:absolute;left:0;width:20%;top:50%;transform:translateY(-50%);height:10px;background:rgba(0,255,106,0.2);border-radius:1px 0 0 1px;pointer-events:none"></div>
        <div style="position:absolute;left:20%;width:60%;top:50%;transform:translateY(-50%);height:10px;background:#222;pointer-events:none"></div>
        <div style="position:absolute;left:80%;width:20%;top:50%;transform:translateY(-50%);height:10px;background:rgba(255,61,61,0.2);border-radius:0 1px 1px 0;pointer-events:none"></div>
        <div id="pov-j15-dot" style="position:absolute;top:50%;transform:translate(-50%,-50%);left:${jLeft}%;width:12px;height:12px;border-radius:50%;background:${jCol};${jGlow}display:flex;align-items:center;justify-content:center;z-index:2">
          <span style="font-size:7px;font-weight:700;color:#000;font-family:'JetBrains Mono',monospace;line-height:1">${v.toFixed(0)}</span>
        </div>
      </div>
      <div style="display:flex;justify-content:space-between;font-size:8px;color:#2a2a2a;font-family:'JetBrains Mono',monospace;margin:2px 0 4px">
        <span>0</span><span>20</span><span>40</span><span>60</span><span>80</span><span>100</span>
      </div>
      <div style="font-family:'JetBrains Mono',monospace;font-size:12px;font-weight:700;color:${txtCol}">needs ${isL ? '&lt;20' : '&gt;80'} for ${isL ? 'LONG' : 'SHORT'}, currently <span id="pov-j15-val" style="color:${txtCol};font-weight:700">${v.toFixed(0)}</span></div>`;
    return _ovGateRowHtml('j15', 'J 15M', _ovPassIcon(pass), body);
  }

  function _ovJ1hHtml(d, dir) {
    const isL    = dir !== 'SHORT';
    const v      = d.j1h || 0;
    const pass   = isL ? v < 40 : v > 60;
    const inZone = v <= 20 || v >= 80;
    const jCol   = inZone ? (v <= 20 ? '#00e676' : '#ff3d3d') : '#888';
    const jGlow  = inZone ? `box-shadow:0 0 6px ${jCol};` : '';
    const jLeft  = Math.min(99.5, Math.max(0.5, v)).toFixed(1);
    const txtCol = pass ? '#00e676' : '#ff5252';
    const body   = `
      <div style="position:relative;height:28px;margin:6px 0 0">
        <div style="position:absolute;left:0;width:20%;top:50%;transform:translateY(-50%);height:10px;background:rgba(0,255,106,0.2);border-radius:1px 0 0 1px;pointer-events:none"></div>
        <div style="position:absolute;left:20%;width:60%;top:50%;transform:translateY(-50%);height:10px;background:#222;pointer-events:none"></div>
        <div style="position:absolute;left:80%;width:20%;top:50%;transform:translateY(-50%);height:10px;background:rgba(255,61,61,0.2);border-radius:0 1px 1px 0;pointer-events:none"></div>
        <div id="pov-j1h-dot" style="position:absolute;top:50%;transform:translate(-50%,-50%);left:${jLeft}%;width:12px;height:12px;border-radius:50%;background:${jCol};${jGlow}display:flex;align-items:center;justify-content:center;z-index:2">
          <span style="font-size:7px;font-weight:700;color:#000;font-family:'JetBrains Mono',monospace;line-height:1">${v.toFixed(0)}</span>
        </div>
      </div>
      <div style="display:flex;justify-content:space-between;font-size:8px;color:#2a2a2a;font-family:'JetBrains Mono',monospace;margin:2px 0 4px">
        <span>0</span><span>20</span><span>40</span><span>60</span><span>80</span><span>100</span>
      </div>
      <div style="font-family:'JetBrains Mono',monospace;font-size:12px;font-weight:700;color:${txtCol}">needs ${isL ? '&lt;40' : '&gt;60'} for ${isL ? 'LONG' : 'SHORT'}, currently <span id="pov-j1h-val" style="color:${txtCol};font-weight:700">${v.toFixed(0)}</span></div>`;
    return _ovGateRowHtml('j1h', 'J 1H', _ovPassIcon(pass), body);
  }

  function _ovStochHtml(d, dir) {
    const isL    = dir !== 'SHORT';
    const K      = d.stoch_k || 0;
    const D      = d.stoch_d || 0;
    const inZone = isL ? K < 25 : K > 75;
    const pass   = isL ? (K < 25 && K > D) : (K > 75 && K < D);
    const kCol   = inZone ? (isL ? '#00e676' : '#ff3d3d') : '#888';
    const dZone  = isL ? D < 25 : D > 75;
    const dCol   = dZone ? (isL ? '#00e676' : '#ff5252') : '#666';
    const desc1  = isL
      ? 'K needs to drop below 25 and cross above D'
      : 'K needs to rise above 75 and cross below D';
    let crossNote;
    if (pass) {
      crossNote = `K ${isL ? 'below' : 'above'} D in zone \u2014 ${isL ? 'LONG' : 'SHORT'} crossover confirmed \u2705`;
    } else if (isL) {
      crossNote = inZone
        ? 'K is in zone but below D \u2014 needs to cross above D \u2191'
        : 'K is above 25 and above zone \u2014 needs to fall and cross \u2193';
    } else {
      crossNote = inZone
        ? 'K is in zone but above D \u2014 needs to cross below D \u2193'
        : 'K is below 75 \u2014 needs to rise and cross \u2191';
    }
    const noteCol = pass ? '#00e676' : '#ff5252';
    const kLeft = Math.min(99, Math.max(0.5, K)).toFixed(1);
    const dLeft = Math.min(99, Math.max(0.5, D)).toFixed(1);
    const kGlow = inZone ? `box-shadow:0 0 6px ${kCol};` : '';
    const body = `
      <div style="font-family:'JetBrains Mono',monospace;font-size:12px;font-weight:700;color:#ffffff">${desc1} for ${isL ? 'LONG' : 'SHORT'}</div>
      <div style="font-family:'JetBrains Mono',monospace;font-size:12px;font-weight:700;color:#ffffff;margin-top:2px">Currently <span id="pov-sk-val" style="color:#ffffff;font-weight:700">K=${K.toFixed(1)}</span> \u00b7 <span id="pov-sd-val" style="color:#ffffff;font-weight:700">D=${D.toFixed(1)}</span></div>
      <div style="position:relative;height:10px;background:#1a1a1a;border-radius:3px;margin:8px 0 0;overflow:visible">
        <div style="position:absolute;left:0;width:25%;height:100%;background:#00ff6a;opacity:0.2;border-radius:3px 0 0 3px;pointer-events:none"></div>
        <div style="position:absolute;left:75%;width:25%;height:100%;background:#ff3d3d;opacity:0.2;border-radius:0 3px 3px 0;pointer-events:none"></div>
        <div id="pov-stoch-k" style="position:absolute;top:50%;transform:translate(-50%,-50%);left:${kLeft}%;width:14px;height:14px;border-radius:50%;background:${kCol};${kGlow}display:flex;align-items:center;justify-content:center;z-index:2">
          <span style="font-size:7px;font-weight:700;color:#000;font-family:'JetBrains Mono',monospace;line-height:1">K</span>
        </div>
        <div id="pov-stoch-d" style="position:absolute;top:50%;transform:translate(-50%,-50%);left:${dLeft}%;width:12px;height:12px;border-radius:2px;border:1.5px solid ${dCol};background:transparent;display:flex;align-items:center;justify-content:center;z-index:1">
          <span style="font-size:7px;font-weight:700;color:${dCol};font-family:'JetBrains Mono',monospace;line-height:1">D</span>
        </div>
      </div>
      <div style="display:flex;justify-content:space-between;font-size:8px;color:#444;font-family:'JetBrains Mono',monospace;margin:3px 0 4px">
        <span>0</span><span>25</span><span>50</span><span>75</span><span>100</span>
      </div>
      <div id="pov-stoch-note" style="font-size:9px;color:${noteCol};font-family:'JetBrains Mono',monospace">${crossNote}</div>`;
    return _ovGateRowHtml('stoch', 'STOCH K/D', _ovPassIcon(pass), body);
  }

  function _ovDepthHtml(d, dir) {
    const isL     = dir !== 'SHORT';
    const v       = isL ? (d.bid_pct || 0) : (d.ask_pct || 0);
    const pass    = v >= 55;
    const label   = isL ? 'bid' : 'ask';
    const zoneCol = isL ? 'rgba(0,255,106,0.2)' : 'rgba(255,61,61,0.2)';
    const dotCol  = pass ? (isL ? '#00e676' : '#ff5252') : '#888';
    const dotGlow = pass ? `box-shadow:0 0 6px ${dotCol};` : '';
    const dLeft   = Math.min(99.5, Math.max(0.5, v)).toFixed(1);
    const txtCol  = pass ? '#00e676' : '#ff5252';
    const body    = `
      <div style="position:relative;height:28px;margin:6px 0 0">
        <div style="position:absolute;left:0;width:55%;top:50%;transform:translateY(-50%);height:10px;background:#222;border-radius:1px 0 0 1px;pointer-events:none"></div>
        <div style="position:absolute;left:55%;width:45%;top:50%;transform:translateY(-50%);height:10px;background:${zoneCol};border-radius:0 1px 1px 0;pointer-events:none"></div>
        <div style="position:absolute;left:55%;top:50%;transform:translate(-50%,-50%);width:2px;height:10px;background:#ffffff;z-index:3;pointer-events:none"></div>
        <div id="pov-depth-dot" style="position:absolute;top:50%;transform:translate(-50%,-50%);left:${dLeft}%;width:12px;height:12px;border-radius:50%;background:${dotCol};${dotGlow}display:flex;align-items:center;justify-content:center;z-index:2">
          <span style="font-size:7px;font-weight:700;color:#000;font-family:'JetBrains Mono',monospace;line-height:1">${v.toFixed(0)}</span>
        </div>
      </div>
      <div style="display:flex;justify-content:space-between;font-size:8px;color:#2a2a2a;font-family:'JetBrains Mono',monospace;margin:2px 0 4px">
        <span>0</span><span>25</span><span>50</span><span>75</span><span>100</span>
      </div>
      <div style="font-family:'JetBrains Mono',monospace;font-size:12px;font-weight:700;color:${txtCol}">≥55% ${label} depth needed, currently <span id="pov-depth-val" style="color:${txtCol};font-weight:700">${v.toFixed(0)}%</span></div>`;
    return _ovGateRowHtml('depth', 'BID/ASK DEPTH', _ovPassIcon(pass), body);
  }

  function _ovScanConfHtml(d, dir, score) {
    if (score < 3) return '';
    const isL   = dir !== 'SHORT';
    const scans = (d.last_scan_summaries || []).slice(0, 4);
    const passed = scans.reduce((n, s) => n + ((isL ? (s.score_long || 0) : (s.score_short || 0)) === 4 ? 1 : 0), 0);
    const dots = Array.from({ length: 4 }, (_, i) => {
      const ok = i < scans.length && (isL ? (scans[i].score_long || 0) : (scans[i].score_short || 0)) === 4;
      return `<div style="width:20px;height:8px;border-radius:2px;background:${ok ? '#00e676' : '#1e1e1e'};border:1px solid ${ok ? 'rgba(0,230,118,0.5)' : '#333'}"></div>`;
    }).join('');
    return `<div style="padding:10px 16px;border-bottom:1px solid #1a1a1a" id="pov-scan-conf">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:5px">
        ${_ovGateLabelHtml('2-SCAN CONFIRMATION')}
      </div>
      <div style="font-family:'JetBrains Mono',monospace;font-size:12px;font-weight:700;color:#ffffff;margin-bottom:6px">${passed} of 4 consecutive scans passed \u2014 needs 4</div>
      <div style="display:flex;gap:4px">${dots}</div>
    </div>`;
  }

  function _ovScanHistHtml(d, dir) {
    const scans = (d.last_scan_summaries || []).slice(0, 3);
    const isL   = dir !== 'SHORT';
    if (!scans.length)
      return '<div style="color:#aaaaaa;font-family:\'JetBrains Mono\',monospace;font-size:10px;font-weight:600;padding:0 16px 8px">no scan data yet</div>';
    return scans.map(s => {
      const sc    = isL ? (s.score_long || 0) : (s.score_short || 0);
      const ready = sc === 4;
      const jVal  = s.j15m || 0;
      const jCol  = jVal < 20 || jVal > 80 ? '#ffb300' : '#aaaaaa';
      const bPct  = s.bid_pct || 0;
      const kVal  = s.stoch_k || 0;
      return `<div style="font-family:'JetBrains Mono',monospace;font-size:10px;font-weight:600;color:#aaaaaa;padding:2px 16px">#${s.n} J:<span style="color:${jCol}">${jVal.toFixed(0)}</span> K:${kVal.toFixed(0)} B:<span style="color:${bPct >= 55 ? '#00e676' : '#aaaaaa'}">${bPct.toFixed(0)}%</span> \u2014 <span style="color:${ready ? '#00e676' : '#555'}">${ready ? 'ready' : 'not ready'}</span></div>`;
    }).join('');
  }

  // ── Actions (kept) ────────────────────────────────────────────────────────────
  function _ovActionsHtml(d, state, dir, trade) {
    const _btcA = (STATE?.pair_states||[]).find(p=>p.symbol==='BTC');
    const _rgA = d.symbol==='BTC'||!_btcA ? null : _btcRegime(_btcA);
    const _corrA = BTC_CORRELATION[d.symbol]??0.75;
    const _btcBlocked = _rgA?.state==='STOP' && _corrA>=0.75;
    const _btcCaution = (_rgA?.state==='CAUTION_LONG'||_rgA?.state==='CAUTION_SHORT') && _corrA>=0.65;
    if (state === 'IN_TRADE' && trade) {
      return `<button class="pov-btn pov-btn-close" onclick="_ovCloseTrade('${d.symbol}','${trade.direction}')">CLOSE HL</button>
              <button class="pov-btn pov-btn-force" onclick="_ovCloseTrade('${d.symbol}','${trade.direction}')">FORCE CLOSE</button>`;
    }
    if (state === 'READY' && d.alert && d.alert_state !== 'STALE') {
      if (_btcBlocked) {
        return `<button class="pov-btn" disabled style="border-color:#ff4646;color:#ff4646;font-weight:700">🚫 LONG BLOCKED</button>
                <div style="font-size:9px;color:#ff5252;font-family:'JetBrains Mono',monospace;font-weight:700;margin-top:4px;text-align:center">BTC J1H in STOP zone — wait for regime to clear</div>`;
      }
      const lev  = d.alert.leverage || 5;
      if (_btcCaution) {
        return `<button class="pov-btn pov-btn-hl" onclick="_ovOpen('${d.symbol}','${dir}','HL',${lev})" style="border-color:#ffb300;color:#ffb300;font-weight:700">⚠️ OPEN — BTC CAUTION ${lev}x</button>`;
      }
      const rCol = (d.trend === 'Strong Bull' || d.trend === 'Bullish') ? '#00e676'
                 : (d.trend === 'Strong Bear' || d.trend === 'Bearish') ? '#ff3d57'
                 :                                                          '#aaa';
      return `<button class="pov-btn pov-btn-hl" onclick="_ovOpen('${d.symbol}','${dir}','HL',${lev})" style="border-color:${rCol};color:${rCol};font-weight:700">OPEN HL ${lev}x</button>`;
    }
    const wCol = (d.trend === 'Strong Bull' || d.trend === 'Bullish') ? '#00e676'
               : (d.trend === 'Strong Bear' || d.trend === 'Bearish') ? '#ff3d57'
               :                                                          '#aaa';
    const _ovSessHalt = (dir === 'LONG' ? d.session_halted_long  : d.session_halted_short)  || false;
    const _ovLgCDRem  = (dir === 'LONG' ? d.large_sl_cooldown_long_remaining : d.large_sl_cooldown_short_remaining) || 0;
    let _ovStatusHtml = '';
    if (_ovSessHalt) {
      _ovStatusHtml = `<div id="pov-halt-info" style="font-size:9px;color:#ff4444;font-family:'JetBrains Mono',monospace;font-weight:700;margin-bottom:6px;text-align:center">🚫 2 SL hits this session — resumes at next session open</div>`;
    } else if (_ovLgCDRem > 0) {
      const _m = Math.floor(_ovLgCDRem / 60), _s = _ovLgCDRem % 60;
      _ovStatusHtml = `<div id="pov-cd-rem" style="font-size:9px;color:#ffaa00;font-family:'JetBrains Mono',monospace;font-weight:700;margin-bottom:6px;text-align:center">⏳ 90 min cooldown: ${_m}m${_s}s remaining</div>`;
    }
    return `${_ovStatusHtml}<button class="pov-btn pov-btn-watch" disabled style="color:${wCol};border-color:${wCol};font-weight:700">WATCHING HL</button>`;
  }

  // ── Full render ───────────────────────────────────────────────────────────────
  function _ovRender(pn, d) {
    const state = _ovState(d);
    const dir   = _ovDir(d);
    const trend = d.trend || '';
    const trade = d.in_trade_long || d.in_trade_short;
    const isL   = dir !== 'SHORT';
    const gates = (isL ? d.gate_long : d.gate_short) || [false, false, false, false];
    const score = gates.filter(Boolean).length;

    pn.dataset.state = state;
    pn.style.borderColor = _ovBorderCol(state, trend);

    const price   = d.price || 0;
    const chg     = d.change_24h;
    const chgStr  = chg != null ? `${chg >= 0 ? '+' : ''}${chg.toFixed(2)}%` : '\u2014';
    const chgCol  = chg != null ? (chg >= 0 ? '#00e676' : '#ff3d57') : '#555';
    const adx     = d.adx || 0;
    const adxTier = adx >= 50 ? 'STRONG' : 'REGULAR';
    const adxCol  = adx >= 50 ? '#00e676' : adx >= 25 ? '#ffaa00' : '#666';

    let pnlHtml = '';
    if (state === 'IN_TRADE' && trade) {
      const pnl = trade.unrealized_pnl || 0;
      const r   = trade.r || 0;
      const pc  = pnl >= 0 ? '#00e676' : '#ff3d57';
      const el  = trade.elapsed_s || 0;
      const age = el < 3600
        ? `${Math.floor(el / 60)}m${el % 60}s`
        : `${Math.floor(el / 3600)}h${Math.floor((el % 3600) / 60)}m`;
      pnlHtml = `<div style="display:flex;gap:10px;align-items:center;margin-top:6px;font-family:'JetBrains Mono',monospace;font-size:10px">
        <span id="pov-pnl-usd" style="font-weight:700;color:${pc};font-size:12px">${pnl >= 0 ? '+' : ''}$${Math.abs(pnl).toFixed(2)}</span>
        <span style="color:#555">${r >= 0 ? '+' : ''}${r.toFixed(2)}R</span>
        <span id="pov-age" style="color:#444">${age}</span>
      </div>`;
    }

    const showScanConf = score >= 3;

    pn.innerHTML = `
      <div style="padding:16px 20px 10px;display:flex;justify-content:space-between;align-items:flex-start;border-bottom:1px solid #1a1a1a">
        <div style="flex:1;min-width:0">
          <div style="font-family:'Bebas Neue',sans-serif;font-size:28px;color:#fff;line-height:1;letter-spacing:0.02em">${d.symbol}</div>
          <div style="display:flex;gap:8px;align-items:center;margin-top:5px;font-family:'JetBrains Mono',monospace;font-size:12px;flex-wrap:wrap">
            <span id="pov-px" style="color:#fff;font-weight:600">${fmtPrice(price)}</span>
            <span id="pov-chg" style="color:${chgCol}">${chgStr}</span>
            <span style="color:${adxCol}">${adxTier}</span>
            ${_ovStatePillHtml(state, dir)}
          </div>
          ${pnlHtml}
        </div>
        <button onclick="closePairOverlay()" style="background:none;border:none;color:#444;font-size:18px;cursor:pointer;padding:2px;line-height:1;flex-shrink:0;margin-left:10px">\u2715</button>
      </div>
      ${_ovVerdictHtml(d, dir)}
      <div id="pov-gates-wrap">
        ${_ovJ15Html(d, dir)}
        ${_ovJ1hHtml(d, dir)}
        ${_ovStochHtml(d, dir)}
        ${_ovDepthHtml(d, dir)}
        ${showScanConf ? _ovScanConfHtml(d, dir, score) : ''}
      </div>
      <div style="border-top:1px solid #1a1a1a;padding:8px 0 6px">
        <div style="font-size:11px;font-weight:700;color:#ffffff;font-family:'JetBrains Mono',monospace;letter-spacing:0.08em;padding:0 16px 4px">SCAN HISTORY</div>
        <div id="pov-scan-hist">${_ovScanHistHtml(d, dir)}</div>
      </div>
      <div class="pov-actions" id="pov-actions" style="border-top:1px solid #1a1a1a;padding:12px 16px">${_ovActionsHtml(d, state, dir, trade)}</div>`;

    _ovPrevGates = gates;
  }

  // ── Targeted update (no full re-render) ───────────────────────────────────────
  function _ovUpdate(pn, d) {
    const state     = _ovState(d);
    const dir       = _ovDir(d);
    const trade     = d.in_trade_long || d.in_trade_short;
    const prevState = pn.dataset.state;

    if (prevState === 'IN_TRADE' && state !== 'IN_TRADE') { _ovExit(pn, d); return; }
    if (prevState !== state) { _ovRender(pn, d); return; }

    pn.dataset.state = state;
    const isL  = dir !== 'SHORT';
    const gates = (isL ? d.gate_long : d.gate_short) || [false, false, false, false];
    const score = gates.filter(Boolean).length;

    // Price
    const pxEl = document.getElementById('pov-px');
    if (pxEl) pxEl.textContent = fmtPrice(d.price);

    // Change %
    const chgEl = document.getElementById('pov-chg');
    if (chgEl && d.change_24h != null) {
      const c = d.change_24h;
      chgEl.textContent = `${c >= 0 ? '+' : ''}${c.toFixed(2)}%`;
      chgEl.style.color = c >= 0 ? '#00e676' : '#ff3d57';
    }

    // Re-render gate section (values update every 2 s from scanner)
    const gatesEl = document.getElementById('pov-gates-wrap');
    if (gatesEl) {
      gatesEl.innerHTML =
        _ovJ15Html(d, dir) +
        _ovJ1hHtml(d, dir) +
        _ovStochHtml(d, dir) +
        _ovDepthHtml(d, dir) +
        (score >= 3 ? _ovScanConfHtml(d, dir, score) : '');
    }

    // Verdict banner
    const vEl = document.getElementById('pov-verdict');
    if (vEl) vEl.outerHTML = _ovVerdictHtml(d, dir);

    // Scan history
    const histEl = document.getElementById('pov-scan-hist');
    if (histEl) histEl.innerHTML = _ovScanHistHtml(d, dir);

    // Trade P&L + age
    if (state === 'IN_TRADE' && trade) {
      const pnlEl = document.getElementById('pov-pnl-usd');
      if (pnlEl) {
        const pnl = trade.unrealized_pnl || 0;
        pnlEl.textContent = `${pnl >= 0 ? '+' : ''}$${Math.abs(pnl).toFixed(2)}`;
        pnlEl.style.color = pnl >= 0 ? '#00e676' : '#ff3d57';
      }
      const ageEl = document.getElementById('pov-age');
      if (ageEl) {
        const el = trade.elapsed_s || 0;
        ageEl.textContent = el < 3600
          ? `${Math.floor(el / 60)}m${el % 60}s`
          : `${Math.floor(el / 3600)}h${Math.floor((el % 3600) / 60)}m`;
      }
    }

    // Actions
    const actEl = document.getElementById('pov-actions');
    if (actEl) actEl.innerHTML = _ovActionsHtml(d, state, dir, trade);

    _ovPrevGates = gates;


    // BTC regime card live refresh
    const _btcNow = (STATE?.pair_states||[]).find(p => p.symbol==='BTC');
    const _rnEl = document.getElementById('btc-regime-pn');
    if (_rnEl && _btcNow) {
      const _sym = document.getElementById('pair-ov-pn')?.dataset?.sym || '';
      if (_sym && _sym !== 'BTC') {
        const _cr = BTC_CORRELATION[_sym] ?? 0.75;
        const _ex = _cr < 0.65;
        const _rg = _ex ? {state:'EXEMPT',cls:'exempt',color:'#fff',label:'⚪ EXEMPT'} : _btcRegime(_btcNow);
        _rnEl.className = _rg.cls;
        _rnEl.innerHTML = _btcRegimeCardHtml(_sym, _btcNow, _rg, _cr);
      }
    }
  }
  
// ── Exit banner (3 s auto-close) ──────────────────────────────────────────────
function _ovExit(pn, d) {
  clearInterval(_ovPollId);
  const last   = d.recent_alerts?.[0];
  const reason = last?.exit_reason || 'CLOSED';
  const pnl    = last?.pnl_usd;
  const pnlStr = pnl != null ? ` · ${pnl >= 0 ? '+' : ''}$${Math.abs(pnl).toFixed(2)}` : '';
  const col    = reason === 'SL' ? '#ff3d57' : '#00e676';
  const banner = document.createElement('div');
  banner.style.cssText = 'position:absolute;inset:0;background:rgba(0,0,0,0.88);display:flex;flex-direction:column;align-items:center;justify-content:center;border-radius:10px;z-index:10;gap:10px';
  banner.innerHTML = `
    <div style="font-family:'JetBrains Mono',monospace;font-size:22px;font-weight:800;color:#fff;letter-spacing:3px">TRADE CLOSED</div>
    <div style="font-family:'JetBrains Mono',monospace;font-size:14px;font-weight:700;color:${col}">${reason}${pnlStr}</div>`;
  pn.style.position = 'relative';
  pn.appendChild(banner);
  setTimeout(() => closePairOverlay(), 3000);
}

// ── Trade actions (overlay) ───────────────────────────────────────────────────
async function _ovOpen(sym, dir, exchange, lev) {
  try {
    const r = await fetch('/api/trade/open', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ symbol: sym, direction: dir, exchange, leverage: lev }),
    });
    if (!r.ok) { const d = await r.json(); alert(`Open failed: ${d.detail}`); return; }
    _ovFetch(sym, true);
  } catch (e) { alert('Request failed'); }
}

async function _ovCloseTrade(sym, dir) {
  try {
    const r = await fetch('/api/trade/close', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ symbol: sym, direction: dir }),
    });
    if (!r.ok) { const d = await r.json(); alert(`Close failed: ${d.detail}`); return; }
    _ovFetch(sym, true);
  } catch (e) { alert('Request failed'); }
}

// ── Reset Session ──────────────────────────────────────────────────────────────────────
function renderResetSessionBtn() {
  if (document.getElementById('reset-session-btn')) return;
  const mb = document.getElementById('mode-badge');
  if (!mb || !mb.parentNode) return;
  const btn = document.createElement('button');
  btn.id        = 'reset-session-btn';
  btn.className = 'reset-session-btn';
  btn.textContent = 'RESET SESSION';
  btn.onclick   = showResetSessionModal;
  mb.parentNode.insertBefore(btn, mb.nextSibling);
}

function showResetSessionModal() {
  if (document.getElementById('reset-session-modal')) return;
  const modal = document.createElement('div');
  modal.id = 'reset-session-modal';
  modal.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,0.75);display:flex;align-items:center;justify-content:center;z-index:9999';
  modal.innerHTML = [
    '<div style="background:#111;border:1px solid rgba(255,170,0,0.4);border-radius:10px;padding:24px 28px;max-width:340px;width:90%;font-family:\'JetBrains Mono\',monospace">',
    '<div style="color:#ffaa00;font-size:11px;font-weight:700;margin-bottom:12px;letter-spacing:0.08em">RESET SESSION</div>',
    '<div style="color:#ccc;font-size:10px;line-height:1.6;margin-bottom:18px">This will reset daily P&amp;L, consecutive losses, circuit breaker, and all cooldowns. Trade log will NOT be cleared. Confirm?</div>',
    '<div style="display:flex;gap:10px">',
    '<button onclick="confirmResetSession()" style="flex:1;padding:8px 0;background:transparent;border:1px solid #ffaa00;border-radius:6px;color:#ffaa00;font-family:\'JetBrains Mono\',monospace;font-size:10px;font-weight:700;cursor:pointer">CONFIRM</button>',
    '<button onclick="cancelResetSession()" style="flex:1;padding:8px 0;background:transparent;border:1px solid #555;border-radius:6px;color:#888;font-family:\'JetBrains Mono\',monospace;font-size:10px;font-weight:700;cursor:pointer">CANCEL</button>',
    '</div></div>',
  ].join('');
  document.body.appendChild(modal);
}

function cancelResetSession() {
  const m = document.getElementById('reset-session-modal');
  if (m) m.remove();
}

async function confirmResetSession() {
  try {
    const r = await fetch('/api/reset-session', { method: 'POST' });
    if (!r.ok) {
      const d = await r.json();
      alert('Reset failed: ' + (d.detail || 'error'));
      return;
    }
    cancelResetSession();
    fetchState();
  } catch (e) { alert('Request failed'); }
}

// Injected styles for overlay card, session halt + large SL CD pills and RESET SESSION button
(function _injectStyles() {
  const id = 'bounce-extra-styles';
  if (document.getElementById(id)) return;
  // Bebas Neue font
  if (!document.querySelector('link[href*="Bebas+Neue"]')) {
    const lk = document.createElement('link');
    lk.rel = 'stylesheet';
    lk.href = 'https://fonts.googleapis.com/css2?family=Bebas+Neue&display=swap';
    document.head.appendChild(lk);
  }
  const s = document.createElement('style');
  s.id = id;
  s.textContent = [
    '.pill-halted{background:rgba(255,68,68,0.12);color:#ff4444;border:1px solid rgba(255,68,68,0.4);border-radius:4px;font-size:8px;padding:2px 6px;font-family:\'JetBrains Mono\',monospace;font-weight:700}',
    '.pill-cd-large{background:rgba(255,170,0,0.12);color:#ffaa00;border:1px solid rgba(255,170,0,0.4);border-radius:4px;font-size:8px;padding:2px 6px;font-family:\'JetBrains Mono\',monospace;font-weight:700}',
    '.reset-session-btn{background:transparent;border:1px solid #ffaa00;border-radius:5px;color:#ffaa00;font-family:\'JetBrains Mono\',monospace;font-size:9px;font-weight:700;padding:3px 8px;cursor:pointer;letter-spacing:0.06em;margin-left:6px;vertical-align:middle}',
    '.reset-session-btn:hover{background:rgba(255,170,0,0.1)}',
    '#pair-ov-bd{position:fixed;inset:0;background:rgba(0,0,0,0.92);backdrop-filter:blur(8px);display:flex!important;align-items:stretch!important;justify-content:center!important;gap:12px!important;padding:20px!important;z-index:9000}',
    '#pair-ov-pn{background:#111;border:1px solid #222;border-radius:6px;flex:1;min-width:0;overflow-y:auto;font-family:\'JetBrains Mono\',monospace;position:relative;box-shadow:0 0 60px rgba(0,0,0,0.8),0 0 120px rgba(0,0,0,0.6),inset 0 1px 0 rgba(255,255,255,0.05)}',
    '.pov-actions{display:flex;flex-wrap:wrap;gap:8px}',
    '.pov-btn{flex:1;padding:9px 0;background:transparent;border:1px solid #444;border-radius:5px;color:#888;font-family:\'JetBrains Mono\',monospace;font-size:10px;font-weight:700;cursor:pointer;letter-spacing:0.06em;min-width:100px}',
    '.pov-btn:not(:disabled):hover{opacity:0.8}',
    '.pov-btn-hl{border-color:#00e676!important;color:#00e676!important}',
    '.pov-btn-close,.pov-btn-force{border-color:#ff5252;color:#ff5252}',
    '.pov-btn-watch:disabled{cursor:default;opacity:0.7}',
    '.pov-loading{padding:30px;text-align:center;font-family:\'JetBrains Mono\',monospace;color:#555;font-size:11px}',
    /* BTC Regime two-panel backdrop */
    '#ov-backdrop{display:flex;align-items:center;justify-content:center;gap:12px;padding:20px;}',
    '#ov-backdrop{display:flex;align-items:center;justify-content:center;gap:12px;padding:20px;}',
    '#btc-regime-pn{flex:1;min-width:0;border-radius:8px;overflow:hidden;display:flex;flex-direction:column;font-family:\'JetBrains Mono\',monospace;}',
    '#btc-regime-pn.confirmed{background:#081408;border:2px solid #00e67666;box-shadow:0 0 40px rgba(0,230,118,0.20),0 0 80px rgba(0,230,118,0.08),0 0 120px rgba(0,0,0,0.8);}',
    '#btc-regime-pn.caution{background:#0e0b00;border:2px solid #ffb30066;box-shadow:0 0 40px rgba(255,179,0,0.18),0 0 80px rgba(255,179,0,0.07),0 0 120px rgba(0,0,0,0.8);}',
    '#btc-regime-pn.stop{background:#140808;border:2px solid #ff525266;box-shadow:0 0 40px rgba(255,82,82,0.22),0 0 80px rgba(255,82,82,0.09),0 0 120px rgba(0,0,0,0.8);}',
    '#btc-regime-pn.exempt{background:#0a0a0a;border:1px solid #2a2a2a;box-shadow:0 0 40px rgba(255,255,255,0.05),0 0 120px rgba(0,0,0,0.8);}',
    /* Threshold bar zones */
    '.tbar{display:flex;height:16px;border-radius:4px;overflow:hidden;}',
    '.tz-safe{flex:2;background:rgba(0,230,118,0.30);display:flex;align-items:center;justify-content:center;font-size:7px;font-weight:700;color:#00e676;}',
    '.tz-caut{flex:2;background:rgba(255,179,0,0.25);display:flex;align-items:center;justify-content:center;font-size:7px;font-weight:700;color:#ffb300;border-left:1px solid #ffb30044;border-right:1px solid #ffb30044;}',
    '.tz-stop{flex:2;background:rgba(255,70,70,0.35);display:flex;align-items:center;justify-content:center;font-size:7px;font-weight:700;color:#ff4646;border-left:1px solid #ff464444;border-right:1px solid #ff464444;}',
    '.tz-caut2{flex:2;background:rgba(255,179,0,0.25);display:flex;align-items:center;justify-content:center;font-size:7px;font-weight:700;color:#ffb300;border-right:1px solid #ffb30044;}',
    '.tz-safe2{flex:2;background:rgba(255,70,70,0.30);display:flex;align-items:center;justify-content:center;font-size:7px;font-weight:700;color:#ff4646;}',
    '.tbar-wrap{position:relative;margin-bottom:3px;}',
    '.tcursor{position:absolute;top:-2px;bottom:-2px;width:3px;border-radius:2px;transform:translateX(-50%);z-index:3;}',
    '.tcursor.conf{background:#00e676;box-shadow:0 0 12px #00e676,0 0 24px rgba(0,230,118,0.5);}',
    '.tcursor.caut{background:#ffb300;box-shadow:0 0 12px #ffb300,0 0 24px rgba(255,179,0,0.5);}',
    '.tcursor.stop{background:#ff4646;box-shadow:0 0 12px #ff4646,0 0 24px rgba(255,70,70,0.5);}',
    '.tcursor.exempt{background:#555;}',
    /* Rail tracks */
    '.rtw{position:relative;height:16px;margin:2px 0;}',
    '.rtl{position:absolute;left:0;width:20%;height:10px;background:#00e676;opacity:0.35;top:50%;transform:translateY(-50%);border-radius:2px 0 0 2px;}',
    '.rtm{position:absolute;left:20%;width:60%;height:10px;background:#2a2a2a;top:50%;transform:translateY(-50%);}',
    '.rth{position:absolute;right:0;width:20%;height:10px;background:#ff4646;opacity:0.35;top:50%;transform:translateY(-50%);border-radius:0 2px 2px 0;}',
    '.rdot{position:absolute;top:50%;transform:translate(-50%,-50%);width:13px;height:13px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:7px;font-weight:700;z-index:2;}',
    '.rsq{position:absolute;top:50%;transform:translate(-50%,-50%);width:11px;height:11px;border-radius:2px;display:flex;align-items:center;justify-content:center;font-size:7px;font-weight:700;z-index:2;background:#000;}',
    '.rdot.glz{background:#00e676;box-shadow:0 0 8px #00e676,0 0 16px rgba(0,230,118,0.4);color:#000;}',
    '.rdot.gsz{background:#ff4646;box-shadow:0 0 8px #ff4646,0 0 16px rgba(255,70,70,0.4);color:#000;}',
    '.rdot.gnz{background:#555;color:#fff;}',
    '.rsq.glz{border:2px solid #00e676;color:#00e676;}',
    '.rsq.gsz{border:2px solid #ff4646;color:#ff4646;}',
    '.rsq.gnz{border:2px solid #888;color:#888;}',
    '.rtticks{display:flex;justify-content:space-between;font-size:7px;font-weight:700;color:#fff;margin-top:1px;}',
    /* SNAP/NOW rows */
    '.sn-pill{font-size:8px;font-weight:700;padding:1px 6px;border-radius:3px;letter-spacing:0.05em;flex-shrink:0;width:40px;text-align:center;border:1px solid;}',
    '.sn-pill.snap{background:#111;border-color:#333;color:#fff;}',
    '.sn-pill.now{background:#1a1200;border-color:#ffb30066;color:#ffb300;}',
    /* Regime decision box */
    '.rdec{border-radius:4px;padding:6px 8px;flex-shrink:0;}',
    '.rdec.conf{background:#0a1a0a;border:1px solid #00e67644;}',
    '.rdec.caut{background:#1a1200;border:1px solid #ffb30044;}',
    '.rdec.stop{background:#1a0808;border:1px solid #ff525266;}',
    '.rdec.exempt{background:#0d0d0d;border:1px solid #2a2a2a;}',
  ].join('');
  document.head.appendChild(s);
})();

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
