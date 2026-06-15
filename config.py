import os
from datetime import datetime, timezone

HL_API_URL = "https://api.hyperliquid.xyz/info"

# -- Supabase persistence -------------------------------------------------------
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")

PAIRS = ["DOGE", "SUI", "BTC", "LINK", "ETH", "NEAR", "XRP", "SOL", "WIF", "AVAX", "HYPE", "ZEC", "TON", "@107", "@8", "@1"]

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
J1H_LONG_MAX     = 40

RSI15M_SHORT_MIN = 60
RSI15M_LONG_MAX  = 40

DEPTH_GATE_PCT   = 55

ATR_SL_MULTIPLIER = 1.0

TP1_R                = 1.0
TP1_CLOSE_PCT        = 0.70        # Trailblazer: close 70% at TP1 (runner 30% stays open)
TP2_R                = 1.5         # still used for tp2_price alert calc; exit replaced by Trailblazer
TRAIL_ATR_MULTIPLIER = 0.5         # trail_stop = trail_best  (atr15m  TRAIL_ATR_MULTIPLIER)

LEVERAGE_HIGH = 10
LEVERAGE_MID  = 7
LEVERAGE_LOW  = 5

COOLDOWN_SECONDS      = 1800
CONSECUTIVE_LOSS_STOP = 3
DAILY_LOSS_LIMIT      = -800.0

MARGIN_PER_TRADE = 2000.0
MARGIN_HARD_CAP  = 25000.0

ADX_FADE_MAX = 60

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
