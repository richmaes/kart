from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

from kartlog import analysis, data, device, gyrocal, path
from .test_analysis import page


def stationary(seconds=40):
    timestamps = np.arange(seconds * 100 + 1, dtype=np.int64) * 10000
    acc = np.tile([0, 0, analysis.G], (len(timestamps), 1))
    gyro = np.tile([0.001, -0.002, 0.003], (len(timestamps), 1))
    temperature = np.full(len(timestamps), 25.0)
    return data.Session(1, timestamps, acc, gyro, temperature)


def points():
    return [gyrocal.Measurement(1, 16, [0, 0, (t - 20) * 0.001], [0, 0, 0],
                               1501, t) for t in (20., 25., 30.)]


class MeasurementTests(unittest.TestCase):
    def test_bias_noise_and_temperature(self):
        session = stationary()
        session.gyro += np.random.default_rng(42).normal(0, 0.001, session.gyro.shape)
        item = gyrocal.measure(session, session.acc, 1, 16)
        self.assertEqual(item.samples, 1501)
        np.testing.assert_allclose(item.bias, [0.001, -0.002, 0.003], atol=0.0001)
        np.testing.assert_allclose(item.noise, [0.001] * 3, atol=0.0001)
        self.assertEqual(item.temperature_c, 25)
        session.temperature = None
        self.assertIsNone(gyrocal.measure(session, session.acc, 1, 16).temperature_c)

    def test_invalid_intervals_and_motion(self):
        for start, end in ((1, 9), (-1, 15), (1, 41), (1, float("nan")), (1.005, 11.005)):
            with self.subTest(start=start, end=end), self.assertRaises(ValueError):
                session = stationary()
                gyrocal.measure(session, session.acc, start, end)
        for mutation in (
            lambda s: setattr(s, "gyro", None),
            lambda s: s.gyro.__setitem__((800, 0), np.nan),
            lambda s: s.gyro.__setitem__((slice(700, 1601), 0), 0.08),
            lambda s: s.acc.__setitem__((slice(700, 1601), 0), 1.0),
            lambda s: s.temperature.__setitem__(800, np.nan),
            lambda s: s.temperature.__setitem__(800, 28),
            lambda s: s.time_us.__setitem__(800, s.time_us[799]),
            lambda s: s.time_us.__setitem__(slice(800, None), s.time_us[800:] + 20000),
        ):
            session = stationary()
            mutation(session)
            with self.assertRaises(ValueError):
                gyrocal.measure(session, session.acc, 1, 16)

    def test_mean_drift_rejected_even_when_noise_passes(self):
        session = stationary()
        session.gyro[:, 2] = np.linspace(0, 0.07, len(session.time_us))
        with self.assertRaisesRegex(ValueError, "mean changes"):
            gyrocal.measure(session, session.acc, 1, 16)


class TemperatureTests(unittest.TestCase):
    def test_save_failure_is_reported_without_partial_file_or_leftover(self):
        with tempfile.TemporaryDirectory() as directory:
            filename = Path(directory) / "gyro.json"
            store = gyrocal.Store(filename)
            store.add("one", points()[0])
            original = filename.read_bytes()
            with patch.object(Path, "replace", side_effect=OSError("simulated I/O error")):
                with self.assertRaisesRegex(OSError, "simulated"):
                    store.save()
            self.assertEqual(filename.read_bytes(), original)
            self.assertEqual(list(Path(directory).iterdir()), [filename])

    def test_fit_interpolation_and_range(self):
        model = gyrocal.fit(points())
        np.testing.assert_allclose(model.correction([20, 22.5, 30]),
                                   [[0, 0, 0], [0, 0, 0.0025], [0, 0, 0.01]])
        for temperatures in ([19.9], [30.1], [np.nan]):
            with self.assertRaisesRegex(ValueError, "no extrapolation"):
                model.correction(temperatures)

    def test_insufficient_or_invalid_models(self):
        for values in (points()[:2], points() + points()[:1]):
            with self.assertRaises(ValueError):
                gyrocal.fit(values)
        missing = points()
        missing[0].temperature_c = None
        with self.assertRaises(ValueError):
            gyrocal.fit(missing)
        narrow = points()
        for i, m in enumerate(narrow):
            m.temperature_c = 25. + i
        with self.assertRaises(ValueError):
            gyrocal.fit(narrow)
        for model in (
            gyrocal.TemperatureModel([20, 25, 30], [[0, 0]] * 3),
            gyrocal.TemperatureModel([20, 25, 30], [[0, 0, np.nan]] * 3),
            gyrocal.TemperatureModel([20, 25, np.inf], [[0, 0, 0]] * 3),
            gyrocal.TemperatureModel([20, 20, 30], [[0, 0, 0]] * 3),
            gyrocal.TemperatureModel([20, 25, 100], [[0, 0, 0]] * 3),
        ):
            with self.assertRaises(ValueError):
                model.correction([25])

    def test_per_unit_store_and_no_accel_store_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            filename = Path(directory) / "gyro.json"
            store = gyrocal.Store(filename)
            for item in points():
                store.add("one", item)
            store.fit("one")
            store = gyrocal.Store(filename)
            self.assertEqual(len(store.measurements("one")), 3)
            self.assertIsNone(store.model("two"))
            np.testing.assert_allclose(store.model("one").correction([25]), [[0, 0, .005]])
            with self.assertRaises(ValueError):
                store.add(None, points()[0])
            filename.write_text('{"version": 99, "units": {}}')
            with self.assertRaisesRegex(ValueError, "version"):
                gyrocal.Store(filename)
            filename.write_text('{"version": 1, "units": []}')
            with self.assertRaisesRegex(ValueError, "units"):
                gyrocal.Store(filename)

    def test_compensation_removes_thermal_yaw_drift_not_twice(self):
        session = stationary()
        session.temperature = np.linspace(20, 30, len(session.time_us))
        model = gyrocal.fit(points())
        session.gyro = model.correction(session.temperature) + [0, 0, .004]
        raw = path.estimate(session, session.acc, fuse_tilt=False)
        compensated = path.estimate(session, session.acc, fuse_tilt=False, temperature_model=model)
        self.assertGreater(abs(np.arctan2(raw.final_orientation[1, 0],
                                         raw.final_orientation[0, 0])), .15)
        np.testing.assert_allclose(compensated.final_orientation, np.eye(3), atol=1e-12)
        np.testing.assert_allclose(compensated.gyro_bias, [0, 0, .004], atol=1e-12)
        session.temperature = None
        with self.assertRaisesRegex(ValueError, "requires v4"):
            path.estimate(session, session.acc, temperature_model=model)
        session.temperature = np.full(len(session.time_us), 31.)
        with self.assertRaisesRegex(ValueError, "no extrapolation"):
            path.estimate(session, session.acc, temperature_model=model)

    def test_explicit_updates_do_not_correct_past_drift_or_reset_velocity(self):
        session = stationary()
        session.gyro[:] = 0
        session.gyro[301:, 2] = .01
        session.acc[301:, 0] = .001  # Small unresolved accelerometer bias.
        original = path.estimate(session, session.acc, fuse_tilt=False)
        updated = path.estimate(session, session.acc, fuse_tilt=False,
                                stationary_intervals=[(4, 16)])
        self.assertEqual(updated.bias_updates, 1)
        np.testing.assert_array_equal(updated.position[:1601], original.position[:1601])
        yaw = np.arctan2(updated.final_orientation[1, 0], updated.final_orientation[0, 0])
        self.assertAlmostEqual(yaw, .12995, places=5)
        self.assertGreater(np.linalg.norm(updated.velocity[-1]), .03)
        self.assertEqual(original.bias_updates, 0)  # No automatic quiet-period updates.
        for intervals in ([(2, 16)], [(4, 16), (10, 21)], [(30, 40)]):
            with self.assertRaises(ValueError):
                path.estimate(session, session.acc, stationary_intervals=intervals)

    def test_mapped_native_temperature_and_interval_bias(self):
        session = stationary()
        session.acc[:] = [0, -analysis.G, 0]
        session.gyro[:] = [0.003, 0.004, 0.005]
        session.gyro[301:] += [0.001, 0.002, 0]
        model = gyrocal.TemperatureModel([20, 25, 30], [[.003, .004, .005]] * 3)
        result = path.estimate(session, session.acc, forward_axis="+Z",
                               axes=("-Y", "+X", "+Z"), fuse_tilt=False,
                               stationary_intervals=[(4, 16)], temperature_model=model)
        np.testing.assert_allclose(result.gyro_bias, 0, atol=1e-12)
        self.assertEqual(result.bias_updates, 1)


class TemperatureDataTests(unittest.TestCase):
    def test_csv_mixed_missing_temperature_and_invalid_shapes(self):
        session = stationary(1)
        legacy = data.Session(2, session.time_us, session.acc)
        session.temperature[3] = np.nan
        with tempfile.TemporaryDirectory() as directory:
            filename = Path(directory) / "recording.csv"
            data.save_csv(filename, [session, legacy])
            loaded = data.load_csv(filename)
            np.testing.assert_allclose(loaded[0].temperature, session.temperature, equal_nan=True)
            self.assertTrue(np.isnan(loaded[1].temperature).all())
            self.assertTrue(np.isnan(loaded[1].gyro).all())
            session.temperature = np.zeros(2)
            with self.assertRaises(ValueError):
                data.save_csv(Path(directory) / "invalid.csv", [session])
            session.temperature = np.full(len(session.time_us), np.inf)
            with self.assertRaises(ValueError):
                data.save_csv(Path(directory) / "infinite.csv", [session])

    def test_gui_v4_decoding_and_legacy_capacity(self):
        import struct
        import zlib
        for raw in (-32767, -512, 0, 1024, 32767):
            buf = bytearray(page(timestamps=range(1, 185), version=4))
            struct.pack_into("<h", buf, 52, raw)
            struct.pack_into("<I", buf, 4092, zlib.crc32(buf[:-4]))
            records = device.decode_page(bytes(buf), 0)
            self.assertEqual(len(records), 184)
            self.assertEqual(records[0][-1], round((23 + raw / 512) * 1e6))
        buf = bytearray(page(version=4))
        struct.pack_into("<h", buf, 52, -32768)
        struct.pack_into("<I", buf, 4092, zlib.crc32(buf[:-4]))
        with self.assertRaises(device.ProtocolError):
            device.decode_page(bytes(buf), 0)
        self.assertEqual(len(device.decode_page(page(version=3, timestamps=range(1, 204)), 0)), 203)
