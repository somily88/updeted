import unittest
from pathlib import Path
from uuid import uuid4

from database import get_db_connection
from risk_controls import daily_loss_status, portfolio_risk_status, risk_cluster


class RiskControlTests(unittest.TestCase):
    def setUp(self):
        self.db = str(Path(__file__).resolve().parent / (".risk-test-{}.sqlite".format(uuid4().hex)))

    def tearDown(self):
        for suffix in ("", "-wal", "-shm"):
            path = Path(self.db + suffix)
            if path.exists():
                path.unlink()

    def test_daily_loss_guard_persists_day_start_equity(self):
        first = daily_loss_status(self.db, "spot", 1000.0, 5.0)
        self.assertFalse(first["blocked"])
        self.assertEqual(first["baseline"], 1000.0)
        second = daily_loss_status(self.db, "spot", 949.0, 5.0)
        self.assertTrue(second["blocked"])
        self.assertAlmostEqual(second["loss_pct"], 5.1)

    def test_portfolio_caps_count_spot_and_futures_together(self):
        open_risks = [
            {"symbol": "BTC/USDT", "market": "Spot", "risk_usdt": 100.0},
            {"symbol": "BTC/USDT:USDT", "market": "Futures Demo", "risk_usdt": 80.0},
        ]
        result = portfolio_risk_status(
            open_risks, {"symbol": "ETH/USDT", "market": "Spot", "risk_usdt": 30.0},
            5000.0, max_total_pct=8.0, max_cluster_pct=4.0)
        self.assertFalse(result["allowed"])
        self.assertIn("المجموعة المترابطة", result["reason"])
        self.assertEqual(risk_cluster("SOL/USDT"), "crypto")

    def test_invalid_equity_fails_closed(self):
        result = portfolio_risk_status([], {"symbol": "BTC/USDT", "risk_usdt": 1.0}, 0.0)
        self.assertFalse(result["allowed"])

    def test_database_transaction_rolls_back_on_error(self):
        with get_db_connection(self.db) as conn:
            conn.execute("CREATE TABLE sample (value INTEGER)")
        with self.assertRaises(RuntimeError):
            with get_db_connection(self.db) as conn:
                conn.execute("INSERT INTO sample VALUES (1)")
                raise RuntimeError("rollback")
        with get_db_connection(self.db) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM sample").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
