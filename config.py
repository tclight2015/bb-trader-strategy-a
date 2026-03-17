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
    "grid_spacing_pct": 0.15,          # 網格間距%（隱形網格間距）

    # === 持倉管理 ===
    "max_symbols": 3,                   # 最多同時持倉幣種數
    "candidate_pool_size": 10,          # 候選監控池大小
    "pre_scan_size": 20,               # 從15分K取前N個再篩候選池

    # === 止盈止損（基於價格幅度%，與槓桿無關）===
    "take_profit_price_pct": 1.0,      # 止盈：SHORT價格下跌X%，1.0 = 跌1%止盈
    "force_close_price_pct": 3.0,      # 止損：SHORT價格上漲X%，3.0 = 漲3%止損
    "tp_limit_pct": 50,                # 止盈止損拆單：限價單佔%（剩餘為Stop-Market）

    # === 開倉保護（基於本金%）===
    "pause_open_capital_pct": -60.0,   # 暫停開新倉：單幣本金虧X%
    "force_close_capital_pct": -90.0,  # 強制平倉：單幣本金虧X%

    # === 保證金水位保護 ===
    "margin_usage_limit_pct": 75.0,    # 全帳戶保證金使用率上限%

    # === 掃描篩選條件 ===
    "min_volume_usdt": 5_000_000,      # 最低24H成交量（USDT），0=無限制
    "max_dist_to_upper_pct": 0.5,      # 距15分K上軌最大距離%
    "max_dist_1h_upper_pct": 1.0,      # 距1H上軌最大距離%
    "min_band_width_pct": 1.0,         # 最低BB帶寬%
    "prev_high_lookback": 5,           # 前高壓力：往前看N根K棒

    # === 異常偵測 ===
    "volume_spike_multiplier": 3.0,    # 成交量異常倍數（超過均量N倍跳過）
    "single_candle_max_rise_pct": 1.0, # 單K最大漲幅%（超過此值不開倉）

    # === 系統設定 ===
    "system_running": True,
    "candidate_pool_refresh_min": 3,   # 候選池更新間隔（分鐘）
}


def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r") as f:
            saved = json.load(f)
        cfg = DEFAULT_CONFIG.copy()
        cfg.update(saved)
        # 環境變數優先
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
    # 不存 api_key/secret 到檔案（從環境變數讀）
    save_data = {k: v for k, v in cfg.items()
                 if k not in ["api_key", "api_secret"]}
    with open(CONFIG_FILE, "w") as f:
        json.dump(save_data, f, indent=2, ensure_ascii=False)


def get_notional(cfg, account_balance):
    """計算每單名義價值"""
    margin_per_order = account_balance * (cfg["capital_per_order_pct"] / 100)
    return margin_per_order * cfg["leverage"]
