import asyncio
import time
import logging
from datetime import datetime, timezone
from typing import Optional

from config import (
    PAIRS, J15M_SHORT_GATE, J15M_LONG_GATE, J1H_SHORT_MIN, J1H_LONG_MAX,
    RSI15M_SHORT_MIN, RSI15M_LONG_MAX, DEPTH_GATE_PCT, ATR_SL_MULTIPLIER,
    TP1_R, TP2_R, LEVERAGE_HIGH, LEVERAGE_MID, LEVERAGE_LOW,
    COOLDOWN_SECONDS, ADX_FADE_MAX, PAPER_MODE, CONSECUTIVE_LOSS_STOP,
    MIN_SL_PCT, MIN_SL_PCT_DEFAULT, MARGIN_PER_TRADE,
)

log = logging.getLogger("scanner")

# ── Module-level state ────────────────────────────────────────────────────────

_last_scores: dict[str, int]   = {}   # keyed "BTCSHORT" / "BTCLONG"
_last_stoch:  dict[str, tuple] = {}   # keyed symbol -> (stoch_k, stoch_d) from previous scan
_last_stoch_fast: dict[str, tuple] = {}   # keyed symbol -> (stoch_k_fast, stoch_d_fast) 8,3,3
_cooldowns:   dict[str, float] = {}   # keyed "BTCSHORT" / "BTCLONG" → expiry ts
_scan_count:  int              = 0
_pending:     dict[str, dict]  = {}   # first-scan confirmed, awaiting 2nd
_stale_prices: set[str]        = set()  # symbols with 2 consecutive missing prices
_btc_j1h: float = 50.0   # cached BTC J1H — updated each scan when BTC is processed

BTC_CORRELATION: dict[str, float] = {
    "ETH": 0.94, "SOL": 0.86, "XRP": 0.84, "DOGE": 0.87,
    "LINK": 0.82, "AVAX": 0.80, "SUI": 0.82, "NEAR": 0.78,
    "WIF": 0.65, "HYPE": 0.50, "ZEC": 0.40,
}


# ── Indicator helpers ─────────────────────────────────────────────────────────

def _compute_kdj(candles: list[dict], period: int = 9) -> tuple[float, float, float]:
    if len(candles) < period:
        return 50.0, 50.0, 50.0
    closes = [c["close"] for c in candles]
    highs  = [c["high"]  for c in candles]
    lows   = [c["low"]   for c in candles]
    K, D = 50.0, 50.0
    for i in range(len(closes)):
        if i < period - 1:
            continue
        h_n = max(highs[i - period + 1 : i + 1])
        l_n = min(lows[i  - period + 1 : i + 1])
        rsv = (closes[i] - l_n) / (h_n - l_n) * 100 if h_n != l_n else 50.0
        K   = 2 / 3 * K + 1 / 3 * rsv
        D   = 2 / 3 * D + 1 / 3 * K
    J = 3 * K - 2 * D
    return K, D, J


def _compute_rsi(candles: list[dict], period: int = 14) -> float:
    closes = [c["close"] for c in candles]
    if len(closes) < period + 1:
        return 50.0
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(0.0, d))
        losses.append(max(0.0, -d))
    avg_g = sum(gains[:period]) / period
    avg_l = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_g = (avg_g * (period - 1) + gains[i]) / period
        avg_l = (avg_l * (period - 1) + losses[i]) / period
    if avg_l == 0:
        return 100.0
    return 100 - 100 / (1 + avg_g / avg_l)


def _compute_stochastic(candles: list[dict], k_period: int = 14, slow_period: int = 3, d_period: int = 3) -> tuple[float, float]:
    """Standard slow stochastic 14,3,3. Returns (%K slow, %D)."""
    if len(candles) < k_period:
        return 50.0, 50.0
    closes = [c["close"] for c in candles]
    highs  = [c["high"]  for c in candles]
    lows   = [c["low"]   for c in candles]
    k_raws = []
    for i in range(k_period - 1, len(candles)):
        h_n   = max(highs[i - k_period + 1 : i + 1])
        l_n   = min(lows[i  - k_period + 1 : i + 1])
        k_raw = (closes[i] - l_n) / (h_n - l_n) * 100 if h_n != l_n else 50.0
        k_raws.append(k_raw)
    if len(k_raws) < slow_period:
        return 50.0, 50.0
    k_slows = []
    for i in range(slow_period - 1, len(k_raws)):
        k_slows.append(sum(k_raws[i - slow_period + 1 : i + 1]) / slow_period)
    if len(k_slows) < d_period:
        return (round(k_slows[-1], 2) if k_slows else 50.0), 50.0
    return round(k_slows[-1], 2), round(sum(k_slows[-d_period:]) / d_period, 2)


def _compute_atr(candles: list[dict], period: int = 14) -> float:
    if len(candles) < 2:
        return 0.0
    trs = []
    for i in range(1, len(candles)):
        h, l, pc = candles[i]["high"], candles[i]["low"], candles[i - 1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    if not trs:
        return 0.0
    atr = sum(trs[:period]) / min(period, len(trs))
    for i in range(period, len(trs)):
        atr = (atr * (period - 1) + trs[i]) / period
    return atr


def _compute_adx(candles: list[dict], period: int = 14) -> float:
    if len(candles) < period + 1:
        return 0.0
    plus_dms, minus_dms, trs = [], [], []
    for i in range(1, len(candles)):
        up   = candles[i]["high"]  - candles[i - 1]["high"]
        down = candles[i - 1]["low"] - candles[i]["low"]
        plus_dms.append(max(0.0, up)   if up   > down else 0.0)
        minus_dms.append(max(0.0, down) if down > up   else 0.0)
        h, l, pc = candles[i]["high"], candles[i]["low"], candles[i - 1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    if len(trs) < period:
        return 0.0
    atr_s  = sum(trs[:period])
    pdm_s  = sum(plus_dms[:period])
    mdm_s  = sum(minus_dms[:period])
    dxs    = []
    for i in range(period, len(trs)):
        atr_s  = atr_s  - atr_s  / period + trs[i]
        pdm_s  = pdm_s  - pdm_s  / period + plus_dms[i]
        mdm_s  = mdm_s  - mdm_s  / period + minus_dms[i]
        if atr_s == 0:
            continue
        pdi = pdm_s / atr_s * 100
        mdi = mdm_s / atr_s * 100
        dxs.append(abs(pdi - mdi) / (pdi + mdi) * 100 if pdi + mdi else 0.0)
    if not dxs:
        return 0.0
    adx = sum(dxs[:period]) / min(period, len(dxs))
    for dx in dxs[period:]:
        adx = (adx * (period - 1) + dx) / period
    return adx


def _compute_ma(candles: list[dict], period: int) -> Optional[float]:
    closes = [c["close"] for c in candles]
    if len(closes) < period:
        return None
    return sum(closes[-period:]) / period


def _trend_from_ma(price: float, candles_5m: list, candles_15m: list, candles_1h: list, adx_1h: float = 0.0) -> str:
    """1H-anchored trend: 1H is primary anchor; 15M/5M add conviction but cannot override."""
    def _vote(candles: list, p: float) -> str:
        ma5  = _compute_ma(candles, 5)
        ma10 = _compute_ma(candles, 10)
        ma30 = _compute_ma(candles, 30)
        if ma5 and ma10 and ma30:
            if ma5 > ma10 > ma30 and p > ma10:
                return "BULL"
            if ma5 < ma10 < ma30 and p < ma10:
                return "BEAR"
        return "NEUTRAL"
    v5m  = _vote(candles_5m,  price)
    v15m = _vote(candles_15m, price)
    v1h  = _vote(candles_1h,  price)
    if v1h == "BEAR" and v15m == "BEAR" and v5m == "BEAR" and adx_1h >= 25:
        return "Strong Bear"
    if v1h == "BEAR" and v15m == "BEAR":
        return "Strong Bear"
    if v1h == "BEAR":
        return "Bearish"
    if v1h == "BULL" and v15m == "BULL" and v5m == "BULL" and adx_1h >= 25:
        return "Strong Bull"
    if v1h == "BULL" and v15m == "BULL":
        return "Strong Bull"
    if v1h == "BULL":
        return "Bullish"
    if v15m == "BEAR" and v5m == "BEAR":
        return "Bearish"
    if v15m == "BULL" and v5m == "BULL":
        return "Bullish"
    return "Neutral"


def _depth_pcts(book: dict) -> tuple[float, float]:
    bids = book.get("bids", [])
    asks = book.get("asks", [])
    bid_vol = sum(b["sz"] for b in bids)
    ask_vol = sum(a["sz"] for a in asks)
    total   = bid_vol + ask_vol
    if total == 0:
        return 50.0, 50.0
    return round(bid_vol / total * 100, 1), round(ask_vol / total * 100, 1)


# ── Scoring ───────────────────────────────────────────────────────────────────

def _leverage_tier(adx: float) -> tuple[str, int]:
    if adx >= 50:
        return "HIGH_PROB", LEVERAGE_HIGH
    if adx >= 25:
        return "STRONG", LEVERAGE_MID
    return "REGULAR", LEVERAGE_LOW


def score_bounce_short(j15m, j1h, rsi15m, ask_pct, adx,
                       j5m: float = 50.0, trend: str = "Neutral",
                       stoch_k: float = 50.0, stoch_d: float = 50.0,
                       stoch_k_prev: float = 50.0, stoch_d_prev: float = 50.0) -> tuple[int, str, int]:
    tier, lev = _leverage_tier(adx)
    stoch_gate = stoch_k > 75 and stoch_k < stoch_d and stoch_k_prev >= stoch_d_prev
    if not (j15m > J15M_SHORT_GATE and j1h > J1H_SHORT_MIN
            and stoch_gate and ask_pct >= DEPTH_GATE_PCT):
        return 0, tier, lev
    score = 4
    if j5m  > 80:              score += 2
    if trend == "Strong Bear": score += 2
    if adx  >= 40:             score += 2
    if score >= 10:
        lev, tier = min(25, max(lev, 10)), "HIGH_CONVICTION"
    return score, tier, lev


def score_bounce_long(j15m, j1h, rsi15m, bid_pct, adx,
                      j5m: float = 50.0, trend: str = "Neutral",
                      stoch_k: float = 50.0, stoch_d: float = 50.0,
                      stoch_k_prev: float = 50.0, stoch_d_prev: float = 50.0) -> tuple[int, str, int]:
    tier, lev = _leverage_tier(adx)
    stoch_gate = stoch_k < 25 and stoch_k > stoch_d and stoch_k_prev <= stoch_d_prev
    if not (j15m < J15M_LONG_GATE and j1h < J1H_LONG_MAX
            and stoch_gate and bid_pct >= DEPTH_GATE_PCT):
        return 0, tier, lev
    score = 4
    if j5m  < 20:              score += 2
    if trend == "Strong Bull": score += 2
    if adx  >= 40:             score += 2
    if score >= 10:
        lev, tier = min(25, max(lev, 10)), "HIGH_CONVICTION"
    return score, tier, lev


# ── Cooldown helpers ──────────────────────────────────────────────────────────

def set_close_cooldown(symbol: str, direction: str):
    _cooldowns[f"{symbol}{direction}"] = time.time() + COOLDOWN_SECONDS


def get_cooldown_remaining(symbol: str, direction: str) -> int:
    exp = _cooldowns.get(f"{symbol}{direction}", 0)
    return max(0, int(exp - time.time()))


def clear_cooldown(symbol: str, direction: str):
    _cooldowns.pop(f"{symbol}{direction}", None)


def get_pending() -> dict:
    return dict(_pending)


def get_scan_count() -> int:
    return _scan_count


def clear_all_scanner_state():
    global _scan_count
    _last_scores.clear()
    _last_stoch.clear()
    _last_stoch_fast.clear()
    _cooldowns.clear()
    _pending.clear()
    _scan_count = 0


def get_session_name() -> str:
    """Return current trading session name based on UTC hour."""
    h = datetime.now(timezone.utc).hour
    if h >= 17 or h < 8:  return "ASIA"
    if 8  <= h < 12:       return "EU"
    if 12 <= h < 17:       return "US"
    return "OFF"


def get_session_sl_buffer() -> float:
    """Additional SL buffer (fraction of price) added by session."""
    s = get_session_name()
    if s == "ASIA": return 0.003
    if s == "EU":   return 0.001
    return 0.0


def compute_market_health(pair_states: list[dict], recent_trades: list[dict]) -> dict:
    """Aggregate market-wide health; returns RUN/CAUTION/HALT per direction."""
    total = len(pair_states)
    if total == 0:
        return {
            "short_status": "CAUTION", "long_status": "CAUTION",
            "bear_count": 0, "bull_count": 0, "total": 0,
            "bear_ratio": 0.0, "bull_ratio": 0.0,
            "avg_adx": 0.0, "avg_j5": 50.0, "sl_rate": 0.0,
        }
    bear_count = sum(1 for s in pair_states if s.get("trend") in ("Bearish", "Strong Bear"))
    bull_count = sum(1 for s in pair_states if s.get("trend") in ("Bullish", "Strong Bull"))
    bear_ratio = bear_count / total
    bull_ratio = bull_count / total
    adx_vals   = [s["adx1h"] for s in pair_states if s.get("adx1h") is not None]
    j5_vals    = [s["j5m"]   for s in pair_states if s.get("j5m")  is not None]
    avg_adx    = sum(adx_vals) / len(adx_vals) if adx_vals else 0.0
    avg_j5     = sum(j5_vals)  / len(j5_vals)  if j5_vals  else 50.0
    recent6    = [t for t in recent_trades
                  if (t.get("close_reason") or t.get("exit_reason"))][-6:]
    sl_rate    = (
        sum(1 for t in recent6
            if (t.get("close_reason") or t.get("exit_reason") or "").upper().startswith("SL"))
        / len(recent6)
    ) if recent6 else 0.0
    if bear_ratio >= 0.6 and avg_adx >= 35 and avg_j5 <= 70 and sl_rate < 0.4:
        short_status = "RUN"
    elif bear_ratio < 0.3 or sl_rate >= 0.6 or (avg_j5 >= 85 and bear_ratio < 0.5):
        short_status = "HALT"
    else:
        short_status = "CAUTION"
    if bull_ratio >= 0.6 and avg_adx >= 35 and avg_j5 >= 30 and sl_rate < 0.4:
        long_status = "RUN"
    elif bull_ratio < 0.3 or sl_rate >= 0.6 or (avg_j5 <= 15 and bull_ratio < 0.5):
        long_status = "HALT"
    else:
        long_status = "CAUTION"
    return {
        "short_status": short_status,
        "long_status":  long_status,
        "bear_count":   bear_count,
        "bull_count":   bull_count,
        "total":        total,
        "bear_ratio":   round(bear_ratio, 3),
        "bull_ratio":   round(bull_ratio, 3),
        "avg_adx":      round(avg_adx, 1),
        "avg_j5":       round(avg_j5, 1),
        "sl_rate":      round(sl_rate, 3),
    }


# ── Main scan ─────────────────────────────────────────────────────────────────

async def run_full_scan(hl_client, market_health: Optional[dict] = None) -> list[dict]:
    global _scan_count

    _scan_count += 1
    new_alerts: list[dict] = []

    for symbol in PAIRS:
        try:
            await asyncio.sleep(0.5)  # rate-limit spacing — 12 pairs × 0.5s = 6s minimum spread
            candles_5m, candles_15m, candles_1h, book, price = await _fetch_pair_data(hl_client, symbol)

            if not price or price == 0:
                log.warning(f"[SCAN] {symbol} — no price, retrying in 2s...")
                await asyncio.sleep(2)
                price = await hl_client.get_price(symbol)
                if not price or price == 0:
                    log.warning(f"[PRICE STALE] {symbol} — two consecutive no price responses")
                    _stale_prices.add(symbol)
                    continue
                _stale_prices.discard(symbol)
            else:
                _stale_prices.discard(symbol)

            # ── Indicators ────────────────────────────────────────────────────
            _, _, j5m  = _compute_kdj(candles_5m)
            _, _, j15m = _compute_kdj(candles_15m)
            _, _, j1h  = _compute_kdj(candles_1h)
            rsi15m     = _compute_rsi(candles_15m)
            rsi1h      = _compute_rsi(candles_1h)
            stoch_k, stoch_d           = _compute_stochastic(candles_15m)
            stoch_k_prev, stoch_d_prev = _last_stoch.get(symbol, (50.0, 50.0))
            _last_stoch[symbol]        = (stoch_k, stoch_d)
            stoch_k_fast, stoch_d_fast           = _compute_stochastic(candles_15m, k_period=8)
            stoch_k_prev_fast, stoch_d_prev_fast = _last_stoch_fast.get(symbol, (50.0, 50.0))
            _last_stoch_fast[symbol]             = (stoch_k_fast, stoch_d_fast)
            print(f"[STOCH FAST] {symbol} K={stoch_k_fast:.1f} D={stoch_d_fast:.1f} prev_K={stoch_k_prev_fast:.1f}")
            atr5m      = _compute_atr(candles_5m)
            atr15m     = _compute_atr(candles_15m)
            atr1h      = _compute_atr(candles_1h)
            adx1h      = _compute_adx(candles_1h)
            ma10       = _compute_ma(candles_1h, 10)
            ma30       = _compute_ma(candles_1h, 30)
            ma60       = _compute_ma(candles_1h, 60)
            trend      = _trend_from_ma(price, candles_5m, candles_15m, candles_1h, adx1h)
            if symbol == "BTC":
                global _btc_j1h
                _btc_j1h = j1h
            bid_pct, ask_pct = _depth_pcts(book)

            vol_15m    = candles_15m[-1]["volume"] if candles_15m else 0
            vol_ma15m  = (sum(c["volume"] for c in candles_15m[-10:]) / min(10, len(candles_15m))
                          if candles_15m else 0)

            # ── SL distance (ATR base, floored by MIN_SL_PCT + session buffer) ───────────────────────────────────────────────────
            _sl_atr      = atr15m * ATR_SL_MULTIPLIER
            _min_sl_pct  = MIN_SL_PCT.get(symbol, MIN_SL_PCT_DEFAULT)
            _sess_buf    = get_session_sl_buffer()
            _min_sl_dist = price * (_min_sl_pct + _sess_buf)
            sl_dist      = max(_sl_atr, _min_sl_dist)

            # ── BTC regime gate ───────────────────────────────────────────────
            _sym_base          = symbol.replace("_USDT", "").replace("USDT", "")
            _pair_corr         = BTC_CORRELATION.get(_sym_base, 0.75)
            _regime_block_short = False
            _regime_block_long  = False
            if _pair_corr >= 0.65:
                if _btc_j1h < 20:
                    _regime_block_short = True
                elif 40 <= _btc_j1h <= 60:
                    _regime_block_short = True
                    _regime_block_long  = True
                elif _btc_j1h > 80:
                    _regime_block_long  = True

            # ── Score both directions ─────────────────────────────────────────
            for direction in ("SHORT", "LONG"):
                key = f"{symbol}{direction}"

                if get_cooldown_remaining(symbol, direction) > 0:
                    continue

                if direction == "SHORT":
                    if _regime_block_short:
                        log.info(f"[REGIME] {symbol} SHORT blocked — BTC J1H={_btc_j1h:.1f} corr={_pair_corr}")
                        _last_scores[key] = 0
                        _pending.pop(key, None)
                        continue
                    g_j15m  = j15m > J15M_SHORT_GATE
                    g_j1h   = j1h  > J1H_SHORT_MIN
                    g_stoch = stoch_k > 75 and stoch_k < stoch_d

                    g_depth = ask_pct >= DEPTH_GATE_PCT
                    score, tier, lev = score_bounce_short(
                        j15m, j1h, rsi15m, ask_pct, adx1h, j5m=j5m, trend=trend,
                        stoch_k=stoch_k, stoch_d=stoch_d,
                        stoch_k_prev=stoch_k_prev, stoch_d_prev=stoch_d_prev)
                    log_gates = (f"j15m={j15m:.1f}(need>{J15M_SHORT_GATE}) "
                                 f"j1h={j1h:.1f}(need>{J1H_SHORT_MIN}) "
                                 f"stoch_k={stoch_k:.1f}/stoch_d={stoch_d:.1f}(need>75,k<d) "
                                 f"ask={ask_pct:.1f}%(need>={DEPTH_GATE_PCT}%)")
                else:
                    if _regime_block_long:
                        log.info(f"[REGIME] {symbol} LONG blocked — BTC J1H={_btc_j1h:.1f} corr={_pair_corr}")
                        _last_scores[key] = 0
                        _pending.pop(key, None)
                        continue
                    g_j15m  = j15m < J15M_LONG_GATE
                    g_j1h   = j1h  < J1H_LONG_MAX
                    g_stoch = stoch_k < 25 and stoch_k > stoch_d

                    g_depth = bid_pct >= DEPTH_GATE_PCT
                    score, tier, lev = score_bounce_long(
                        j15m, j1h, rsi15m, bid_pct, adx1h, j5m=j5m, trend=trend,
                        stoch_k=stoch_k, stoch_d=stoch_d,
                        stoch_k_prev=stoch_k_prev, stoch_d_prev=stoch_d_prev)
                    log_gates = (f"j15m={j15m:.1f}(need<{J15M_LONG_GATE}) "
                                 f"j1h={j1h:.1f}(need<{J1H_LONG_MAX}) "
                                 f"stoch_k={stoch_k:.1f}/stoch_d={stoch_d:.1f}(need<25,k>d) "
                                 f"bid={bid_pct:.1f}%(need>={DEPTH_GATE_PCT}%)")

                # ── GATE3 log — every scan when >= 3 of 4 gates pass ────────────
                _gate_list  = [g_j15m, g_j1h, g_stoch, g_depth]
                _gate_count = sum(_gate_list)
                _blocked    = [n for n, v in zip(["J15M", "J1H", "STOCH", "DEPTH"], _gate_list) if not v]
                if _gate_count >= 3:
                    log.info(
                        f"[GATE3] {symbol} {direction} "
                        f"stoch_k={stoch_k:.1f} stoch_d={stoch_d:.1f} rsi={rsi15m:.1f} "
                        f"passed={'true' if _gate_count == 4 else 'false'} "
                        f"blocked_gates={_blocked}"
                    )

                if score >= 4:
                    log.info(f"[SCORE] {symbol} {direction} gates=PASS score={score} {log_gates}")
                else:
                    if _last_scores.get(key, 0) >= 4:
                        log.info(f"[SCORE] {symbol} {direction} score=0 {log_gates}")
                    _last_scores[key] = 0
                    _pending.pop(key, None)
                    continue

                # Consecutive scan confirmation
                if _last_scores.get(key, 0) < 4:
                    _last_scores[key] = score
                    _pending[key] = {
                        "symbol": symbol, "direction": direction,
                        "score": score, "tier": tier,
                    }
                    log.info(f"[SCORE] {symbol} {direction} first-scan confirmed — awaiting 2nd")
                    continue

                # Second consecutive scan — check ADX fade-max before emitting alert
                _last_scores[key] = score

                if adx1h > ADX_FADE_MAX:
                    log.info(f"[SKIP] {symbol} {direction} adx={adx1h:.1f} exceeds fade max {ADX_FADE_MAX} — trend too strong to fade")
                    continue

                # Compute SL / TP prices (HC score-10: 2.5R TP1, 3.5R TP2)
                is_hc = score >= 10
                if direction == "SHORT":
                    sl_price = round(price + sl_dist, 6)
                    if is_hc:
                        partial_price = round(price - sl_dist * 1.5, 6)
                        tp1_price     = round(price - sl_dist * 2.5, 6)
                        tp2_price     = round(price - sl_dist * 3.5, 6)
                    else:
                        partial_price = None
                        tp1_price     = round(price - sl_dist * TP1_R, 6)
                        tp2_price     = round(price - sl_dist * TP2_R, 6)
                else:
                    sl_price = round(price - sl_dist, 6)
                    if is_hc:
                        partial_price = round(price + sl_dist * 1.5, 6)
                        tp1_price     = round(price + sl_dist * 2.5, 6)
                        tp2_price     = round(price + sl_dist * 3.5, 6)
                    else:
                        partial_price = None
                        tp1_price     = round(price + sl_dist * TP1_R, 6)
                        tp2_price     = round(price + sl_dist * TP2_R, 6)

                dollar_risk = round(
                    2000.0 * lev * (sl_dist / price) if price else 0, 2
                )

                alert = {
                    "symbol":       symbol,
                    "direction":    direction,
                    "score":        score,
                    "tier":         tier,
                    "leverage":     lev,
                    "entry_price":  price,
                    "sl_price":     sl_price,
                    "sl_dist":      round(sl_dist, 6),
                    "tp1_price":    tp1_price,
                    "tp2_price":    tp2_price,
                    "dollar_risk":  dollar_risk,
                    "j15m":         round(j15m, 2),
                    "j1h":          round(j1h, 2),
                    "j5m":          round(j5m, 2),
                    "rsi15m":       round(rsi15m, 2),
                    "stoch_k":      round(stoch_k, 2),
                    "stoch_d":      round(stoch_d, 2),
                    "rsi1h":        round(rsi1h, 2),
                    "atr15m":       round(atr15m, 6),
                    "adx1h":        round(adx1h, 2),
                    "bid_pct":      bid_pct,
                    "ask_pct":      ask_pct,
                    "trend":        trend,
                    "ma10":         round(ma10, 6) if ma10 else None,
                    "ma30":         round(ma30, 6) if ma30 else None,
                    "ma60":         round(ma60, 6) if ma60 else None,
                    "fired_at":     int(time.time()),
                    "is_in_trade":   False,
                    "is_score10":    is_hc,
                    "margin":        MARGIN_PER_TRADE * 2 if is_hc else MARGIN_PER_TRADE,
                    "partial_price": partial_price,
                    "session":       get_session_name(),
                }
                new_alerts.append(alert)
                _pending.pop(key, None)
                log.info(f"[ALERT] {symbol} {direction} tier={tier} lev={lev}x entry={price} "
                         f"sl={sl_price} tp1={tp1_price} adx={adx1h:.1f} "
                         f"stoch_k={stoch_k:.1f} stoch_d={stoch_d:.1f} rsi={rsi15m:.1f}")

        except Exception as e:
            log.error(f"[SCAN] {symbol} error: {e}", exc_info=True)

    log.info(f"[SCAN] #{_scan_count} complete — {len(new_alerts)} new alerts")
    return new_alerts


async def scan_pair_state(hl_client) -> list[dict]:
    """Return lightweight per-pair indicator state for the dashboard grid."""
    states = []
    for symbol in PAIRS:
        try:
            await asyncio.sleep(0.5)  # rate-limit spacing between pairs
            candles_5m, candles_15m, candles_1h, book, price = await _fetch_pair_data(hl_client, symbol)
            if not price:
                states.append({"symbol": symbol, "price": 0})
                continue

            _, _, j5m  = _compute_kdj(candles_5m)
            _, _, j15m = _compute_kdj(candles_15m)
            _, _, j1h  = _compute_kdj(candles_1h)
            rsi15m     = _compute_rsi(candles_15m)
            rsi1h      = _compute_rsi(candles_1h)
            stoch_k, stoch_d           = _compute_stochastic(candles_15m)
            stoch_k_prev, stoch_d_prev = _last_stoch.get(symbol, (50.0, 50.0))
            _last_stoch[symbol]        = (stoch_k, stoch_d)
            stoch_k_fast, stoch_d_fast           = _compute_stochastic(candles_15m, k_period=8)
            stoch_k_prev_fast, stoch_d_prev_fast = _last_stoch_fast.get(symbol, (50.0, 50.0))
            _last_stoch_fast[symbol]             = (stoch_k_fast, stoch_d_fast)
            print(f"[STOCH FAST] {symbol} K={stoch_k_fast:.1f} D={stoch_d_fast:.1f} prev_K={stoch_k_prev_fast:.1f}")
            atr15m     = _compute_atr(candles_15m)
            adx1h      = _compute_adx(candles_1h)
            ma10       = _compute_ma(candles_1h, 10)
            ma30       = _compute_ma(candles_1h, 30)
            ma60       = _compute_ma(candles_1h, 60)
            trend      = _trend_from_ma(price, candles_5m, candles_15m, candles_1h, adx1h)
            bid_pct, ask_pct = _depth_pcts(book)

            short_score, short_tier, short_lev = score_bounce_short(
                j15m, j1h, rsi15m, ask_pct, adx1h, j5m=j5m, trend=trend,
                stoch_k=stoch_k, stoch_d=stoch_d,
                stoch_k_prev=stoch_k_prev, stoch_d_prev=stoch_d_prev)
            long_score,  long_tier,  long_lev  = score_bounce_long(
                j15m, j1h, rsi15m, bid_pct, adx1h, j5m=j5m, trend=trend,
                stoch_k=stoch_k, stoch_d=stoch_d,
                stoch_k_prev=stoch_k_prev, stoch_d_prev=stoch_d_prev)

            states.append({
                "symbol":      symbol,
                "price":       price,
                "j5m":         round(j5m, 2),
                "j15m":        round(j15m, 2),
                "j1h":         round(j1h, 2),
                "rsi15m":      round(rsi15m, 2),
                "stoch_k":     round(stoch_k, 2),
                "stoch_d":     round(stoch_d, 2),
                "stoch_k_prev": round(stoch_k_prev, 2),
                "stoch_d_prev": round(stoch_d_prev, 2),
                "stoch_k_fast":      round(stoch_k_fast, 2),
                "stoch_d_fast":      round(stoch_d_fast, 2),
                "stoch_k_prev_fast": round(stoch_k_prev_fast, 2),
                "stoch_d_prev_fast": round(stoch_d_prev_fast, 2),
                "rsi1h":       round(rsi1h, 2),
                "atr15m":      round(atr15m, 6),
                "adx1h":       round(adx1h, 2),
                "bid_pct":     bid_pct,
                "ask_pct":     ask_pct,
                "trend":       trend,
                "ma10":        round(ma10, 6) if ma10 else None,
                "ma30":        round(ma30, 6) if ma30 else None,
                "ma60":        round(ma60, 6) if ma60 else None,
                "short_score": short_score,
                "short_tier":  short_tier,
                "long_score":  long_score,
                "long_tier":   long_tier,
                "cooldown_short": get_cooldown_remaining(symbol, "SHORT"),
                "cooldown_long":  get_cooldown_remaining(symbol, "LONG"),
            })
        except Exception as e:
            log.error(f"[STATE] {symbol} error: {e}")
            states.append({"symbol": symbol, "price": 0})
    return states


async def _fetch_pair_data(hl_client, symbol: str):
    candles_5m, candles_15m, candles_1h, book, price = await asyncio.gather(
        hl_client.get_candles(symbol, "5m",  100),
        hl_client.get_candles(symbol, "15m", 100),
        hl_client.get_candles(symbol, "1h",  100),
        hl_client.get_orderbook(symbol, 20),
        hl_client.get_price(symbol),
    )
    return candles_5m, candles_15m, candles_1h, book, price


def log_startup_config():
    log.info(
        f"[CONFIG] PAIRS={len(PAIRS)} "
        f"J15M_SHORT={J15M_SHORT_GATE} J15M_LONG={J15M_LONG_GATE} "
        f"J1H_SHORT_MIN={J1H_SHORT_MIN} J1H_LONG_MAX={J1H_LONG_MAX} "
        f"RSI_SHORT={RSI15M_SHORT_MIN} RSI_LONG={RSI15M_LONG_MAX} "
        f"DEPTH={DEPTH_GATE_PCT}% ATR_SL={ATR_SL_MULTIPLIER}x "
        f"ADX_FADE_MAX={ADX_FADE_MAX} "
        f"COOLDOWN={COOLDOWN_SECONDS//60}min "
        f"CIRCUIT_BREAKER={CONSECUTIVE_LOSS_STOP} PAPER={PAPER_MODE}"
    )
