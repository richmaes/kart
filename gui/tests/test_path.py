from pathlib import Path
import tempfile
import unittest

import numpy as np

from kartlog import analysis, data, path


class PathTests(unittest.TestCase):
    def session(self, seconds=6):
        t = np.arange(seconds * 100 + 1) / 100
        acc = np.tile([0.0, 0.0, analysis.G], (len(t), 1))
        gyro = np.zeros_like(acc)
        return data.Session(1, np.rint(t * 1e6).astype(np.int64), acc, gyro)

    def test_stationary_bias_and_tilt_produce_zero_motion(self):
        session = self.session()
        tilt = path._rotation(np.array([0.2, -0.1, 0.0]))
        session.acc = session.acc @ tilt
        session.gyro[:] = [0.01, -0.02, 0.005]
        result = path.estimate(session, session.acc)
        np.testing.assert_allclose(result.position, 0, atol=1e-12)
        np.testing.assert_allclose(result.gyro_bias, [0.01, -0.02, 0.005])
        np.testing.assert_array_equal(result.velocity[:result.rest_end], 0)

    def test_straight_constant_acceleration(self):
        session = self.session()
        session.acc[session.t > 3, 0] = 1.0
        result = path.estimate(session, session.acc)
        self.assertAlmostEqual(result.velocity[-1, 0], 3.0, delta=0.01)
        self.assertAlmostEqual(result.position[-1, 0], 4.5, delta=0.02)
        np.testing.assert_allclose(result.position[:, 1:], 0, atol=1e-12)

    def test_startup_transient_excluded_without_weakening_motion_check(self):
        session = self.session()
        session.gyro[:] = [0.01, -0.02, 0.005]
        session.gyro[:50, 0] += 0.4
        session.acc[:50, 0] += 2.0
        result = path.estimate(session, session.acc)
        np.testing.assert_allclose(result.gyro_bias, [0.01, -0.02, 0.005])
        np.testing.assert_allclose(result.position, 0, atol=1e-12)
        np.testing.assert_array_equal(result.velocity[:result.rest_end], 0)
        with self.assertRaises(ValueError):
            path.estimate(session, session.acc, settle_s=0)
        session.gyro[150:200, 0] += 0.4
        with self.assertRaisesRegex(ValueError, "gyro variation .*exceeds 0.03"):
            path.estimate(session, session.acc)

    def test_startup_acceleration_variation_limit_is_configurable(self):
        session = self.session()
        session.acc[:300, 0] = np.sin(np.linspace(0, 30 * np.pi, 300)) * 0.8
        with self.assertRaisesRegex(ValueError, "acceleration variation .*exceeds 0.0500 g"):
            path.estimate(session, session.acc)
        result = path.estimate(session, session.acc, startup_accel_variation_limit_g=0.08)
        self.assertEqual(result.position.shape, session.acc.shape)
        for limit in (0, -0.1, float("nan")):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                path.estimate(session, session.acc, startup_accel_variation_limit_g=limit)

    def test_invalid_settling_intervals(self):
        session = self.session()
        for settling in (-1, 3, 4, float("nan"), 2.999):
            with self.subTest(settling=settling), self.assertRaises(ValueError):
                path.estimate(session, session.acc, settle_s=settling)

    def test_startup_gyro_variation_limit_is_configurable(self):
        session = self.session()
        session.gyro[:, 0] = 0.2  # Constant bias alone is not variation.
        path.estimate(session, session.acc)
        session.gyro[100:301, 0] += np.where(np.arange(201) % 2, 0.04, -0.04)
        measured = float(np.linalg.norm(session.gyro[100:301].std(axis=0)))
        with self.assertRaisesRegex(ValueError, "gyro variation .*exceeds 0.03 rad/s"):
            path.estimate(session, session.acc)
        result = path.estimate(session, session.acc, startup_gyro_variation_limit_rad_s=0.05)
        self.assertEqual(result.position.shape, session.acc.shape)
        path.estimate(session, session.acc, startup_gyro_variation_limit_rad_s=measured)
        with self.assertRaisesRegex(ValueError, "gyro variation"):
            path.estimate(session, session.acc,
                          startup_gyro_variation_limit_rad_s=np.nextafter(measured, 0))
        for limit in (0, -0.1, np.nan, np.inf):
            with self.subTest(limit=limit), self.assertRaisesRegex(ValueError, "positive finite"):
                path.estimate(session, session.acc, startup_gyro_variation_limit_rad_s=limit)
        session.acc[:301:2, 0] = 2
        with self.assertRaisesRegex(ValueError, "acceleration variation"):
            path.estimate(session, session.acc, startup_gyro_variation_limit_rad_s=0.05)

    def test_rotation_tracks_gravity_without_false_translation(self):
        session = self.session()
        orientation = np.eye(3)
        omega = np.array([0.3, 0.0, 0.0])
        for index in range(301, len(session.t)):
            session.gyro[index] = omega
            previous = session.gyro[index - 1]
            orientation = orientation @ path._rotation((previous + omega) * 0.005)
            session.acc[index] = orientation.T @ [0.0, 0.0, analysis.G]
        result = path.estimate(session, session.acc)
        np.testing.assert_allclose(result.position, 0, atol=1e-10)

    def test_turning_transforms_body_acceleration_into_world(self):
        session = self.session(seconds=5)
        orientation = np.eye(3)
        for index in range(301, len(session.t)):
            session.gyro[index, 2] = 0.5
            orientation = orientation @ path._rotation(
                (session.gyro[index - 1] + session.gyro[index]) * 0.005)
            session.acc[index] = orientation.T @ [1.0, 0.0, analysis.G]
        result = path.estimate(session, session.acc)
        self.assertAlmostEqual(result.position[-1, 0], 2.0, delta=0.02)
        np.testing.assert_allclose(result.position[:, 1:], 0, atol=1e-10)

    def test_two_circles_close_with_expected_diameter(self):
        dt = 0.005
        turn_start = 4.0
        t = np.arange(0, turn_start + 4 * np.pi + dt / 2, dt)
        acc = np.tile([0.0, 0.0, analysis.G], (len(t), 1))
        gyro = np.zeros_like(acc)
        acc[(t > 3) & (t <= turn_start), 0] = 1.0
        turning = t > turn_start
        gyro[turning, 2] = 1.0
        acc[turning, 1] = 1.0  # centripetal acceleration, speed=1 m/s, radius=1 m
        session = data.Session(1, np.rint(t * 1e6).astype(np.int64), acc, gyro)
        result = path.estimate(session, acc)
        start = result.position[np.searchsorted(t, turn_start), :2]
        circle = result.position[turning, :2]
        self.assertLess(np.linalg.norm(circle[-1] - start), 0.01)
        np.testing.assert_allclose(np.ptp(circle, axis=0), [2, 2], atol=0.01)

    def test_chip_axis_mapping_recovers_rotating_gravity(self):
        session = self.session()
        mapping = path.gyro_mapping(("-Y", "+X", "+Z"))
        orientation = np.eye(3)
        accel_frame_gyro = np.zeros_like(session.gyro)
        for index in range(301, len(session.t)):
            accel_frame_gyro[index] = [0.3, 0.0, 0.0]
            orientation = orientation @ path._rotation(
                (accel_frame_gyro[index - 1] + accel_frame_gyro[index]) * 0.005)
            session.acc[index] = orientation.T @ [0.0, 0.0, analysis.G]
        session.gyro = accel_frame_gyro @ mapping
        result = path.estimate(session, session.acc, axes=("-Y", "+X", "+Z"))
        np.testing.assert_allclose(result.position, 0, atol=1e-10)
        incorrect = path.estimate(session, session.acc, axes=("+X", "+Y", "+Z"))
        self.assertGreater(np.linalg.norm(incorrect.position[-1]), 1.0)

    def test_mapping_validation_and_signed_permutation(self):
        mapping = path.gyro_mapping(("+Y", "-X", "+Z"))
        np.testing.assert_array_equal(mapping @ [1, 2, 3], [2, -1, 3])
        for axes in (("+X", "+X", "+Z"), ("-X", "+Y", "+Z"), ("bogus", "+Y", "+Z")):
            with self.subTest(axes=axes), self.assertRaises(ValueError):
                path.gyro_mapping(axes)
        session = self.session()
        session.gyro[:] = [0.01, 0.02, 0.03]
        result = path.estimate(session, session.acc, axes=("+Y", "-X", "+Z"))
        np.testing.assert_allclose(result.gyro_bias, [0.02, -0.01, 0.03])

    def test_invalid_inputs_are_explicit_errors(self):
        for kind in ("missing", "nan", "gap", "reverse", "noise", "gyro_noise", "gravity"):
            session = self.session()
            if kind == "missing":
                session.gyro = None
            elif kind == "nan":
                session.gyro[400, 0] = np.nan
            elif kind == "gap":
                session.time_us[400:] += 20_000
            elif kind == "reverse":
                session.time_us[1] = 0
            elif kind == "noise":
                session.acc[:300:2, 0] = 2.0
            elif kind == "gyro_noise":
                session.gyro[:300:2, 0] = 1.0
            else:
                session.acc[:, 2] = analysis.G * 1.2
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                path.estimate(session, session.acc)
        session = self.session()
        for rest in (0, -1, float("nan"), 7):
            with self.subTest(rest=rest), self.assertRaises(ValueError):
                path.estimate(session, session.acc, rest)


class TiltFusionTests(unittest.TestCase):
    session = PathTests.session

    def test_gravity_observation_gates_at_thresholds(self):
        g = analysis.G
        accepted = path._gravity_correction(np.eye(3), np.array([0., 0., g]), g, 0.019, 0.099)
        np.testing.assert_array_equal(accepted, [0, 0, 0])
        cases = [
            (np.array([0., 0., 1.031 * g]), 0.0, 0.0),
            (np.array([0., 0., g]), 0.021, 0.0),
            (np.array([0., 0., g]), 0.0, 0.101),
            (np.array([0.081, 0., np.sqrt(1 - 0.081 ** 2)]) * g, 0.0, 0.0),
            (np.array([0., 0., -g]), 0.0, 0.0),
        ]
        for measured, noise, rate in cases:
            with self.subTest(measured=measured, noise=noise, rate=rate):
                self.assertIsNone(path._gravity_correction(np.eye(3), measured, g, noise, rate))

    def test_correction_requires_half_second_and_stops_on_maneuver(self):
        session = self.session(seconds=4)
        session.gyro[session.t > 3, 0] = 0.003
        before = data.Session(1, session.time_us[:350], session.acc[:350], session.gyro[:350])
        self.assertEqual(path.estimate(before, before.acc).tilt_corrections, 0)
        self.assertGreater(path.estimate(session, session.acc).tilt_corrections, 0)
        session.acc[session.t >= 3.6, 0] = 2.0
        before_maneuver = data.Session(
            1, session.time_us[:360], session.acc[:360], session.gyro[:360])
        expected = path.estimate(before_maneuver, before_maneuver.acc).tilt_corrections
        self.assertEqual(path.estimate(session, session.acc).tilt_corrections, expected)

    def test_small_roll_and_pitch_drift_is_reduced(self):
        for axis in (0, 1):
            session = self.session(seconds=25)
            session.gyro[session.t > 3, axis] = 0.003
            raw = path.estimate(session, session.acc, fuse_tilt=False)
            fused = path.estimate(session, session.acc, fuse_tilt=True)
            raw_tilt = np.arccos(np.clip(raw.final_orientation[2, 2], -1, 1))
            fused_tilt = np.arccos(np.clip(fused.final_orientation[2, 2], -1, 1))
            with self.subTest(axis=axis):
                self.assertLess(fused_tilt, raw_tilt * 0.3)
                self.assertLess(np.linalg.norm(fused.position[-1]),
                                np.linalg.norm(raw.position[-1]) * 0.6)
                self.assertGreater(fused.tilt_corrections, 0)
                np.testing.assert_allclose(fused.final_orientation @ fused.final_orientation.T,
                                           np.eye(3), atol=1e-10)

    def test_no_gravity_correction_during_braking_cornering_or_impacts(self):
        for kind in ("acceleration", "braking", "cornering", "impact", "vibration"):
            session = self.session()
            moving = session.t > 3
            if kind == "acceleration":
                session.acc[moving, 0] = 2.0
            elif kind == "braking":
                session.acc[moving, 0] = -2.0
            elif kind == "cornering":
                session.gyro[moving, 2] = 0.5
                session.acc[moving, 1] = 0.3
            elif kind == "impact":
                session.acc[moving, 2] = 2 * analysis.G
            else:
                session.acc[moving, 0] = np.where(np.arange(moving.sum()) % 2, 0.4, -0.4)
            raw = path.estimate(session, session.acc, fuse_tilt=False)
            fused = path.estimate(session, session.acc, fuse_tilt=True)
            with self.subTest(kind=kind):
                self.assertEqual(fused.tilt_corrections, 0)
                np.testing.assert_allclose(fused.position, raw.position)
                np.testing.assert_allclose(fused.final_orientation, raw.final_orientation)

    def test_gravity_does_not_remove_yaw_drift(self):
        session = self.session(seconds=12)
        session.gyro[session.t > 3, 2] = 0.02
        raw = path.estimate(session, session.acc, fuse_tilt=False)
        fused = path.estimate(session, session.acc, fuse_tilt=True)
        np.testing.assert_allclose(fused.final_orientation, raw.final_orientation, atol=1e-12)
        self.assertGreater(abs(np.arctan2(fused.final_orientation[1, 0],
                                         fused.final_orientation[0, 0])), 0.1)


class GyroDataTests(unittest.TestCase):
    def test_decoded_samples_preserve_gyro_and_legacy_missing_rows(self):
        sessions = data.sessions_from_records([
            (1, 10000, 0, 0, 9806650, None, None, None),
            (1, 20000, 0, 0, 9806650, 1000000, -2000000, 3000000),
        ])
        self.assertTrue(np.isnan(sessions[0].gyro[0]).all())
        np.testing.assert_allclose(sessions[0].gyro[1], [1, -2, 3])

    def test_csv_preserves_gyro_and_mixed_legacy_sessions(self):
        times = np.array([10000, 20000])
        acc = np.tile([0, 0, analysis.G], (2, 1))
        gyro = np.array([[0.1, -0.2, 0.3], [np.nan, np.nan, np.nan]])
        sessions = [data.Session(1, times, acc), data.Session(2, times, acc, gyro)]
        with tempfile.TemporaryDirectory() as directory:
            filename = Path(directory) / "motion.csv"
            data.save_csv(filename, sessions)
            self.assertEqual(tuple(filename.read_text().splitlines()[0].split(",")),
                             data.CSV_HEADER_WITH_GYRO)
            loaded = data.load_csv(filename)
        self.assertTrue(np.isnan(loaded[0].gyro).all())
        np.testing.assert_allclose(loaded[1].gyro, gyro, equal_nan=True)

    def test_v3_decode_csv_and_path_end_to_end(self):
        from tests.test_analysis import page
        from kartlog import device

        records = []
        for index in range(3):
            timestamps = tuple(range(index * 150 + 1, index * 150 + 151))
            records.extend(device.decode_page(
                page(index=index, timestamps=tuple(t * 10000 for t in timestamps), version=3),
                index))
        session = data.sessions_from_records(records)[0]
        with tempfile.TemporaryDirectory() as directory:
            filename = Path(directory) / "v3.csv"
            data.save_csv(filename, [session])
            loaded = data.load_csv(filename)[0]
        np.testing.assert_allclose(loaded.gyro, session.gyro, atol=1e-6)
        result = path.estimate(loaded, loaded.acc, forward_axis="+X")
        np.testing.assert_allclose(result.position, 0, atol=1e-10)

    def test_invalid_gyro_save_leaves_no_file(self):
        with tempfile.TemporaryDirectory() as directory:
            filename = Path(directory) / "bad.csv"
            for gyro in (np.full((2, 3), np.inf), np.zeros((1, 3))):
                session = data.Session(1, np.array([10000, 20000]), np.zeros((2, 3)), gyro)
                with self.assertRaises(ValueError):
                    data.save_csv(filename, [session])
                self.assertFalse(filename.exists())


if __name__ == "__main__":
    unittest.main()
