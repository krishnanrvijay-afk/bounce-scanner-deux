import asyncio
import time
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional
import os as _os

from config import (
    PAIRS, J15M_SHORT_GATE, J15M_LONG_GATE, J1H_SHORT_MIN, J1H_SHORT_MAX, J1H_LONG_MIN, J1H_LONG_MAX,
    RSI15M_SHORT_MIN, RSI15M_LONG_MAX, DEPTH_GATE_PCT, ATR_SL_MULTIPLIER,
    TP1_R, TP1_CLOSE_PCT, TP2_R, LEVERAGE_HIGH, LEVERAGE_MID, LEVERAGE_LOW,
    ADX_MIN_LONG, ADX_MIN_SHORT, PAPER_MODE, CONSECUTIVE_LOSS_STOP,
    MIN_SL_PCT, MIN_SL_PCT_DEFAULT, MARGIN_PER_TRADE,
    J15M_SHORT_CEILING, J15M_LONG_FLOOR,
    PAIR_COOLDOWN_SECONDS,
    KILL_PCT_FLOOR,
    SE_J1H_DECAY_PTS,
    BLOCKED_PAIR_SESSIONS,
)

log = logging.getLogger("scanner")

# -- Supabase gate logging (fire-and-forget, never blocks entry) --
_SB_URL: str = _os.environ.get("SUPABASE_URL", "").rstrip("/")
_SB_KEY: str = _os.environ.get("SUPABASE_KEY", "")

async def _log_gate(venue: str, pair: str, gate_type: str, direction: str, reason: str) -> None:
    if not _SB_URL or not _SB_KEY:
        return
    try:
        import httpx as _httpx
        async with _httpx.AsyncClient(timeout=3.0) as _c:
            await _c.post(
                f"{_SB_URL}/rest/v1/gate_activity_log",
                headers={
                    "apikey": _SB_KEY,
                    "Authorization": f"Bearer {_SB_KEY}",
                    "Content-Type": "application/json",
                    "Prefer": "return=minimal",
                },
                json={"venue": venue, "pair": pair, "gate_type": gate_type,
                      "direction": direction, "reason": reason},
            )
    except Exception:
        pass

# ÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂ Module-level state ÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂ

_last_stoch:  dict[str, tuple] = {}   # keyed symbol -> (stoch_k, stoch_d) from previous scan
_last_stoch_fast: dict[str, tuple] = {}   # keyed symbol -> (stoch_k_fast, stoch_d_fast) 8,3,3
_adverse_cluster: dict = {"long": [], "short": []}  # rolling adverse exit timestamps per direction
_adverse_cooldown_until: dict = {"long": None, "short": None}  # graduated adverse cooldown expiry per direction
_btc_flash_block_until: dict = {"long": None}                  # expiry for BTC 1m flash crash LONG block
_flash_closed: set = set()                                      # trade keys already force-closed this flash event
_btc_flash_tg_pending = [False]                                 # set True when flash arms; cleared in main.py after TG sent
_cooldowns:   dict[str, float] = {}   # keyed "BTCSHORT" / "BTCLONG" ÃÂ¢ÃÂÃÂ expiry ts
_pair_cooldowns: dict           = {}   # keyed symbol -> expiry ts
_scan_count:  int              = 0
_fleet_halt:  bool             = False  # set by fleet-scorecard halt API via Supabase
_stale_prices: set[str]        = set()  # symbols with 2 consecutive missing prices
_stale_counts: dict             = {}     # consecutive no-price count per symbol
_btc_j1h: float = 50.0   # cached BTC J1H ÃÂ¢ÃÂÃÂ updated each scan when BTC is processed
_btc_j1h_history: list = []  # Tracks last 12 BTC J1H values — ~10-15 minutes of macro trend

BTC_CORRELATION: dict[str, float] = {
    "ETH": 0.94, "SOL": 0.86, "XRP": 0.84, "DOGE": 0.87,
    "LINK": 0.82, "AVAX": 0.80, "SUI": 0.82, "NEAR": 0.78,
    "WIF": 0.65, "HYPE": 0.50, "ZEC": 0.40,
    "@107": 0.50,  # HYPE perp on HL
    "@8":   0.40,  # ZEC perp on HL
}


# ÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂ Indicator helpers ÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂ

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


# ÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂ Scoring ÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂ

def _leverage_tier(adx: float) -> tuple[str, int]:
    if adx >= 50:
        return "HIGH_PROB", LEVERAGE_HIGH
    if adx >= 25:
        return "STRONG", LEVERAGE_MID
    return "REGULAR", LEVERAGE_LOW


def score_bounce_short(j15m, j1h, ask_pct, adx,
                       j5m: float = 50.0, trend: str = "Neutral",
                       stoch_k: float = 50.0, stoch_d: float = 50.0) -> tuple[int, str, int]:
    tier, lev = _leverage_tier(adx)
    stoch_gate = (j5m > 88 and j15m > 80)  # R3: j5m floor raised 80->88; 26.7% WR at >80 vs 71% WR at >88
    _bid_pct = 100 - ask_pct
    if not (j15m > J15M_SHORT_GATE
            and stoch_gate
            and _bid_pct <= 65):
        return 0, tier, lev
    score = 4
    if j5m  > 80:              score += 2
    if trend == "Strong Bear": score += 2
    if adx  >= 40:             score += 2
    return score, tier, lev


def score_bounce_long(j15m, j1h, bid_pct, adx,
                      j5m: float = 50.0, trend: str = "Neutral",
                      stoch_k: float = 50.0, stoch_d: float = 50.0) -> tuple[int, str, int]:
    tier, lev = _leverage_tier(adx)
    stoch_gate = (j5m < 20 and j15m < 20)
    if not (j15m < J15M_LONG_GATE
            and stoch_gate
            and bid_pct >= 45):
        return 0, tier, lev
    score = 4
    if j5m  < 20:              score += 2
    if trend == "Strong Bull": score += 2
    if adx  >= 40:             score += 2
    return score, tier, lev


# ÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂ Cooldown helpers ÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂ

def set_close_cooldown(
        symbol: str,
        direction: str,
        seconds: int = None):
    _secs = (seconds if seconds is not None
             else PAIR_COOLDOWN_SECONDS)
    _cooldowns[
        f"{symbol}{direction}"] = (
        time.time() + _secs)


def get_cooldown_remaining(symbol: str, direction: str) -> int:
    exp = _cooldowns.get(f"{symbol}{direction}", 0)
    return max(0, int(exp - time.time()))


def clear_cooldown(symbol: str, direction: str):
    _cooldowns.pop(f"{symbol}{direction}", None)


def get_scan_count() -> int:
    return _scan_count


def clear_all_scanner_state():
    global _scan_count
    _last_stoch.clear()
    _last_stoch_fast.clear()
    _cooldowns.clear()
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


# ÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂ Main scan ÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂ

async def run_full_scan(hl_client, market_health: Optional[dict] = None, open_trades: dict = None) -> list[dict]:
    global _scan_count
    global _fleet_halt
    _open_trades_ref = open_trades if open_trades is not None else {}

    _scan_count += 1
    if _scan_count < 3:
        print(
            f"[WARMUP] scan "
            f"#{_scan_count}/3 — "
            f"KDJ initializing, "
            f"skipping signal "
            f"evaluation")
        return []

    # Fleet halt check
    # Reads from scanner state
    # Takes effect within one
    # scan cycle (~30 seconds)
    if _SB_URL and _SB_KEY:
        try:
            import httpx as _httpx
            async with _httpx.AsyncClient(timeout=2.0) as _hc:
                _fh = await _hc.get(
                    f"{_SB_URL}/rest/v1/hl_scanner_state",
                    params={"select": "fleet_halt",
                            "id": "eq.1",
                            "limit": "1"},
                    headers={"apikey": _SB_KEY,
                             "Authorization": f"Bearer {_SB_KEY}"},
                )
                if _fh.status_code == 200 and _fh.json():
                    _fleet_halt = bool(
                        _fh.json()[0].get(
                            "fleet_halt", False))
        except Exception:
            pass
    if _fleet_halt:
        log.info(
            "[FLEET HALT] active"
            " — signal generation"
            " suspended")
        return []

    new_alerts: list[dict] = []

    for symbol in PAIRS:
        try:
            await asyncio.sleep(0.5)  # rate-limit spacing ÃÂ¢ÃÂÃÂ 12 pairs ÃÂÃÂ 0.5s = 6s minimum spread
            candles_1m, candles_5m, candles_15m, candles_1h, book, price = await _fetch_pair_data(hl_client, symbol)

            if not price or price == 0:
                _stale_counts[symbol] = \
                    _stale_counts.get(symbol, 0) + 1
                if _stale_counts[symbol] >= 5:
                    log.warning(f"[PRICE STALE] {symbol} - "
                        f"{_stale_counts[symbol]} consecutive "
                        f"scans with no price")
                    _stale_prices.add(symbol)
                else:
                    log.warning(f"[PRICE STALE] {symbol} - "
                        f"{_stale_counts[symbol]}/5 "
                        f"consecutive no-price scans")
                if (symbol in _stale_prices and
                        _stale_counts.get(symbol, 0) > 8):
                    log.error(
                        f"[PRICE STALE CRITICAL] {symbol} - "
                        f"{_stale_counts[symbol]} consecutive scan misses "
                        f"with OPEN TRADE — exit monitor staleness "
                        f"refetch (Change 3) should be compensating, "
                        f"verify manually if this persists")
                continue
            _stale_counts[symbol] = 0
            _stale_prices.discard(symbol)

            # Pair cooldown pre-signal check
            if get_pair_cooldown_remaining(
                    symbol) > 0:
                continue

            # ÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂ Indicators ÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂ
            _, _, j5m  = _compute_kdj(candles_1m[:-1])
            _, _, j15m = _compute_kdj(candles_15m[:-1])
            _, _, j1h  = _compute_kdj(candles_1h[:-1])
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
                global _btc_j1h_history
                _btc_j1h_history.append(_btc_j1h)
                if len(_btc_j1h_history) > 12:
                    _btc_j1h_history = \
                        _btc_j1h_history[-12:]
            bid_pct, ask_pct = _depth_pcts(book)

            vol_15m    = candles_15m[-1]["volume"] if candles_15m else 0
            vol_ma15m  = (sum(c["volume"] for c in candles_15m[-10:]) / min(10, len(candles_15m))
                          if candles_15m else 0)

            # ÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂ SL distance (ATR base, floored by MIN_SL_PCT + session buffer) ÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂ
            # R8: dynamic ATR mult — ASIA wider spreads (x1.2), US tightest (x0.9), EU baseline
            _atr_sess    = get_session_name()
            _atr_mult    = (ATR_SL_MULTIPLIER * 1.2 if _atr_sess == "ASIA"
                            else ATR_SL_MULTIPLIER * 0.9 if _atr_sess == "US"
                            else ATR_SL_MULTIPLIER)
            _sl_atr      = atr15m * _atr_mult
            _min_sl_pct  = MIN_SL_PCT.get(symbol, MIN_SL_PCT_DEFAULT)
            _sess_buf    = get_session_sl_buffer()
            _min_sl_dist = price * (_min_sl_pct + _sess_buf)
            sl_dist      = max(_sl_atr, _min_sl_dist)

            # ÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂ BTC regime gate ÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂ
            _sym_base          = symbol.replace("_USDT", "").replace("USDT", "")
            _pair_corr         = BTC_CORRELATION.get(_sym_base, 0.75)
            _regime_block_short = False
            _regime_block_long  = False
            if _btc_j1h > 80.0:
                _btc_regime_context = "LONG_BLOCKED"
            elif _btc_j1h < 20.0:
                _btc_regime_context = "SHORT_BLOCKED"
            elif 40.0 <= _btc_j1h <= 60.0:
                _btc_regime_context = "NEUTRAL_BLOCK"
            else:
                _btc_regime_context = "CLEAR"
            _btc_regime_context = (_btc_regime_context or "UNKNOWN")
            # ÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂ Component A2: BTC 1m flash crash detector ÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂ
            _flash_thresholds = {"ASIA": 0.0030, "EU": 0.0055, "US": 0.0050}
            _cur_session  = get_session_name()
            _flash_thresh = _flash_thresholds.get(_cur_session, 0.0050)
            try:
                _btc_sym = "BTC"
                _btc_1m = await hl_client.get_candles(_btc_sym, "1m", 3)
                if _btc_1m and len(_btc_1m) >= 2:
                    _lc = _btc_1m[-2]
                    _body_pct = abs(
                        float(_lc["close"]) - float(_lc["open"])
                    ) / float(_lc["open"])
                    if (_body_pct >= _flash_thresh and
                            float(_lc["close"]) < float(_lc["open"])):
                        _btc_flash_block_until["long"] = (
                            datetime.now(timezone.utc) + timedelta(minutes=5))
                        _flash_closed.clear()
                        _btc_flash_tg_pending[0] = True
                        _regime_block_long = True
                        asyncio.create_task(_log_gate(
                            "HL", _btc_sym, "BTC_FLASH_BLOCK", "LONG",
                            f"1m body={_body_pct*100:.2f}% "
                            f">= {_flash_thresh*100:.2f}% "
                            f"session={_cur_session} LONGs blocked 5min"))
            except Exception as _fe:
                log.warning(f"[BTC_FLASH] candle error: {_fe}")
            # ÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂ Component B: Adverse cluster directional halt ÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂ
            if len(_adverse_cluster.get("long",  [])) >= 3: _regime_block_long  = True
            if len(_adverse_cluster.get("short", [])) >= 3: _regime_block_short = True
            # -- Graduated adverse cooldown check --------------------------------
            _now_utc = datetime.now(timezone.utc)
            if (_adverse_cooldown_until.get("long") and
                    _now_utc < _adverse_cooldown_until["long"]):
                _regime_block_long = True
            if (_adverse_cooldown_until.get("short") and
                    _now_utc < _adverse_cooldown_until["short"]):
                _regime_block_short = True

            if (_btc_flash_block_until.get("long") and
                    _now_utc < _btc_flash_block_until["long"]):
                _regime_block_long = True
            # ÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂ Score both directions ÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂ
            for direction in ("SHORT", "LONG"):
                key = f"{symbol}{direction}"
                _cur_sess = get_session_name()
                if BLOCKED_PAIR_SESSIONS.get(
                        (symbol, direction, _cur_sess),
                        False):
                    asyncio.create_task(
                        _log_gate(
                            "HL", symbol,
                            "SESSION_BLOCK",
                            direction,
                            f"blocked: {symbol} "
                            f"{direction} "
                            f"{_cur_sess}"))
                    continue
                _cd = get_cooldown_remaining(
                    symbol, direction)
                if _cd > 0:
                    asyncio.create_task(
                        _log_gate(
                            "HL", symbol,
                            "KILL_COOLDOWN",
                            direction,
                            f"post-KILL cooldown "
                            f"{_cd}s remaining"))
                    continue

                # Skip scoring if trade already open — prevents BLOCKED_DUPLICATE noise
                if key in _open_trades_ref:
                    continue

                if direction == "SHORT":
                    if _regime_block_short:
                        log.info(f"[REGIME] {symbol} SHORT blocked ÃÂ¢ÃÂÃÂ BTC J1H={_btc_j1h:.1f} corr={_pair_corr}")
                        continue
                    g_j15m  = j15m > J15M_SHORT_GATE
                    g_j1h   = j1h  > J1H_SHORT_MIN
                    g_stoch = stoch_k > 75 and stoch_k < stoch_d

                    g_depth = ask_pct >= DEPTH_GATE_PCT
                    # BTC macro trend SHORT gate
                    # Block SHORTs when BTC J1H is higher now than 6 scans
                    # ago — macro uptrend active
                    # prevents firing SHORTs into a sustained BTC rally
                    if (direction == "SHORT"
                            and len(_btc_j1h_history) >= 10
                            and _btc_j1h > _btc_j1h_history[-10]):
                        asyncio.create_task(_log_gate(
                            "HL", symbol, "BTC_MACRO_RISE", direction,
                            f"btc_j1h={_btc_j1h:.1f} > "
                            f"{_btc_j1h_history[-10]:.1f} 10 scans ago"))
                        continue
                    # SHORT session/J1H directional gates
                    # Gate 1: SHORT_US_HALT
                    # US session SHORTs: 0% WR, -$1,570 net (3 trades 7/12)
                    # US volume/trend momentum overrides J exhaustion signals
                    if _cur_sess == "US":
                        asyncio.create_task(_log_gate(
                            "HL", symbol, "SHORT_US_HALT", direction,
                            f"US session — 0% WR historical, -$1,570 net"))
                        continue
                    # Gate 2: SHORT_EU_J1H_HIGH
                    # EU + J1H >= 78: 5 losses 1 win, -$1,062 net
                    # High J1H in EU = trend has continuation room; exhaustion reversal fails
                    if _cur_sess == "EU" and j1h >= 78:
                        asyncio.create_task(_log_gate(
                            "HL", symbol, "SHORT_EU_J1H_HIGH", direction,
                            f"EU j1h={j1h:.1f} >= 78 — trend continuation not reversal"))
                        continue
                    # Gate 3b: J15M extreme overbought ceiling
                    # Data: j15m>115 = 0% WR, squeeze risk -- extreme OB can extend
                    if j15m > J15M_SHORT_CEILING:
                        asyncio.create_task(_log_gate(
                            "HL", symbol, "J15M_EXTREME_OB", direction,
                            f"j15m={j15m:.1f} > J15M_SHORT_CEILING={J15M_SHORT_CEILING}"
                            f" -- extreme overbought, squeeze can extend"))
                        continue
                    # Gate 3: SHORT_J1H_FLOOR
                    # J1H < 45: shorting into 1H oversold — no reversal context
                    # Evidence: 7/12 AVAX EU j1h=35 -$129, 3L_HIGHER_LOW in 7min
                    if j1h < 45:
                        asyncio.create_task(_log_gate(
                            "HL", symbol, "SHORT_J1H_FLOOR", direction,
                            f"j1h={j1h:.1f} < 45 — 1H oversold, no reversal context"))
                        continue
                    # MA-stack gate for SHORTs
                    # Data: 3L_HIGHER_LOW 20 trades 0% WR -$744, all in BULL ma_stack
                    # Block SHORTs when 1H trend structure is bullish aligned
                    _ma_stack_now = (
                        "BULL" if (ma10 and ma30 and ma60 and ma10 > ma30 > ma60) else
                        "BEAR" if (ma10 and ma30 and ma60 and ma10 < ma30 < ma60)
                        else "MIXED"
                    )
                    if _ma_stack_now == "BULL":
                        asyncio.create_task(_log_gate(
                            "HL", symbol, "MA_STACK_BULL_BLOCK", direction,
                            f"ma10={ma10:.4f} ma30={ma30:.4f} ma60={ma60:.4f}"
                            f" -- 1H uptrend, SHORT reversal invalid"))
                        continue
                    # J1H ceiling gate (enforced) — blocks SHORTs above valid bounce zone
                    # Lower bound (j1h<=30) removed: SHORT_J1H_FLOOR (j1h<45) already covers it
                    if j1h >= J1H_SHORT_MAX:
                        asyncio.create_task(_log_gate(
                            "HL", symbol, "J1H_RANGE_FAIL", direction,
                            f"j1h={j1h:.1f} >= J1H_SHORT_MAX={J1H_SHORT_MAX}"))
                        continue
                    # RSI floor gate (enforced) — blocks SHORTs when 15m RSI approaching oversold
                    if rsi15m <= RSI15M_SHORT_MIN:
                        asyncio.create_task(_log_gate(
                            "HL", symbol, "RSI_FLOOR_FAIL", direction,
                            f"rsi15m={rsi15m:.1f} need>{RSI15M_SHORT_MIN}"))
                        continue
                    score, tier, lev = score_bounce_short(
                        j15m, j1h, ask_pct, adx1h, j5m=j5m, trend=trend,
                        stoch_k=stoch_k_fast, stoch_d=stoch_d_fast)
                    _bid_from_ask = 100 - ask_pct
                    _depth_ok_s   = (_bid_from_ask <= 65)
                    _log_gates = (
                        f"j5m={j5m:.1f}(need>80)"
                        f" j15m={j15m:.1f}(need>{J15M_SHORT_GATE})"
                        f" j1h={j1h:.1f}"
                        f" btc={_btc_j1h:.1f}"
                        f" depth_bid={_bid_from_ask:.1f}%"
                        f"({'PASS' if _depth_ok_s else 'FAIL'})"
                        f" depth_ask={ask_pct:.1f}%"
                    )
                else:
                    if _regime_block_long:
                        log.info(f"[REGIME] {symbol} LONG blocked ÃÂ¢ÃÂÃÂ BTC J1H={_btc_j1h:.1f} corr={_pair_corr}")
                        continue
                    g_j15m  = j15m < J15M_LONG_GATE
                    g_j1h   = j1h  >= J1H_LONG_MIN
                    g_stoch = stoch_k < 25 and stoch_k > stoch_d

                    g_depth = bid_pct >= DEPTH_GATE_PCT
                    # BTC regime LONG gate
                    # Block LONG entries when BTC J1H is overbought or neutral
                    # -- market context unfavorable for LONG bounces
                    if (direction == "LONG" and
                            _btc_regime_context in (
                                "LONG_BLOCKED",
                                "NEUTRAL_BLOCK")):
                        continue
                    # J1H ceiling gate (enforced) — blocks LONGs above valid bounce zone
                    if j1h >= J1H_LONG_MAX:
                        asyncio.create_task(_log_gate(
                            "HL", symbol, "J1H_CEILING_FAIL", direction,
                            f"j1h={j1h:.1f} need j1h<{J1H_LONG_MAX}"))
                        continue
                    # RSI ceiling gate (enforced) — blocks LONGs when 15m RSI approaching overbought
                    if rsi15m >= RSI15M_LONG_MAX:
                        asyncio.create_task(_log_gate(
                            "HL", symbol, "RSI_CEILING_FAIL", direction,
                            f"rsi15m={rsi15m:.1f} need<{RSI15M_LONG_MAX}"))
                        continue
                    # BTC macro downtrend LONG gate — symmetric to SHORT uptrend gate
                    # Block LONGs when BTC J1H is lower now than 10 scans ago (5 min)
                    # Evidence: 7/12 02:06-02:14 cascade — 16 trades 0/16 WR -$759
                    # Same criteria 45 min later (Batch B): 7/9 WR +$649
                    if (len(_btc_j1h_history) >= 10
                            and _btc_j1h < _btc_j1h_history[-10]):
                        asyncio.create_task(_log_gate(
                            "HL", symbol, "BTC_MACRO_FALL", direction,
                            f"btc_j1h={_btc_j1h:.1f} < "
                            f"{_btc_j1h_history[-10]:.1f} 10 scans ago"))
                        continue
                    # J15M freefall floor gate
                    # Data: j15m < -10 = 22% WR -$278 (entries into free-fall)
                    if j15m < J15M_LONG_FLOOR:
                        asyncio.create_task(_log_gate(
                            "HL", symbol, "J15M_FREEFALL", direction,
                            f"j15m={j15m:.1f} < J15M_LONG_FLOOR={J15M_LONG_FLOOR}"
                            f" -- freefall, no bounce context"))
                        continue
                    # EU LONG j5m freefall gate
                    # Data: 7/13 @107 EU j5m=-4.13 -> -$48.6, never moved up (0.02R MFE)
                    # Price still falling at 5m = bounce hasn't started, don't enter
                    if _cur_sess == "EU" and j5m < 0:
                        asyncio.create_task(_log_gate(
                            "HL", symbol, "EU_LONG_J5M_FREEFALL", direction,
                            f"EU j5m={j5m:.1f} < 0 -- still descending at 5m, no bounce start"))
                        continue
                    # R5: EU LONG j15m tight gate
                    # EU LONGs only valid when j15m < 15 (deep oversold)
                    # Data: EU LONG j15m 15-30 = 25% WR -$112; EU LONG j15m < 15 = 71% WR +$389
                    if _cur_sess == "EU" and j15m >= 15:
                        asyncio.create_task(_log_gate(
                            "HL", symbol, "EU_LONG_J15M_TIGHT", direction,
                            f"EU j15m={j15m:.1f} >= 15 -- EU LONGs require j15m<15"))
                        continue
                    score, tier, lev = score_bounce_long(
                        j15m, j1h, bid_pct, adx1h, j5m=j5m, trend=trend,
                        stoch_k=stoch_k_fast, stoch_d=stoch_d_fast)
                    _depth_ok_l  = (bid_pct >= 45)
                    _log_gates = (
                        f"j5m={j5m:.1f}(need<10)"
                        f" j15m={j15m:.1f}(need<{J15M_LONG_GATE})"
                        f" j1h={j1h:.1f}"
                        f" btc={_btc_j1h:.1f}"
                        f" depth_bid={bid_pct:.1f}%"
                        f"({'PASS' if _depth_ok_l else 'FAIL'})"
                        f" depth_ask={ask_pct:.1f}%"
                    )

                # ÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂ GATE_FAIL log ÃÂ¢ÃÂÃÂ every scan when >= 3 of 4 gates pass ÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂÃÂ¢ÃÂÃÂ
                _gate_list  = [g_j15m, g_j1h, g_stoch, g_depth]
                _gate_count = sum(_gate_list)
                _blocked    = [n for n, v in zip(["J15M", "J1H", "STOCH", "DEPTH"], _gate_list) if not v]
                if _gate_count >= 3:
                    log.info(
                        f"[GATE_FAIL] {symbol}"
                        f" {direction}"
                        f" j5m={j5m:.1f}"
                        f" j15m={j15m:.1f}"
                        f" bid={bid_pct:.1f}%"
                        f" blocked="
                        f"{_blocked}")

                if score >= 4:
                    log.info(f"[SCORE] {symbol} {direction} gates=PASS score={score} {_log_gates}")
                else:
                    _stoch_pass = (
                        (direction == "SHORT" and j5m > 80 and j15m > J15M_SHORT_GATE) or
                        (direction == "LONG"  and j5m < 10 and j15m < J15M_LONG_GATE)
                    )
                    if not _stoch_pass:
                        asyncio.create_task(_log_gate("HL", symbol, "STOCH_GATE_FAIL", direction,
                            f"j5m={j5m:.1f} j15m={j15m:.1f}"))
                    else:
                        asyncio.create_task(_log_gate("HL", symbol, "DEPTH_GATE_FAIL", direction,
                            f"bid_pct={bid_pct:.1f} ask_pct={ask_pct:.1f}"))
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
                    "btc_regime_context": _btc_regime_context,
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
                    "stoch_k_entry": round(stoch_k_fast, 2),
                    "stoch_d_entry": round(stoch_d_fast, 2),
                    "rsi1h":        round(rsi1h, 2),
                    "atr15m":       round(atr15m, 6),
                    "adx1h":        round(adx1h, 2),
                    "bid_pct":      bid_pct,
                    "ask_pct":      ask_pct,
                    "adx_context":  round(adx1h, 2),
                    "adx_tier": (
                        "STRONG" if adx1h >= 35
                        else "MODERATE" if adx1h >= 20
                        else "WEAK"),
                    "depth_bid_pct": bid_pct,
                    "depth_ask_pct": ask_pct,
                    "depth_context": (
                        "STRONG_BID" if bid_pct >= 65
                        else "NEUTRAL" if bid_pct >= 45
                        else "STRONG_ASK"),
                    "vol_15m":      vol_15m,
                    "vol_ma15m":    vol_ma15m,
                    "vol_surge":    (vol_15m > vol_ma15m * 1.5),
                    "ma_stack_1h": (
                        "BULL" if (ma10 and ma30 and ma60 and ma10 > ma30 > ma60)
                        else "BEAR" if (ma10 and ma30 and ma60 and ma10 < ma30 < ma60)
                        else "MIXED"),
                    "trend":        trend,
                    "ma10":         round(ma10, 6) if ma10 else None,
                    "ma30":         round(ma30, 6) if ma30 else None,
                    "ma60":         round(ma60, 6) if ma60 else None,
                    "fired_at":     int(time.time()),
                    "is_in_trade":   False,
                    "is_score10":    is_hc,
                    "margin":        MARGIN_PER_TRADE * 2 if (is_hc or (_btc_j1h < 25 and j1h < 25)) else MARGIN_PER_TRADE,  # Tier3: 2x on hc score OR both btc+pair j1h<25 (max conviction oversold)
                    "partial_price": partial_price,
                    "session":       get_session_name(),
                    "btc_j1h":       round(_btc_j1h, 1),
                }
                _vwap_v, _vwap_pct, _vwap_pos = _compute_session_vwap(candles_15m, price)
                if _vwap_v is not None:
                    alert["vwap_at_entry"] = _vwap_v
                    alert["vwap_pct_diff"] = _vwap_pct
                    alert["vwap_position"] = _vwap_pos
                # Co-fire limiter: cap same-direction signals per scan at 5
                # Evidence: 7/12 02:06-02:14 — 12 LONG co-fires in 8 min, 0/12 WR -$759
                _cofire_n = sum(1 for _ca in new_alerts if _ca["direction"] == direction)
                if _cofire_n >= 3:   # tighter: data shows 3+ co-fires = 0% WR -$759
                    log.info(
                        f"[COFIRE_LIMIT] {symbol} {direction} "
                        f"skipped — {_cofire_n} {direction} signals "
                        f"already queued this scan")
                    continue
                new_alerts.append(alert)
                log.info(
                    f"[SIGNAL] {symbol} {direction}"
                    f" tier={tier} lev={lev}x"
                    f" score={score}"
                    f" entry={price:.5f}"
                    f" sl={sl_price:.5f}"
                    f" tp1={tp1_price:.5f}"
                    f" j5m={j5m:.1f}"
                    f" j15m={j15m:.1f}"
                    f" j1h={j1h:.1f}"
                    f" btc={_btc_j1h:.1f}"
                    f"({_btc_regime_context})"
                    f" depth={bid_pct:.1f}%B/{ask_pct:.1f}%A"
                    f" adx={adx1h:.1f}"
                    f" sess={_cur_sess}"
                )

        except Exception as e:
            log.error(f"[SCAN] {symbol} error: {e}", exc_info=True)

    log.info(f"[SCAN] #{_scan_count} complete ÃÂ¢ÃÂÃÂ {len(new_alerts)} new alerts")
    return new_alerts


async def scan_pair_state(hl_client) -> list[dict]:
    """Return lightweight per-pair indicator state for the dashboard grid."""
    states = []
    for symbol in PAIRS:
        try:
            await asyncio.sleep(0.5)  # rate-limit spacing between pairs
            candles_1m, candles_5m, candles_15m, candles_1h, book, price = await _fetch_pair_data(hl_client, symbol)
            if not price:
                states.append({"symbol": symbol, "price": 0})
                continue

            _, _, j5m  = _compute_kdj(candles_1m[:-1])
            _, _, j15m = _compute_kdj(candles_15m[:-1])
            _, _, j1h  = _compute_kdj(candles_1h[:-1])
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
                j15m, j1h, ask_pct, adx1h, j5m=j5m, trend=trend,
                stoch_k=stoch_k_fast, stoch_d=stoch_d_fast)
            long_score,  long_tier,  long_lev  = score_bounce_long(
                j15m, j1h, bid_pct, adx1h, j5m=j5m, trend=trend,
                stoch_k=stoch_k_fast, stoch_d=stoch_d_fast)

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



def _compute_session_vwap(candles_15m: list, entry_price: float):
    """Session VWAP from midnight ET. Returns (None, None, None) on any failure."""
    try:
        from zoneinfo import ZoneInfo as _ZI
        from datetime import datetime as _DT, timezone as _TZ
        _et     = _ZI("America/New_York")
        _now_et = _DT.now(_et)
        _mid_et = _now_et.replace(hour=0, minute=0, second=0, microsecond=0)
        _mid_s  = _mid_et.astimezone(_TZ.utc).timestamp()
        if not candles_15m:
            return None, None, None
        _ts_key = "time" if "time" in candles_15m[0] else "timestamp"
        _is_ms  = candles_15m[0].get(_ts_key, 0) > 1_000_000_000_000
        _thresh = _mid_s * 1000 if _is_ms else _mid_s
        _sess   = [c for c in candles_15m if c.get(_ts_key, 0) >= _thresh]
        if not _sess:
            return None, None, None
        _tpv = sum(((c["high"] + c["low"] + c["close"]) / 3.0) * c["volume"] for c in _sess)
        _vol = sum(c["volume"] for c in _sess)
        if _vol == 0:
            return None, None, None
        _vwap = _tpv / _vol
        _pct  = (entry_price - _vwap) / _vwap * 100
        _pos  = "ABOVE" if _pct > 0.1 else "BELOW" if _pct < -0.1 else "AT"
        return round(_vwap, 6), round(_pct, 3), _pos
    except Exception:
        return None, None, None


async def _fetch_pair_data(hl_client, symbol: str):
    candles_1m, candles_5m, candles_15m, candles_1h, book, price = await asyncio.gather(
        hl_client.get_candles(symbol, "1m",  30),
        hl_client.get_candles(symbol, "5m",  100),
        hl_client.get_candles(symbol, "15m", 100),
        hl_client.get_candles(symbol, "1h",  100),
        hl_client.get_orderbook(symbol, 20),
        hl_client.get_price(symbol),
    )
    return candles_1m, candles_5m, candles_15m, candles_1h, book, price


def log_startup_config():
    log.info(
        f"[CONFIG] PAIRS={len(PAIRS)} "
        f"J15M_SHORT={J15M_SHORT_GATE} J15M_LONG={J15M_LONG_GATE} "
        f"J1H_SHORT_MIN={J1H_SHORT_MIN} J1H_LONG_MIN={J1H_LONG_MIN} "
        f"RSI_SHORT={RSI15M_SHORT_MIN} RSI_LONG={RSI15M_LONG_MAX} "
        f"DEPTH={DEPTH_GATE_PCT}% ATR_SL={ATR_SL_MULTIPLIER}x "
        f"ADX_MIN_LONG={ADX_MIN_LONG} ADX_MIN_SHORT={ADX_MIN_SHORT} "
        f"CIRCUIT_BREAKER={CONSECUTIVE_LOSS_STOP} PAPER={PAPER_MODE}"
    )


def set_pair_cooldown(
        symbol: str,
        duration: int =
        PAIR_COOLDOWN_SECONDS
) -> None:
    _pair_cooldowns[symbol] = (
        time.time() + duration)
    print(
        f"[PAIR COOLDOWN] {symbol}"
        f" cooling down for"
        f" {duration}s")


def get_pair_cooldown_remaining(
        symbol: str
) -> float:
    exp = _pair_cooldowns.get(
        symbol, 0)
    remaining = exp - time.time()
    if remaining <= 0:
        _pair_cooldowns.pop(
            symbol, None)
        return 0.0
    return round(remaining, 1)


def get_all_cooldowns() -> dict:
    now = time.time()
    result = {}
    expired = []
    for sym, exp in list(
            _pair_cooldowns.items()):
        rem = exp - now
        if rem > 0:
            result[sym] = round(rem, 1)
        else:
            expired.append(sym)
    for sym in expired:
        _pair_cooldowns.pop(sym, None)
    return result

