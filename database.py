import sqlite3
import json
from datetime import datetime

DB_FILE = "trading.db"


def get_conn():
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_conn()
    c = conn.cursor()

    # 持倉紀錄（每個幣種的所有開倉）
    c.execute("""
        CREATE TABLE IF NOT EXISTS positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            order_id TEXT,
            side TEXT DEFAULT 'SHORT',
            entry_price REAL,
            quantity REAL,
            notional REAL,
            margin REAL,
            leverage INTEGER,
            grid_level INTEGER DEFAULT 0,
            status TEXT DEFAULT 'OPEN',  -- OPEN / CLOSED / FORCE_CLOSED
            open_time TEXT,
            close_time TEXT,
            close_price REAL,
            pnl REAL,
            roe_pct REAL,
            close_reason TEXT  -- TP / FORCE_CLOSE / MANUAL
        )
    """)

    # 歷史交易摘要（每個幣種平倉後的彙總）
    c.execute("""
        CREATE TABLE IF NOT EXISTS trade_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT,
            open_time TEXT,
            close_time TEXT,
            avg_entry_price REAL,
            close_price REAL,
            total_quantity REAL,
            total_notional REAL,
            total_margin REAL,
            total_pnl REAL,
            roe_pct REAL,
            position_count INTEGER,
            close_reason TEXT
        )
    """)

    # 網格狀態（每個幣種當前的網格掛單）
    c.execute("""
        CREATE TABLE IF NOT EXISTS grids (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            price REAL NOT NULL,
            direction TEXT,  -- UP / DOWN
            order_id TEXT,
            status TEXT DEFAULT 'PENDING',  -- PENDING / FILLED / CANCELLED
            created_time TEXT
        )
    """)

    # 日績效摘要
    c.execute("""
        CREATE TABLE IF NOT EXISTS daily_summary (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT UNIQUE,
            total_trades INTEGER DEFAULT 0,
            winning_trades INTEGER DEFAULT 0,
            total_pnl REAL DEFAULT 0,
            win_rate REAL DEFAULT 0,
            starting_balance REAL,
            ending_balance REAL
        )
    """)

    # 出入金紀錄
    c.execute("""
        CREATE TABLE IF NOT EXISTS capital_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            time TEXT,
            type TEXT,   -- DEPOSIT / WITHDRAW
            amount REAL,
            note TEXT,
            balance_after REAL
        )
    """)

    # 系統日誌（詳細交易事件，供分析優化用）
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


# === 持倉操作 ===

def add_position(symbol, order_id, entry_price, quantity, notional, margin, leverage, grid_level=0):
    conn = get_conn()
    conn.execute("""
        INSERT INTO positions (symbol, order_id, side, entry_price, quantity, notional, margin, leverage, grid_level, open_time)
        VALUES (?, ?, 'SHORT', ?, ?, ?, ?, ?, ?, ?)
    """, (symbol, order_id, entry_price, quantity, notional, margin, leverage, grid_level,
          datetime.now().isoformat()))
    conn.commit()
    conn.close()


def get_open_positions(symbol=None):
    conn = get_conn()
    if symbol:
        rows = conn.execute(
            "SELECT * FROM positions WHERE status='OPEN' AND symbol=? ORDER BY open_time",
            (symbol,)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM positions WHERE status='OPEN' ORDER BY symbol, open_time"
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_open_symbols():
    """取得目前有持倉的幣種列表"""
    conn = get_conn()
    rows = conn.execute(
        "SELECT DISTINCT symbol FROM positions WHERE status='OPEN'"
    ).fetchall()
    conn.close()
    return [r["symbol"] for r in rows]


def close_positions(symbol, close_price, close_reason="TP"):
    """平倉：關閉該幣種所有持倉，計算PnL，寫入歷史"""
    conn = get_conn()
    positions = conn.execute(
        "SELECT * FROM positions WHERE status='OPEN' AND symbol=?", (symbol,)
    ).fetchall()

    if not positions:
        conn.close()
        return None

    total_qty = sum(p["quantity"] for p in positions)
    total_notional = sum(p["notional"] for p in positions)
    total_margin = sum(p["margin"] for p in positions)
    avg_entry = sum(p["entry_price"] * p["quantity"] for p in positions) / total_qty

    # PnL for SHORT: (entry - close) * qty
    total_pnl = (avg_entry - close_price) * total_qty
    roe_pct = (total_pnl / total_margin) * 100 if total_margin > 0 else 0

    close_time = datetime.now().isoformat()

    # Update positions
    conn.execute("""
        UPDATE positions SET status=?, close_time=?, close_price=?, pnl=?, roe_pct=?, close_reason=?
        WHERE status='OPEN' AND symbol=?
    """, (close_reason if close_reason == "FORCE_CLOSED" else "CLOSED",
          close_time, close_price, total_pnl, roe_pct, close_reason, symbol))

    # Write trade history
    conn.execute("""
        INSERT INTO trade_history
        (symbol, open_time, close_time, avg_entry_price, close_price, total_quantity,
         total_notional, total_margin, total_pnl, roe_pct, position_count, close_reason)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (symbol, positions[0]["open_time"], close_time, avg_entry, close_price,
          total_qty, total_notional, total_margin, total_pnl, roe_pct,
          len(positions), close_reason))

    conn.commit()
    conn.close()

    return {
        "symbol": symbol,
        "avg_entry": avg_entry,
        "close_price": close_price,
        "total_pnl": total_pnl,
        "roe_pct": roe_pct,
        "position_count": len(positions)
    }


# === 網格操作 ===

def save_grids(symbol, grid_prices, direction="DOWN"):
    conn = get_conn()
    # 清除舊網格
    conn.execute("DELETE FROM grids WHERE symbol=? AND status='PENDING'", (symbol,))
    for price in grid_prices:
        conn.execute("""
            INSERT INTO grids (symbol, price, direction, status, created_time)
            VALUES (?, ?, ?, 'PENDING', ?)
        """, (symbol, price, direction, datetime.now().isoformat()))
    conn.commit()
    conn.close()


def get_grids(symbol):
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM grids WHERE symbol=? AND status='PENDING' ORDER BY price DESC",
        (symbol,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def clear_grids(symbol):
    conn = get_conn()
    conn.execute("DELETE FROM grids WHERE symbol=?", (symbol,))
    conn.commit()
    conn.close()


# === 歷史查詢 ===

def get_trade_history(limit=100):
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM trade_history ORDER BY close_time DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_daily_pnl():
    """每日損益統計"""
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
    """累積損益曲線資料"""
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


def add_capital_log(type_, amount, note, balance_after):
    conn = get_conn()
    conn.execute("""
        INSERT INTO capital_log (time, type, amount, note, balance_after)
        VALUES (?, ?, ?, ?, ?)
    """, (datetime.now().isoformat(), type_, amount, note, balance_after))
    conn.commit()
    conn.close()


def get_capital_log():
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM capital_log ORDER BY time DESC LIMIT 50"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# === 系統日誌 ===

def write_log(event_type, note, symbol=None, detail=None):
    """寫入系統日誌"""
    import json as _json
    conn = get_conn()
    conn.execute("""
        INSERT INTO system_log (time, event_type, symbol, detail, note)
        VALUES (?, ?, ?, ?, ?)
    """, (datetime.now().isoformat(), event_type, symbol,
          _json.dumps(detail, ensure_ascii=False) if detail else None, note))
    conn.commit()
    conn.close()


def get_logs(event_type=None, symbol=None, limit=200):
    """查詢日誌"""
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
    """日誌統計摘要"""
    conn = get_conn()
    rows = conn.execute("""
        SELECT event_type, COUNT(*) as count
        FROM system_log
        GROUP BY event_type
        ORDER BY count DESC
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]
