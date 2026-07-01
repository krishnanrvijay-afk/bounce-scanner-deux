import requests
from datetime import datetime, timezone

BASE = "https://contract.mexc.com/api/v1/contract/kline"

def fetch_klines(symbol, interval, start, end):
    r = requests.get(
        f"{BASE}/{symbol}",
        params={"interval": interval,
                "start": start,
                "end": end},
        timeout=15)
    r.raise_for_status()
    d = r.json()
    if not d.get("success"):
        raise ValueError(str(d)[:120])
    raw = d["data"]
    out = []
    for i in range(len(raw["time"])):
        out.append({
            "t": int(raw["time"][i]),
            "c": float(raw["close"][i]),
            "h": float(raw["high"][i]),
            "l": float(raw["low"][i]),
        })
    return sorted(out, key=lambda x: x["t"])

def calc_kdj(candles, n=9):
    K, D = 50.0, 50.0
    result = []
    for i, c in enumerate(candles):
        w = candles[max(0,i-n+1):i+1]
        hi = max(x["h"] for x in w)
        lo = min(x["l"] for x in w)
        rng = hi - lo
        rsv = (c["c"]-lo)/rng*100 if rng>0 else 50.0
        K = (2/3)*K + (1/3)*rsv
        D = (2/3)*D + (1/3)*K
        result.append(round(3*K-2*D, 2))
    return result

def fmt(ts):
    return datetime.fromtimestamp(
        ts, tz=timezone.utc
    ).strftime("%H:%M")

def analyze(label, symbol, direction,
            entry_ts, close_ts,
            entry_price, j1h_entry,
            loss_dollars):
    warmup = 6 * 3600
    c1h = fetch_klines(symbol, "Min60",
        entry_ts - warmup,
        close_ts + 3600)
    j1h_vals = calc_kdj(c1h)

    trade_candles = [
        (c, j1h_vals[i])
        for i, c in enumerate(c1h)
        if c["t"] >= (entry_ts - 3600)
        and c["t"] <= close_ts
    ]

    if not trade_candles:
        print(f"\n{label}: no candles")
        return

    j1h_peak = None
    j1h_peak_time = None
    j1h_at_entry_cross = None
    decay_at_entry_cross = None

    print(f"\n{'='*64}")
    print(f"  {label}")
    print(f"  {symbol} {direction} "
          f"j1h_entry={j1h_entry:.1f} "
          f"loss=${loss_dollars}")
    print(f"  Entry: {fmt(entry_ts)} UTC  "
          f"Close: {fmt(close_ts)} UTC")
    print(f"{'='*64}")
    print(f"  {'TIME':>5}  {'J1H':>7}  "
          f"{'PEAK':>7}  {'DECAY':>7}  "
          f"NOTE")
    print(f"  {'-'*52}")

    for c, j1h in trade_candles:
        is_entry = (c["t"] >= entry_ts
            and c["t"] < entry_ts + 3600)

        if direction == "LONG":
            if (j1h_peak is None or
                    j1h > j1h_peak):
                j1h_peak = j1h
                j1h_peak_time = c["t"]
        else:
            if (j1h_peak is None or
                    j1h < j1h_peak):
                j1h_peak = j1h
                j1h_peak_time = c["t"]

        if direction == "LONG":
            decay = ((j1h_peak - j1h)
                     if j1h_peak else 0)
            price_adverse = (
                entry_price and
                c["c"] < entry_price)
        else:
            decay = ((j1h - j1h_peak)
                     if j1h_peak else 0)
            price_adverse = (
                entry_price and
                c["c"] > entry_price)

        if (price_adverse and
                j1h_at_entry_cross is None
                and j1h_peak is not None):
            j1h_at_entry_cross = j1h
            decay_at_entry_cross = decay

        note = ""
        if is_entry:
            note = "ENTRY CANDLE"
        if (j1h_peak_time and
                c["t"] == j1h_peak_time):
            note = "J1H PEAK"
        if (price_adverse and
                j1h_at_entry_cross == j1h
                and decay_at_entry_cross
                == decay and not note):
            note = "PRICE CROSSED ENTRY"

        print(f"  {fmt(c['t']):>5}  "
              f"{j1h:>7.1f}  "
              f"{j1h_peak or 0:>7.1f}  "
              f"{decay:>7.1f}  "
              f"{note}")

    print(f"\n  J1H at entry:      "
          f"{j1h_entry:.1f}")
    if j1h_peak:
        print(f"  J1H peak:          "
              f"{j1h_peak:.1f} "
              f"at {fmt(j1h_peak_time)}")
    if j1h_at_entry_cross is not None:
        print(f"  J1H when crossed   "
              f"back through entry: "
              f"{j1h_at_entry_cross:.1f}")
        print(f"  J1H decay at that  "
              f"moment: "
              f"{decay_at_entry_cross:.1f} pts")
    else:
        print(f"  Price never crossed"
              f" back through entry")

def ts(y, mo, d, h, mi):
    return int(datetime(y, mo, d, h, mi,
        tzinfo=timezone.utc).timestamp())

TRADES = [
    ("NEAR_USDT LONG MFE 0.30R -$139",
     "NEAR_USDT", "LONG",
     ts(2026,7,1,8,2),
     ts(2026,7,1,8,23),
     None, 55.0, -139.51),
    ("ZEC_USDT LONG MFE 0.23R -$125",
     "ZEC_USDT", "LONG",
     ts(2026,7,1,8,4),
     ts(2026,7,1,8,23),
     None, 45.91, -125.51),
    ("XRP_USDT LONG MFE 0.15R -$110",
     "XRP_USDT", "LONG",
     ts(2026,7,1,7,28),
     ts(2026,7,1,8,34),
     None, 69.45, -110.35),
    ("LTC_USDT LONG MFE 0.18R -$111",
     "LTC_USDT", "LONG",
     ts(2026,7,1,7,29),
     ts(2026,7,1,8,0),
     None, 68.47, -111.68),
    ("SOL_USDT SHORT MFE 0.27R -$110",
     "SOL_USDT", "SHORT",
     ts(2026,7,1,3,17),
     ts(2026,7,1,3,36),
     None, 90.56, -110.68),
    ("BTC_USDT SHORT MFE 0.12R -$110",
     "BTC_USDT", "SHORT",
     ts(2026,7,1,3,18),
     ts(2026,7,1,3,52),
     None, 99.94, -110.18),
]

print("Fetching J1H candles for "
      f"{len(TRADES)} KILL trades...")

for t in TRADES:
    try:
        analyze(*t)
    except Exception as e:
        print(f"\n{t[0]}: ERROR {e}")

print("\nDone.")
