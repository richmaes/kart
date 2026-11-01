from pathlib import Path
import tempfile
from threading import Event
import unittest
from unittest.mock import patch

import numpy as np

from kartlog import alignment, analysis, data, laptimes


def recording(values=None, offset=9.37):
    values = values or [11000, 12400, 10300, 11700, 10900]
    edges = offset + np.r_[0, np.cumsum(values) / 1000]
    t = np.arange(2, edges[-1] + 7, 0.01)
    gyro = np.tile([0.12, -0.08, 0.05], (len(t), 1))
    acc = np.tile([0.0, 0.0, analysis.G], (len(t), 1))
    for start, end in zip(edges[:-1], edges[1:]):
        mask = (t >= start) & (t < end)
        p = (t[mask] - start) / (end - start)
        turns = (1.2 * np.exp(-((p - 0.12) / 0.035) ** 2)
                 - 0.8 * np.exp(-((p - 0.43) / 0.055) ** 2)
                 + 1.7 * np.exp(-((p - 0.74) / 0.04) ** 2))
        gyro[mask, 2] += turns * 11 / (end - start)
        gyro[mask, 0] += 0.1 * turns
        acc[mask, 1] += turns * 4
        acc[mask, 0] += np.sin(p * 2 * np.pi) * 2
    return data.Session(1, np.rint(t * 1e6).astype(np.int64), acc, gyro), values, offset


class AlignmentTests(unittest.TestCase):
    def test_variable_durations_recover_offset_without_anchor(self):
        session, values, expected = recording()
        raw_acc, raw_gyro, raw_times = session.acc.copy(), session.gyro.copy(), session.time_us.copy()
        result = alignment.search(session, values)
        self.assertLess(abs(result.candidates[0].offset_s - expected), 0.15)
        self.assertGreater(result.candidates[0].similarity, 0.95)
        self.assertEqual(result.candidates[0].matched_laps, 5)
        self.assertTrue(any("physical timing line" in w for w in result.warnings))
        np.testing.assert_array_equal(session.acc, raw_acc)
        np.testing.assert_array_equal(session.gyro, raw_gyro)
        np.testing.assert_array_equal(session.time_us, raw_times)

    def test_anchor_and_partial_overlap_preserve_external_numbers(self):
        session, values, expected = recording()
        # Recording begins halfway through timing lap 2 and ends during lap 5.
        lo, hi = expected + 16, expected + sum(values) / 1000 - 4
        mask = (session.t >= lo) & (session.t <= hi)
        session = data.Session(1, session.time_us[mask], session.acc[mask], session.gyro[mask])
        crossing = expected + sum(values[:2]) / 1000
        result = alignment.search(session, values, anchor_time_s=crossing,
                                  anchor_lap=3, anchor_radius_s=0.5)
        self.assertLess(abs(result.candidates[0].offset_s - expected), 0.15)
        complete, partial = alignment.boundaries(session, values, expected)
        self.assertEqual([lap.external_number for lap in complete], [3, 4])
        self.assertEqual(partial, [2, 5])
        self.assertEqual([lap.end_us - lap.start_us for lap in complete], [10300000, 11700000])
        # Same timestamps rebased to zero require a negative timing-lap-1 offset.
        origin = session.time_us[0] / 1e6
        session.time_us -= session.time_us[0]
        rebased = alignment.search(session, values, anchor_time_s=crossing - origin,
                                   anchor_lap=3, anchor_radius_s=0.5)
        self.assertLess(abs(rebased.candidates[0].offset_s - (expected - origin)), 0.15)

    def test_equal_laps_report_ambiguity(self):
        session, values, _offset = recording([11000] * 5)
        result = alignment.search(session, values)
        self.assertTrue(any("Ambiguous" in warning for warning in result.warnings))
        self.assertGreater(len(result.candidates), 1)

    def test_full_40_lap_sequence_with_anchor(self):
        session, values, expected = recording([10000 + (i % 5) * 500 for i in range(40)])
        result = alignment.search(session, values, anchor_time_s=expected, anchor_radius_s=0.4)
        self.assertLess(abs(result.candidates[0].offset_s - expected), 0.15)
        self.assertEqual(result.candidates[0].matched_laps, 40)
        self.assertEqual(len(alignment.boundaries(session, values, expected)[0]), 40)

    def test_gap_and_missing_gyro_laps_excluded_not_interpolated(self):
        session, values, expected = recording()
        mask = ~((session.t > expected + 3) & (session.t < expected + 3.3))
        session = data.Session(1, session.time_us[mask], session.acc[mask], session.gyro[mask])
        start = expected + sum(values[:1]) / 1000
        session.gyro[(session.t > start + 3) & (session.t < start + 3.3)] = np.nan
        result = alignment.search(session, values, anchor_time_s=expected,
                                  anchor_radius_s=0.5)
        self.assertEqual(result.candidates[0].matched_laps, 3)
        self.assertTrue(any("missing gyro" in warning for warning in result.warnings))
        complete, _partial = alignment.boundaries(session, values, expected)
        self.assertEqual([lap.external_number for lap in complete if lap.include], [3, 4, 5])

    def test_invalid_and_uninformative_data_fail_explicitly(self):
        session, values, expected = recording()
        for invalid in ([], [1000], [1000] * 41, [True, 1000], [-1, 1000]):
            with self.subTest(values=invalid), self.assertRaises(ValueError):
                alignment.search(session, invalid)
        for settings in ({"anchor_time_s": np.nan}, {"anchor_time_s": expected, "anchor_lap": 0},
                         {"anchor_time_s": expected, "anchor_radius_s": -1},
                         {"anchor_time_s": -100}):
            with self.subTest(settings=settings), self.assertRaises(ValueError):
                alignment.search(session, values, **settings)
        with self.assertRaises(ValueError):
            alignment.boundaries(session, values, np.inf)
        with self.assertRaises(ValueError):
            alignment.boundaries(session, values, 10000)
        session.gyro[:] = 0.1
        with self.assertRaisesRegex(ValueError, "varying gyro"):
            alignment.search(session, values)
        session.gyro = None
        with self.assertRaisesRegex(ValueError, "gyro samples"):
            alignment.search(session, values)
        session, values, _ = recording()
        session.time_us[3] = session.time_us[2]
        with self.assertRaisesRegex(ValueError, "increasing timestamps"):
            alignment.search(session, values)

    def test_cancel_and_bad_sidecar_are_explicit(self):
        session, values, _expected = recording()
        cancelled = Event()
        cancelled.set()
        with self.assertRaisesRegex(ValueError, "cancelled"):
            alignment.search(session, values, cancel_event=cancelled)
        with tempfile.TemporaryDirectory() as directory:
            csv = Path(directory) / "test.csv"
            for saved in ({"1": []}, {"1": {"durations_ms": values, "offset_us": True,
                                          "timestamps_sha256": "0" * 64}},
                          {"1": {"durations_ms": values, "offset_us": 0,
                                  "timestamps_sha256": "not a fingerprint"}}):
                with self.subTest(saved=saved), self.assertRaises(ValueError):
                    laptimes.save(csv, {"1": values}, saved)

    def test_persistence_export_staleness_and_atomic_failure(self):
        session, values, expected = recording()
        saved = {"1": alignment.record(session, values, expected)}
        with tempfile.TemporaryDirectory() as directory:
            csv = Path(directory) / "test.csv"
            data.save_csv(csv, [session])
            laptimes.save(csv, {"1": values}, saved)
            self.assertEqual(laptimes.load(csv), {"1": values})
            restored = alignment.restore(data.load_csv(csv)[0], values, laptimes.load_alignments(csv)["1"])
            self.assertEqual(restored[0].start_us, round(expected * 1e6))
            self.assertEqual(len(restored), 5)
            before = laptimes.sidecar(csv).read_bytes()
            with patch.object(Path, "replace", side_effect=OSError("disk full")):
                with self.assertRaisesRegex(OSError, "disk full"):
                    laptimes.save(csv, {"1": values}, {})
            self.assertEqual(before, laptimes.sidecar(csv).read_bytes())
            with self.assertRaisesRegex(ValueError, "does not match"):
                alignment.restore(session, [10000] * 5, saved["1"])
            session.time_us[0] += 1
            with self.assertRaisesRegex(ValueError, "does not match"):
                alignment.restore(session, values, saved["1"])
            laptimes.save(csv, {"1": [10000] * 5}, saved)
            self.assertEqual(laptimes.load_alignments(csv), {})
            self.assertEqual(len(list(Path(directory).iterdir())), 2)


if __name__ == "__main__":
    unittest.main()
