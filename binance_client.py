import hmac
import hashlib
import time
import aiohttp
import asyncio
import logging
from urllib.parse import urlencode

logger = logging.getLogger(__name__)

TESTNET_BASE = "https://testnet.binancefuture.com"
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

    async def get_price(self, symbol):
        data = await self._get("/fapi/v1/ticker/price", {"symbol": symbol})
        return float(data["price"]) if "price" in data else None

    async def get_all_prices(self):
        """批次取得所有幣種現價，回傳 {symbol: price} dict"""
        data = await self._get("/fapi/v1/ticker/price")
        if isinstance(data, list):
            return {item["symbol"]: float(item["price"]) for item in data if "symbol" in item}
        return {}

    async def get_klines(self, symbol, interval="1m", limit=10):
        return await self._get("/fapi/v1/klines", {
            "symbol": symbol,
            "interval": interval,
            "limit": limit
        })

    async def get_24h_volume(self, symbol):
        """取得24H成交量（USDT）"""
        try:
            data = await self._get("/fapi/v1/ticker/24hr", {"symbol": symbol})
            return float(data.get("quoteVolume", 0))
        except Exception:
            return 0

    # === 全倉模式設定 ===

    async def set_margin_type(self, symbol, margin_type="CROSSED"):
        """
        設定保證金模式
        margin_type: CROSSED（全倉）或 ISOLATED（逐倉）
        若已是目標模式，Binance會回傳錯誤碼 -4046，直接忽略
        """
        result = await self._post("/fapi/v1/marginType", {
            "symbol": symbol,
            "marginType": margin_type
        })
        # -4046 = No need to change margin type（已是該模式，正常忽略）
        if result.get("code") == -4046:
            return True
        if result.get("code") and result.get("code") != 200:
            logger.warning(f"set_margin_type {symbol} {margin_type}: {result}")
            return False
        return True

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
            "side": side,
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
        """市價單（即時執行）"""
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

    async def place_stop_market_order(self, symbol, side, quantity, stop_price, reduce_only=True):
        """
        掛 Stop-Market 單（預掛，觸碰 stop_price 後自動市價執行）
        用於止盈保底：預掛在交易所，程式當機也能自動出場
        """
        params = {
            "symbol": symbol,
            "side": side,
            "type": "STOP_MARKET",
            "quantity": quantity,
            "stopPrice": stop_price,
            "positionSide": "BOTH",
            "timeInForce": "GTE_GTC",  # 有效直到取消
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

    async def get_symbol_filters(self, symbol):
        """一次取得並快取幣種精度資訊"""
        info = await self.get_symbol_info(symbol)
        if not info:
            return None
        step_size = 0.001
        tick_size = 0.0001
        min_notional = 5.0
        for f in info.get("filters", []):
            if f["filterType"] == "LOT_SIZE":
                step_size = float(f["stepSize"])
            elif f["filterType"] == "PRICE_FILTER":
                tick_size = float(f["tickSize"])
            elif f["filterType"] == "MIN_NOTIONAL":
                min_notional = float(f.get("notional", 5.0))
        return {
            "step_size": step_size,
            "tick_size": tick_size,
            "min_notional": min_notional
        }

    async def get_quantity_precision(self, symbol, notional, price):
        """根據notional和price計算下單數量，符合交易所精度"""
        import math
        filters = await self.get_symbol_filters(symbol)
        if not filters:
            return None
        step_size = filters["step_size"]
        quantity = notional / price
        precision = max(0, -int(math.log10(step_size)))
        quantity = round(quantity - (quantity % step_size), precision)
        return quantity

    async def get_price_precision(self, symbol, price):
        """對齊價格精度"""
        import math
        filters = await self.get_symbol_filters(symbol)
        if not filters:
            return round(price, 4)
        tick_size = filters["tick_size"]
        precision = max(0, -int(math.log10(tick_size)))
        price = round(price - (price % tick_size), precision)
        return price
