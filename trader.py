"""
交易引擎 — 策略A v2

核心原則：一切以幣安為準，DB只記已平倉歷史

開倉邏輯：
- 候選池所有幣持續嘗試開倉，被持倉數濾網擋住
- 現價觸碰15分K BB上軌 → 掛限價空單
- 現價突破上軌 → 等黑K收完 → 取含黑K的3根最高點掛空單
- 確認幣安成交後才算正式開倉

隱形網格：
- 開倉成交後，以成交價計算4個向下網格存入state（不掛幣安）
- 每輪掃描：現價穿越隱形網格 → 真實掛出 + 從state移除
- 有新成交（任何一筆）→ 重算4格取代舊隱形網格
- 已掛出的網格單隨緣成交，不取消

止盈止損：
- 以幣安實際持倉計算，每次有新成交後重新掛
- SHORT止盈：入場均價 × (1 - take_profit_price_pct%)
- SHORT止損：入場均價 × (1 + force_close_price_pct%)
- 掛單前驗證價格方向正確

保證金模式：全倉（CROSSED）
"""

import asyncio
import time
import math
import logging
from datetime import datetime, timezone, timedelta

TZ_TAIPEI = timezone(timedelta(hours=8))
from binance_client import BinanceClient
from database import write_log, record_trade_close, add_trade_analytics
from config import load_config, get_notional

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


# 啟動時批次取得所有幣種精度快取
FILTERS_CACHE_TTL = 3600  # 1小時更新一次
_filters_last_refresh = 0

async def refresh_all_filters(client):
    """啟動時一次批次取得所有幣種精度，存入快取，避免頻繁打 exchangeInfo"""
    global _filters_last_refresh
    now = time.time()
    if now - _filters_last_refresh < FILTERS_CACHE_TTL and state["symbol_filters_cache"]:
        return
    try:
        data = await client.get_exchange_info()
        count = 0
        for s in data.get("symbols", []):
            sym = s["symbol"]
            step_size = 0.001
            tick_size = 0.0001
            for f in s.get("filters", []):
                if f["filterType"] == "LOT_SIZE":
                    step_size = float(f["stepSize"])
                elif f["filterType"] == "PRICE_FILTER":
                    tick_size = float(f["tickSize"])
            state["symbol_filters_cache"][sym] = {
                "step_size": step_size,
                "tick_size": tick_size,
            }
            count += 1
        _filters_last_refresh = now
        logger.info(f"批次精度快取完成，共 {count} 個幣種")
    except Exception as e:
        logger.error(f"批次精度快取失敗: {e}")


# ===== 全局狀態 =====
state = {
    "running": True,
    "paused": False,
    "last_pool_scan": 0,
    "margin_pause": False,
    "candidate_pool": [],
    "scanner_latest_result": [],

    # 止盈止損單ID（symbol -> {"tp_limit": id, "tp_stop": id, "sl_limit": id, "sl_stop": id}）
    "tp_sl_orders": {},

    # 黑K偵測狀態
    "black_k_targets": {},       # symbol -> target_price
    "black_k_last_k_time": {},   # symbol -> 上一根K棒開盤時間，防重複

    # 隱形網格（symbol -> [price1, price2, ...]，最多4個，以幣安成交價為基準）
    "hidden_grids": {},

    # 已知成交單ID，避免重複處理（symbol -> set of order_ids）
    "known_fills": {},

    # 幣種設定快取（symbol -> {margin_set, leverage_set}）
    "symbol_setup_done": set(),

    # 幣種精度快取
    "symbol_filters_cache": {},

    # 批次取價快取
    "price_cache": {},
    "price_cache_time": 0,

    # 餘額快取
    "balance_cache": None,
    "balance_cache_time": 0,

    # 觸發防重複（symbol -> bool）
    "triggered_symbols": set(),
}

PRICE_CACHE_TTL = 10
BALANCE_CACHE_TTL = 30


def get_client(cfg):
    return BinanceClient(cfg["api_key"], cfg["api_secret"], cfg["testnet"])


# ===== 快取工具 =====

async def refresh_price_cache(client):
    try:
        prices = await client.get_all_prices()
        if prices:
            state["price_cache"] = prices
            state["price_cache_time"] = time.time()
    except Exception as e:
        logger.error(f"批次取價失敗: {e}")


def get_cached_price(symbol):
    return state["price_cache"].get(symbol)


async def get_balance_cached(client):
    now = time.time()
    if state["balance_cache"] and (now - state["balance_cache_time"]) < BALANCE_CACHE_TTL:
        return state["balance_cache"]
    try:
        balance = await client.get_balance()
    except Exception as e:
        write_log("ERROR", f"餘額取得例外: {e}")
        return None
    if balance:
        state["balance_cache"] = balance
        state["balance_cache_time"] = now
    else:
        # 直接查幣安回傳了什麼
        try:
            raw = await client.get_account()
            write_log("ERROR", f"餘額取得失敗，Binance回傳: {str(raw)[:200]}")
        except Exception as e2:
            write_log("ERROR", f"餘額取得失敗，get_account例外: {e2}")
    return balance


async def get_filters_cached(client, symbol):
    if symbol in state["symbol_filters_cache"]:
        return state["symbol_filters_cache"][symbol]
    try:
        filters = await client.get_symbol_filters(symbol)
        if filters:
            state["symbol_filters_cache"][symbol] = filters
        return filters
    except Exception as e:
        logger.error(f"取得精度失敗 {symbol}: {e}")
        return None


def align_price(price, tick_size):
    if not tick_size or tick_size <= 0:
        return round(price, 8)
    precision = max(0, -int(math.log10(tick_size)))
    return round(price - (price % tick_size), precision)


def align_qty(qty, step_size):
    if not step_size or step_size <= 0:
        return round(qty, 8)
    precision = max(0, -int(math.log10(step_size)))
    return round(qty - (qty % step_size), precision)


# ===== 幣種初始化（全倉+槓桿，每個幣只做一次）=====

async def ensure_symbol_setup(client, cfg, symbol):
    if symbol in state["symbol_setup_done"]:
        return True
    try:
        await client.set_margin_type(symbol, "CROSSED")
        await client.set_leverage(symbol, cfg["leverage"])
        state["symbol_setup_done"].add(symbol)
        return True
    except Exception as e:
        logger.error(f"幣種初始化失敗 {symbol}: {e}")
        return False


# ===== 從幣安取得實際持倉 =====

async def get_binance_position(client, symbol):
    """從幣安取得單一幣種實際持倉，回傳 {qty, avg_entry, unrealized_pnl} 或 None"""
    try:
        positions = await client.get_positions(symbol)
        if not positions:
            return None
        p = positions[0]
        qty = abs(float(p["positionAmt"]))
        if qty <= 0:
            return None
        return {
            "qty": qty,
            "avg_entry": float(p["entryPrice"]),
            "unrealized_pnl": float(p["unRealizedProfit"]),
            "initial_margin": float(p["initialMargin"]),
            "leverage": int(float(p.get("leverage", 1))),
        }
    except Exception as e:
        logger.error(f"取得幣安持倉失敗 {symbol}: {e}")
        return None


async def get_all_binance_positions(client):
    """取得所有有持倉的幣種"""
    try:
        positions = await client.get_positions()
        return {p["symbol"]: {
            "qty": abs(float(p["positionAmt"])),
            "avg_entry": float(p["entryPrice"]),
            "unrealized_pnl": float(p["unRealizedProfit"]),
            "initial_margin": float(p["initialMargin"]),
        } for p in positions if abs(float(p["positionAmt"])) > 0}
    except Exception as e:
        logger.error(f"取得所有持倉失敗: {e}")
        return {}


# ===== 成交確認 =====

async def get_recent_fills(client, symbol, limit=10):
    """取得最近成交紀錄，回傳 [{order_id, price, qty, side, time}]"""
    try:
        trades = await client._get("/fapi/v1/userTrades", {
            "symbol": symbol,
            "limit": limit
        }, signed=True)
        if not isinstance(trades, list):
            return []
        return [{
            "order_id": str(t["orderId"]),
            "price": float(t["price"]),
            "qty": float(t["qty"]),
            "side": t["side"],
            "time": t["time"],
            "realized_pnl": float(t.get("realizedPnl", 0)),
        } for t in trades]
    except Exception as e:
        logger.error(f"取得成交紀錄失敗 {symbol}: {e}")
        return []


async def check_new_fills(client, symbol):
    """
    檢查是否有新成交，回傳新成交列表
    用來觸發：隱形網格重算、止盈止損更新
    """
    fills = await get_recent_fills(client, symbol, limit=5)
    if not fills:
        return []

    known = state["known_fills"].setdefault(symbol, set())
    new_fills = [f for f in fills if f["order_id"] not in known]

    for f in new_fills:
        known.add(f["order_id"])

    return new_fills


# ===== 隱形網格 =====

def calc_hidden_grids(entry_price, grid_spacing_pct, count=4):
    """計算隱形網格價格列表（往下）"""
    return [round(entry_price * (1 - grid_spacing_pct / 100 * i), 8)
            for i in range(1, count + 1)]


def update_hidden_grids(symbol, entry_price, cfg):
    """以新成交價重算4格隱形網格，取代舊的"""
    grids = calc_hidden_grids(entry_price, cfg["grid_spacing_pct"], 4)
    state["hidden_grids"][symbol] = grids
    logger.info(f"隱形網格更新 {symbol}: {grids}")
    write_log("HIDDEN_GRID_UPDATE", f"隱形網格重算，基準價={entry_price}", symbol=symbol,
              detail={"entry_price": entry_price, "grids": grids})


async def check_and_place_hidden_grids(client, cfg, symbol):
    """
    檢查隱形網格是否被穿越，穿越就真實掛出並從state移除
    已掛出的不取消，隨緣成交
    """
    grids = state["hidden_grids"].get(symbol, [])
    if not grids:
        return

    current_price = get_cached_price(symbol)
    if not current_price:
        return

    filters = await get_filters_cached(client, symbol)
    if not filters:
        return

    balance = await get_balance_cached(client)
    if not balance:
        return

    notional = get_notional(cfg, balance["total"])
    remaining = []

    for grid_price in grids:
        if current_price < grid_price:
            # 現價已穿越此隱形網格，真實掛出
            qty = align_qty(notional / grid_price, filters["step_size"])
            aligned_price = align_price(grid_price, filters["tick_size"])

            if qty <= 0:
                continue

            result = await client.place_limit_order(symbol, "SELL", qty, aligned_price)
            if result and "orderId" in result:
                logger.info(f"📌 隱形網格掛出 {symbol} @ {aligned_price}")
                write_log("GRID_PLACE", f"隱形網格掛出 @ {aligned_price}", symbol=symbol,
                          detail={"price": aligned_price, "qty": qty,
                                  "current_price": current_price,
                                  "order_id": str(result["orderId"])})
                # 從隱形列表移除（不加入remaining）
            else:
                # 掛單失敗，保留在隱形列表稍後重試
                remaining.append(grid_price)
                write_log("ERROR", f"隱形網格掛單失敗 @ {aligned_price}", symbol=symbol,
                          detail={"resp": result})
        else:
            remaining.append(grid_price)

    state["hidden_grids"][symbol] = remaining


# ===== 止盈止損 =====

def calc_tp_price(avg_entry, cfg):
    """SHORT止盈：價格下跌 take_profit_price_pct%"""
    pct = cfg.get("take_profit_price_pct", 1.0)
    return avg_entry * (1 - pct / 100)


def calc_sl_price(avg_entry, cfg):
    """SHORT止損：價格上漲 force_close_price_pct%"""
    pct = cfg.get("force_close_price_pct", 3.0)
    return avg_entry * (1 + pct / 100)


async def place_tp_sl(client, cfg, symbol):
    """
    從幣安取實際持倉，計算並掛止盈止損單
    掛單前驗證價格方向（止盈必須低於入場價，止損必須高於入場價）
    """
    pos = await get_binance_position(client, symbol)
    if not pos:
        write_log("TP_SL", "幣安無持倉，跳過止盈止損", symbol=symbol)
        return

    avg_entry = pos["avg_entry"]
    total_qty = pos["qty"]

    tp_price_raw = calc_tp_price(avg_entry, cfg)
    sl_price_raw = calc_sl_price(avg_entry, cfg)

    # 方向驗證
    if tp_price_raw >= avg_entry:
        write_log("ERROR", f"止盈價{tp_price_raw}>=入場價{avg_entry}，放棄掛止盈", symbol=symbol)
        return
    if sl_price_raw <= avg_entry:
        write_log("ERROR", f"止損價{sl_price_raw}<=入場價{avg_entry}，放棄掛止損", symbol=symbol)
        return

    filters = await get_filters_cached(client, symbol)
    if not filters:
        return

    tp_price = align_price(tp_price_raw, filters["tick_size"])
    sl_price = align_price(sl_price_raw, filters["tick_size"])

    # 取消舊止盈止損單
    old = state["tp_sl_orders"].get(symbol, {})
    for order_id in old.values():
        try:
            await client.cancel_order(symbol, order_id)
        except Exception:
            pass
    state["tp_sl_orders"][symbol] = {}

    # 拆單計算
    tp_limit_pct = cfg.get("tp_limit_pct", 50)
    limit_qty = align_qty(total_qty * (tp_limit_pct / 100), filters["step_size"])
    stop_qty = align_qty(total_qty - limit_qty, filters["step_size"])

    new_orders = {}
    tp_pct = round((avg_entry - tp_price) / avg_entry * 100, 3)
    sl_pct = round((sl_price - avg_entry) / avg_entry * 100, 3)

    # 止盈限價單
    if limit_qty > 0:
        r = await client.place_limit_order(symbol, "BUY", limit_qty, tp_price, reduce_only=True)
        if "orderId" in r:
            new_orders["tp_limit"] = str(r["orderId"])
            logger.info(f"✅ 止盈限價 {symbol} @ {tp_price} (-{tp_pct}%)")
        else:
            write_log("ERROR", f"止盈限價單失敗: {r.get('msg','')}", symbol=symbol,
                      detail={"tp_price": tp_price, "qty": limit_qty, "resp": r})

    # 止盈Stop-Market單
    if stop_qty > 0:
        r = await client.place_stop_market_order(symbol, "BUY", stop_qty, tp_price, reduce_only=True)
        if "orderId" in r:
            new_orders["tp_stop"] = str(r["orderId"])
            logger.info(f"✅ 止盈Stop {symbol} @ {tp_price}")
        else:
            write_log("ERROR", f"止盈Stop單失敗: {r.get('msg','')}", symbol=symbol,
                      detail={"tp_price": tp_price, "qty": stop_qty, "resp": r})

    # 止損限價單
    if limit_qty > 0:
        r = await client.place_limit_order(symbol, "BUY", limit_qty, sl_price, reduce_only=True)
        if "orderId" in r:
            new_orders["sl_limit"] = str(r["orderId"])
            logger.info(f"✅ 止損限價 {symbol} @ {sl_price} (+{sl_pct}%)")
        else:
            write_log("ERROR", f"止損限價單失敗: {r.get('msg','')}", symbol=symbol,
                      detail={"sl_price": sl_price, "qty": limit_qty, "resp": r})

    # 止損Stop-Market單
    if stop_qty > 0:
        r = await client.place_stop_market_order(symbol, "BUY", stop_qty, sl_price, reduce_only=True)
        if "orderId" in r:
            new_orders["sl_stop"] = str(r["orderId"])
            logger.info(f"✅ 止損Stop {symbol} @ {sl_price}")
        else:
            write_log("ERROR", f"止損Stop單失敗: {r.get('msg','')}", symbol=symbol,
                      detail={"sl_price": sl_price, "qty": stop_qty, "resp": r})

    state["tp_sl_orders"][symbol] = new_orders

    write_log("TP_SL_ORDER",
              f"止盈止損更新 avg={avg_entry:.6f} tp={tp_price}(-{tp_pct}%) sl={sl_price}(+{sl_pct}%)",
              symbol=symbol, detail={
                  "avg_entry": avg_entry,
                  "tp_price": tp_price, "sl_price": sl_price,
                  "tp_pct": tp_pct, "sl_pct": sl_pct,
                  "total_qty": total_qty,
                  "limit_qty": limit_qty, "stop_qty": stop_qty,
                  "orders": new_orders
              })


# ===== 開倉邏輯 =====

async def try_open_position(client, cfg, symbol, entry_price, trigger_type="UPPER"):
    """
    掛限價空單，不記DB，等幣安成交確認
    trigger_type: UPPER（觸碰上軌）或 BLACK_K（黑K目標）
    """
    if state["paused"] or state["margin_pause"]:
        return False

    # 檢查持倉數（以幣安為準）
    all_positions = await get_all_binance_positions(client)
    open_syms = set(all_positions.keys())

    if symbol not in open_syms and len(open_syms) >= cfg["max_symbols"]:
        return False

    balance = await get_balance_cached(client)
    if not balance:
        write_log("ERROR", "餘額取得失敗", symbol=symbol)
        return False

    total = balance["total"]
    margin_used = balance["margin_used"]
    if total > 0 and (margin_used / total * 100) >= cfg["margin_usage_limit_pct"]:
        state["margin_pause"] = True
        write_log("MARGIN_PAUSE", f"保證金使用率超限，暫停開倉", symbol=symbol)
        return False

    # 幣種初始化
    await ensure_symbol_setup(client, cfg, symbol)

    filters = await get_filters_cached(client, symbol)
    if not filters:
        write_log("ERROR", "無法取得幣種精度", symbol=symbol)
        return False

    notional = get_notional(cfg, total)
    qty = align_qty(notional / entry_price, filters["step_size"])
    price = align_price(entry_price, filters["tick_size"])

    if qty <= 0:
        return False

    result = await client.place_limit_order(symbol, "SELL", qty, price)
    if "orderId" not in result:
        write_log("ERROR", f"下單失敗: {result.get('msg','')}", symbol=symbol,
                  detail={"price": price, "qty": qty, "resp": result})
        return False

    order_id = str(result["orderId"])
    logger.info(f"✅ 掛單 {symbol} @ {price} qty={qty} trigger={trigger_type}")

    # 取得候選池市場資訊供log用
    candidate_info = next((c for c in state["candidate_pool"] if c["symbol"] == symbol), {})
    write_log("ORDER", f"掛限價空單 @ {price} [{trigger_type}]", symbol=symbol,
              detail={
                  "order_id": order_id,
                  "price": price,
                  "qty": qty,
                  "notional": notional,
                  "trigger_type": trigger_type,
                  "account_balance": total,
                  "market_snapshot": {
                      "upper_15m": candidate_info.get("upper_15m", 0),
                      "dist_15m_pct": candidate_info.get("dist_15m", 0),
                      "dist_1h_pct": candidate_info.get("dist_1h", 0),
                      "band_width_pct": candidate_info.get("band_width_pct", 0),
                      "volume_usdt": candidate_info.get("volume_usdt", 0),
                      "prev_high_score": candidate_info.get("prev_high_score", 0),
                  }
              })
    return True


# ===== 平倉邏輯 =====

async def close_symbol(client, cfg, symbol, reason="TP"):
    """市價平倉，取消所有掛單，清除state，記錄歷史"""
    logger.info(f"平倉 {symbol} reason={reason}")

    pos = await get_binance_position(client, symbol)
    actual_close_price = None

    if pos:
        total_qty = pos["qty"]
        avg_entry = pos["avg_entry"]

        result = await client.place_market_order(symbol, "BUY", total_qty, reduce_only=True)
        logger.info(f"市價平倉 {symbol}: {result}")

        # 取實際成交均價
        if result and "avgPrice" in result:
            try:
                actual_close_price = float(result["avgPrice"])
            except Exception:
                pass

        if not actual_close_price or actual_close_price <= 0:
            await asyncio.sleep(0.5)
            actual_close_price = await client.get_price(symbol) or avg_entry

        pnl = (avg_entry - actual_close_price) * total_qty
        margin = pos["initial_margin"]
        roe_pct = (pnl / margin * 100) if margin > 0 else 0
        price_drop_pct = (avg_entry - actual_close_price) / avg_entry * 100

        logger.info(f"💰 {symbol} PnL={pnl:.4f} ROE={roe_pct:.2f}%")

        # 記錄歷史（DB只記已平倉）
        record_trade_close(
            symbol=symbol,
            avg_entry=avg_entry,
            close_price=actual_close_price,
            total_qty=total_qty,
            total_margin=margin,
            total_pnl=pnl,
            roe_pct=roe_pct,
            close_reason=reason
        )

        write_log("CLOSE", f"平倉完成 PnL={pnl:.4f} ROE={roe_pct:.2f}%",
                  symbol=symbol, detail={
                      "avg_entry": avg_entry,
                      "close_price": actual_close_price,
                      "price_drop_pct": round(price_drop_pct, 3),
                      "total_pnl": round(pnl, 4),
                      "roe_pct": round(roe_pct, 2),
                      "reason": reason
                  })

        # 記錄 trade_analytics（為未來學習系統準備）
        try:
            candidate_info = next((c for c in state["candidate_pool"] if c["symbol"] == symbol), {})
            add_trade_analytics(
                symbol=symbol,
                avg_entry=avg_entry,
                close_price=actual_close_price,
                total_qty=total_qty,
                total_margin=margin,
                total_pnl=pnl,
                roe_pct=roe_pct,
                close_reason=reason,
                market_snapshot=candidate_info
            )
        except Exception as e:
            logger.error(f"trade_analytics記錄失敗: {e}")

    # 取消所有掛單
    try:
        await client.cancel_all_orders(symbol)
    except Exception as e:
        logger.error(f"取消掛單失敗 {symbol}: {e}")

    # 清除state
    _clear_symbol_state(symbol)


def _clear_symbol_state(symbol):
    state["tp_sl_orders"].pop(symbol, None)
    state["black_k_targets"].pop(symbol, None)
    state["black_k_last_k_time"].pop(symbol, None)
    state["hidden_grids"].pop(symbol, None)
    state["known_fills"].pop(symbol, None)
    state["triggered_symbols"].discard(symbol)
    state["margin_pause"] = False


# ===== 黑K偵測 =====

async def check_black_k(client, symbol):
    """
    等1分K收完確認是黑K，取含該K在內往前3根的最高點
    K棒未收完繼續觀察，不提前行動
    """
    try:
        klines = await client.get_klines(symbol, "1m", limit=10)
    except Exception:
        return None

    if not klines or len(klines) < 4:
        return None

    # klines[-1] 是當前未收完的K，klines[-2] 是上一根已收完的K
    last_k = klines[-2]
    k_open_time = last_k[0]
    open_p = float(last_k[1])
    high_p = float(last_k[2])
    close_p = float(last_k[4])
    volume = float(last_k[5])

    # 防重複：同一根K棒只處理一次
    if state["black_k_last_k_time"].get(symbol) == k_open_time:
        return None

    # 不是黑K就跳過
    if close_p >= open_p:
        return None

    state["black_k_last_k_time"][symbol] = k_open_time

    # 取含黑K在內往前3根的最高點
    three_ks = klines[-4:-1]  # 包含黑K這根
    highest = max(float(k[2]) for k in three_ks)

    body_pct = round((open_p - close_p) / open_p * 100, 3)
    upper_shadow = round((high_p - open_p) / open_p * 100, 3) if high_p > open_p else 0

    logger.info(f"🖤 黑K {symbol} body={body_pct}% 目標={highest}")
    write_log("BLACK_K", f"黑K確認，目標={highest}", symbol=symbol,
              detail={
                  "open": open_p, "close": close_p, "high": high_p,
                  "body_pct": body_pct,
                  "upper_shadow_pct": upper_shadow,
                  "volume": volume,
                  "highest": highest,
                  "k_open_time": k_open_time
              })
    return highest


# ===== ROE保護（以幣安持倉為準）=====

async def check_roe_protection(client, cfg, symbol):
    """檢查浮動損益，觸發暫停或強制平倉"""
    pos = await get_binance_position(client, symbol)
    if not pos:
        return

    avg_entry = pos["avg_entry"]
    current_price = get_cached_price(symbol)
    if not current_price:
        return

    total_qty = pos["qty"]
    # 用實際保證金計算，從幣安取
    margin = pos["initial_margin"]
    if margin <= 0:
        return

    unrealized_pnl = (avg_entry - current_price) * total_qty
    roe_pct = unrealized_pnl / margin * 100
    capital_return_pct = roe_pct / cfg["leverage"]

    if capital_return_pct <= cfg["force_close_capital_pct"]:
        write_log("ROE_FORCE", f"本金虧損{capital_return_pct:.1f}%，強制平倉", symbol=symbol,
                  detail={"avg_entry": avg_entry, "current_price": current_price,
                          "unrealized_pnl": round(unrealized_pnl, 4)})
        await close_symbol(client, cfg, symbol, reason="FORCE_CLOSE")


# ===== 持倉監控（新成交處理）=====

async def monitor_symbol(client, cfg, symbol):
    """
    監控單一幣種：
    1. 檢查是否有新成交 → 更新隱形網格、更新止盈止損
    2. 檢查隱形網格是否被穿越 → 真實掛出
    3. ROE保護（用價格快取，不打API）
    """
    new_fills = await check_new_fills(client, symbol)

    if new_fills:
        sell_fills = [f for f in new_fills if f["side"] == "SELL"]
        if sell_fills:
            latest_fill = max(sell_fills, key=lambda f: f["time"])
            update_hidden_grids(symbol, latest_fill["price"], cfg)

        await place_tp_sl(client, cfg, symbol)

        fill_summary = [{"side": f["side"], "price": f["price"], "qty": f["qty"]}
                        for f in new_fills]
        write_log("FILL", f"新成交 {len(new_fills)}筆", symbol=symbol,
                  detail={"fills": fill_summary})

    # 檢查隱形網格
    await check_and_place_hidden_grids(client, cfg, symbol)

    # ROE保護（用快取持倉，不打額外API）
    cached_pos = state.get("_binance_positions_cache", {}).get(symbol)
    if cached_pos:
        current_price = get_cached_price(symbol)
        if current_price:
            avg_entry = cached_pos["avg_entry"]
            total_qty = cached_pos["qty"]
            margin = cached_pos["initial_margin"]
            if margin > 0:
                unrealized_pnl = (avg_entry - current_price) * total_qty
                roe_pct = unrealized_pnl / margin * 100
                capital_return_pct = roe_pct / cfg["leverage"]
                if capital_return_pct <= cfg["force_close_capital_pct"]:
                    write_log("ROE_FORCE", f"本金虧損{capital_return_pct:.1f}%，強制平倉",
                              symbol=symbol)
                    await close_symbol(client, cfg, symbol, reason="FORCE_CLOSE")


# ===== 掃描候選池 =====

async def scan_candidates(cfg, scanner_data=None):
    if not scanner_data:
        return []

    candidates = []
    for item in scanner_data:
        try:
            sym = item.get("full_symbol", "")
            if not sym:
                raw = item.get("symbol", "")
                sym = raw if "USDT" in raw else raw + "USDT"

            candidates.append({
                "symbol": sym,
                "price": float(item.get("price", 0)),
                "upper_15m": float(item.get("upper", 0)),
                "dist_15m": float(item.get("dist_to_upper", item.get("dist_to_upper_pct", 0))),
                "dist_1h": float(item.get("dist_1h_pct", 0)) if item.get("dist_1h_pct") is not None else 0,
                "band_width_pct": float(item.get("band_width_pct", 0)),
                "volume_usdt": float(item.get("volume_usdt", 0)),
                "prev_high_score": float(item.get("prev_high_score", 0)),
            })
        except Exception:
            continue

    write_log("SCAN", f"候選池更新 {len(candidates)} 個",
              detail={"from_scanner": len(scanner_data), "candidates": len(candidates)})
    return candidates


# ===== Reset =====

async def reset_system(client, cfg):
    """取消所有掛單，重新掛止盈止損，清除state"""
    logger.info("🔄 Reset開始")

    all_positions = await get_all_binance_positions(client)

    for symbol in list(all_positions.keys()):
        try:
            await client.cancel_all_orders(symbol)
        except Exception as e:
            logger.error(f"取消掛單失敗 {symbol}: {e}")

    # 清除state
    state["tp_sl_orders"].clear()
    state["black_k_targets"].clear()
    state["black_k_last_k_time"].clear()
    state["hidden_grids"].clear()
    state["known_fills"].clear()
    state["triggered_symbols"].clear()
    state["symbol_setup_done"].clear()
    state["symbol_filters_cache"].clear()
    state["balance_cache"] = None
    state["margin_pause"] = False

    # 重新掛止盈止損
    for symbol in all_positions.keys():
        await place_tp_sl(client, cfg, symbol)

    write_log("RESET", f"Reset完成，持倉幣種: {list(all_positions.keys())}")
    return {"status": "ok", "open_symbols": list(all_positions.keys())}


# ===== 暫停 =====

async def handle_pause(client, cfg):
    """取消所有開倉網格掛單，保留止盈止損"""
    all_positions = await get_all_binance_positions(client)

    for symbol in all_positions.keys():
        try:
            open_orders = await client.get_open_orders(symbol)
            protected = set(state["tp_sl_orders"].get(symbol, {}).values())

            for order in open_orders:
                oid = str(order["orderId"])
                if oid not in protected:
                    await client.cancel_order(symbol, oid)

            state["hidden_grids"].pop(symbol, None)
        except Exception as e:
            logger.error(f"暫停處理失敗 {symbol}: {e}")

    write_log("PAUSE", "系統暫停，已取消開倉掛單，保留止盈止損")


# ===== 主循環 =====

async def trading_loop():
    logger.info("🚀 交易引擎啟動")
    loop_count = 0

    while True:
        try:
            cfg = load_config()

            if not cfg.get("system_running", True):
                await asyncio.sleep(5)
                continue

            client = get_client(cfg)
            loop_count += 1

            # 批次取價（每輪）
            await refresh_price_cache(client)

            # 批次精度快取（啟動時 + 每小時）
            await refresh_all_filters(client)

            # 取得幣安實際持倉（每輪一次，結果共用）
            all_positions = await get_all_binance_positions(client)
            open_syms = set(all_positions.keys())
            # 存入state供儀表板讀取
            state["_binance_positions_cache"] = all_positions

            # 餘額（每輪從快取讀，快取30秒更新一次）
            balance = await get_balance_cached(client)

            # 1. 監控現有持倉（新成交、隱形網格、ROE）
            for symbol in list(open_syms):
                try:
                    await monitor_symbol(client, cfg, symbol)
                except Exception as e:
                    logger.error(f"監控失敗 {symbol}: {e}")

            # 2. 更新候選池
            pool_refresh_sec = cfg.get("candidate_pool_refresh_min", 3) * 60
            time_since_scan = time.time() - state["last_pool_scan"]
            need_refresh = time_since_scan >= pool_refresh_sec and not state["paused"]

            if need_refresh:
                scanner_data = state.get("scanner_latest_result", [])
                candidates = await scan_candidates(cfg, scanner_data=scanner_data)
                state["candidate_pool"] = candidates
                state["last_pool_scan"] = time.time()

            # 3. 候選池開倉監控
            if not state["paused"] and not state["margin_pause"]:
                # 每輪重新取持倉數，確保準確
                current_open_count = len(open_syms)

                for candidate in state["candidate_pool"]:
                    sym = candidate["symbol"]
                    current_price = get_cached_price(sym)
                    if not current_price:
                        continue

                    upper = candidate["upper_15m"]
                    already_has_position = sym in open_syms

                    # 持倉數檢查：用即時計數，開倉成功後立刻更新
                    at_max = not already_has_position and current_open_count >= cfg["max_symbols"]

                    # 觸碰上軌開倉
                    if current_price >= upper * 0.9995:
                        if sym not in state["triggered_symbols"]:
                            write_log("TRIGGER", f"觸碰上軌 price={current_price} upper={upper}",
                                      symbol=sym)
                            state["triggered_symbols"].add(sym)
                            if not at_max:
                                success = await try_open_position(client, cfg, sym, upper, "UPPER")
                                if success:
                                    current_open_count += 1  # 立刻更新計數
                            else:
                                write_log("BLOCKED", f"持倉已滿({current_open_count}/{cfg['max_symbols']})", symbol=sym)

                    # 離開上軌解鎖
                    if current_price < upper * 0.998 and sym in state["triggered_symbols"]:
                        if sym not in open_syms:
                            state["triggered_symbols"].discard(sym)

                    # 突破上軌：黑K偵測
                    if current_price > upper:
                        if sym not in state["black_k_targets"]:
                            target = await check_black_k(client, sym)
                            if target:
                                state["black_k_targets"][sym] = target
                                # 黑K確認後立刻以最高點建立隱形網格
                                # 不等第一張成交，讓後續下跌直接吃到網格
                                update_hidden_grids(sym, target, cfg)

                    # 黑K目標觸價
                    if sym in state["black_k_targets"]:
                        target_price = state["black_k_targets"][sym]
                        if current_price >= target_price * 0.9995:
                            at_max = not already_has_position and current_open_count >= cfg["max_symbols"]
                            if not at_max:
                                success = await try_open_position(client, cfg, sym, target_price, "BLACK_K")
                                if success:
                                    current_open_count += 1
                                    state["black_k_targets"].pop(sym, None)
                                    state["black_k_last_k_time"].pop(sym, None)
                            else:
                                write_log("BLOCKED", f"持倉已滿({current_open_count}/{cfg['max_symbols']})", symbol=sym)

        except Exception as e:
            logger.error(f"主循環錯誤: {e}", exc_info=True)

        await asyncio.sleep(10)


def start_trading_loop():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(trading_loop())
