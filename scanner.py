import asyncio
import time
import logging
from typing import Optional

from config import (
    PAIRS, J15M_SHORT_GATE, J15M_LONG_GATE, J1H_SHORT_MIN, J1H_LONG_MAX,
    RSI15M_SHORT_MIN, RSI15M_LONG_MAX, DEPTH_GATE_PCT, ATR_SL_MULTIPLIER,
    TP1_R, TP2_R, LEVERAGE_HIGH, LEVERAGE_MID, LEVERAGE_LOW,
    COOLDOWN_SECONDS, BTC_REGIME_FILTER_ENABLED, PAPER_MODE,
    CONSECUTIVE_LOSS_STOP,
)

log = logging.getLogger("scanner")

# ── Module-level state ────────────────────────────────────────────────────────

_last_scores:     dict[str, int]   = {}   # keyed "BTCSHORT" / "BTCLONG"
_cooldowns:       dict[str, float] = {}   # keyed "BTCSHORT" / "BTCLONG" → expiry ts
_btc_regime:      str              = "Neutral"  # from PREVIOUS completed scan cycle
_btc_regime_next: str              = "Neutral"  # staged from current scan cycle
_scan_count:      int              = 0
_pending:         dict[str, dict]  = {}   # first-scan confirmed, awaiting 2nd


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


def _trend_from_ma(price: float, ma10: Optional[float], ma30: Optional[float], ma60: Optional[float]) -> str:
    if ma10 and ma30 and ma60:
        if price > ma10 > ma30 > ma60:
            return "Strong Bull"
        if price < ma10 < ma30 < ma60:
            return "Strong Bear"
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


def score_bounce_short(j15m, j1h, rsi15m, ask_pct, adx) -> tuple[int, str, int]:
    gates = [
        j15m    >  J15M_SHORT_GATE,
        j1h     >  J1H_SHORT_MIN,
        rsi15m  >  RSI15M_SHORT_MIN,
        ask_pct >= DEPTH_GATE_PCT,
    ]
    score = 4 if all(gates) else 0
    tier, lev = _leverage_tier(adx)
    return score, tier, lev


def score_bounce_long(j15m, j1h, rsi15m, bid_pct, adx) -> tuple[int, str, int]:
    gates = [
        j15m   <  J15M_LONG_GATE,
        j1h    <  J1H_LONG_MAX,
        rsi15m <  RSI15M_LONG_MAX,
        bid_pct >= DEPTH_GATE_PCT,
    ]
    score = 4 if all(gates) else 0
    tier, lev = _leverage_tier(adx)
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


def get_btc_regime() -> str:
    return _btc_regime


def get_scan_count() -> int:
    return _scan_count


def clear_all_scanner_state():
    global _scan_count, _btc_regime, _btc_regime_next
    _last_scores.clear()
    _cooldowns.clear()
    _pending.clear()
    _scan_count       = 0
    _btc_regime       = "Neutral"
    _btc_regime_next  = "Neutral"


# ── Main scan ─────────────────────────────────────────────────────────────────

async def run_full_scan(hl_client) -> list[dict]:
    global _btc_regime, _btc_regime_next, _scan_count

    _scan_count += 1
    new_alerts: list[dict] = []
    blocked_shorts = 0
    allowed_longs  = 0

    for symbol in PAIRS:
        try:
            await asyncio.sleep(0.5)  # rate-limit spacing — 12 pairs × 0.5s = 6s minimum spread
            candles_5m, candles_15m, candles_1h, book, price = await _fetch_pair_data(hl_client, symbol)

            if not price or price == 0:
                log.warning(f"[SCAN] {symbol} — no price, skipping")
                continue

            # ── Indicators ────────────────────────────────────────────────────
            _, _, j5m  = _compute_kdj(candles_5m)
            _, _, j15m = _compute_kdj(candles_15m)
            _, _, j1h  = _compute_kdj(candles_1h)
            rsi15m     = _compute_rsi(candles_15m)
            rsi1h      = _compute_rsi(candles_1h)
            atr5m      = _compute_atr(candles_5m)
            atr15m     = _compute_atr(candles_15m)
            atr1h      = _compute_atr(candles_1h)
            adx1h      = _compute_adx(candles_1h)
            ma10       = _compute_ma(candles_1h, 10)
            ma30       = _compute_ma(candles_1h, 30)
            ma60       = _compute_ma(candles_1h, 60)
            trend      = _trend_from_ma(price, ma10, ma30, ma60)
            bid_pct, ask_pct = _depth_pcts(book)

            vol_15m    = candles_15m[-1]["volume"] if candles_15m else 0
            vol_ma15m  = (sum(c["volume"] for c in candles_15m[-10:]) / min(10, len(candles_15m))
                          if candles_15m else 0)

            # ── BTC regime — stage for NEXT scan cycle (avoid circular dependency) ──
            if symbol == "BTC":
                _btc_regime_next = trend
                log.info(f"[REGIME] current={_btc_regime} (from prev scan) next={_btc_regime_next} "
                         f"blocked_shorts={blocked_shorts} allowed_longs={allowed_longs}")

            # ── SL distance ───────────────────────────────────────────────────
            sl_dist = atr15m * ATR_SL_MULTIPLIER

            # ── Score both directions ─────────────────────────────────────────
            for direction in ("SHORT", "LONG"):
                key = f"{symbol}{direction}"

                if get_cooldown_remaining(symbol, direction) > 0:
                    continue

                if direction == "SHORT":
                    score, tier, lev = score_bounce_short(j15m, j1h, rsi15m, ask_pct, adx1h)
                    log_gates = (f"j15m={j15m:.1f}(need>{J15M_SHORT_GATE}) "
                                 f"j1h={j1h:.1f}(need>{J1H_SHORT_MIN}) "
                                 f"rsi15m={rsi15m:.1f}(need>{RSI15M_SHORT_MIN}) "
                                 f"ask={ask_pct:.1f}%(need>={DEPTH_GATE_PCT}%)")
                else:
                    score, tier, lev = score_bounce_long(j15m, j1h, rsi15m, bid_pct, adx1h)
                    log_gates = (f"j15m={j15m:.1f}(need<{J15M_LONG_GATE}) "
                                 f"j1h={j1h:.1f}(need<{J1H_LONG_MAX}) "
                                 f"rsi15m={rsi15m:.1f}(need<{RSI15M_LONG_MAX}) "
                                 f"bid={bid_pct:.1f}%(need>={DEPTH_GATE_PCT}%)")

                if score == 4:
                    log.info(f"[SCORE] {symbol} {direction} gates=PASS score=4 {log_gates}")
                else:
                    if _last_scores.get(key, 0) == 4:
                        log.info(f"[SCORE] {symbol} {direction} score=0 {log_gates}")
                    _last_scores[key] = 0
                    _pending.pop(key, None)
                    continue

                # Consecutive scan confirmation
                if _last_scores.get(key, 0) < 4:
                    _last_scores[key] = 4
                    _pending[key] = {
                        "symbol": symbol, "direction": direction,
                        "score": score, "tier": tier,
                    }
                    log.info(f"[SCORE] {symbol} {direction} first-scan confirmed — awaiting 2nd")
                    continue

                # Second consecutive scan — emit alert
                _last_scores[key] = 4

                # BTC regime filter — BTC itself is exempt (self-referential pair)
                if BTC_REGIME_FILTER_ENABLED:
                    if symbol == "BTC":
                        log.info(f"[REGIME EXEMPT] BTC {direction} exempt from regime filter — self-referential pair")
                    elif _btc_regime == "Strong Bull" and direction == "SHORT":
                        log.info(f"[REGIME BLOCK] {symbol} SHORT blocked — BTC regime is Strong Bull")
                        blocked_shorts += 1
                        continue
                    elif _btc_regime == "Strong Bear" and direction == "LONG":
                        log.info(f"[REGIME BLOCK] {symbol} LONG blocked — BTC regime is Strong Bear")
                        continue
                    elif _btc_regime == "Neutral":
                        log.info(f"[REGIME BLOCK] {symbol} {direction} blocked — BTC regime is Neutral")
                        continue

                if direction == "LONG":
                    allowed_longs += 1

                # Compute SL / TP prices
                if direction == "SHORT":
                    sl_price  = round(price + sl_dist, 6)
                    tp1_price = round(price - sl_dist * TP1_R, 6)
                    tp2_price = round(price - sl_dist * TP2_R, 6)
                else:
                    sl_price  = round(price - sl_dist, 6)
                    tp1_price = round(price + sl_dist * TP1_R, 6)
                    tp2_price = round(price + sl_dist * TP2_R, 6)

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
                    "is_in_trade":  False,
                }
                new_alerts.append(alert)
                _pending.pop(key, None)
                log.info(f"[ALERT] {symbol} {direction} tier={tier} lev={lev}x entry={price} "
                         f"sl={sl_price} tp1={tp1_price} tp2={tp2_price}")

        except Exception as e:
            log.error(f"[SCAN] {symbol} error: {e}", exc_info=True)

    # Commit staged BTC regime — now safe to use on the NEXT scan cycle
    _btc_regime = _btc_regime_next
    log.info(f"[SCAN] #{_scan_count} complete — {len(new_alerts)} new alerts — regime committed: {_btc_regime}")
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
            atr15m     = _compute_atr(candles_15m)
            adx1h      = _compute_adx(candles_1h)
            ma10       = _compute_ma(candles_1h, 10)
            ma30       = _compute_ma(candles_1h, 30)
            ma60       = _compute_ma(candles_1h, 60)
            trend      = _trend_from_ma(price, ma10, ma30, ma60)
            bid_pct, ask_pct = _depth_pcts(book)

            short_score, short_tier, short_lev = score_bounce_short(j15m, j1h, rsi15m, ask_pct, adx1h)
            long_score,  long_tier,  long_lev  = score_bounce_long(j15m, j1h, rsi15m, bid_pct, adx1h)

            states.append({
                "symbol":      symbol,
                "price":       price,
                "j5m":         round(j5m, 2),
                "j15m":        round(j15m, 2),
                "j1h":         round(j1h, 2),
                "rsi15m":      round(rsi15m, 2),
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
        f"COOLDOWN={COOLDOWN_SECONDS//60}min "
        f"CIRCUIT_BREAKER={CONSECUTIVE_LOSS_STOP} PAPER={PAPER_MODE}"
    )
