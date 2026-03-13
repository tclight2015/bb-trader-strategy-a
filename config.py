import json
import os

CONFIG_FILE = "trading_config.json"

DEFAULT_CONFIG = {
    # === 帳戶設定 ===
    "api_key": "HSfsVaFKyG7HcjV0OqDwtDfU0Su8646AkOYVcCm0vjUsVbC6bf2Ly81cKyP9cJ1p",
    "api_secret": "7p4oRCCmHV5EjbsPt065RDmSwVMFuP0oGh4O5oYwsTnfxlD81tu6LTIQ3LYrU5FA",
    "testnet": True,

    # === 開單設定 ===
    "capital_per_order_pct": 1.0,      # 每單保證金佔帳戶餘額%
    "leverage": 30,                     # 槓桿倍數
    # notional = 帳戶餘額 * capital_per_order_pct% * leverage

    # === 網格設定 ===
    "grid_spacing_pct": 0.15,          # 網格間距%
    "grid_down_count": 4,              # 往下網格數量
    "grid_up_count": 0,                # 往上網格數量（策略A=0，靠黑K）

    # === 持倉管理 ===
    "max_symbols": 3,                   # 最多持倉幣種數
    "candidate_pool_size": 10,          # 候選監控池大小

    # === 止盈止損（基於本金%，非ROE%）===
    "take_profit_capital_pct": 30.0,   # 止盈：本金賺X%
    "pause_open_capital_pct": -60.0,   # 暫停開新倉：本金虧X%
    "force_close_capital_pct": -90.0,  # 強制平倉：本金虧X%

    # === 保證金水位保護 ===
    "margin_usage_limit_pct": 75.0,    # 保證金使用率上限%（超過停開新倉）

    # === 篩選條件 ===
    "min_volume_usdt": 5_000_000,      # 最低24H成交量（USDT）
    "max_dist_to_upper_pct": 0.5,      # 距上軌最大距離%（15分K）
    "max_dist_1h_upper_pct": 1.0,      # 距上軌最大距離%（1小時K）
    "min_band_width_pct": 1.0,         # 最低帶寬%
    "prev_high_lookback": 5,           # 前高保護：往前看N根K棒

    # === 異常偵測 ===
    "volume_spike_multiplier": 3.0,    # 成交量暴增倍數（超過均量N倍跳過）
    "volume_shrink_lookback": 10,       # 量縮判斷回看K棒數
    "volume_shrink_threshold": 0.7,    # 量縮門檻（當前量 < 均量 × 此值為量縮）
    "single_candle_max_rise_pct": 1.0, # 單根K棒最大漲幅%（超過跳過）

    # === 系統設定 ===
    "system_running": True,            # 系統運行狀態
    "scan_interval_sec": 60,           # 掃描間隔秒數
    "candidate_pool_refresh_min": 3,   # 候選池強制更新間隔（分鐘）
    "max_orders_per_symbol": 20,       # 單幣種最大開單數
    "scale_after_order": 10,           # 第N單後開始加碼
    "scale_multiplier": 1.5,           # 加碼倍數

    # === 出入金紀錄（列表）===
    "capital_transactions": [],        # [{time, type, amount, note}]
}


def load_config():
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r") as f:
            saved = json.load(f)
        # Merge with defaults (in case new keys added)
        cfg = DEFAULT_CONFIG.copy()
        cfg.update(saved)
        return cfg
    return DEFAULT_CONFIG.copy()


def save_config(cfg):
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)


def get_notional(cfg, account_balance):
    """計算每單合約價值"""
    margin_per_order = account_balance * (cfg["capital_per_order_pct"] / 100)
    notional = margin_per_order * cfg["leverage"]
    return notional


def get_tp_roe(cfg):
    """將本金%止盈轉換為ROE%"""
    return cfg["take_profit_capital_pct"] * cfg["leverage"] / 100


def get_pause_roe(cfg):
    """將本金%暫停開倉轉換為ROE%"""
    return cfg["pause_open_capital_pct"] * cfg["leverage"] / 100


def get_force_close_roe(cfg):
    """將本金%強制平倉轉換為ROE%"""
    return cfg["force_close_capital_pct"] * cfg["leverage"] / 100
