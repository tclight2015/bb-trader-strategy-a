import json
import os

CONFIG_FILE = "trading_config.json"

DEFAULT_CONFIG = {
    # === 帳戶設定（從環境變數讀取）===
    "api_key": os.environ.get("BINANCE_API_KEY", ""),
    "api_secret": os.environ.get("BINANCE_API_SECRET", ""),
    "testnet": os.environ.get("BINANCE_TESTNET", "true").lower() == "true",

    # === 開單設定 ===
    "capital_per_order_pct": 1.0,      # 每單保證金佔帳戶餘額%
    "leverage": 30,                     # 槓桿倍數
    # notional = 帳戶餘額 * capital_per_order_pct% * leverage

    # === 網格設定 ===
    "grid_spacing_pct": 0.15,          # 網格間距%
    "grid_down_count": 4,              # 往下網格數量

    # === 持倉管理 ===
    "max_symbols": 3,                   # 最多持倉幣種數
    "candidate_pool_size": 10,          # 候選監控池大小
    "pre_scan_size": 20,               # 先從15分K取前N個，再從中按1H取候選池

    # === 止盈止損（基於本金%）===
    "take_profit_capital_pct": 30.0,   # 止盈：本金賺X%
    "tp_limit_pct": 50,                # 止盈拆單：限價單佔%（剩餘為Stop-Market）
    "pause_open_capital_pct": -60.0,   # 暫停開新倉：本金虧X%
    "force_close_capital_pct": -90.0,  # 強制平倉：本金虧X%

    # === 保證金水位保護 ===
    "margin_usage_limit_pct": 75.0,    # 保證金使用率上限%

    # === 篩選條件 ===
    "min_volume_usdt": 5_000_000,      # 最低24H成交量（USDT）
    "max_dist_to_upper_pct": 0.5,      # 距上軌最大距離%（15分K）
    "max_dist_1h_upper_pct": 1.0,      # 距上軌最大距離%（1小時K）
    "min_band_width_pct": 1.0,         # 最低帶寬%
    "prev_high_lookback": 5,           # 前高保護：往前看N根K棒

    # === 異常偵測 ===
    "volume_spike_multiplier": 3.0,
    "volume_shrink_lookback": 10,
    "volume_shrink_threshold": 0.7,
    "single_candle_max_rise_pct": 1.0,

    # === 系統設定 ===
    "system_running": True,
    "scan_interval_sec": 60,
    "candidate_pool_refresh_min": 3,
    "max_orders_per_symbol": 20,
    "scale_after_order": 10,
    "scale_multiplier": 1.5,

    # === 出入金紀錄 ===
    "capital_transactions": [],
}


def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r") as f:
            saved = json.load(f)
        cfg = DEFAULT_CONFIG.copy()
        cfg.update(saved)
        env_key = os.environ.get("BINANCE_API_KEY", "")
        env_secret = os.environ.get("BINANCE_API_SECRET", "")
        env_testnet = os.environ.get("BINANCE_TESTNET", "")
        if env_key:
            cfg["api_key"] = env_key
        if env_secret:
            cfg["api_secret"] = env_secret
        if env_testnet:
            cfg["testnet"] = env_testnet.lower() == "true"
        return cfg
    return DEFAULT_CONFIG.copy()


def save_config(cfg):
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)


def get_notional(cfg, account_balance):
    margin_per_order = account_balance * (cfg["capital_per_order_pct"] / 100)
    return margin_per_order * cfg["leverage"]


def get_tp_roe(cfg):
    return cfg["take_profit_capital_pct"] * cfg["leverage"] / 100


def get_pause_roe(cfg):
    return cfg["pause_open_capital_pct"] * cfg["leverage"] / 100


def get_force_close_roe(cfg):
    return cfg["force_close_capital_pct"] * cfg["leverage"] / 100
