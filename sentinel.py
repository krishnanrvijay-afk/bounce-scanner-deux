"""
sentinel.py -- Sentinel Watchdog + Executor (Phase 0: Observe-only)

Computes market regime from breadth signals every scan cycle.
Phase 0: ZERO scanner behavior changes. All outputs are log-only.

Regimes:
  NORMAL   -- trade the system
  CASCADE  -- forced selling, everything down together
  SQUEEZE  -- violent counter-move, shorts being swept
  CHOP     -- low conviction, whipsaw risk
  TRENDING -- sustained directional move with broad breadth

Sentinel Executor (Phase 0 -- LOG ONLY):
  Monitors aggregate open PnL across winning positions.
  Logs when collective winners would hit the $150 threshold.
  No positions are closed in Phase 0.
"""

import statistics
import time
import threading
import logging
from collections import deque
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger("sentinel")

# -- Venue (set via init()) ---------------------------------------------------
_VENUE = "HL"

# -- Cluster definitions ------------------------------------------------------
MAJORS        = {"BTC","ETH","SOL","XRP","AVAX","LINK","NEAR","SUI","ARB"}
MEME          = {"DOGE","WIF"}
IDIOSYNCRATIC = {"ZEC","HYPE"}

# -- Regime thresholds (Phase 0 hypotheses -- calibrate after 2 weeks) --------
BREADTH_CASCADE_MIN  = 0.80   # >= 80% pairs with j15m < 20
BREADTH_SQUEEZE_MIN  = 0.80   # >= 80% pairs with j15m > 80
ADX_CHOP_MAX         = 18.0   # avg ADX < 18 = chop
ADX_TREND_MIN        = 28.0   # avg ADX > 28 = trending
BREADTH_TREND_MIN    = 0.70   # >= 70% one direction = trending
VELOCITY_THRESHOLD   = 0.010  # |median 5-scan return| > 1% = directional sync

# -- Hysteresis ---------------------------------------------------------------
ENTER_HYSTERESIS    = 2   # consecutive same candidate to commit
EXIT_HYSTERESIS_MIN = 3   # NORMAL readings needed to exit CASCADE/SQUEEZE

# -- Price history for sync_velocity ------------------------------------------
_PRICE_HIST_LEN = 10   # 10 scans ~ 5 min at 30s/scan
_price_hist: dict = {}

# -- Sentinel Executor --------------------------------------------------------
EXECUTOR_THRESHOLD   = 150.0   # USD aggregate winning-position PnL
EXECUTOR_MIN_AGE_S   = 120     # position must be >= 2 min old
EXECUTOR_MIN_PNL     = 5.0     # each position must be > $5 to qualify
EXECUTOR_COOLDOWN_S  = 300     # 5 min between executor observations
_executor_last_ts    = 0.0

# -- Regime state (single-threaded per scan cycle) ----------------------------
_regime          = "NORMAL"
_candidate       = "NORMAL"
_candidate_count = 0
_normal_count    = 0

# -- Injected dependency ------------------------------------------------------
_get_supabase_fn = None


def init(venue: str, get_supabase_fn) -> None:
    global _VENUE, _get_supabase_fn
    _VENUE           = venue
    _get_supabase_fn = get_supabase_fn
    log.info("[SENTINEL] initialized -- venue=%s observe-only Phase 0", venue)


# -- Internal helpers ---------------------------------------------------------

def _update_price(sym: str, price: float) -> None:
    if sym not in _price_hist:
        _price_hist[sym] = deque(maxlen=_PRICE_HIST_LEN)
    _price_hist[sym].append(price)


def _velocity(sym: str, current: float):
    h = _price_hist.get(sym)
    if not h or len(h) < 5:
        return None
    old = h[-5]
    return (current - old) / old if old > 0 else None


def _compute_metrics(pair_states: list, prices: dict) -> dict:
    n = len(pair_states)
    if n == 0:
        return {}

    j15m_list  = [p["j15m"]   for p in pair_states if p.get("j15m")  is not None]
    adx_list   = [p["adx1h"]  for p in pair_states if p.get("adx1h") and p["adx1h"] > 0]
    depth_list = [p["bid_pct"] for p in pair_states if p.get("bid_pct") is not None]

    breadth_dn = sum(1 for v in j15m_list if v < 20) / max(len(j15m_list), 1)
    breadth_up = sum(1 for v in j15m_list if v > 80) / max(len(j15m_list), 1)
    avg_adx    = statistics.mean(adx_list)   if adx_list   else 0.0
    avg_depth  = statistics.mean(depth_list) if depth_list else 50.0

    velocities = []
    for p in pair_states:
        sym   = p.get("symbol", "")
        price = (prices or {}).get(sym)
        if price:
            _update_price(sym, float(price))
            v = _velocity(sym, float(price))
            if v is not None:
                velocities.append(v)

    sync_vel = statistics.median(velocities) if velocities else 0.0

    return {
        "breadth_dn": round(breadth_dn, 3),
        "breadth_up": round(breadth_up, 3),
        "avg_adx":    round(avg_adx,    1),
        "sync_vel":   round(sync_vel,   5),
        "avg_depth":  round(avg_depth,  1),
        "pair_count": n,
        "vel_pairs":  len(velocities),
    }


def _classify(m: dict) -> str:
    if not m:
        return "NORMAL"
    sv  = m.get("sync_vel",   0.0)
    bd  = m.get("breadth_dn", 0.0)
    bu  = m.get("breadth_up", 0.0)
    adx = m.get("avg_adx",    0.0)

    if sv < -VELOCITY_THRESHOLD and bd >= BREADTH_CASCADE_MIN:
        return "CASCADE"
    if sv >  VELOCITY_THRESHOLD and bu >= BREADTH_SQUEEZE_MIN:
        return "SQUEEZE"
    if adx < ADX_CHOP_MAX and abs(bd - bu) < 0.20:
        return "CHOP"
    if adx > ADX_TREND_MIN and max(bd, bu) >= BREADTH_TREND_MIN:
        return "TRENDING"
    return "NORMAL"


def _apply_hysteresis(candidate: str):
    global _candidate, _candidate_count, _normal_count, _regime

    if candidate == _regime:
        _candidate_count = 0
        if candidate == "NORMAL":
            _normal_count += 1
        return None

    if candidate == "NORMAL":
        _normal_count += 1
        if _regime in ("CASCADE", "SQUEEZE") and _normal_count < EXIT_HYSTERESIS_MIN:
            return None
        return "NORMAL"
    else:
        _normal_count = 0
        if candidate == _candidate:
            _candidate_count += 1
        else:
            _candidate       = candidate
            _candidate_count = 1
        return candidate if _candidate_count >= ENTER_HYSTERESIS else None


# -- Supabase (fire-and-forget daemon threads) --------------------------------

def _sb_log(m: dict, regime: str, prev: str, changed: bool) -> None:
    try:
        sb = _get_supabase_fn() if _get_supabase_fn else None
        if sb is None:
            return
        sb.table("sentinel_log").insert({
            "venue":         _VENUE,
            "regime":        regime,
            "prev_regime":   prev,
            "breadth_dn":    m.get("breadth_dn"),
            "breadth_up":    m.get("breadth_up"),
            "avg_adx":       m.get("avg_adx"),
            "sync_vel":      m.get("sync_vel"),
            "avg_depth":     m.get("avg_depth"),
            "pair_count":    m.get("pair_count"),
            "vel_pairs":     m.get("vel_pairs"),
            "state_changed": changed,
            "ts":            datetime.now(timezone.utc).isoformat(),
        }).execute()
    except Exception as e:
        log.debug("[SENTINEL] sb_log error: %s", e)


def _sb_exec_log(summary: dict) -> None:
    try:
        sb = _get_supabase_fn() if _get_supabase_fn else None
        if sb is None:
            return
        sb.table("sentinel_executor_log").insert({
            "venue":       _VENUE,
            "agg_pnl":     summary["agg_pnl"],
            "threshold":   summary["threshold"],
            "sweep_count": summary["sweep_count"],
            "sweep_pairs": summary["sweep_pairs"],
            "action":      "LOG_ONLY",
            "ts":          datetime.now(timezone.utc).isoformat(),
        }).execute()
    except Exception as e:
        log.debug("[SENTINEL] exec_log error: %s", e)


# -- Public API ---------------------------------------------------------------

def update(pair_states: list, prices: dict) -> dict:
    """
    Called once per scan cycle.
    Returns {"regime", "changed", "prev_regime", "metrics", "telegram_text"}.
    Phase 0: no scanner behavior is modified.
    """
    global _regime, _normal_count

    m         = _compute_metrics(pair_states, prices)
    candidate = _classify(m)
    prev      = _regime
    committed = _apply_hysteresis(candidate)

    tg_text = None
    if committed is not None and committed != _regime:
        _regime       = committed
        _normal_count = 0

        desc = {
            "NORMAL":   "back to normal -- resume standard system",
            "CASCADE":  "forced selling detected -- stand back",
            "SQUEEZE":  "squeeze in progress -- shorts being swept",
            "CHOP":     "no conviction -- whipsaw risk high",
            "TRENDING": "broad directional move -- respect the trend",
        }.get(committed, committed)

        tg_text = (
            "SENTINEL: " + prev + " -> " + committed + "\n"
            + desc + "\n"
            + "breadth dn=" + str(round(m.get("breadth_dn", 0) * 100)) + "% "
            + "up=" + str(round(m.get("breadth_up", 0) * 100)) + "% "
            + "adx=" + str(m.get("avg_adx", 0)) + " "
            + "vel=" + str(round(m.get("sync_vel", 0) * 100, 2)) + "%\n"
            + "[OBSERVE ONLY -- no trades affected]"
        )
        print("[SENTINEL]", prev, "->", committed, "|", m)
        threading.Thread(
            target=lambda: _sb_log(m, committed, prev, True),
            daemon=True).start()
    else:
        threading.Thread(
            target=lambda cv=_regime, pv=prev, mv=dict(m): _sb_log(mv, cv, pv, False),
            daemon=True).start()

    return {
        "regime":        _regime,
        "changed":       tg_text is not None,
        "prev_regime":   prev,
        "metrics":       m,
        "telegram_text": tg_text,
    }


def get_regime() -> str:
    return _regime


def get_pill_text() -> str:
    labels = {
        "NORMAL":   "NORMAL -- trade the system",
        "CASCADE":  "CASCADE -- stand back",
        "SQUEEZE":  "SQUEEZE -- squeeze in progress",
        "CHOP":     "CHOP -- no conviction",
        "TRENDING": "TRENDING -- respect direction",
    }
    return labels.get(_regime, _regime)


def check_executor(open_trades: dict):
    """
    Phase 0 -- LOG ONLY. Called from exit monitor loop.
    Returns summary dict when aggregate winners >= $150, else None.
    No positions are closed.
    """
    global _executor_last_ts
    now = time.time()
    if now - _executor_last_ts < EXECUTOR_COOLDOWN_S:
        return None

    qualifying = []
    for key, trade in open_trades.items():
        cpnl   = trade.get("unrealized_pnl", 0) or 0
        opened = trade.get("opened_at", now) or now
        age    = now - opened
        if cpnl >= EXECUTOR_MIN_PNL and age >= EXECUTOR_MIN_AGE_S:
            qualifying.append({
                "symbol": trade.get("symbol", "?"),
                "dir":    trade.get("direction", "?"),
                "cpnl":   round(cpnl, 2),
            })

    if not qualifying:
        return None

    total = sum(q["cpnl"] for q in qualifying)
    if total < EXECUTOR_THRESHOLD:
        return None

    _executor_last_ts = now
    pairs_str = ", ".join(
        q["symbol"] + " " + q["dir"] + " +$" + str(q["cpnl"])
        for q in qualifying)
    summary = {
        "agg_pnl":     round(total, 2),
        "threshold":   EXECUTOR_THRESHOLD,
        "sweep_count": len(qualifying),
        "sweep_pairs": pairs_str,
    }
    print("[EXECUTOR] WOULD SWEEP", len(qualifying), "positions --", "$" + str(total), "|", pairs_str)
    threading.Thread(target=lambda s=dict(summary): _sb_exec_log(s), daemon=True).start()

    summary["telegram_text"] = (
        "SENTINEL EXECUTOR (OBSERVE)\n"
        + "Would sweep " + str(len(qualifying)) + " positions -- $" + str(round(total, 2)) + " collective\n"
        + "\n".join("  " + q["symbol"] + " " + q["dir"] + " +$" + str(q["cpnl"]) for q in qualifying) + "\n"
        + "Threshold: $" + str(int(EXECUTOR_THRESHOLD)) + "\n"
        + "[LOG ONLY -- no positions closed in Phase 0]"
    )
    return summary
