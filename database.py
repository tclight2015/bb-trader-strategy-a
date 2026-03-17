"""
資料庫模組

設計原則：DB只記已完成的歷史，不維護即時持倉狀態（即時狀態以幣安為準）

表結構：
- trade_history：每筆交易平倉後的彙總紀錄
- trade_analytics：完整交易資料，為未來學習系統設計
- daily_summary：每日績效（從trade_history計算）
- capital_log：出入金紀錄
- system_log：系統事件日誌
"""

import sqlite3
import json
from datetime import datetime, timezone, timedelta

TZ_TAIPEI = timezone(timedelta(hours=8))
DB_FILE = "trading.db"


def get_conn():
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_conn()
    c = conn.cursor()

    # 歷史交易彙總（每次平倉寫一筆）
    c.execute("""
        CREATE TABLE IF NOT EXISTS trade_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT,
            open_time TEXT,
            close_time TEXT,
            avg_entry_price REAL,
            close_price REAL,
            total_quantity REAL,
            total_margin REAL,
            total_pnl REAL,
            roe_pct REAL,
            close_reason TEXT
        )
    """)

    # 完整交易分析資料（為學習系統設計）
    c.execute("""
        CREATE TABLE IF NOT EXISTS trade_analytics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT,
            close_time TEXT,
            avg_entry_price REAL,
            close_price REAL,
            total_qty REAL,
            total_margin REAL,
            total_pnl REAL,
            roe_pct REAL,
            close_reason TEXT,
            -- 開倉時市場狀態快照
            upper_15m REAL,
            dist_15m_pct REAL,
            dist_1h_pct REAL,
            band_width_pct REAL,
            volume_usdt REAL,
            prev_high_score REAL,
            -- 出場品質追蹤（未來填入）
            price_1h_after REAL,
            price_4h_after REAL,
            -- 完整資料JSON備份
            extra_data TEXT
        )
    """)

    # 出入金紀錄
    c.execute("""
        CREATE TABLE IF NOT EXISTS capital_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            time TEXT,
            type TEXT,
            amount REAL,
            note TEXT,
            balance_after REAL
        )
    """)

    # 系統日誌
    c.execute("""
        CREATE TABLE IF NOT EXISTS system_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            time TEXT,
            event_type TEXT,
            symbol TEXT,
            detail TEXT,
            note TEXT
        )
    """)

    conn.commit()
    conn.close()


# ===== 交易歷史 =====

def record_trade_close(symbol, avg_entry, close_price, total_qty,
                       total_margin, total_pnl, roe_pct, close_reason,
                       open_time=None):
    """平倉後記錄歷史"""
    conn = get_conn()
    now = datetime.now(TZ_TAIPEI).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute("""
        INSERT INTO trade_history
        (symbol, open_time, close_time, avg_entry_price, close_price,
         total_quantity, total_margin, total_pnl, roe_pct, close_reason)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (symbol, open_time or now, now,
          avg_entry, close_price, total_qty,
          total_margin, total_pnl, roe_pct, close_reason))
    conn.commit()
    conn.close()


def add_trade_analytics(symbol, avg_entry, close_price, total_qty,
                        total_margin, total_pnl, roe_pct, close_reason,
                        market_snapshot=None):
    """記錄完整交易分析資料（為學習系統）"""
    conn = get_conn()
    snap = market_snapshot or {}
    now = datetime.now(TZ_TAIPEI).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute("""
        INSERT INTO trade_analytics
        (symbol, close_time, avg_entry_price, close_price, total_qty,
         total_margin, total_pnl, roe_pct, close_reason,
         upper_15m, dist_15m_pct, dist_1h_pct, band_width_pct,
         volume_usdt, prev_high_score, extra_data)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (symbol, now, avg_entry, close_price, total_qty,
          total_margin, total_pnl, roe_pct, close_reason,
          snap.get("upper_15m", 0),
          snap.get("dist_15m", 0),
          snap.get("dist_1h", 0),
          snap.get("band_width_pct", 0),
          snap.get("volume_usdt", 0),
          snap.get("prev_high_score", 0),
          json.dumps(snap, ensure_ascii=False)))
    conn.commit()
    conn.close()


def get_trade_history(limit=100):
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM trade_history ORDER BY close_time DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_daily_pnl():
    conn = get_conn()
    rows = conn.execute("""
        SELECT
            DATE(close_time) as date,
            COUNT(*) as trades,
            SUM(CASE WHEN total_pnl > 0 THEN 1 ELSE 0 END) as wins,
            SUM(total_pnl) as pnl
        FROM trade_history
        GROUP BY DATE(close_time)
        ORDER BY date DESC
        LIMIT 30
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_cumulative_pnl():
    conn = get_conn()
    rows = conn.execute("""
        SELECT close_time, total_pnl, symbol
        FROM trade_history
        ORDER BY close_time ASC
    """).fetchall()
    conn.close()
    data = [dict(r) for r in rows]
    cumulative = 0
    for d in data:
        cumulative += d["total_pnl"]
        d["cumulative_pnl"] = round(cumulative, 4)
    return data


# ===== 出入金 =====

def add_capital_log(type_, amount, note, balance_after):
    conn = get_conn()
    conn.execute("""
        INSERT INTO capital_log (time, type, amount, note, balance_after)
        VALUES (?, ?, ?, ?, ?)
    """, (datetime.now(TZ_TAIPEI).strftime("%Y-%m-%d %H:%M:%S"),
          type_, amount, note, balance_after))
    conn.commit()
    conn.close()


def get_capital_log():
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM capital_log ORDER BY time DESC LIMIT 50"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ===== 系統日誌 =====

def write_log(event_type, note, symbol=None, detail=None):
    conn = get_conn()
    conn.execute("""
        INSERT INTO system_log (time, event_type, symbol, detail, note)
        VALUES (?, ?, ?, ?, ?)
    """, (datetime.now(TZ_TAIPEI).strftime("%Y-%m-%d %H:%M:%S"),
          event_type, symbol,
          json.dumps(detail, ensure_ascii=False) if detail else None,
          note))
    conn.commit()
    conn.close()


def get_logs(event_type=None, symbol=None, limit=200):
    conn = get_conn()
    conditions = []
    params = []
    if event_type:
        conditions.append("event_type=?")
        params.append(event_type)
    if symbol:
        conditions.append("symbol=?")
        params.append(symbol)
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    params.append(limit)
    rows = conn.execute(
        f"SELECT * FROM system_log {where} ORDER BY time DESC LIMIT ?", params
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_log_summary():
    conn = get_conn()
    rows = conn.execute("""
        SELECT event_type, COUNT(*) as count
        FROM system_log
        GROUP BY event_type
        ORDER BY count DESC
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def export_logs_json(limit=200):
    """匯出最近N筆日誌為JSON字串，供下載分析用"""
    logs = get_logs(limit=limit)
    for log in logs:
        if log.get("detail") and isinstance(log["detail"], str):
            try:
                log["detail"] = json.loads(log["detail"])
            except Exception:
                pass
    return json.dumps(logs, ensure_ascii=False, indent=2)
