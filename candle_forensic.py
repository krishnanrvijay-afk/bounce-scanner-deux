#!/usr/bin/env python3
"""
candle_forensic.py — HL LTC SHORT 7/17/2026 minute-by-minute gate trace
Repo: bounce-scanner-deux (HL)  krishnanrvijay-afk

Fetches HL 1M candles for LTC 11:50-14:55 ET (15:50-18:55 UTC) on 7/17/2026.
Replays every scanner exit gate at each candle close.

Gate columns
  CPNL         = (44.8405 - close) * 557.54
  BE_X         = "ARM" on first candle whose LOW <= 44.7957, "yes" after, "-" before
  EXTREME      = running min(low) — the ungated price tracker
  PEAK_A       = peak_pnl_usd: running max(CPNL), updated at most ONCE per candle
                 when be_armed=True; implements the same-candle guard from PEAK_DECAY_10
  SLP40        = "FIRE" when close >= 45.1095 (be_armed=True,  0.40x multiplier)
  SLP45        = "FIRE" when close >= 45.0871 (be_armed=False, 0.45x multiplier)
  PD10         = "FIRE" when be_armed AND PEAK_A >= 57.50 AND CPNL < PEAK_A*0.90
                 AND current candle != candle where PEAK_A was last set (candle guard)
  AR           = "FIRE" when be_armed=True AND CPNL < 0
  TAE          = "FIRE" when elapsed >= 600s AND CPNL <= 0
"""

import json
import datetime
import urllib.request
import sys

# ── Trade constants (verbatim from log) ──────────────────────────────────────
ENTRY       = 44.8405
SL          = 45.2889
TP1         = 44.3921
TP2         = 44.3024
SIZE        = 557.54
NOTIONAL    = 25_000.0
DR          = 250.00
BE_PRICE    = 44.7957      # entry * 0.999
SL_DIST_PCT = 0.01
SENTINEL    = 57.50        # NOTIONAL * 0.0023  (LTC US sentinel floor)

SLP40_THR   = 45.1095      # be_armed=True  (SL consumed 60%, multiplier 0.40)
SLP45_THR   = 45.0871      # be_armed=False (SL consumed 55%, multiplier 0.45)

# ── Time setup (EDT = UTC-4) ─────────────────────────────────────────────────
EDT = datetime.timezone(datetime.timedelta(hours=-4))
UTC = datetime.timezone.utc

# Open: 7/17/2026 ~11:55 ET  (close 14:49 minus 2h54m)
OPEN_UTC_S   = datetime.datetime(2026, 7, 17, 15, 55, 0, tzinfo=UTC).timestamp()
START_UTC_MS = int(datetime.datetime(2026, 7, 17, 15, 50, 0, tzinfo=UTC).timestamp() * 1000)
END_UTC_MS   = int(datetime.datetime(2026, 7, 17, 18, 55, 0, tzinfo=UTC).timestamp() * 1000)

# ── Fetch 1M candles from HL ─────────────────────────────────────────────────
ENDPOINT = "https://api.hyperliquid.xyz/info"
body = json.dumps({
    "type": "candleSnapshot",
    "req": {
        "coin":      "LTC",
        "interval":  "1m",
        "startTime": START_UTC_MS,
        "endTime":   END_UTC_MS,
    }
}).encode()

req = urllib.request.Request(
    ENDPOINT, data=body,
    headers={"Content-Type": "application/json"}
)
try:
    resp    = urllib.request.urlopen(req, timeout=30)
    candles = json.load(resp)
except Exception as exc:
    print(f"ERROR fetching candles: {exc}", file=sys.stderr)
    sys.exit(1)

# ── Header ───────────────────────────────────────────────────────────────────
print("=" * 90)
print("HL LTC SHORT  7/17/2026  candle forensic")
print("=" * 90)
print(f"Endpoint : POST {ENDPOINT}")
print(f"Payload  : type=candleSnapshot  coin=LTC  interval=1m  "
      f"startTime={START_UTC_MS}  endTime={END_UTC_MS}")
print(f"Candles  : {len(candles)} returned")
print(f"Window   : 11:50 – 14:55 ET  (15:50 – 18:55 UTC)  7/17/2026")
print()

# ── Gate state ───────────────────────────────────────────────────────────────
be_armed       = False
peak_pnl_usd   = 0.0
extreme        = None
peak_candle_i  = -1   # index of candle where peak was last updated (candle guard)

# for summary
armed_row_i  = None
armed_time   = None
armed_low    = None
peak_time    = None
peak_close_v = None

HDR = (
    "TIME  | CLOSE   | LOW     | HIGH    |    CPNL  | BE_X"
    " | EXTREME  |  PEAK_A  |  SLP40 |  SLP45 |  PD10 |    AR |   TAE"
)
SEP = "─" * len(HDR)
print(HDR)
print(SEP)

rows = []
for i, c in enumerate(candles):
    ts_s    = c["t"] / 1000
    lo      = float(c["l"])
    hi      = float(c["h"])
    cl      = float(c["c"])
    elapsed = ts_s - OPEN_UTC_S   # seconds since trade open

    dt_et = datetime.datetime.fromtimestamp(ts_s, tz=EDT)
    label = dt_et.strftime("%H:%M")

    # Extreme: running min low
    extreme = lo if extreme is None else min(extreme, lo)

    # CPNL at candle close (SHORT: profitable when price falls)
    cpnl = (ENTRY - cl) * SIZE

    # be_armed: flip on first candle where LOW touches or crosses BE_PRICE
    if not be_armed and lo <= BE_PRICE:
        be_armed     = True
        armed_row_i  = i
        armed_time   = label
        armed_low    = lo
        bx           = " ARM"
    elif be_armed:
        bx = " yes"
    else:
        bx = "   -"

    # PEAK_IF_ARMED: update at most once per candle (candle guard via peak_candle_i)
    if be_armed and cpnl > peak_pnl_usd and i != peak_candle_i:
        peak_pnl_usd  = cpnl
        peak_candle_i = i
        peak_time     = label
        peak_close_v  = cl

    # ── Gate evaluations at candle close ─────────────────────────────────────
    slp40 = "FIRE" if cl >= SLP40_THR else "     -"
    slp45 = "FIRE" if cl >= SLP45_THR else "     -"

    # PD10: be_armed, peak >= sentinel, 10% drawdown from peak, candle guard
    pd10 = ("FIRE" if (
                be_armed
                and peak_pnl_usd >= SENTINEL
                and cpnl < peak_pnl_usd * 0.90
                and i != peak_candle_i   # not the same candle peak was set
            ) else "    -")

    # ARMED_REVERSAL: be_armed AND trade in loss at close
    ar  = ("FIRE" if (be_armed and cpnl < 0) else "   -")

    # TIME_ADVERSE_EXIT: age >= 600s AND cpnl <= 0
    tae = ("FIRE" if (elapsed >= 600 and cpnl <= 0) else "   -")

    print(
        f"{label:5s} | {cl:7.4f} | {lo:7.4f} | {hi:7.4f} | {cpnl:+8.2f}"
        f" | {bx:>4s} | {extreme:8.4f} | {peak_pnl_usd:8.2f}"
        f" | {slp40:>6s} | {slp45:>6s} | {pd10:>5s} | {ar:>5s} | {tae:>5s}"
    )

    rows.append({
        "i": i, "label": label, "cl": cl, "lo": lo, "hi": hi,
        "cpnl": cpnl, "be_armed": be_armed, "extreme": extreme,
        "peak": peak_pnl_usd, "elapsed": elapsed,
        "slp40": slp40.strip(), "slp45": slp45.strip(),
        "pd10": pd10.strip(), "ar": ar.strip(), "tae": tae.strip(),
    })

print(SEP)
print()

# ── SUMMARY ──────────────────────────────────────────────────────────────────
print("=" * 90)
print("SUMMARY")
print("=" * 90)

# 1. First be_price cross
print("\n1. First be_price cross:")
if armed_row_i is not None:
    print(f"   {armed_time} ET — low={armed_low:.4f} crossed BE_PRICE {BE_PRICE:.4f}"
          f" → be_armed FLIPPED to True")
else:
    print("   NEVER — no candle low reached BE_PRICE 44.7957 in this window")

# 2. Peak CPNL
print("\n2. Peak cpnl (peak_pnl_usd):")
if peak_time:
    print(f"   {peak_time} ET — peak_pnl_usd=${peak_pnl_usd:.2f}  (close={peak_close_v:.4f})")
    print(f"   = {peak_pnl_usd/DR:.3f}R of dollar_risk ${DR:.2f}")
else:
    print("   No positive peak recorded (be_armed never set or trade always adverse)")

# 3. First PD10
pd10_rows = [r for r in rows if r["pd10"] == "FIRE"]
print("\n3. First PD10 (be_armed + candle guard applied):")
if pd10_rows:
    r = pd10_rows[0]
    print(f"   {r['label']} ET — close={r['cl']:.4f}  cpnl=${r['cpnl']:+.2f}"
          f"  peak=${r['peak']:.2f}")
    print(f"   10%-decay threshold: ${r['peak']*0.90:.2f}  |  cpnl ${r['cpnl']:+.2f} < threshold")
    print(f"   PnL that would have been banked: ${r['cpnl']:+.2f}")
else:
    print("   Never triggered in window")

# 4. First ARMED_REVERSAL
ar_rows = [r for r in rows if r["ar"] == "FIRE"]
print("\n4. First ARMED_REVERSAL (be_armed=True AND cpnl < 0):")
if ar_rows:
    r = ar_rows[0]
    print(f"   {r['label']} ET — close={r['cl']:.4f}  cpnl=${r['cpnl']:+.2f}")
else:
    print("   Never triggered in window")

# 5. First SLP45
slp45_rows = [r for r in rows if r["slp45"] == "FIRE"]
print("\n5. First SLP45 (actual exit — be_armed=False, 0.45× multiplier):")
if slp45_rows:
    r = slp45_rows[0]
    print(f"   {r['label']} ET — close={r['cl']:.4f}  cpnl=${r['cpnl']:+.2f}")
else:
    print("   Threshold 45.0871 not reached in window")

# 6. SLP40 ever hit?
slp40_rows = [r for r in rows if r["slp40"] == "FIRE"]
print(f"\n6. SLP40 threshold (45.1095) — armed trade exit:")
if slp40_rows:
    r = slp40_rows[0]
    print(f"   YES — first at {r['label']} ET, close={r['cl']:.4f}")
else:
    print(f"   NO — close NEVER reached 45.1095 in this window.")
    print(f"   ⟹  An armed trade (be_armed=True, multiplier 0.40) would NOT have")
    print(f"       exited via SL_PROXIMITY at all during this 2h54m window.")

# 7. PnL comparison table
actual = -144.96
pd10_p = pd10_rows[0]["cpnl"] if pd10_rows else None
ar_p   = ar_rows[0]["cpnl"]   if ar_rows   else None

print(f"\n7. PnL comparison:")
print(f"   {'Exit path':<32s} {'PnL':>10s}   {'vs actual':>12s}")
print(f"   {'─'*57}")
print(f"   {'Actual (SL_PROXIMITY, unarmed)':<32s} ${actual:>9.2f}   {'—':>12s}")
if pd10_p is not None:
    delta = pd10_p - actual
    print(f"   {'PD10 (if armed)':<32s} ${pd10_p:>+9.2f}   ${delta:>+11.2f}")
else:
    print(f"   {'PD10 (if armed)':<32s} {'never':>10s}   {'—':>12s}")
if ar_p is not None:
    delta = ar_p - actual
    print(f"   {'ARMED_REVERSAL (if armed)':<32s} ${ar_p:>+9.2f}   ${delta:>+11.2f}")
else:
    print(f"   {'ARMED_REVERSAL (if armed)':<32s} {'never':>10s}   {'—':>12s}")

print()
print("=" * 90)
print("NOTES")
print("─" * 90)
print("• CPNL uses candle close; actual scanner sees every scan tick (8s).")
print("  Intracandle extremes (LOW/HIGH) affect be_armed detection but not CPNL here.")
print("• peak_pnl_usd uses close CPNL sampled once per candle (same-candle guard applied).")
print("  Actual scanner can set a higher peak intracandle if close < low of minute.")
print("• PD10 candle guard: cannot fire in the same candle the peak was set.")
print("• PEAK_GIVEBACK (streak>=3) not shown — requires sub-minute tick data.")
print("=" * 90)
