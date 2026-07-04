import asyncio
import sys
import time
sys.path.insert(0, '.')
from hl_client import HLClient
from scanner import _compute_kdj
import os
from supabase import create_client

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
MARGIN = 5000
LEVERAGE = 5
WINDOW_SECONDS = 480

def proj_pnl(signal_price, close, direction):
    sz = (MARGIN * LEVERAGE) / signal_price
    if direction == "LONG":
        return round((close - signal_price) * sz, 2)
    else:
        return round((signal_price - close) * sz, 2)

async def main():
    sb = create_client(SUPABASE_URL, SUPABASE_KEY)
    client = HLClient()
    await client.init()

    # Fetch all J1H_DISCARDED from alert_log
    # for HL venue today
    rows = sb.table("alert_log")\
        .select("*")\
        .eq("outcome", "J1H_DISCARDED")\
        .eq("venue", "HL")\
        .gte("created_at",
             "2026-07-03T00:00:00+00:00")\
        .order("created_at", desc=False)\
        .execute()

    alerts = rows.data
    print(f"Found {len(alerts)} J1H_DISCARDED "
          f"alerts for HL today")
    print()

    print(f"{'PAIR':>8} {'DIR':>6} {'TIME':>6} "
          f"{'J1H':>7} {'SIG_PX':>10} "
          f"{'PEAK_PNL':>10} {'AT':>6} "
          f"{'FINAL_PNL':>10} {'RESULT':>15}")
    print("-" * 85)

    total_left = 0.0

    for a in alerts:
        pair = a["pair"]
        direction = a["direction"]
        signal_price = float(a["signal_price"])
        j1h = float(a["j1h_at_signal"] or 0)
        created_at = a["created_at"]

        # Convert created_at to unix ts
        from datetime import datetime, timezone
        dt = datetime.fromisoformat(
            created_at.replace("+00:00", "")
        ).replace(tzinfo=timezone.utc)
        signal_ts = int(dt.timestamp())

        # Fetch 1M candles for window + warmup
        start_ms = (signal_ts - 1800) * 1000
        end_ms = (signal_ts + WINDOW_SECONDS
                  + 300) * 1000

        candles = await client.get_candles(
            pair, "1m", 200)

        # Filter to window
        window = [c for c in candles
                  if c["time"] >= signal_ts * 1000
                  and c["time"] <= (signal_ts +
                  WINDOW_SECONDS) * 1000]

        if not window:
            print(f"{pair:>8} {direction:>6} "
                  f"{dt.strftime('%H:%M'):>6} "
                  f"{j1h:>7.1f} "
                  f"{signal_price:>10.4f} "
                  f"{'NO CANDLES':>10}")
            continue

        peak_pnl = 0.0
        peak_time = ""
        final_pnl = 0.0

        for c in window:
            hi_pnl = proj_pnl(
                signal_price,
                c["high"] if direction == "LONG"
                else c["low"],
                direction)
            cl_pnl = proj_pnl(
                signal_price, c["close"],
                direction)
            if hi_pnl > peak_pnl:
                peak_pnl = hi_pnl
                from datetime import datetime
                peak_time = datetime.fromtimestamp(
                    c["time"] / 1000,
                    tz=timezone.utc
                ).strftime("%H:%M")
            final_pnl = cl_pnl

        result = "WINNER" if peak_pnl > 20 \
            else "LOSER" if peak_pnl <= 0 \
            else "SMALL"

        if peak_pnl > 0:
            total_left += peak_pnl

        time_str = dt.strftime("%H:%M")
        print(f"{pair:>8} {direction:>6} "
              f"{time_str:>6} {j1h:>7.1f} "
              f"{signal_price:>10.4f} "
              f"{peak_pnl:>+10.2f} "
              f"{peak_time:>6} "
              f"{final_pnl:>+10.2f} "
              f"{result:>15}")

        await asyncio.sleep(0.2)

    print()
    print(f"Total hypothetical left on table "
          f"(HL J1H_DISCARDED): "
          f"+${total_left:.2f}")
    print("Done.")

asyncio.run(main())
