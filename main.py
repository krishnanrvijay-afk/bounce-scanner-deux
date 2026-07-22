import asyncio
import csv
import io
import logging
import os
import time
import threading
import requests
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from typing import Optional

from dotenv import load_dotenv
load_dotenv()

_EDT = timezone(timedelta(hours=-4))
ET   = ZoneInfo("America/New_York")
DEPLOY_TIME = datetime.now(_EDT).strftime("%Y-%m-%d %H:%M EDT")

_scanner_log = logging.getLogger("scanner")
if not _scanner_log.handlers:
    _sh = logging.StreamHandler()
    _sh.setFormatter(logging.Formatter("%(levelname)s:%(name)s: %(message)s"))
    _scanner_log.addHandler(_sh)
_scanner_log.setLevel(logging.INFO)
_scanner_log.propagate = False

from fastapi import FastAPI, HTTPException, Request, Query
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from config import (
    PAIRS, SCAN_INTERVAL_SECONDS, PRICE_INTERVAL_SECONDS,
    MARGIN_PER_TRADE, MARGIN_HARD_CAP, PAPER_MODE, LIVE_MANUAL_ENTRY_ONLY,
    CONSECUTIVE_LOSS_STOP, DAILY_LOSS_LIMIT, TP1_R, TP2_R, TP1_CLOSE_PCT, TRAIL_ATR_MULTIPLIER,
    SUPABASE_URL, SUPABASE_KEY,
    SENTINEL_MIN_PEAK_PCT, SENTINEL_MIN_PEAK_PCT_DEFAULT,
)
from supabase import create_client, Client
import sentinel as _sentinel_mod
from hl_client import HLClient
from mexc_client import MexcClient
from scanner import (
    run_full_scan, scan_pair_state,
    get_scan_count, set_close_cooldown, clear_cooldown,
    get_cooldown_remaining, clear_all_scanner_state, log_startup_config,
    compute_market_health, get_session_name,
    set_pair_cooldown, get_pair_cooldown_remaining, get_all_cooldowns,
)
import scanner as _scanner_mod  # direct access to _cooldowns dict for persistence

# Ã¢ÂÂÃ¢ÂÂ Telegram config Ã¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂ
TELEGRAM_BOT_TOKEN  = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID    = int(os.environ.get("TELEGRAM_CHAT_ID", "0") or "0")
TELEGRAM_ENABLED    = os.environ.get("TELEGRAM_ENABLED", "true").lower() == "true"
_digest_task = None  # type: ignore
_stale_tg_sent: set[str] = set()  # symbols for which stale-price TG alert was already sent
_session_sl_counts: dict[str, int]   = {}    # "SYMBOL_DIRECTION_SESSION" -> SL count
_session_halted:    set[str]         = set() # "SYMBOL_DIRECTION_SESSION" halted for session
_pending_alerts:    dict[str, dict]  = {}    # f"{symbol}_{direction}" -> alert pending price confirmation
_large_sl_cooldowns: dict[str, float] = {}   # "SYMBOLDIR" -> expiry ts for 90-min cooldowns
_3hlh_cooldowns:     dict[str, float] = {}   # "SYM_DIR" -> expiry ts; 30-min re-entry block after 3H_LOWER_HIGH
_peak_shadow: dict = {}   # trade_key -> shadow tracking state (observation only)
_sentinel_sweep: list = []   # deferred protective exits (PEAK_DECAY_10 / RUNNER_DECAY_10) Ã¢ÂÂ flushed once per scan cycle
_adverse_shadow: dict = {}  # trade_key -> adverse-cut shadow state (observation only)
_sign_shadow:   dict = {}  # trade_key -> PnL-sign transition history (observation only)
_signal_shadow: dict = {}  # trade_key -> signal invalidation shadow state (observation only)
_se_j1h_extreme: dict = {}  # key -> best J1H while cpnl > 0; LONGs: highest, SHORTs: lowest
_candle_close_history: dict = {}
_candle_high_history: dict = {}
# keyed by trade key
# value: list of last 3 candle
# close PnL values in order
# oldest first e.g. [c1, c2, c3]

# Ã¢ÂÂÃ¢ÂÂ Per-pair adverse dollar cut thresholds Ã¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂ
# If adverse PnL <= -threshold AND max favourable excursion < $10, cut immediately.
ADVERSE_CUT_USD: dict[str, float] = {
    "@107":  45.0,
    "WIF":   45.0,
    "SUI":   45.0,
    "NEAR":  50.0,
    "BTC":   50.0,
    "DOGE":  55.0,
    "ETH":   55.0,
    "AVAX":  60.0,
    "SOL":   65.0,
    "XRP":   65.0,
    "LTC":   50.0,
    "ADA":   55.0,
}
ADVERSE_CUT_DEFAULT_USD: float = 60.0

# Ã¢ÂÂÃ¢ÂÂ Bot identity Ã¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂ
BOT_INSTANCE_ID: str        = "default"
_BOT_IDENTITY_COMMITTED: bool = False
_prev_session:      str              = ""

# Ã¢ÂÂÃ¢ÂÂ Global safety state Ã¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂ
consecutive_losses:     int   = 0
circuit_breaker_active: bool  = False
daily_pnl:              float = 0.0
trading_halted_today:   bool  = False
_last_midnight_day:     int   = datetime.now(ET).day


# Ã¢ÂÂÃ¢ÂÂ App state Ã¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂ

class AppState:
    def __init__(self):
        self.pair_states:          list[dict]        = []
        self.alerts:               list[dict]        = []
        self.prices:               dict[str, float]  = {}
        self.price_changes:        dict[str, float]  = {}
        self.open_trades:          dict[str, dict]   = {}
        self.trade_log:            list[dict]        = []
        self.margin_deployed:      float             = 0.0
        self.trades_opened:        int               = 0
        self.last_scan_at:         Optional[int]     = None
        self.scan_snapshots:       dict              = {}  # symbol -> last 3 scan snapshots
        self.market_health:        dict              = {}
        self.price_stale:          dict[str, bool]   = {}  # symbols with stale price feed
        self.price_updated_at:      dict[str, float]  = {}  # symbol -> unix timestamp of last successful price write

    @property
    def slots_used(self) -> int:
        return len(self.open_trades)

    @property
    def cap_reached(self) -> bool:
        return self.margin_deployed >= MARGIN_HARD_CAP

    def trade_key(self, symbol: str, direction: str) -> str:
        return f"{symbol}{direction}"

    def serialise(self) -> dict:
        global consecutive_losses, circuit_breaker_active, daily_pnl, trading_halted_today

        trades_out = {}
        for k, t in self.open_trades.items():
            entry   = t["entry_price"]
            current = self.prices.get(t["symbol"], entry)
            dir_    = t["direction"]
            size    = t.get("remaining_size", t.get("size", 0))
            margin  = t.get("margin", 0)
            lev     = t.get("leverage", 1)
            sl_dist = t.get("sl_dist", 0) or 0

            pnl = (current - entry) * size if dir_ == "LONG" else (entry - current) * size
            dollar_risk = margin * lev * (sl_dist / entry) if entry else 0
            r   = round(pnl / dollar_risk, 2) if dollar_risk else 0

            trailing_sl = None
            if t.get("tp1_hit") and t.get("extreme_price"):
                ep = t["extreme_price"]
                atr = t.get("sl_dist", 0) or 0
                trailing_sl = round(ep * (1 + 0.005) if dir_ == "SHORT"
                                    else ep * (1 - 0.005), 6)

            trades_out[k] = {
                **t,
                "current_price":  current,
                "unrealized_pnl": round(pnl, 2),
                "r":              r,
                "elapsed_s":      int(time.time()) - t.get("opened_at", int(time.time())),
                "trailing_sl":    trailing_sl,
            }

        pair_states_out = []
        for ps in self.pair_states:
            sym = ps.get("symbol", "")
            pair_states_out.append({
                **ps,
                "cooldown_short": get_cooldown_remaining(sym, "SHORT"),
                "cooldown_long":  get_cooldown_remaining(sym, "LONG"),
            })

        pair_order = {s: i for i, s in enumerate(PAIRS)}
        pair_states_out.sort(key=lambda p: pair_order.get(p.get("symbol", ""), 999))

        for i, ps in enumerate(pair_states_out):
            sym = ps.get("symbol", "")
            kl, ks = self.trade_key(sym, "LONG"), self.trade_key(sym, "SHORT")
            in_trade = kl in trades_out or ks in trades_out
            cd_s = get_cooldown_remaining(sym, "SHORT")
            cd_l = get_cooldown_remaining(sym, "LONG")
            _lgcd_l = max(0, int(_large_sl_cooldowns.get(f"{sym}LONG",  0) - time.time()))
            _lgcd_s = max(0, int(_large_sl_cooldowns.get(f"{sym}SHORT", 0) - time.time()))
            _sess   = get_session_name()
            _j15m   = ps.get("j15m",   50)
            _j1h    = ps.get("j1h",    50)
            _rsi    = ps.get("rsi15m", 50)
            _bid    = ps.get("bid_pct", 50)
            _ask    = ps.get("ask_pct", 50)
            _gates_long  = [
                _j15m < _scanner_mod.J15M_LONG_GATE,
                _j1h  < _scanner_mod.J1H_LONG_MAX,
                _rsi  < _scanner_mod.RSI15M_LONG_MAX,
                _bid  >= _scanner_mod.DEPTH_GATE_PCT,
                f"{sym}_LONG_{_sess}"  not in _session_halted,
                cd_l == 0 and _lgcd_l == 0,
            ]
            _gates_short = [
                _j15m > _scanner_mod.J15M_SHORT_GATE,
                _j1h  > _scanner_mod.J1H_SHORT_MIN and _j1h < _scanner_mod.J1H_SHORT_MAX,
                _rsi  > _scanner_mod.RSI15M_SHORT_MIN,
                _ask  >= _scanner_mod.DEPTH_GATE_PCT,
                f"{sym}_SHORT_{_sess}" not in _session_halted,
                cd_s == 0 and _lgcd_s == 0,
            ]
            pair_states_out[i] = {
                **ps,
                "in_trade":           in_trade,
                "cooldown_short":     cd_s,
                "cooldown_long":      cd_l,
                "session_halted_long":  f"{sym}_LONG_{_sess}"  in _session_halted,
                "session_halted_short": f"{sym}_SHORT_{_sess}" in _session_halted,
                "large_sl_cd_long":     _lgcd_l or None,
                "large_sl_cd_short":    _lgcd_s or None,
                "gates_long":           _gates_long,
                "gates_short":          _gates_short,
            }

        return {
            "pair_states":    pair_states_out,
            "thresholds": {
                "j15m_short":    _scanner_mod.J15M_SHORT_GATE,
                "j15m_long":     _scanner_mod.J15M_LONG_GATE,
                "j1h_long_max":  _scanner_mod.J1H_LONG_MAX,
                "j1h_short_min": _scanner_mod.J1H_SHORT_MIN,
                "j1h_short_max": _scanner_mod.J1H_SHORT_MAX,
                "rsi_short_min": _scanner_mod.RSI15M_SHORT_MIN,
                "rsi_long_max":  _scanner_mod.RSI15M_LONG_MAX,
                "depth_gate":    _scanner_mod.DEPTH_GATE_PCT,
                "depth_short_min": _scanner_mod.DEPTH_SHORT_MIN,
                "depth_long_min":  _scanner_mod.DEPTH_LONG_MIN,
                "j5m_short_min":   _scanner_mod.J5M_SHORT_MIN,
                "j5m_long_max":    _scanner_mod.J5M_LONG_MAX,
            },
            "session":        get_session_name(),
            "alerts":         self.alerts,
            "prices":         self.prices,
            "open_trades":    trades_out,
            "trade_log":      self.trade_log,
            "unrealized_pnl": round(sum(t.get("unrealized_pnl", 0) for t in trades_out.values()), 2),
            "account": {
                "cap":             MARGIN_HARD_CAP,
                "cap_pct":         round(self.margin_deployed / MARGIN_HARD_CAP * 100, 1),
                "cap_reached":     self.cap_reached,
                "trades_opened":   self.trades_opened,
                "paper_mode":            PAPER_MODE,
                "live_manual_entry_only": LIVE_MANUAL_ENTRY_ONLY,
                "slots_used":            self.slots_used,
            },
            "circuit_breaker": {
                "active":             circuit_breaker_active,
                "consecutive_losses": consecutive_losses,
                "stop_at":            CONSECUTIVE_LOSS_STOP,
            },
            "daily": {
                "pnl":    round(daily_pnl, 2),
                "limit":  DAILY_LOSS_LIMIT,
                "halted": trading_halted_today,
            },
            "scan_count":       get_scan_count(),
            "last_scan_at":     self.last_scan_at,
            "price_changes":    self.price_changes,
            "deploy_time":      DEPLOY_TIME,
            "market_health":    self.market_health,
        }


app_state  = AppState()
hl_client:   Optional[HLClient]   = None
mexc_client: Optional[MexcClient] = None


# Ã¢ÂÂÃ¢ÂÂ Helpers Ã¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂ

def _retire_alert(symbol: str, direction: str):
    app_state.alerts = [
        a for a in app_state.alerts
        if not (a["symbol"] == symbol and a["direction"] == direction)
    ]


# Ã¢ÂÂÃ¢ÂÂ Persistence Ã¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂ

# Ã¢ÂÂÃ¢ÂÂ Supabase client Ã¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂ

_supabase:             Optional[Client]   = None
_last_save_fail_alert: Optional[datetime] = None


def _get_supabase() -> Optional[Client]:
    global _supabase
    if _supabase is None:
        if SUPABASE_URL and SUPABASE_KEY:
            try:
                _supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
            except Exception as _e:
                print(f"[PERSIST] Supabase client init error: {_e}")
        else:
            print("[PERSIST] SUPABASE_URL/KEY not set Ã¢ÂÂ persistence disabled")
    return _supabase


def _alert_save_failure(error_msg: str) -> None:
    """Telegram alert on _save_state() failure Ã¢ÂÂ at most once per 5 min (cooldown)."""
    global _last_save_fail_alert
    now = datetime.now(timezone.utc)
    if _last_save_fail_alert and (now - _last_save_fail_alert) < timedelta(minutes=5):
        return
    _last_save_fail_alert = now
    msg = (
        "Ã¢ÂÂ Ã¯Â¸Â HL PERSIST FAILURE Ã¢ÂÂ _save_state() raised:\n"
        + error_msg
        + "\n\nCheck hl_scanner_state immediately. State is NOT being saved."
    )
    if TELEGRAM_ENABLED:
        threading.Thread(target=lambda m=msg: _tg_post(m), daemon=True).start()


def _save_state():
    """Upsert full scanner state to Supabase hl_scanner_state table (row id=1)."""
    sb = _get_supabase()
    if sb is None:
        return
    try:
        data = {
            "id":                     1,
            "saved_date":             datetime.now(ET).strftime("%Y-%m-%d"),
            "open_trades":            app_state.open_trades,
            "margin_deployed":        app_state.margin_deployed,
            "daily_pnl":              daily_pnl,
            "trading_halted_today":   trading_halted_today,
            "consecutive_losses":     consecutive_losses,
            "circuit_breaker_active": circuit_breaker_active,
            "cooldowns":              dict(_scanner_mod._cooldowns),
            "peak_shadow":            dict(_peak_shadow),
            "adverse_shadow":         dict(_adverse_shadow),
            "signal_shadow":          dict(_signal_shadow),
            "updated_at":             datetime.now(timezone.utc).isoformat(),
        }
        sb.table("hl_scanner_state").upsert(data).execute()
    except Exception as _e:
        print(f"[PERSIST] save error: {_e}")
        _alert_save_failure(str(_e))


def _load_state():
    """On startup: restore all state from Supabase."""
    global daily_pnl, trading_halted_today, consecutive_losses, circuit_breaker_active, PAPER_MODE, TELEGRAM_ENABLED, DAILY_LOSS_LIMIT, MARGIN_PER_TRADE, CONSECUTIVE_LOSS_STOP, MARGIN_HARD_CAP, TRAIL_ATR_MULTIPLIER
    sb = _get_supabase()
    if sb is None:
        print("[RESTORE] No Supabase client Ã¢ÂÂ starting fresh")
        return
    try:
        # Ã¢ÂÂÃ¢ÂÂ Trade log Ã¢ÂÂ in-memory list Ã¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂ
        log_rows = sb.table("hl_trade_log").select("*").order("created_at").limit(1000).execute()
        if log_rows.data:
            for row in [r for r in log_rows.data if r.get("close_time") is not None]:
                def _ts(iso):
                    if not iso:
                        return 0
                    try:
                        return int(datetime.fromisoformat(
                            iso.replace("Z", "+00:00")).timestamp())
                    except Exception:
                        return 0
                def _fn(k):
                    v = row.get(k)
                    return float(v) if v is not None else None
                app_state.trade_log.append({
                    "timestamp_opened": _ts(row.get("open_time")),
                    "timestamp_closed": _ts(row.get("close_time")),
                    "symbol":           row.get("pair", ""),
                    "direction":        row.get("direction", ""),
                    "tier":             row.get("tier"),
                    "adx1h":            None,
                    "score":            None,
                    "entry_price":      _fn("entry_price"),
                    "sl_price":         _fn("sl"),
                    "tp1_price":        _fn("tp1"),
                    "tp2_price":        _fn("tp2"),
                    "exit_price":       _fn("exit_price"),
                    "exit_reason":      row.get("exit_reason", ""),
                    "pnl_usd":          float(row.get("pnl_dollars") or 0),
                    "r_value":          float(row.get("r_value") or 0),
                    "duration_seconds": int(row.get("duration_seconds") or 0),
                    "exchange":         row.get("exchange", "HL"),
                    "session_opened":   row.get("session_opened"),
                    "mae_r":            float(row.get("mae_r")) if row.get("mae_r") is not None else None,
                    "mfe_r":            float(row.get("mfe_r")) if row.get("mfe_r") is not None else None,
                    "paper":            True,
                })
            print(f"[RESTORE] trade log: {len(log_rows.data)} entries restored")

        # Ã¢ÂÂÃ¢ÂÂ Scanner state Ã¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂ
        result = sb.table("hl_scanner_state").select("*").eq("id", 1).execute()
        if not result.data:
            print("[RESTORE] No state row found Ã¢ÂÂ starting fresh")
            return
        data = result.data[0]

        # ── Restore settings from Supabase
        if data.get("paper_mode") is not None:
            PAPER_MODE = bool(data["paper_mode"])
            _scanner_mod.PAPER_MODE = PAPER_MODE
        if data.get("telegram_enabled") is not None:
            TELEGRAM_ENABLED = bool(
                data["telegram_enabled"])
        if data.get("depth_gate_pct") is not None:
            _scanner_mod.DEPTH_GATE_PCT = float(
                data["depth_gate_pct"])
        if data.get("adx_min_long") is not None:
            _scanner_mod.ADX_MIN_LONG = float(
                data["adx_min_long"])
        if data.get("j15m_short_gate") is not None:
            _scanner_mod.J15M_SHORT_GATE = float(
                data["j15m_short_gate"])
        if data.get("j15m_long_gate") is not None:
            _scanner_mod.J15M_LONG_GATE = float(
                data["j15m_long_gate"])
        if data.get("j1h_short_min") is not None:
            _scanner_mod.J1H_SHORT_MIN = float(
                data["j1h_short_min"])
        if data.get("j1h_short_max") is not None:
            _scanner_mod.J1H_SHORT_MAX = float(
                data["j1h_short_max"])
        if data.get("atr_sl_multiplier") is not None:
            _scanner_mod.ATR_SL_MULTIPLIER = float(
                data["atr_sl_multiplier"])
        if data.get("tp1_close_pct") is not None:
            _scanner_mod.TP1_CLOSE_PCT = float(
                data["tp1_close_pct"])
        if data.get("tp2_r") is not None:
            _scanner_mod.TP2_R = float(
                data["tp2_r"])
        if data.get("margin_per_trade") is not None:
            MARGIN_PER_TRADE = float(
                data["margin_per_trade"])
            _scanner_mod.MARGIN_PER_TRADE = \
                MARGIN_PER_TRADE
        if data.get("daily_loss_limit") is not None:
            DAILY_LOSS_LIMIT = float(
                data["daily_loss_limit"])
        if data.get("consecutive_loss_stop") is not None:
            CONSECUTIVE_LOSS_STOP = int(
                data["consecutive_loss_stop"])
            _scanner_mod.CONSECUTIVE_LOSS_STOP = \
                CONSECUTIVE_LOSS_STOP
        if data.get("kill_cooldown_seconds") is not None:
            _scanner_mod.PAIR_COOLDOWN_SECONDS = int(
                data["kill_cooldown_seconds"])
        if data.get("kill_grace_seconds") is not None:
            _scanner_mod.KILL_GRACE_SECONDS = int(
                data["kill_grace_seconds"])
        if data.get(
                "j1h_long_max") \
                is not None:
            _scanner_mod\
                .J1H_LONG_MAX = \
                float(data[
                    "j1h_long_max"])
        if data.get("j1h_long_min") \
                is not None:
            _scanner_mod\
                .J1H_LONG_MIN = \
                float(data[
                    "j1h_long_min"])
        if data.get(
                "se_j1h_decay_pts") \
                is not None:
            _scanner_mod\
                .SE_J1H_DECAY_PTS = \
                float(data[
                    "se_j1h_decay_pts"])
        if data.get(
                "kill_pct_floor") \
                is not None:
            _scanner_mod\
                .KILL_PCT_FLOOR = \
                float(data[
                    "kill_pct_floor"])
        if data.get("j5m_short_min") is not None:
            _scanner_mod.J5M_SHORT_MIN = float(data["j5m_short_min"])
        if data.get("j5m_long_max") is not None:
            _scanner_mod.J5M_LONG_MAX = float(data["j5m_long_max"])
        if data.get("depth_short_min") is not None:
            _scanner_mod.DEPTH_SHORT_MIN = float(data["depth_short_min"])
        if data.get("depth_long_min") is not None:
            _scanner_mod.DEPTH_LONG_MIN = float(data["depth_long_min"])
        if data.get("leverage") is not None:
            _scanner_mod.LEVERAGE = int(data["leverage"])
        if data.get("margin_hard_cap") is not None:
            MARGIN_HARD_CAP = float(data["margin_hard_cap"])
        if data.get("trail_atr_multiplier") is not None:
            TRAIL_ATR_MULTIPLIER = float(data["trail_atr_multiplier"])

        # Ã¢ÂÂÃ¢ÂÂ New-day check Ã¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂ
        today_str = datetime.now(ET).strftime("%Y-%m-%d")
        if data.get("saved_date") != today_str:
            saved = data.get("saved_date", "unknown")
            print(f"[DAILY RESET] New trading day ({saved} Ã¢ÂÂ {today_str}) Ã¢ÂÂ P&L reset to $0")
            daily_pnl              = 0.0
            trading_halted_today   = False
            consecutive_losses     = 0
            circuit_breaker_active = False
            _save_state()
            return

        # Ã¢ÂÂÃ¢ÂÂ Restore globals Ã¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂ
        daily_pnl              = float(data.get("daily_pnl") or 0)
        trading_halted_today   = bool(data.get("trading_halted_today", False))
        consecutive_losses     = int(data.get("consecutive_losses") or 0)
        circuit_breaker_active = bool(data.get("circuit_breaker_active", False))
        app_state.margin_deployed = float(data.get("margin_deployed") or 0)

        # Ã¢ÂÂÃ¢ÂÂ Restore open trades Ã¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂ
        for key, trade in (data.get("open_trades") or {}).items():
            app_state.open_trades[key] = trade
            print(f"[RESTORE] {trade.get('symbol')} {trade.get('direction')} "
                  f"entry={trade.get('entry_price')} sl={trade.get('sl_price')} "
                  f"tp1={trade.get('tp1_price')} restored")

        # Ã¢ÂÂÃ¢ÂÂ Restore shadow dicts (peak + adverse) Ã¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂ
        for key, sh in (data.get("peak_shadow") or {}).items():
            if key in app_state.open_trades:
                _peak_shadow[key] = sh
        _now_candle_restore = (int(time.time()) // 60) * 60
        for sh in _peak_shadow.values():
            if not sh.get("last_peak_candle_ts"):
                sh["last_peak_candle_ts"] = _now_candle_restore
        for key, sh in (data.get("adverse_shadow") or {}).items():
            if key in app_state.open_trades:
                _adverse_shadow[key] = sh
        for key, sh in (data.get("signal_shadow") or {}).items():
            if key in app_state.open_trades:
                _signal_shadow[key] = sh
        print(f"[RESTORE] shadow dicts Ã¢ÂÂ peak={len(_peak_shadow)} adverse={len(_adverse_shadow)}" + f" signal={len(_signal_shadow)}")

        # -- Sanitize phantom trade-log entries (null/zero exit_price or |R| > 10) --
        _keep_log = []
        _drop_log  = []
        for _e in app_state.trade_log:
            _ep = _e.get("exit_price") or 0
            _rv = abs(_e.get("r_value") or 0)
            if _ep > 0 and _rv <= 10:
                _keep_log.append(_e)
            else:
                _drop_log.append(_e)
        for _ph in _drop_log:
            print(f"[SANITIZE] dropped phantom trade {_ph.get('symbol')} {_ph.get('direction')} "
                  f"pnl={_ph.get('pnl_usd')} r={_ph.get('r_value')} exit_price={_ph.get('exit_price')}")
        if _drop_log:
            app_state.trade_log = _keep_log
            print(f"[SANITIZE] {len(_drop_log)} phantom trade(s) removed from restored log")
            _save_state()
        print(f"[RESTORE] settings restored "
              f"from Supabase")

        # Clear all cooldowns on startup
        # — prevents stale cooldowns
        # from blocking signals after
        # restart. Cooldowns are
        # ephemeral per-session state,
        # not persistent state.
        _scanner_mod._cooldowns.clear()
        print("[STARTUP] All cooldowns"
              " cleared on startup")

        print(f"[RESTORE] complete Ã¢ÂÂ trades={len(app_state.open_trades)} "
              f"daily_pnl=${daily_pnl:.2f} cooldowns={len(_scanner_mod._cooldowns)} "
              f"cb={consecutive_losses}/{CONSECUTIVE_LOSS_STOP}")

    except Exception as _e:
        print(f"[RESTORE] Error: {_e} Ã¢ÂÂ starting fresh")


def _update_daily_pnl(pnl: float):
    global daily_pnl, trading_halted_today
    daily_pnl = round(daily_pnl + pnl, 2)
    if not trading_halted_today and daily_pnl <= DAILY_LOSS_LIMIT:
        trading_halted_today = True
        print(f"[DAILY LIMIT] daily_pnl=${daily_pnl:.2f} Ã¢ÂÂ trading halted")
    _save_state()


def _on_trade_close(reason: str):
    _save_state()


def _get_session(opened_at: int) -> str:
    """Derive session from entry timestamp (America/New_York, DST-aware)."""
    from zoneinfo import ZoneInfo
    dt  = datetime.fromtimestamp(opened_at, tz=ZoneInfo("America/New_York"))
    hm  = dt.hour * 60 + dt.minute
    if hm >= 20 * 60 or hm < 3 * 60:  return "ASIA"
    if hm < 9 * 60 + 30:              return "EU"
    if hm < 16 * 60:                  return "US"
    return "OFF"


def _append_trade_log(trade: dict, exit_price: float, reason: str, pnl: float, r: float):
    if not exit_price or exit_price <= 0:
        raise ValueError(
            f"[ASSERT] _append_trade_log: exit_price={exit_price!r} "
            f"symbol={trade.get('symbol')} direction={trade.get('direction')} reason={reason} "
            f"-- refusing to write trade row with null/zero price"
        )
    now_ts    = int(time.time())
    opened_at = trade.get("opened_at", now_ts)
    is_short  = trade.get("direction") == "SHORT"

    # Ã¢ÂÂÃ¢ÂÂ MAE / MFE in R units Ã¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂ
    _entry  = trade.get("entry_price") or 0
    _sl_d   = trade.get("sl_dist") or (
        abs(_entry - (trade.get("sl_price") or 0)) if _entry else 0
    )
    _ep     = trade.get("extreme_price")
    _ap     = trade.get("adverse_price")
    _mfe_r  = (
        round((((_entry - _ep) if is_short else (_ep - _entry)) / _sl_d), 2)
        if (_ep is not None and _sl_d and _entry) else None
    )
    _mae_r  = (
        round((((_entry - _ap) if is_short else (_ap - _entry)) / _sl_d), 2)
        if (_ap is not None and _sl_d and _entry) else None
    )
    _session = trade.get("session") or _get_session(opened_at)

    # Ã¢ÂÂÃ¢ÂÂ In-memory entry (powers the LOG tab + CSV export) Ã¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂ
    entry = {
        "timestamp_opened": opened_at,
        "timestamp_closed": now_ts,
        "symbol":           trade["symbol"],
        "direction":        trade["direction"],
        "score":            trade.get("score"),
        "adx1h":            trade.get("adx1h"),
        "tier":             trade.get("tier"),
        "entry_price":      trade.get("entry_price"),
        "sl_price":         trade.get("sl_price"),
        "tp1_price":        trade.get("tp1_price"),
        "tp2_price":        trade.get("tp2_price"),
        "exit_price":       exit_price,
        "exit_reason":      reason,
        "pnl_usd":          round(pnl, 2),
        "r_value":          r,
        "duration_seconds": now_ts - opened_at,
        "exchange":         trade.get("exchange", "HL"),
        "paper":            trade.get("paper", True),
        "session_opened":   _session,
        "mae_r":            _mae_r,
        "mfe_r":            _mfe_r,
                  "btc_j1h_entry":    trade.get("btc_j1h_entry"),
    }
    app_state.trade_log.append(entry)

    # Ã¢ÂÂÃ¢ÂÂ Supabase insert Ã¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂ
    sb = _get_supabase()
    if sb is not None:
        try:
            open_iso  = datetime.fromtimestamp(opened_at, tz=timezone.utc).isoformat()
            close_iso = datetime.fromtimestamp(now_ts,    tz=timezone.utc).isoformat()
            _tl_row = {
                "pair":             trade["symbol"],
                "direction":        trade["direction"],
                "tier":             trade.get("tier"),
                "leverage":         trade.get("leverage"),
                "exchange":         trade.get("exchange", "HL"),
                "entry_price":      trade.get("entry_price"),
                "exit_price":       exit_price,
                "sl":               trade.get("sl_price"),
                "tp1":              trade.get("tp1_price"),
                "tp2":              trade.get("tp2_price"),
                "exit_reason":      reason,
                "pnl_dollars":      round(pnl, 2),
                "r_value":          r,
                "open_time":        open_iso,
                "close_time":       close_iso,
                "duration_seconds": now_ts - opened_at,
                "stoch_k":          trade.get("stoch_k"),
                "stoch_d":          trade.get("stoch_d"),
                "session_opened":   _session,
                "j15m_entry":       trade.get("j15m"),
                "j1h_entry":        trade.get("j1h"),
                "stoch_k_entry":    trade.get("stoch_k"),
                "stoch_d_entry":    trade.get("stoch_d"),
                "rsi_entry":        trade.get("rsi15m"),
                "depth_pct_entry":  trade.get("bid_pct") if not is_short else trade.get("ask_pct"),
                "chg24h_entry":     trade.get("chg24h"),
                "score":            trade.get("score"),
                "adx1h":            trade.get("adx1h"),
                "mae_r":            _mae_r,
                "mfe_r":            _mfe_r,
                "size":             trade.get("size", None),
            }
            if trade.get("vwap_at_entry") is not None:
                _tl_row["vwap_at_entry"] = trade.get("vwap_at_entry")
                _tl_row["vwap_pct_diff"] = trade.get("vwap_pct_diff")
                _tl_row["vwap_position"] = trade.get("vwap_position")
            sb.table("hl_trade_log")\
                .update(_tl_row)\
                .eq("pair",      trade["symbol"])\
                .eq("direction", trade["direction"])\
                .eq("open_time", open_iso)\
                .is_("close_time", "null")\
                .execute()
            # Fallback: try pair name with _USDT suffix in case alert_log stored it differently
            _sym_alt = trade["symbol"] + "_USDT" if not trade["symbol"].endswith("_USDT") else trade["symbol"].replace("_USDT", "")
            if _sym_alt != trade["symbol"]:
                try:
                    sb.table("hl_trade_log")\
                        .update(_tl_row)\
                        .eq("pair",      _sym_alt)\
                        .eq("direction", trade["direction"])\
                        .eq("open_time", open_iso)\
                        .is_("close_time", "null")\
                        .execute()
                except Exception:
                    pass
        except Exception as _e:
            print(f"[PERSIST] hl_trade_log insert error: {_e}")

async def _resolve_bot_identity(exchange: str) -> None:
      """Resolve BOT_INSTANCE_ID from Supabase bot_identity table or env-var fallback.

      Called once at startup. Falls back silently if Supabase is unavailable.
      """
      global BOT_INSTANCE_ID, _BOT_IDENTITY_COMMITTED
      sb = _get_supabase()
      if sb:
          try:
              result = sb.table("bot_identity").select("*").eq("exchange", exchange).execute()
              if result.data:
                  BOT_INSTANCE_ID = result.data[0]["bot_instance_id"]
                  _BOT_IDENTITY_COMMITTED = True
                  print(f"[BOT IDENTITY] Resolved from Supabase: {BOT_INSTANCE_ID} (committed)")
                  return
          except Exception as _e:
              print(f"[BOT IDENTITY] Supabase lookup failed -- using env-var fallback: {_e}")
      BOT_INSTANCE_ID = (
          os.environ.get("BOT_INSTANCE_ID")
          or os.environ.get("RAILWAY_SERVICE_ID", "default")
      )
      _BOT_IDENTITY_COMMITTED = False
      print(f"[BOT IDENTITY] Auto-derived (not committed): {BOT_INSTANCE_ID}")


async def _open_trade_log_row(trade: dict):
    """Insert an entry-analytics snapshot into hl_trade_log at trade-open time.

    If any column is missing, run once in Supabase SQL editor:
      ALTER TABLE hl_trade_log ADD COLUMN IF NOT EXISTS j15m_entry       float;
      ALTER TABLE hl_trade_log ADD COLUMN IF NOT EXISTS j1h_entry        float;
      ALTER TABLE hl_trade_log ADD COLUMN IF NOT EXISTS stoch_k_entry    float;
      ALTER TABLE hl_trade_log ADD COLUMN IF NOT EXISTS stoch_d_entry    float;
      ALTER TABLE hl_trade_log ADD COLUMN IF NOT EXISTS rsi_entry        float;
      ALTER TABLE hl_trade_log ADD COLUMN IF NOT EXISTS depth_pct_entry  float;
      ALTER TABLE hl_trade_log ADD COLUMN IF NOT EXISTS chg24h_entry     float;
      ALTER TABLE hl_trade_log ADD COLUMN IF NOT EXISTS session_opened   text;
      ALTER TABLE hl_trade_log ADD COLUMN IF NOT EXISTS mae_r            float;
      ALTER TABLE hl_trade_log ADD COLUMN IF NOT EXISTS mfe_r            float;
      ALTER TABLE hl_trade_log ADD COLUMN IF NOT EXISTS score           integer;
      ALTER TABLE hl_trade_log ADD COLUMN IF NOT EXISTS adx1h           float;
    """
    sb = _get_supabase()
    if not sb:
        return
    try:
        is_short  = trade.get("direction") == "SHORT"
        opened_at = trade.get("opened_at", int(time.time()))
        open_iso  = datetime.fromtimestamp(opened_at, tz=timezone.utc).isoformat()
        sb.table("hl_trade_log").insert({
            "pair":            trade["symbol"],
            "direction":       trade["direction"],
            "tier":            trade.get("tier"),
            "leverage":        trade.get("leverage"),
            "exchange":        trade.get("exchange", "HL"),
            "entry_price":     trade.get("entry_price"),
            "sl":              trade.get("sl_price"),
            "tp1":             trade.get("tp1_price"),
            "tp2":             trade.get("tp2_price"),
            "open_time":       open_iso,
            "session_opened":  trade.get("session") or _get_session(opened_at),
            "j15m_entry":      trade.get("j15m"),
            "j1h_entry":       trade.get("j1h"),
            "stoch_k_entry":   trade.get("stoch_k"),
            "stoch_d_entry":   trade.get("stoch_d"),
            "rsi_entry":       trade.get("rsi15m"),
            "depth_pct_entry": trade.get("bid_pct") if not is_short else trade.get("ask_pct"),
            "chg24h_entry":    trade.get("chg24h"),
            "score":           trade.get("score"),
            "adx1h":           trade.get("adx1h"),
            "j5m_entry":                 trade.get("j5m"),
            "btc_regime_context":        trade.get("btc_regime_context"),
            "depth_bid_pct_entry":       trade.get("depth_bid_pct"),
            "depth_ask_pct_entry":       trade.get("depth_ask_pct"),
            "depth_context_entry":       trade.get("depth_context"),
            "vol_surge_entry":           trade.get("vol_surge"),
            "ma_stack_1h_entry":         trade.get("ma_stack_1h"),
            "j1h_prev_entry":            trade.get("j1h_prev"),
            "j1h_short_direction_entry": trade.get("j1h_short_direction"),
            "btc_j1h_entry":             trade.get("btc_j1h_entry"),
            "size":                      trade.get("size"),
            "vwap_at_entry":             trade.get("vwap_at_entry"),
            "vwap_pct_diff":             trade.get("vwap_pct_diff"),
            "vwap_position":             trade.get("vwap_position"),
        }).execute()
        print(f"[TRADE LOG OPEN] {trade['symbol']} {trade['direction']} open-row written to hl_trade_log")
    except Exception as _e:
        print(f"[TRADE LOG WRITE FAILED] hl_trade_log open-row: {_e}")


async def _do_open_trade(
    symbol: str, direction: str,
    margin_usdc: float, leverage: int,
    alert_data: Optional[dict] = None,
    exchange: str = "HL",
) -> tuple[Optional[dict], Optional[str]]:
    global circuit_breaker_active, trading_halted_today

    if trading_halted_today:
        asyncio.create_task(
            _log_alert_outcome(
                {"symbol": symbol, "direction": direction},
                "BLOCKED_DAILY_LIMIT",
                exchange,
            ))
        return None, "daily_limit"

    key = app_state.trade_key(symbol, direction)
    if key in app_state.open_trades:
        asyncio.create_task(
            _log_alert_outcome(
                {"symbol": symbol, "direction": direction},
                "BLOCKED_ALREADY_OPEN",
                exchange,
            ))
        return None, "already_open"

    lock_key = f"{exchange}:{symbol}:{direction}:{BOT_INSTANCE_ID}"
    _sb = _get_supabase()
    if _sb:
        try:
            _thirty_ago = (datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat()
            _sb.table("trade_open_locks").delete().eq("lock_key", lock_key).lt("created_at", _thirty_ago).execute()
            _sb.table("trade_open_locks").insert({
                "lock_key":        lock_key,
                "exchange":        exchange,
                "symbol":          symbol,
                "direction":       direction,
                "bot_instance_id": BOT_INSTANCE_ID,
            }).execute()
        except Exception as _lock_e:
            _err_str = str(_lock_e).lower()
            _is_duplicate = (
                "duplicate" in _err_str or
                "unique" in _err_str or
                "conflict" in _err_str or
                "23505" in _err_str
            )
            if _is_duplicate:
                _msg = (
                    f"\u26a0 DUPLICATE BLOCKED: "
                    f"{symbol} {direction} - "
                    f"signal already open"
                )
            else:
                _msg = (
                    f"\u26a0 LOCK ERROR: "
                    f"{symbol} {direction} - "
                    f"Supabase unavailable, "
                    f"allowing trade: {_lock_e}"
                )
                if TELEGRAM_ENABLED:
                    threading.Thread(
                        target=lambda m=_msg: _tg_post(m),
                        daemon=True
                    ).start()
                print(f"[LOCK ERROR] {lock_key} - "
                      f"infrastructure failure, "
                      f"proceeding: {_lock_e}")
                # Infrastructure failure - do not block
                # the trade, just skip the lock
                lock_key = None
                # fall through to trade open
            if _is_duplicate:
                if TELEGRAM_ENABLED:
                    threading.Thread(
                        target=lambda m=_msg: _tg_post(m),
                        daemon=True
                    ).start()
                print(f"[LOCK CONFLICT] {lock_key} - "
                      f"blocked duplicate open: {_lock_e}")
                return None, "already_open"

    _client = mexc_client if exchange == "MEXC" else hl_client
    sl_price = alert_data.get("sl_price") if alert_data else None
    result   = await _client.open_position(
        symbol, direction, margin_usdc, leverage, sl_price=sl_price
    )
    if result.get("status") != "ok":
        asyncio.create_task(
            _log_alert_outcome(
                {"symbol": symbol, "direction": direction},
                "BLOCKED_OPEN_FAILED",
                exchange,
            ))
        return None, result.get("msg", "open_failed")

    entry = result["entry_price"]
    if not entry or entry == 0.0:
        print(f"[TRADE BLOCKED] {symbol} {direction} null price rejected")
        asyncio.create_task(
            _log_alert_outcome(
                {"symbol": symbol, "direction": direction},
                "BLOCKED_NULL_PRICE",
                exchange,
            ))
        return None, "null_price"

    if not sl_price or sl_price <= 0:
        print(f"[OPEN BLOCKED] {symbol} {direction} "
              f"sl_price invalid: {sl_price} - "
              f"refusing to open without valid SL")
        if lock_key and _sb:
            try:
                _sb.table("trade_open_locks")\
                   .delete()\
                   .eq("lock_key", lock_key)\
                   .execute()
            except Exception:
                pass
        asyncio.create_task(
            _log_alert_outcome(
                {"symbol": symbol, "direction": direction},
                "BLOCKED_INVALID_SL",
                exchange,
            ))
        return None, "invalid_sl"

    size = result.get("size", (margin_usdc * leverage) / entry if entry else 0)

    trade = {
        "symbol":     symbol,
        "direction":  direction,
        "entry_price": entry,
        "size":       size,
        "remaining_size": size,
        "margin":     margin_usdc,
        "leverage":   leverage,
        "opened_at":  int(time.time()),
        "paper":      result.get("paper", True),
        "exchange":   exchange,
        "sl_price":   alert_data.get("sl_price")  if alert_data else None,
        "sl_dist": (
            max(
                abs(entry - sl_price),
                entry * 0.001
            ) if sl_price else None
        ),
        "tp1_price":  alert_data.get("tp1_price") if alert_data else None,
        "tp2_price":  alert_data.get("tp2_price") if alert_data else None,
        "score":      alert_data.get("score")     if alert_data else None,
        "tier":       alert_data.get("tier")      if alert_data else None,
        "adx1h":      alert_data.get("adx1h")     if alert_data else None,
        "j15m":       alert_data.get("j15m")      if alert_data else None,
        "j1h":        alert_data.get("j1h")       if alert_data else None,
        "j5m":
            alert_data.get("j5m")
            if alert_data else None,
        "btc_regime_context":
            alert_data.get(
                "btc_regime_context")
            if alert_data else None,
        "depth_bid_pct":
            alert_data.get(
                "depth_bid_pct")
            if alert_data else None,
        "depth_ask_pct":
            alert_data.get(
                "depth_ask_pct")
            if alert_data else None,
        "depth_context":
            alert_data.get(
                "depth_context")
            if alert_data else None,
        "j1h_prev":
            alert_data.get(
                "j1h_prev")
            if alert_data else None,
        "j1h_short_direction":
            alert_data.get(
                "j1h_short_direction")
            if alert_data else None,
        "ma_stack_1h":
            alert_data.get(
                "ma_stack_1h")
            if alert_data else None,
        "vol_15m":
            alert_data.get(
                "vol_15m")
            if alert_data else None,
        "vol_ma15m":
            alert_data.get(
                "vol_ma15m")
            if alert_data else None,
        "vol_surge":
            alert_data.get(
                "vol_surge")
            if alert_data else None,
        "rsi15m":     alert_data.get("rsi15m")    if alert_data else None,
        "stoch_k":    alert_data.get("stoch_k")    if alert_data else None,
        "stoch_d":    alert_data.get("stoch_d")    if alert_data else None,
        "bid_pct":    alert_data.get("bid_pct")   if alert_data else None,
        "ask_pct":    alert_data.get("ask_pct")   if alert_data else None,
        "be_price":   round(entry * 1.001, 6) if direction == "LONG" else round(entry * 0.999, 6),
        "be_confirm_price":
            alert_data.get(
                "be_confirm_price")
            if alert_data else None,
        "tp1_hit":       False,
        "partial_hit":   False,
        "is_score10":    alert_data.get("is_score10", False) if alert_data else False,
        "partial_price": alert_data.get("partial_price")     if alert_data else None,
        "session":       alert_data.get("session", "")       if alert_data else "",
        "extreme_price": entry,
        "adverse_price": entry,
        "chg24h":        alert_data.get("chg24h") if alert_data else None,
        "_lock_key":     lock_key,
        "btc_regime_entry":  _get_btc_regime(),
        "stoch_k_fast":      alert_data.get("stoch_k_fast") if alert_data else None,
        "stoch_d_fast":      alert_data.get("stoch_d_fast") if alert_data else None,
        "btc_correlation":   _scanner_mod.BTC_CORRELATION.get(symbol, 0.75),
        "vwap_at_entry":     alert_data.get("vwap_at_entry") if alert_data else None,
        "vwap_pct_diff":     alert_data.get("vwap_pct_diff") if alert_data else None,
        "vwap_position":     alert_data.get("vwap_position") if alert_data else None,
        "btc_j1h_entry":    _scanner_mod._btc_j1h,
        "dollar_risk":      (margin_usdc * leverage * (max(abs(entry - sl_price), entry * 0.001) / entry)
                            if entry and sl_price else 0.0),
    }

    app_state.open_trades[key] = trade
    app_state.margin_deployed += margin_usdc
    app_state.trades_opened   += 1

    asyncio.create_task(_open_trade_log_row(trade))
    asyncio.create_task(
        _log_alert_outcome(
            trade,
            "TRADE_OPENED",
            exchange,
        ))

    for a in app_state.alerts:
        if a["symbol"] == symbol and a["direction"] == direction:
            a["is_in_trade"] = True

    print(f"[TRADE OPEN] {symbol} {direction} tier={trade.get('tier')} "
          f"entry={entry} sl={trade.get('sl_price')} tp1={trade.get('tp1_price')} "
          f"lev={leverage}x exchange={exchange}")
    _save_state()
    return trade, None


# Ã¢ÂÂÃ¢ÂÂ Telegram alerting Ã¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂ

_TREND_EMOJI = {
    "Strong Bull": "Ã°ÂÂÂ",
    "Bullish":     "Ã°ÂÂÂ",
    "Neutral":     "Ã¢ÂÂ¡Ã¯Â¸Â",
    "Bearish":     "Ã°ÂÂÂ",
    "Strong Bear": "Ã°ÂÂÂ»",
}


def _fmt_p(v: float) -> str:
    if v >= 1000: return f"{v:,.2f}"
    if v >= 1:    return f"{v:.4f}"
    return f"{v:.6f}"


def _tg_post(msg: str) -> None:
    """POST to Telegram in a daemon thread Ã¢ÂÂ never blocks the scan loop."""
    def _send(text: str, parse_mode: str) -> None:
        url  = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        data = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": parse_mode}
        try:
            requests.post(url, json=data, timeout=10)
        except Exception as _e:
            print(f"[TG] send error: {_e}")

    full_msg = (
        "\U0001F7E3  HL BOUNCE\n"
        + msg)
    def _worker() -> None:
        try:
            _send(full_msg, "HTML")
        except Exception:
            try:
                import re
                plain = re.sub(r"<[^>]+>", "", full_msg)
                _send(plain, "")
            except Exception as _e2:
                print(f"[TG] fallback error: {_e2}")
    threading.Thread(target=_worker, daemon=True).start()


def send_telegram(alert: dict) -> None:
    """Build and send the compact entry alert."""
    sym       = alert.get("symbol", "")
    direction = alert.get("direction", "LONG")
    j15m      = float(alert.get("j15m", 0) or 0)
    j1h       = float(alert.get("j1h",  0) or 0)
    bid_pct   = float(alert.get("bid_pct", 0) or 0)
    ask_pct   = float(alert.get("ask_pct", 0) or 0)
    entry     = float(alert.get("entry_price", 0) or 0)
    sl        = float(alert.get("sl_price", 0) or 0)
    tp1       = float(alert.get("tp1_price", 0) or 0)
    leverage  = int(alert.get("leverage", 5) or 5)
    margin    = float(alert.get("margin", MARGIN_PER_TRADE) or MARGIN_PER_TRADE)
    j5m_v         = float(alert.get("j5m", 50) or 50)
    j1h_prev_v    = float(alert.get("j1h_prev", j1h) or j1h)
    j1h_prev_ok   = bool(alert.get("j1h_prev_valid", False))
    btc_v         = float(alert.get("btc_j1h", 50) or 50)
    btc_ctx       = str(alert.get("btc_regime_context", "") or "")
    sess_v        = str(alert.get("session", "") or "")
    is_long      = direction == "LONG"
    size         = (margin * leverage / entry) if entry else 0
    full_sl_pnl  = ((sl  - entry) * size) if is_long else ((entry - sl)  * size)
    full_tp1_pnl = ((tp1 - entry) * size) if is_long else ((entry - tp1) * size)
    max_loss      = abs(full_sl_pnl)
    tp1_profit_70 = abs(full_tp1_pnl) * 0.70

    cross_arrow = "\u2191" if is_long else "\u2193"
    j1h_dir = (
        ("FALL" if j1h_prev_ok and j1h <= j1h_prev_v else "FLAT")
        if not is_long
        else ("RISE" if j1h_prev_ok and j1h >= j1h_prev_v else "FLAT")
    )

    if bid_pct >= ask_pct:
        depth_pct  = bid_pct
        depth_side = "B"
    else:
        depth_pct  = ask_pct
        depth_side = "S"

    ts = datetime.now(_EDT).strftime("%I:%M %p ET").lstrip("0")

    msg = (
        f"\U0001F7E3  HL \u00B7 {direction} {sym} \u00B7 {leverage}x\n"
        "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
        f"ENTRY  {_fmt_p(entry)}\n"
        f"SL     {_fmt_p(sl)}   \u2212${max_loss:.2f}\n"
        f"TP1    {_fmt_p(tp1)}   +${tp1_profit_70:.2f} (70%)\n"
        "       runner trails after\n"
        "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
        f"J5={j5m_v:.0f} J15={j15m:.0f} J1={j1h:.0f} dir={j1h_dir}\n"
        f"BTC={btc_v:.0f}({btc_ctx})  {bid_pct:.0f}%B/{ask_pct:.0f}%A  {sess_v}\n"
        f"\u23F1 {ts}"
    )
    _tg_post(msg)



def _send_position_digest() -> None:
    """Send a one-shot position digest to Telegram."""
    trades = app_state.open_trades
    if not trades:
        return
    n          = len(trades)
    total_unrl = 0.0
    pos_lines  = []
    for key, trade in trades.items():
        sym     = trade["symbol"]
        d       = trade["direction"]
        lev     = int(trade.get("leverage", 5) or 5)
        entry   = float(trade.get("entry_price", 0) or 0)
        tp1p    = float(trade.get("tp1_price", 0) or 0)
        current = float(app_state.prices.get(sym, 0) or 0)
        rem     = float(trade.get("remaining_size", trade.get("size", 0)) or 0)
        if current and entry and rem:
            upnl = (current - entry) * rem if d == "LONG" else (entry - current) * rem
        else:
            upnl = 0.0
        total_unrl  += upnl
        sl_dist      = float(trade.get("sl_dist") or abs(float(trade.get("sl_price", entry) or entry) - entry))
        marg         = float(trade.get("margin", MARGIN_PER_TRADE) or MARGIN_PER_TRADE)
        dollar_risk  = marg * lev * (sl_dist / entry) if entry else 0
        r_val        = round(upnl / dollar_risk, 2) if dollar_risk else 0.0
        near_flag = ""
        if tp1p and current and entry:
            tp1_dist = abs(tp1p - entry)
            if tp1_dist > 0 and abs(current - tp1p) <= 0.20 * tp1_dist:
                near_flag = " Ã¢ÂÂTP1"
        sl_label = "S" if d == "SHORT" else "L"
        r_dir    = "Ã¢ÂÂ" if r_val >= 0 else "Ã¢ÂÂ"
        pos_lines.append(
            f"{sym}  {sl_label} {lev}x  "
            f"{'+' if upnl >= 0 else '-'}${abs(upnl):.2f}  "
            f"{r_dir}{abs(r_val)}R{near_flag}"
        )
    sign_unrl = "+" if total_unrl >= 0 else "-"
    sign_day  = "+" if daily_pnl >= 0 else "-"
    ts  = datetime.now(_EDT).strftime("%I:%M %p").lstrip("0")
    msg = (
        f"\U0001F7E3  HL \u00B7 {n} OPEN \u00B7 {sign_unrl}${abs(total_unrl):.2f} unrl\n"
        "Ã¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂ\n"
        + "\n".join(pos_lines) + "\n"
        + "Ã¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂ\n"
        + f"day {sign_day}${abs(daily_pnl):.2f} ÃÂ· {ts}"
    )
    _tg_post(msg)


async def _digest_loop() -> None:
    """Send a position digest every 30 min while at least one position is open."""
    await asyncio.sleep(1800)
    while app_state.open_trades:
        _send_position_digest()
        await asyncio.sleep(1800)

# Ã¢ÂÂÃ¢ÂÂ Background loops Ã¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂ

async def _scan_loop():
    await asyncio.sleep(3)
    while True:
        try:
            # Fleet-wide pre-checks
            # before any scan runs.
            # If either is active skip
            # the entire scan cycle —
            # no signals generated,
            # no alerts logged,
            # no Telegram fired.
            if circuit_breaker_active:
                print(
                    "[SCAN SKIP] circuit"
                    " breaker active —"
                    " skipping scan")
                await asyncio.sleep(
                    SCAN_INTERVAL_SECONDS)
                continue
            if trading_halted_today:
                print(
                    "[SCAN SKIP] daily"
                    " halt active —"
                    " skipping scan")
                await asyncio.sleep(
                    SCAN_INTERVAL_SECONDS)
                continue
            _scan_result = await run_full_scan(hl_client, market_health=app_state.market_health, open_trades=app_state.open_trades)
            new_alerts, _pair_states = _scan_result if isinstance(_scan_result, tuple) else (_scan_result, [])
            # -- BTC flash TG alert -- fires once per flash event when block arms --------
            if _scanner_mod._btc_flash_tg_pending[0]:
                _scanner_mod._btc_flash_tg_pending[0] = False
                if TELEGRAM_ENABLED:
                    _flash_tg = (
                        "BTC FLASH CRASH DETECTED\n"
                        f"Session: {get_session_name()}\n"
                        "ALL LONG ENTRIES BLOCKED 5 MINUTES")
                    threading.Thread(
                        target=lambda m=_flash_tg: _tg_post(m),
                        daemon=True).start()
            _check_stale_prices()
            # Session change detection Ã¢ÂÂ reset per-pair session halts when session rolls
            global _prev_session
            _curr_sess = get_session_name()
            if _prev_session and _curr_sess != _prev_session:
                _gone = [k for k in list(_session_sl_counts) if k.endswith(f"_{_prev_session}")]
                for _k in _gone:
                    _session_sl_counts.pop(_k, None)
                    _session_halted.discard(_k)
                print(f"[SESSION RESET] {_prev_session} session ended Ã¢ÂÂ clearing all session halts.")
                _scanner_mod._consec_adverse = {"long": 0, "short": 0}
                print("[SESSION RESET] consecutive kill counters cleared")
                try:
                    _sb_sr = _get_supabase()
                    if _sb_sr:
                        _sb_sr.table("hl_scanner_state").update(
                            {"halt_short": False, "halt_long": False}).eq("id", 1).execute()
                        print("[SESSION RESET] directional halts cleared in Supabase")
                except Exception as _sr_e:
                    print(f"[SESSION RESET] dir-halt clear failed: {_sr_e}")
            _prev_session = _curr_sess
            app_state.last_scan_at = int(time.time())
            app_state.pair_states  = _pair_states if _pair_states else await scan_pair_state(hl_client)
            app_state.market_health = compute_market_health(
                app_state.pair_states, list(app_state.trade_log)
            )
            # -- Sentinel Phase 0: compute regime, log only --
            try:
                _s = _sentinel_mod.update(app_state.pair_states, app_state.prices)
                app_state.market_health["sentinel_regime"] = _s["regime"]
                app_state.market_health["sentinel_text"]   = _sentinel_mod.get_pill_text()
                _sm = _sentinel_mod.get_metrics()
                app_state.market_health["sentinel_metrics"] = _sm
                # R4: push local convergence state into scanner gate vars
                _scanner_mod._conv_gate_short = bool(_sm.get("fleet_convergence_short", False))
                _scanner_mod._conv_gate_long  = bool(_sm.get("fleet_convergence_long",  False))
                if _s.get("changed") and _s.get("telegram_text") and TELEGRAM_ENABLED:
                    threading.Thread(
                        target=lambda m=_s["telegram_text"]: _tg_post(m),
                        daemon=True).start()
            except Exception as _se:
                print(f"[SENTINEL] update error: {_se}")

            # Capture per-pair scan snapshots for the live overlay
            for _ps in app_state.pair_states:
                _sym = _ps.get("symbol")
                if _sym:
                    _snap = {
                        "n":           get_scan_count(),
                        "ts":          int(time.time()),
                        "j15m":        _ps.get("j15m"),
                        "bid_pct":     _ps.get("bid_pct"),
                        "ask_pct":     _ps.get("ask_pct"),
                        "rsi15m":      _ps.get("rsi15m"),
                        "adx1h":       _ps.get("adx1h"),
                        "score_long":  _ps.get("long_score"),
                        "score_short": _ps.get("short_score"),
                    }
                    _hist = app_state.scan_snapshots.get(_sym, [])
                    app_state.scan_snapshots[_sym] = ([_snap] + _hist)[:3]

            for alert in new_alerts:
                sym, dir_ = alert["symbol"], alert["direction"]

                # Update alerts panel
                existing = next(
                    (a for a in app_state.alerts
                     if a["symbol"] == sym and a["direction"] == dir_), None
                )
                if existing:
                    app_state.alerts.remove(existing)
                app_state.alerts.insert(0, alert)

                # Telegram alert + reset position digest timer
                if TELEGRAM_ENABLED:
                    threading.Thread(target=lambda a=alert: send_telegram(a), daemon=True).start()
                    global _digest_task
                    if _digest_task is not None and not _digest_task.done():
                        _digest_task.cancel()
                    _digest_task = asyncio.create_task(_digest_loop())

                # Auto-entry gate: blocked when live and LIVE_MANUAL_ENTRY_ONLY is True
                if not PAPER_MODE and LIVE_MANUAL_ENTRY_ONLY:
                    print(
                        f"[SIGNAL] {sym} {dir_} tier={alert.get('tier')} "
                        f"lev={alert.get('leverage')}x entry={alert.get('entry_price')} "
                        f"sl={alert.get('sl_price')} tp1={alert.get('tp1_price')} "
                        f"Ã¢ÂÂ live manual entry required via overlay. "
                        f"Do not open position automatically."
                    )
                else:
                    if not PAPER_MODE:
                        print(
                            "[WARNING] LIVE AUTO-ENTRY ACTIVE"
                            " — LIVE_MANUAL_ENTRY_ONLY is disabled."
                        )
                    # J1H DIRECTION GATE — confirm J1H moving in right direction
                    # LONG:  j1h must be rising  (j1h_now > j1h_prev)
                    # SHORT: j1h must be falling (j1h_now < j1h_prev)
                    # DIRECT-OPEN ARCHITECTURE (replaces _pending_alerts queue):
                    # cooldown check -> already-open check -> price-drift guard
                    # -> _do_open_trade(), all in the same scan cycle as the signal.
                    _ep = alert.get("entry_price", 0) or 0

                    # Stamp cooldown only when a trade will actually open
                    set_close_cooldown(sym, dir_)
                    _save_state()

                    # Price-drift guard (formerly EXPIRED_PRICE in the pending
                    # queue) — protects against opening into a price that has
                    # already moved > 1.5% adverse from signal_price during
                    # this scan cycle's processing time.
                    _cur = app_state.prices.get(sym, 0) or 0
                    _p_drift = abs(_cur - _ep) / _ep * 100 if _ep else 0
                    if _cur <= 0 or not _ep or _p_drift > 1.5:
                        print(
                            f"[EXPIRED_PRICE] {sym} {dir_} "
                            f"drift={_p_drift:.2f}% "
                            f"signal={_ep:.5f} cur={_cur:.5f}")
                        asyncio.create_task(
                            _log_alert_outcome(
                                alert,
                                "EXPIRED_PRICE",
                                "HL",
                            ))
                        continue

                    print(
                        f"[DIRECT OPEN] {sym} {dir_} price={_cur:.5f}"
                        f" signal={_ep:.5f} — opening trade")
                    alert["be_confirm_price"] = _ep
                    _margin = alert.get("margin", MARGIN_PER_TRADE)
                    trade, err = await _do_open_trade(
                        sym, dir_,
                        _margin, alert["leverage"],
                        alert_data=alert,
                        exchange="HL",
                    )
                    if trade:
                        print(
                            f"[OPENED] {sym} {dir_}"
                            f" entry={trade.get('entry_price')}")
                        # No separate "OPENED" alert_log row here — _do_open_trade()
                        # already logs "TRADE_OPENED" internally on success. Logging
                        # again here produced a duplicate row per trade open.
                    elif err:
                        print(f"[OPEN FAILED] {sym} {dir_}: {err}")
                        asyncio.create_task(
                            _log_alert_outcome(
                                alert,
                                "OPEN_FAILED",
                                "HL",
                                confirm_price=_cur,
                            ))
        except Exception as e:
            print(f"[SCAN LOOP] error: {e}")
        # _process_pending_alerts() no longer called — direct-open
        # architecture opens trades inline above. Function body kept
        # in place, uncalled, pending a future cleanup pass.
        await asyncio.sleep(SCAN_INTERVAL_SECONDS)


async def _price_loop():
    _chg_tick = 0
    while True:
        try:
            all_prices = await hl_client.get_all_prices()
            if not all_prices:
                print("[PRICE] get_all_prices returned empty -- skipping price update")
            else:
                for sym in PAIRS:
                    if sym in all_prices:
                        app_state.prices[sym] = all_prices[sym]
                        app_state.price_updated_at[sym] = time.time()

            # Fetch 24h changes every 5 price ticks (~40s) to avoid extra rate pressure
            _chg_tick += 1
            if _chg_tick >= 5:
                _chg_tick = 0
                changes = await hl_client.get_all_price_changes(PAIRS)
                if changes:
                    app_state.price_changes.update(changes)

            # Auto-reset daily PnL at ET midnight
            global daily_pnl, trading_halted_today, _last_midnight_day
            today = datetime.now(ET).day
            if today != _last_midnight_day:
                daily_pnl            = 0.0
                trading_halted_today = False
                _last_midnight_day   = today
                print("[DAILY RESET] midnight UTC Ã¢ÂÂ daily_pnl reset")

        except Exception as e:
            print(f"[PRICE LOOP] error: {e}")
        await asyncio.sleep(PRICE_INTERVAL_SECONDS)


def _check_stale_prices() -> None:
    """Send a one-shot Telegram alert when a pair with an open trade loses price data."""
    global _stale_tg_sent
    stale: set[str] = set(_scanner_mod._stale_prices)

    for sym in stale:
        app_state.price_stale[sym] = True
        has_trade = any(t.get("symbol") == sym for t in app_state.open_trades.values())
        if has_trade and sym not in _stale_tg_sent:
            _stale_tg_sent.add(sym)
            msg = (
                f"Ã¢ÂÂ Ã¯Â¸Â PRICE STALE Ã¢ÂÂ {sym} Ã¢ÂÂ "
                f"no price for 2 consecutive scans. "
                f"Open trade at risk. Check manually."
            )
            print(f"[PRICE STALE] {sym} Ã¢ÂÂ Telegram alert sent")
            if TELEGRAM_ENABLED:
                threading.Thread(target=lambda m=msg: _tg_post(m), daemon=True).start()

    for sym in list(_stale_tg_sent):
        if sym not in stale:
            _stale_tg_sent.discard(sym)
            app_state.price_stale.pop(sym, None)

# Ã¢ÂÂÃ¢ÂÂ Exit monitor helpers Ã¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂ

def _compute_r(pnl: float, trade: dict) -> float:
    entry       = trade.get("entry_price") or 0
    sl_dist     = trade.get("sl_dist") or 0
    lev         = trade.get("leverage", 1)
    margin      = trade.get("margin", MARGIN_PER_TRADE)
    dollar_risk = margin * lev * (sl_dist / entry) if entry else 0
    return round(pnl / dollar_risk, 2) if dollar_risk else 0.0


def _do_hc_partial_close(key: str, trade: dict, exit_price: float):
    """HC Score-10: close 1/3 at 1.5R, move SL to entry (breakeven)."""
    if not exit_price or exit_price <= 0:
        print(f"[EXIT GUARD] {trade.get('symbol')} {trade.get('direction')} Ã¢ÂÂ refused HC partial close: exit_price={exit_price!r} is null/zero Ã¢ÂÂ skipping")
        return
    sym, direction = trade["symbol"], trade["direction"]
    full_size = trade.get("remaining_size", trade["size"])
    close_sz  = full_size / 3
    entry     = trade["entry_price"]
    pnl       = (exit_price - entry) * close_sz if direction == "LONG" \
                else (entry - exit_price) * close_sz
    r         = _compute_r(pnl, trade)
    _append_trade_log(trade, exit_price, "HC_PARTIAL_1.5R", pnl, r)
    _update_daily_pnl(pnl)
    trade["remaining_size"] = full_size - close_sz
    trade["partial_hit"]    = True
    trade["sl_price"]       = entry  # move SL to breakeven
    old_margin              = trade.get("margin", MARGIN_PER_TRADE)
    trade["margin"]         = old_margin * 2 / 3
    app_state.open_trades[key]    = trade
    app_state.margin_deployed     = max(0.0, app_state.margin_deployed - old_margin / 3)
    print(f"[HC PARTIAL] {sym} {direction} 1/3 closed at {exit_price:.6f} "
          f"pnl=${pnl:.2f} r={r:+.2f}R Ã¢ÂÂ SL moved to breakeven {entry:.6f}")
    _save_state()



def _pair_family(pair: str) -> str:
    p = (pair or "").upper()
    if "BTC" in p:
        return "BTC"
    if "ZEC" in p:
        return "ZEC"
    return "OTHER"


def _get_btc_regime() -> str:
    """Compute current BTC regime from live _btc_j1h (same logic as state endpoint)."""
    _j = _scanner_mod._btc_j1h
    if _j > 80.0:               return "LONG_BLOCKED"
    if _j < 20.0:               return "SHORT_BLOCKED"
    if 40.0 <= _j <= 60.0:      return "NEUTRAL_BLOCK"
    return "CLEAR"


async def _write_peak_shadow_row(key: str, trade: dict, reason: str,
                                  final_pnl: float) -> None:
    try:
        sh = _peak_shadow.pop(key, None)
        if sh is None:
            return
        sb = _get_supabase()
        if sb is None:
            return
        opened_at = trade.get("opened_at")
        open_iso  = (datetime.fromtimestamp(opened_at, tz=timezone.utc).isoformat()
                     if opened_at else None)
        session   = trade.get("session") or (_get_session(opened_at) if opened_at else None)
        _psh_row = {
            "venue":                  "hl",
            "pair":                   trade.get("symbol", ""),
            "direction":              trade.get("direction", ""),
            "open_time":              open_iso,
            "exit_reason":            reason,
            "peak_pnl_usd":           round(sh["peak_pnl_usd"], 2),
            "peak_reached_at":        sh.get("peak_reached_at"),
            "pair_family":            _pair_family(trade.get("symbol", "")),
            "decay20_triggered_at":   sh.get("d20_at"),
            "decay20_pnl_at_trigger": sh.get("d20_pnl"),
            "decay20_phase":          sh.get("d20_phase"),
            "decay30_triggered_at":   sh.get("d30_at"),
            "decay30_pnl_at_trigger": sh.get("d30_pnl"),
            "decay30_phase":          sh.get("d30_phase"),
            "decay40_triggered_at":   sh.get("d40_at"),
            "decay40_pnl_at_trigger": sh.get("d40_pnl"),
            "decay40_phase":          sh.get("d40_phase"),
            "pnl_dollars":            round(final_pnl, 2),
            "session_opened":         session,
        }
        if reason == "RUNNER_DECAY_10":
            _psh_row["runner_peak_pnl"]            = round(sh.get("runner_peak_pnl", 0.0), 2)
            _psh_row["runner_decay_triggered_at"]  = datetime.now(timezone.utc).isoformat()
            _psh_row["runner_decay_pnl_at_trigger"] = round(final_pnl, 2)
        sb.table("peak_protection_shadow").insert(_psh_row).execute()
        print(f"[SHADOW] wrote peak_protection_shadow {trade.get('symbol')} "
              f"{trade.get('direction')} peak=${sh['peak_pnl_usd']:.2f} reason={reason}")
    except Exception as _psh_e:
        print(f"[SHADOW] write error: {_psh_e}")


async def _write_adverse_shadow_row(key: str, trade: dict, reason: str,
                                     final_pnl: float, final_r: float) -> None:
    try:
        sh = _adverse_shadow.pop(key, None)
        if sh is None:
            return
        sb = _get_supabase()
        if sb is None:
            return
        opened_at = trade.get("opened_at")
        open_iso  = (datetime.fromtimestamp(opened_at, tz=timezone.utc).isoformat()
                     if opened_at else None)
        _session_w = trade.get("session") or (_get_session(opened_at) if opened_at else None)
        _ent_w     = trade.get("entry_price") or 0
        _sl_d_w    = trade.get("sl_dist") or 0
        _ap_w      = trade.get("adverse_price")
        _is_sh_w   = trade.get("direction") == "SHORT"
        _mae_r_w   = None
        if _ap_w is not None and _sl_d_w and _ent_w:
            _adv_w = (_ent_w - _ap_w) if not _is_sh_w else (_ap_w - _ent_w)
            _mae_r_w = round(_adv_w / _sl_d_w, 2)
        sb.table("adverse_cut_shadow").insert({
            "venue":                   "hl",
            "pair":                    trade.get("symbol", ""),
            "direction":               trade.get("direction", ""),
            "open_time":               open_iso,
            "exit_reason":             reason,
            "final_pnl_dollars":       round(final_pnl, 2),
            "final_r_value":           round(final_r,   2),
            "mae_r":                   _mae_r_w,
            "pair_family":             _pair_family(trade.get("symbol", "")),
            "session_opened":          _session_w,
            "ever_recovered":          sh.get("ever_recovered", False),
            "rulea_triggered_at":      sh.get("ruleA_at"),
            "rulea_elapsed_min":       sh.get("ruleA_min"),
            "rulea_sl_pct_at_trigger": sh.get("ruleA_pct"),
            "rulea_pnl_at_trigger":    sh.get("ruleA_pnl"),
            "ruleb_triggered_at":      sh.get("ruleB_at"),
            "ruleb_elapsed_min":       sh.get("ruleB_min"),
            "ruleb_sl_pct_at_trigger": sh.get("ruleB_pct"),
            "ruleb_pnl_at_trigger":    sh.get("ruleB_pnl"),
            "rulec_triggered_at":      sh.get("ruleC_at"),
            "rulec_elapsed_min":       sh.get("ruleC_min"),
            "rulec_sl_pct_at_trigger": sh.get("ruleC_pct"),
            "rulec_pnl_at_trigger":    sh.get("ruleC_pnl"),
            "ruled_triggered_at":      sh.get("ruleD_at"),
            "ruled_elapsed_min":       sh.get("ruleD_min"),
            "ruled_sl_pct_at_trigger": sh.get("ruleD_pct"),
            "ruled_pnl_at_trigger":    sh.get("ruleD_pnl"),
        }).execute()
        print("[ADVERSE SHADOW] wrote adverse_cut_shadow " + str(trade.get("symbol")) +
              " " + str(trade.get("direction")) + " reason=" + reason +
              " pnl=$" + str(round(final_pnl, 2)))
    except Exception as _ash_e:
        print("[ADVERSE SHADOW] write error: " + str(_ash_e))


async def _write_signal_shadow_row(key: str, trade: dict, reason: str,
                                    final_pnl: float, final_r: float) -> None:
    """Write one row to signal_invalidation_shadow at trade close."""
    try:
        sh = _signal_shadow.pop(key, {})
        sb = _get_supabase()
        if sb is None:
            return
        opened_at  = trade.get("opened_at")
        open_iso   = (datetime.fromtimestamp(opened_at, tz=timezone.utc).isoformat()
                      if opened_at else None)
        _sess_s    = trade.get("session") or (_get_session(opened_at) if opened_at else None)
        _sym_s     = trade.get("symbol", "")
        _corr_s    = trade.get("btc_correlation")
        if _corr_s is None:
            _corr_s = _scanner_mod.BTC_CORRELATION.get(_sym_s, 0.75)
        sb.table("signal_invalidation_shadow").insert({
            "venue":                          "hl",
            "pair":                           _sym_s,
            "direction":                      trade.get("direction", ""),
            "pair_family":                    _pair_family(_sym_s),
            "session_opened":                 _sess_s,
            "open_time":                      open_iso,
            "exit_reason":                    reason,
            "final_pnl_dollars":              round(final_pnl, 2),
            "final_r_value":                  round(final_r,   2),
            "btc_correlation":                _corr_s,
            "stochflip_triggered_at":         sh.get("stochflip_at"),
            "stochflip_elapsed_min":          sh.get("stochflip_min"),
            "stochflip_pnl_at_trigger":       sh.get("stochflip_pnl"),
            "stochflip_sl_pct_at_trigger":    sh.get("stochflip_sl_pct"),
            "jgiveback_triggered_at":         sh.get("jgiveback_at"),
            "jgiveback_elapsed_min":          sh.get("jgiveback_min"),
            "jgiveback_pnl_at_trigger":       sh.get("jgiveback_pnl"),
            "jgiveback_sl_pct_at_trigger":    sh.get("jgiveback_sl_pct"),
            "btcregime_triggered_at":         sh.get("btcregime_at"),
            "btcregime_elapsed_min":          sh.get("btcregime_min"),
            "btcregime_pnl_at_trigger":       sh.get("btcregime_pnl"),
            "btcregime_sl_pct_at_trigger":    sh.get("btcregime_sl_pct"),
            "btcregime_old_value":            sh.get("btcregime_old"),
            "btcregime_new_value":            sh.get("btcregime_new"),
        }).execute()
        print("[SIG SHADOW] wrote signal_invalidation_shadow " + _sym_s
              + " " + str(trade.get("direction")) + " reason=" + reason
              + " pnl=$" + str(round(final_pnl, 2)))
    except Exception as _siw_e:
        print("[SIG SHADOW] write error: " + str(_siw_e))


async def _write_sign_shadow_rows(key: str, trade: dict, reason: str,
                                   final_pnl: float) -> None:
    try:
        ss = _sign_shadow.pop(key, None)
        if not ss or not ss.get("transitions"):
            return
        sb = _get_supabase()
        if sb is None:
            return
        opened_at = trade.get("opened_at")
        open_iso  = (datetime.fromtimestamp(opened_at, tz=timezone.utc).isoformat()
                     if opened_at else None)
        close_iso = datetime.now(timezone.utc).isoformat()
        rows = []
        for _i, _ev in enumerate(ss["transitions"], start=1):
            rows.append({
                "venue":                    "hl",
                "pair":                     trade.get("symbol", ""),
                "direction":                trade.get("direction", ""),
                "open_time":                open_iso,
                "trade_close_time":         close_iso,
                "transition_timestamp":     _ev["ts"],
                "sign":                     _ev["sign"],
                "pnl_usd_at_transition":    _ev["pnl"],
                "transition_sequence_number": _i,
                "total_transitions":        len(ss["transitions"]),
            })
        sb.table("pnl_sign_transitions").insert(rows).execute()
        print("[SIGN SHADOW] wrote " + str(len(rows)) + " sign-transition rows for " +
              str(trade.get("symbol")) + " " + str(trade.get("direction")) +
              " reason=" + reason + " final_pnl=$" + str(round(final_pnl, 2)))
    except Exception as _ss_e:
        print("[SIGN SHADOW] write error: " + str(_ss_e))




def _do_close_trade(key: str, trade: dict, exit_price: float, reason: str):
    """Synchronous internal close Ã¢ÂÂ no exchange call, price already known."""
    if not exit_price or exit_price <= 0:
        print(f"[EXIT GUARD] {trade.get('symbol')} {trade.get('direction')} Ã¢ÂÂ refused close (reason={reason}): exit_price={exit_price!r} is null/zero Ã¢ÂÂ skipping")
        return
    sym       = trade["symbol"]
    direction = trade["direction"]
    remaining = trade.get("remaining_size", trade["size"])
    entry     = trade["entry_price"]

    pnl = (exit_price - entry) * remaining if direction == "LONG" \
          else (entry - exit_price) * remaining
    r   = _compute_r(pnl, trade)

    if (reason in ("ADVERSE_CUT", "SL", "KILL")
            or (reason == "PEAK_DECAY_10" and pnl <= 0)):
        _now_ac  = datetime.now(timezone.utc)
        _dir_key = "long" if direction == "LONG" else "short"
        _scanner_mod._adverse_cluster[_dir_key].append(_now_ac)
        _scanner_mod._adverse_cluster[_dir_key] = [
            t for t in _scanner_mod._adverse_cluster[_dir_key]
            if (_now_ac - t).total_seconds() < 600
        ]
        if len(_scanner_mod._adverse_cluster[_dir_key]) >= 3:
            print(f"[CLUSTER_HALT] {_dir_key.upper()} entries halted"
                  f" Ã¢ÂÂ {len(_scanner_mod._adverse_cluster[_dir_key])} adverse exits"
                  f" in 10min window")
        _now_cd  = datetime.now(timezone.utc)
        _recent_5min = [t for t in _scanner_mod._adverse_cluster[_dir_key]
                        if (_now_cd - t).total_seconds() < 300]
        if len(_recent_5min) >= 2:
            _scanner_mod._adverse_cooldown_until[_dir_key] = _now_cd + timedelta(minutes=15)
            print(f"[COOLDOWN_15] {_dir_key.upper()} cooled 15min")
        else:
            _cur_cd = _scanner_mod._adverse_cooldown_until.get(_dir_key)
            if _cur_cd is None or _now_cd >= _cur_cd:
                _scanner_mod._adverse_cooldown_until[_dir_key] = _now_cd + timedelta(minutes=3)
                print(f"[COOLDOWN_3] {_dir_key.upper()} cooled 3min")

    # -- Consecutive kill halt counter --------------------------------
    _dir_key_c = "long" if direction == "LONG" else "short"
    if reason in ("KILL", "DEAD_TRADE_KILL"):
        _scanner_mod._consec_adverse[_dir_key_c] = (
            _scanner_mod._consec_adverse.get(_dir_key_c, 0) + 1)
        if _scanner_mod._consec_adverse[_dir_key_c] == 3:
            _halt_msg = (
                f"⛔ HL {direction} DIRECTION HALTED\n"
                f"3 consecutive kills — {direction} entries suspended\n"
                f"Resumes when BTC regime = CLEAR\n"
                f"Session: {get_session_name()}")
            print(f"[CONSEC_HALT] HL {direction} halted — 3 consecutive kills")
            if TELEGRAM_ENABLED:
                threading.Thread(
                    target=lambda m=_halt_msg: _tg_post(m),
                    daemon=True).start()
    elif pnl > 0:
        _scanner_mod._consec_adverse[_dir_key_c] = 0

    _append_trade_log(trade, exit_price, reason, pnl, r)
    _update_daily_pnl(pnl)
    _on_trade_close(reason)

    app_state.margin_deployed = max(0.0, app_state.margin_deployed - trade["margin"])
    if key in app_state.open_trades:
        del app_state.open_trades[key]
    _retire_alert(sym, direction)

    print(f"[EXIT] {sym} {direction} closed at {exit_price} reason={reason} "
          f"pnl=${pnl:.2f} r={r:+.2f}R")
    if TELEGRAM_ENABLED:
        _pd_peak_tg = _peak_shadow.get(key, {}).get("peak_pnl_usd", 0.0)
        if reason in ("PEAK_DECAY_10", "RUNNER_DECAY_10"):
            if reason == "RUNNER_DECAY_10":
                _sweep_peak = _peak_shadow.get(key, {}).get("runner_peak_pnl", 0.0)
            else:
                _sweep_peak = _pd_peak_tg
            _pct = round((1 - (pnl / _sweep_peak)) * 100, 1) if _sweep_peak else 0
            _sentinel_sweep.append((reason, sym, direction, pnl, _sweep_peak, round(pnl, 2), _pct))
        else:
            def _exit_tg(r=reason, s=sym, d=direction, ep=exit_price, p=pnl, dp=daily_pnl, pk=_pd_peak_tg):
                sl_lbl = "S" if d == "SHORT" else "L"
                if r == "SL":
                    _tg_post("\u274C " + s + " " + sl_lbl + " \u00B7 SL at " + _fmt_p(ep)
                             + "\n\u2212$" + f"{abs(p):.2f}" + " \u00B7 day " + ("+" if dp >= 0 else "-") + "$" + f"{abs(dp):.2f}")
                else:
                    _tg_post("\U0001F535 " + s + " " + sl_lbl + " \u00B7 closed (" + r + ") at " + _fmt_p(ep)
                             + "\n" + ("+" if p >= 0 else "-") + "$" + f"{abs(p):.2f}")
            threading.Thread(target=_exit_tg, daemon=True).start()
    asyncio.create_task(_write_peak_shadow_row(key, trade, reason, pnl))
    asyncio.create_task(_write_adverse_shadow_row(key, trade, reason, pnl, r))
    asyncio.create_task(_write_sign_shadow_rows(key, trade, reason, pnl))
    asyncio.create_task(_write_signal_shadow_row(key, trade, reason, pnl, r))
    _lk = trade.get("_lock_key")
    if _lk:
        _sb2 = _get_supabase()
        if _sb2:
            try:
                _sb2.table("trade_open_locks").delete().eq("lock_key", _lk).execute()
            except Exception as _unlock_e:
                print(f"[LOCK CLEANUP FAILED] {_lk}: {_unlock_e}")
    _se_j1h_extreme.pop(key, None)
    set_pair_cooldown(sym)
    _candle_close_history.pop(key, None)
    _candle_high_history.pop(key, None)
    _save_state()


def _do_partial_close_tp1(key: str, trade: dict, exit_price: float):
    """Close 70% of position at TP1, keep 30% runner open for Trailblazer ATR trailing stop."""
    if not exit_price or exit_price <= 0:
        print(f"[EXIT GUARD] {trade.get('symbol')} {trade.get('direction')} Ã¢ÂÂ refused TP1 close: exit_price={exit_price!r} is null/zero Ã¢ÂÂ skipping")
        return
    sym        = trade["symbol"]
    direction  = trade["direction"]
    full_size  = trade.get("remaining_size", trade["size"])
    # R6: ADX>=40 high-confidence setups use 50% TP1 close (vs 70%) to extend runner toward TP2 (1.5R)
    # Data: ADX>=40 signals have trending character -- wider runner captures more
    _tp1_pct   = 0.50 if trade.get("adx1h", 0) >= 40 else TP1_CLOSE_PCT
    close_size = full_size * _tp1_pct
    rem_size   = full_size - close_size
    entry      = trade["entry_price"]

    pnl = (exit_price - entry) * close_size if direction == "LONG" \
          else (entry - exit_price) * close_size
    r   = _compute_r(pnl, trade)

    # Log the TP1 partial close BEFORE modifying trade dict (so size/metadata is correct)
    _append_trade_log(trade, exit_price, "TP1", pnl, r)
    _update_daily_pnl(pnl)

    # Update trade in-place Ã¢ÂÂ keep 30% runner open for Trailblazer
    trade["remaining_size"]   = rem_size
    trade["tp1_hit"]          = True
    _cpnl_tp1 = (
        (exit_price - entry)
        * rem_size
        if direction == "LONG"
        else
        (entry - exit_price)
        * rem_size)
    _peak_shadow.setdefault(key, {}).update({
        "runner_peak_pnl": 0.0,
        "runner_armed":    True,
        "peak_pnl_usd":
            max(0.0, _cpnl_tp1),
    })
    trade["extreme_price"]    = exit_price
    trade["trail_best_price"] = exit_price
    trade["trail_anchor"]     = exit_price
    trade["tp1_pnl"]          = pnl
    # Reduce deployed margin proportionally (TP1_CLOSE_PCT closed)
    old_margin = trade.get("margin", MARGIN_PER_TRADE)
    trade["margin"] = old_margin * (1.0 - _tp1_pct)
    app_state.open_trades[key]     = trade
    app_state.margin_deployed      = max(0.0, app_state.margin_deployed - old_margin * _tp1_pct)

    print(f"[EXIT] {sym} {direction} TP1 partial close ({int(_tp1_pct*100)}%) at {exit_price} "
          f"pnl=${pnl:.2f} r={r:+.2f}R Ã¢ÂÂ 30% runner open watching Trailblazer ATR trail")
    if TELEGRAM_ENABLED:
        def _tp1_tg(s=sym, d=direction, ep=exit_price, p=pnl):
            sl_lbl = "S" if d == "SHORT" else "L"
            _tg_post("\u2705 " + s + " " + sl_lbl + " \u00B7 TP1 \u2014 70% out at " + _fmt_p(ep)
                     + "\n+$" + f"{p:.2f}" + " banked \u00B7 runner trails")
        threading.Thread(target=_tp1_tg, daemon=True).start()
    _save_state()


def _do_trailblazer_close(key: str, trade: dict, exit_price: float,
                           trail_best: float, trail_stop: float):
    """Close remaining 30% runner at Trailblazer ATR trailing stop trigger."""
    if not exit_price or exit_price <= 0:
        print(f"[EXIT GUARD] {trade.get('symbol')} {trade.get('direction')} Ã¢ÂÂ refused TRAILBLAZER close: exit_price={exit_price!r} is null/zero Ã¢ÂÂ skipping")
        return
    sym       = trade["symbol"]
    direction = trade["direction"]
    remaining = trade.get("remaining_size", trade["size"])
    entry     = trade["entry_price"]

    pnl       = (exit_price - entry) * remaining if direction == "LONG" \
                else (entry - exit_price) * remaining
    r         = _compute_r(pnl, trade)
    tp1_pnl   = trade.get("tp1_pnl") or 0
    total_pnl = round(tp1_pnl + pnl, 2)

    _append_trade_log(trade, exit_price, "TRAILBLAZER", pnl, r)
    _update_daily_pnl(pnl)
    _on_trade_close("TRAILBLAZER")

    app_state.margin_deployed = max(0.0, app_state.margin_deployed - trade["margin"])
    if key in app_state.open_trades:
        del app_state.open_trades[key]
    _retire_alert(sym, direction)

    print(f"[TRAILBLAZER] {sym} {direction} Ã¢ÂÂ runner closed at {exit_price}, "
          f"best price was {trail_best}, trail stop triggered at {trail_stop}")
    if TELEGRAM_ENABLED:
        def _trail_tg(s=sym, d=direction, ep=exit_price, p=pnl, tp=total_pnl):
            sl_lbl = "S" if d == "SHORT" else "L"
            _tg_post("\U0001F3C3 " + s + " " + sl_lbl + " \u00B7 runner out at " + _fmt_p(ep)
                     + "\n+$" + f"{p:.2f}" + " \u00B7 trade total " + ("+" if tp >= 0 else "-") + "$" + f"{abs(tp):.2f}")
        threading.Thread(target=_trail_tg, daemon=True).start()
    asyncio.create_task(_write_peak_shadow_row(key, trade, "TRAILBLAZER", pnl))
    asyncio.create_task(_write_adverse_shadow_row(key, trade, "TRAILBLAZER", pnl, r))
    asyncio.create_task(_write_sign_shadow_rows(key, trade, "TRAILBLAZER", pnl))
    asyncio.create_task(_write_signal_shadow_row(key, trade, "TRAILBLAZER", pnl, r))
    _lk = trade.get("_lock_key")
    if _lk:
        _sb2 = _get_supabase()
        if _sb2:
            try:
                _sb2.table("trade_open_locks").delete().eq("lock_key", _lk).execute()
            except Exception as _unlock_e:
                print(f"[LOCK CLEANUP FAILED] {_lk}: {_unlock_e}")
    _save_state()


# Ã¢ÂÂÃ¢ÂÂ Exit monitor loop Ã¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂ

def _flush_sentinel_sweep() -> None:
    """Send one consolidated Telegram message per scan cycle for protective exits (PEAK_DECAY_10 / RUNNER_DECAY_10)."""
    global _sentinel_sweep
    exits = list(_sentinel_sweep)
    _sentinel_sweep.clear()
    if not exits:
        return
    has_sentinel = any(e[0] == "PEAK_DECAY_10"  for e in exits)
    has_runner   = any(e[0] == "RUNNER_DECAY_10" for e in exits)
    if len(exits) == 1:
        reason, sym, direction, pnl, peak, locked, pct = exits[0]
        sl_lbl = "S" if direction == "SHORT" else "L"
        if reason == "PEAK_DECAY_10":
            _tg_post("\U0001F6E1\uFE0F SENTINEL \u2014 " + sym + " " + sl_lbl
                     + " \u00B7 peak-decay exit"
                     + "\nPeaked +$" + f"{peak:.2f}" + " \u2192 locked +$" + f"{locked:.2f}"
                     + " (" + f"{pct}" + "% given back)"
                     + "\nProtected capital before further decay")
        else:
            _tg_post("\U0001F3C3 RUNNER PROTECTED \u2014 " + sym + " " + sl_lbl
                     + " \u00B7 10% decay on runner"
                     + "\nRunner peaked +$" + f"{peak:.2f}" + " \u2192 locking +$" + f"{locked:.2f}"
                     + " (" + f"{pct}" + "% given back)"
                     + "\nBanking runner before reversal deepens")
    else:
        net   = sum(e[3] for e in exits)
        parts = []
        for reason, sym, direction, pnl, peak, locked, pct in exits:
            sl_lbl = "S" if direction == "SHORT" else "L"
            sign   = "+" if locked >= 0 else "-"
            tag    = "\U0001F3C3" if reason == "RUNNER_DECAY_10" else "\U0001F6E1"
            parts.append(tag + " " + sym + " " + sl_lbl + " " + sign + "$" + f"{abs(locked):.2f}")
        if has_sentinel and has_runner:
            header = "\u26A1 PROTECTION SWEEP"
        elif has_runner:
            header = "\U0001F3C3 RUNNER SWEEP"
        else:
            header = "\U0001F6E1\uFE0F SENTINEL SWEEP"
        _tg_post(header + " \u00B7 " + str(len(exits)) + " exits \u00B7 HL"
                 + "\n" + " \u00B7 ".join(parts)
                 + "\n" + "\u2500" * 32
                 + "\nNet locked: " + ("+" if net >= 0 else "-") + "$" + f"{abs(net):.2f}")


async def _exit_monitor_loop():
    """Runs every PRICE_INTERVAL_SECONDS. Checks every open trade against SL/TP."""
    _extclose_tick = 0
    while True:
        _extclose_tick += 1
        for key, trade in list(app_state.open_trades.items()):
            try:
                sym       = trade["symbol"]
                direction = trade["direction"]
                sl_price  = trade.get("sl_price")
                tp1_price = trade.get("tp1_price")
                tp2_price = trade.get("tp2_price")
                current   = app_state.prices.get(sym)
                tp1_hit   = trade.get("tp1_hit", False)
                is_short  = direction == "SHORT"

                _px_age = (
                    time.time() -
                    app_state.price_updated_at.get(sym, 0)
                )
                if not current or current <= 0:
                    print(f"[EXIT CHECK] {sym} {direction} skipped - "
                          f"no price ({current})")
                    continue
                if _px_age > 90:
                    print(f"[STALE PRICE] {sym} {direction} price age="
                          f"{_px_age:.0f}s — attempting direct refetch")
                    _fresh_px = None
                    try:
                        _all = await hl_client.get_all_prices()
                        _fresh_px = _all.get(sym)
                    except Exception as _refetch_e:
                        print(f"[STALE PRICE] {sym} refetch failed: {_refetch_e!r}")
                    if _fresh_px and _fresh_px > 0:
                        app_state.prices[sym] = float(_fresh_px)
                        app_state.price_updated_at[sym] = time.time()
                        current = float(_fresh_px)
                        print(f"[STALE PRICE] {sym} refetch succeeded: {current}")
                    else:
                        print(f"[STALE PRICE] {sym} refetch FAILED — price still stale, "
                              f"exit checks proceeding with last-known price as fallback "
                              f"rather than skipping entirely")
                # -- External close detection (every 5 ticks) ─────────────────
                if not PAPER_MODE and _extclose_tick % 5 == 0:
                    try:
                        _exsz = await hl_client.get_open_position_size(sym)
                        if _exsz is not None and _exsz == 0:
                            print(f"[EXTERNAL_CLOSE] {sym} {direction} "
                                  f"— position gone on exchange, logging close")
                            _entry_px = trade.get("entry_price", 0)
                            _rem_sz   = trade.get("remaining_size", trade.get("size", 0))
                            _pnl      = (
                                (current - _entry_px) * _rem_sz if direction == "LONG"
                                else (_entry_px - current) * _rem_sz
                            )
                            _sl_d  = trade.get("sl_dist") or 0
                            _lev   = trade.get("leverage", 1)
                            _mar   = trade.get("margin", MARGIN_PER_TRADE)
                            _dr    = _mar * _lev * (_sl_d / _entry_px) if _entry_px else 0
                            _r     = round(_pnl / _dr, 2) if _dr else 0.0
                            _append_trade_log(trade, current, "EXTERNAL_CLOSE", _pnl, _r)
                            _update_daily_pnl(_pnl)
                            _on_trade_close("EXTERNAL_CLOSE")
                            asyncio.create_task(
                                _write_adverse_shadow_row(key, trade, "EXTERNAL_CLOSE", _pnl, _r))
                            asyncio.create_task(
                                _write_sign_shadow_rows(key, trade, "EXTERNAL_CLOSE", _pnl))
                            asyncio.create_task(
                                _write_signal_shadow_row(key, trade, "EXTERNAL_CLOSE", _pnl, _r))
                            app_state.margin_deployed = max(
                                0.0, app_state.margin_deployed - trade["margin"])
                            del app_state.open_trades[key]
                            _retire_alert(sym, direction)
                            _save_state()
                            if TELEGRAM_ENABLED:
                                def _ext_tg(s=sym, d=direction, ep=current, p=_pnl):
                                    _sl_lbl = "S" if d == "SHORT" else "L"
                                    _tg_post(
                                        "🟠 " + s + " " + _sl_lbl +
                                        " · closed (EXTERNAL) at " + _fmt_p(ep) +
                                        "\n" + ("+" if p >= 0 else "-") +
                                        "$" + f"{abs(p):.2f}")
                                import threading as _th
                                _th.Thread(target=_ext_tg, daemon=True).start()
                            continue
                    except Exception as _ece:
                        print(f"[EXTERNAL_CLOSE] {sym} check error: {_ece}")
                # ─────────────────────────────────────────────────────────────
                # Update excursion tracking regardless of sl_price
                ep = trade.get("extreme_price") or current
                trade["extreme_price"] = (min(ep, current) if is_short
                                          else max(ep, current))
                ap = trade.get("adverse_price") or current
                trade["adverse_price"] = (max(ap, current) if is_short
                                          else min(ap, current))

                # -- Adverse cut: excessive adverse move with no meaningful MFE -------
                _adv_price = trade.get("adverse_price") or current
                _ext_price  = trade.get("extreme_price") or current
                _entry      = trade.get("entry_price", 0)
                _size       = trade.get("remaining_size", trade.get("size", 0)) or 0
                _adv_pnl    = ((_adv_price - _entry) * _size) if not is_short else ((_entry - _adv_price) * _size)
                _mfe_pnl    = ((_ext_price - _entry) * _size) if not is_short else ((_entry - _ext_price) * _size)
                _cut_usd    = ADVERSE_CUT_USD.get(sym, ADVERSE_CUT_DEFAULT_USD)
                _cpnl       = ((_entry - current) * _size if is_short
                               else (current - _entry) * _size)

                # ── PROFIT GUARDS: WALL_TP → SE (before AR/SLP) ─────────────────
                # These gates exit while cpnl > 0.  They read _ar_pre
                # (previous-tick shadow) -- no dependency on current-tick shadow block.
                # PEAK_PROTECT (after shadow update) replaces PEAK_DECAY_10 + PEAK_GIVEBACK.
                _ar_pre       = _peak_shadow.get(key, {})
                _session      = get_session_name()
                _sentinel_pct = SENTINEL_MIN_PEAK_PCT.get(
                    (sym, _session), SENTINEL_MIN_PEAK_PCT_DEFAULT)
                _notional = (
                    trade.get("margin", 2000)
                    * trade.get("leverage", 5))
                _sentinel_min = _notional * _sentinel_pct

                # -- TP1: FIRST EXIT -- price target partial close (fires before all condition gates) --
                if not tp1_hit and tp1_price:
                    tp1_reached = (is_short and current <= tp1_price) or \
                                  (not is_short and current >= tp1_price)
                    print(f"[EXIT CHECK] {sym} {direction} price={current} "
                          f"tp1={tp1_price} tp1_hit={tp1_hit} Ã¢ÂÂ "
                          f"{'TP1 TRIGGERED Ã¢ÂÂ partial close' if tp1_reached else 'watching tp1'}")
                    if tp1_reached:
                        _do_partial_close_tp1(key, trade, current)
                        continue


                # -- WALL_TP: exit at book wall on runner (post-TP1 only) -------------
                # Largest bid (SHORT)/ask (LONG) within 0.30% of price. Requires tp1_hit: guards
                # 30% runner only; pre-TP1 protection is handled by PEAK_PROTECT.
                _ps_wt = next((p for p in app_state.pair_states if p.get("symbol") == sym), None)
                if _ps_wt and _cpnl > 0 and _ar_pre.get("be_armed") and tp1_hit:
                    if is_short:
                        _bw = _ps_wt.get("bid_wall")
                        if _bw and _bw["dist_pct"] <= 0.30:
                            print(f"[WALL_TP] HL {sym} SHORT"
                                  f" wall={_bw['price']:.5f}"
                                  f" dist={_bw['dist_pct']:.3f}%"
                                  f" ratio={_bw['ratio']:.1f}x"
                                  f" cpnl={_cpnl:.2f}")
                            _do_close_trade(key, trade, current, "WALL_TP")
                            continue
                    else:
                        _aw = _ps_wt.get("ask_wall")
                        if _aw and _aw["dist_pct"] <= 0.30:
                            print(f"[WALL_TP] HL {sym} LONG"
                                  f" wall={_aw['price']:.5f}"
                                  f" dist={_aw['dist_pct']:.3f}%"
                                  f" ratio={_aw['ratio']:.1f}x"
                                  f" cpnl={_cpnl:.2f}")
                            _do_close_trade(key, trade, current, "WALL_TP")
                            continue

                # -- SIGNAL_EXHAUSTION: J1H turns against runner (post-TP1 only) ----
                # Tracks J1H peak (LONG)/trough (SHORT), fires on SE_J1H_DECAY_PTS. Requires tp1_hit.
                # Evidence: June 29 39-trade analysis + June 30 candle confirmation.
                _cur_j1h = None
                for _ps in app_state.pair_states:
                    if _ps.get("symbol") == sym:
                        _cur_j1h = _ps.get("j1h")
                        break
                if _cur_j1h is not None and _cpnl > 0 and tp1_hit:
                    if not is_short:
                        # LONG: track J1H peak, fire when decays SE_J1H_DECAY_PTS+
                        _prev = _se_j1h_extreme.get(key, _cur_j1h)
                        _se_j1h_extreme[key] = max(_prev, _cur_j1h)
                        _j1h_decay = _se_j1h_extreme[key] - _cur_j1h
                        if _j1h_decay >= _scanner_mod.SE_J1H_DECAY_PTS and _ar_pre.get("peak_pnl_usd", 0.0) >= _sentinel_min and _cpnl > 0:
                            print(f"[SIGNAL_EXHAUSTION] HL {sym} {direction}"
                                  f" j1h_peak={_se_j1h_extreme[key]:.1f}"
                                  f" j1h_now={_cur_j1h:.1f}"
                                  f" decay={_j1h_decay:.1f}"
                                  f" cpnl={_cpnl:.2f}")
                            _do_close_trade(
                                key, trade, current, "SIGNAL_EXHAUSTION")
                            _se_j1h_extreme.pop(key, None)
                            continue
                    else:
                        # SHORT: track J1H trough, fire when rises SE_J1H_DECAY_PTS+
                        _prev = _se_j1h_extreme.get(key, _cur_j1h)
                        _se_j1h_extreme[key] = min(_prev, _cur_j1h)
                        _j1h_rise = _cur_j1h - _se_j1h_extreme[key]
                        if _j1h_rise >= _scanner_mod.SE_J1H_DECAY_PTS and _ar_pre.get("peak_pnl_usd", 0.0) >= _sentinel_min and _cpnl > 0:
                            print(f"[SIGNAL_EXHAUSTION] HL {sym} {direction}"
                                  f" j1h_trough={_se_j1h_extreme[key]:.1f}"
                                  f" j1h_now={_cur_j1h:.1f}"
                                  f" rise={_j1h_rise:.1f}"
                                  f" cpnl={_cpnl:.2f}")
                            _do_close_trade(
                                key, trade, current, "SIGNAL_EXHAUSTION")
                            _se_j1h_extreme.pop(key, None)
                            continue

                # -- ARMED_REVERSAL (pre-SLP guard): backstop for fast reversals ----
                # WALL_TP/SE run first above to protect profit while positive;
                # AR catches reversals after arming. Peak-scaled threshold:
                #   peak_r >= 0.10R -> fire at cpnl < +0.02R (protect high-MFE trades)
                #   peak_r <  0.10R -> fire at cpnl < -0.05R (original hysteresis)
                # Requires 2 consecutive scan ticks below threshold to avoid
                # false-triggering on intracandle dips (ar_below_thresh_ticks).
                # _ar_pre is already defined in the profit-guards block above.
                _ar_dr     = trade.get("dollar_risk", 0) or 0
                _ar_peak_r = _ar_pre.get("peak_pnl_r", 0.0)
                _ar_thresh = 0.02 if _ar_peak_r >= 0.10 else -0.05
                # Trend trades: skip ARMED_REVERSAL — trends retrace before continuing
                if (trade.get("trade_mode") != "trend"
                        and _ar_pre.get("be_armed")
                        and _ar_dr > 0
                        and _ar_pre.get("ar_below_thresh_ticks", 0) >= 1
                        and (_cpnl / _ar_dr) < _ar_thresh):
                    print(f"[ARMED_REVERSAL] HL {sym} {direction}"
                          f" peak_r={_ar_peak_r:.3f}R thresh={_ar_thresh:.2f}R"
                          f" ticks={_ar_pre.get('ar_below_thresh_ticks', 0)}"
                          f" peak={_ar_pre.get('peak_pnl_usd', 0):.2f}"
                          f" cpnl={_cpnl:.2f}"
                          f" cpnl_r={_cpnl/_ar_dr:.3f}R")
                    _do_close_trade(key, trade, current, "ARMED_REVERSAL")
                    continue

                # -- SL_PROXIMITY_EXIT: all tiers, all pairs, both directions.
                # Threshold: 50% of SL distance consumed → exit for all states.
                # Dead "armed pre-TP1 0.40" branch removed — AR fires first when
                # armed+adverse (cpnl < -0.05R hysteresis). Unified at 0.50 for simplicity.
                _entry_sp = trade.get("entry_price", 0) or 0
                _sl_sp    = trade.get("sl_price")
                if _entry_sp > 0 and _sl_sp:
                    if not is_short:
                        _sl_distance_pct = (
                            (_entry_sp - _sl_sp) / _entry_sp)
                        _price_to_sl_pct = (
                            (current - _sl_sp) / _entry_sp)
                    else:
                        _sl_distance_pct = (
                            (_sl_sp - _entry_sp) / _entry_sp)
                        _price_to_sl_pct = (
                            (_sl_sp - current) / _entry_sp)
                    _slp_mul = 0.50
                    if (_sl_distance_pct > 0 and
                            _price_to_sl_pct <=
                            _sl_distance_pct * _slp_mul):
                        print(
                            f"[SL_PROXIMITY] {sym} {direction}"
                            f" price={current}"
                            f" entry={_entry_sp}"
                            f" sl={_sl_sp}"
                            f" price_to_sl_pct="
                            f"{_price_to_sl_pct*100:.2f}%"
                            f" sl_distance_pct="
                            f"{_sl_distance_pct*100:.2f}%"
                            f" — exiting")
                        _do_close_trade(
                            key, trade,
                            current,
                            "SL_PROXIMITY")
                        continue


                _elapsed = time.time() - trade.get("opened_at", time.time())
                _entry_px = trade.get("entry_price", 0) or 0
                _adverse_pct = (
                    (_entry_px - current) / _entry_px
                    if not is_short else
                    (current - _entry_px) / _entry_px
                ) if _entry_px > 0 else 0
                _dr_ac = trade.get("dollar_risk", 0) or 0
                # Tier 1: continuous floor
                _kill_floor_hit = (
                    _adverse_pct >=
                    _scanner_mod.KILL_PCT_FLOOR
                )
                if _kill_floor_hit:
                    print(f"[KILL] HL {sym} {direction}"
                          f" adverse_pct={_adverse_pct*100:.2f}%"
                          f" elapsed={_elapsed:.0f}s")
                    _do_close_trade(
                        key, trade, current, "KILL")
                    # Per-pair direction session adverse-exit count
                    _skey = f"{sym}_{direction}_{get_session_name()}"
                    _session_sl_counts[_skey] = _session_sl_counts.get(_skey, 0) + 1
                    if _session_sl_counts[_skey] >= 2 and _skey not in _session_halted:
                        _session_halted.add(_skey)
                        print(f"[SESSION HALT] {sym} {direction} — 2 adverse exits (KILL) in {get_session_name()} session. Halted for remainder of session.")
                    continue

                # -- Peak PnL protection shadow (observation only, no exit logic) ----
                try:
                    _sh = _peak_shadow.setdefault(key, {
                        "peak_pnl_usd":    0.0,
                        "peak_pnl_r":      0.0,
                        "peak_reached_at": None,
                        "be_armed":        False,
                        "giveback_streak": 0,
                        "ar_below_thresh_ticks": 0,
                        "last_cpnl_r":     None,
                        "d20_at": None, "d20_pnl": None, "d20_phase": None,
                        "d30_at": None, "d30_pnl": None, "d30_phase": None,
                        "d40_at": None, "d40_pnl": None, "d40_phase": None,
                        "last_peak_candle_ts": 0,
                        "peak_set_time":       0.0,
                    })
                    _sz   = trade.get("remaining_size", trade.get("size", 0)) or 0
                    _ent  = trade.get("entry_price", 0) or 0
                    _cpnl = ((current - _ent) * _sz if not is_short
                             else (_ent - current) * _sz)
                    # Arm at 0.05R profit (R-based) — fixes 1R arming barrier.
                    # be_price ±0.1% hardcoded + dollar_risk 0.1% floor → arming
                    # required 1.0R on all tight-stop EU pairs (ETH, SOL, BTC, etc.)
                    # disabling PEAK_PROTECT/ARMED_REVERSAL/WALL_TP for every trade
                    # peaking < 1R. Now arms at the PEAK_PROTECT activation threshold.
                    if _dr_ac > 0 and (_cpnl / _dr_ac) >= 0.05:
                        _sh["be_armed"] = True
                    # -- Giveback streak: consecutive scan ticks of declining cpnl in R --
                    # Feeds PEAK_GIVEBACK exit. Reset when a new R-peak is set.
                    if _sh["be_armed"] and _dr_ac > 0:
                        _cpnl_r_tick = _cpnl / _dr_ac
                        if _cpnl_r_tick > _sh["peak_pnl_r"]:
                            _sh["peak_pnl_r"]      = _cpnl_r_tick
                            _sh["giveback_streak"] = 0
                        elif _sh["last_cpnl_r"] is not None:
                            if _cpnl_r_tick < _sh["last_cpnl_r"]:
                                _sh["giveback_streak"] += 1
                            else:
                                _sh["giveback_streak"] = 0
                        _sh["last_cpnl_r"] = _cpnl_r_tick
                        # AR 2-tick confirmation: track consecutive ticks below threshold
                        _ar_pk_sh  = _sh.get("peak_pnl_r", 0.0)
                        _ar_thr_sh = 0.02 if _ar_pk_sh >= 0.10 else -0.05
                        if _cpnl_r_tick < _ar_thr_sh:
                            _sh["ar_below_thresh_ticks"] = _sh.get("ar_below_thresh_ticks", 0) + 1
                        else:
                            _sh["ar_below_thresh_ticks"] = 0
                    _now_candle_ts = (
                        int(time.time())
                        // 60) * 60
                    if (_sh["be_armed"] and _cpnl > _sh["peak_pnl_usd"]
                            and _now_candle_ts > _sh["last_peak_candle_ts"]):
                        _sh["peak_pnl_usd"]    = _cpnl
                        _sh["peak_reached_at"] = datetime.now(timezone.utc).isoformat()
                        _sh["last_peak_candle_ts"] = _now_candle_ts
                        _sh["peak_set_time"]   = time.time()
                        _sh["d20_at"] = _sh["d20_pnl"] = _sh["d20_phase"] = None
                        _sh["d30_at"] = _sh["d30_pnl"] = _sh["d30_phase"] = None
                        _sh["d40_at"] = _sh["d40_pnl"] = _sh["d40_phase"] = None
                    if _sh["be_armed"]:
                        _psh_now   = datetime.now(timezone.utc).isoformat()
                        _psh_phase = "post_tp1" if trade.get("tp1_hit") else "pre_tp1"
                        for _psh_th, _psh_dk, _psh_pk, _psh_phk in (
                            (0.20, "d20_at", "d20_pnl", "d20_phase"),
                            (0.30, "d30_at", "d30_pnl", "d30_phase"),
                            (0.40, "d40_at", "d40_pnl", "d40_phase"),
                        ):
                            if (_sh[_psh_dk] is None
                                    and _cpnl < _sh["peak_pnl_usd"] * (1 - _psh_th)):
                                _sh[_psh_dk]  = _psh_now
                                _sh[_psh_pk]  = round(_cpnl, 2)
                                _sh[_psh_phk] = _psh_phase
                except Exception as _psh_e:
                    print(f"[SHADOW] poll error: {_psh_e}")

                # -- PEAK_PROTECT: armed trade given back beyond decay threshold ----------
                # Requires peak_pnl_usd >= sentinel_min (per-pair/per-session floor from
                # SENTINEL_MIN_PEAK_PCT). Anchors (BTC/SOL/ETH/XRP) carry higher floors so
                # small peaks on wide-ranging pairs don't trigger premature exits.
                # TP1 proximity tiers (data-driven, July 2026 analysis):
                #   >= 60% to TP1  ->  suppressed: trade is at TP1's door, let it run
                #   40-60% to TP1  ->  lenient: 40% decay allowed (threshold=0.60)
                #   <  40% to TP1  ->  standard: 30% decay (threshold=0.70)
                _pp_tp1_dist   = ((tp1_price - _entry) if not is_short else (_entry - tp1_price)) if tp1_price else 0.0
                _pp_exit_dist  = (current - _entry) if not is_short else (_entry - current)
                _pp_pct_to_tp1 = (_pp_exit_dist / _pp_tp1_dist) if _pp_tp1_dist > 0 else 0.0
                _pp_suppressed = _pp_pct_to_tp1 >= 0.60
                _pp_decay_th   = 0.60 if _pp_pct_to_tp1 >= 0.40 else 0.70
                if (not _pp_suppressed
                        and _sh.get("be_armed")
                        and _elapsed >= 16
                        and _dr_ac > 0
                        and _sh.get("peak_pnl_r", 0) >= 0.05
                        and _sh.get("peak_pnl_usd", 0.0) >= _sentinel_min
                        and (_cpnl / _dr_ac) < _sh.get("peak_pnl_r", 0) * _pp_decay_th):
                    _pp_r = _cpnl / _dr_ac
                    print(f"[PEAK_PROTECT] HL {sym} {direction}"
                          f" peak_usd={_sh['peak_pnl_usd']:.2f} sentinel_min={_sentinel_min:.2f}"
                          f" peak_r={_sh['peak_pnl_r']:.3f}R"
                          f" cur_r={_pp_r:.3f}R"
                          f" pct_to_tp1={_pp_pct_to_tp1:.0%}"
                          f" decay_th={_pp_decay_th:.0%}"
                          f" cpnl={_cpnl:.2f}")
                    _do_close_trade(key, trade, current, "PEAK_PROTECT")
                    continue


                # -- Adverse cut shadow (observation only, no exit logic) ------
                try:
                    _ent_a  = trade.get("entry_price", 0) or 0
                    # sl_dist is stored once at trade open (immutable original
                    # distance). The abs(...) fallback is defensive dead code
                    # after FIX 1 Ã¢ÂÂ kept only as a guard against legacy rows.
                    _sl_d_a = (trade.get("sl_dist") or
                               abs(_ent_a - (trade.get("sl_price") or _ent_a)))
                    _sz_a   = trade.get("remaining_size", trade.get("size", 0)) or 0
                    _cpnl_a = ((current - _ent_a) * _sz_a if not is_short
                               else (_ent_a - current) * _sz_a)
                    if _cpnl_a < 0 and _sl_d_a and _ent_a:
                        _toward_sl_a = (_ent_a - current) if not is_short else (current - _ent_a)
                        _sl_pct_a    = min(_toward_sl_a / _sl_d_a, 1.0)
                        if _sl_pct_a > 0:
                            _ash = _adverse_shadow.setdefault(key, {
                                "ruleA_at": None, "ruleA_min": None,
                                "ruleA_pct": None, "ruleA_pnl": None,
                                "ruleB_at": None, "ruleB_min": None,
                                "ruleB_pct": None, "ruleB_pnl": None,
                                "ruleC_at": None, "ruleC_min": None,
                                "ruleC_pct": None, "ruleC_pnl": None,
                                "ruleD_at": None, "ruleD_min": None,
                                "ruleD_pct": None, "ruleD_pnl": None,
                                "ever_recovered": False,
                            })
                            _ash_now = datetime.now(timezone.utc).isoformat()
                            _ash_ela = (int(time.time()) - trade.get("opened_at", int(time.time()))) / 60.0
                            _ash_pnl = round(_cpnl_a, 2)
                            for _rname, _rtmin, _rspct, _rk_at, _rk_min, _rk_pct, _rk_pnl in (
                                ("A", 60,  0.80, "ruleA_at", "ruleA_min", "ruleA_pct", "ruleA_pnl"),
                                ("B", 90,  0.75, "ruleB_at", "ruleB_min", "ruleB_pct", "ruleB_pnl"),
                                ("C", 120, 0.70, "ruleC_at", "ruleC_min", "ruleC_pct", "ruleC_pnl"),
                                ("D", 45,  0.85, "ruleD_at", "ruleD_min", "ruleD_pct", "ruleD_pnl"),
                            ):
                                if (_ash[_rk_at] is None
                                        and _ash_ela  >= _rtmin
                                        and _sl_pct_a >= _rspct):
                                    _ash[_rk_at]  = _ash_now
                                    _ash[_rk_min] = round(_ash_ela, 1)
                                    _ash[_rk_pct] = round(_sl_pct_a, 4)
                                    _ash[_rk_pnl] = _ash_pnl
                                    print("[ADVERSE SHADOW] rule " + _rname + " triggered: " +
                                          sym + " " + direction +
                                          " elapsed=" + str(round(_ash_ela, 1)) + "m" +
                                          " sl_pct=" + str(round(_sl_pct_a * 100, 1)) + "%" +
                                          " pnl=$" + str(_ash_pnl))
                                    if TELEGRAM_ENABLED:
                                        def _adverse_watch_tg(sym=sym, direction=direction, rule=_rname, elapsed=_ash_ela, pct=_sl_pct_a, pnl=_ash_pnl):
                                            d_lbl = "S" if direction == "SHORT" else "L"
                                            _tg_post("\U0001F7E7\U0001F7E7\U0001F7E7 ADVERSE WATCH \U0001F7E7\U0001F7E7\U0001F7E7"
                                                     + "\n<b>" + sym + " " + d_lbl + " \u00B7 Rule " + rule + "</b>"
                                                     + "\n" + f"{elapsed:.0f}" + "min elapsed \u00B7 " + f"{pct*100:.0f}" + "% to SL"
                                                     + "\nCurrent: " + ("+" if pnl >= 0 else "-") + "$" + f"{abs(pnl):.2f}"
                                                     + "\n<i>Observation only \u2014 no action taken</i>")
                                        threading.Thread(target=_adverse_watch_tg, daemon=True).start()
                    elif key in _adverse_shadow and not _adverse_shadow[key]["ever_recovered"]:
                        _adverse_shadow[key]["ever_recovered"] = True
                except Exception as _ash_e:
                    print("[ADVERSE SHADOW] poll error: " + str(_ash_e))

                # -- PnL sign shadow (observation only, no exit logic) ----------
                try:
                    _ss_sz   = trade.get("remaining_size", trade.get("size", 0)) or 0
                    _ss_ent  = trade.get("entry_price", 0) or 0
                    _ss_pnl  = ((current - _ss_ent) * _ss_sz if not is_short
                                else (_ss_ent - current) * _ss_sz)
                    _ss_sign = ("positive"  if _ss_pnl >  0.01
                                else "negative" if _ss_pnl < -0.01
                                else "breakeven")
                    _ssb = _sign_shadow.setdefault(key, {
                        "last_sign": None, "transitions": [],
                    })
                    if _ssb["last_sign"] != _ss_sign:
                        _ssb["transitions"].append({
                            "ts":   datetime.now(timezone.utc).isoformat(),
                            "sign": _ss_sign,
                            "pnl":  round(_ss_pnl, 2),
                        })
                        print("[SIGN SHADOW] " + sym + " " + direction +
                              " sign: " + str(_ssb["last_sign"]) +
                              " -> " + _ss_sign +
                              " pnl=$" + str(round(_ss_pnl, 2)))
                        _ssb["last_sign"] = _ss_sign
                except Exception as _sse:
                    print("[SIGN SHADOW] poll error: " + str(_sse))

                # -- Signal invalidation shadow (observation only, no exit logic) --
                try:
                    _sis = _signal_shadow.setdefault(key, {
                        "stochflip_at": None, "stochflip_min": None,
                        "stochflip_pnl": None, "stochflip_sl_pct": None,
                        "jgiveback_at": None, "jgiveback_min": None,
                        "jgiveback_pnl": None, "jgiveback_sl_pct": None,
                        "btcregime_at": None, "btcregime_min": None,
                        "btcregime_pnl": None, "btcregime_sl_pct": None,
                        "btcregime_old": None, "btcregime_new": None,
                    })
                    _sis_now  = datetime.now(timezone.utc).isoformat()
                    _sis_ela  = (int(time.time()) - trade.get("opened_at", int(time.time()))) / 60.0
                    _sis_sz   = trade.get("remaining_size", trade.get("size", 0)) or 0
                    _sis_ent  = trade.get("entry_price", 0) or 0
                    _sis_cpnl = ((current - _sis_ent) * _sis_sz if not is_short
                                 else (_sis_ent - current) * _sis_sz)
                    _sis_pnl  = round(_sis_cpnl, 2)
                    _sis_sld  = (trade.get("sl_dist") or
                                 abs(_sis_ent - (sl_price or _sis_ent)))
                    _sis_toward_sl = ((_sis_ent - current) if not is_short
                                      else (current - _sis_ent))
                    _sis_sl_pct = (round(max(0.0, min(_sis_toward_sl / _sis_sld, 1.0)), 4)
                                   if _sis_sld and _sis_ent else None)
                    # 1. STOCH_FLIP: fast 8-3-3 K/D cross direction reverses from entry
                    if _sis["stochflip_at"] is None:
                        _sf_cur = _scanner_mod._last_stoch_fast.get(sym, (50.0, 50.0))
                        _sf_ck, _sf_cd  = _sf_cur
                        _sf_ek  = trade.get("stoch_k_fast")
                        _sf_ed  = trade.get("stoch_d_fast")
                        if _sf_ek is not None and _sf_ed is not None:
                            _entry_k_above_d = _sf_ek > _sf_ed
                            _cur_k_above_d   = _sf_ck > _sf_cd
                            if _entry_k_above_d != _cur_k_above_d:
                                _sis["stochflip_at"]     = _sis_now
                                _sis["stochflip_min"]    = round(_sis_ela, 1)
                                _sis["stochflip_pnl"]    = _sis_pnl
                                _sis["stochflip_sl_pct"] = _sis_sl_pct
                                print("[SIG SHADOW] STOCH_FLIP " + sym + " " + direction
                                      + " entry_K=" + str(round(_sf_ek, 1))
                                      + " entry_D=" + str(round(_sf_ed, 1))
                                      + " cur_K=" + str(round(_sf_ck, 1))
                                      + " cur_D=" + str(round(_sf_cd, 1))
                                      + " elapsed=" + str(round(_sis_ela, 1)) + "m"
                                      + " pnl=$" + str(_sis_pnl))
                    # 2. J_GIVEBACK: current J15M crosses back past entry-time J15M adversely
                    if _sis["jgiveback_at"] is None:
                        _jg_ent_j = trade.get("j15m")
                        _jg_ps    = next((p for p in app_state.pair_states
                                          if p.get("symbol") == sym), None)
                        _jg_cur_j = _jg_ps.get("j15m") if _jg_ps else None
                        if _jg_ent_j is not None and _jg_cur_j is not None:
                            _jgive = ((not is_short and _jg_cur_j < _jg_ent_j) or
                                      (is_short      and _jg_cur_j > _jg_ent_j))
                            if _jgive:
                                _sis["jgiveback_at"]     = _sis_now
                                _sis["jgiveback_min"]    = round(_sis_ela, 1)
                                _sis["jgiveback_pnl"]    = _sis_pnl
                                _sis["jgiveback_sl_pct"] = _sis_sl_pct
                                print("[SIG SHADOW] J_GIVEBACK " + sym + " " + direction
                                      + " entry_J=" + str(round(_jg_ent_j, 1))
                                      + " cur_J="   + str(round(_jg_cur_j, 1))
                                      + " elapsed=" + str(round(_sis_ela, 1)) + "m"
                                      + " pnl=$" + str(_sis_pnl))
                    # 3. BTC_REGIME_SHIFT: regime changes to contradict trade direction
                    if _sis["btcregime_at"] is None:
                        _brs_entry  = trade.get("btc_regime_entry")
                        _brs_cur    = _get_btc_regime()
                        if _brs_entry is not None and _brs_cur != _brs_entry:
                            _brs_contra = ((not is_short and _brs_cur in ("LONG_BLOCKED", "NEUTRAL_BLOCK")) or
                                           (is_short     and _brs_cur in ("SHORT_BLOCKED", "NEUTRAL_BLOCK")))
                            if _brs_contra:
                                _sis["btcregime_at"]     = _sis_now
                                _sis["btcregime_min"]    = round(_sis_ela, 1)
                                _sis["btcregime_pnl"]    = _sis_pnl
                                _sis["btcregime_sl_pct"] = _sis_sl_pct
                                _sis["btcregime_old"]    = _brs_entry
                                _sis["btcregime_new"]    = _brs_cur
                                print("[SIG SHADOW] BTC_REGIME_SHIFT " + sym + " " + direction
                                      + " " + str(_brs_entry) + " -> " + str(_brs_cur)
                                      + " elapsed=" + str(round(_sis_ela, 1)) + "m"
                                      + " pnl=$" + str(_sis_pnl))
                except Exception as _sis_e:
                    print("[SIG SHADOW] poll error: " + str(_sis_e))
                # Ã¢ÂÂÃ¢ÂÂ SL breach Ã¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂ
                # SHORT: SL triggers when price RISES above sl_price
                # LONG : SL triggers when price FALLS below sl_price
                if not sl_price:
                    sl_breached = False  # no SL set yet - skip breach check
                else:
                    sl_breached = (is_short and current >= sl_price) or \
                                  (not is_short and current <= sl_price)

                if sl_breached:
                    print(f"[EXIT CHECK] {sym} {direction} price={current} "
                          f"sl={sl_price} tp1={tp1_price} Ã¢ÂÂ SL BREACHED Ã¢ÂÂ closing")
                    _do_close_trade(key, trade, current, "SL")
                    # Per-pair direction session SL count
                    _skey = f"{sym}_{direction}_{get_session_name()}"
                    _session_sl_counts[_skey] = _session_sl_counts.get(_skey, 0) + 1
                    if _session_sl_counts[_skey] >= 2 and _skey not in _session_halted:
                        _session_halted.add(_skey)
                        print(f"[SESSION HALT] {sym} {direction} Ã¢ÂÂ 2 SL hits in {get_session_name()} session. Halted for remainder of session.")
                    # $100 SL cooldown Ã¢ÂÂ override with 90-min directional cooldown
                    _rem_sz = trade.get("remaining_size", trade.get("size", 0))
                    _sl_pnl = (current - trade["entry_price"]) * _rem_sz if not is_short \
                              else (trade["entry_price"] - current) * _rem_sz
                    if abs(_sl_pnl) >= 100:
                        _exp = time.time() + 90 * 60
                        _scanner_mod._cooldowns[f"{sym}{direction}"] = _exp
                        _large_sl_cooldowns[f"{sym}{direction}"]     = _exp
                        print(f"[LARGE SL COOLDOWN] {sym} {direction} Ã¢ÂÂ SL ${abs(_sl_pnl):.2f} >= $100 threshold. 90 min cooldown applied.")
                    continue


                # -- BAD_TRADE_EXIT: progressive time-based cut for never-armed trades --
                # Replaces ADVERSE_CUT (90s/0.05R), TREND_ADVERSE_EXIT (300s/0.20R),
                # DEAD_TRADE_KILL (600s/0.08R), and TIME_ADVERSE_EXIT (600s/0.15R).
                # Single rule: NOT armed + cpnl <= 0 + MFE < threshold(elapsed).
                # Thresholds: 0.08R at 180s · 0.15R at 300s · 0.20R at 600s.
                # MFE escape hatch: if mfe_r >= 0.05R the trade showed real excursion
                # (possibly between scan ticks, missing the be_armed window). Skip BTE
                # entirely — let PEAK_PROTECT / ARMED_REVERSAL / SL handle it instead.
                if (not _sh.get("be_armed", False)
                        and _cpnl <= 0
                        and _dr_ac > 0):
                    _bte_mfe_r = _mfe_pnl / _dr_ac
                    _bte_fired = False
                    if _bte_mfe_r < 0.05:  # escape hatch: real excursion — skip BTE
                        _bte_schedule = (
                            ((300, 0.08), (450, 0.15), (600, 0.20))
                            if trade.get("trade_mode") == "trend"
                            else ((180, 0.08), (300, 0.15), (600, 0.20))
                        )
                        for _bte_age, _bte_thr in _bte_schedule:
                            if _elapsed >= _bte_age and _bte_mfe_r < _bte_thr:
                                _bte_fired = True
                                break
                    if _bte_fired:
                        print(f"[BAD_TRADE_EXIT] HL {sym} {direction}"
                              f" elapsed={_elapsed:.0f}s"
                              f" mfe_r={_bte_mfe_r:.3f}R"
                              f" cpnl={_cpnl:.2f}")
                        _do_close_trade(key, trade, current, "BAD_TRADE_EXIT")
                        _skey = f"{sym}_{direction}_{get_session_name()}"
                        _session_sl_counts[_skey] = _session_sl_counts.get(_skey, 0) + 1
                        if _session_sl_counts[_skey] >= 2 and _skey not in _session_halted:
                            _session_halted.add(_skey)
                            print(f"[SESSION HALT] {sym} {direction}"
                                  f" — 2 adverse exits (BAD_TRADE_EXIT)"
                                  f" in {get_session_name()}")
                            _dh_col = "halt_short" if direction == "SHORT" else "halt_long"
                            try:
                                _sb_dh = _get_supabase()
                                if _sb_dh:
                                    _sb_dh.table("hl_scanner_state").update(
                                        {_dh_col: True}).eq("id", 1).execute()
                                    print(f"[DIR_HALT] HL {direction} session-paused"
                                          f" — {_dh_col}=True written to Supabase")
                            except Exception as _dh_e:
                                print(f"[DIR_HALT] Supabase write failed: {_dh_e}")
                        continue



                if tp1_hit:
                    _ps   = next((p for p in app_state.pair_states if p.get("symbol") == sym), None)
                    _atr  = (_ps.get("atr15m") or 0) if _ps else 0
                    if _atr > 0:
                        _best = trade.get("trail_best_price") or current
                        if not is_short:
                            _best       = max(_best, current)
                            _trail_stop = _best - _atr * TRAIL_ATR_MULTIPLIER
                            if current <= _trail_stop:
                                _do_trailblazer_close(key, trade, current, _best, _trail_stop)
                                continue
                        else:
                            _best       = min(_best, current)
                            _trail_stop = _best + _atr * TRAIL_ATR_MULTIPLIER
                            if current >= _trail_stop:
                                _do_trailblazer_close(key, trade, current, _best, _trail_stop)
                                continue
                        trade["trail_best_price"] = _best
                        trade["trail_stop_price"] = round(_trail_stop, 6)
                        app_state.open_trades[key]["trail_best_price"] = _best
                        app_state.open_trades[key]["trail_stop_price"] = round(_trail_stop, 6)
                        print(f"[TRAIL] {sym} {direction} best={_best} stop={round(_trail_stop,6)} current={current}")


                # No exit this cycle
                _trail_info = (f" trail_best={trade.get('trail_best_price')} trail_stop={trade.get('trail_stop_price')}"
                               if tp1_hit else "")
                print(f"[EXIT CHECK] {sym} {direction} price={current} "
                      f"sl={sl_price} tp1={tp1_price}{_trail_info} Ã¢ÂÂ no exit")

            except Exception as e:
                print(f"[EXIT MONITOR] {trade.get('symbol')} {trade.get('direction')} error: {e}")
                continue

        _flush_sentinel_sweep()
        # -- Sentinel Executor Phase 0: observe only, no closes --
        try:
            _ex = _sentinel_mod.check_executor(app_state.open_trades)
            if _ex and _ex.get("telegram_text") and TELEGRAM_ENABLED:
                threading.Thread(
                    target=lambda m=_ex["telegram_text"]: _tg_post(m),
                    daemon=True).start()
        except Exception as _exe:
            print(f"[SENTINEL] executor error: {_exe}")
        await asyncio.sleep(PRICE_INTERVAL_SECONDS)


async def _state_heartbeat_loop():
    """Saves state every 60 s while any position is open."""
    while True:
        await asyncio.sleep(60)
        if app_state.open_trades:
            _save_state()


# Ã¢ÂÂÃ¢ÂÂ Lifespan Ã¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂ

async def _supervised(coro_fn, name: str):
    """Runs coro_fn() forever. If it ever exits (crash or otherwise),
    logs loudly and relaunches after a short delay, indefinitely."""
    while True:
        try:
            await coro_fn()
        except Exception as e:
            print(f"[WATCHDOG] {name} task died: {e!r} — respawning in 2s")
        else:
            print(f"[WATCHDOG] {name} task exited cleanly (unexpected) — respawning in 2s")
        await asyncio.sleep(2)


async def _log_alert_outcome(
        alert: dict,
        outcome: str,
        venue: str,
        pending_duration_seconds:
            int = 0,
        confirm_price: float = None):
    """Write one row to alert_log
    table for every alert outcome:
    J1H_DISCARDED, EXPIRED_AGE,
    EXPIRED_J15M, EXPIRED_PRICE,
    CONFIRMED. Best-effort -- never
    raises, never blocks scan loop.
    """
    try:
        _sb = _get_supabase()
        if _sb is None:
            return
        _row = {
            "venue":
                venue,
            "pair":
                alert.get("symbol"),
            "direction":
                alert.get("direction"),
            "signal_price":
                alert.get(
                    "entry_price"),
            "be_confirm_price":
                alert.get(
                    "be_confirm_price"),
            "j1h_at_signal":
                alert.get("j1h"),
            "j1h_prev_at_signal":
                alert.get("j1h_prev"),
            "j1h_prev_valid":
                alert.get(
                    "j1h_prev_valid",
                    True),
            "outcome":
                outcome,
            "pending_duration_seconds":
                pending_duration_seconds,
            "confirm_price":
                confirm_price,
            "session":
                alert.get("session"),
            "tier":
                alert.get("tier"),
            "score":
                alert.get("score"),
            "adx":
                alert.get("adx1h"),
            "j15m_at_signal":
                alert.get("j15m"),
            "j5m_at_signal":
                alert.get("j5m", None),
            "depth_bid_pct":
                alert.get("depth_bid_pct"),
            "depth_ask_pct":
                alert.get("depth_ask_pct"),
            "depth_context":
                alert.get("depth_context"),
            "vol_15m":
                alert.get("vol_15m"),
            "vol_ma15m":
                alert.get("vol_ma15m"),
            "vol_surge":
                alert.get("vol_surge"),
            "ma_stack_1h":
                alert.get("ma_stack_1h"),
            "btc_regime_context":
                alert.get("btc_regime_context"),
            "j1h_short_direction":
                alert.get("j1h_short_direction"),
        }
        _sb.table("alert_log")\
           .insert(_row)\
           .execute()
    except Exception as _e:
        print(f"[ALERT LOG] write "
              f"failed: {_e}")


async def _process_pending_alerts():
    """Called each scan cycle. Checks pending alerts for expiry or proj_pnl
    gate pass. Expiry thresholds reuse data-derived staleness values:
    age>480s, J15M drift>30pts, price drift>1.5%.
    Proj_pnl gate: current price must not be more than 0.1% adverse from
    signal_price (LONG: cur >= signal*0.999, SHORT: cur <= signal*1.001).
    Opens immediately once the gate passes -- no price-move wait.
    """
    if not _pending_alerts:
        return
    _to_remove = []
    for _pk, _alert in list(_pending_alerts.items()):
        _sym   = _alert["symbol"]
        _dir   = _alert["direction"]
        _ep    = _alert.get("entry_price", 0) or 0
        _since = _alert.get("pending_since", int(time.time()))
        _age   = int(time.time()) - _since
        _cur   = app_state.prices.get(_sym, 0) or 0
        _alert_j15m = _alert.get("j15m", 50)

        _cur_j15m = _alert_j15m
        for _ps in app_state.pair_states:
            if _ps.get("symbol") == _sym:
                _cur_j15m = _ps.get("j15m", _alert_j15m)
                break

        _j15m_drift = abs(_cur_j15m - _alert_j15m)
        _p_drift    = abs(_cur - _ep) / _ep * 100 if _ep else 0

        # Expiry — data-derived thresholds (mirrors /api/state staleness)
        _expired = _age > 480 or _j15m_drift > 30 or _p_drift > 1.5
        if _expired:
            _exp_reason = (
                "EXPIRED_AGE"
                if _age > 480
                else "EXPIRED_J15M"
                if _j15m_drift > 30
                else "EXPIRED_PRICE")
            print(
                f"[PENDING EXPIRED] "
                f"{_sym} {_dir} "
                f"reason={_exp_reason}")
            asyncio.create_task(
                _log_alert_outcome(
                    _alert,
                    _exp_reason,
                    "HL",
                    pending_duration_seconds
                        =_age,
                ))
            _to_remove.append(_pk)
            continue

        if _cur <= 0 or not _ep:
            continue

        # Proj_pnl gate — current price must not be more than 0.1% adverse
        # from signal_price. Prevents opening into a price that has already
        # moved hard against the signal.
        _gate_ok = (
            (_dir == "LONG"  and _cur >= _ep * 0.999) or
            (_dir == "SHORT" and _cur <= _ep * 1.001))
        if _gate_ok:
            print(
                f"[CONFIRMED] {_sym} {_dir} price={_cur:.5f}"
                f" signal={_ep:.5f} — opening trade")
            _alert["be_confirm_price"] = _ep
            _margin = _alert.get("margin", MARGIN_PER_TRADE)
            trade, err = await _do_open_trade(
                _sym, _dir,
                _margin, _alert["leverage"],
                alert_data=_alert,
                exchange="HL",
            )
            if trade:
                print(
                    f"[CONFIRMED TRADE] {_sym} {_dir}"
                    f" entry={trade.get('entry_price')}"
                    f" pending_age={_age}s")
            elif err:
                print(f"[CONFIRMED] {_sym} {_dir} open failed: {err}")
            asyncio.create_task(
                _log_alert_outcome(
                    _alert,
                    "PRICE_GATE_PASSED",
                    "HL",
                    pending_duration_seconds
                        =_age,
                    confirm_price=_cur,
                ))
            _to_remove.append(_pk)

    for _pk in _to_remove:
        _pending_alerts.pop(_pk, None)

@asynccontextmanager
async def lifespan(app: FastAPI):
    global hl_client
    hl_client   = HLClient()
    log_startup_config()
    _load_state()
    if _pending_alerts:
        _pending_alerts.clear()
    print("[STARTUP] Pending alerts cleared on restart — direct open mode active")
    await _resolve_bot_identity("HL")
    _sentinel_mod.init("HL", _get_supabase)
    print("[SENTINEL] Phase 0 watchdog initialized -- observe-only")
    print("[SCHEMA] hl_trade_log analytics columns Ã¢ÂÂ run once in Supabase SQL editor if any are missing:")
    print("  ALTER TABLE hl_trade_log ADD COLUMN IF NOT EXISTS j15m_entry       float;")
    print("  ALTER TABLE hl_trade_log ADD COLUMN IF NOT EXISTS j1h_entry        float;")
    print("  ALTER TABLE hl_trade_log ADD COLUMN IF NOT EXISTS stoch_k_entry    float;")
    print("  ALTER TABLE hl_trade_log ADD COLUMN IF NOT EXISTS stoch_d_entry    float;")
    print("  ALTER TABLE hl_trade_log ADD COLUMN IF NOT EXISTS rsi_entry        float;")
    print("  ALTER TABLE hl_trade_log ADD COLUMN IF NOT EXISTS depth_pct_entry  float;")
    print("  ALTER TABLE hl_trade_log ADD COLUMN IF NOT EXISTS chg24h_entry     float;")
    print("  ALTER TABLE hl_trade_log ADD COLUMN IF NOT EXISTS session_opened   text;")
    print("  ALTER TABLE hl_trade_log ADD COLUMN IF NOT EXISTS mae_r            float;")
    print("  ALTER TABLE hl_trade_log ADD COLUMN IF NOT EXISTS mfe_r            float;")
    print("  ALTER TABLE hl_trade_log ADD COLUMN IF NOT EXISTS score           integer;")
    print("  ALTER TABLE hl_trade_log ADD COLUMN IF NOT EXISTS adx1h           float;")

    # Ã¢ÂÂÃ¢ÂÂ Mode log Ã¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂ
    if PAPER_MODE:
        print("[MODE] PAPER trading Ã¢ÂÂ auto-entry enabled")
    elif LIVE_MANUAL_ENTRY_ONLY:
        print("[MODE] LIVE trading Ã¢ÂÂ manual entry only via overlay. Auto-entry blocked.")
    else:
        print("[MODE] LIVE trading Ã¢ÂÂ AUTO-ENTRY ACTIVE. All signals will open live positions automatically. Confirm this is intentional.")

    scan_task  = asyncio.create_task(_supervised(_scan_loop,            "scan_loop"))
    price_task = asyncio.create_task(_supervised(_price_loop,           "price_loop"))
    exit_task  = asyncio.create_task(_supervised(_exit_monitor_loop,    "exit_monitor_loop"))
    state_task = asyncio.create_task(_supervised(_state_heartbeat_loop, "state_heartbeat_loop"))
    yield
    scan_task.cancel()
    price_task.cancel()
    exit_task.cancel()
    state_task.cancel()
    if _digest_task is not None and not _digest_task.done():
        _digest_task.cancel()
    await hl_client.close()


app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")


# Ã¢ÂÂÃ¢ÂÂ Routes Ã¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂ

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(request, "dashboard.html", {
        "paper_mode":    PAPER_MODE,
        "scan_interval": SCAN_INTERVAL_SECONDS,
        "margin_cap":    MARGIN_HARD_CAP,
        "cache_bust":    int(time.time()),
    }, headers={"Content-Type": "text/html; charset=utf-8"})


@app.get("/api/state")
async def get_state():
    _state = app_state.serialise()
    _flash_exp = _scanner_mod._btc_flash_block_until.get("long")
    _flash_active = bool(_flash_exp) and datetime.now(timezone.utc) < _flash_exp
    _state["btc_flash_active"]  = _flash_active
    _state["btc_flash_expires"] = _flash_exp.isoformat() if _flash_active else None
    _state["btc_j1h"]            = _scanner_mod._btc_j1h
    _state["regime_block_long"]  = False
    _state["regime_block_short"] = False
    _state["pair_cooldowns"]     = _scanner_mod.get_all_cooldowns()
    try:
        _fh_sb = _get_supabase()
        _fh_r  = _fh_sb.table("hl_scanner_state").select("fleet_halt").eq("id", 1).execute() if _fh_sb else None
        _state["fleet_halt"] = bool(_fh_r.data[0].get("fleet_halt", False)) if _fh_r and _fh_r.data else False
    except Exception:
        _state["fleet_halt"] = False
    return _state


@app.get("/api/account")
async def get_account():
    return {
        "margin_deployed": round(app_state.margin_deployed, 2),
        "cap":             MARGIN_HARD_CAP,
        "paper_mode":      PAPER_MODE,
        "slots_used":      app_state.slots_used,
    }


# Ã¢ÂÂÃ¢ÂÂ Per-pair overlay endpoint Ã¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂ

@app.get("/api/pair/{symbol}")
async def get_pair(symbol: str):
    ps = next((p for p in app_state.pair_states if p.get("symbol") == symbol), None)
    if ps is None:
        raise HTTPException(status_code=404, detail="pair not found")

    j15m    = ps.get("j15m",    50)
    j5m     = ps.get("j5m",     50)
    j1h     = ps.get("j1h",     50)
    rsi15m  = ps.get("rsi15m",  50)
    bid_pct = ps.get("bid_pct", 50)
    ask_pct = ps.get("ask_pct", 50)
    adx     = ps.get("adx1h",   0)
    atr     = ps.get("atr15m",  0)
    price   = app_state.prices.get(symbol, ps.get("price", 0))
    chg     = app_state.price_changes.get(symbol)

    stoch_k      = ps.get("stoch_k",      50)
    stoch_d      = ps.get("stoch_d",      50)
    stoch_k_prev = ps.get("stoch_k_prev", stoch_k)
    stoch_d_prev = ps.get("stoch_d_prev", stoch_d)
    stoch_k_fast      = ps.get("stoch_k_fast",      50)
    stoch_d_fast      = ps.get("stoch_d_fast",      50)
    stoch_k_prev_fast = ps.get("stoch_k_prev_fast", stoch_k_fast)
    stoch_d_prev_fast = ps.get("stoch_d_prev_fast", stoch_d_fast)
    # Live thresholds — always match settings overlay in real time
    _rsi_long_max    = _scanner_mod.RSI15M_LONG_MAX
    _rsi_short_min   = _scanner_mod.RSI15M_SHORT_MIN
    _j1h_long_max    = _scanner_mod.J1H_LONG_MAX
    _j1h_short_min   = _scanner_mod.J1H_SHORT_MIN
    _j1h_short_max   = _scanner_mod.J1H_SHORT_MAX
    _depth_gate      = _scanner_mod.DEPTH_GATE_PCT
    _j15m_short_gate = _scanner_mod.J15M_SHORT_GATE
    _j15m_long_gate  = _scanner_mod.J15M_LONG_GATE
    rsi_gate_long  = rsi15m < _rsi_long_max
    rsi_gate_short = rsi15m > _rsi_short_min
    j1h_gate_long  = j1h < _j1h_long_max
    j1h_gate_short = j1h > _j1h_short_min and j1h < _j1h_short_max
    sess_gate_long  = f"{symbol}_LONG_{get_session_name()}"  not in _session_halted
    sess_gate_short = f"{symbol}_SHORT_{get_session_name()}" not in _session_halted
    _cd_long  = get_cooldown_remaining(symbol, "LONG")
    _cd_short = get_cooldown_remaining(symbol, "SHORT")
    _lgcd_long  = max(0, int(_large_sl_cooldowns.get(f"{symbol}LONG",  0) - time.time()))
    _lgcd_short = max(0, int(_large_sl_cooldowns.get(f"{symbol}SHORT", 0) - time.time()))
    cool_gate_long  = _cd_long  == 0 and _lgcd_long  == 0
    cool_gate_short = _cd_short == 0 and _lgcd_short == 0
    gate_long  = [j15m < _j15m_long_gate,  j1h_gate_long,  rsi_gate_long,  bid_pct >= _depth_gate,  sess_gate_long,  cool_gate_long]
    gate_short = [j15m > _j15m_short_gate, j1h_gate_short, rsi_gate_short, ask_pct >= _depth_gate, sess_gate_short, cool_gate_short]
    score_long  = sum(gate_long)
    score_short = sum(gate_short)
    confluence_long  = j15m < 20 and j1h < 40
    confluence_short = j15m > 80 and j1h > 60

    # Active alert for this symbol (first match)
    alert = next((a for a in app_state.alerts if a.get("symbol") == symbol), None)

    # Alert staleness
    alert_state_val = None
    alert_age_sec   = None
    if alert:
        fired_at      = alert.get("fired_at", int(time.time()))
        alert_age_sec = int(time.time()) - fired_at
        entry_p       = alert.get("entry_price", price) or price or 1
        alert_j15m    = alert.get("j15m", j15m)
        j_drift       = abs(j15m - alert_j15m)
        p_drift       = abs(price - entry_p) / entry_p * 100 if entry_p else 0
        if   alert_age_sec > 480 or j_drift > 30 or p_drift > 1.5:
            alert_state_val = "STALE"
        elif alert_age_sec > 180 or j_drift > 15 or p_drift > 0.5:
            alert_state_val = "AGING"
        else:
            alert_state_val = "FRESH"

    # Open trades for this symbol
    in_trade_long  = None
    in_trade_short = None
    for k, t in app_state.open_trades.items():
        if t.get("symbol") != symbol:
            continue
        cur   = app_state.prices.get(symbol, t["entry_price"])
        entry = t["entry_price"]
        dir_  = t["direction"]
        size  = t.get("remaining_size", t.get("size", 0))
        mg    = t.get("margin", 0)
        lev   = t.get("leverage", 1)
        sl_d  = t.get("sl_dist", 0) or 0
        pnl   = (cur - entry) * size if dir_ == "LONG" else (entry - cur) * size
        dr    = mg * lev * (sl_d / entry) if entry else 0
        r_val = round(pnl / dr, 2) if dr else 0
        out   = {**t,
                 "current_price":  cur,
                 "unrealized_pnl": round(pnl, 2),
                 "r":              r_val,
                 "elapsed_s":      int(time.time()) - t.get("opened_at", int(time.time()))}
        if dir_ == "LONG":
            in_trade_long  = out
        else:
            in_trade_short = out

    # Last 5 closed trades for this symbol
    recent_alerts = [row for row in reversed(app_state.trade_log)
                     if row.get("symbol") == symbol][:5]

    return {
        "symbol":              symbol,
        "price":               price,
        "change_24h":          chg,
        "j15m":                j15m,
        "j5m":                 j5m,
        "j1h":                 j1h,
        "rsi15m":              rsi15m,
        "adx":                 adx,
        "atr":                 atr,
        "bid_pct":             bid_pct,
        "ask_pct":             ask_pct,
        "stoch_k":             stoch_k,
        "stoch_d":             stoch_d,
        "stoch_k_prev":        stoch_k_prev,
        "stoch_d_prev":        stoch_d_prev,
        "stoch_k_fast":         stoch_k_fast,
        "stoch_d_fast":         stoch_d_fast,
        "stoch_k_prev_fast":    stoch_k_prev_fast,
        "stoch_d_prev_fast":    stoch_d_prev_fast,
        "gate_long":           gate_long,
        "gate_short":          gate_short,
        "thresholds": {
            "j15m_short":    _j15m_short_gate,
            "j15m_long":     _j15m_long_gate,
            "j1h_long_max":  _j1h_long_max,
            "j1h_short_min": _j1h_short_min,
            "j1h_short_max": _j1h_short_max,
            "rsi_short_min": _rsi_short_min,
            "rsi_long_max":  _rsi_long_max,
            "depth_gate":    _depth_gate,
            "j5m_short_min": _scanner_mod.J5M_SHORT_MIN,
            "j5m_long_max":  _scanner_mod.J5M_LONG_MAX,
        },
        "score_long":          score_long,
        "score_short":         score_short,
        "alert":               alert,
        "alert_state":         alert_state_val,
        "alert_age_seconds":   alert_age_sec,
        "in_trade_long":       in_trade_long,
        "in_trade_short":      in_trade_short,
        "last_scan_summaries": app_state.scan_snapshots.get(symbol, []),
        "recent_alerts":       recent_alerts,
        "confluence_long":     confluence_long,
        "confluence_short":    confluence_short,
        "trend":               ps.get("trend"),
        "session_halted_long":  f"{symbol}_LONG_{get_session_name()}"  in _session_halted,
        "session_halted_short": f"{symbol}_SHORT_{get_session_name()}" in _session_halted,
        "large_sl_cooldown_long_remaining":  (lambda v: v or None)(max(0, int(_large_sl_cooldowns.get(f"{symbol}LONG",  0) - time.time()))),
        "large_sl_cooldown_short_remaining": (lambda v: v or None)(max(0, int(_large_sl_cooldowns.get(f"{symbol}SHORT", 0) - time.time()))),
        "cooldown_long":                     _cd_long,
        "cooldown_short":                    _cd_short,
        "session_halt_reason":  "2 SL hits this session Ã¢ÂÂ resumes at next session open" if (
            f"{symbol}_LONG_{get_session_name()}"  in _session_halted or
            f"{symbol}_SHORT_{get_session_name()}" in _session_halted
        ) else None,
    }


# Ã¢ÂÂÃ¢ÂÂ Trade open Ã¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂ

class OpenTradeRequest(BaseModel):
    symbol:      str
    direction:   str
    exchange:    str = "HL"
    margin_usdc: float = MARGIN_PER_TRADE
    leverage:    int   = 5
    sl_price:    Optional[float] = None


@app.post("/api/trade/open")
async def open_trade(req: OpenTradeRequest):
    # Session halt gate (also applies to manual overlay entry)
    _s_gate = f"{req.symbol}_{req.direction}_{get_session_name()}"
    if _s_gate in _session_halted:
        raise HTTPException(status_code=400,
            detail=f"{req.symbol} {req.direction} halted for {get_session_name()} session Ã¢ÂÂ 2 SL hits. Resumes at next session open.")
    # Large SL cooldown gate
    _lcd_k = f"{req.symbol}{req.direction}"
    if _lcd_k in _large_sl_cooldowns and _large_sl_cooldowns[_lcd_k] > time.time():
        _lcd_rem = max(0, int(_large_sl_cooldowns[_lcd_k] - time.time()))
        _lcd_m, _lcd_s = divmod(_lcd_rem, 60)
        raise HTTPException(status_code=400,
            detail=f"{req.symbol} {req.direction} Ã¢ÂÂ 90 min cooldown active, {_lcd_m}m{_lcd_s}s remaining. Large SL hit.")
    # Manual entry via overlay Ã¢ÂÂ always permitted regardless of LIVE_MANUAL_ENTRY_ONLY setting.
    alert_data = None
    for a in app_state.alerts:
        if a["symbol"] == req.symbol and a["direction"] == req.direction:
            alert_data = a
            break

    if req.sl_price and alert_data:
        alert_data = {**alert_data, "sl_price": req.sl_price}
    elif req.sl_price:
        alert_data = {"sl_price": req.sl_price}

    trade, err = await _do_open_trade(
        req.symbol, req.direction,
        req.margin_usdc, req.leverage,
        alert_data, req.exchange,
    )
    if err:
        code = 400 if err in ("cap_reached", "already_open", "circuit_breaker", "daily_limit") else 500
        if err == "daily_limit":
            detail = (f"Daily loss limit reached Ã¢ÂÂ ${daily_pnl:.2f} of ${DAILY_LOSS_LIMIT:.0f}."
                      " Tap Reset Session to resume trading.")
        elif err == "circuit_breaker":
            detail = (f"Circuit breaker active Ã¢ÂÂ {consecutive_losses} consecutive losses."
                      " Tap Reset Session to resume.")
        elif err == "cap_reached":
            detail = (f"Margin cap reached Ã¢ÂÂ ${app_state.margin_deployed:.0f} of ${MARGIN_HARD_CAP:.0f} deployed."
                      " Close a position to continue.")
        else:
            detail = err
        raise HTTPException(status_code=code, detail=detail)
    return {"status": "ok", "trade": trade}


# Ã¢ÂÂÃ¢ÂÂ Trade close Ã¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂ

class CloseTradeRequest(BaseModel):
    symbol:    str
    direction: str


@app.post("/api/trade/close")
async def close_trade(req: CloseTradeRequest):
    key   = app_state.trade_key(req.symbol, req.direction)
    trade = app_state.open_trades.get(key)
    if not trade:
        raise HTTPException(status_code=404, detail=f"No open trade for {key}")

    exchange = trade.get("exchange", "HL")
    _client  = mexc_client if exchange == "MEXC" else hl_client
    result   = await _client.close_position(req.symbol, req.direction, trade["size"])
    if result.get("status") != "ok":
        raise HTTPException(status_code=500, detail=result.get("msg", "close failed"))

    close_price = result.get("close_price") or app_state.prices.get(req.symbol)
    if not close_price or close_price <= 0:
        print(f"[CLOSE GUARD] {req.symbol} -- price feed returned {close_price!r}, attempting fresh fetch")
        try:
            close_price = await mexc_client.get_price(req.symbol)
            if not close_price or close_price <= 0:
                await asyncio.sleep(2)
                close_price = await mexc_client.get_price(req.symbol)
        except Exception as _pe:
            print(f"[CLOSE GUARD] {req.symbol} fresh price fetch failed: {_pe}")
            close_price = None
    if not close_price or close_price <= 0:
        raise HTTPException(
            status_code=503,
            detail="Price unavailable -- try again in a few seconds"
        )
    entry       = trade["entry_price"]
    remaining   = trade.get("remaining_size", trade["size"])

    pnl = (close_price - entry) * remaining if req.direction == "LONG" else (entry - close_price) * remaining

    sl_dist = trade.get("sl_dist") or 0
    lev     = trade.get("leverage", 1)
    margin  = trade.get("margin", MARGIN_PER_TRADE)
    dollar_risk = margin * lev * (sl_dist / entry) if entry else 0
    r = round(pnl / dollar_risk, 2) if dollar_risk else 0.0

    _append_trade_log(trade, close_price, "MANUAL", pnl, r)
    _update_daily_pnl(pnl)
    _on_trade_close("MANUAL")
    asyncio.create_task(_write_adverse_shadow_row(key, trade, "MANUAL", pnl, r))
    asyncio.create_task(_write_sign_shadow_rows(key, trade, "MANUAL", pnl))
    asyncio.create_task(_write_signal_shadow_row(key, trade, "MANUAL", pnl, r))

    app_state.margin_deployed = max(0.0, app_state.margin_deployed - trade["margin"])
    closed = {**trade, "close_price": close_price, "final_pnl": round(pnl, 2)}
    del app_state.open_trades[key]
    _retire_alert(req.symbol, req.direction)

    _save_state()
    print(f"[TRADE CLOSE] {req.symbol} {req.direction} MANUAL pnl=${pnl:.2f} r={r:+.2f}")
    if TELEGRAM_ENABLED:
        def _manual_tg(s=req.symbol, d=req.direction, ep=close_price, p=pnl):
            sl_lbl = "S" if d == "SHORT" else "L"
            _tg_post("\U0001F535 " + s + " " + sl_lbl + " \u00B7 closed (MANUAL) at " + _fmt_p(ep)
                     + "\n" + ("+" if p >= 0 else "-") + "$" + f"{abs(p):.2f}")
        threading.Thread(target=_manual_tg, daemon=True).start()
    return {"status": "ok", "closed": closed}




@app.get("/api/live-brief/{symbol}/{direction}")
async def live_brief(symbol: str, direction: str):
    """Pre-flight data for the OPEN LIVE overlay. In-memory only except pair_stats."""
    sess_key = f"{symbol}_{direction}_{get_session_name()}"
    sess_halted = sess_key in _session_halted
    large_sl_cd_key = f"{symbol}{direction}"
    large_sl_cd_rem = max(0, int(_large_sl_cooldowns.get(large_sl_cd_key, 0) - time.time()))
    margin_cap = app_state.margin_deployed + MARGIN_PER_TRADE > MARGIN_HARD_CAP

    gate_status = {
        "session_halted":                     sess_halted,
        "large_sl_cooldown_remaining_seconds": large_sl_cd_rem,
        "circuit_breaker_active":             circuit_breaker_active,
        "daily_halted":                       trading_halted_today,
        "margin_cap_reached":                 margin_cap,
    }

    ps = next((p for p in app_state.pair_states if p.get("symbol") == symbol), None)
    depth_pct = None
    j5m_live  = None
    if ps:
        depth_pct = ps.get("bid_pct") if direction == "LONG" else ps.get("ask_pct")
        j5m_live  = ps.get("j5m")

    btc_j1h = _scanner_mod._btc_j1h
    if btc_j1h > 80.0:
        btc_regime = "LONG_BLOCKED"
    elif btc_j1h < 20.0:
        btc_regime = "SHORT_BLOCKED"
    elif 40.0 <= btc_j1h <= 60.0:
        btc_regime = "NEUTRAL_BLOCK"
    else:
        btc_regime = "CLEAR"

    std_cd = get_cooldown_remaining(symbol, direction)

    informational_only = {
        "depth_pct":                          depth_pct,
        "btc_regime":                         btc_regime,
        "standard_cooldown_remaining_seconds": std_cd,
        "j5m_live":                           j5m_live,
        "j5m_short_min":                      _scanner_mod.J5M_SHORT_MIN,
        "j5m_long_max":                       _scanner_mod.J5M_LONG_MAX,
        "depth_short_min":                    _scanner_mod.DEPTH_SHORT_MIN,
        "depth_long_min":                     _scanner_mod.DEPTH_LONG_MIN,
    }

    daily_out = {
        "pnl":    daily_pnl,
        "limit":  DAILY_LOSS_LIMIT,
        "halted": trading_halted_today,
    }

    cb_out = {
        "active":             circuit_breaker_active,
        "consecutive_losses": consecutive_losses,
        "stop_at":            CONSECUTIVE_LOSS_STOP,
    }

    open_positions = []
    for t in app_state.open_trades.values():
        cur = app_state.prices.get(t["symbol"], t["entry_price"])
        sz  = t.get("remaining_size", t.get("size", 0))
        raw = (cur - t["entry_price"]) * sz if t["direction"] == "LONG" \
              else (t["entry_price"] - cur) * sz
        open_positions.append({
            "symbol":         t.get("symbol"),
            "direction":      t.get("direction"),
            "unrealized_pnl": round(raw, 2),
        })

    alert = next(
        (a for a in app_state.alerts
         if a.get("symbol") == symbol and a.get("direction") == direction),
        None,
    )
    alert_data = None
    if alert:
        alert_data = {
            "entry_price": alert.get("entry_price"),
            "sl_price":    alert.get("sl_price"),
            "tp1_price":   alert.get("tp1_price"),
            "score":       alert.get("score"),
            "adx1h":       alert.get("adx1h"),
            "j15m":        alert.get("j15m"),
            "j1h":         alert.get("j1h"),
            "stoch_k":     alert.get("stoch_k"),
            "stoch_d":     alert.get("stoch_d"),
            "session":     alert.get("session", ""),
            "leverage":    alert.get("leverage", 5),
            "tier":        alert.get("tier"),
            "fired_at":    alert.get("fired_at"),
        }

    pair_stats = None
    sb = _get_supabase()
    if sb:
        try:
            cutoff_7d = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
            rows_all = (
                sb.table("hl_trade_log")
                .select("pnl_dollars,mfe_r,mae_r,open_time")
                .eq("pair", symbol)
                .eq("direction", direction)
                .execute()
                .data or []
            )

            def _ps_stats(rows):
                if not rows:
                    return {"wr": None, "trades": 0, "wins": 0, "losses": 0,
                            "avg_best_peak": None, "avg_worst_dip": None}
                wins  = sum(1 for r in rows if (r.get("pnl_dollars") or 0) > 0)
                mfe_v = [r["mfe_r"] for r in rows if r.get("mfe_r") is not None]
                mae_v = [r["mae_r"] for r in rows if r.get("mae_r") is not None]
                return {
                    "wr":            round(wins / len(rows) * 100, 1),
                    "trades":        len(rows),
                    "wins":          wins,
                    "losses":        len(rows) - wins,
                    "avg_best_peak": round(sum(mfe_v) / len(mfe_v), 2) if mfe_v else None,
                    "avg_worst_dip": round(sum(mae_v) / len(mae_v), 2) if mae_v else None,
                }

            rows_7d = [r for r in rows_all if (r.get("open_time") or "") >= cutoff_7d]
            at = _ps_stats(rows_all)
            d7 = _ps_stats(rows_7d)
            pair_stats = {
                "7d_wr":                 d7["wr"],
                "7d_trades":             d7["trades"],
                "7d_wins":               d7["wins"],
                "7d_losses":             d7["losses"],
                "7d_avg_best_peak":      d7["avg_best_peak"],
                "7d_avg_worst_dip":      d7["avg_worst_dip"],
                "alltime_wr":            at["wr"],
                "alltime_trades":        at["trades"],
                "alltime_wins":          at["wins"],
                "alltime_losses":        at["losses"],
                "alltime_avg_best_peak": at["avg_best_peak"],
                "alltime_avg_worst_dip": at["avg_worst_dip"],
            }
        except Exception as _ps_e:
            print(f"[LIVE BRIEF] pair_stats error: {_ps_e}")

    return {
        "symbol":             symbol,
        "direction":          direction,
        "gate_status":        gate_status,
        "informational_only": informational_only,
        "daily":              daily_out,
        "circuit_breaker":    cb_out,
        "open_positions":     open_positions,
        "alert_data":         alert_data,
        "pair_stats":         pair_stats,
    }

# Ã¢ÂÂÃ¢ÂÂ Circuit breaker Ã¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂ

@app.post("/api/circuit-breaker/reset")
async def reset_circuit_breaker():
    global consecutive_losses, circuit_breaker_active
    circuit_breaker_active = False
    consecutive_losses     = 0
    print("[CIRCUIT BREAKER RESET] manual reset")
    return {"status": "ok", "circuit_breaker_active": False, "consecutive_losses": 0}


@app.post("/api/reset-session")
async def reset_session():
    global daily_pnl, trading_halted_today, consecutive_losses, circuit_breaker_active
    daily_pnl              = 0.0
    trading_halted_today   = False
    consecutive_losses     = 0
    circuit_breaker_active = False
    _session_sl_counts.clear()
    _session_halted.clear()
    _large_sl_cooldowns.clear()
    clear_all_scanner_state()
    _save_state()
    print("[SESSION RESET] manual reset Ã¢ÂÂ daily P&L, cooldowns, circuit breaker cleared Ã¢ÂÂ state persisted")
    return {"reset": True, "message": "Session reset Ã¢ÂÂ daily P&L, cooldowns and circuit breaker cleared"}


# -- Runtime settings --------------------------------------------------------

@app.get("/api/settings")
async def get_settings():
    """Return live values of all runtime-adjustable scanner settings."""
    return {
        "paper_mode":
            PAPER_MODE,
        "telegram_enabled":
            TELEGRAM_ENABLED,
        "depth_gate_pct":
            _scanner_mod.DEPTH_GATE_PCT,
        "adx_min_long":
            _scanner_mod.ADX_MIN_LONG,
        "j15m_short_gate":
            _scanner_mod.J15M_SHORT_GATE,
        "j15m_long_gate":
            _scanner_mod.J15M_LONG_GATE,
        "j1h_short_min":
            _scanner_mod.J1H_SHORT_MIN,
        "j1h_short_max":
            _scanner_mod.J1H_SHORT_MAX,
        "j1h_long_max":
            _scanner_mod.J1H_LONG_MAX,
        "j1h_long_min":
            _scanner_mod.J1H_LONG_MIN,
        "atr_sl_multiplier":
            _scanner_mod.ATR_SL_MULTIPLIER,
        "tp1_close_pct":
            _scanner_mod.TP1_CLOSE_PCT,
        "tp2_r":
            _scanner_mod.TP2_R,
        "margin_per_trade":
            MARGIN_PER_TRADE,
        "daily_loss_limit":
            DAILY_LOSS_LIMIT,
        "consecutive_loss_stop":
            CONSECUTIVE_LOSS_STOP,
        "kill_cooldown_seconds":
            _scanner_mod.PAIR_COOLDOWN_SECONDS,
        "kill_pct_floor":
            _scanner_mod.KILL_PCT_FLOOR,
        "leverage":
            _scanner_mod.LEVERAGE,
        "margin_hard_cap":
            MARGIN_HARD_CAP,
        "trail_atr_multiplier":
            TRAIL_ATR_MULTIPLIER,
        "j5m_short_min":
            _scanner_mod.J5M_SHORT_MIN,
        "j5m_long_max":
            _scanner_mod.J5M_LONG_MAX,
        "depth_short_min":
            _scanner_mod.DEPTH_SHORT_MIN,
        "depth_long_min":
            _scanner_mod.DEPTH_LONG_MIN,
    }


@app.post("/api/settings")
async def post_settings(request: Request):
    """Partial-update runtime settings. Only fields present in the body are changed."""
    global PAPER_MODE, TELEGRAM_ENABLED, \
           DAILY_LOSS_LIMIT, MARGIN_PER_TRADE, \
           CONSECUTIVE_LOSS_STOP, MARGIN_HARD_CAP, TRAIL_ATR_MULTIPLIER
    body = await request.json()
    if "paper_mode" in body:
        PAPER_MODE = bool(body["paper_mode"])
        _scanner_mod.PAPER_MODE = PAPER_MODE
    if "telegram_enabled" in body:
        TELEGRAM_ENABLED = bool(
            body["telegram_enabled"])
    if "depth_gate_pct" in body:
        _scanner_mod.DEPTH_GATE_PCT = float(
            body["depth_gate_pct"])
    if "adx_min_long" in body:
        _scanner_mod.ADX_MIN_LONG = float(
            body["adx_min_long"])
    if "j15m_short_gate" in body:
        _scanner_mod.J15M_SHORT_GATE = float(
            body["j15m_short_gate"])
    if "j15m_long_gate" in body:
        _scanner_mod.J15M_LONG_GATE = float(
            body["j15m_long_gate"])
    if "j1h_short_min" in body:
        _scanner_mod.J1H_SHORT_MIN = float(
            body["j1h_short_min"])
    if "j1h_short_max" in body:
        _scanner_mod.J1H_SHORT_MAX = float(
            body["j1h_short_max"])
    if "j1h_long_max" in body:
        _scanner_mod.J1H_LONG_MAX = float(
            body["j1h_long_max"])
    if "j1h_long_min" in body:
        _scanner_mod.J1H_LONG_MIN = float(
            body["j1h_long_min"])
    if "atr_sl_multiplier" in body:
        _scanner_mod.ATR_SL_MULTIPLIER = float(
            body["atr_sl_multiplier"])
    if "tp1_close_pct" in body:
        _scanner_mod.TP1_CLOSE_PCT = float(
            body["tp1_close_pct"])
    if "tp2_r" in body:
        _scanner_mod.TP2_R = float(
            body["tp2_r"])
    if "margin_per_trade" in body:
        MARGIN_PER_TRADE = float(
            body["margin_per_trade"])
        _scanner_mod.MARGIN_PER_TRADE = \
            MARGIN_PER_TRADE
    if "daily_loss_limit" in body:
        DAILY_LOSS_LIMIT = float(
            body["daily_loss_limit"])
    if "consecutive_loss_stop" in body:
        CONSECUTIVE_LOSS_STOP = int(
            body["consecutive_loss_stop"])
        _scanner_mod.CONSECUTIVE_LOSS_STOP = \
            CONSECUTIVE_LOSS_STOP
    if "kill_cooldown_seconds" in body:
        _scanner_mod.PAIR_COOLDOWN_SECONDS = int(
            body["kill_cooldown_seconds"])
    if "kill_pct_floor" in body:
        _scanner_mod.KILL_PCT_FLOOR = float(
            body["kill_pct_floor"])
    if "leverage" in body:
        _scanner_mod.LEVERAGE = int(
            body["leverage"])
    if "margin_hard_cap" in body:
        MARGIN_HARD_CAP = float(
            body["margin_hard_cap"])
    if "trail_atr_multiplier" in body:
        TRAIL_ATR_MULTIPLIER = float(
            body["trail_atr_multiplier"])
    if "j5m_short_min" in body:
        _scanner_mod.J5M_SHORT_MIN = float(body["j5m_short_min"])
    if "j5m_long_max" in body:
        _scanner_mod.J5M_LONG_MAX = float(body["j5m_long_max"])
    if "depth_short_min" in body:
        _scanner_mod.DEPTH_SHORT_MIN = float(body["depth_short_min"])
    if "depth_long_min" in body:
        _scanner_mod.DEPTH_LONG_MIN = float(body["depth_long_min"])

    # Persist ALL settings to Supabase
    # NOTE: columns require migration if not yet in schema.
    _sb = _get_supabase()
    if _sb is not None:
        try:
            _settings_payload = {
                "paper_mode":
                    PAPER_MODE,
                "telegram_enabled":
                    TELEGRAM_ENABLED,
                "depth_gate_pct":
                    _scanner_mod.DEPTH_GATE_PCT,
                "adx_min_long":
                    _scanner_mod.ADX_MIN_LONG,
                "j15m_short_gate":
                    _scanner_mod.J15M_SHORT_GATE,
                "j15m_long_gate":
                    _scanner_mod.J15M_LONG_GATE,
                "j1h_short_min":
                    _scanner_mod.J1H_SHORT_MIN,
                "j1h_short_max":
                    _scanner_mod.J1H_SHORT_MAX,
                "j1h_long_max":
                    _scanner_mod.J1H_LONG_MAX,
                "j1h_long_min":
                    _scanner_mod.J1H_LONG_MIN,
                "atr_sl_multiplier":
                    _scanner_mod.ATR_SL_MULTIPLIER,
                "tp1_close_pct":
                    _scanner_mod.TP1_CLOSE_PCT,
                "tp2_r":
                    _scanner_mod.TP2_R,
                "margin_per_trade":
                    MARGIN_PER_TRADE,
                "daily_loss_limit":
                    DAILY_LOSS_LIMIT,
                "consecutive_loss_stop":
                    CONSECUTIVE_LOSS_STOP,
                "kill_cooldown_seconds":
                    _scanner_mod.PAIR_COOLDOWN_SECONDS,
                "kill_pct_floor":
                    _scanner_mod.KILL_PCT_FLOOR,
                "leverage":
                    _scanner_mod.LEVERAGE,
                "margin_hard_cap":
                    MARGIN_HARD_CAP,
                "trail_atr_multiplier":
                    TRAIL_ATR_MULTIPLIER,
                "j5m_short_min":
                    _scanner_mod.J5M_SHORT_MIN,
                "j5m_long_max":
                    _scanner_mod.J5M_LONG_MAX,
                "depth_short_min":
                    _scanner_mod.DEPTH_SHORT_MIN,
                "depth_long_min":
                    _scanner_mod.DEPTH_LONG_MIN,
            }
            _settings_payload["id"] = 1
            _sb.table("hl_scanner_state")\
               .upsert(_settings_payload)\
               .execute()
        except Exception as _e:
            print(f"[SETTINGS] Supabase "
                  f"save failed: {_e}")
            import traceback
            traceback.print_exc()

    return await get_settings()


# Ã¢ÂÂÃ¢ÂÂ Bot identity Ã¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂ

@app.get("/api/bot-identity")
async def get_bot_identity():
      """Return current bot identity and whether it has been committed to Supabase."""
      return {"bot_instance_id": BOT_INSTANCE_ID, "committed": _BOT_IDENTITY_COMMITTED}


@app.post("/api/bot-identity/set")
async def set_bot_identity(request: Request):
      """Commit or update the bot instance name.  No restart required."""
      global BOT_INSTANCE_ID, _BOT_IDENTITY_COMMITTED
      body = await request.json()
      name = (body.get("name") or "").strip()
      if not name:
          raise HTTPException(status_code=400, detail="name must be a non-empty string")
      if ":" in name:
          raise HTTPException(status_code=400, detail="name must not contain ':' (used as lock key delimiter)")
      sb = _get_supabase()
      if sb:
          try:
              sb.table("bot_identity").upsert({
                  "exchange":        "HL",
                  "bot_instance_id": name,
                  "set_at":          datetime.now(timezone.utc).isoformat(),
              }).execute()
          except Exception as _e:
              print(f"[BOT IDENTITY] Supabase upsert failed: {_e}")
      BOT_INSTANCE_ID = name
      _BOT_IDENTITY_COMMITTED = True
      print(f"[BOT IDENTITY] Updated to: {name} (committed)")
      return {"bot_instance_id": BOT_INSTANCE_ID, "committed": _BOT_IDENTITY_COMMITTED}


# Ã¢ÂÂÃ¢ÂÂ Daily reset Ã¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂ

@app.post("/api/reset-day")
async def reset_day():
    global daily_pnl, trading_halted_today
    daily_pnl            = 0.0
    trading_halted_today = False
    print("[DAILY RESET] manual reset")
    return {"status": "ok"}


# Ã¢ÂÂÃ¢ÂÂ Trade log Ã¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂÃ¢ÂÂ

@app.get("/api/tradelog")
async def get_tradelog():
    return app_state.trade_log


@app.get("/api/hl-balance")
def hl_balance():
    wallet = os.environ.get("HL_WALLET_ADDRESS", "")
    if not wallet:
        return {"error": "HL_WALLET_ADDRESS not configured"}
    short_wallet = wallet[:6] + "..." + wallet[-4:]
    try:
        resp = requests.post(
            "https://api.hyperliquid.xyz/info",
            json={"type": "clearinghouseState", "user": wallet},
            timeout=10,
        )
        if resp.status_code != 200:
            return {"error": f"HL API error: {resp.status_code}"}
        data = resp.json()
        import json as _json; print("[HL BALANCE RAW]", _json.dumps(data)[:500])
        ms             = data.get("marginSummary", {})
        equity         = float(ms.get("accountValue",    0) or 0)
        available      = float(data.get("withdrawable",  0) or 0)
        margin_used    = float(ms.get("totalMarginUsed", 0) or 0)
        unrealized_pnl = float(ms.get("totalRawUpl",    0) or 0)
        positions      = data.get("assetPositions", [])
        open_positions = sum(1 for p in positions if p.get("position", {}).get("szi", "0") != "0")
        print(f"[HL BALANCE] fetched for wallet {short_wallet} equity={equity}")
        return {
            "equity":         equity,
            "available":      available,
            "margin_used":    margin_used,
            "unrealized_pnl": unrealized_pnl,
            "open_positions": open_positions,
            "wallet":         short_wallet,
            "fetched_at":     datetime.now(timezone.utc).isoformat(),
        }
    except Exception as e:
        return {"error": f"HL API request failed: {str(e)}"}


@app.get("/api/tradelog/csv")
async def download_tradelog_csv():
    fieldnames = [
        "timestamp_opened", "timestamp_closed", "symbol", "direction",
        "score", "adx1h", "tier", "entry_price", "sl_price",
        "tp1_price", "tp2_price", "exit_price", "exit_reason",
        "pnl_usd", "r_value", "duration_seconds", "exchange", "paper",
    ]
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    for row in app_state.trade_log:
        writer.writerow({k: row.get(k, "") for k in fieldnames})
    today   = datetime.now(timezone.utc).strftime("%Y%m%d")
    content = output.getvalue()
    return StreamingResponse(
        iter([content]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=trade_log_{today}.csv"},
    )


class DismissAlertRequest(BaseModel):
    symbol:    str
    direction: str

@app.post("/api/alert/dismiss")
async def dismiss_alert(req: DismissAlertRequest):
    _retire_alert(req.symbol, req.direction)
    return {"status": "ok"}


@app.delete("/api/alerts")
async def clear_alerts_endpoint():
    app_state.alerts.clear()
    clear_all_scanner_state()
    print("[CLEAR ALERTS] alerts cleared, consecutive-scan state reset")
    return {"status": "ok"}


@app.delete("/api/tradelog")
async def clear_tradelog(
    from_ts: Optional[int] = Query(None, description="Unix epoch seconds Ã¢ÂÂ start of date range (inclusive)"),
    to_ts:   Optional[int] = Query(None, description="Unix epoch seconds Ã¢ÂÂ end of date range (inclusive)"),
):
    """With from_ts+to_ts: deletes only log entries in that range (no state reset).
    Without params: full clear Ã¢ÂÂ force-closes open trades, resets all state."""

    if from_ts is not None and to_ts is not None:
        # Ã¢ÂÂÃ¢ÂÂ Date-ranged delete Ã¢ÂÂ only remove matching closed log entries Ã¢ÂÂÃ¢ÂÂ
        removed = [
            r for r in app_state.trade_log
            if from_ts <= (r.get("timestamp_closed") or 0) <= to_ts
        ]
        app_state.trade_log = [
            r for r in app_state.trade_log
            if not (from_ts <= (r.get("timestamp_closed") or 0) <= to_ts)
        ]
        # Supabase date-range delete
        sb = _get_supabase()
        if sb is not None:
            try:
                from_iso = datetime.fromtimestamp(from_ts, tz=timezone.utc).isoformat()
                to_iso   = datetime.fromtimestamp(to_ts,   tz=timezone.utc).isoformat()
                sb.table("hl_trade_log").delete() \
                    .gte("close_time", from_iso) \
                    .lte("close_time", to_iso) \
                    .execute()
            except Exception as _e:
                print(f"[CLEAR] Supabase date-range delete error: {_e}")
        print(f"[CLEAR] {len(removed)} log entries removed for range {from_ts}Ã¢ÂÂ{to_ts}")
        return {"status": "ok", "entries_removed": len(removed)}

    # Ã¢ÂÂÃ¢ÂÂ Full clear (no date params) Ã¢ÂÂ existing behaviour unchanged Ã¢ÂÂÃ¢ÂÂ
    global consecutive_losses, circuit_breaker_active, daily_pnl, trading_halted_today

    count = len(app_state.open_trades)
    for key, trade in list(app_state.open_trades.items()):
        sym   = trade["symbol"]
        ep = app_state.prices.get(sym) or 0
        if not ep or ep <= 0:
            try:
                ep = await mexc_client.get_price(sym)
                if not ep or ep <= 0:
                    await asyncio.sleep(2)
                    ep = await mexc_client.get_price(sym)
            except Exception:
                ep = None
        if not ep or ep <= 0:
            print(f"[FORCE CLOSE] {sym} -- price unavailable, trade cleared without log entry")
        else:
            entry = trade["entry_price"]
            rem   = trade.get("remaining_size", trade["size"])
            pnl   = (ep - entry) * rem if trade["direction"] == "LONG" else (entry - ep) * rem
            _append_trade_log(trade, ep, "FORCE_CLOSE", pnl, 0.0)
        app_state.margin_deployed = max(0.0, app_state.margin_deployed - trade["margin"])

    consecutive_losses     = 0
    circuit_breaker_active = False
    daily_pnl              = 0.0
    trading_halted_today   = False
    app_state.trade_log.clear()
    app_state.open_trades.clear()
    app_state.margin_deployed = 0.0
    app_state.alerts.clear()
    clear_all_scanner_state()

    print(f"[CLEAR] {count} trades force closed, state reset")
    return {"status": "ok", "trades_force_closed": count}


