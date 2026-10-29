"""Shared SQLite connection helpers.

The database path is read from DB_PATH (default data/trading_bot.db) so every
module (main, futures_engine, risk_controls) uses the same file. Schema creation
and trade logging live in main.py / futures_engine.py; the duplicated, unused
init_db/log_trade/get_recent_trades helpers that used a different path were removed.
"""
import contextlib
import os
import sqlite3

DB_NAME = os.getenv("DB_PATH", "data/trading_bot.db")


def open_db_connection(db_name=DB_NAME):
    """Create every SQLite connection with the same timeout and row settings."""
    conn = sqlite3.connect(db_name, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


@contextlib.contextmanager
def get_db_connection(db_name=DB_NAME):
    """Open one SQLite transaction and always close the connection."""
    conn = open_db_connection(db_name)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
