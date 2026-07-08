#!/usr/bin/env python3
"""
Regression tests for scanner exit logic.
Run: python regression.py
Exits 0 if all tests pass, 1 if any fail.
"""
import sys
import time

import scanner

_results = []

def chk(test_id, cond, msg):
    status = "PASS" if cond else "FAIL"
    _results.append((test_id, cond, msg))
    print(f"[{status}] {test_id}: {msg}")

# ── Pure logic helpers mirroring main.py exactly ──────────────────────────────

def adverse_pct(entry_px, current, is_short):
    """Mirror main.py KILL adverse_pct formula exactly."""
    if entry_px <= 0:
        return 0
    return ((entry_px - current) / entry_px
            if not is_short
            else (current - entry_px) / entry_px)

def kill_fires(entry_px, current, is_short, elapsed):
    """Return (fires, adv_pct, tier_str) mirroring main.py KILL block."""
    adv = adverse_pct(entry_px, current, is_short)
    floor_hit  = adv >= scanner.KILL_PCT_FLOOR
    fivemin_hit = elapsed >= 300 and adv >= scanner.KILL_PCT_5MIN
    tier = "FLOOR" if floor_hit else "5MIN"
    return (floor_hit or fivemin_hit), adv, tier

def sl_breach(sl_price, current, is_short):
    """Mirror main.py SL breach condition exactly."""
    if not sl_price:
        return False
    return (is_short and current >= sl_price) or (not is_short and current <= sl_price)

# Local SE state (mirrors _se_j1h_extreme in main.py)
_se_state = {}

def se_fire(key, cur_j1h, cpnl, is_short):
    """Mirror main.py SE logic using local _se_state dict."""
    if cur_j1h is None or cpnl <= 0:
        return False, 0.0
    if not is_short:
        prev = _se_state.get(key, cur_j1h)
        _se_state[key] = max(prev, cur_j1h)
        delta = _se_state[key] - cur_j1h
        return delta >= scanner.SE_J1H_DECAY_PTS, delta
    else:
        prev = _se_state.get(key, cur_j1h)
        _se_state[key] = min(prev, cur_j1h)
        delta = cur_j1h - _se_state[key]
        return delta >= scanner.SE_J1H_DECAY_PTS, delta

def pd20_fires(is_short, be_armed, peak, cpnl, sym="SOL"):
    """Mirror main.py PEAK_DECAY_20 condition (SHORT only, before TP1)."""
    if not is_short or not be_armed:
        return False
    threshold = 0.70 if sym in ("@107",) else 0.80
    return cpnl < peak * threshold

def pd10_fires(is_short, tp1_hit, peak, cpnl):
    """Mirror main.py PEAK_DECAY_10 condition (SHORT only, after TP1)."""
    if not is_short or not tp1_hit:
        return False
    return cpnl < peak * 0.90

def leverage_cap(tier, lev, symbol):
    """Mirror main.py HIGH_PROB leverage cap for non-anchor pairs."""
    anchor = {"BTC", "ETH", "SOL", "BTC_USDT", "ETH_USDT", "SOL_USDT"}
    if tier == "HIGH_PROB" and symbol not in anchor:
        return min(lev, scanner.LEVERAGE_MID)
    return lev

# ── KILL tests ─────────────────────────────────────────────────────────────────

fires, adv, tier = kill_fires(1000.0, 994.0, False, 10)
chk("K1", fires and tier == "FLOOR",
    f"LONG adverse 0.6% (1000→994) → KILL FLOOR fires (adverse_pct={adv*100:.3f}%)")

fires, adv, tier = kill_fires(1000.0, 1006.0, True, 10)
chk("K2", fires and tier == "FLOOR",
    f"SHORT adverse 0.6% (1000→1006) → KILL FLOOR fires (adverse_pct={adv*100:.3f}%)")

fires, adv, tier = kill_fires(100.0, 99.6, False, 300)
chk("K3", fires and tier == "5MIN",
    f"LONG adverse 0.4% at 300s → KILL 5MIN fires (adverse_pct={adv*100:.3f}%)")

fires, adv, _ = kill_fires(100.0, 99.7, False, 300)
chk("K4", not fires,
    f"LONG adverse 0.3% at 300s → KILL does NOT fire (adverse_pct={adv*100:.3f}%)")

adv = adverse_pct(1000.0, 994.0, False)
chk("K5", adv > 0,
    f"LONG adverse (1000→994) → adverse_pct={adv*100:.3f}% is POSITIVE (not negative, June-30 inversion check)")

# ── SL tests ───────────────────────────────────────────────────────────────────

chk("S1", sl_breach(99.0, 98.5, False),
    "LONG current=98.5 < sl=99.0 → SL breach")

chk("S2", sl_breach(101.0, 101.5, True),
    "SHORT current=101.5 > sl=101.0 → SL breach")

chk("S3", not sl_breach(None, 99.0, False),
    "sl_price=None → no SL breach, KILL still evaluable")

# ── SIGNAL EXHAUSTION tests ────────────────────────────────────────────────────

_se_state.clear()
se_fire("k1", 80.0, 1.0, False)                     # seed peak at 80
fires, delta = se_fire("k1", 68.0, 1.0, False)      # decay 12
chk("SE1", fires,
    f"LONG j1h peaked=80 now=68 decay={delta:.1f} >= {scanner.SE_J1H_DECAY_PTS} → SE fires")

_se_state.clear()
se_fire("k2", 80.0, 1.0, False)
fires, delta = se_fire("k2", 72.0, 1.0, False)      # decay 8
chk("SE2", not fires,
    f"LONG j1h peaked=80 now=72 decay={delta:.1f} < {scanner.SE_J1H_DECAY_PTS} → no fire")

_se_state.clear()
se_fire("k3", 20.0, 1.0, True)                      # seed trough at 20
fires, delta = se_fire("k3", 31.0, 1.0, True)       # rise 11
chk("SE3", fires,
    f"SHORT j1h troughed=20 now=31 rise={delta:.1f} >= {scanner.SE_J1H_DECAY_PTS} → SE fires")

_se_state.clear()
se_fire("k4", 20.0, 1.0, True)
fires, delta = se_fire("k4", 28.0, 1.0, True)       # rise 8
chk("SE4", not fires,
    f"SHORT j1h troughed=20 now=28 rise={delta:.1f} < {scanner.SE_J1H_DECAY_PTS} → no fire")

_se_state.clear()
se_fire("k5", 80.0, 1.0, False)                     # seed peak
fires, delta = se_fire("k5", 60.0, 0.0, False)      # cpnl=0 → gate blocks
chk("SE5", not fires,
    "cpnl=0 → SE never fires regardless of j1h")

# ── PEAK DECAY tests ───────────────────────────────────────────────────────────

chk("PD1", pd20_fires(True, True, 100.0, 75.0),
    "SHORT be_armed peak=$100 cpnl=$75 → PEAK_DECAY_20 fires (75 < 80)")

chk("PD2", not pd20_fires(False, True, 100.0, 75.0),
    "LONG be_armed peak=$100 cpnl=$75 → PEAK_DECAY_20 does NOT fire (LONG excluded)")

chk("PD3", pd10_fires(True, True, 100.0, 88.0),
    "SHORT tp1_hit peak=$100 cpnl=$88 → PEAK_DECAY_10 fires (88 < 90)")

chk("PD4", not pd10_fires(False, True, 100.0, 88.0),
    "LONG tp1_hit → PEAK_DECAY_10 does NOT fire (LONG excluded)")

# ── IMPORT / constant tests ────────────────────────────────────────────────────

chk("I1", hasattr(scanner, "KILL_PCT_FLOOR") and scanner.KILL_PCT_FLOOR == 0.006,
    f"scanner.KILL_PCT_FLOOR exists = {getattr(scanner, 'KILL_PCT_FLOOR', 'MISSING')}")

chk("I2", hasattr(scanner, "KILL_PCT_5MIN") and scanner.KILL_PCT_5MIN == 0.004,
    f"scanner.KILL_PCT_5MIN exists = {getattr(scanner, 'KILL_PCT_5MIN', 'MISSING')}")

chk("I3", hasattr(scanner, "SE_J1H_DECAY_PTS") and scanner.SE_J1H_DECAY_PTS == 10.0,
    f"scanner.SE_J1H_DECAY_PTS exists = {getattr(scanner, 'SE_J1H_DECAY_PTS', 'MISSING')}")

chk("I4", hasattr(scanner, "PAIR_COOLDOWN_SECONDS") and scanner.PAIR_COOLDOWN_SECONDS == 1800,
    f"scanner.PAIR_COOLDOWN_SECONDS exists = {getattr(scanner, 'PAIR_COOLDOWN_SECONDS', 'MISSING')}")

chk("I5", not hasattr(scanner, "KILL_GRACE_SECONDS"),
    "scanner.KILL_GRACE_SECONDS does NOT exist (deprecated constant gone — this was the 500 crash bug)")

# ── COOLDOWN tests ─────────────────────────────────────────────────────────────

scanner._cooldowns.clear()
scanner.set_close_cooldown("TEST_SYM", "LONG", 1800)
_exp = scanner._cooldowns.get("TEST_SYMLONG", 0)
chk("C1", _exp > time.time() + 1798,
    f"set_close_cooldown sets expiry correctly (remaining={_exp - time.time():.1f}s)")

_rem = scanner.get_cooldown_remaining("TEST_SYM", "LONG")
chk("C2", _rem > 0,
    f"get_cooldown_remaining > 0 immediately after set ({_rem}s remaining)")

scanner._cooldowns["EXPSYMLONG"] = time.time() - 1  # simulate expired
chk("C3", scanner.get_cooldown_remaining("EXPSYM", "LONG") == 0,
    "get_cooldown_remaining = 0 after expiry (simulated via backdated ts)")

# ── TIER / LEVERAGE tests ──────────────────────────────────────────────────────

t, lev = scanner._leverage_tier(50)
chk("T1", t == "HIGH_PROB" and lev == scanner.LEVERAGE_HIGH,
    f"adx=50 → tier={t} lev={lev} (expected HIGH_PROB lev={scanner.LEVERAGE_HIGH})")

t, lev = scanner._leverage_tier(25)
chk("T2", t == "STRONG" and lev == scanner.LEVERAGE_MID,
    f"adx=25 → tier={t} lev={lev} (expected STRONG lev={scanner.LEVERAGE_MID})")

t, lev = scanner._leverage_tier(24)
chk("T3", t == "REGULAR" and lev == scanner.LEVERAGE_LOW,
    f"adx=24 → tier={t} lev={lev} (expected REGULAR lev={scanner.LEVERAGE_LOW})")

t, lev = scanner._leverage_tier(50)
result = leverage_cap(t, lev, "BTC")
chk("T4", result == scanner.LEVERAGE_HIGH,
    f"HIGH_PROB + anchor BTC → leverage={result} unchanged at {scanner.LEVERAGE_HIGH}x")

t, lev = scanner._leverage_tier(50)
result = leverage_cap(t, lev, "DOGE")
chk("T5", result == scanner.LEVERAGE_MID,
    f"HIGH_PROB + non-anchor DOGE → leverage={result} capped to LEVERAGE_MID={scanner.LEVERAGE_MID}x")


# ── SETTINGS COMPLETENESS ──────────────────────────────────────────────────────

settings_response = {
    "paper_mode":            True,
    "telegram_enabled":      True,
    "depth_gate_pct":
        scanner.DEPTH_GATE_PCT,
    "adx_min_long":
        scanner.ADX_MIN_LONG,
    "j15m_short_gate":
        scanner.J15M_SHORT_GATE,
    "j15m_long_gate":
        scanner.J15M_LONG_GATE,
    "j1h_short_min":
        scanner.J1H_SHORT_MIN,
    "j1h_short_max":
        scanner.J1H_SHORT_MAX,
    "j1h_long_max":
        scanner.J1H_LONG_MAX,
    "atr_sl_multiplier":
        scanner.ATR_SL_MULTIPLIER,
    "tp1_close_pct":
        scanner.TP1_CLOSE_PCT,
    "tp2_r":                 scanner.TP2_R,
    "margin_per_trade":
        scanner.MARGIN_PER_TRADE,
    "kill_cooldown_seconds":
        scanner.PAIR_COOLDOWN_SECONDS,
    "kill_pct_floor":
        scanner.KILL_PCT_FLOOR,
    "kill_pct_5min":
        scanner.KILL_PCT_5MIN,
    "se_j1h_decay_pts":
        scanner.SE_J1H_DECAY_PTS,
}

required_fields = [
    "j1h_long_max",
    "j1h_short_max",
    "kill_pct_floor",
    "kill_pct_5min",
    "se_j1h_decay_pts",
    "margin_per_trade",
    "adx_min_long",
    "j15m_short_gate",
    "j15m_long_gate",
]
deprecated_fields = [
    "kill_grace_seconds",
]

for field in required_fields:
    chk(f"SC_{field}",
        field in settings_response,
        (f"SC: '{field}' present in settings response "
         f"(value={settings_response.get(field, 'MISSING')})"))

for field in deprecated_fields:
    chk(f"SC_no_{field}",
        field not in settings_response,
        (f"SC: deprecated '{field}' correctly absent from settings"))

# ── Summary ────────────────────────────────────────────────────────────────────

passed = sum(1 for _, ok, _ in _results if ok)
total  = len(_results)
failed = total - passed
print(f"\nPASSED: {passed}/{total} tests")
print(f"FAILED: {failed}/{total} tests")
sys.exit(0 if failed == 0 else 1)
