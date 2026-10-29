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


def test_prefetch_ohlcv_parallel_fills_cache_and_skips_failures():
    from strategy_engine import prefetch_ohlcv
    import threading
    calls, lock = [], threading.Lock()

    class Ex:
        def fetch_ohlcv(self, symbol, timeframe=None, limit=None):
            with lock:
                calls.append((symbol, timeframe))
            if symbol == "BAD/USDT":
                raise RuntimeError("x")
            return [[1, 1, 1, 1, 1, 1], [2, 1, 1, 1, 1, 1]]
    caches = prefetch_ohlcv(Ex(), ["A/USDT", "BAD/USDT"], timeframes=("1h", "4h"), workers=4)
    assert caches["A/USDT"] == {"1h": [[1, 1, 1, 1, 1, 1]], "4h": [[1, 1, 1, 1, 1, 1]]}
    assert caches["BAD/USDT"] == {}
