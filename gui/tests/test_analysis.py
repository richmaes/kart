import io
from pathlib import Path
import struct
import tempfile
import unittest
import zlib

import numpy as np

from kartlog import analysis, data, device, synth


def page(index=0, session=1, timestamps=(10000, 20000), version=1):
    buf = bytearray(b"\xff" * 4096)
    struct.pack_into(
        "<IHHIIII", buf, 0, 0x33474C41, version, len(timestamps), session, index, 100, 4
    )
    if version == 2:
        struct.pack_into("<I", buf, 24, 2000)
    elif version >= 3:
        struct.pack_into("<IH", buf, 24, 2000, 500)
    for i, timestamp in enumerate(timestamps):
        if version == 1:
            struct.pack_into("<Qiii", buf, 32 + i * 20, timestamp, -1, 9806650, 0)
        elif version == 2:
            struct.pack_into(
                "<Qiiiiii", buf, 32 + i * 32,
                timestamp, -1, 9806650, 0, 1500000, -2, 0,
            )
        elif version == 3:
            struct.pack_into(
                "<Qhhhhhh", buf, 32 + i * 20,
                timestamp, -1, 500, 0, 100, -2, 0,
            )
        else:
            struct.pack_into("<Qhhhhhhh", buf, 32 + i * 22, timestamp,
                             -1, 500, 0, 100, -2, 0, 1024)
    struct.pack_into("<I", buf, 4092, zlib.crc32(buf[:-4]))
    return bytes(buf)


class Port(io.BytesIO):
    def write(self, payload):
        assert payload == b"DUMP\n", payload
        return len(payload)


class CalibrationTests(unittest.TestCase):
    def setUp(self):
        self.session = synth.synthetic_session()
        self.t = self.session.t

    def test_up_vector_recovered_from_rest(self):
        cal = analysis.calibrate(self.t, self.session.acc, 0.5, 2.0, "-Y")
        np.testing.assert_allclose(cal.up, synth.MOUNT[:, 2], atol=0.01)
        self.assertAlmostEqual(cal.magnitude_g, 1.0, places=2)
        self.assertAlmostEqual(cal.tilt_deg, 7.2, delta=0.3)

    def test_kart_frame_matches_truth(self):
        _, truth = synth.kart_profile(rng=np.random.default_rng(1))
        cal = analysis.calibrate(self.t, self.session.acc, 0.5, 2.0, "-Y")
        kart = analysis.smooth(analysis.to_kart_g(self.session.acc, cal), 0.5)
        ref = analysis.smooth(truth, 0.5)
        self.assertLess(np.sqrt(np.mean((kart - ref) ** 2)), 0.05)

    def test_wrong_forward_axis_fixed_by_launch_yaw(self):
        cal = analysis.calibrate(self.t, self.session.acc, 0.5, 2.0, "+X")
        yaw, launch = analysis.estimate_launch_yaw(
            self.t, analysis.to_kart_g(self.session.acc, cal), after_s=2.5)
        self.assertAlmostEqual(launch, 4.0, delta=0.3)
        self.assertAlmostEqual(yaw, -90.0, delta=3.0)
        fixed = analysis.calibrate(self.t, self.session.acc, 0.5, 2.0, "+X", yaw)
        good = analysis.calibrate(self.t, self.session.acc, 0.5, 2.0, "-Y")
        np.testing.assert_allclose(fixed.rotation, good.rotation, atol=0.05)

    def test_find_rest_and_normalize(self):
        start, dur = analysis.find_rest(self.t, self.session.acc)
        self.assertLess(start, 0.1)
        self.assertAlmostEqual(dur, 3.0, delta=0.01)  # capped at max_s
        scaled = data.Session(1, self.session.time_us, self.session.acc * 1.13)
        cal = analysis.calibrate(scaled.t, scaled.acc, start, dur, "-Y", normalize=True)
        rest = analysis.to_kart_g(scaled.acc[:100], cal)
        self.assertAlmostEqual(rest[:, 2].mean(), 1.0, places=2)

    def test_measured_rate(self):
        t = np.arange(100) * 0.01278
        self.assertAlmostEqual(analysis.sample_rate(t), 78.25, places=1)

    def test_vertical_forward_axis_rejected(self):
        with self.assertRaises(ValueError):
            analysis.calibrate(self.t, self.session.acc, 0.5, 2.0, "+Z")

    def test_summary_signs(self):
        cal = analysis.calibrate(self.t, self.session.acc, 0.5, 2.0, "-Y")
        s = analysis.summarize(self.t, analysis.smooth(analysis.to_kart_g(self.session.acc, cal), 0.5))
        self.assertGreater(s["max_accel"][0], 0.3)
        self.assertLess(s["max_brake"][0], -0.7)
        self.assertGreater(s["max_left"][0], 0.9)
        self.assertLess(s["max_right"][0], -1.1)


class DataTests(unittest.TestCase):
    def test_csv_round_trip_and_no_overwrite(self):
        sessions = [synth.synthetic_session(1, laps=1), synth.synthetic_session(3, laps=1, seed=2)]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "x.csv"
            data.save_csv(path, sessions)
            loaded = data.load_csv(path)
            with self.assertRaises(FileExistsError):
                data.save_csv(path, sessions)
        self.assertEqual([s.number for s in loaded], [1, 3])
        np.testing.assert_array_equal(loaded[0].time_us, sessions[0].time_us)
        np.testing.assert_allclose(loaded[1].acc, sessions[1].acc, atol=1e-6)

    def test_gaps(self):
        s = data.Session(1, np.array([0, 10000, 20000, 60000]), np.zeros((4, 3)))
        self.assertEqual(s.gaps(), [(0.02, 40.0)])

    def test_load_logger_csv_with_gyro_columns_and_blank_legacy_gyro(self):
        contents = (
            "session,time_us,ax_m_s2,ay_m_s2,az_m_s2,gx_rad_s,gy_rad_s,gz_rad_s\n"
            "1,10000,1.000000,2.000000,3.000000,,,\n"
            "1,20000,4.000000,5.000000,6.000000,0.100000,-0.200000,0.300000\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "logger.csv"
            path.write_text(contents, encoding="ascii")
            loaded = data.load_csv(path)
        self.assertEqual(len(loaded), 1)
        np.testing.assert_allclose(loaded[0].acc, [[1, 2, 3], [4, 5, 6]])


class DeviceTests(unittest.TestCase):
    def test_download_from_port(self):
        port = Port(b"BEGIN 3\n" + page() + b"\xff" * 4096 + page(2, 2) + b"END\n")
        records, warnings = device.download_from(port)
        self.assertEqual(warnings, [])
        sessions = data.sessions_from_records(records)
        self.assertEqual([s.number for s in sessions], [1, 2])
        self.assertAlmostEqual(sessions[0].acc[0, 1], 9.80665)

    def test_decode_versions_1_2_and_3(self):
        expected = {
            1: (1, 10000, -1, 9806650, 0, None, None, None),
            2: (1, 10000, -1, 9806650, 0, 1500000, -2, 0),
            3: (1, 10000, -19613, 9806650, 0, 106526, -2131, 0),
        }
        for version, record in expected.items():
            with self.subTest(version=version):
                self.assertEqual(device.decode_page(page(version=version), 0)[0], record)

    def test_v3_gyro_rows_are_ignored_by_accelerometer_analysis(self):
        records = device.decode_page(page(version=3), 0)
        sessions = data.sessions_from_records(records)
        self.assertAlmostEqual(sessions[0].acc[0, 1], 9.80665)

    def test_skip_invalid(self):
        bad = bytearray(page(1, 1, (30000,)))
        bad[40] ^= 1
        port = Port(b"BEGIN 2\n" + page() + bytes(bad) + b"END\n")
        with self.assertRaises(device.ProtocolError):
            device.download_from(Port(port.getvalue()))
        records, warnings = device.download_from(port, skip_invalid=True)
        self.assertEqual(len(records), 2)
        self.assertEqual(len(warnings), 1)


if __name__ == "__main__":
    unittest.main()
