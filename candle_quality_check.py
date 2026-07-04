import asyncio
import sys
sys.path.insert(0, '.')
from hl_client import HLClient
from scanner import _compute_kdj

PAIRS = [
    "DOGE", "SUI", "BTC", "ETH", "NEAR", "XRP",
    "SOL", "WIF", "AVAX", "@107", "@1", "LTC", "ADA"
]

async def main():
    client = HLClient()
    await client.init()

    print(f"{'PAIR':>8} {'TF':>4} {'COUNT':>7} "
          f"{'J':>8} {'FLAG':>20}")
    print("-" * 55)

    for pair in PAIRS:
        for tf, label in [
            ("5m",  "5M"),
            ("15m", "15M"),
            ("1h",  "1H")
        ]:
            candles = await client.get_candles(
                pair, tf, 100)
            count = len(candles)
            _, _, j = _compute_kdj(candles)
            j_val = round(j, 2)

            flag = ""
            if count == 0:
                flag = "*** NO CANDLES ***"
            elif count < 20:
                flag = f"*** LOW COUNT {count} ***"
            elif abs(j_val - 50.0) < 1.0:
                flag = "*** J=50 SEED STUCK ***"
            elif j_val < 0 or j_val > 110:
                flag = f"*** J OUT OF RANGE ***"

            print(f"{pair:>8} {label:>4} "
                  f"{count:>7} {j_val:>8} "
                  f"{flag}")

    print("\nDone.")

asyncio.run(main())
