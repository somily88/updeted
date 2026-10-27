import unittest

from backtest import walk_forward_windows


class WalkForwardWindowTests(unittest.TestCase):
    def test_windows_are_contiguous_non_overlapping_and_cover_oos(self):
        timeline = list(range(100))
        windows = walk_forward_windows(timeline, 70, 3)
        self.assertEqual(len(windows), 3)
        self.assertEqual(windows[0][0], 70)
        self.assertEqual(windows[-1][1], 100)
        self.assertTrue(all(a[1] == b[0] for a, b in zip(windows, windows[1:])))
        self.assertTrue(all(end > start for start, end in windows))

    def test_requested_folds_clamp_to_remaining_points(self):
        windows = walk_forward_windows([10, 20, 30], 2, 8)
        self.assertEqual(windows, [(30, 31)])


if __name__ == "__main__":
    unittest.main()
