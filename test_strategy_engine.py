import unittest

from strategy_engine import _candidate, _select_best, evaluate_symbol


class FlatExchange:
    def fetch_ohlcv(self, symbol, timeframe, limit):
        return [
            [i * 60_000, 100.0, 101.0, 99.0, 100.0, 1000.0]
            for i in range(90)
        ]


class StrategySelectorTests(unittest.TestCase):
    def test_selects_highest_strength_candidate(self):
        candidates = [
            {"strategy": "bounce", "signal_strength": 71.0},
            {"strategy": "trend", "signal_strength": 84.0},
            {"strategy": "breakout", "signal_strength": 77.0},
        ]
        self.assertEqual(_select_best(candidates)["strategy"], "trend")

    def test_candidate_strength_is_clamped_and_returned(self):
        view = {
            "atr": 2.0, "rsi": 50.0, "ema50": 100.0, "hist": 0.0,
            "ref_close": 101.0, "candles": [[123, 100, 102, 99, 101, 1]],
        }
        candidate = _candidate("trend", "4h", view, "test", signal_strength=140)
        self.assertEqual(candidate["signal_strength"], 100.0)

    def test_flat_market_returns_wait_without_strength(self):
        result = evaluate_symbol(FlatExchange(), "BTC/USDT")
        self.assertFalse(result["signal"])
        self.assertEqual(result["strategy"], "wait")
        self.assertEqual(result["signal_strength"], 0.0)


if __name__ == "__main__":
    unittest.main()
