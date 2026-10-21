import sqlite3
import datetime

DB_NAME = "trading_bot.db"

def init_db():
    """إنشاء جدول الصفقات إذا لم يكن موجوداً"""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT,
            entry_price REAL,
            exit_price REAL,
            pnl_pct REAL,
            status TEXT,
            timestamp TEXT
        )
    ''')
    conn.commit()
    conn.close()

def log_trade(symbol, entry_price, exit_price, pnl_pct, status):
    """تسجيل صفقة جديدة عند إغلاقها"""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO trades (symbol, entry_price, exit_price, pnl_pct, status, timestamp)
        VALUES (?, ?, ?, ?, ?, ?)
    ''', (symbol, entry_price, exit_price, pnl_pct, status, datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    conn.commit()
    conn.close()

def get_recent_trades(limit=10):
    """جلب آخر الصفقات لعرضها في لوحة التحكم أو تيليجرام"""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute('''
        SELECT symbol, entry_price, exit_price, pnl_pct, status, timestamp 
        FROM trades ORDER BY id DESC LIMIT ?
    ''', (limit,))
    rows = cursor.fetchall()
    conn.close()
    return rows