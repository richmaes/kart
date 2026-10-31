import unittest

import numpy as np

from kartlog import analysis, data, path


class VibrationFilterTests(unittest.TestCase):
    def test_removes_vibration_preserves_slow_signal_and_phase(self):
        t = np.arange(2001) / 100
        slow = np.sin(2 * np.pi * t)
        noisy = slow + 0.5 * np.sin(2 * np.pi * 25 * t)
        acc = np.column_stack([noisy, np.zeros(len(t)), np.full(len(t), analysis.G)])
        original = acc.copy()
        filtered = analysis.vibration_filter(t, acc, 5)
        middle = slice(200, -200)
        self.assertLess(np.sqrt(np.mean((filtered[middle, 0] - slow[middle]) ** 2)), 0.01)
        self.assertGreater(np.sqrt(np.mean((noisy[middle] - slow[middle]) ** 2)), 0.3)
        np.testing.assert_allclose(filtered[:, 2], analysis.G, atol=1e-10)
        np.testing.assert_array_equal(acc, original)
        np.testing.assert_array_equal(analysis.vibration_filter(t, acc, 0), acc)
        heavy = analysis.vibration_filter(t, acc, 0.5)
        self.assertLess(np.std(heavy[middle, 0]), np.std(filtered[middle, 0]) * 0.1)

    def test_timestamp_jitter_and_gaps(self):
        t = np.arange(201) / 100
        t[1:-1] += np.sin(np.arange(199)) * 0.0001
        acc = np.tile([1., 2., 3.], (len(t), 1))
        t[100:] += 1
        acc[100:] = [4., 5., 6.]
        filtered = analysis.vibration_filter(t, acc, 5)
        np.testing.assert_allclose(filtered, acc, atol=1e-10)

    def test_invalid_parameters_samples_and_short_segments(self):
        t = np.arange(100) / 100
        acc = np.zeros((100, 3))
        for cutoff in (-1, np.nan, np.inf, 50, 80):
            with self.subTest(cutoff=cutoff), self.assertRaises(ValueError):
                analysis.vibration_filter(t, acc, cutoff)
        for times, values in ((t[:10], acc[:10]), (t[::-1], acc),
                              (t, np.full((100, 3), np.nan))):
            with self.assertRaises(ValueError):
                analysis.vibration_filter(times, values, 5)

    def test_path_uses_filter_but_preserves_raw_startup_gate(self):
        t = np.arange(1001) / 100
        acc = np.tile([0., 0., analysis.G], (len(t), 1))
        acc[:, 0] = 0.8 * np.sin(2 * np.pi * 25 * t)
        session = data.Session(1, np.rint(t * 1e6).astype(np.int64), acc, np.zeros_like(acc))
        with self.assertRaisesRegex(ValueError, "acceleration variation"):
            path.estimate(session, acc, vibration_cutoff_hz=5)
        raw = path.estimate(session, acc, startup_accel_variation_limit_g=0.1)
        filtered = path.estimate(session, acc, startup_accel_variation_limit_g=0.1,
                                 vibration_cutoff_hz=5)
        self.assertLess(np.std(filtered.velocity[400:-100, 0]),
                        np.std(raw.velocity[400:-100, 0]) * 0.1)
