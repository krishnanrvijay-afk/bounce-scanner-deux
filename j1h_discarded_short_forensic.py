import asyncio
import httpx
import requests
import os
import sys
import time
sys.path.insert(0, '.')
from scanner import _compute_kdj
from supabase import create_client
from datetime import datetime, timezone

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
MARGIN = 5000
LEVERAGE = 5
WINDOW_SECONDS = 480

HL_API_URL = "https://api.hyperliquid.xyz/info"
MEXC_BASE = (
    "https://contract.mexc.com"
    "/api/v1/contract/kline"
)

def proj_pnl(signal_price, close, direction):
    sz = (MARGIN * LEVERAGE) / signal_price
    if direction == "LONG":
        return round((close - signal_price) * sz, 2)
    else:
        return round((signal_price - close) * sz, 2)

async def fetch_hl(symbol, interval,
                   start_ms, end_ms):
    payload = {
        "type": "candleSnapshot",
        "req": {
            "coin": symbol,
            "interval": interval,
            "startTime": start_ms,
            "endTime": end_ms
        }
    }
    try:
        async with httpx.AsyncClient(
                timeout=30.0) as c:
            r = await c.post(
                HL_API_URL, json=payload)
            r.raise_for_status()
            data = r.json()
        if not isinstance(data, list):
            return []
        return sorted([{
            "t": int(x.get("t", 0)),
            "h": float(x.get("h", 0)),
            "l": float(x.get("l", 0)),
            "c": float(x.get("c", 0))
        } for x in data],
            key=lambda x: x["t"])
    except Exception:
        return []

def fetch_mexc(symbol, interval,
               start_s, end_s):
    try:
        r = requests.get(
            f"{MEXC_BASE}/{symbol}",
            params={
                "interval": interval,
                "start": start_s,
                "end": end_s
            },
            timeout=15)
        r.raise_for_status()
        d = r.json()
        if not d.get("success"):
            return []
        raw = d["data"]
        return sorted([{
            "t": int(raw["time"][i]) * 1000,
            "h": float(raw["high"][i]),
            "l": float(raw["low"][i]),
            "c": float(raw["close"][i])
        } for i in range(len(raw["time"]))],
            key=lambda x: x["t"])
    except Exception:
        return []

async def main():
    sb = create_client(SUPABASE_URL,
                       SUPABASE_KEY)

    # Fetch ALL J1H_DISCARDED SHORT alerts
    # from both venues since July 1
    rows = sb.table("alert_log")\
        .select("*")\
        .eq("outcome", "J1H_DISCARDED")\
        .eq("direction", "SHORT")\
        .gte("created_at",
             "2026-07-01T00:00:00+00:00")\
        .order("created_at", desc=False)\
        .execute().data

    print(f"Found {len(rows)} J1H_DISCARDED "
          f"SHORT alerts since July 1")
    print()
    print(f"{'VN':>5} {'PAIR':>10} "
          f"{'TIME':>6} {'J1H':>7} "
          f"{'J1H_PREV':>9} {'DELTA':>7} "
          f"{'SIG_PX':>10} {'PEAK_PNL':>10} "
          f"{'AT':>6} {'FINAL_PNL':>11} "
          f"{'RESULT':>12}")
    print("-" * 100)

    total_left = 0.0
    winners = 0
    losers = 0
    no_data = 0

    for a in rows:
        pair = a["pair"]
        venue = a["venue"]
        direction = "SHORT"
        signal_price = float(
            a["signal_price"] or 0)
        j1h = float(
            a["j1h_at_signal"] or 0)
        j1h_prev = float(
            a["j1h_prev_at_signal"] or 0)
        j1h_delta = round(j1h - j1h_prev, 2)
        created_at = a["created_at"]

        dt = datetime.fromisoformat(
            created_at.replace(
                "+00:00", "")
        ).replace(tzinfo=timezone.utc)
        signal_ts = int(dt.timestamp())
        signal_ts_ms = signal_ts * 1000
        expiry_ts_ms = (
            signal_ts + WINDOW_SECONDS
        ) * 1000

        if venue == "HL":
            candles = await fetch_hl(
                pair, "1m",
                signal_ts_ms - 60000,
                expiry_ts_ms + 60000)
        else:
            candles = fetch_mexc(
                pair, "Min1",
                signal_ts - 60,
                signal_ts + WINDOW_SECONDS
                + 60)

        window = [
            c for c in candles
            if c["t"] >= signal_ts_ms
            and c["t"] <= expiry_ts_ms
        ]

        if not window:
            no_data += 1
            print(
                f"{venue:>5} {pair:>10} "
                f"{dt.strftime('%H:%M'):>6} "
                f"{j1h:>7.1f} "
                f"{j1h_prev:>9.1f} "
                f"{j1h_delta:>+7.1f} "
                f"{signal_price:>10.4f} "
                f"{'NO DATA':>10}"
            )
            continue

        peak_pnl = 0.0
        peak_time = ""
        final_pnl = 0.0

        for c in window:
            best = c["l"]
            hi_pnl = proj_pnl(
                signal_price, best,
                direction)
            cl_pnl = proj_pnl(
                signal_price, c["c"],
                direction)
            if hi_pnl > peak_pnl:
                peak_pnl = hi_pnl
                peak_time = (
                    datetime.fromtimestamp(
                        c["t"] / 1000,
                        tz=timezone.utc
                    ).strftime("%H:%M"))
            final_pnl = cl_pnl

        result = (
            "WINNER" if peak_pnl > 20
            else "LOSER" if peak_pnl <= 0
            else "SMALL")

        if peak_pnl > 0:
            total_left += peak_pnl
            if peak_pnl > 20:
                winners += 1
            else:
                losers += 1
        else:
            losers += 1

        print(
            f"{venue:>5} {pair:>10} "
            f"{dt.strftime('%H:%M'):>6} "
            f"{j1h:>7.1f} "
            f"{j1h_prev:>9.1f} "
            f"{j1h_delta:>+7.1f} "
            f"{signal_price:>10.4f} "
            f"{peak_pnl:>+10.2f} "
            f"{peak_time:>6} "
            f"{final_pnl:>+11.2f} "
            f"{result:>12}")

        await asyncio.sleep(0.2)

    print()
    print(f"WINNERS (peak > $20):    {winners}")
    print(f"LOSERS:                  {losers}")
    print(f"NO DATA:                 {no_data}")
    print(f"Total hypothetical PnL "
          f"left on table: "
          f"+${total_left:.2f}")
    print("Done.")

asyncio.run(main())
