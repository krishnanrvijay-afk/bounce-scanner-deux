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
from typing import Optional

from dotenv import load_dotenv
load_dotenv()

_EDT = timezone(timedelta(hours=-4))
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
)
from supabase import create_client, Client
from hl_client import HLClient
from mexc_client import MexcClient
from scanner import (
    run_full_scan, scan_pair_state, get_pending,
    get_scan_count, set_close_cooldown, clear_cooldown,
    get_cooldown_remaining, clear_all_scanner_state, log_startup_config,
    compute_market_health, get_session_name,
)
import scanner as _scanner_mod  # direct access to _cooldowns dict for persistence

# ── Telegram config ────────────────────────────────────────────
TELEGRAM_BOT_TOKEN  = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID    = int(os.environ.get("TELEGRAM_CHAT_ID", "0") or "0")
TELEGRAM_ENABLED    = os.environ.get("TELEGRAM_ENABLED", "true").lower() == "true"
_pending_reminders: dict = {}
_stale_tg_sent: set[str] = set()  # symbols for which stale-price TG alert was already sent
_session_sl_counts: dict[str, int]   = {}    # "SYMBOL_DIRECTION_SESSION" -> SL count
_session_halted:    set[str]         = set() # "SYMBOL_DIRECTION_SESSION" halted for session
_large_sl_cooldowns: dict[str, float] = {}   # "SYMBOLDIR" -> expiry ts for 90-min cooldowns
_prev_session:      str              = ""

# ── Global safety state ────────────────────────────────────────────────────────
consecutive_losses:     int   = 0
circuit_breaker_active: bool  = False
daily_pnl:              float = 0.0
trading_halted_today:   bool  = False
_last_midnight_day:     int   = datetime.now(timezone.utc).day


# ── App state ─────────────────────────────────────────────────────────────────

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
            size    = t.get("size", 0)
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
            pair_states_out[i] = {
                **ps,
                "in_trade":           in_trade,
                "cooldown_short":     cd_s,
                "cooldown_long":      cd_l,
                "session_halted_long":  f"{sym}_LONG_{get_session_name()}"  in _session_halted,
                "session_halted_short": f"{sym}_SHORT_{get_session_name()}" in _session_halted,
                "large_sl_cd_long":     (lambda v: v or None)(max(0, int(_large_sl_cooldowns.get(f"{sym}LONG",  0) - time.time()))),
                "large_sl_cd_short":    (lambda v: v or None)(max(0, int(_large_sl_cooldowns.get(f"{sym}SHORT", 0) - time.time()))),
            }

        return {
            "pair_states":    pair_states_out,
            "session":        get_session_name(),
            "alerts":         self.alerts,
            "pending_alerts": get_pending(),
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


# ── Helpers ───────────────────────────────────────────────────────────────────

def _retire_alert(symbol: str, direction: str):
    app_state.alerts = [
        a for a in app_state.alerts
        if not (a["symbol"] == symbol and a["direction"] == direction)
    ]


# ── Persistence ────────────────────────────────────────────────────────────────

# ── Supabase client ────────────────────────────────────────────────────────────

_supabase: Optional[Client] = None


def _get_supabase() -> Optional[Client]:
    global _supabase
    if _supabase is None:
        if SUPABASE_URL and SUPABASE_KEY:
            try:
                _supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
            except Exception as _e:
                print(f"[PERSIST] Supabase client init error: {_e}")
        else:
            print("[PERSIST] SUPABASE_URL/KEY not set — persistence disabled")
    return _supabase


def _save_state():
    """Upsert full scanner state to Supabase scanner_state table (row id=1)."""
    sb = _get_supabase()
    if sb is None:
        return
    try:
        data = {
            "id":                     1,
            "saved_date":             datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "open_trades":            app_state.open_trades,
            "margin_deployed":        app_state.margin_deployed,
            "daily_pnl":              daily_pnl,
            "trading_halted_today":   trading_halted_today,
            "consecutive_losses":     consecutive_losses,
            "circuit_breaker_active": circuit_breaker_active,
            "cooldowns":              dict(_scanner_mod._cooldowns),
            "updated_at":             datetime.now(timezone.utc).isoformat(),
        }
        sb.table("scanner_state").upsert(data).execute()
    except Exception as _e:
        print(f"[PERSIST] save error: {_e}")


def _load_state():
    """On startup: restore all state from Supabase."""
    global daily_pnl, trading_halted_today, consecutive_losses, circuit_breaker_active
    sb = _get_supabase()
    if sb is None:
        print("[RESTORE] No Supabase client — starting fresh")
        return
    try:
        # ── Trade log → in-memory list ─────────────────────────────────────────
        log_rows = sb.table("trade_log").select("*").order("created_at").limit(1000).execute()
        if log_rows.data:
            for row in log_rows.data:
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
                    "paper":            True,
                })
            print(f"[RESTORE] trade log: {len(log_rows.data)} entries restored")

        # ── Scanner state ──────────────────────────────────────────────────────
        result = sb.table("scanner_state").select("*").eq("id", 1).execute()
        if not result.data:
            print("[RESTORE] No state row found — starting fresh")
            return
        data = result.data[0]

        # ── New-day check ──────────────────────────────────────────────────────
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if data.get("saved_date") != today_str:
            saved = data.get("saved_date", "unknown")
            print(f"[DAILY RESET] New trading day ({saved} → {today_str}) — P&L reset to $0")
            daily_pnl              = 0.0
            trading_halted_today   = False
            consecutive_losses     = 0
            circuit_breaker_active = False
            _save_state()
            return

        # ── Restore globals ────────────────────────────────────────────────────
        daily_pnl              = float(data.get("daily_pnl") or 0)
        trading_halted_today   = bool(data.get("trading_halted_today", False))
        consecutive_losses     = int(data.get("consecutive_losses") or 0)
        circuit_breaker_active = bool(data.get("circuit_breaker_active", False))
        app_state.margin_deployed = float(data.get("margin_deployed") or 0)

        # ── Restore open trades ────────────────────────────────────────────────
        for key, trade in (data.get("open_trades") or {}).items():
            app_state.open_trades[key] = trade
            print(f"[RESTORE] {trade.get('symbol')} {trade.get('direction')} "
                  f"entry={trade.get('entry_price')} sl={trade.get('sl_price')} "
                  f"tp1={trade.get('tp1_price')} restored")

        # ── Restore cooldowns (filter expired) ────────────────────────────────
        now     = time.time()
        dropped = 0
        for key, expiry in (data.get("cooldowns") or {}).items():
            if float(expiry) > now:
                _scanner_mod._cooldowns[key] = float(expiry)
            else:
                dropped += 1
                print(f"[RESTORE] cooldown {key} expired — dropped")
        if dropped:
            print(f"[RESTORE] {dropped} expired cooldown(s) dropped")

        print(f"[RESTORE] complete — trades={len(app_state.open_trades)} "
              f"daily_pnl=${daily_pnl:.2f} cooldowns={len(_scanner_mod._cooldowns)} "
              f"cb={consecutive_losses}/{CONSECUTIVE_LOSS_STOP}")

    except Exception as _e:
        print(f"[RESTORE] Error: {_e} — starting fresh")


def _update_daily_pnl(pnl: float):
    global daily_pnl, trading_halted_today
    daily_pnl = round(daily_pnl + pnl, 2)
    if not trading_halted_today and daily_pnl <= DAILY_LOSS_LIMIT:
        trading_halted_today = True
        print(f"[DAILY LIMIT] daily_pnl=${daily_pnl:.2f} — trading halted")
    _save_state()


def _on_trade_close(reason: str):
    global consecutive_losses, circuit_breaker_active
    if reason == "SL":
        consecutive_losses += 1
        print(f"[CIRCUIT BREAKER] consecutive_losses={consecutive_losses}/{CONSECUTIVE_LOSS_STOP}")
        if consecutive_losses >= CONSECUTIVE_LOSS_STOP and not circuit_breaker_active:
            circuit_breaker_active = True
            print("[CIRCUIT BREAKER] ACTIVE — auto-entry paused")
    else:
        consecutive_losses = 0
    _save_state()


def _append_trade_log(trade: dict, exit_price: float, reason: str, pnl: float, r: float):
    if not exit_price or exit_price <= 0:
        raise ValueError(
            f"[ASSERT] _append_trade_log: exit_price={exit_price!r} "
            f"symbol={trade.get('symbol')} direction={trade.get('direction')} reason={reason} "
            f"-- refusing to write trade row with null/zero price"
        )
    now_ts    = int(time.time())
    opened_at = trade.get("opened_at", now_ts)

    # ── In-memory entry (powers the LOG tab + CSV export) ─────────────────────
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
    }
    app_state.trade_log.append(entry)

    # ── Supabase insert ────────────────────────────────────────────────────────
    sb = _get_supabase()
    if sb is not None:
        try:
            open_iso  = datetime.fromtimestamp(opened_at, tz=timezone.utc).isoformat()
            close_iso = datetime.fromtimestamp(now_ts,    tz=timezone.utc).isoformat()
            sb.table("trade_log").insert({
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
            }).execute()
        except Exception as _e:
            print(f"[PERSIST] trade_log insert error: {_e}")



# ── Paper trade Supabase logging ─────────────────────────────────────────────

async def _save_paper_trade(trade: dict, alert: dict):
    """Insert a row into bounce_paper_trades when a paper trade opens."""
    if not PAPER_MODE or not supabase:
        return
    try:
        row = {
            "pair":          trade["symbol"],
            "direction":     trade["direction"],
            "score":         alert.get("score"),
            "tier":          trade.get("tier"),
            "is_score10":    trade.get("is_score10", False),
            "leverage":      trade.get("leverage"),
            "margin":        trade.get("margin"),
            "entry_price":   trade.get("entry_price"),
            "sl_price":      trade.get("sl_price"),
            "tp1_price":     trade.get("tp1_price"),
            "tp2_price":     trade.get("tp2_price"),
            "sl_pct":        round(trade.get("sl_dist", 0) / trade.get("entry_price", 1), 6)
                             if trade.get("entry_price") else None,
            "adx":           alert.get("adx1h"),
            "trend":         alert.get("trend"),
            "j_value":       alert.get("j15m"),
            "rsi":           alert.get("rsi15m"),
            "stoch_k":       alert.get("stoch_k"),
            "stoch_d":       alert.get("stoch_d"),
            "fired_at":      datetime.fromtimestamp(
                                 trade.get("opened_at", int(time.time())), tz=timezone.utc
                             ).isoformat(),
            "session":       trade.get("session", ""),
            "paper_mode":    True,
            "status":        "OPEN",
        }
        await asyncio.to_thread(
            lambda: supabase.table("bounce_paper_trades").insert(row).execute()
        )
    except Exception as e:
        print(f"[PAPER LOG] insert error: {e}")


async def _update_paper_trade_close(trade: dict, exit_price: float,
                                    reason: str, pnl: float):
    """Update the bounce_paper_trades row when a paper trade closes."""
    if not PAPER_MODE or not supabase:
        return
    try:
        opened_at = trade.get("opened_at", int(time.time()))
        duration  = round((int(time.time()) - opened_at) / 60, 1)
        await asyncio.to_thread(
            lambda: supabase.table("bounce_paper_trades")
                    .update({
                        "close_price":      exit_price,
                        "close_reason":     reason,
                        "pnl":              round(pnl, 2),
                        "duration_minutes": duration,
                        "status":           "WIN" if pnl >= 0 else "LOSS",
                        "closed_at":        datetime.now(timezone.utc).isoformat(),
                    })
                    .eq("pair",      trade["symbol"])
                    .eq("direction", trade["direction"])
                    .eq("status",    "OPEN")
                    .execute()
        )
    except Exception as e:
        print(f"[PAPER LOG] update error: {e}")


async def _do_open_trade(
    symbol: str, direction: str,
    margin_usdc: float, leverage: int,
    alert_data: Optional[dict] = None,
    exchange: str = "HL",
) -> tuple[Optional[dict], Optional[str]]:
    global circuit_breaker_active, trading_halted_today

    if app_state.margin_deployed + margin_usdc > MARGIN_HARD_CAP:
        return None, "cap_reached"
    if circuit_breaker_active:
        return None, "circuit_breaker"
    if trading_halted_today:
        return None, "daily_limit"

    key = app_state.trade_key(symbol, direction)
    if key in app_state.open_trades:
        return None, "already_open"

    _client = mexc_client if exchange == "MEXC" else hl_client
    sl_price = alert_data.get("sl_price") if alert_data else None
    result   = await _client.open_position(
        symbol, direction, margin_usdc, leverage, sl_price=sl_price
    )
    if result.get("status") != "ok":
        return None, result.get("msg", "open_failed")

    entry = result["entry_price"]
    if not entry or entry == 0.0:
        print(f"[TRADE BLOCKED] {symbol} {direction} null price rejected")
        return None, "null_price"

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
        "sl_dist":    alert_data.get("sl_dist")   if alert_data else None,
        "tp1_price":  alert_data.get("tp1_price") if alert_data else None,
        "tp2_price":  alert_data.get("tp2_price") if alert_data else None,
        "score":      alert_data.get("score")     if alert_data else None,
        "tier":       alert_data.get("tier")      if alert_data else None,
        "adx1h":      alert_data.get("adx1h")     if alert_data else None,
        "j15m":       alert_data.get("j15m")      if alert_data else None,
        "j1h":        alert_data.get("j1h")       if alert_data else None,
        "rsi15m":     alert_data.get("rsi15m")    if alert_data else None,
        "stoch_k":    alert_data.get("stoch_k")    if alert_data else None,
        "stoch_d":    alert_data.get("stoch_d")    if alert_data else None,
        "bid_pct":    alert_data.get("bid_pct")   if alert_data else None,
        "ask_pct":    alert_data.get("ask_pct")   if alert_data else None,
        "be_price":   round(entry * 1.001, 6) if direction == "LONG" else round(entry * 0.999, 6),
        "tp1_hit":       False,
        "partial_hit":   False,
        "is_score10":    alert_data.get("is_score10", False) if alert_data else False,
        "partial_price": alert_data.get("partial_price")     if alert_data else None,
        "session":       alert_data.get("session", "")       if alert_data else "",
        "extreme_price": None,
    }

    app_state.open_trades[key] = trade
    app_state.margin_deployed += margin_usdc
    app_state.trades_opened   += 1

    if PAPER_MODE and alert_data:
        asyncio.create_task(_save_paper_trade(trade, alert_data))

    for a in app_state.alerts:
        if a["symbol"] == symbol and a["direction"] == direction:
            a["is_in_trade"] = True

    print(f"[TRADE OPEN] {symbol} {direction} tier={trade.get('tier')} "
          f"entry={entry} sl={trade.get('sl_price')} tp1={trade.get('tp1_price')} "
          f"lev={leverage}x exchange={exchange}")
    _save_state()
    return trade, None


# ━━ Telegram alerting ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_TREND_EMOJI = {
    "Strong Bull": "🚀",
    "Bullish":     "📈",
    "Neutral":     "➡️",
    "Bearish":     "📉",
    "Strong Bear": "🔻",
}


def _fmt_p(v: float) -> str:
    if v >= 1000: return f"{v:,.2f}"
    if v >= 1:    return f"{v:.4f}"
    return f"{v:.6f}"


def _tg_post(msg: str) -> None:
    """POST to Telegram in a daemon thread — never blocks the scan loop."""
    def _send(text: str, parse_mode: str) -> None:
        url  = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        data = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": parse_mode}
        try:
            requests.post(url, json=data, timeout=10)
        except Exception as _e:
            print(f"[TG] send error: {_e}")

    full_msg = "🟣 HL BOUNCE\n" + msg
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
    """Build and send the HTML signal message for a new alert."""
    sym       = alert.get("symbol", "")
    direction = alert.get("direction", "LONG")
    tier      = alert.get("tier", "REGULAR")
    score     = alert.get("score", 0)
    trend     = alert.get("trend", "Neutral")
    j15m      = float(alert.get("j15m", 0) or 0)
    j1h       = float(alert.get("j1h",  0) or 0)
    adx       = float(alert.get("adx1h", 0) or 0)
    bid_pct   = float(alert.get("bid_pct", 0) or 0)
    ask_pct   = float(alert.get("ask_pct", 0) or 0)
    entry     = float(alert.get("entry_price", 0) or 0)
    sl        = float(alert.get("sl_price", 0) or 0)
    tp1       = float(alert.get("tp1_price", 0) or 0)
    tp2       = float(alert.get("tp2_price", 0) or 0)
    leverage  = int(alert.get("leverage", 5) or 5)
    margin    = float(alert.get("margin", MARGIN_PER_TRADE) or MARGIN_PER_TRADE)
    session   = alert.get("session", "—") or "—"

    tier_map    = {"HIGH_PROB": "✦ HIGH PROB", "STRONG": "◆ STRONG"}
    tier_label  = tier_map.get(tier, "● REGULAR")
    trend_emoji = _TREND_EMOJI.get(trend, "➡️")

    sess_lo = session.lower()
    if "asia" in sess_lo:                          session_emoji = "🌏"
    elif "london" in sess_lo:                      session_emoji = "🌍"
    elif "ny" in sess_lo or "new york" in sess_lo: session_emoji = "🌎"
    else:                                          session_emoji = "🌑"

    is_long  = direction == "LONG"
    size     = (margin * leverage / entry) if entry else 0
    tp1_pnl  = ((tp1 - entry) * size) if is_long else ((entry - tp1) * size)
    tp2_pnl  = ((tp2 - entry) * size) if is_long else ((entry - tp2) * size)
    sl_pnl   = ((sl  - entry) * size) if is_long else ((entry - sl)  * size)
    sl_dist  = abs(entry - sl)
    d_risk   = margin * leverage * (sl_dist / entry) if entry else margin
    rr       = round(abs(tp1_pnl) / d_risk, 2) if d_risk else 0
    liq      = (entry - margin / size) if (is_long and size) else \
               (entry + margin / size) if size else 0
    adx_lbl  = "Trending" if adx >= 25 else "Ranging"
    dir_lbl  = "Open Long" if is_long else "Open Short"
    ts       = datetime.now(_EDT).strftime("%Y-%m-%d %H:%M EDT")

    parts = [
        f"<b>{tier_label} {direction} — {sym}</b>",
        f"Score: {score}/4",
        f"Trend: {trend_emoji} {trend}",
        f"J15M: {j15m:.1f}",
        f"J1H: {j1h:.1f}",
        "📊 ADX: " + f"{adx:.1f} — {adx_lbl}",
        f"Depth: B{bid_pct:.0f}% / S{ask_pct:.0f}%",
        f"Session: {session_emoji} {session} +0",
        "━━━ ORDER SETUP ━━━",
        f"Direction:   {dir_lbl}",
        f"Entry:       {_fmt_p(entry)}",
        f"Cost:        {margin:.0f} USDT",
        f"Leverage:    {leverage}x (Isolated)",
        f"TP1:         {_fmt_p(tp1)}",
        f"TP2:         {_fmt_p(tp2)}",
        f"SL:          {_fmt_p(sl)}",
        "━━━ RISK SUMMARY ━━━",
        "Est. Profit TP1: $" + f"{tp1_pnl:.2f}" + " net",
        "Est. Profit TP2: $" + f"{tp2_pnl:.2f}" + " net",
        "Max Loss:        $" + f"{sl_pnl:.2f}"  + " net",
        f"R:R:             1:{rr}",
        f"Liq Price:       ~{_fmt_p(liq)}",
        "⏱ " + ts,
    ]
    _tg_post("\n".join(parts))


def send_reminder(alert: dict, cancel_event: threading.Event) -> None:
    """Sleep 30 min (in chunks), then send a reminder unless cancelled."""
    sym       = alert.get("symbol", "")
    direction = alert.get("direction", "LONG")
    sl        = float(alert.get("sl_price", 0) or 0)
    tp1       = float(alert.get("tp1_price", 0) or 0)
    tp2       = float(alert.get("tp2_price", 0) or 0)
    fired_at  = alert.get("fired_at", int(time.time()))
    orig_time = datetime.fromtimestamp(fired_at, tz=_EDT).strftime("%H:%M EDT")

    elapsed = 0
    while elapsed < 1800 and not cancel_event.is_set():
        time.sleep(10)
        elapsed += 10

    if cancel_event.is_set():
        return

    live_price = app_state.prices.get(sym, 0)
    key        = f"{sym}{direction}"

    if key in app_state.open_trades:
        status = "✅ ACTIVE"
    else:
        closed = next(
            (e for e in reversed(app_state.trade_log)
             if e["symbol"] == sym and e["direction"] == direction), None
        )
        if closed:
            reason = closed.get("exit_reason", "")
            if reason == "SL":
                status = "🔴 SL Breached"
            elif reason in ("TP2", "HC_PARTIAL_1.5R"):
                status = "✅ TP2 Reached"
            else:
                status = "Closed (" + reason + ")"
        else:
            status = "✅ ACTIVE"

    ts  = datetime.now(_EDT).strftime("%Y-%m-%d %H:%M EDT")
    msg = (
        "⏰ REMINDER — " + sym + " " + direction + "\n"
        "Alert sent 30 min ago at " + orig_time + "\n"
        "Current price: " + _fmt_p(live_price) + "\n"
        "SL: " + _fmt_p(sl) + " | TP1: " + _fmt_p(tp1) + " | TP2: " + _fmt_p(tp2) + "\n"
        "Status: " + status + "\n"
        "⏱ " + ts
    )
    _tg_post(msg)


# ── Background loops ──────────────────────────────────────────────────────────

async def _scan_loop():
    await asyncio.sleep(3)
    while True:
        try:
            new_alerts = await run_full_scan(hl_client, market_health=app_state.market_health)
            _check_stale_prices()
            # Session change detection — reset per-pair session halts when session rolls
            global _prev_session
            _curr_sess = get_session_name()
            if _prev_session and _curr_sess != _prev_session:
                _gone = [k for k in list(_session_sl_counts) if k.endswith(f"_{_prev_session}")]
                for _k in _gone:
                    _session_sl_counts.pop(_k, None)
                    _session_halted.discard(_k)
                print(f"[SESSION RESET] {_prev_session} session ended — clearing all session halts.")
            _prev_session = _curr_sess
            app_state.last_scan_at = int(time.time())
            app_state.pair_states  = await scan_pair_state(hl_client)
            app_state.market_health = compute_market_health(
                app_state.pair_states, list(app_state.trade_log)
            )

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

                # Session halt gate
                _sg = f"{sym}_{dir_}_{get_session_name()}"
                if _sg in _session_halted:
                    print(f"[GATE] SESSION HALT — {sym} {dir_} halted for {get_session_name()} session (2 SL hits)")
                    continue

                # Issue 2 fix: set cooldown immediately when alert fires so scanner
                # stops re-confirming the same signal on subsequent scans
                set_close_cooldown(sym, dir_)
                _save_state()

                # Update alerts panel
                existing = next(
                    (a for a in app_state.alerts
                     if a["symbol"] == sym and a["direction"] == dir_), None
                )
                if existing:
                    app_state.alerts.remove(existing)
                app_state.alerts.insert(0, alert)

                # Telegram alert + 30-min reminder
                if TELEGRAM_ENABLED:
                    threading.Thread(target=lambda a=alert: send_telegram(a), daemon=True).start()
                    ev = threading.Event()
                    if sym in _pending_reminders:
                        _pending_reminders[sym].set()
                    _pending_reminders[sym] = ev
                    threading.Thread(target=lambda a=alert, e=ev: send_reminder(a, e), daemon=True).start()

                # Auto-entry gate: blocked when live and LIVE_MANUAL_ENTRY_ONLY is True
                if not PAPER_MODE and LIVE_MANUAL_ENTRY_ONLY:
                    print(
                        f"[SIGNAL] {sym} {dir_} tier={alert.get('tier')} "
                        f"lev={alert.get('leverage')}x entry={alert.get('entry_price')} "
                        f"sl={alert.get('sl_price')} tp1={alert.get('tp1_price')} "
                        f"— live manual entry required via overlay. "
                        f"Do not open position automatically."
                    )
                else:
                    if not PAPER_MODE:
                        print(
                            "[WARNING] LIVE AUTO-ENTRY ACTIVE — "
                            "LIVE_MANUAL_ENTRY_ONLY is disabled."
                        )
                    _margin = alert.get("margin", MARGIN_PER_TRADE)
                    trade, err = await _do_open_trade(
                        sym, dir_,
                        _margin, alert["leverage"],
                        alert_data=alert,
                        exchange="HL",
                    )
                    if trade:
                        print(
                            f"[AUTO TRADE] {sym} {dir_} opened "
                            f"tier={alert.get('tier')} lev={alert.get('leverage')}x "
                            f"entry={trade.get('entry_price')} sl={trade.get('sl_price')} "
                            f"margin=${_margin:.0f}"
                        )
                    elif err:
                        print(f"[AUTO TRADE] {sym} {dir_} skipped: {err}")
        except Exception as e:
            print(f"[SCAN LOOP] error: {e}")
        await asyncio.sleep(SCAN_INTERVAL_SECONDS)


async def _price_loop():
    _chg_tick = 0
    while True:
        try:
            all_prices = await hl_client.get_all_prices()
            for sym in PAIRS:
                if sym in all_prices:
                    app_state.prices[sym] = all_prices[sym]

            # Fetch 24h changes every 5 price ticks (~40s) to avoid extra rate pressure
            _chg_tick += 1
            if _chg_tick >= 5:
                _chg_tick = 0
                changes = await hl_client.get_all_price_changes(PAIRS)
                if changes:
                    app_state.price_changes.update(changes)

            # Auto-reset daily PnL at UTC midnight
            global daily_pnl, trading_halted_today, _last_midnight_day
            today = datetime.now(timezone.utc).day
            if today != _last_midnight_day:
                daily_pnl            = 0.0
                trading_halted_today = False
                _last_midnight_day   = today
                print("[DAILY RESET] midnight UTC — daily_pnl reset")

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
                f"⚠️ PRICE STALE — {sym} — "
                f"no price for 2 consecutive scans. "
                f"Open trade at risk. Check manually."
            )
            print(f"[PRICE STALE] {sym} — Telegram alert sent")
            if TELEGRAM_ENABLED:
                threading.Thread(target=lambda m=msg: _tg_post(m), daemon=True).start()

    for sym in list(_stale_tg_sent):
        if sym not in stale:
            _stale_tg_sent.discard(sym)
            app_state.price_stale.pop(sym, None)

# ── Exit monitor helpers ───────────────────────────────────────────────────────

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
        print(f"[EXIT GUARD] {trade.get('symbol')} {trade.get('direction')} — refused HC partial close: exit_price={exit_price!r} is null/zero — skipping")
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
          f"pnl=${pnl:.2f} r={r:+.2f}R — SL moved to breakeven {entry:.6f}")
    _save_state()


def _do_close_trade(key: str, trade: dict, exit_price: float, reason: str):
    """Synchronous internal close — no exchange call, price already known."""
    if not exit_price or exit_price <= 0:
        print(f"[EXIT GUARD] {trade.get('symbol')} {trade.get('direction')} — refused close (reason={reason}): exit_price={exit_price!r} is null/zero — skipping")
        return
    sym       = trade["symbol"]
    direction = trade["direction"]
    remaining = trade.get("remaining_size", trade["size"])
    entry     = trade["entry_price"]

    pnl = (exit_price - entry) * remaining if direction == "LONG" \
          else (entry - exit_price) * remaining
    r   = _compute_r(pnl, trade)

    _append_trade_log(trade, exit_price, reason, pnl, r)
    _update_daily_pnl(pnl)
    _on_trade_close(reason)

    app_state.margin_deployed = max(0.0, app_state.margin_deployed - trade["margin"])
    if key in app_state.open_trades:
        del app_state.open_trades[key]
    _retire_alert(sym, direction)
    set_close_cooldown(sym, direction)

    print(f"[EXIT] {sym} {direction} closed at {exit_price} reason={reason} "
          f"pnl=${pnl:.2f} r={r:+.2f}R")
    if TELEGRAM_ENABLED:
        def _exit_tg(r=reason, s=sym, d=direction, ep=exit_price, p=pnl):
            if r == "SL":
                _tg_post("🔴 EXIT — " + s + " " + d + " — SL hit at " + _fmt_p(ep) + ". Final P&L: $" + f"{p:.2f}")
            elif r == "TP2":
                _tg_post("✅ TP2 HIT — " + s + " " + d + " — full close at " + _fmt_p(ep) + ". Final P&L: $" + f"{p:.2f}")
            else:
                _tg_post("📋 EXIT (" + r + ") — " + s + " " + d + " — closed at " + _fmt_p(ep) + ". P&L: $" + f"{p:.2f}")
        threading.Thread(target=_exit_tg, daemon=True).start()
    if PAPER_MODE:
        asyncio.create_task(_update_paper_trade_close(trade, exit_price, reason, pnl))
    _save_state()


def _do_partial_close_tp1(key: str, trade: dict, exit_price: float):
    """Close 70% of position at TP1, keep 30% runner open for Trailblazer ATR trailing stop."""
    if not exit_price or exit_price <= 0:
        print(f"[EXIT GUARD] {trade.get('symbol')} {trade.get('direction')} — refused TP1 close: exit_price={exit_price!r} is null/zero — skipping")
        return
    sym        = trade["symbol"]
    direction  = trade["direction"]
    full_size  = trade.get("remaining_size", trade["size"])
    close_size = full_size * TP1_CLOSE_PCT
    rem_size   = full_size - close_size
    entry      = trade["entry_price"]

    pnl = (exit_price - entry) * close_size if direction == "LONG" \
          else (entry - exit_price) * close_size
    r   = _compute_r(pnl, trade)

    # Log the TP1 partial close BEFORE modifying trade dict (so size/metadata is correct)
    _append_trade_log(trade, exit_price, "TP1", pnl, r)
    _update_daily_pnl(pnl)

    # Update trade in-place — keep 30% runner open for Trailblazer
    trade["remaining_size"]   = rem_size
    trade["tp1_hit"]          = True
    trade["extreme_price"]    = exit_price
    trade["trail_best_price"] = exit_price
    trade["trail_anchor"]     = exit_price
    trade["tp1_pnl"]          = pnl
    # Reduce deployed margin proportionally (TP1_CLOSE_PCT closed)
    old_margin = trade.get("margin", MARGIN_PER_TRADE)
    trade["margin"] = old_margin * (1.0 - TP1_CLOSE_PCT)
    app_state.open_trades[key]     = trade
    app_state.margin_deployed      = max(0.0, app_state.margin_deployed - old_margin * TP1_CLOSE_PCT)

    print(f"[EXIT] {sym} {direction} TP1 partial close ({int(TP1_CLOSE_PCT*100)}%) at {exit_price} "
          f"pnl=${pnl:.2f} r={r:+.2f}R — 30% runner open watching Trailblazer ATR trail")
    if TELEGRAM_ENABLED:
        def _tp1_tg(s=sym, d=direction, ep=exit_price, p=pnl):
            _tg_post("✅ TP1 HIT — " + s + " " + d + " — partial close at " + _fmt_p(ep) + ". P&L so far: $" + f"{p:.2f}")
        threading.Thread(target=_tp1_tg, daemon=True).start()
    _save_state()


def _do_trailblazer_close(key: str, trade: dict, exit_price: float,
                           trail_best: float, trail_stop: float):
    """Close remaining 30% runner at Trailblazer ATR trailing stop trigger."""
    if not exit_price or exit_price <= 0:
        print(f"[EXIT GUARD] {trade.get('symbol')} {trade.get('direction')} — refused TRAILBLAZER close: exit_price={exit_price!r} is null/zero — skipping")
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
    set_close_cooldown(sym, direction)

    print(f"[TRAILBLAZER] {sym} {direction} — runner closed at {exit_price}, "
          f"best price was {trail_best}, trail stop triggered at {trail_stop}")
    if TELEGRAM_ENABLED:
        import datetime as _dt
        _ts = _dt.datetime.now(_dt.timezone(
            _dt.timedelta(hours=-5))).strftime("%Y-%m-%d %H:%M EST")
        def _trail_tg(s=sym, d=direction, ep=exit_price, p=pnl, rv=r,
                      tb=trail_best, ts_=trail_stop, tp=total_pnl, ts_str=_ts):
            _tg_post(
                "🏃 TRAILBLAZER EXIT — " + s + " " + d + "\n"
                + "Runner 30% closed at " + _fmt_p(ep) + "\n"
                + "Best price reached: " + _fmt_p(tb) + "\n"
                + "Trail stop triggered: " + _fmt_p(ts_) + "\n"
                + "Runner P&L: $" + f"{p:.2f}" + " (" + f"{rv:+.2f}" + "R)\n"
                + "Total trade P&L: $" + f"{tp:.2f}" + "\n"
                + "⏱ " + ts_str
            )
        threading.Thread(target=_trail_tg, daemon=True).start()
    _save_state()


# ── Exit monitor loop ─────────────────────────────────────────────────────────────

async def _exit_monitor_loop():
    """Runs every PRICE_INTERVAL_SECONDS. Checks every open trade against SL/TP."""
    while True:
        try:
            for key, trade in list(app_state.open_trades.items()):
                sym       = trade["symbol"]
                direction = trade["direction"]
                sl_price  = trade.get("sl_price")
                tp1_price = trade.get("tp1_price")
                tp2_price = trade.get("tp2_price")
                current   = app_state.prices.get(sym)
                tp1_hit   = trade.get("tp1_hit", False)
                is_short  = direction == "SHORT"

                if not current or current <= 0 or not sl_price:
                    print(f"[EXIT CHECK] {sym} {direction} skipped — "
                          f"no price ({current}) or no sl ({sl_price})")
                    continue

                # Track extreme price (lowest for SHORT, highest for LONG)
                ep = trade.get("extreme_price") or current
                trade["extreme_price"] = min(ep, current) if is_short else max(ep, current)

                # ── SL breach ──────────────────────────────────────────────────
                # SHORT: SL triggers when price RISES above sl_price
                # LONG : SL triggers when price FALLS below sl_price
                sl_breached = (is_short and current >= sl_price) or \
                              (not is_short and current <= sl_price)

                if sl_breached:
                    print(f"[EXIT CHECK] {sym} {direction} price={current} "
                          f"sl={sl_price} tp1={tp1_price} → SL BREACHED → closing")
                    _do_close_trade(key, trade, current, "SL")
                    # Per-pair direction session SL count
                    _skey = f"{sym}_{direction}_{get_session_name()}"
                    _session_sl_counts[_skey] = _session_sl_counts.get(_skey, 0) + 1
                    if _session_sl_counts[_skey] >= 2 and _skey not in _session_halted:
                        _session_halted.add(_skey)
                        print(f"[SESSION HALT] {sym} {direction} — 2 SL hits in {get_session_name()} session. Halted for remainder of session.")
                    # $100 SL cooldown — override with 90-min directional cooldown
                    _rem_sz = trade.get("remaining_size", trade.get("size", 0))
                    _sl_pnl = (current - trade["entry_price"]) * _rem_sz if not is_short \
                              else (trade["entry_price"] - current) * _rem_sz
                    if abs(_sl_pnl) >= 100:
                        _exp = time.time() + 90 * 60
                        _scanner_mod._cooldowns[f"{sym}{direction}"] = _exp
                        _large_sl_cooldowns[f"{sym}{direction}"]     = _exp
                        print(f"[LARGE SL COOLDOWN] {sym} {direction} — SL ${abs(_sl_pnl):.2f} >= $100 threshold. 90 min cooldown applied.")
                    continue

                # ── HC early partial close at 1.5R → SL to breakeven ────────────
                if (trade.get("is_score10") and not trade.get("partial_hit")
                        and trade.get("partial_price")):
                    _pp     = trade["partial_price"]
                    _pp_hit = (is_short and current <= _pp) or (not is_short and current >= _pp)
                    if _pp_hit:
                        _do_hc_partial_close(key, trade, current)
                        continue

                # ── TP1 (always checked first — partial close, half position) ────
                if not tp1_hit and tp1_price:
                    tp1_reached = (is_short and current <= tp1_price) or \
                                  (not is_short and current >= tp1_price)
                    print(f"[EXIT CHECK] {sym} {direction} price={current} "
                          f"tp1={tp1_price} tp1_hit={tp1_hit} → "
                          f"{'TP1 TRIGGERED → partial close' if tp1_reached else 'watching tp1'}")
                    if tp1_reached:
                        _do_partial_close_tp1(key, trade, current)
                        continue

                # ── TRAILBLAZER: ATR trailing stop after tp1_hit ──────────────
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

                # HC trailing SL after tp1_hit: lock 1.5R minimum profit
                if trade.get("is_score10") and tp1_hit:
                    _sl_d = trade.get("sl_dist") or 0
                    if _sl_d > 0:
                        _ent   = trade["entry_price"]
                        _lock  = (_ent + 1.5 * _sl_d if not is_short else _ent - 1.5 * _sl_d)
                        _ep    = trade.get("extreme_price") or current
                        _trail = (_ep - 2.0 * _sl_d if not is_short else _ep + 2.0 * _sl_d)
                        _nsl   = (max(_lock, _trail) if not is_short else min(_lock, _trail))
                        if sl_price and ((not is_short and _nsl > sl_price) or
                                        (is_short and _nsl < sl_price)):
                            trade["sl_price"] = round(_nsl, 6)
                            app_state.open_trades[key]["sl_price"] = round(_nsl, 6)

                # No exit this cycle
                _trail_info = (f" trail_best={trade.get('trail_best_price')} trail_stop={trade.get('trail_stop_price')}"
                               if tp1_hit else "")
                print(f"[EXIT CHECK] {sym} {direction} price={current} "
                      f"sl={sl_price} tp1={tp1_price}{_trail_info} → no exit")

        except Exception as e:
            print(f"[EXIT MONITOR] error: {e}")

        await asyncio.sleep(PRICE_INTERVAL_SECONDS)


# ── Lifespan ──────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    global hl_client, mexc_client
    hl_client   = HLClient()
    mexc_client = MexcClient()
    log_startup_config()
    _load_state()

    # ── Mode log ──────────────────────────────────────────────────────────────
    if PAPER_MODE:
        print("[MODE] PAPER trading — auto-entry enabled")
    elif LIVE_MANUAL_ENTRY_ONLY:
        print("[MODE] LIVE trading — manual entry only via overlay. Auto-entry blocked.")
    else:
        print("[MODE] LIVE trading — AUTO-ENTRY ACTIVE. All signals will open live positions automatically. Confirm this is intentional.")

    scan_task  = asyncio.create_task(_scan_loop())
    price_task = asyncio.create_task(_price_loop())
    exit_task  = asyncio.create_task(_exit_monitor_loop())
    yield
    scan_task.cancel()
    price_task.cancel()
    exit_task.cancel()
    await hl_client.close()
    await mexc_client.close()


app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(request, "dashboard.html", {
        "paper_mode":    PAPER_MODE,
        "scan_interval": SCAN_INTERVAL_SECONDS,
        "margin_cap":    MARGIN_HARD_CAP,
    }, headers={"Content-Type": "text/html; charset=utf-8"})


@app.get("/api/state")
async def get_state():
    return app_state.serialise()


@app.get("/api/account")
async def get_account():
    return {
        "margin_deployed": round(app_state.margin_deployed, 2),
        "cap":             MARGIN_HARD_CAP,
        "paper_mode":      PAPER_MODE,
        "slots_used":      app_state.slots_used,
    }


# ── Per-pair overlay endpoint ─────────────────────────────────────────────────

@app.get("/api/pair/{symbol}")
async def get_pair(symbol: str):
    ps = next((p for p in app_state.pair_states if p.get("symbol") == symbol), None)
    if ps is None:
        raise HTTPException(status_code=404, detail="pair not found")

    j15m    = ps.get("j15m",    50)
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
    stoch_gate_long  = stoch_k < 25 and stoch_k_prev <= stoch_d_prev and stoch_k > stoch_d
    stoch_gate_short = stoch_k > 75 and stoch_k_prev >= stoch_d_prev and stoch_k < stoch_d
    gate_long  = [j15m < 20, j1h < 40, stoch_gate_long,  bid_pct >= 55]
    gate_short = [j15m > 80, j1h > 60, stoch_gate_short, ask_pct >= 55]
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
        size  = t.get("size",   0)
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
        "gate_long":           gate_long,
        "gate_short":          gate_short,
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
        "session_halt_reason":  "2 SL hits this session — resumes at next session open" if (
            f"{symbol}_LONG_{get_session_name()}"  in _session_halted or
            f"{symbol}_SHORT_{get_session_name()}" in _session_halted
        ) else None,
    }


# ── Trade open ────────────────────────────────────────────────────────────────

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
            detail=f"{req.symbol} {req.direction} halted for {get_session_name()} session — 2 SL hits. Resumes at next session open.")
    # Large SL cooldown gate
    _lcd_k = f"{req.symbol}{req.direction}"
    if _lcd_k in _large_sl_cooldowns and _large_sl_cooldowns[_lcd_k] > time.time():
        _lcd_rem = max(0, int(_large_sl_cooldowns[_lcd_k] - time.time()))
        _lcd_m, _lcd_s = divmod(_lcd_rem, 60)
        raise HTTPException(status_code=400,
            detail=f"{req.symbol} {req.direction} — 90 min cooldown active, {_lcd_m}m{_lcd_s}s remaining. Large SL hit.")
    # Manual entry via overlay — always permitted regardless of LIVE_MANUAL_ENTRY_ONLY setting.
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
            detail = (f"Daily loss limit reached — ${daily_pnl:.2f} of ${DAILY_LOSS_LIMIT:.0f}."
                      " Tap Reset Session to resume trading.")
        elif err == "circuit_breaker":
            detail = (f"Circuit breaker active — {consecutive_losses} consecutive losses."
                      " Tap Reset Session to resume.")
        elif err == "cap_reached":
            detail = (f"Margin cap reached — ${app_state.margin_deployed:.0f} of ${MARGIN_HARD_CAP:.0f} deployed."
                      " Close a position to continue.")
        else:
            detail = err
        raise HTTPException(status_code=code, detail=detail)
    return {"status": "ok", "trade": trade}


# ── Trade close ───────────────────────────────────────────────────────────────

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

    app_state.margin_deployed = max(0.0, app_state.margin_deployed - trade["margin"])
    closed = {**trade, "close_price": close_price, "final_pnl": round(pnl, 2)}
    del app_state.open_trades[key]
    _retire_alert(req.symbol, req.direction)
    set_close_cooldown(req.symbol, req.direction)

    _save_state()
    print(f"[TRADE CLOSE] {req.symbol} {req.direction} MANUAL pnl=${pnl:.2f} r={r:+.2f}")
    return {"status": "ok", "closed": closed}


# ── Circuit breaker ───────────────────────────────────────────────────────────

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
    print("[SESSION RESET] manual reset — daily P&L, cooldowns, circuit breaker cleared")
    return {"reset": True, "message": "Session reset — daily P&L, cooldowns and circuit breaker cleared"}


# ── Daily reset ───────────────────────────────────────────────────────────────

@app.post("/api/reset-day")
async def reset_day():
    global daily_pnl, trading_halted_today
    daily_pnl            = 0.0
    trading_halted_today = False
    print("[DAILY RESET] manual reset")
    return {"status": "ok"}


# ── Trade log ─────────────────────────────────────────────────────────────────

@app.get("/api/tradelog")
async def get_tradelog():
    return app_state.trade_log


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
    from_ts: Optional[int] = Query(None, description="Unix epoch seconds — start of date range (inclusive)"),
    to_ts:   Optional[int] = Query(None, description="Unix epoch seconds — end of date range (inclusive)"),
):
    """With from_ts+to_ts: deletes only log entries in that range (no state reset).
    Without params: full clear — force-closes open trades, resets all state."""

    if from_ts is not None and to_ts is not None:
        # ── Date-ranged delete — only remove matching closed log entries ──
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
                sb.table("trade_log").delete() \
                    .gte("close_time", from_iso) \
                    .lte("close_time", to_iso) \
                    .eq("exchange", "HL") \
                    .execute()
            except Exception as _e:
                print(f"[CLEAR] Supabase date-range delete error: {_e}")
        print(f"[CLEAR] {len(removed)} log entries removed for range {from_ts}–{to_ts}")
        return {"status": "ok", "entries_removed": len(removed)}

    # ── Full clear (no date params) — existing behaviour unchanged ──
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
