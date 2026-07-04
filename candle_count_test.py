import asyncio
import sys
sys.path.insert(0, '.')
from hl_client import HLClient

PAIRS = ['BTC', 'SOL', 'ADA', 'AVAX', '@8', '@107',
         'ZEC', 'WIF', 'DOGE', 'NEAR', 'ETH', 'XRP',
         'SUI', 'LTC', 'AVAX', 'TON', 'WIF']

async def main():
    client = HLClient()
    print(f"{'PAIR':>8} {'1H_COUNT':>10} "
          f"{'5M_COUNT':>10} {'15M_COUNT':>10}")
    print("-" * 42)
    for pair in PAIRS:
        c1h = await client.get_candles(pair, "1h", 100)
        c5m = await client.get_candles(pair, "5m", 100)
        c15m = await client.get_candles(pair, "15m", 100)
        print(f"{pair:>8} {len(c1h):>10} "
              f"{len(c5m):>10} {len(c15m):>10}")

asyncio.run(main())
