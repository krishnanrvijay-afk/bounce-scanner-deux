import asyncio
import requests
import httpx
import os
import sys
sys.path.insert(0, '.')
from scanner import _compute_kdj
from supabase import create_client
from datetime import datetime, timezone

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
MARGIN = 5000
LEVERAGE = 5
POST_EXIT_WINDOW = 1800

HL_API_URL = "https://api.hyperliquid.xyz/info"
MEXC_BASE = (
    "https://contract.mexc.com"
    "/api/v1/contract/kline"
)

def proj_pnl(entry, price, direction):
    sz = (MARGIN * LEVERAGE) / entry
    if direction == "LONG":
        return round((price - entry) * sz, 2)
    else:
        return round((entry - price) * sz, 2)

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

    # Fetch all CONFIRM_REVERSAL trades
    # duration >= 300s only (Group B)
    hl = sb.table("hl_trade_log")\
        .select("*")\
        .eq("exit_reason",
            "CONFIRM_REVERSAL")\
        .gte("duration_seconds", 300)\
        .gte("created_at",
             "2026-07-03T00:00:00+00:00")\
        .order("created_at",
               desc=False)\
        .execute().data

    mx = sb.table("mexc_trade_log")\
        .select("*")\
        .eq("exit_reason",
            "CONFIRM_REVERSAL")\
        .gte("duration_seconds", 300)\
        .gte("created_at",
             "2026-07-03T00:00:00+00:00")\
        .order("created_at",
               desc=False)\
        .execute().data

    rows = (
        [("HL", r) for r in hl] +
        [("MEXC", r) for r in mx]
    )

    print(f"Group B CONFIRM_REVERSAL: "
          f"{len(hl)} HL + {len(mx)} MEXC "
          f"= {len(rows)} total")
    print()
    print(
        f"{'VN':>4} {'PAIR':>10} "
        f"{'DIR':>6} {'DUR':>5} "
        f"{'EXIT_PNL':>9} {'MFE':>6} "
        f"{'POST_PK':>9} {'POST_FN':>9} "
        f"{'VERDICT':>12}"
    )
    print("-" * 80)

    continued = reversed_ = flat = 0
    no_data = 0
    total_recoverable = 0.0

    for venue, t in rows:
        pair = t["pair"]
        direction = t["direction"]
        entry = float(t["entry_price"])
        exit_p = float(t["exit_price"])
        exit_pnl = float(
            t["pnl_dollars"] or 0)
        dur = int(
            t.get("duration_seconds",
                  300) or 300)
        mfe_r = float(t.get("mfe_r") or 0)

        dt = datetime.fromisoformat(
            t["created_at"].replace(
                "+00:00", "")
        ).replace(tzinfo=timezone.utc)
        open_ts = int(dt.timestamp())
        exit_ts = open_ts + dur
        exit_ms = exit_ts * 1000
        post_end_ms = (
            exit_ts + POST_EXIT_WINDOW
        ) * 1000

        if venue == "HL":
            candles = await fetch_hl(
                pair, "1m",
                exit_ms - 60000,
                post_end_ms)
        else:
            candles = fetch_mexc(
                pair, "Min1",
                exit_ts - 60,
                exit_ts + POST_EXIT_WINDOW)

        post = [c for c in candles
                if c["t"] >= exit_ms]

        if not post:
            no_data += 1
            print(
                f"{venue:>4} {pair:>10} "
                f"{direction:>6} "
                f"{dur:>5} "
                f"{exit_pnl:>+9.2f} "
                f"{mfe_r:>6.3f} "
                f"{'NO DATA':>9} "
                f"{'':>9} "
                f"{'':>12}"
            )
            continue

        post_peak = exit_pnl
        post_final = exit_pnl

        for c in post:
            best = (c["l"] if
                    direction == "SHORT"
                    else c["h"])
            pk = proj_pnl(entry, best,
                          direction)
            fn = proj_pnl(entry, c["c"],
                          direction)
            if pk > post_peak:
                post_peak = pk
            post_final = fn

        additional = post_peak - exit_pnl

        if additional > 15:
            verdict = "RECOVERED"
            continued += 1
            total_recoverable += additional
        elif post_final < exit_pnl - 15:
            verdict = "CONTINUED ADV"
            reversed_ += 1
        else:
            verdict = "FLAT"
            flat += 1

        print(
            f"{venue:>4} {pair:>10} "
            f"{direction:>6} "
            f"{dur:>5} "
            f"{exit_pnl:>+9.2f} "
            f"{mfe_r:>6.3f} "
            f"{post_peak:>+9.2f} "
            f"{post_final:>+9.2f} "
            f"{verdict:>12}"
        )

        await asyncio.sleep(0.25)

    print()
    print(f"RECOVERED after exit:    "
          f"{continued}")
    print(f"CONTINUED ADVERSE:       "
          f"{reversed_}")
    print(f"FLAT:                    "
          f"{flat}")
    print(f"NO DATA:                 "
          f"{no_data}")
    print(f"Recoverable PnL left:    "
          f"+${total_recoverable:.2f}")
    print("Done.")

asyncio.run(main())
