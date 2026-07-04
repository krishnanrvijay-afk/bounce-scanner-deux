import asyncio
import httpx
from datetime import datetime, timezone

HL_API_URL = "https://api.hyperliquid.xyz/info"

SYMBOL     = "@8"
ENTRY_PX   = 0.003472
EXIT_PX    = 0.003400
ENTRY_TS   = 1751582024  # 2026-07-03 17:33:44 UTC
EXIT_TS    = 1751589960  # 2026-07-03 19:46:00 UTC
AFTER_TS   = EXIT_TS + 3600
MARGIN     = 5000
LEVERAGE   = 5

def ts_to_et(ts):
    return datetime.fromtimestamp(
        ts, tz=timezone.utc
    ).strftime("%H:%M")

def pnl(entry, price, margin=MARGIN, lev=LEVERAGE):
    sz = (margin * lev) / entry
    return round((price - entry) * sz, 2)

async def fetch_candles(symbol, interval,
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
    async with httpx.AsyncClient(timeout=30.0) as c:
        r = await c.post(HL_API_URL, json=payload)
        r.raise_for_status()
        data = r.json()
    if not isinstance(data, list):
        raise ValueError(f"Unexpected response: "
                         f"{str(data)[:80]}")
    out = []
    for c in data:
        out.append({
            "t": int(c.get("t", 0)) // 1000,
            "o": float(c.get("o", 0)),
            "h": float(c.get("h", 0)),
            "l": float(c.get("l", 0)),
            "c": float(c.get("c", 0)),
        })
    return sorted(out, key=lambda x: x["t"])

def calc_kdj(candles, n=9):
    K, D = 50.0, 50.0
    result = []
    for i, c in enumerate(candles):
        w = candles[max(0, i-n+1):i+1]
        hi = max(x["h"] for x in w)
        lo = min(x["l"] for x in w)
        rng = hi - lo
        rsv = ((c["c"]-lo)/rng*100
               if rng > 0 else 50.0)
        K = (2/3)*K + (1/3)*rsv
        D = (2/3)*D + (1/3)*K
        result.append(round(3*K-2*D, 2))
    return result

async def main():
    print("Fetching @8 candles from HL...")

    # Warmup: 2h before entry for accurate KDJ
    warm_start = (ENTRY_TS - 7200) * 1000
    after_end  = AFTER_TS * 1000

    c1m_warm, c5m_warm, c1h_warm = await asyncio.gather(
        fetch_candles(SYMBOL, "1m",
                      warm_start, after_end),
        fetch_candles(SYMBOL, "5m",
                      warm_start, after_end),
        fetch_candles(SYMBOL, "1h",
                      warm_start, after_end),
    )

    j1m = calc_kdj(c1m_warm)
    j5m = calc_kdj(c5m_warm)
    j1h = calc_kdj(c1h_warm)

    j1m_map = {c["t"]: j1m[i]
               for i, c in enumerate(c1m_warm)}
    j5m_map = {c["t"]: j5m[i]
               for i, c in enumerate(c5m_warm)}
    j1h_map = {c["t"]: j1h[i]
               for i, c in enumerate(c1h_warm)}

    def get_j(j_map, t, bucket_s):
        b = (t // bucket_s) * bucket_s
        for d in [0, -bucket_s, bucket_s]:
            if b+d in j_map:
                return j_map[b+d]
        return None

    # Filter to trade window + 60m after
    trade_c = [c for c in c1m_warm
               if c["t"] >= ENTRY_TS - 60
               and c["t"] <= AFTER_TS]

    print(f"\n{'='*80}")
    print(f"  @8 LONG — KILL EXIT FORENSIC")
    print(f"  Entry: {ENTRY_PX}  Exit: {EXIT_PX}"
          f"  -$370.08")
    print(f"  J15M at entry: 7.9  "
          f"J1H at entry: 1.6")
    print(f"  Duration: 2h 13m")
    print(f"{'='*80}")
    print(f"  {'TIME':>5}  "
          f"{'CLOSE':>9}  "
          f"{'J1M':>7}  "
          f"{'J5M':>7}  "
          f"{'J1H':>7}  "
          f"{'PNL':>9}  "
          f"NOTE")
    print(f"  {'-'*72}")

    peak_pnl = None
    peak_ts  = None
    post_exit_high = None
    post_exit_low  = None

    for c in trade_c:
        cpnl   = pnl(ENTRY_PX, c["c"])
        p_hi   = pnl(ENTRY_PX, c["h"])

        if peak_pnl is None or p_hi > peak_pnl:
            peak_pnl = p_hi
            peak_ts  = c["t"]

        j1m_v = get_j(j1m_map, c["t"], 60)
        j5m_v = get_j(j5m_map, c["t"], 300)
        j1h_v = get_j(j1h_map, c["t"], 3600)

        is_after = c["t"] > EXIT_TS
        if is_after:
            if (post_exit_high is None
                    or c["h"] > post_exit_high):
                post_exit_high = c["h"]
            if (post_exit_low is None
                    or c["l"] < post_exit_low):
                post_exit_low = c["l"]

        note = ""
        if abs(c["t"] - ENTRY_TS) < 90:
            note = "ENTRY"
        if abs(c["t"] - EXIT_TS) < 90:
            note = "★ KILL EXIT"
        if is_after and not note:
            note = "POST-EXIT"

        print(f"  {ts_to_et(c['t']):>5}"
              f"  {c['c']:9.6f}"
              f"  {j1m_v or 0:7.1f}"
              f"  {j5m_v or 0:7.1f}"
              f"  {j1h_v or 0:7.1f}"
              f"  {cpnl:9.2f}"
              f"  {note}")

    print(f"\n{'='*80}")
    print(f"  @8 LONG FORENSIC SUMMARY")
    print(f"{'='*80}")
    print(f"  Entry price:    {ENTRY_PX}")
    print(f"  Exit price:     {EXIT_PX}")
    print(f"  Exit reason:    KILL")
    print(f"  Final PnL:      -$370.08")
    if peak_pnl is not None:
        print(f"  Peak PnL hi:    "
              f"+${peak_pnl:.2f} at "
              f"{ts_to_et(peak_ts)}")
    else:
        print(f"  Peak PnL hi:    never positive")
    if post_exit_high:
        post_hi_pnl = pnl(ENTRY_PX, post_exit_high)
        print(f"\n  AFTER EXIT (60 min):")
        print(f"  Price high:     "
              f"{post_exit_high:.6f}"
              f" (would have been "
              f"+${post_hi_pnl:.2f})")
    if post_exit_low:
        post_lo_pnl = pnl(ENTRY_PX, post_exit_low)
        print(f"  Price low:      "
              f"{post_exit_low:.6f}"
              f" (would have been "
              f"{post_lo_pnl:.2f})")
    print("\nDone.")

asyncio.run(main())
