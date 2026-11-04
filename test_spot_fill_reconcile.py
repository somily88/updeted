"""Read-only Spot fill reconciliation. Exchange responses are mocked."""
import importlib
import json
import sqlite3
import sys

import pytest

import spot_fill_reconcile as rec
import trade_stats


SCHEMA = """
CREATE TABLE trades (
    id INTEGER PRIMARY KEY,
    symbol TEXT,
    entry_price REAL,
    exit_price REAL,
    pnl_pct REAL,
    status TEXT,
    timestamp TEXT,
    pnl_usdt REAL,
    strategy TEXT,
    timeframe TEXT,
    cost_quality TEXT,
    gross_pnl_usdt REAL,
    fees_usdt REAL,
    exit_reason TEXT,
    risk_usdt REAL,
    r_multiple REAL,
    excluded_from_stats INTEGER NOT NULL DEFAULT 0
);
"""


class NetworkError(Exception):
    pass


class OrderNotFound(Exception):
    pass


class BadRequest(Exception):
    pass


class FakeExchange:
    def __init__(self, trades=None, orders=None, candles=None, error=None, order_error=None):
        self.trades = list(trades or [])
        self.orders = {str(key): value for key, value in (orders or {}).items()}
        self.candles = candles or {}
        self.error = error
        self.order_error = order_error
        self.calls = []
        self.created = False

    def fetch_my_trades(self, symbol, since=None, limit=None, params=None):
        self.calls.append(("fetch_my_trades", symbol, since, limit, dict(params or {})))
        if self.error:
            raise self.error
        params = params or {}
        if params.get("orderId"):
            return [row for row in self.trades if str(row.get("order")) == str(params["orderId"])]
        since = 0 if since is None else int(since)
        batch = [row for row in self.trades if int(row.get("timestamp") or 0) >= since]
        batch.sort(key=lambda row: int(row.get("timestamp") or 0))
        if limit:
            batch = batch[:int(limit)]
        return batch

    def fetch_order(self, order_id, symbol):
        self.calls.append(("fetch_order", symbol, str(order_id)))
        if self.order_error:
            raise self.order_error
        if self.error:
            raise self.error
        return self.orders.get(str(order_id))

    def fetch_ohlcv(self, symbol, timeframe="1m", since=None, limit=None):
        self.calls.append(("fetch_ohlcv", symbol, timeframe))
        if self.error:
            raise self.error
        return list(self.candles.get(symbol, []))

    def create_order(self, *args, **kwargs):
        self.created = True
        raise AssertionError("create_order must not be called")

    def cancel_order(self, *args, **kwargs):
        self.created = True
        raise AssertionError("cancel_order must not be called")


def fill(order, side, amount, cost, fee_cost, fee_ccy, ts, price=None):
    return {
        "id": "{}-{}-{}".format(order, side, ts),
        "order": str(order),
        "symbol": "BTC/USDT",
        "side": side,
        "amount": amount,
        "cost": cost,
        "price": (cost / amount) if price is None else price,
        "timestamp": ts,
        "fee": {"cost": fee_cost, "currency": fee_ccy},
    }


def make_db(tmp_path):
    path = str(tmp_path / "trades.db")
    with sqlite3.connect(path) as conn:
        conn.executescript(SCHEMA)
    with rec._connect(path) as conn:
        rec.ensure_schema(conn)
        conn.commit()
    return path


def add_trade(path, **overrides):
    row = {
        "symbol": "BTC/USDT", "entry_price": 1.0, "exit_price": 1.0, "pnl_pct": 0.0,
        "status": "LOSS", "timestamp": "2024-06-01 12:00:00", "pnl_usdt": -4.0,
        "strategy": "trend", "timeframe": "1h", "cost_quality": "legacy_or_unverified",
        "gross_pnl_usdt": None, "fees_usdt": None, "risk_usdt": None, "r_multiple": None,
        "entry_order_ids": None, "exit_order_ids": None, "opened_at": None,
    }
    row.update(overrides)
    with rec._connect(path) as conn:
        conn.execute(
            """INSERT INTO trades
               (symbol, entry_price, exit_price, pnl_pct, status, timestamp, pnl_usdt, strategy, timeframe,
                cost_quality, gross_pnl_usdt, fees_usdt, risk_usdt, r_multiple, entry_order_ids, exit_order_ids,
                opened_at)
               VALUES (:symbol, :entry_price, :exit_price, :pnl_pct, :status, :timestamp, :pnl_usdt, :strategy,
                       :timeframe, :cost_quality, :gross_pnl_usdt, :fees_usdt, :risk_usdt, :r_multiple,
                       :entry_order_ids, :exit_order_ids, :opened_at)""",
            row,
        )
        trade_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.commit()
    return trade_id


def run(path, exchange, **kwargs):
    kwargs.setdefault("pause_sec", 0)
    kwargs.setdefault("min_age_sec", 0)
    kwargs.setdefault("sleeper", lambda _seconds: None)
    return rec.reconcile_closed_spot_trades(path, exchange, **kwargs)


def trade_row(path, trade_id):
    with rec._connect(path) as conn:
        return dict(conn.execute("SELECT * FROM trades WHERE id=?", (trade_id,)).fetchone())


def trade_ts(stamp="2024-06-01 12:00:00"):
    return int(rec._parse_ts(stamp) * 1000)


def quote_pair(ts=None):
    ts = trade_ts() if ts is None else ts
    buy = fill("11", "buy", 1.0, 100.0, 0.1, "USDT", ts)
    sell = fill("22", "sell", 1.0, 110.0, 0.11, "USDT", ts + 1000)
    return [buy, sell]


def test_match_quote_fees_and_audit_keeps_original(tmp_path):
    path = make_db(tmp_path)
    trade_id = add_trade(path, entry_order_ids="11", exit_order_ids="22", risk_usdt=20)
    ex = FakeExchange(trades=quote_pair())
    result = run(path, ex, page_limit=1)
    row = trade_row(path, trade_id)
    assert result["matched_this_run"] == 1
    assert row["cost_quality"] == "actual"
    assert row["entry_price"] == pytest.approx(100)
    assert row["exit_price"] == pytest.approx(110)
    assert row["pnl_usdt"] == pytest.approx(9.79)
    assert row["fees_usdt"] == pytest.approx(0.21)
    assert row["gross_pnl_usdt"] == pytest.approx(10)
    assert row["r_multiple"] == pytest.approx(9.79 / 20)
    assert row["status"] == "WIN"
    assert not ex.created
    assert sum(1 for call in ex.calls if call[0] == "fetch_my_trades") >= 2
    with rec._connect(path) as conn:
        original = json.loads(conn.execute(
            "SELECT original_json FROM spot_reconcile_original WHERE trade_id=?", (trade_id,)
        ).fetchone()[0])
        audits = conn.execute(
            "SELECT action FROM spot_reconcile_audit WHERE trade_id=?", (trade_id,)
        ).fetchall()
    assert original["entry_price"] == 1.0
    assert original["pnl_usdt"] == -4.0
    assert [item["action"] for item in audits] == ["matched"]
    calls_after = len(ex.calls)
    again = run(path, ex, page_limit=1)
    assert again["matched_this_run"] == 0
    assert len(ex.calls) == calls_after
    assert trade_row(path, trade_id)["pnl_usdt"] == pytest.approx(9.79)


def test_partial_fill_uses_filled_quantity(tmp_path):
    path = make_db(tmp_path)
    trade_id = add_trade(path, entry_order_ids="11", exit_order_ids="22", entry_price=999, exit_price=999)
    ts = trade_ts()
    trades = [
        fill("11", "buy", 0.4, 40.0, 0.04, "USDT", ts),
        fill("22", "sell", 0.4, 44.0, 0.044, "USDT", ts + 1000),
    ]
    ex = FakeExchange(trades=trades, orders={"11": {"amount": 1.0, "filled": 0.4, "side": "buy"}})
    run(path, ex)
    row = trade_row(path, trade_id)
    assert row["cost_quality"] == "actual"
    assert row["entry_price"] == pytest.approx(100)
    assert row["exit_price"] == pytest.approx(110)
    assert row["pnl_usdt"] == pytest.approx(44 - 0.044 - 40 - 0.04)
    assert not any(call[0] == "fetch_order" for call in ex.calls)


def test_base_fee_is_not_subtracted_twice(tmp_path):
    path = make_db(tmp_path)
    trade_id = add_trade(path, entry_order_ids="11", exit_order_ids="22")
    ts = trade_ts()
    trades = [
        fill("11", "buy", 1.0, 100.0, 0.001, "BTC", ts),
        fill("22", "sell", 0.999, 109.89, 0.05, "USDT", ts + 1000),
    ]
    run(path, FakeExchange(trades=trades))
    row = trade_row(path, trade_id)
    assert row["cost_quality"] == "actual"
    assert row["pnl_usdt"] == pytest.approx(109.89 - 0.05 - 100)
    assert row["fees_usdt"] == pytest.approx(0.05)


def test_bnb_fee_uses_exchange_candle(tmp_path):
    path = make_db(tmp_path)
    trade_id = add_trade(path, entry_order_ids="11", exit_order_ids="22")
    ts = trade_ts()
    trades = [
        fill("11", "buy", 2.0, 50.0, 0.0002, "BNB", ts),
        fill("22", "sell", 2.0, 60.0, 0.02, "USDT", ts + 1000),
    ]
    candles = {"BNB/USDT": [[ts, 590, 610, 580, 600, 1]]}
    run(path, FakeExchange(trades=trades, candles=candles))
    row = trade_row(path, trade_id)
    assert row["cost_quality"] == "actual"
    assert row["fees_usdt"] == pytest.approx(0.0002 * 600 + 0.02)
    assert row["pnl_usdt"] == pytest.approx(60 - 0.02 - 50 - 0.0002 * 600)


def test_bnb_fee_without_price_stays_unverified(tmp_path):
    path = make_db(tmp_path)
    trade_id = add_trade(path, entry_order_ids="11", exit_order_ids="22", entry_price=7, exit_price=8)
    ts = trade_ts()
    trades = [
        fill("11", "buy", 2.0, 50.0, 0.0002, "BNB", ts),
        fill("22", "sell", 2.0, 60.0, 0.02, "USDT", ts + 1000),
    ]
    result = run(path, FakeExchange(trades=trades, candles={}))
    row = trade_row(path, trade_id)
    assert row["cost_quality"] == "legacy_or_unverified"
    assert row["entry_price"] == 7
    assert row["exit_price"] == 8
    assert row["reconcile_reason"] == "fee_currency_unpriced"
    assert result["reasons"]["fee_currency_unpriced"] == 1
    assert "عمولة بعملة بلا سعر" in result["warning_ar"]


def test_no_fills_keeps_stored_prices(tmp_path):
    path = make_db(tmp_path)
    trade_id = add_trade(path, entry_order_ids="11", exit_order_ids="22", entry_price=3, exit_price=4)
    ex = FakeExchange(orders={
        "11": {"id": "11", "filled": 0, "side": "buy", "status": "canceled"},
        "22": {"id": "22", "filled": 0, "side": "sell", "status": "canceled"},
    })
    ex.order_error = OrderNotFound("missing")
    run(path, ex)
    row = trade_row(path, trade_id)
    assert row["reconcile_reason"] == "no_fills"
    assert row["entry_price"] == 3 and row["exit_price"] == 4
    assert row["cost_quality"] == "legacy_or_unverified"


def test_missing_order_id_does_not_call_exchange(tmp_path, monkeypatch):
    monkeypatch.setenv("SPOT_RECONCILE_FALLBACK_ENABLED", "false")
    path = make_db(tmp_path)
    trade_id = add_trade(path, entry_price=5, exit_price=6)
    ex = FakeExchange(trades=quote_pair())
    result = run(path, ex)
    row = trade_row(path, trade_id)
    assert ex.calls == []
    assert row["reconcile_reason"] == "order_id_missing"
    assert row["entry_price"] == 5 and row["pnl_usdt"] == -4
    assert result["matched"] == 0
    assert "معرف الأمر غير محفوظ" in result["warning_ar"]


def test_quantity_mismatch_does_not_invent_prices(tmp_path):
    path = make_db(tmp_path)
    trade_id = add_trade(path, entry_order_ids="11", exit_order_ids="22", entry_price=42, exit_price=43)
    ts = trade_ts()
    trades = [
        fill("11", "buy", 1.0, 100.0, 0.1, "USDT", ts),
        fill("22", "sell", 0.4, 44.0, 0.04, "USDT", ts + 1000),
    ]
    run(path, FakeExchange(trades=trades))
    row = trade_row(path, trade_id)
    assert row["reconcile_reason"] == "quantity_mismatch"
    assert row["entry_price"] == 42 and row["exit_price"] == 43
    assert row["cost_quality"] == "legacy_or_unverified"


def test_exchange_outage_does_not_mark_no_fills_or_log_secrets(tmp_path):
    path = make_db(tmp_path)
    trade_id = add_trade(path, entry_order_ids="11", exit_order_ids="22", entry_price=5)
    secret = "signature=super-secret-signature"
    ex = FakeExchange(error=NetworkError(secret))
    result = run(path, ex)
    row = trade_row(path, trade_id)
    assert result["exchange_unavailable"] is True
    assert result["ok"] is False
    assert row["reconcile_reason"] is None
    assert row["entry_price"] == 5
    assert row["cost_quality"] == "legacy_or_unverified"
    blob = open(path, "rb").read()
    assert b"super-secret-signature" not in blob
    with rec._connect(path) as conn:
        state = conn.execute("SELECT last_error FROM spot_reconcile_state WHERE id=1").fetchone()
    assert state["last_error"] == "exchange_unavailable"
    assert "ستُعاد المطابقة لاحقًا" in result["warning_ar"]


def test_trade_history_rejection_is_not_stored_as_no_fills(tmp_path):
    path = make_db(tmp_path)
    trade_id = add_trade(path, entry_order_ids="11", exit_order_ids="22", entry_price=5)
    result = run(path, FakeExchange(error=BadRequest("demo myTrades unsupported")))
    row = trade_row(path, trade_id)
    assert result["exchange_unavailable"] is True
    assert row["reconcile_reason"] is None
    assert row["entry_price"] == 5
    assert b"unsupported" not in open(path, "rb").read()


def test_fetch_order_used_when_my_trades_misses_the_order(tmp_path):
    path = make_db(tmp_path)
    trade_id = add_trade(path, entry_order_ids="11", exit_order_ids="22")
    orders = {
        "11": {"id": "11", "side": "buy", "filled": 1.0, "cost": 100.0, "average": 100.0,
               "fee": {"cost": 0.1, "currency": "USDT"}},
        "22": {"id": "22", "side": "sell", "filled": 1.0, "cost": 110.0, "average": 110.0,
               "fee": {"cost": 0.11, "currency": "USDT"}},
    }
    run(path, FakeExchange(orders=orders))
    row = trade_row(path, trade_id)
    assert row["cost_quality"] == "actual"
    assert row["pnl_usdt"] == pytest.approx(9.79)


def test_decision_log_order_ids_match_without_stored_columns(tmp_path):
    path = make_db(tmp_path)
    trade_id = add_trade(path, timestamp="2024-06-01 12:00:00")
    with rec._connect(path) as conn:
        conn.execute("""CREATE TABLE decision_log (
            id INTEGER PRIMARY KEY, created_at TEXT, market TEXT, symbol TEXT, outcome TEXT, order_id TEXT
        )""")
        conn.executemany(
            "INSERT INTO decision_log (created_at, market, symbol, outcome, order_id) VALUES (?, 'Spot', 'BTC/USDT', ?, ?)",
            [("2024-06-01 11:00:00", "تم فتح الصفقة", "11"),
             ("2024-06-01 11:50:00", "تم تنفيذ الإغلاق", "22")],
        )
        conn.commit()
    run(path, FakeExchange(trades=quote_pair()))
    row = trade_row(path, trade_id)
    assert row["cost_quality"] == "actual"
    assert row["entry_order_ids"] == "11"
    assert row["exit_order_ids"] == "22"


def test_decision_windows_do_not_steal_another_trades_orders():
    trades = [
        {"id": 1, "timestamp": "2024-06-01 10:00:00"},
        {"id": 2, "timestamp": "2024-06-01 16:00:00"},
    ]
    decisions = [
        {"created_at": "2024-06-01 09:30:00", "outcome": "تم فتح الصفقة", "order_id": "a-buy"},
        {"created_at": "2024-06-01 09:50:00", "outcome": "تم تنفيذ الإغلاق", "order_id": "a-sell"},
        {"created_at": "2024-06-01 14:10:00", "outcome": "تم فتح الصفقة", "order_id": "b-buy"},
        {"created_at": "2024-06-01 15:40:00", "outcome": "تم تنفيذ الإغلاق", "order_id": "b-sell"},
    ]
    assigned = rec.assign_decision_orders(trades, decisions)
    assert assigned[1] == {"entry": ["a-buy"], "exit": ["a-sell"]}
    assert assigned[2] == {"entry": ["b-buy"], "exit": ["b-sell"]}


def test_recent_trade_is_left_for_a_later_run(tmp_path):
    path = make_db(tmp_path)
    trade_id = add_trade(path, entry_order_ids="11", exit_order_ids="22",
                         timestamp="2099-01-01 00:00:00")
    ex = FakeExchange(error=NetworkError("should-not-run"))
    result = run(path, ex, min_age_sec=10 ** 9)
    assert ex.calls == []
    assert trade_row(path, trade_id)["reconcile_reason"] is None
    assert result["exchange_unavailable"] is False


def test_r_backfill_uses_stored_risk_and_skips_when_risk_missing(tmp_path):
    path = make_db(tmp_path)
    verified = add_trade(path, cost_quality="actual", pnl_usdt=-10, risk_usdt=20, r_multiple=None,
                         entry_price=100, exit_price=90)
    no_risk = add_trade(path, entry_order_ids="11", exit_order_ids="22", risk_usdt=None)
    ex = FakeExchange(trades=quote_pair())
    result = run(path, ex)
    assert trade_row(path, verified)["r_multiple"] == pytest.approx(-0.5)
    assert trade_row(path, no_risk)["r_multiple"] is None
    assert result["r_backfilled"] >= 1


def test_read_only_client_has_no_order_methods():
    fake = FakeExchange()
    client = rec.ReadOnlySpotExchange(fake)
    assert not hasattr(client, "create_order")
    assert not hasattr(client, "cancel_order")
    with pytest.raises(AttributeError):
        client.create_order("BTC/USDT", "market", "buy", 1)
    assert fake.created is False


def test_combine_fee_quality_does_not_drop_a_real_fill():
    assert rec.combine_fee_quality("actual", "actual") == "actual"
    assert rec.combine_fee_quality("unverified", "actual") == "estimated"
    assert rec.combine_fee_quality(None, None) == "unverified"
    assert rec.closed_spot_quality(True, "actual", "actual") == "simulated"
    assert rec.closed_spot_quality(False, "actual", "estimated") == "estimated"


def test_strategy_stats_use_the_same_verified_pnl(tmp_path):
    path = make_db(tmp_path)
    add_trade(path, cost_quality="actual", pnl_usdt=-56.59, strategy="trend")
    add_trade(path, cost_quality="legacy_or_unverified", pnl_usdt=-100, strategy="trend")
    add_trade(path, cost_quality="simulated", pnl_usdt=80, strategy="trend")
    stats = trade_stats.load_strategy_stats(path)
    spot = [row for row in stats if row["market"] == "spot"]
    assert len(spot) == 1
    assert spot[0]["net_pnl_usdt"] == pytest.approx(-56.59)
    assert spot[0]["trades"] == 1
    with sqlite3.connect(path) as conn:
        realized = conn.execute(
            "SELECT COALESCE(SUM(pnl_usdt), 0) FROM trades WHERE " + trade_stats.spot_verified_sql()
        ).fetchone()[0]
    assert realized == pytest.approx(-56.59)


def test_warning_mentions_unverified_count_and_match_totals():
    text = rec.warning_ar(118, 0, 118, {"order_id_missing": 100, "no_fills_in_window": 18}, matched_by_time=4)
    assert text.startswith("تنبيه: 118 صفقة سبوت غير موثّقة. ربحها غير داخل في المحقق حتى تُطابق التكلفة.")
    assert "0 طُوبقت برقم الأمر، 4 طُوبقت بالوقت وتُحسب في المحقق، 118 لم تُطابق" in text
    assert "100 معرف الأمر غير محفوظ" in text
    assert "18 لا تنفيذ داخل النافذة" in text


def test_repeated_unmatched_does_not_duplicate_the_audit(tmp_path):
    path = make_db(tmp_path)
    trade_id = add_trade(path)
    run(path, FakeExchange())
    run(path, FakeExchange())
    with rec._connect(path) as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM spot_reconcile_audit WHERE trade_id=?", (trade_id,)
        ).fetchone()[0]
        original = conn.execute(
            "SELECT COUNT(*) FROM spot_reconcile_original WHERE trade_id=?", (trade_id,)
        ).fetchone()[0]
    assert count == 1 and original == 1


@pytest.fixture()
def app_module(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "bot.db"))
    monkeypatch.setenv("DASHBOARD_USER", "admin")
    monkeypatch.setenv("DASHBOARD_PASSWORD", "secret-pass")
    monkeypatch.setenv("TRADING_ENABLED", "false")
    monkeypatch.setenv("SPOT_RECONCILE_ENABLED", "false")
    for name in ("main", "config", "database", "futures_engine", "risk_controls"):
        sys.modules.pop(name, None)
    main = importlib.import_module("main")
    main.init_db()
    return main


def test_api_state_and_strategy_stats_share_realized_pnl(app_module, monkeypatch):
    monkeypatch.setattr(app_module, "get_binance_balance", lambda ttl=10: (0.0, 0.0))
    with app_module.get_db_connection(app_module.DB_NAME) as conn:
        conn.execute(
            """INSERT INTO trades
               (symbol, entry_price, exit_price, pnl_pct, status, timestamp, pnl_usdt, strategy, timeframe, cost_quality)
               VALUES ('BTC/USDT', 1, 1, 0, 'LOSS', '2024-06-01 12:00:00', -56.59, 'trend', '1h', 'actual')"""
        )
        conn.execute(
            """INSERT INTO trades
               (symbol, entry_price, exit_price, pnl_pct, status, timestamp, pnl_usdt, strategy, timeframe, cost_quality)
               VALUES ('ETH/USDT', 1, 1, 0, 'LOSS', '2024-06-01 12:00:00', -100, 'trend', '1h', 'legacy_or_unverified')"""
        )
    from fastapi.testclient import TestClient
    import base64
    token = base64.b64encode(b"admin:secret-pass").decode()
    client = TestClient(app_module.app)
    state = client.get("/api/state", headers={"Authorization": "Basic " + token})
    assert state.status_code == 200, state.text
    body = state.json()
    assert body["stats"]["realized_total"] == pytest.approx(-56.59)
    assert body["stats"]["unverified_spot_trades"] == 1
    warning = body["stats"]["spot_reconcile"]["warning_ar"]
    assert "تنبيه: 1 صفقة سبوت غير موثّقة" in warning
    assert "لم تُطابق" in warning
    strategies = client.get("/api/strategy-stats", headers={"Authorization": "Basic " + token})
    payload = strategies.json()
    assert payload["spot_realized_usdt"] == pytest.approx(body["stats"]["realized_total"])
    spot_rows = [row for row in payload["strategies"] if row["market"] == "spot"]
    assert sum(row["net_pnl_usdt"] for row in spot_rows) == pytest.approx(-56.59)


def _qty_trade(path, **overrides):
    """Stored gross implies base qty 1.0 from entry 50 and exit 60.

    opened_at defaults to the close timestamp so a short round trip still has an
    entry window. Pass opened_at=None to cover a row that never stored an open time.
    """
    fields = dict(entry_price=50.0, exit_price=60.0, gross_pnl_usdt=10.0, pnl_usdt=-4.0,
                  pnl_pct=-1.0, entry_order_ids=None, exit_order_ids=None)
    fields.update(overrides)
    if "opened_at" not in overrides:
        fields["opened_at"] = fields.get("timestamp", "2024-06-01 12:00:00")
    return add_trade(path, **fields)


def test_time_fallback_unique_match_counts_in_realized(tmp_path):
    path = make_db(tmp_path)
    trade_id = _qty_trade(path)
    ts = trade_ts()
    ex = FakeExchange(trades=[
        fill("11", "buy", 1.0, 100.0, 0.1, "USDT", ts - 1000),
        fill("22", "sell", 1.0, 110.0, 0.11, "USDT", ts),
    ])
    result = run(path, ex)
    row = trade_row(path, trade_id)
    assert result["matched_by_time"] == 1
    assert result["matched_by_time_in_realized"] is True
    assert row["cost_quality"] == "matched_by_time"
    assert row["entry_price"] == pytest.approx(100)
    assert row["exit_price"] == pytest.approx(110)
    assert row["pnl_usdt"] == pytest.approx(9.79)
    assert row["entry_order_ids"] == "11" and row["exit_order_ids"] == "22"
    assert not ex.created
    assert "1 طُوبقت بالوقت وتُحسب في المحقق" in result["warning_ar"]
    with rec._connect(path) as conn:
        original = json.loads(conn.execute(
            "SELECT original_json FROM spot_reconcile_original WHERE trade_id=?", (trade_id,)
        ).fetchone()[0])
    assert original["entry_price"] == 50.0
    assert original["cost_quality"] == "legacy_or_unverified"
    realized = trade_stats.load_strategy_stats(path)
    assert realized[0]["net_pnl_usdt"] == pytest.approx(9.79)
    again = run(path, ex)
    assert again["matched_this_run"] == 0
    assert trade_row(path, trade_id)["pnl_usdt"] == pytest.approx(9.79)


def test_time_fallback_partial_fills_aggregate_per_order(tmp_path):
    path = make_db(tmp_path)
    trade_id = _qty_trade(path)
    ts = trade_ts()
    ex = FakeExchange(trades=[
        fill("11", "buy", 0.4, 40.0, 0.0, "USDT", ts - 2000),
        fill("11", "buy", 0.6, 60.0, 0.0, "USDT", ts - 1000),
        fill("22", "sell", 1.0, 110.0, 0.0, "USDT", ts),
    ])
    run(path, ex)
    row = trade_row(path, trade_id)
    assert row["cost_quality"] == "matched_by_time"
    assert row["entry_price"] == pytest.approx(100)
    assert row["pnl_usdt"] == pytest.approx(110 - 100)


def test_time_fallback_ambiguous_match_stays_unverified(tmp_path):
    path = make_db(tmp_path)
    trade_id = _qty_trade(path, entry_price=9, exit_price=10, gross_pnl_usdt=1.0)
    ts = trade_ts()
    ex = FakeExchange(trades=[
        fill("11", "buy", 1.0, 100.0, 0.0, "USDT", ts - 1000),
        fill("22", "sell", 1.0, 110.0, 0.0, "USDT", ts),
        fill("33", "sell", 1.0, 111.0, 0.0, "USDT", ts + 500),
    ])
    run(path, ex)
    row = trade_row(path, trade_id)
    assert row["reconcile_reason"] == "ambiguous_match"
    assert row["cost_quality"] == "legacy_or_unverified"
    assert row["entry_price"] == 9 and row["exit_price"] == 10


def test_time_fallback_does_not_assign_one_fill_to_two_trades(tmp_path):
    path = make_db(tmp_path)
    first = _qty_trade(path)
    second = _qty_trade(path)
    ts = trade_ts()
    ex = FakeExchange(trades=[
        fill("11", "buy", 1.0, 100.0, 0.0, "USDT", ts - 1000),
        fill("22", "sell", 1.0, 110.0, 0.0, "USDT", ts),
    ])
    run(path, ex)
    assert trade_row(path, first)["reconcile_reason"] == "ambiguous_match"
    assert trade_row(path, second)["reconcile_reason"] == "ambiguous_match"
    assert trade_row(path, first)["cost_quality"] == "legacy_or_unverified"
    assert trade_row(path, second)["entry_price"] == 50.0


def test_time_fallback_out_of_window_is_not_verified(tmp_path, monkeypatch):
    monkeypatch.setenv("SPOT_RECONCILE_FALLBACK_WINDOW_SEC", "180")
    path = make_db(tmp_path)
    trade_id = _qty_trade(path, entry_price=4, exit_price=5, gross_pnl_usdt=1.0)
    ts = trade_ts()
    ex = FakeExchange(trades=[
        fill("1", "buy", 0.01, 1.0, 0.0, "USDT", ts - 86400 * 1000),
        fill("11", "buy", 1.0, 100.0, 0.0, "USDT", ts + 600 * 1000),
        fill("22", "sell", 1.0, 110.0, 0.0, "USDT", ts + 600 * 1000 + 1000),
    ])
    run(path, ex)
    row = trade_row(path, trade_id)
    assert row["reconcile_reason"] == "no_fills_in_window"
    assert row["entry_price"] == 4 and row["exit_price"] == 5
    assert row["cost_quality"] == "legacy_or_unverified"


def test_time_fallback_long_hold_matches_entry_and_exit_separately(tmp_path):
    path = make_db(tmp_path)
    opened = "2024-06-01 06:00:00"
    closed = "2024-06-01 12:00:00"
    trade_id = _qty_trade(path, timestamp=closed, opened_at=opened)
    ex = FakeExchange(trades=[
        fill("11", "buy", 1.0, 100.0, 0.1, "USDT", trade_ts(opened)),
        fill("22", "sell", 1.0, 110.0, 0.11, "USDT", trade_ts(closed)),
    ])
    result = run(path, ex)
    row = trade_row(path, trade_id)
    assert result["matched_by_time"] == 1
    assert row["cost_quality"] == "matched_by_time"
    assert row["entry_price"] == pytest.approx(100)
    assert row["exit_price"] == pytest.approx(110)
    assert row["pnl_usdt"] == pytest.approx(9.79)
    assert row["entry_order_ids"] == "11" and row["exit_order_ids"] == "22"


def test_time_fallback_ambiguous_entry_stays_unverified(tmp_path):
    path = make_db(tmp_path)
    opened = "2024-06-01 06:00:00"
    closed = "2024-06-01 12:00:00"
    trade_id = _qty_trade(path, timestamp=closed, opened_at=opened, entry_price=9, exit_price=10,
                          gross_pnl_usdt=1.0)
    ex = FakeExchange(trades=[
        fill("11", "buy", 1.0, 100.0, 0.0, "USDT", trade_ts(opened) - 1000),
        fill("12", "buy", 1.0, 101.0, 0.0, "USDT", trade_ts(opened) + 1000),
        fill("22", "sell", 1.0, 110.0, 0.0, "USDT", trade_ts(closed)),
    ])
    run(path, ex)
    row = trade_row(path, trade_id)
    assert row["reconcile_reason"] == "ambiguous_match"
    assert row["cost_quality"] == "legacy_or_unverified"
    assert row["entry_price"] == 9 and row["exit_price"] == 10


def test_time_fallback_missing_open_time_stays_unverified(tmp_path):
    path = make_db(tmp_path)
    trade_id = _qty_trade(path, opened_at=None, entry_price=4, exit_price=5, gross_pnl_usdt=1.0)
    ts = trade_ts()
    ex = FakeExchange(trades=[
        fill("11", "buy", 1.0, 100.0, 0.0, "USDT", ts - 1000),
        fill("22", "sell", 1.0, 110.0, 0.0, "USDT", ts),
    ])
    run(path, ex)
    row = trade_row(path, trade_id)
    assert ex.calls == []
    assert row["reconcile_reason"] == "open_time_missing"
    assert row["entry_price"] == 4 and row["exit_price"] == 5
    assert row["cost_quality"] == "legacy_or_unverified"
    assert "وقت الفتح غير محفوظ" in rec.warning_ar(1, 0, 1, {"open_time_missing": 1})


def test_time_fallback_history_does_not_reach_the_trade(tmp_path):
    path = make_db(tmp_path)
    trade_id = _qty_trade(path, timestamp="2020-01-01 00:00:00", entry_price=4, exit_price=5, gross_pnl_usdt=1.0)
    future = trade_ts("2024-06-01 12:00:00")
    ex = FakeExchange(trades=[
        fill("11", "buy", 1.0, 100.0, 0.0, "USDT", future),
        fill("22", "sell", 1.0, 110.0, 0.0, "USDT", future + 1000),
    ])
    run(path, ex)
    row = trade_row(path, trade_id)
    assert row["reconcile_reason"] == "history_unavailable"
    assert row["entry_price"] == 4


def test_record_closed_keeps_known_fees_and_order_ids(app_module):
    quality = rec.closed_spot_quality(False, "unverified", "actual")
    assert quality == "estimated"
    app_module.record_closed_spot_trade(
        "BTC/USDT", 100, 110, 9, "WIN", 9, "trend", "1h", quality,
        gross_pnl_usdt=10, fees_usdt=1, exit_reason="TP", risk_usdt=20,
        entry_order_ids="11", exit_order_ids="22", stop_price=90,
        opened_at="2024-06-01 06:00:00",
    )
    with app_module.get_db_connection(app_module.DB_NAME) as conn:
        row = conn.execute(
            "SELECT cost_quality, entry_order_ids, exit_order_ids, r_multiple, stop_price, opened_at FROM trades"
        ).fetchone()
    assert row["cost_quality"] == "estimated"
    assert row["entry_order_ids"] == "11"
    assert row["exit_order_ids"] == "22"
    assert row["stop_price"] == 90
    assert row["opened_at"] == "2024-06-01 06:00:00"
    assert row["r_multiple"] == pytest.approx(9 / 20)
