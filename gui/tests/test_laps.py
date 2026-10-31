import unittest

import numpy as np

from kartlog import laps
from kartlog.data import Session


class LapTests(unittest.TestCase):
    def setUp(self):
        self.time_us = np.arange(0, 4_000_001, 10_000, dtype=np.int64)
        self.session = Session(1, self.time_us, np.zeros((len(self.time_us), 3)))

    def test_validate_rejects_bad_and_overlapping_boundaries(self):
        first = laps.Lap(0, 1_000_000)
        laps.validate(self.session, first)
        for candidate in (
            laps.Lap(-1, 1_000_000),
            laps.Lap(2_000_000, 1_000_000),
            laps.Lap(500_000, 1_500_000),
            laps.Lap(0, 10_000),
        ):
            with self.subTest(candidate=candidate), self.assertRaises(ValueError):
                laps.validate(self.session, candidate, [first])

    def test_samples_use_half_open_boundaries_and_keep_final_sample(self):
        first = laps.Lap(0, 1_000_000)
        t, values = laps.samples(self.session, first, self.session.acc)
        self.assertEqual(t[0], 0)
        self.assertAlmostEqual(t[-1], 0.99)
        self.assertEqual(len(values), 100)

        final = laps.Lap(3_000_000, 4_000_000)
        t, _ = laps.samples(self.session, final, self.session.acc)
        self.assertAlmostEqual(t[-1], 1.0)
        laps.validate(self.session, final)

    def test_comparison_builds_elapsed_and_progress_templates(self):
        first = laps.Lap(0, 1_000_000)
        second = laps.Lap(2_000_000, 4_000_000)
        signal = np.zeros(len(self.time_us))
        signal[:101] = np.linspace(0, 1, 101)
        values = np.column_stack([signal, np.zeros(len(signal)), np.ones(len(signal))])
        values[200:401, 0] = np.linspace(0, 1, 201)
        selected = [(1, first), (2, second)]

        progress = laps.comparison(self.session, values, selected, "progress")
        self.assertEqual(len(progress["curves"]), 2)
        self.assertEqual(progress["median"].shape, (201, 3))
        np.testing.assert_allclose(progress["median"][:, 0], np.linspace(0, 1, 201),
                                   atol=0.0051)

        elapsed = laps.comparison(self.session, values, selected, "elapsed")
        self.assertEqual(len(elapsed["x"]), 201)
        self.assertAlmostEqual(elapsed["x"][0], 0)
        self.assertLessEqual(elapsed["x"][-1], 1.0)
        with self.assertRaises(ValueError):
            laps.comparison(self.session, values, selected, "unknown")

    def test_flags_report_recording_edges_gaps_and_duration_outliers(self):
        times = np.arange(401, dtype=np.int64) * 10_000
        times[250:] += 30_000
        session = Session(1, times, np.zeros((len(times), 3)))
        first = laps.Lap(int(times[0]), int(times[100]))
        second = laps.Lap(int(times[100]), int(times[200]))
        third = laps.Lap(int(times[200]), int(times[-1]))
        all_laps = [first, second, third]

        first_flags = laps.flags(session, first, all_laps)
        self.assertIn("recording edge", first_flags)
        third_flags = laps.flags(session, third, all_laps)
        self.assertIn("recording edge", third_flags)
        self.assertIn("1 gap >15 ms", third_flags)
        self.assertIn("duration >20% from median", third_flags)


if __name__ == "__main__":
    unittest.main()
