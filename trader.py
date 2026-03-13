"""
交易引擎 — 策略A
往上：等黑K出現，取黑K本身+前2根共3根最高點作為空單點位
往下：固定間距網格，穿越後回彈觸碰再開空單
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
from config import load_config, get_notional, get_tp_roe

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


# ===== 全局狀態 =====
state = {
    "running": True,
    "paused": False,
    "last_pool_scan": 0,          # 上次候選池掃描時間（timestamp）               # 手動暫停
    "margin_pause": False,         # 保證金水位觸發暫停
    "roe_pause_symbols": set(),    # 單幣種ROE暫停
    "candidate_pool": [],          # 候選監控池 [{symbol, upper_price, ...}]
    "pending_orders": {},          # symbol -> [order_id, ...]  掛單追蹤
    "black_k_targets": {},         # symbol -> target_price  黑K訊號追蹤
    "last_scan_result": [],        # 最新掃描結果
}


def get_client(cfg):
    return BinanceClient(cfg["api_key"], cfg["api_secret"], cfg["testnet"])


# ===== 掃描邏輯（整合現有BB Scanner）=====

async def scan_candidates(cfg, scanner_data=None):
    """
    候選池直接使用掃描器已處理好的結果
    掃描器已完成：15分K初篩 → 取前N個 → 按1H距離排序 → 取前M個
    """
    if not scanner_data:
        write_log("SCAN", "掃描器資料為空，跳過本次候選池更新")
        return []

    candidates = []
    for item in scanner_data:
        try:
            candidates.append({
                "symbol": item.get("full_symbol", item.get("symbol", "") + "USDT").replace("USDT","") + "USDT" if "USDT" not in item.get("symbol","") else item.get("full_symbol", item.get("symbol","")),
                "price": float(item.get("price", 0)),
                "upper_15m": float(item.get("upper", 0)),
                "upper_1h": 0,
                "dist_15m": float(item.get("dist_to_upper", item.get("dist_to_upper_pct", 0))),
                "dist_1h": float(item.get("dist_1h_pct", 0)) if item.get("dist_1h_pct") is not None else 0,
                "band_width_pct": float(item.get("band_width_pct", 0)),
                "volume_usdt": float(item.get("volume_usdt", 0)),
                "volume_shrinking": item.get("volume_shrinking", False),
            })
        except Exception as e:
            continue

    write_log("SCAN", f"候選池更新，共 {len(candidates)} 個候選",
              detail={"from_scanner": len(scanner_data), "candidates": len(candidates)})

    return candidates


# ===== 網格計算 =====

def calc_grid_prices(base_price, grid_spacing_pct, count, direction="DOWN"):
    """計算網格價格列表"""
    prices = []
    for i in range(1, count + 1):
        if direction == "DOWN":
            p = base_price * (1 - grid_spacing_pct / 100 * i)
        else:
            p = base_price * (1 + grid_spacing_pct / 100 * i)
        prices.append(round(p, 8))
    return prices


# ===== 開倉邏輯 =====

async def try_open_position(client, cfg, symbol, entry_price, grid_level=0):
    """嘗試開空倉（掛限價單）"""
    # 檢查系統狀態
    if state["paused"] or state["margin_pause"]:
        return False
    if symbol in state["roe_pause_symbols"]:
        return False

    # 檢查持倉幣種數
    open_syms = get_open_symbols()
    if symbol not in open_syms and len(open_syms) >= cfg["max_symbols"]:
        return False

    # 檢查單幣種最大開單數
    existing = get_open_positions(symbol)
    if len(existing) >= cfg.get("max_orders_per_symbol", 20):
        logger.info(f"{symbol} 已達最大開單數 {cfg.get("max_orders_per_symbol", 20)}，暫停開倉")
        return False

    # 取帳戶餘額
    balance = await client.get_balance()
    if not balance:
        return False

    # 保證金水位檢查
    total = balance["total"]
    margin_used = balance["margin_used"]
    if total > 0:
        margin_ratio = (margin_used / total) * 100
        if margin_ratio >= cfg["margin_usage_limit_pct"]:
            state["margin_pause"] = True
            logger.warning(f"保證金使用率 {margin_ratio:.1f}% 超過上限，暫停開倉")
            return False

    # 設定槓桿
    await client.set_leverage(symbol, cfg["leverage"])

    # 計算下單數量（含加碼邏輯）
    base_notional = get_notional(cfg, total)
    existing_count = len(get_open_positions(symbol))
    scale_after = cfg.get("scale_after_order", 10)
    scale_mult = cfg.get("scale_multiplier", 1.5)
    if existing_count >= scale_after:
        notional = base_notional * scale_mult
        logger.info(f"加碼模式：第{existing_count+1}單，合約價值 x{scale_mult} = {notional:.2f}")
    else:
        notional = base_notional
    quantity = await client.get_quantity_precision(symbol, notional, entry_price)
    if not quantity or quantity <= 0:
        return False

    # 對齊價格精度
    price = await client.get_price_precision(symbol, entry_price)

    # 計算實際保證金
    margin = notional / cfg["leverage"]

    # 掛限價空單
    result = await client.place_limit_order(symbol, "SELL", quantity, price)
    if "orderId" not in result:
        logger.error(f"下單失敗 {symbol}: {result}")
        write_log("ERROR", f"下單失敗: {result.get('msg','unknown')}", symbol=symbol,
                  detail={"entry_price": entry_price, "grid_level": grid_level, "error": result})
        return False

    order_id = str(result["orderId"])
    logger.info(f"✅ 掛單成功 {symbol} @ {price} qty={quantity} grid_level={grid_level}")
    write_log("ORDER", f"掛限價空單 @ {price}", symbol=symbol,
              detail={"order_id": order_id, "price": price, "quantity": quantity,
                      "notional": notional, "margin": margin, "leverage": cfg["leverage"],
                      "grid_level": grid_level, "account_balance": total})

    # 記錄到DB（狀態為OPEN，等實際成交確認）
    add_position(symbol, order_id, price, quantity, notional, margin,
                 cfg["leverage"], grid_level)

    # 追蹤掛單
    if symbol not in state["pending_orders"]:
        state["pending_orders"][symbol] = []
    state["pending_orders"][symbol].append(order_id)

    return True


# ===== 平倉邏輯 =====

async def close_symbol(client, cfg, symbol, reason="TP"):
    """平倉：取消所有掛單，市價平倉"""
    logger.info(f"平倉 {symbol} reason={reason}")

    # 取消所有掛單
    await client.cancel_all_orders(symbol)

    # 取得當前持倉
    positions = await client.get_positions(symbol)
    if not positions:
        return

    # 計算總持倉量
    total_qty = abs(sum(float(p["positionAmt"]) for p in positions))
    if total_qty <= 0:
        return

    # 取當前價格
    current_price = await client.get_price(symbol)
    if not current_price:
        return

    # 市價平倉（SHORT用BUY平）
    result = await client.place_market_order(symbol, "BUY", total_qty, reduce_only=True)
    logger.info(f"平倉結果 {symbol}: {result}")

    # 更新DB
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

    # 清除網格和狀態
    clear_grids(symbol)
    state["pending_orders"].pop(symbol, None)
    state["black_k_targets"].pop(symbol, None)
    if symbol in state["roe_pause_symbols"]:
        state["roe_pause_symbols"].discard(symbol)


# ===== 黑K偵測（策略A往上邏輯）=====

async def check_black_k(client, cfg, symbol):
    """
    策略A：偵測黑K
    黑K = 收盤價 < 開盤價
    取黑K本身 + 前2根 共3根 K棒最高點 = 新空單點位
    """
    klines = await client.get_klines(symbol, "1m", limit=10)
    if not klines or len(klines) < 4:
        return None

    # 最新完成的K棒（倒數第2根，最後一根未收盤）
    last_k = klines[-2]
    open_p = float(last_k[1])
    close_p = float(last_k[4])

    # 判斷是否為黑K
    if close_p >= open_p:
        return None

    # 取黑K本身 + 前2根，共3根最高點
    three_ks = klines[-4:-1]  # 往前取3根（包含黑K）
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

    # 計算當前ROE（基於本金%）
    total_unrealized_pnl = sum(float(p["unRealizedProfit"]) for p in positions)
    total_initial_margin = sum(float(p["initialMargin"]) for p in positions)

    if total_initial_margin <= 0:
        return

    capital_pct = (total_unrealized_pnl / total_initial_margin) * 100 * (1 / cfg["leverage"]) * cfg["leverage"]
    # 實際用本金%計算
    roe_pct = (total_unrealized_pnl / total_initial_margin) * 100

    # 換算成本金%
    capital_return_pct = roe_pct / cfg["leverage"]

    # 暫停開倉門檻
    if capital_return_pct <= cfg["pause_open_capital_pct"]:
        if symbol not in state["roe_pause_symbols"]:
            state["roe_pause_symbols"].add(symbol)
            logger.warning(f"⚠️ {symbol} 本金虧損 {capital_return_pct:.1f}%，暫停開新倉")
            write_log("ROE_PAUSE", f"本金虧損 {capital_return_pct:.1f}%，暫停開倉", symbol=symbol,
                      detail={"capital_return_pct": round(capital_return_pct,2), "threshold": cfg["pause_open_capital_pct"], "unrealized_pnl": round(total_unrealized_pnl,4), "margin": round(total_initial_margin,4)})

    # 強制平倉門檻
    if capital_return_pct <= cfg["force_close_capital_pct"]:
        logger.warning(f"🔴 {symbol} 本金虧損 {capital_return_pct:.1f}%，強制平倉")
        write_log("ROE_FORCE", f"本金虧損 {capital_return_pct:.1f}%，強制平倉", symbol=symbol,
                  detail={"capital_return_pct": round(capital_return_pct,2), "threshold": cfg["force_close_capital_pct"]})
        await close_symbol(client, cfg, symbol, reason="FORCE_CLOSE")
        return

    # ROE回升，解除暫停
    if capital_return_pct > cfg["pause_open_capital_pct"] and symbol in state["roe_pause_symbols"]:
        state["roe_pause_symbols"].discard(symbol)
        logger.info(f"✅ {symbol} ROE回升，恢復開倉")
        write_log("ROE_RESUME", f"ROE回升，恢復開倉", symbol=symbol,
                  detail={"capital_return_pct": round(capital_return_pct,2)})


# ===== 止盈檢查 =====

async def check_take_profit(client, cfg, symbol):
    """檢查止盈條件"""
    positions = await client.get_positions(symbol)
    if not positions:
        return

    # 計算平均成本
    total_qty = sum(abs(float(p["positionAmt"])) for p in positions)
    if total_qty <= 0:
        return

    avg_entry = sum(float(p["entryPrice"]) * abs(float(p["positionAmt"]))
                    for p in positions) / total_qty

    current_price = await client.get_price(symbol)
    if not current_price:
        return

    # SHORT止盈：(entry - current) / entry
    roe_pct = (avg_entry - current_price) / avg_entry * 100 * cfg["leverage"]
    capital_return_pct = roe_pct / cfg["leverage"]

    if capital_return_pct >= cfg["take_profit_capital_pct"]:
        logger.info(f"🎯 {symbol} 止盈觸發 本金+{capital_return_pct:.1f}%")
        await close_symbol(client, cfg, symbol, reason="TP")


# ===== 網格監控 =====

async def monitor_grids(client, cfg, symbol):
    """監控下方網格：價格穿越後回彈觸碰再開空"""
    grids = get_grids(symbol)
    if not grids:
        return

    current_price = await client.get_price(symbol)
    if not current_price:
        return

    # 找到當前價格以上最近的網格（待觸發）
    above_grids = [g for g in grids if g["price"] > current_price]
    if not above_grids:
        return

    # 最近的網格
    nearest = min(above_grids, key=lambda g: g["price"])

    # 掛限價單在網格價格
    if symbol not in state["pending_orders"] or nearest["price"] not in [
        o for o in state.get("pending_order_prices", {}).get(symbol, [])
    ]:
        open_positions = get_open_positions(symbol)
        max_orders = cfg.get("max_orders_per_symbol", 20)
        if len(open_positions) < max_orders:
            await try_open_position(client, cfg, symbol, nearest["price"],
                                    grid_level=len(open_positions))


# ===== 主循環 =====

async def trading_loop():
    """主交易循環"""
    logger.info("🚀 交易引擎啟動")

    while True:
        try:
            cfg = load_config()

            if not cfg.get("system_running", True):
                await asyncio.sleep(5)
                continue

            client = get_client(cfg)

            # 1. 更新候選池（有空位 OR 距上次掃描超過N分鐘，強制更新）
            open_syms = get_open_symbols()
            pool_refresh_sec = cfg.get("candidate_pool_refresh_min", 3) * 60
            time_since_scan = time.time() - state["last_pool_scan"]
            has_vacancy = len(open_syms) < cfg["max_symbols"]
            need_refresh = (has_vacancy or time_since_scan >= pool_refresh_sec) and not state["paused"]

            if need_refresh:
                logger.info(f"掃描候選池（距上次 {int(time_since_scan)}秒）...")
                # 傳入掃描器頁面已跑好的結果做第一段初篩
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
                # 止盈檢查
                await check_take_profit(client, cfg, symbol)

                # ROE保護
                await check_symbol_roe(client, cfg, symbol)

                # 網格監控（往下）
                if symbol not in state["roe_pause_symbols"]:
                    await monitor_grids(client, cfg, symbol)

            # 3. 候選池觸價監控（往上：黑K邏輯）
            open_syms = get_open_symbols()
            for candidate in state["candidate_pool"]:
                sym = candidate["symbol"]

                # 跳過已持倉達上限
                if sym not in open_syms and len(open_syms) >= cfg["max_symbols"]:
                    break

                if state["paused"] or state["margin_pause"]:
                    break

                # 取當前價格
                current_price = await client.get_price(sym)
                if not current_price:
                    continue

                upper = candidate["upper_15m"]

                # 觸價條件：價格觸碰到15分K上軌
                if current_price >= upper * 0.9995:  # 允許0.05%誤差
                    # 首次觸發：建立下方網格並開第一單
                    if sym not in [s for s in open_syms]:
                        logger.info(f"🎯 觸價 {sym} @ {current_price} 上軌={upper}")
                        write_log("TRIGGER", f"觸碰1分K上軌，開第一單", symbol=sym,
                                  detail={"price": current_price, "upper_15m": upper,
                                          "dist_pct": round((current_price - upper) / upper * 100, 4)})

                        # 開第一單
                        success = await try_open_position(client, cfg, sym, upper, grid_level=0)
                        if success:
                            open_syms = get_open_symbols()

                            # 建立下方網格
                            grid_prices = calc_grid_prices(
                                upper,
                                cfg["grid_spacing_pct"],
                                cfg["grid_down_count"],
                                "DOWN"
                            )
                            save_grids(sym, grid_prices, "DOWN")
                            logger.info(f"網格建立 {sym}: {grid_prices}")

                # 策略A往上：偵測黑K
                if current_price > upper:
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
                            # 重建下方網格
                            grid_prices = calc_grid_prices(
                                target_price,
                                cfg["grid_spacing_pct"],
                                cfg["grid_down_count"],
                                "DOWN"
                            )
                            save_grids(sym, grid_prices, "DOWN")
                            state["black_k_targets"].pop(sym, None)

        except Exception as e:
            logger.error(f"交易循環錯誤: {e}", exc_info=True)

        await asyncio.sleep(10)  # 每10秒檢查一次


def start_trading_loop():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(trading_loop())
