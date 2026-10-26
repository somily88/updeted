import contextlib
import datetime
import sqlite3

DB_NAME = "trading_bot.db"


@contextlib.contextmanager
def get_db_connection(db_name=DB_NAME):
    """Open one SQLite transaction and always close the connection."""
    conn = sqlite3.connect(db_name, timeout=10.0)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    """Create the trade history table when it does not exist."""
    with get_db_connection() as conn:
        conn.execute('''
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


def log_trade(symbol, entry_price, exit_price, pnl_pct, status):
    """Record a closed trade."""
    with get_db_connection() as conn:
        conn.execute('''
            INSERT INTO trades (symbol, entry_price, exit_price, pnl_pct, status, timestamp)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (symbol, entry_price, exit_price, pnl_pct, status,
              datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")))


def get_recent_trades(limit=10):
    """Return the most recent trades as sqlite3.Row values."""
    with get_db_connection() as conn:
        return conn.execute('''
            SELECT symbol, entry_price, exit_price, pnl_pct, status, timestamp
            FROM trades ORDER BY id DESC LIMIT ?
        ''', (limit,)).fetchall()

