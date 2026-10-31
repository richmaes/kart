from pathlib import Path
import tempfile
import unittest

import numpy as np

from kartlog import sensorcal
from kartlog.analysis import G

OFFSET = np.array([0.04, 0.12, -0.03]) * G  # m/s^2
GAIN = np.array([1.02, 1.01, 0.97])
FACES = ("+X", "-X", "+Y", "-Y", "+Z", "-Z")


def tilt(v, deg):
    """Tilt a vector slightly about an axis perpendicular to it (uneven table)."""
    axis = np.cross(v, [1.0, 1.0, 1.0])
    axis /= np.linalg.norm(axis)
    a = np.radians(deg)
    return v * np.cos(a) + np.cross(axis, v) * np.sin(a)


def recording(faces, table_tilt_deg=0.0, rate=78.2, seed=0):
    """Rest 5 s on each face with 2 s of handling motion between faces."""
    rng = np.random.default_rng(seed)
    chunks = []
    for k, face in enumerate(faces):
        true = tilt(sensorcal.face_vector(face), table_tilt_deg) * G
        n = int(5 * rate)
        chunks.append(np.tile(true, (n, 1)) + rng.normal(0, 0.003 * G, (n, 3)))
        if k < len(faces) - 1:
            m = int(2 * rate)
            chunks.append(rng.normal(0, 0.5 * G, (m, 3)))
    true = np.vstack(chunks)
    t = np.arange(len(true)) / rate
    return t, true * GAIN + OFFSET


class SensorCalTests(unittest.TestCase):
    def test_six_faces_recover_offset_and_gain_despite_table_tilt(self):
        t, acc = recording(FACES, table_tilt_deg=1.5)
        segs = sensorcal.segments(t, acc)
        self.assertEqual([s["face"] for s in segs], list(FACES))
        cal, residuals = sensorcal.fit(segs)
        self.assertEqual(cal.method, "sphere")
        self.assertTrue(all(cal.solved))
        np.testing.assert_allclose(cal.offset, OFFSET, atol=0.002 * G)
        np.testing.assert_allclose(cal.gain, GAIN, atol=0.002)
        self.assertLess(max(abs(r) for r in residuals), 0.002)

    def test_three_faces_with_tilt_use_face_fit(self):
        t, acc = recording(("+X", "+Y", "+Z"), table_tilt_deg=1.0)
        cal, _ = sensorcal.fit(sensorcal.segments(t, acc))
        self.assertEqual(cal.method, "faces")
        # Tilt leaks in: about 1.3 % gain and 12 mg offset per degree.
        np.testing.assert_allclose(cal.gain, GAIN, atol=0.02)
        np.testing.assert_allclose(cal.offset, OFFSET, atol=0.02 * G)

    def test_three_faces_solve_every_axis(self):
        t, acc = recording(("+X", "+Y", "+Z"))
        cal, _ = sensorcal.fit(sensorcal.segments(t, acc))
        self.assertTrue(all(cal.solved))
        np.testing.assert_allclose(cal.offset, OFFSET, atol=0.002 * G)
        np.testing.assert_allclose(cal.gain, GAIN, atol=0.002)

    def test_two_faces_flag_unsolved_axis(self):
        t, acc = recording(("+Z", "+Y"))
        cal, _ = sensorcal.fit(sensorcal.segments(t, acc))
        self.assertEqual(cal.solved, [False, True, True])
        self.assertAlmostEqual(cal.offset[0], OFFSET[0], delta=0.002 * G)  # X offset still known
        self.assertEqual(cal.gain[0], 1.0)

    def test_apply_corrects_check_recording(self):
        cal, _ = sensorcal.fit(sensorcal.segments(*recording(FACES)))
        t, acc = recording(("+Z", "-Y"), seed=5)
        check = sensorcal.check(cal, sensorcal.segments(t, acc))
        self.assertLess(max(abs(r) for r in check), 0.002)

    def test_store_round_trip(self):
        cal, _ = sensorcal.fit(sensorcal.segments(*recording(FACES)))
        cal.name = "kart 1"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "calibration.json"
            sensorcal.Store(path).put("ABC123", cal)
            loaded = sensorcal.Store(path).get("ABC123")
            csv = Path(directory) / "kart-1.csv"
            sensorcal.write_meta(csv, serial="ABC123")
            self.assertEqual(sensorcal.read_meta(csv), {"serial": "ABC123"})
        self.assertEqual(loaded.name, "kart 1")
        np.testing.assert_allclose(loaded.gain, cal.gain)


if __name__ == "__main__":
    unittest.main()
