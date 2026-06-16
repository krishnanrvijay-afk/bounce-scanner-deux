import asyncio
import os
import time
import httpx
from typing import Optional
from config import HL_API_URL, PAPER_MODE


class HLClient:
    def __init__(self):
        self._http = httpx.AsyncClient(timeout=15.0)
        self._sem  = asyncio.Semaphore(4)
        self._paper_mode = PAPER_MODE
        self._exchange = None
        self._info = None
        self._wallet_address = os.getenv("HL_WALLET_ADDRESS", "")

        if not self._paper_mode:
            self._init_live_client()

    def _init_live_client(self):
        try:
            from hyperliquid.exchange import Exchange
            from hyperliquid.info import Info
            from eth_account import Account

            private_key = os.getenv("HL_PRIVATE_KEY", "")
            if not private_key:
                raise ValueError("HL_PRIVATE_KEY not set — required for live trading")

            acct = Account.from_key(private_key)
            account_address = os.getenv("HL_WALLET_ADDRESS", "")
            if not account_address:
                raise ValueError("HL_WALLET_ADDRESS not set -- required for live trading")
            self._wallet_address = account_address
            self._info = Info(HL_API_URL)
            self._exchange = Exchange(acct, HL_API_URL, account_address=account_address)
        except Exception as e:
            print(f"[HLClient] Failed to init live client: {e}")

    async def _post(self, payload: dict) -> dict | list:
        async with self._sem:
            resp = await self._http.post(HL_API_URL, json=payload)
            if resp.status_code == 429:
                coin = payload.get("req", {}).get("coin", payload.get("type", "?"))
                print(f"[RATE LIMIT] {coin} 429 — backing off 3s")
                await asyncio.sleep(3)
                resp = await self._http.post(HL_API_URL, json=payload)
                if resp.status_code == 429:
                    print(f"[RATE LIMIT] {coin} 429 on retry — giving up")
                    return None
            resp.raise_for_status()
            return resp.json()

    async def get_price(self, symbol: str) -> Optional[float]:
        try:
            data = await self._post({"type": "allMids"})
            raw = data.get(symbol)
            if raw is None:
                return None
            return float(raw)
        except Exception as e:
            print(f"[HLClient] get_price({symbol}) error: {e}")
            return None

    async def get_all_prices(self) -> dict[str, float]:
        try:
            data = await self._post({"type": "allMids"})
            return {k: float(v) for k, v in data.items() if v is not None}
        except Exception as e:
            print(f"[HLClient] get_all_prices error: {e}")
            return {}

    async def get_candles(self, symbol: str, interval: str, limit: int = 50) -> list[dict]:
        try:
            now_ms = int(time.time() * 1000)
            interval_ms_map = {
                "1m":  60_000,
                "5m":  300_000,
                "15m": 900_000,
                "1h":  3_600_000,
                "4h":  14_400_000,
                "1d":  86_400_000,
            }
            interval_ms = interval_ms_map.get(interval, 300_000)
            start_ms = now_ms - interval_ms * (limit + 5)

            payload = {
                "type": "candleSnapshot",
                "req": {
                    "coin":      symbol,
                    "interval":  interval,
                    "startTime": start_ms,
                    "endTime":   now_ms,
                },
            }
            data = await self._post(payload)

            candles = []
            for c in data:
                candles.append({
                    "time":   c.get("t", 0),
                    "open":   float(c.get("o", 0)),
                    "high":   float(c.get("h", 0)),
                    "low":    float(c.get("l", 0)),
                    "close":  float(c.get("c", 0)),
                    "volume": float(c.get("v", 0)),
                })

            candles.sort(key=lambda x: x["time"])
            return candles[-limit:] if len(candles) > limit else candles
        except Exception as e:
            print(f"[HLClient] get_candles({symbol}, {interval}) error: {e}")
            return []

    async def get_orderbook(self, symbol: str, depth: int = 20) -> dict:
        try:
            data = await self._post({"type": "l2Book", "coin": symbol})
            levels = data.get("levels", [[], []])
            bids = levels[0] if len(levels) > 0 else []
            asks = levels[1] if len(levels) > 1 else []

            def parse_level(lvl):
                if isinstance(lvl, dict):
                    return {"px": float(lvl.get("px", 0)), "sz": float(lvl.get("sz", 0))}
                return {"px": 0.0, "sz": 0.0}

            return {
                "bids": [parse_level(b) for b in bids[:depth]],
                "asks": [parse_level(a) for a in asks[:depth]],
            }
        except Exception as e:
            print(f"[HLClient] get_orderbook({symbol}) error: {e}")
            return {"bids": [], "asks": []}

    async def get_all_price_changes(self, symbols: list[str]) -> dict[str, float]:
        """Returns dict of symbol → 24h pct change, computed from prevDayPx vs markPx."""
        try:
            data = await self._post({"type": "metaAndAssetCtxs"})
            meta       = data[0]
            asset_ctxs = data[1]
            universe   = meta.get("universe", [])
            sym_set    = set(symbols)
            result: dict[str, float] = {}
            for i, asset in enumerate(universe):
                name = asset.get("name")
                if name not in sym_set or i >= len(asset_ctxs):
                    continue
                ctx  = asset_ctxs[i]
                prev = float(ctx.get("prevDayPx") or 0)
                mark = float(ctx.get("markPx")    or 0)
                if prev > 0 and mark > 0:
                    result[name] = round((mark - prev) / prev * 100, 2)
            return result
        except Exception as e:
            print(f"[HLClient] get_all_price_changes error: {e}")
            return {}

    async def get_funding_rate(self, symbol: str) -> Optional[float]:
        try:
            data = await self._post({"type": "metaAndAssetCtxs"})
            meta = data[0]
            asset_ctxs = data[1]
            universe = meta.get("universe", [])

            for i, asset in enumerate(universe):
                if asset.get("name") == symbol:
                    if i < len(asset_ctxs):
                        fr = asset_ctxs[i].get("funding", None)
                        if fr is not None:
                            return float(fr)
            return None
        except Exception as e:
            print(f"[HLClient] get_funding_rate({symbol}) error: {e}")
            return None

    async def open_position(
        self,
        symbol: str,
        direction: str,
        margin_usdc: float,
        leverage: int,
        entry_price: Optional[float] = None,
        order_type: str = "MARKET",
        limit_px: Optional[float] = None,
        sl_price: Optional[float] = None,
    ) -> dict:
        is_buy = direction.upper() == "LONG"

        if self._paper_mode:
            price = entry_price or limit_px or await self.get_price(symbol) or 0.0
            size  = (margin_usdc * leverage) / price if price > 0 else 0.0
            if order_type == "LIMIT" and limit_px:
                return {
                    "status":    "pending",
                    "paper":     True,
                    "order_id":  f"paper-{int(time.time())}",
                    "symbol":    symbol,
                    "direction": direction,
                    "limit_px":  limit_px,
                    "size":      size,
                    "margin":    margin_usdc,
                    "leverage":  leverage,
                    "timestamp": int(time.time()),
                }
            return {
                "status":      "ok",
                "paper":       True,
                "symbol":      symbol,
                "direction":   direction,
                "entry_price": price,
                "size":        size,
                "margin":      margin_usdc,
                "leverage":    leverage,
                "timestamp":   int(time.time()),
            }

        try:
            if not self._exchange:
                return {"status": "error", "msg": "Live client not initialized"}

            price = entry_price or await self.get_price(symbol) or 0.0
            size  = round((margin_usdc * leverage) / price, 6) if price > 0 else 0.0

            if order_type == "LIMIT" and limit_px:
                exec_px          = limit_px
                entry_order_type = {"limit": {"tif": "Gtc"}}
            else:
                exec_px          = price
                entry_order_type = {"limit": {"tif": "Ioc"}}

            orders = [
                {
                    "coin":        symbol,
                    "is_buy":      is_buy,
                    "sz":          size,
                    "limit_px":    exec_px,
                    "order_type":  entry_order_type,
                    "reduce_only": False,
                }
            ]

            if sl_price:
                orders.append({
                    "coin":        symbol,
                    "is_buy":      not is_buy,
                    "sz":          size,
                    "limit_px":    sl_price,
                    "order_type":  {
                        "trigger": {
                            "isMarket":  True,
                            "triggerPx": sl_price,
                            "tpsl":      "sl",
                        }
                    },
                    "reduce_only": True,
                })

            try:
                order_result = self._exchange.bulk_orders(orders)
            except AttributeError:
                order_result = self._exchange.order(
                    symbol, is_buy, size, exec_px, entry_order_type
                )

            return {
                "status":      "ok",
                "paper":       False,
                "symbol":      symbol,
                "direction":   direction,
                "entry_price": exec_px,
                "size":        size,
                "margin":      margin_usdc,
                "leverage":    leverage,
                "timestamp":   int(time.time()),
                "raw":         order_result,
            }
        except Exception as e:
            return {"status": "error", "msg": str(e)}

    async def cancel_order(self, coin: str, order_id: str) -> dict:
        if self._paper_mode:
            print(f"[HLClient] cancel_order paper: {coin} {order_id}")
            return {"status": "ok", "paper": True}
        try:
            if not self._exchange:
                return {"status": "error", "msg": "Live client not initialized"}
            result = self._exchange.cancel(coin, int(order_id))
            return {"status": "ok", "raw": result}
        except Exception as e:
            print(f"[HLClient] cancel_order({coin}, {order_id}) error: {e}")
            return {"status": "error", "msg": str(e)}

    async def close_position(self, symbol: str, direction: str, size: float) -> dict:
        if self._paper_mode:
            price = await self.get_price(symbol) or 0.0
            return {
                "status":      "ok",
                "paper":       True,
                "symbol":      symbol,
                "close_price": price,
                "timestamp":   int(time.time()),
            }

        try:
            if not self._exchange:
                return {"status": "error", "msg": "Live client not initialized"}

            is_buy = direction.upper() == "SHORT"
            price = await self.get_price(symbol) or 0.0
            order_result = self._exchange.order(
                symbol,
                is_buy,
                abs(size),
                price,
                {"limit": {"tif": "Ioc"}},
            )
            return {
                "status":      "ok",
                "paper":       False,
                "symbol":      symbol,
                "close_price": price,
                "timestamp":   int(time.time()),
                "raw":         order_result,
            }
        except Exception as e:
            return {"status": "error", "msg": str(e)}

    async def close(self):
        await self._http.aclose()
