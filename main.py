import asyncio
import csv
import io
import logging
import os
import time
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

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from config import (
    PAIRS, SCAN_INTERVAL_SECONDS, PRICE_INTERVAL_SECONDS,
    MARGIN_PER_TRADE, MARGIN_HARD_CAP, PAPER_MODE, LIVE_MANUAL_ENTRY_ONLY,
    CONSECUTIVE_LOSS_STOP, DAILY_LOSS_LIMIT, TP1_R, TP2_R,
    SUPABASE_URL, SUPABASE_KEY,
)
from supabase import create_client, Client
from hl_client import HLClient
from mexc_client import MexcClient
from scanner import (
    run_full_scan, scan_pair_state, get_pending,
    get_scan_count, set_close_cooldown, clear_cooldown,
    get_cooldown_remaining, clear_all_scanner_state, log_startup_config,
)
import scanner as _scanner_mod  # direct access to _cooldowns dict for persistence

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
                "in_trade":      in_trade,
                "cooldown_short": cd_s,
                "cooldown_long":  cd_l,
            }

        return {
            "pair_states":    pair_states_out,
            "alerts":         self.alerts,
            "pending_alerts": get_pending(),
            "prices":         self.prices,
            "open_trades":    trades_out,
            "trade_log":      self.trade_log,
            "account": {
                "margin_deployed": round(self.margin_deployed, 2),
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
        log_rows = sb.table("trade_log").select("*").order("created_at").execute()
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
            }).execute()
        except Exception as _e:
            print(f"[PERSIST] trade_log insert error: {_e}")


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
        "bid_pct":    alert_data.get("bid_pct")   if alert_data else None,
        "ask_pct":    alert_data.get("ask_pct")   if alert_data else None,
        "be_price":   round(entry * 1.001, 6) if direction == "LONG" else round(entry * 0.999, 6),
        "tp1_hit":    False,
        "extreme_price": None,
    }

    app_state.open_trades[key] = trade
    app_state.margin_deployed += margin_usdc
    app_state.trades_opened   += 1

    for a in app_state.alerts:
        if a["symbol"] == symbol and a["direction"] == direction:
            a["is_in_trade"] = True

    print(f"[TRADE OPEN] {symbol} {direction} tier={trade.get('tier')} "
          f"entry={entry} sl={trade.get('sl_price')} tp1={trade.get('tp1_price')} "
          f"lev={leverage}x exchange={exchange}")
    _save_state()
    return trade, None


# ── Background loops ──────────────────────────────────────────────────────────

async def _scan_loop():
    await asyncio.sleep(3)
    while True:
        try:
            new_alerts = await run_full_scan(hl_client)
            app_state.last_scan_at = int(time.time())
            app_state.pair_states  = await scan_pair_state(hl_client)

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
                    trade, err = await _do_open_trade(
                        sym, dir_,
                        MARGIN_PER_TRADE, alert["leverage"],
                        alert_data=alert,
                        exchange="HL",
                    )
                    if trade:
                        print(
                            f"[AUTO TRADE] {sym} {dir_} opened "
                            f"tier={alert.get('tier')} lev={alert.get('leverage')}x "
                            f"entry={trade.get('entry_price')} sl={trade.get('sl_price')} "
                            f"margin=${MARGIN_PER_TRADE:.0f}"
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


# ── Exit monitor helpers ───────────────────────────────────────────────────────

def _compute_r(pnl: float, trade: dict) -> float:
    entry       = trade.get("entry_price") or 0
    sl_dist     = trade.get("sl_dist") or 0
    lev         = trade.get("leverage", 1)
    margin      = trade.get("margin", MARGIN_PER_TRADE)
    dollar_risk = margin * lev * (sl_dist / entry) if entry else 0
    return round(pnl / dollar_risk, 2) if dollar_risk else 0.0


def _do_close_trade(key: str, trade: dict, exit_price: float, reason: str):
    """Synchronous internal close — no exchange call, price already known."""
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
    _save_state()


def _do_partial_close_tp1(key: str, trade: dict, exit_price: float):
    """Close half the position at TP1, keep remainder open watching for TP2."""
    sym       = trade["symbol"]
    direction = trade["direction"]
    full_size = trade.get("remaining_size", trade["size"])
    half_size = full_size / 2
    entry     = trade["entry_price"]

    pnl = (exit_price - entry) * half_size if direction == "LONG" \
          else (entry - exit_price) * half_size
    r   = _compute_r(pnl, trade)

    # Log the TP1 partial close BEFORE modifying trade dict (so size/metadata is correct)
    _append_trade_log(trade, exit_price, "TP1", pnl, r)
    _update_daily_pnl(pnl)

    # Update trade in-place — keep it open for TP2 watch
    trade["remaining_size"] = half_size
    trade["tp1_hit"]        = True
    trade["extreme_price"]  = exit_price
    # Halve the deployed margin to reflect the partial close
    old_margin = trade.get("margin", MARGIN_PER_TRADE)
    trade["margin"] = old_margin / 2
    app_state.open_trades[key]     = trade
    app_state.margin_deployed      = max(0.0, app_state.margin_deployed - old_margin / 2)

    print(f"[EXIT] {sym} {direction} TP1 partial close at {exit_price} "
          f"half_pnl=${pnl:.2f} r={r:+.2f}R — remainder open watching TP2")
    _save_state()


# ── Exit monitor loop ──────────────────────────────────────────────────────────

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

                if current is None or not sl_price:
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

                # ── TP2 (only after tp1_hit=True — closes remainder) ──────────
                if tp1_hit and tp2_price:
                    tp2_reached = (is_short and current <= tp2_price) or \
                                  (not is_short and current >= tp2_price)
                    print(f"[EXIT CHECK] {sym} {direction} price={current} "
                          f"tp2={tp2_price} tp1_hit={tp1_hit} → "
                          f"{'TP2 TRIGGERED → full close remainder' if tp2_reached else 'watching tp2'}")
                    if tp2_reached:
                        _do_close_trade(key, trade, current, "TP2")
                        continue

                # No exit this cycle
                print(f"[EXIT CHECK] {sym} {direction} price={current} "
                      f"sl={sl_price} tp1={tp1_price} tp2={tp2_price} → no exit")

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
    })


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

    gate_long  = [j15m < 20, j1h < 40, rsi15m < 35, bid_pct >= 55]
    gate_short = [j15m > 80, j1h > 60, rsi15m > 65, ask_pct >= 55]
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
        raise HTTPException(status_code=code, detail=err)
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

    close_price = result.get("close_price", app_state.prices.get(req.symbol, trade["entry_price"]))
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
        headers={"Content-Disposition": f"attachment; filename=bounce_trade_log_{today}.csv"},
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
async def clear_tradelog():
    global consecutive_losses, circuit_breaker_active, daily_pnl, trading_halted_today

    count = len(app_state.open_trades)
    for key, trade in list(app_state.open_trades.items()):
        sym   = trade["symbol"]
        ep    = app_state.prices.get(sym, trade["entry_price"])
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
