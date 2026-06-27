import os
from datetime import datetime, timezone

HL_API_URL = "https://api.hyperliquid.xyz/info"

# -- Supabase persistence -------------------------------------------------------
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")

PAIRS = ["DOGE", "SUI", "BTC", "ETH", "NEAR", "XRP", "SOL", "WIF", "AVAX", "TON", "@107", "@8", "@1", "LTC", "ADA"]

SCAN_INTERVAL_SECONDS  = 30
PRICE_INTERVAL_SECONDS = 8
PAPER_MODE             = True

# -- Live trading safety --------------------------------------------------------
# When PAPER_MODE is False and LIVE_MANUAL_ENTRY_ONLY is True, the scanner will
# never automatically open a live exchange position. Alerts fire and the overlay
# updates normally but all live trade entry requires deliberate human action via
# the symbol overlay Open HL or Open MEXC buttons. SL and TP exits continue to
# execute automatically once a trade is open. This is the required mode for live
# trading. Only set LIVE_MANUAL_ENTRY_ONLY to False if you explicitly want fully
# automated live entry on every signal.
LIVE_MANUAL_ENTRY_ONLY = True

J15M_SHORT_GATE  = 80
J15M_LONG_GATE   = 20
J1H_SHORT_MIN    = 60
J1H_SHORT_MAX    = 89   # Real trading ceiling — data: SHORT J1H 90-100 65.5% WR -$1,513
J1H_LONG_MIN     = 0    # Bounds validator — guards negative J1H calculation edge cases. Not a trading gate.
J1H_LONG_MAX     = 59   # Trading ceiling for LONGs — data: J1H 60+ LONG 10% WR -$399

RSI15M_SHORT_MIN = 60
RSI15M_LONG_MAX  = 40

DEPTH_GATE_PCT   = 55

ATR_SL_MULTIPLIER = 1.0

TP1_R                = 1.0
TP1_CLOSE_PCT        = 0.70        # Trailblazer: close 70% at TP1 (runner 30% stays open)
TP2_R                = 1.2         # p75 MFE = 1.1-1.2R. 25% of winners reach 1.2R vs 10% reaching 1.5R.
TRAIL_ATR_MULTIPLIER = 0.5         # trail_stop = trail_best  (atr15m  TRAIL_ATR_MULTIPLIER)

LEVERAGE_HIGH = 10
LEVERAGE_MID  = 5
LEVERAGE_LOW  = 5

CONSECUTIVE_LOSS_STOP = 3
DAILY_LOSS_LIMIT      = -800.0

MARGIN_PER_TRADE = 2000.0
MARGIN_HARD_CAP  = 25000.0

ADX_MIN_LONG  = 20  # data: LONG ADX 0-19: 119 trades -$2,391
ADX_MIN_SHORT = 0   # data: SHORT ADX 0-14: 21 trades +$493. SHORTs profitable at all ADX levels

SESSION_FILTER_ENABLED = False
PLACE_EXCHANGE_SL      = True

MIN_SL_PCT: dict = {
    "BTC":  0.008,
    "ETH":  0.006,
    "SOL":  0.008,
    "XRP":  0.007,
    "DOGE": 0.007,
    "SUI":  0.010,
    "NEAR": 0.010,
    "LINK": 0.008,
    "ARB":  0.012,
    "ZEC":  0.030,
}
MIN_SL_PCT_DEFAULT = 0.010
# Per-pair per-session Sentinel minimum peak thresholds
# Derived from p25 winner MFE per pair x $10k notional
# ASIA scaled to 60% -- winner peaks smaller in ASIA session
# Reviewed and updated first Monday of each month
SENTINEL_MIN_PEAK_USD: dict = {
    # (symbol, session): minimum peak USD
    ("NEAR",  "ASIA"): 7.00,  ("NEAR",  "EU"): 7.00,  ("NEAR",  "US"): 7.00,
    ("@107",  "ASIA"): 19.00, ("@107",  "EU"): 32.00, ("@107",  "US"): 24.00,
    ("WIF",   "ASIA"): 17.00, ("WIF",   "EU"): 28.00, ("WIF",   "US"): 21.00,
    ("AVAX",  "ASIA"): 17.00, ("AVAX",  "EU"): 29.00, ("AVAX",  "US"): 22.00,
    ("SUI",   "ASIA"): 17.00, ("SUI",   "EU"): 29.00, ("SUI",   "US"): 22.00,
    ("DOGE",  "ASIA"): 15.00, ("DOGE",  "EU"): 25.00, ("DOGE",  "US"): 19.00,
    ("SOL",   "ASIA"): 36.00, ("SOL",   "EU"): 60.00, ("SOL",   "US"): 45.00,
    ("XRP",   "ASIA"): 30.00, ("XRP",   "EU"): 50.00, ("XRP",   "US"): 38.00,
    ("ETH",   "ASIA"): 19.00, ("ETH",   "EU"): 31.00, ("ETH",   "US"): 23.00,
    ("BTC",   "ASIA"): 22.00, ("BTC",   "EU"): 36.00, ("BTC",   "US"): 27.00,
    ("LTC",   "ASIA"): 18.00, ("LTC",   "EU"): 30.00, ("LTC",   "US"): 23.00,
    ("ADA",   "ASIA"): 18.00, ("ADA",   "EU"): 30.00, ("ADA",   "US"): 23.00,
}
SENTINEL_MIN_PEAK_USD_DEFAULT: float = 18.00  # ASIA-safe default
