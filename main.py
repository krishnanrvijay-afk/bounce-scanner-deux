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
    MARGIN_PER_TRADE, MARGIN_HARD_CAP, PAPER_MODE,
    CONSECUTIVE_LOSS_STOP, DAILY_LOSS_LIMIT, TP1_R, TP2_R,
)
from hl_client import HLClient
from mexc_client import MexcClient
from scanner import (
    run_full_scan, scan_pair_state, get_pending, get_btc_regime,
    get_scan_count, set_close_cooldown, clear_cooldown,
    get_cooldown_remaining, clear_all_scanner_state, log_startup_config,
)

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
        self.open_trades:          dict[str, dict]   = {}
        self.trade_log:            list[dict]        = []
        self.margin_deployed:      float             = 0.0
        self.trades_opened:        int               = 0
        self.last_scan_at:         Optional[int]     = None

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
                "paper_mode":      PAPER_MODE,
                "slots_used":      self.slots_used,
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
            "btc_regime":       get_btc_regime(),
            "scan_count":       get_scan_count(),
            "last_scan_at":     self.last_scan_at,
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


def _update_daily_pnl(pnl: float):
    global daily_pnl, trading_halted_today
    daily_pnl = round(daily_pnl + pnl, 2)
    if not trading_halted_today and daily_pnl <= DAILY_LOSS_LIMIT:
        trading_halted_today = True
        print(f"[DAILY LIMIT] daily_pnl=${daily_pnl:.2f} — trading halted")


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


def _append_trade_log(trade: dict, exit_price: float, reason: str, pnl: float, r: float):
    app_state.trade_log.append({
        "timestamp_opened": trade.get("opened_at", 0),
        "timestamp_closed": int(time.time()),
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
        "duration_seconds": int(time.time()) - trade.get("opened_at", int(time.time())),
        "exchange":         trade.get("exchange", "HL"),
        "paper":            trade.get("paper", True),
    })


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
    return trade, None


# ── Background loops ──────────────────────────────────────────────────────────

async def _scan_loop():
    await asyncio.sleep(3)
    while True:
        try:
            new_alerts = await run_full_scan(hl_client)
            app_state.last_scan_at = int(time.time())
            app_state.pair_states  = await scan_pair_state(hl_client)
            for alert in new_alerts:
                sym, dir_ = alert["symbol"], alert["direction"]
                key = app_state.trade_key(sym, dir_)
                existing = next(
                    (a for a in app_state.alerts
                     if a["symbol"] == sym and a["direction"] == dir_), None
                )
                if existing:
                    app_state.alerts.remove(existing)
                app_state.alerts.insert(0, alert)
        except Exception as e:
            print(f"[SCAN LOOP] error: {e}")
        await asyncio.sleep(SCAN_INTERVAL_SECONDS)


async def _price_loop():
    while True:
        try:
            all_prices = await hl_client.get_all_prices()
            for sym in PAIRS:
                if sym in all_prices:
                    app_state.prices[sym] = all_prices[sym]

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


# ── Lifespan ──────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    global hl_client, mexc_client
    hl_client   = HLClient()
    mexc_client = MexcClient()
    log_startup_config()
    scan_task  = asyncio.create_task(_scan_loop())
    price_task = asyncio.create_task(_price_loop())
    yield
    scan_task.cancel()
    price_task.cancel()
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
        "btc_regime":      get_btc_regime(),
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
