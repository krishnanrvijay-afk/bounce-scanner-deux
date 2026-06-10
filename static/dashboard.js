/* ── Bounce Scanner II — dashboard.js ──────────────────────────────────────── */
let STATE        = null;
let activeFilter = 'ALL';
let activeTab    = 'grid';
let lastScanAt   = null;
let marketOpen   = false;
let posTimers    = {};
let bannerTF     = 'BOTH';

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

// ── Market Health strip ───────────────────────────────────────────────────────
function renderMarketHealth() {
  const mh = STATE?.market_health;
  const pillCls = st => 'mh-pill mh-pill-' + (st || 'caution').toLowerCase();
  const compCls = st => 'mh-companion mh-comp-' + (st || 'caution').toLowerCase();

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

  const renderSide = (pillId, ctxId, status, side) => {
    const pill = document.getElementById(pillId);
    const ctx  = document.getElementById(ctxId);
    if (!pill || !ctx) return;
    pill.className = pillCls(status);
    pill.innerHTML = `<span class="mh-dir">${side}</span><span class="mh-state">${status || 'CAUTION'}</span>`;
    ctx.className  = compCls(status);
    ctx.innerHTML  = context(status, side, mh);
  };
  renderSide('mh-short', 'mh-short-ctx', mh?.short_status || 'CAUTION', 'SHORT');
  renderSide('mh-long',  'mh-long-ctx',  mh?.long_status  || 'CAUTION', 'LONG');
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

  document.getElementById('h-margin').textContent    = `$${Math.round(account?.margin_deployed || 0).toLocaleString()}`;
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
  const rsi15m = p.rsi15m  || 0;
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
  const shortGates = [j15m > 80, j1h > 60, rsi15m > 65, askPct >= 55];
  const longGates  = [j15m < 20, j1h < 40, rsi15m < 35, bidPct >= 55];
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
  if (showShort) rows += dirRow('SHORT', rsi15m, askPct);
  if (showLong)  rows += dirRow('LONG',  rsi15m, bidPct);

  // ── Confluence mini bars (RSI + Depth) — shown only on confluence cards ───────
  let confBars = '';
  if (isConf) {
    const depthPct   = confIsLong ? bidPct : askPct;
    const depthLabel = confIsLong ? 'BID' : 'ASK';
    const depthPass  = depthPct >= 55;
    const rsiPass    = confIsLong ? rsi15m < 35 : rsi15m > 65;
    const rsiPct     = Math.min(100, Math.max(0, rsi15m));
    const rsiCurCol  = confIsLong ? (rsi15m < 35 ? '#00e676' : '#555') : (rsi15m > 65 ? '#ff3d57' : '#555');
    const rsiDotCls  = rsiPass ? (confIsLong ? 'long-pass' : 'short-pass') : (confIsLong ? 'long-fail' : 'short-fail');
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
      <span class="gc-dot cbar-dot ${rsiDotCls}"></span>
      <span class="cbar-label">RSI</span>
      <div class="cbar-track">
        <div class="cbar-zg" style="width:35%"></div>
        <div class="cbar-zr" style="left:65%;width:35%"></div>
        <div class="cbar-thresh cbar-thresh-l" style="left:35%"></div>
        <div class="cbar-thresh cbar-thresh-r" style="left:65%"></div>
        <div class="cbar-cursor" style="left:${rsiPct}%;background:${rsiCurCol};box-shadow:0 0 5px ${rsiCurCol}"></div>
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
    if      (passing === 4) pills = `<span class="pill ${rdyCls}">✦ READY</span>`;
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

function dirRow(direction, rsi15m, depthPct) {
  const isLong     = direction === 'LONG';
  const rowCls     = isLong ? 'long-row' : 'short-row';
  const depthLabel = isLong ? 'BID%' : 'ASK%';
  const rsiColor   = isLong ? (rsi15m < 35 ? 'green' : 'grey') : (rsi15m > 65 ? 'red' : 'grey');
  const depthColor = depthPct >= 55 ? (isLong ? 'green' : 'red') : 'grey';

  return `<div class="dir-row ${rowCls}">
    <div class="dir-vals">
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

    // Anti-overlap: pairs within 4 pts alternate between row 0 and row 1 (max 2 rows)
    const rowEdge = [undefined, undefined];
    const placed = items.map(item => {
      let row = 0;
      if (rowEdge[0] !== undefined && rowEdge[0] > item.j - 4) row = 1;
      rowEdge[row] = item.j + 4;
      return { ...item, row };
    });

    container.innerHTML = placed.map(({ sym, j, row, longConf, shortConf }) => {
      const isConf = longConf || shortConf;
      const col = tfKey === '15m'
        ? (j < 20 ? '#00e676' : j < 35 ? 'rgba(0,230,118,0.5)' : j < 65 ? 'rgba(255,255,255,0.4)' : j < 80 ? 'rgba(255,61,87,0.5)' : '#ff3d57')
        : (j < 40 ? '#00e676' : j < 50 ? 'rgba(0,230,118,0.5)' : j < 60 ? 'rgba(255,255,255,0.4)' : j < 70 ? 'rgba(255,61,87,0.5)' : '#ff3d57');
      const pulseCls   = isConf ? ' cb-conf' : '';
      const extraBot   = row * 12;
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
      <div class="ac2-metric-label" style="color:#fff;font-weight:700">${lbl}</div>
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

  const adxCl = v => v >= 50 ? '#00ff88' : v >= 25 ? '#ffaa00' : '#fff';
  const rsiCl = v => v > 65  ? '#ff4444' : v < 35  ? '#00ff88' : '#fff';
  const jCl   = v => v > 80  ? '#ff4444' : v < 20  ? '#00ff88' : '#fff';
  const dCol  = isLong ? (bidPc >= 60 ? '#00ff88' : '#ff4444') : (askPc >= 60 ? '#00ff88' : '#ff4444');

  // Scan narrative
  const jTr  = j15m > 60 ? 'rising' : j15m < 40 ? 'falling' : 'flat';
  const narr = ps.symbol
    ? `SCAN  J ${(+j15m).toFixed(1)}  ${dLbl} ${(+dPct).toFixed(1)}%  ADX ${(+adx).toFixed(1)}  RSI ${(+rsi).toFixed(1)}  J ${jTr}`
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
    <div class="pcv2-metric"><span class="pcv2-ml" style="color:#fff;font-weight:700">ADX</span><span class="pcv2-mv" style="color:${adxCl(adx)}">${(+adx).toFixed(1)}</span></div>
    <div class="pcv2-metric"><span class="pcv2-ml" style="color:#fff;font-weight:700">RSI15M</span><span class="pcv2-mv" style="color:${rsiCl(rsi)}">${(+rsi).toFixed(1)}</span></div>
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

// ── Log tab ───────────────────────────────────────────────────────────────────
function renderLogTab() {
  const log = STATE.trade_log || [];
  document.getElementById('log-count').textContent = `${log.length} trade${log.length!==1?'s':''}`;
  renderStatsPanel(log);
  renderPerfPanel(log);

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
      <td style="color:#fff;">${r.tier||'—'}</td>
      <td style="color:#fff;">${r.leverage||'—'}x</td>
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
    </tr>` + _tradeVisRow(r);
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

// ── Pair Symbol Overlay ───────────────────────────────────────────────────────
let _ovPollId    = null;
let _ovPrevGates = null;

function openPairOverlay(sym) {
  if (document.getElementById('pair-ov-bd')) return;
  const bd = document.createElement('div');
  bd.id = 'pair-ov-bd';
  bd.addEventListener('click', e => { if (e.target === bd) closePairOverlay(); });
  const pn = document.createElement('div');
  pn.id = 'pair-ov-pn';
  pn.dataset.sym   = sym;
  pn.dataset.state = '';
  pn.innerHTML = `<div class="pov-loading">Loading ${sym}…</div>`;
  bd.appendChild(pn);
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
  return 'WATCHING';
}
function _ovDir(d) {
  if (d.in_trade_long)    return 'LONG';
  if (d.in_trade_short)   return 'SHORT';
  if (d.alert)            return d.alert.direction;
  if (d.confluence_long)  return 'LONG';
  if (d.confluence_short) return 'SHORT';
  if (d.score_long  > d.score_short) return 'LONG';
  if (d.score_short > d.score_long)  return 'SHORT';
  return null;
}
function _ovGates(d, dir) { return dir === 'SHORT' ? d.gate_short : d.gate_long; }
function _ovBorderCol(state, trend) {
  if (state === 'IN_TRADE')                                    return 'rgba(100,160,255,0.5)';
  if (trend === 'Strong Bull' || trend === 'Bullish')          return 'rgba(0,230,118,0.5)';
  if (trend === 'Strong Bear' || trend === 'Bearish')          return 'rgba(255,61,87,0.5)';
  return 'rgba(255,255,255,0.15)';
}
function _ovSymCol(state, trend) {
  if (state === 'IN_TRADE')                                    return '#66aaff';
  if (trend === 'Strong Bull' || trend === 'Bullish')          return '#00e676';
  if (trend === 'Strong Bear' || trend === 'Bearish')          return '#ff3d57';
  return '#aaa';
}

// ── HTML builders ─────────────────────────────────────────────────────────────
function _ovStatePillHtml(state, dir) {
  if (state === 'IN_TRADE')                   return `<span class="pov-badge pov-st-trade">IN TRADE</span>`;
  if (state === 'READY' && dir === 'LONG')    return `<span class="pov-badge pov-st-rdy-l">READY</span>`;
  if (state === 'READY' && dir === 'SHORT')   return `<span class="pov-badge pov-st-rdy-s">READY</span>`;
  return `<span class="pov-badge pov-st-watch">WATCHING</span>`;
}

function _ovGateBarsHtml(d, dir) {
  const isL     = dir !== 'SHORT';
  const gArr    = isL ? d.gate_long : d.gate_short;
  const j15m    = d.j15m    || 0;
  const j1h     = d.j1h     || 0;
  const rsi     = d.rsi15m  || 0;
  const bid     = d.bid_pct || 0;
  const ask     = d.ask_pct || 0;
  const dotCls  = (pass) => pass
    ? (isL ? 'pov-gd pov-gd-pass-l' : 'pov-gd pov-gd-pass-s')
    : 'pov-gd pov-gd-fail';
  const j15Col  = j15m < 20 ? '#00e676' : j15m > 80 ? '#ff3d57' : '#fff';
  const j1hCol  = j1h  < 40 ? '#00e676' : j1h  > 60 ? '#ff3d57' : '#fff';
  const rsiCol  = rsi  < 35 ? '#00e676' : rsi  > 65 ? '#ff3d57' : '#fff';
  const depPct  = isL ? bid : ask;
  const depCol  = gArr[3] ? (isL ? '#00e676' : '#ff3d57') : '#fff';
  const bidW    = Math.min(100, Math.max(0, bid));
  const askW    = Math.min(100, Math.max(0, ask));
  return `
    <div class="pov-gr" data-gi="0">
      <div class="${dotCls(gArr[0])}" id="pov-gd-0"></div>
      <span class="pov-gn">J15M</span>
      <div class="pov-gt">
        <div class="pov-gzg" style="width:20%"></div>
        <div class="pov-gzr" style="left:80%;width:20%"></div>
        <div class="pov-gth" style="left:20%"></div><div class="pov-gth" style="left:80%"></div>
        <div class="pov-gcur" id="pov-gc-0" style="left:${Math.min(99,j15m).toFixed(1)}%;background:${j15Col}"></div>
      </div>
      <span class="pov-gv" id="pov-gv-0" style="color:${j15Col}">${j15m.toFixed(0)}</span>
    </div>
    <div class="pov-gr" data-gi="1">
      <div class="${dotCls(gArr[1])}" id="pov-gd-1"></div>
      <span class="pov-gn">J1H</span>
      <div class="pov-gt">
        <div class="pov-gzg" style="width:40%"></div>
        <div class="pov-gzr" style="left:60%;width:40%"></div>
        <div class="pov-gth" style="left:40%"></div><div class="pov-gth" style="left:60%"></div>
        <div class="pov-gcur" id="pov-gc-1" style="left:${Math.min(99,j1h).toFixed(1)}%;background:${j1hCol}"></div>
      </div>
      <span class="pov-gv" id="pov-gv-1" style="color:${j1hCol}">${j1h.toFixed(0)}</span>
    </div>
    <div class="pov-gr" data-gi="2">
      <div class="${dotCls(gArr[2])}" id="pov-gd-2"></div>
      <span class="pov-gn">RSI</span>
      <div class="pov-gt">
        <div class="pov-gzg" style="width:35%"></div>
        <div class="pov-gzr" style="left:65%;width:35%"></div>
        <div class="pov-gth" style="left:35%"></div><div class="pov-gth" style="left:65%"></div>
        <div class="pov-gcur" id="pov-gc-2" style="left:${Math.min(99,rsi).toFixed(1)}%;background:${rsiCol}"></div>
      </div>
      <span class="pov-gv" id="pov-gv-2" style="color:${rsiCol}">${rsi.toFixed(0)}</span>
    </div>
    <div class="pov-gr" data-gi="3">
      <div class="${dotCls(gArr[3])}" id="pov-gd-3"></div>
      <span class="pov-gn">DEPTH</span>
      <div class="pov-dt">
        <div class="pov-dbid" id="pov-dbid" style="width:${bidW.toFixed(0)}%;opacity:${isL && bidW >= 55 ? '0.75' : '0.2'}"></div>
        <div class="pov-dask" id="pov-dask" style="width:${askW.toFixed(0)}%;opacity:${!isL && askW >= 55 ? '0.75' : '0.2'}"></div>
        <div class="pov-dgln" style="left:55%"></div>
      </div>
      <span class="pov-gv" id="pov-gv-3" style="color:${depCol}">${depPct.toFixed(0)}%</span>
    </div>`;
}

function _ovRulerHtml(d, dir) {
  const src = d.in_trade_long || d.in_trade_short || d.alert;
  if (!src || !src.sl_price || !src.entry_price) return '';
  const sl   = src.sl_price;
  const ep   = src.entry_price;
  const tp1  = src.tp1_price;
  const tp2  = src.tp2_price;
  const cur  = d.price || ep;
  const slD  = Math.abs(ep - sl);
  const tp2R = tp2 || (dir === 'LONG' ? ep + slD * 2 : ep - slD * 2);
  const lo   = Math.min(sl, cur, tp2R) - slD * 0.05;
  const hi   = Math.max(sl, cur, tp2R) + slD * 0.05;
  const span = hi - lo || 1;
  const pct  = (v) => Math.min(99, Math.max(1, ((v - lo) / span) * 100));
  const curP = pct(cur);
  const epP  = pct(ep);
  const curCol = (dir === 'LONG' ? cur >= ep : cur <= ep) ? '#00e676' : '#ff3d57';
  const slZL  = dir === 'LONG' ? pct(sl) : epP;
  const slZW  = dir === 'LONG' ? epP - pct(sl) : pct(sl) - epP;
  const pfL   = dir === 'LONG' ? epP : (tp2R ? pct(tp2R) : epP);
  const pfW   = dir === 'LONG' ? (tp2R ? pct(tp2R) - epP : 0) : epP - (tp2R ? pct(tp2R) : epP);
  let marks = `<div class="pov-rm pov-rm-ep" style="left:${epP.toFixed(1)}%"></div>`;
  if (tp1)  marks += `<div class="pov-rm pov-rm-tp1" style="left:${pct(tp1).toFixed(1)}%"></div>`;
  if (tp2R) marks += `<div class="pov-rm pov-rm-tp2" style="left:${pct(tp2R).toFixed(1)}%"></div>`;
  return `<div class="pov-ruler-hdr">
    <span style="color:#ff3d57">SL ${fmtPrice(sl)}</span>
    ${tp1 ? `<span style="color:#66aaff">TP1 ${fmtPrice(tp1)}</span>` : ''}
    ${tp2R ? `<span style="color:#00e676">TP2 ${fmtPrice(tp2R)}</span>` : ''}
  </div>
  <div class="pov-ruler-track">
    <div class="pov-rzsl" style="left:${Math.min(slZL,slZL+slZW).toFixed(1)}%;width:${Math.abs(slZW).toFixed(1)}%"></div>
    <div class="pov-rzpf" style="left:${Math.min(pfL,pfL+pfW).toFixed(1)}%;width:${Math.abs(pfW).toFixed(1)}%"></div>
    ${marks}
    <div class="pov-rdot" id="pov-rdot" style="left:${curP.toFixed(1)}%;background:${curCol}"></div>
  </div>`;
}

function _ovActionsHtml(d, state, dir, trade) {
  if (state === 'IN_TRADE' && trade) {
    return `<button class="pov-btn pov-btn-close" onclick="_ovCloseTrade('${d.symbol}','${trade.direction}')">CLOSE HL</button>
            <button class="pov-btn pov-btn-force" onclick="_ovCloseTrade('${d.symbol}','${trade.direction}')">FORCE CLOSE</button>`;
  }
  if (state === 'READY' && d.alert && d.alert_state !== 'STALE') {
    const lev = d.alert.leverage || 5;
    const rCol = (d.trend === 'Strong Bull' || d.trend === 'Bullish') ? '#00e676'
               : (d.trend === 'Strong Bear' || d.trend === 'Bearish') ? '#ff3d57'
               :                                                          '#aaa';
    return `<button class="pov-btn pov-btn-hl" onclick="_ovOpen('${d.symbol}','${dir}','HL',${lev})" style="border-color:${rCol};color:${rCol};font-weight:700">OPEN HL ${lev}x</button>`;
  }
  const wCol = (d.trend === 'Strong Bull' || d.trend === 'Bullish') ? '#00e676'
             : (d.trend === 'Strong Bear' || d.trend === 'Bearish') ? '#ff3d57'
             :                                                         '#aaa';
  // Session halt / large SL CD status above WATCHING button
  const _ovSessHalt = (dir === 'LONG' ? d.session_halted_long : d.session_halted_short) || false;
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

function _ovStaleHtml(d) {
  const age = d.alert_age_seconds || 0;
  const MAX = 600;
  const pct = Math.max(0, Math.min(100, 100 - (age / MAX) * 100));
  const col = d.alert_state === 'STALE' ? '#ff3d57' : d.alert_state === 'AGING' ? '#ffaa00' : '#00e676';
  const rem = Math.max(0, MAX - age);
  const rs  = rem >= 60 ? `${Math.floor(rem/60)}m${rem % 60}s` : `${rem}s`;
  return `<div class="pov-stale-hdr">
    <span style="color:${col};font-weight:800">${d.alert_state || 'FRESH'}</span>
    <span style="color:#555">${rs} remaining</span>
  </div>
  <div class="pov-stale-track">
    <div class="pov-sfill" id="pov-sfill" style="width:${pct.toFixed(1)}%;background:${col}"></div>
  </div>`;
}

function _ovScanRowsHtml(snaps) {
  if (!snaps || !snaps.length) return `<div style="color:#2a2a2a;font-size:9px">no scan data yet</div>`;
  return snaps.map((s, i) => {
    const lc = (s.score_long  || 0) === 4 ? '#00e676' : '#555';
    const sc = (s.score_short || 0) === 4 ? '#ff3d57' : '#555';
    const jc = (s.j15m || 50) < 20 ? '#00e676' : (s.j15m || 50) > 80 ? '#ff3d57' : '#fff';
    return `<div class="pov-scan-r ${i === 0 ? 'pov-scan-fresh' : ''}">
      <span style="color:#fff">#${s.n}</span>
      <span>J:<span style="color:${jc}">${(s.j15m||0).toFixed(0)}</span></span>
      <span>RSI:<span style="color:#fff">${(s.rsi15m||0).toFixed(0)}</span></span>
      <span>B:<span style="color:${(s.bid_pct||0)>=55?'#00e676':'#555'}">${(s.bid_pct||0).toFixed(0)}%</span></span>
      <span>A:<span style="color:${(s.ask_pct||0)>=55?'#ff3d57':'#555'}">${(s.ask_pct||0).toFixed(0)}%</span></span>
      <span>ADX:<span style="color:${(s.adx1h||0)>=50?'#00e676':'#555'}">${(s.adx1h||0).toFixed(0)}</span></span>
      <span style="color:${lc}">L${s.score_long||0}</span>
      <span style="color:${sc}">S${s.score_short||0}</span>
    </div>`;
  }).join('');
}

function _ovHistHtml(hist) {
  if (!hist || !hist.length) return `<div style="color:#222;font-family:'JetBrains Mono',monospace;font-size:9px">no history yet</div>`;
  const rc = (r) => r === 'TP2' ? '#00e676' : r === 'TP1' ? '#66aaff' : r === 'SL' ? '#ff3d57' : '#444';
  return hist.map(h => {
    const pnl   = h.pnl_usd || 0;
    const pc    = pnl >= 0 ? '#00e676' : '#ff3d57';
    const dirCl = h.direction === 'LONG' ? 'card-dir-l' : 'card-dir-s';
    const ts    = h.timestamp_closed
      ? new Date(h.timestamp_closed * 1000).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'})
      : '—';
    return `<div class="pov-hr">
      <span style="color:#333;min-width:36px">${ts}</span>
      <span class="${dirCl}" style="font-size:8px;padding:1px 5px">${h.direction}</span>
      <span style="color:#555">${fmtPrice(h.entry_price)}</span>
      <span style="color:${rc(h.exit_reason)};font-weight:800">${h.exit_reason||'—'}</span>
      <span style="color:${pc}">${pnl >= 0 ? '+' : ''}$${Math.abs(pnl).toFixed(2)}</span>
      <span style="color:#333">${h.r_value != null ? (h.r_value >= 0 ? '+' : '') + h.r_value + 'R' : ''}</span>
    </div>`;
  }).join('');
}

// ── Full render ───────────────────────────────────────────────────────────────
function _ovRender(pn, d) {
  const state = _ovState(d);
  const dir   = _ovDir(d);
  const trend = d.trend || '';
  const trade = d.in_trade_long || d.in_trade_short;
  const alert = d.alert;

  pn.dataset.state = state;
  pn.style.border  = `1px solid ${_ovBorderCol(state, trend)}`;

  const symCol    = _ovSymCol(state, trend);
  const statePill = _ovStatePillHtml(state, dir);

  let dirBadge = '';
  if (alert || state === 'IN_TRADE') {
    const dl = dir === 'LONG' ? 'pov-dir-l' : 'pov-dir-s';
    dirBadge = `<span class="pov-badge ${dl}">BOUNCE ${dir||''}</span>`;
  }
  let confBadge = '';
  if (d.confluence_long || d.confluence_short)
    confBadge = `<span class="pov-badge pov-badge-conf">✦ CONFL</span>`;
  let tierBadge = '';
  const tier = alert?.tier || trade?.tier;
  const lev  = alert?.leverage || trade?.leverage;
  if (tier && lev) tierBadge = `<span class="pov-badge pov-badge-tier">${tier} ${lev}x</span>`;

  const chg = d.change_24h;
  const chgHtml = chg != null
    ? `<span class="pov-chg" style="color:${chg >= 0 ? '#00e676' : '#ff3d57'}">${chg >= 0 ? '+' : ''}${chg.toFixed(2)}%</span>`
    : '';

  let pnlHtml = '';
  if (state === 'IN_TRADE' && trade) {
    const pnl = trade.unrealized_pnl || 0;
    const r   = trade.r || 0;
    const pc  = pnl >= 0 ? '#00e676' : '#ff3d57';
    const el  = trade.elapsed_s || 0;
    const age = el < 3600 ? `${Math.floor(el/60)}m${el%60}s` : `${Math.floor(el/3600)}h${Math.floor((el%3600)/60)}m`;
    pnlHtml = `<div class="pov-pnl"><div class="pov-pnl-usd" style="color:${pc}">${pnl >= 0 ? '+' : ''}$${Math.abs(pnl).toFixed(2)}</div><div class="pov-pnl-r">${r >= 0 ? '+' : ''}${r.toFixed(2)}R</div><div class="pov-pnl-age" id="pov-age">${age}</div></div>`;
  }

  const showRuler = (state === 'READY' || state === 'IN_TRADE') && (alert || trade);
  const rulerHtml = showRuler ? _ovRulerHtml(d, dir) : '';
  const gatesHtml = _ovGateBarsHtml(d, dir);
  const adxCol    = (d.adx||0) >= 50 ? '#00e676' : (d.adx||0) >= 25 ? '#ffaa00' : '#fff';
  const staleHtml = state === 'READY' && alert ? _ovStaleHtml(d) : '';
  const actHtml   = _ovActionsHtml(d, state, dir, trade);

  pn.innerHTML = `
    <div class="pov-hdr">
      <div>
        <div class="pov-sym" style="color:${symCol}">${d.symbol}</div>
        <div class="pov-badges">${dirBadge}${confBadge}${tierBadge}${statePill}</div>
      </div>
      <button class="pov-x" onclick="closePairOverlay()">✕</button>
    </div>
    <div class="pov-body">
      <div class="pov-price-row">
        <div><span class="pov-px" id="pov-px">${fmtPrice(d.price)}</span> <span id="pov-chg">${chgHtml}</span></div>
        <div id="pov-pnl">${pnlHtml}</div>
      </div>
      <div id="pov-ruler" class="pov-ruler-wrap">${rulerHtml}</div>
      <div class="pov-gates-sec" id="pov-gates">${gatesHtml}</div>
      <div class="pov-adx-row">
        <div>
          <div class="pov-adx-val" id="pov-adx" style="color:${adxCol}">${(d.adx||0).toFixed(1)}</div>
          <div class="pov-adx-lbl">ADX 1H</div>
        </div>
        <div class="pov-scans" id="pov-scans">${_ovScanRowsHtml(d.last_scan_summaries)}</div>
      </div>
      ${staleHtml ? `<div class="pov-stale" id="pov-stale">${staleHtml}</div>` : '<div id="pov-stale" style="display:none"></div>'}
      <div class="pov-hist-sec">
        <div class="pov-hist-lbl">RECENT TRADES</div>
        <div id="pov-hist">${_ovHistHtml(d.recent_alerts)}</div>
      </div>
    </div>
    <div class="pov-actions" id="pov-actions">${actHtml}</div>`;

  _ovPrevGates = _ovGates(d, dir);
}

// ── Targeted update (no full re-render) ───────────────────────────────────────
function _ovUpdate(pn, d) {
  const state     = _ovState(d);
  const dir       = _ovDir(d);
  const trade     = d.in_trade_long || d.in_trade_short;
  const prevState = pn.dataset.state;

  // Trade just closed — show exit banner then close
  if (prevState === 'IN_TRADE' && state !== 'IN_TRADE') {
    _ovExit(pn, d); return;
  }
  // State transition — full re-render
  if (prevState !== state) { _ovRender(pn, d); return; }

  pn.dataset.state = state;

  // Price
  const pxEl = document.getElementById('pov-px');
  if (pxEl) pxEl.textContent = fmtPrice(d.price);

  // Change %
  const chgEl = document.getElementById('pov-chg');
  if (chgEl && d.change_24h != null) {
    const chg = d.change_24h;
    chgEl.innerHTML = `<span class="pov-chg" style="color:${chg >= 0 ? '#00e676' : '#ff3d57'}">${chg >= 0 ? '+' : ''}${chg.toFixed(2)}%</span>`;
  }

  // Unrealized P&L
  const pnlEl = document.getElementById('pov-pnl');
  if (pnlEl && state === 'IN_TRADE' && trade) {
    const pnl = trade.unrealized_pnl || 0;
    const r   = trade.r || 0;
    const pc  = pnl >= 0 ? '#00e676' : '#ff3d57';
    pnlEl.innerHTML = `<div class="pov-pnl"><div class="pov-pnl-usd" style="color:${pc}">${pnl >= 0 ? '+' : ''}$${Math.abs(pnl).toFixed(2)}</div><div class="pov-pnl-r">${r >= 0 ? '+' : ''}${r.toFixed(2)}R</div></div>`;
  }

  // Gate cursors + dot flash on pass/fail change
  const curGates = _ovGates(d, dir);
  const vals     = [d.j15m||0, d.j1h||0, d.rsi15m||0];
  const cols     = [
    d.j15m   < 20 ? '#00e676' : d.j15m   > 80 ? '#ff3d57' : '#fff',
    d.j1h    < 40 ? '#00e676' : d.j1h    > 60 ? '#ff3d57' : '#fff',
    d.rsi15m < 35 ? '#00e676' : d.rsi15m > 65 ? '#ff3d57' : '#fff',
  ];
  const isL = dir !== 'SHORT';
  const dotPassCls = (pass) => pass ? (isL ? 'pov-gd pov-gd-pass-l' : 'pov-gd pov-gd-pass-s') : 'pov-gd pov-gd-fail';
  [0, 1, 2].forEach(i => {
    const cur = document.getElementById(`pov-gc-${i}`);
    if (cur) { cur.style.left = `${Math.min(99, vals[i]).toFixed(1)}%`; cur.style.background = cols[i]; }
    const val = document.getElementById(`pov-gv-${i}`);
    if (val) { val.textContent = vals[i].toFixed(0); val.style.color = cols[i]; }
    const dot = document.getElementById(`pov-gd-${i}`);
    if (dot) {
      dot.className = dotPassCls(curGates[i]);
      if (_ovPrevGates && _ovPrevGates[i] !== curGates[i]) {
        dot.classList.add('pov-gd-flash');
        setTimeout(() => dot.classList.remove('pov-gd-flash'), 350);
      }
    }
  });
  // Gate dot 3 — DEPTH (add to same update pass)
  const dot3 = document.getElementById('pov-gd-3');
  if (dot3) {
    dot3.className = dotPassCls(curGates[3]);
    if (_ovPrevGates && _ovPrevGates[3] !== curGates[3]) {
      dot3.classList.add('pov-gd-flash');
      setTimeout(() => dot3.classList.remove('pov-gd-flash'), 350);
    }
  }
  // Depth fills
  const dbid = document.getElementById('pov-dbid');
  const dask = document.getElementById('pov-dask');
  if (dbid) { dbid.style.width = `${Math.min(100,d.bid_pct||0).toFixed(0)}%`; dbid.style.opacity = isL && (d.bid_pct||0) >= 55 ? '0.75' : '0.2'; }
  if (dask) { dask.style.width = `${Math.min(100,d.ask_pct||0).toFixed(0)}%`; dask.style.opacity = !isL && (d.ask_pct||0) >= 55 ? '0.75' : '0.2'; }
  const gv3 = document.getElementById('pov-gv-3');
  if (gv3) { gv3.textContent = `${(isL ? d.bid_pct : d.ask_pct || 0).toFixed(0)}%`; }

  // Trade age timer
  const ageEl = document.getElementById('pov-age');
  if (ageEl && state === 'IN_TRADE' && trade) {
    const el = trade.elapsed_s || 0;
    ageEl.textContent = el < 3600 ? `${Math.floor(el/60)}m${el%60}s` : `${Math.floor(el/3600)}h${Math.floor((el%3600)/60)}m`;
  }

  // Ruler price dot
  const rdot = document.getElementById('pov-rdot');
  if (rdot) {
    const src = trade || d.alert;
    if (src?.sl_price && src?.entry_price) {
      const sl = src.sl_price, ep = src.entry_price;
      const slD = Math.abs(ep - sl);
      const tp2R = src.tp2_price || (dir === 'LONG' ? ep + slD * 2 : ep - slD * 2);
      const lo   = Math.min(sl, d.price, tp2R) - slD * 0.05;
      const hi   = Math.max(sl, d.price, tp2R) + slD * 0.05;
      const span = hi - lo || 1;
      const p    = Math.min(99, Math.max(1, ((d.price - lo) / span) * 100));
      const col  = (dir === 'LONG' ? d.price >= ep : d.price <= ep) ? '#00e676' : '#ff3d57';
      rdot.style.left       = `${p.toFixed(1)}%`;
      rdot.style.background = col;
    }
  }

  // Stale bar drain
  const sfill = document.getElementById('pov-sfill');
  if (sfill && state === 'READY') {
    const age = d.alert_age_seconds || 0;
    const pct = Math.max(0, Math.min(100, 100 - (age / 600) * 100));
    const col = d.alert_state === 'STALE' ? '#ff3d57' : d.alert_state === 'AGING' ? '#ffaa00' : '#00e676';
    sfill.style.width      = `${pct.toFixed(1)}%`;
    sfill.style.background = col;
  }

  // ADX
  const adxEl = document.getElementById('pov-adx');
  if (adxEl) {
    adxEl.textContent = (d.adx||0).toFixed(1);
    adxEl.style.color = (d.adx||0) >= 50 ? '#00e676' : (d.adx||0) >= 25 ? '#ffaa00' : '#fff';
  }

  // Scan rows (updated each poll)
  const scanEl = document.getElementById('pov-scans');
  if (scanEl) scanEl.innerHTML = _ovScanRowsHtml(d.last_scan_summaries);

  // Actions
  const actEl = document.getElementById('pov-actions');
  if (actEl) actEl.innerHTML = _ovActionsHtml(d, state, dir, trade);

  // Session halt / large SL CD live refresh
  const _ovHaltEl = document.getElementById('pov-halt-info');
  const _ovCdEl   = document.getElementById('pov-cd-rem');
  if (_ovCdEl) {
    const _rem = (dir === 'LONG' ? d.large_sl_cooldown_long_remaining : d.large_sl_cooldown_short_remaining) || 0;
    if (_rem > 0) {
      const _m = Math.floor(_rem / 60), _s = _rem % 60;
      _ovCdEl.textContent = `⏳ 90 min cooldown: ${_m}m${_s}s remaining`;
    } else {
      _ovCdEl.textContent = '';
    }
  }
  _ovPrevGates = curGates;
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

// Injected styles for session halt + large SL CD pills and RESET SESSION button
(function _injectStyles() {
  const id = 'bounce-extra-styles';
  if (document.getElementById(id)) return;
  const s = document.createElement('style');
  s.id = id;
  s.textContent = [
    '.pill-halted{background:rgba(255,68,68,0.12);color:#ff4444;border:1px solid rgba(255,68,68,0.4);border-radius:4px;font-size:8px;padding:2px 6px;font-family:\'JetBrains Mono\',monospace;font-weight:700}',
    '.pill-cd-large{background:rgba(255,170,0,0.12);color:#ffaa00;border:1px solid rgba(255,170,0,0.4);border-radius:4px;font-size:8px;padding:2px 6px;font-family:\'JetBrains Mono\',monospace;font-weight:700}',
    '.reset-session-btn{background:transparent;border:1px solid #ffaa00;border-radius:5px;color:#ffaa00;font-family:\'JetBrains Mono\',monospace;font-size:9px;font-weight:700;padding:3px 8px;cursor:pointer;letter-spacing:0.06em;margin-left:6px;vertical-align:middle}',
    '.reset-session-btn:hover{background:rgba(255,170,0,0.1)}',
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
