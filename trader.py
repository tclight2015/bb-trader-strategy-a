"""
交易引擎 — 策略A
往上：等黑K出現，取黑K本身+前2根共3根最高點作為空單點位
往下：固定間距網格，K棒收盤後以K棒高點重建網格
止盈：限價單（X%）+ Stop-Market單（剩餘%）預掛交易所
保證金模式：全倉（CROSSED）
"""

import asyncio
import time
import math
import logging
from datetime import datetime, timezone, timedelta
TZ_TAIPEI = timezone(timedelta(hours=8))
from binance_client import BinanceClient
from database import (
    write_log, get_logs,
    add_position, get_open_positions, get_open_symbols,
    close_positions, save_grids, get_grids, clear_grids
)
from config import load_config, get_notional

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


# ===== 全局狀態 =====
state = {
    "running": True,
    "paused": False,
    "last_pool_scan": 0,
    "margin_pause": False,
    "roe_pause_symbols": set(),
    "candidate_pool": [],
    "pending_orders": {},           # symbol -> [order_id, ...]
    "pending_grid_prices": {},      # symbol -> set(price)，防網格重複下單
    "tp_order_ids": {},             # symbol -> {"limit": order_id, "stop": order_id}
    "triggered_symbols": set(),
    "black_k_targets": {},          # symbol -> target_price
    "black_k_last_k_time": {},      # symbol -> k棒開始時間戳，防同根K重複觸發
    "last_grid_k_time": {},         # symbol -> k棒開始時間戳，K棒收盤後重建網格用
    "last_scan_result": [],
    "scanner_latest_result": [],    # 由 app.py 寫入
}


def get_client(cfg):
    return BinanceClient(cfg["api_key"], cfg["api_secret"], cfg["testnet"])


# ===== 掃描邏輯 =====

async def scan_candidates(cfg, scanner_data=None):
    """候選池直接使用掃描器已處理好的結果，加入成交量篩選"""
    if not scanner_data:
        write_log("SCAN", "掃描器資料為空，跳過本次候選池更新")
        return []

    candidates = []
    for item in scanner_data:
        try:
            # 成交量篩選（volume_usdt > 0 才篩，=0 代表掃描器還沒有這欄位，不擋）
            volume = float(item.get("volume_usdt", 0))
            min_vol = cfg.get("min_volume_usdt", 5_000_000)
            if volume > 0 and volume < min_vol:
                continue

            sym = item.get("full_symbol", "")
            if not sym:
                raw = item.get("symbol", "")
                sym = raw if "USDT" in raw else raw + "USDT"

            candidates.append({
                "symbol": sym,
                "price": float(item.get("price", 0)),
                "upper_15m": float(item.get("upper", 0)),
                "upper_1h": 0,
                "dist_15m": float(item.get("dist_to_upper", item.get("dist_to_upper_pct", 0))),
                "dist_1h": float(item.get("dist_1h_pct", 0)) if item.get("dist_1h_pct") is not None else 0,
                "band_width_pct": float(item.get("band_width_pct", 0)),
                "volume_usdt": volume,
                "volume_shrinking": item.get("volume_shrinking", False),
            })
        except Exception:
            continue

    write_log("SCAN", f"候選池更新，共 {len(candidates)} 個候選",
              detail={"from_scanner": len(scanner_data), "candidates": len(candidates)})
    return candidates


# ===== 網格計算 =====

def calc_grid_prices(base_price, grid_spacing_pct, count, direction="DOWN"):
    prices = []
    for i in range(1, count + 1):
        if direction == "DOWN":
            p = base_price * (1 - grid_spacing_pct / 100 * i)
        else:
            p = base_price * (1 + grid_spacing_pct / 100 * i)
        prices.append(round(p, 8))
    return prices


# ===== 止盈價計算 =====

def calc_tp_price(avg_entry, take_profit_capital_pct, leverage):
    """
    SHORT止盈價：
    capital_return = (entry - tp) / entry * leverage
    => tp = entry * (1 - take_profit_capital_pct / 100 / leverage)
    """
    return avg_entry * (1 - take_profit_capital_pct / 100 / leverage)


# ===== 止盈掛單管理 =====

async def place_tp_orders(client, cfg, symbol):
    """
    重新計算止盈價，取消舊止盈單，掛新的兩張：
    - 限價單：佔 tp_limit_pct%
    - Stop-Market單：佔剩餘%
    """
    positions = await client.get_positions(symbol)
    if not positions:
        return

    total_qty = sum(abs(float(p["positionAmt"])) for p in positions)
    if total_qty <= 0:
        return

    avg_entry = sum(float(p["entryPrice"]) * abs(float(p["positionAmt"]))
                    for p in positions) / total_qty

    tp_price_raw = calc_tp_price(avg_entry, cfg["take_profit_capital_pct"], cfg["leverage"])
    tp_price = await client.get_price_precision(symbol, tp_price_raw)

    # 取消舊止盈單
    old_ids = state["tp_order_ids"].get(symbol, {})
    for order_id in old_ids.values():
        try:
            await client.cancel_order(symbol, order_id)
        except Exception:
            pass
    state["tp_order_ids"][symbol] = {}

    # 計算拆單數量
    tp_limit_pct = cfg.get("tp_limit_pct", 50)
    filters = await client.get_symbol_filters(symbol)
    if not filters:
        return
    step_size = filters["step_size"]
    precision = max(0, -int(math.log10(step_size))) if step_size > 0 else 3

    limit_qty_raw = total_qty * (tp_limit_pct / 100)
    stop_qty_raw = total_qty - limit_qty_raw
    limit_qty = round(limit_qty_raw - (limit_qty_raw % step_size), precision)
    stop_qty = round(stop_qty_raw - (stop_qty_raw % step_size), precision)

    new_ids = {}

    if limit_qty > 0:
        result = await client.place_limit_order(symbol, "BUY", limit_qty, tp_price, reduce_only=True)
        if "orderId" in result:
            new_ids["limit"] = str(result["orderId"])
            logger.info(f"✅ 止盈限價單 {symbol} @ {tp_price} qty={limit_qty}")
        else:
            logger.warning(f"止盈限價單失敗 {symbol}: {result}")

    if stop_qty > 0:
        result = await client.place_stop_market_order(symbol, "BUY", stop_qty, tp_price, reduce_only=True)
        if "orderId" in result:
            new_ids["stop"] = str(result["orderId"])
            logger.info(f"✅ 止盈Stop-Market單 {symbol} stopPrice={tp_price} qty={stop_qty}")
        else:
            logger.warning(f"止盈Stop-Market單失敗 {symbol}: {result}")

    state["tp_order_ids"][symbol] = new_ids

    write_log("TP_ORDER", f"止盈掛單更新 avg={avg_entry:.4f} tp={tp_price} limit={limit_qty} stop={stop_qty}",
              symbol=symbol, detail={
                  "avg_entry": round(avg_entry, 6),
                  "tp_price": tp_price,
                  "limit_qty": limit_qty,
                  "stop_qty": stop_qty,
                  "tp_limit_pct": tp_limit_pct,
                  "order_ids": new_ids
              })


# ===== 開倉邏輯 =====

async def try_open_position(client, cfg, symbol, entry_price, grid_level=0):
    """嘗試開空倉（掛限價單），開倉後更新止盈掛單"""
    if state["paused"] or state["margin_pause"]:
        return False
    if symbol in state["roe_pause_symbols"]:
        return False

    open_syms = get_open_symbols()
    if symbol not in open_syms and len(open_syms) >= cfg["max_symbols"]:
        return False

    existing = get_open_positions(symbol)
    if len(existing) >= cfg.get("max_orders_per_symbol", 20):
        logger.info(f"{symbol} 已達最大開單數，暫停開倉")
        return False

    balance = await client.get_balance()
    if not balance:
        write_log("ERROR", "帳戶餘額取得失敗", symbol=symbol,
                  detail={"api_key_len": len(cfg.get('api_key', ''))})
        return False

    total = balance["total"]
    margin_used = balance["margin_used"]
    if total > 0:
        margin_ratio = (margin_used / total) * 100
        if margin_ratio >= cfg["margin_usage_limit_pct"]:
            state["margin_pause"] = True
            logger.warning(f"保證金使用率 {margin_ratio:.1f}% 超過上限，暫停開倉")
            return False

    # 設定全倉模式，再設槓桿
    await client.set_margin_type(symbol, "CROSSED")
    await client.set_leverage(symbol, cfg["leverage"])

    base_notional = get_notional(cfg, total)
    existing_count = len(get_open_positions(symbol))
    scale_after = cfg.get("scale_after_order", 10)
    scale_mult = cfg.get("scale_multiplier", 1.5)
    if existing_count >= scale_after:
        notional = base_notional * scale_mult
        logger.info(f"加碼模式：第{existing_count+1}單 x{scale_mult} = {notional:.2f}")
    else:
        notional = base_notional

    quantity = await client.get_quantity_precision(symbol, notional, entry_price)
    if not quantity or quantity <= 0:
        return False

    price = await client.get_price_precision(symbol, entry_price)
    margin = notional / cfg["leverage"]

    result = await client.place_limit_order(symbol, "SELL", quantity, price)
    if "orderId" not in result:
        logger.error(f"下單失敗 {symbol}: {result}")
        write_log("ERROR", f"下單失敗: {result.get('msg', 'unknown')}", symbol=symbol,
                  detail={"entry_price": entry_price, "grid_level": grid_level, "error": result})
        return False

    order_id = str(result["orderId"])
    logger.info(f"✅ 掛單成功 {symbol} @ {price} qty={quantity} grid_level={grid_level}")
    write_log("ORDER", f"掛限價空單 @ {price}", symbol=symbol,
              detail={"order_id": order_id, "price": price, "quantity": quantity,
                      "notional": notional, "margin": margin, "leverage": cfg["leverage"],
                      "grid_level": grid_level, "account_balance": total})

    add_position(symbol, order_id, price, quantity, notional, margin, cfg["leverage"], grid_level)

    if symbol not in state["pending_orders"]:
        state["pending_orders"][symbol] = []
    state["pending_orders"][symbol].append(order_id)

    if symbol not in state["pending_grid_prices"]:
        state["pending_grid_prices"][symbol] = set()
    state["pending_grid_prices"][symbol].add(price)

    # 每次開倉後重新計算並更新止盈掛單
    await place_tp_orders(client, cfg, symbol)

    return True


# ===== 平倉邏輯 =====

async def close_symbol(client, cfg, symbol, reason="TP"):
    """平倉：取消所有掛單（含止盈單），市價平倉"""
    logger.info(f"平倉 {symbol} reason={reason}")

    await client.cancel_all_orders(symbol)

    positions = await client.get_positions(symbol)
    if not positions:
        _clear_symbol_state(symbol)
        return

    total_qty = abs(sum(float(p["positionAmt"]) for p in positions))
    if total_qty <= 0:
        _clear_symbol_state(symbol)
        return

    current_price = await client.get_price(symbol)
    if not current_price:
        return

    result = await client.place_market_order(symbol, "BUY", total_qty, reduce_only=True)
    logger.info(f"平倉結果 {symbol}: {result}")

    close_result = close_positions(symbol, current_price, reason)
    if close_result:
        logger.info(f"💰 平倉完成 {symbol} PnL={close_result['total_pnl']:.4f} ROE={close_result['roe_pct']:.2f}%")
        write_log("TP" if reason == "TP" else "FORCE_CLOSE",
                  f"平倉完成 PnL={close_result['total_pnl']:.4f} ROE={close_result['roe_pct']:.2f}%",
                  symbol=symbol,
                  detail={"avg_entry": close_result["avg_entry"],
                          "close_price": close_result["close_price"],
                          "total_pnl": close_result["total_pnl"],
                          "roe_pct": close_result["roe_pct"],
                          "position_count": close_result["position_count"],
                          "reason": reason})

    _clear_symbol_state(symbol)


def _clear_symbol_state(symbol):
    """統一清除幣種所有狀態"""
    clear_grids(symbol)
    state["pending_orders"].pop(symbol, None)
    state["pending_grid_prices"].pop(symbol, None)
    state["tp_order_ids"].pop(symbol, None)
    state["black_k_targets"].pop(symbol, None)
    state["black_k_last_k_time"].pop(symbol, None)
    state["last_grid_k_time"].pop(symbol, None)
    state["roe_pause_symbols"].discard(symbol)
    state["triggered_symbols"].discard(symbol)


# ===== 黑K偵測 =====

async def check_black_k(client, cfg, symbol):
    """
    偵測黑K（收盤 < 開盤）
    取黑K本身 + 前2根共3根最高點 = 新空單點位
    """
    klines = await client.get_klines(symbol, "1m", limit=10)
    if not klines or len(klines) < 4:
        return None

    last_k = klines[-2]
    k_open_time = last_k[0]
    open_p = float(last_k[1])
    close_p = float(last_k[4])

    if state["black_k_last_k_time"].get(symbol) == k_open_time:
        return None

    if close_p >= open_p:
        return None

    state["black_k_last_k_time"][symbol] = k_open_time

    three_ks = klines[-4:-1]
    highest = max(float(k[2]) for k in three_ks)

    logger.info(f"🖤 偵測到黑K {symbol} 最高點={highest}")
    write_log("BLACK_K", f"偵測到黑K，目標價={highest}", symbol=symbol,
              detail={"black_k_open": open_p, "black_k_close": close_p,
                      "body_pct": round((open_p - close_p) / open_p * 100, 3),
                      "three_k_high": highest,
                      "k1": {"h": float(three_ks[0][2]), "o": float(three_ks[0][1]), "c": float(three_ks[0][4])},
                      "k2": {"h": float(three_ks[1][2]), "o": float(three_ks[1][1]), "c": float(three_ks[1][4])},
                      "k3": {"h": float(three_ks[2][2]), "o": float(three_ks[2][1]), "c": float(three_ks[2][4])}})
    return highest


# ===== ROE 檢查 =====

async def check_symbol_roe(client, cfg, symbol):
    """檢查單幣種ROE，觸發保護機制"""
    positions = await client.get_positions(symbol)
    if not positions:
        return

    total_unrealized_pnl = sum(float(p["unRealizedProfit"]) for p in positions)
    total_initial_margin = sum(float(p["initialMargin"]) for p in positions)

    if total_initial_margin <= 0:
        return

    roe_pct = (total_unrealized_pnl / total_initial_margin) * 100
    capital_return_pct = roe_pct / cfg["leverage"]

    if capital_return_pct <= cfg["pause_open_capital_pct"]:
        if symbol not in state["roe_pause_symbols"]:
            state["roe_pause_symbols"].add(symbol)
            logger.warning(f"⚠️ {symbol} 本金虧損 {capital_return_pct:.1f}%，暫停開新倉")
            write_log("ROE_PAUSE", f"本金虧損 {capital_return_pct:.1f}%，暫停開倉", symbol=symbol,
                      detail={"capital_return_pct": round(capital_return_pct, 2),
                              "threshold": cfg["pause_open_capital_pct"],
                              "unrealized_pnl": round(total_unrealized_pnl, 4),
                              "margin": round(total_initial_margin, 4)})

    if capital_return_pct <= cfg["force_close_capital_pct"]:
        logger.warning(f"🔴 {symbol} 本金虧損 {capital_return_pct:.1f}%，強制平倉")
        write_log("ROE_FORCE", f"本金虧損 {capital_return_pct:.1f}%，強制平倉", symbol=symbol,
                  detail={"capital_return_pct": round(capital_return_pct, 2),
                          "threshold": cfg["force_close_capital_pct"]})
        await close_symbol(client, cfg, symbol, reason="FORCE_CLOSE")
        return

    if capital_return_pct > cfg["pause_open_capital_pct"] and symbol in state["roe_pause_symbols"]:
        state["roe_pause_symbols"].discard(symbol)
        logger.info(f"✅ {symbol} ROE回升，恢復開倉")
        write_log("ROE_RESUME", f"ROE回升，恢復開倉", symbol=symbol,
                  detail={"capital_return_pct": round(capital_return_pct, 2)})


# ===== 網格監控（K棒收盤後以高點重建）=====

async def monitor_grids(client, cfg, symbol):
    """
    監控下方網格。
    不管K棒內貫穿幾個網格，等K棒收盤後以該K棒最高點重建網格。
    """
    klines = await client.get_klines(symbol, "1m", limit=3)
    if not klines or len(klines) < 2:
        return

    last_k = klines[-2]  # 最新完成的K棒
    k_open_time = last_k[0]
    k_high = float(last_k[2])

    current_price = await client.get_price(symbol)
    if not current_price:
        return

    grids = get_grids(symbol)
    last_processed = state["last_grid_k_time"].get(symbol)
    new_k_closed = (last_processed != k_open_time)

    if new_k_closed and grids:
        # 檢查這根K棒是否有反彈貫穿網格（K棒高點 >= 最低未觸發網格價格）
        above_grids = [g for g in grids if g["price"] <= k_high]
        if above_grids:
            logger.info(f"📊 {symbol} K棒高點={k_high} 貫穿網格，重建")
            state["last_grid_k_time"][symbol] = k_open_time
            state["pending_grid_prices"].pop(symbol, None)

            grid_prices = calc_grid_prices(
                k_high, cfg["grid_spacing_pct"], cfg["grid_down_count"], "DOWN"
            )
            save_grids(symbol, grid_prices, "DOWN")
            write_log("GRID_REBUILD", f"K棒高點重建網格 high={k_high}", symbol=symbol,
                      detail={"k_high": k_high, "new_grids": grid_prices})
            return  # 本輪不立刻掛單，等下一輪

        state["last_grid_k_time"][symbol] = k_open_time

    # 正常網格監控：找最近網格掛單
    if not grids:
        return

    above_grids = [g for g in grids if g["price"] > current_price]
    if not above_grids:
        return

    nearest = min(above_grids, key=lambda g: g["price"])
    nearest_price = nearest["price"]

    already_pending = nearest_price in state.get("pending_grid_prices", {}).get(symbol, set())
    if already_pending:
        return

    open_positions = get_open_positions(symbol)
    if len(open_positions) < cfg.get("max_orders_per_symbol", 20):
        await try_open_position(client, cfg, symbol, nearest_price,
                                grid_level=len(open_positions))


# ===== 主循環 =====

async def trading_loop():
    logger.info("🚀 交易引擎啟動")

    while True:
        try:
            cfg = load_config()

            if not cfg.get("system_running", True):
                await asyncio.sleep(5)
                continue

            client = get_client(cfg)

            # 1. 更新候選池
            open_syms = get_open_symbols()
            pool_refresh_sec = cfg.get("candidate_pool_refresh_min", 3) * 60
            time_since_scan = time.time() - state["last_pool_scan"]
            has_vacancy = len(open_syms) < cfg["max_symbols"]
            need_refresh = (has_vacancy or time_since_scan >= pool_refresh_sec) and not state["paused"]

            if need_refresh:
                logger.info(f"掃描候選池（距上次 {int(time_since_scan)}秒）...")
                scanner_data = state.get("scanner_latest_result", [])
                candidates = await scan_candidates(cfg, scanner_data=scanner_data)
                state["candidate_pool"] = candidates
                state["last_scan_result"] = candidates
                state["last_pool_scan"] = time.time()
                write_log("SCAN", f"候選池更新，共 {len(candidates)} 個候選",
                          detail={"trigger": "vacancy" if has_vacancy else "timer",
                                  "seconds_since_last": int(time_since_scan),
                                  "open_symbols": list(open_syms)})

            # 2. 監控現有持倉
            for symbol in list(open_syms):
                await check_symbol_roe(client, cfg, symbol)
                if symbol not in state["roe_pause_symbols"]:
                    await monitor_grids(client, cfg, symbol)

            # 3. 候選池觸價監控
            open_syms = get_open_symbols()
            for candidate in state["candidate_pool"]:
                sym = candidate["symbol"]

                if sym not in open_syms and len(open_syms) >= cfg["max_symbols"]:
                    break
                if state["paused"] or state["margin_pause"]:
                    break

                current_price = await client.get_price(sym)
                if not current_price:
                    continue

                upper = candidate["upper_15m"]

                # 往下：首次觸碰上軌開第一單
                if current_price >= upper * 0.9995:
                    already = sym in open_syms or sym in state["triggered_symbols"]
                    if not already:
                        logger.info(f"🎯 觸價 {sym} @ {current_price} 上軌={upper}")
                        write_log("TRIGGER", f"觸碰上軌，開第一單", symbol=sym,
                                  detail={"price": current_price, "upper_15m": upper,
                                          "dist_pct": round((current_price - upper) / upper * 100, 4)})
                        state["triggered_symbols"].add(sym)
                        success = await try_open_position(client, cfg, sym, upper, grid_level=0)
                        if success:
                            open_syms = get_open_symbols()
                            grid_prices = calc_grid_prices(
                                upper, cfg["grid_spacing_pct"], cfg["grid_down_count"], "DOWN"
                            )
                            save_grids(sym, grid_prices, "DOWN")
                            logger.info(f"網格建立 {sym}: {grid_prices}")
                        else:
                            state["triggered_symbols"].discard(sym)

                # 價格離開上軌且無持倉，解鎖觸發
                if (current_price < upper * 0.998
                        and sym in state["triggered_symbols"]
                        and sym not in get_open_symbols()):
                    state["triggered_symbols"].discard(sym)

                # 往上：策略A黑K偵測（已有目標時不重複偵測）
                if current_price > upper and sym not in state["black_k_targets"]:
                    target = await check_black_k(client, cfg, sym)
                    if target:
                        state["black_k_targets"][sym] = target

                # 黑K目標觸價
                if sym in state["black_k_targets"]:
                    target_price = state["black_k_targets"][sym]
                    if current_price >= target_price * 0.9995:
                        logger.info(f"🖤 黑K目標觸價 {sym} @ {current_price}")
                        success = await try_open_position(client, cfg, sym, target_price,
                                                          grid_level=len(get_open_positions(sym)))
                        if success:
                            grid_prices = calc_grid_prices(
                                target_price, cfg["grid_spacing_pct"], cfg["grid_down_count"], "DOWN"
                            )
                            save_grids(sym, grid_prices, "DOWN")
                            state["black_k_targets"].pop(sym, None)
                            state["black_k_last_k_time"].pop(sym, None)

        except Exception as e:
            logger.error(f"交易循環錯誤: {e}", exc_info=True)

        await asyncio.sleep(10)


def start_trading_loop():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(trading_loop())
