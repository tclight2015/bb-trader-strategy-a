import asyncio
import aiohttp
import json
import time
import math
import os
import threading
import logging
from flask import Flask, jsonify, render_template, request
from datetime import datetime
from config import load_config, save_config, get_notional
from database import (
    init_db, get_open_positions, get_open_symbols,
    get_trade_history, get_daily_pnl, get_cumulative_pnl,
    add_capital_log, get_capital_log, close_positions, clear_grids,
    get_logs, get_log_summary
)
from trader import state, start_trading_loop, close_symbol, get_client

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

BINANCE_BASE = "https://fapi.binance.com"

# ===== Scanner Cache =====
scanner_cache = {
    "data": [],
    "last_updated": None,
    "is_scanning": False
}


async def fetch_json(session, url, params=None):
    try:
        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as r:
            return await r.json()
    except:
        return None


async def get_all_symbols(session):
    data = await fetch_json(session, f"{BINANCE_BASE}/fapi/v1/exchangeInfo")
    if not data or "symbols" not in data:
        return []
    symbols = []
    for s in data["symbols"]:
        try:
            if (s.get("contractType") == "PERPETUAL" and
                    s.get("quoteAsset") == "USDT" and
                    s.get("status") == "TRADING" and
                    s.get("fundingIntervalHours", 8) != 1):
                symbols.append(s["symbol"])
        except Exception:
            continue
    return symbols


async def get_klines(session, symbol):
    return await fetch_json(session, f"{BINANCE_BASE}/fapi/v1/klines", {
        "symbol": symbol, "interval": "15m", "limit": 25
    })

async def get_klines_1h(session, symbol):
    return await fetch_json(session, f"{BINANCE_BASE}/fapi/v1/klines", {
        "symbol": symbol, "interval": "1h", "limit": 25
    })

async def get_ticker_24h(session, symbol):
    """取得24H成交量"""
    data = await fetch_json(session, f"{BINANCE_BASE}/fapi/v1/ticker/24hr", {"symbol": symbol})
    if data and "quoteVolume" in data:
        return float(data["quoteVolume"])
    return 0


def calc_bollinger(klines, period=20, std_mult=2.0):
    if not klines or len(klines) < period:
        return None
    closes = [float(k[4]) for k in klines]
    window = closes[-period:]
    mean = sum(window) / period
    variance = sum((x - mean) ** 2 for x in window) / period
    std = math.sqrt(variance)
    upper = mean + std_mult * std
    lower = mean - std_mult * std
    current_price = closes[-1]
    return {"price": current_price, "upper": upper, "middle": mean,
            "lower": lower, "std": std}


async def scan_symbol(session, symbol, cfg=None):
    klines, klines_1h, volume_usdt = await asyncio.gather(
        get_klines(session, symbol),
        get_klines_1h(session, symbol),
        get_ticker_24h(session, symbol)
    )
    if not klines:
        return None

    # 成交量第一步篩選
    if cfg:
        min_vol = cfg.get("min_volume_usdt", 0)
        if min_vol > 0 and volume_usdt > 0 and volume_usdt < min_vol:
            return None

    bb = calc_bollinger(klines)
    if not bb:
        return None
    price = bb["price"]
    upper = bb["upper"]
    middle = bb["middle"]
    if price >= upper:
        return None
    band_width_pct = (upper - middle) / middle * 100
    if band_width_pct < 1.0:
        return None
    dist_to_upper_pct = (upper - price) / upper * 100

    dist_1h_pct = None
    if klines_1h:
        bb1h = calc_bollinger(klines_1h)
        if bb1h and bb1h["upper"] > 0:
            dist_1h_pct = (bb1h["upper"] - price) / bb1h["upper"] * 100

    # 前高壓力評分：過去N根K棒最高點 vs 當前上軌
    prev_high_score = 0
    lookback = cfg.get("prev_high_lookback", 5) if cfg else 5
    if lookback > 0 and len(klines) >= lookback + 1:
        recent_highs = [float(k[2]) for k in klines[-(lookback+1):-1]]
        prev_high = max(recent_highs)
        # 前高在上軌附近（上軌的98%-105%之間）→ 有壓力，加分
        if upper * 0.98 <= prev_high <= upper * 1.05:
            prev_high_score = 1.0
        elif upper * 0.95 <= prev_high <= upper * 1.10:
            prev_high_score = 0.5

    return {
        "symbol": symbol.replace("USDT", ""),
        "full_symbol": symbol,
        "price": price,
        "upper": upper,
        "middle": middle,
        "lower": bb["lower"],
        "dist_to_upper_pct": dist_to_upper_pct,
        "dist_1h_pct": dist_1h_pct,
        "band_width_pct": band_width_pct,
        "volume_usdt": volume_usdt,
        "prev_high_score": prev_high_score,
    }


async def run_scan():
    scanner_cache["is_scanning"] = True
    results = []
    try:
        async with aiohttp.ClientSession() as session:
            symbols = await get_all_symbols(session)
            if not symbols:
                return
            cfg_scan = load_config()
            batch_size = 20
            for i in range(0, len(symbols), batch_size):
                batch = symbols[i:i + batch_size]
                tasks = [scan_symbol(session, sym, cfg_scan) for sym in batch]
                batch_results = await asyncio.gather(*tasks, return_exceptions=True)
                for r in batch_results:
                    if r and not isinstance(r, Exception):
                        results.append(r)
                await asyncio.sleep(0.15)
        results.sort(key=lambda x: x["dist_to_upper_pct"])
        scanner_cache["data"] = results
        scanner_cache["last_updated"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    except Exception as e:
        logger.error(f"掃描器錯誤: {e}", exc_info=True)
    finally:
        scanner_cache["is_scanning"] = False

    # 同步給交易引擎
    from trader import state as trader_state
    cfg = load_config()
    max_dist = cfg.get("max_dist_to_upper_pct", 1.0)
    pre_scan_size = cfg.get("pre_scan_size", 20)
    pool_size = cfg.get("candidate_pool_size", 10)

    filtered = [r for r in results if r.get("dist_to_upper_pct", 999) <= max_dist]
    top_15m = filtered[:pre_scan_size]
    # 綜合評分排序：1H距上軌（主要）- 前高壓力加分（次要）
    # 前高壓力score=1.0扣0.5分（排更前），score=0.5扣0.25分
    def sort_key(x):
        dist = x.get("dist_1h_pct", 999) if x.get("dist_1h_pct") is not None else 999
        bonus = x.get("prev_high_score", 0) * 0.5
        return dist - bonus
    top_15m.sort(key=sort_key)
    final_pool = top_15m[:pool_size]

    trader_state["scanner_latest_result"] = [
        {**r, "dist_to_upper": r.get("dist_to_upper_pct", 0)}
        for r in final_pool
    ]


def run_scan_sync():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(run_scan())
    loop.close()


def background_scanner():
    while True:
        if not scanner_cache["is_scanning"]:
            run_scan_sync()
        time.sleep(60)


# ===== 帳戶資訊（從 trader state 快取讀取，不直接打 Binance）=====

def get_account_sync():
    """從 trader state 的餘額快取讀取，避免儀表板刷新打 API"""
    return state.get("balance_cache")


# ===== Flask Routes =====

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/scanner/data")
def api_scanner_data():
    return jsonify({
        "data": scanner_cache["data"],
        "last_updated": scanner_cache["last_updated"],
        "is_scanning": scanner_cache["is_scanning"],
        "count": len(scanner_cache["data"])
    })


@app.route("/api/scanner/refresh", methods=["POST"])
def api_scanner_refresh():
    if not scanner_cache["is_scanning"]:
        t = threading.Thread(target=run_scan_sync)
        t.daemon = True
        t.start()
    return jsonify({"status": "started"})


@app.route("/api/account")
def api_account():
    balance = get_account_sync()
    cfg = load_config()
    if balance:
        total = balance["total"]
        margin_used = balance["margin_used"]
        margin_ratio = (margin_used / total * 100) if total > 0 else 0
        notional_per_order = get_notional(cfg, total)
        balance["margin_ratio"] = round(margin_ratio, 2)
        balance["notional_per_order"] = round(notional_per_order, 2)
        balance["margin_limit_pct"] = cfg["margin_usage_limit_pct"]
    return jsonify({
        "balance": balance,
        "system_running": cfg.get("system_running", True),
        "paused": state["paused"],
        "margin_pause": state["margin_pause"],
        "roe_pause_symbols": list(state["roe_pause_symbols"]),
        "candidate_pool": state["candidate_pool"],
    })


@app.route("/api/positions")
def api_positions():
    positions = get_open_positions()
    by_symbol = {}
    for p in positions:
        sym = p["symbol"]
        if sym not in by_symbol:
            by_symbol[sym] = []
        by_symbol[sym].append(p)
    return jsonify({"positions": by_symbol, "symbols": list(by_symbol.keys())})


@app.route("/api/close/<symbol>", methods=["POST"])
def api_close_symbol(symbol):
    cfg = load_config()
    client = get_client(cfg)

    async def do_close():
        await close_symbol(client, cfg, symbol, reason="MANUAL")

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(do_close())
    loop.close()
    return jsonify({"status": "ok", "symbol": symbol})


@app.route("/api/reset", methods=["POST"])
def api_reset():
    from trader import reset_system
    cfg = load_config()
    client = get_client(cfg)

    async def do_reset():
        return await reset_system(client, cfg)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    result = loop.run_until_complete(do_reset())
    loop.close()
    return jsonify(result)


@app.route("/api/control", methods=["POST"])
def api_control():
    data = request.json
    action = data.get("action")

    if action == "pause":
        state["paused"] = True
        cfg = load_config()
        client = get_client(cfg)
        from trader import handle_pause
        async def do_pause():
            await handle_pause(client, cfg)
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(do_pause())
        loop.close()
    elif action == "resume":
        state["paused"] = False
        state["margin_pause"] = False
    elif action == "stop":
        cfg = load_config()
        cfg["system_running"] = False
        save_config(cfg)
    elif action == "start":
        cfg = load_config()
        cfg["system_running"] = True
        save_config(cfg)

    return jsonify({"status": "ok", "action": action})


@app.route("/api/config", methods=["GET"])
def api_config_get():
    cfg = load_config()
    safe_cfg = {k: v for k, v in cfg.items()
                if k not in ["api_key", "api_secret", "capital_transactions"]}
    return jsonify(safe_cfg)


@app.route("/api/config", methods=["POST"])
def api_config_set():
    cfg = load_config()
    data = request.json
    allowed_keys = [
        "capital_per_order_pct", "leverage", "grid_spacing_pct",
        "grid_down_count", "max_symbols",
        "candidate_pool_size", "take_profit_capital_pct",
        "tp_limit_pct",                          # 新增：止盈拆單比例
        "pause_open_capital_pct", "force_close_capital_pct",
        "margin_usage_limit_pct", "min_volume_usdt", "candidate_pool_refresh_min",
        "max_orders_per_symbol", "scale_after_order", "scale_multiplier",
        "pre_scan_size",
        "volume_shrink_lookback", "volume_shrink_threshold",
        "max_dist_to_upper_pct", "max_dist_1h_upper_pct",
        "min_band_width_pct", "prev_high_lookback",
        "volume_spike_multiplier", "single_candle_max_rise_pct",
        "system_running"
    ]
    for k in allowed_keys:
        if k in data:
            cfg[k] = data[k]
    save_config(cfg)
    return jsonify({"status": "ok"})


@app.route("/api/reports/history")
def api_history():
    return jsonify(get_trade_history(100))


@app.route("/api/reports/daily")
def api_daily():
    return jsonify(get_daily_pnl())


@app.route("/api/reports/pnl_curve")
def api_pnl_curve():
    return jsonify(get_cumulative_pnl())


@app.route("/api/capital_log", methods=["GET"])
def api_capital_log_get():
    return jsonify(get_capital_log())


@app.route("/api/capital_log", methods=["POST"])
def api_capital_log_post():
    data = request.json
    balance = get_account_sync()
    balance_after = balance["total"] if balance else 0
    add_capital_log(
        data.get("type", "DEPOSIT"),
        float(data.get("amount", 0)),
        data.get("note", ""),
        balance_after
    )
    return jsonify({"status": "ok"})


@app.route("/api/logs")
def api_logs():
    event_type = request.args.get("event_type")
    symbol = request.args.get("symbol")
    limit = int(request.args.get("limit", 200))
    return jsonify(get_logs(event_type, symbol, limit))

@app.route("/api/logs/summary")
def api_logs_summary():
    return jsonify(get_log_summary())


init_db()

_scanner_thread = threading.Thread(target=background_scanner)
_scanner_thread.daemon = True
_scanner_thread.start()

_trader_thread = threading.Thread(target=start_trading_loop)
_trader_thread.daemon = True
_trader_thread.start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
