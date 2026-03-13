import hmac
import hashlib
import time
import aiohttp
import asyncio
import logging
from urllib.parse import urlencode

logger = logging.getLogger(__name__)

TESTNET_BASE = "https://demo-fapi.binance.com"
LIVE_BASE = "https://fapi.binance.com"


class BinanceClient:
    def __init__(self, api_key, api_secret, testnet=True):
        self.api_key = api_key
        self.api_secret = api_secret
        self.base = TESTNET_BASE if testnet else LIVE_BASE
        self.session = None

    def _sign(self, params: dict) -> str:
        query = urlencode(params)
        signature = hmac.new(
            self.api_secret.encode(),
            query.encode(),
            hashlib.sha256
        ).hexdigest()
        return signature

    def _headers(self):
        return {
            "X-MBX-APIKEY": self.api_key,
            "Content-Type": "application/x-www-form-urlencoded"
        }

    async def _get(self, path, params=None, signed=False):
        if params is None:
            params = {}
        if signed:
            params["timestamp"] = int(time.time() * 1000)
            params["signature"] = self._sign(params)
        url = self.base + path
        async with aiohttp.ClientSession() as s:
            async with s.get(url, params=params, headers=self._headers(),
                             timeout=aiohttp.ClientTimeout(total=10)) as r:
                return await r.json()

    async def _post(self, path, params=None):
        if params is None:
            params = {}
        params["timestamp"] = int(time.time() * 1000)
        params["signature"] = self._sign(params)
        url = self.base + path
        async with aiohttp.ClientSession() as s:
            async with s.post(url, data=params, headers=self._headers(),
                              timeout=aiohttp.ClientTimeout(total=10)) as r:
                return await r.json()

    async def _delete(self, path, params=None):
        if params is None:
            params = {}
        params["timestamp"] = int(time.time() * 1000)
        params["signature"] = self._sign(params)
        url = self.base + path
        async with aiohttp.ClientSession() as s:
            async with s.delete(url, params=params, headers=self._headers(),
                                timeout=aiohttp.ClientTimeout(total=10)) as r:
                return await r.json()

    # === 帳戶資訊 ===

    async def get_account(self):
        return await self._get("/fapi/v2/account", signed=True)

    async def get_balance(self):
        """取得 USDT 可用餘額與總餘額"""
        try:
            data = await self.get_account()
        except Exception as e:
            logger.error(f"get_account 例外: {e}")
            return None
        if "assets" not in data:
            logger.error(f"get_balance 失敗，Binance回傳: {str(data)[:300]}")
            return None
        for asset in data["assets"]:
            if asset["asset"] == "USDT":
                return {
                    "total": float(asset["walletBalance"]),
                    "available": float(asset["availableBalance"]),
                    "unrealized_pnl": float(asset["unrealizedProfit"]),
                    "margin_used": float(asset["initialMargin"]),
                    "margin_ratio": float(asset["maintMargin"])
                }
        return None

    async def get_positions(self, symbol=None):
        """取得持倉資訊"""
        params = {}
        if symbol:
            params["symbol"] = symbol
        data = await self._get("/fapi/v2/positionRisk", params, signed=True)
        if isinstance(data, list):
            if symbol:
                return [p for p in data if float(p.get("positionAmt", 0)) != 0]
            return [p for p in data if float(p.get("positionAmt", 0)) != 0]
        return []

    # === 交易所資訊 ===

    async def get_exchange_info(self):
        return await self._get("/fapi/v1/exchangeInfo")

    async def get_symbol_info(self, symbol):
        """取得幣種精度、最大槓桿等資訊"""
        data = await self.get_exchange_info()
        for s in data.get("symbols", []):
            if s["symbol"] == symbol:
                return s
        return None

    async def get_max_leverage(self, symbol):
        """取得幣種最大槓桿"""
        info = await self.get_symbol_info(symbol)
        if info:
            return info.get("leverageBracket", [{}])[0].get("initialLeverage", 20)
        return 20

    async def get_price(self, symbol):
        data = await self._get("/fapi/v1/ticker/price", {"symbol": symbol})
        return float(data["price"]) if "price" in data else None

    async def get_klines(self, symbol, interval="1m", limit=10):
        return await self._get("/fapi/v1/klines", {
            "symbol": symbol,
            "interval": interval,
            "limit": limit
        })

    # === 槓桿設定 ===

    async def set_leverage(self, symbol, leverage):
        return await self._post("/fapi/v1/leverage", {
            "symbol": symbol,
            "leverage": leverage
        })

    # === 下單 ===

    async def place_limit_order(self, symbol, side, quantity, price, reduce_only=False):
        """掛限價單"""
        params = {
            "symbol": symbol,
            "side": side,           # BUY / SELL
            "type": "LIMIT",
            "timeInForce": "GTC",
            "quantity": quantity,
            "price": price,
            "positionSide": "BOTH"
        }
        if reduce_only:
            params["reduceOnly"] = "true"
        return await self._post("/fapi/v1/order", params)

    async def place_market_order(self, symbol, side, quantity, reduce_only=False):
        """市價單（強制平倉用）"""
        params = {
            "symbol": symbol,
            "side": side,
            "type": "MARKET",
            "quantity": quantity,
            "positionSide": "BOTH"
        }
        if reduce_only:
            params["reduceOnly"] = "true"
        return await self._post("/fapi/v1/order", params)

    async def cancel_order(self, symbol, order_id):
        return await self._delete("/fapi/v1/order", {
            "symbol": symbol,
            "orderId": order_id
        })

    async def cancel_all_orders(self, symbol):
        return await self._delete("/fapi/v1/allOpenOrders", {"symbol": symbol})

    async def get_open_orders(self, symbol=None):
        params = {}
        if symbol:
            params["symbol"] = symbol
        return await self._get("/fapi/v1/openOrders", params, signed=True)

    # === 精度處理 ===

    async def get_quantity_precision(self, symbol, notional, price):
        """根據notional和price計算下單數量，符合交易所精度"""
        info = await self.get_symbol_info(symbol)
        if not info:
            return None

        quantity = notional / price

        # 找 LOT_SIZE filter
        step_size = 0.001
        for f in info.get("filters", []):
            if f["filterType"] == "LOT_SIZE":
                step_size = float(f["stepSize"])
                break

        # 對齊精度
        import math
        precision = max(0, -int(math.log10(step_size)))
        quantity = round(quantity - (quantity % step_size), precision)
        return quantity

    async def get_price_precision(self, symbol, price):
        """對齊價格精度"""
        info = await self.get_symbol_info(symbol)
        if not info:
            return round(price, 4)

        tick_size = 0.0001
        for f in info.get("filters", []):
            if f["filterType"] == "PRICE_FILTER":
                tick_size = float(f["tickSize"])
                break

        import math
        precision = max(0, -int(math.log10(tick_size)))
        price = round(price - (price % tick_size), precision)
        return price
