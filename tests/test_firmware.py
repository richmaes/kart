from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from tools.logger import decode_page

ROOT = Path(__file__).resolve().parents[1]


class FirmwareTests(unittest.TestCase):
    def test_storage_and_button_state_machine(self):
        compiler = shutil.which("cc")
        if compiler is None:
            self.fail("A host C compiler (cc) is required for firmware unit tests")
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / "firmware-tests"
            fixture = Path(directory) / "page.bin"
            subprocess.run([
                compiler, "-std=c11", "-Wall", "-Wextra", "-Werror",
                "-I", str(ROOT / "tests/fakes"),
                "-I", str(ROOT / "src"),
                str(ROOT / "tests/test_firmware.c"),
                str(ROOT / "src/storage.c"),
                str(ROOT / "src/writer.c"),
                str(ROOT / "src/button.c"), "-o", str(binary),
            ], check=True)
            subprocess.run([str(binary), str(fixture)], check=True)
            records = decode_page(fixture.read_bytes(), 0)
            self.assertEqual(len(records), 184)
            # Raw counts accel (-1, 500, 0) and gyro (100, -2, 7), converted by the host.
            self.assertEqual(records[0], (1, 10000, -19613, 9806650, 0, 106526, -2131, 7457,
                                          25000000))
            self.assertEqual(records[-1][1], 1840000)
            # Cross the production firmware -> CLI -> GUI decoding boundary.
            import sys
            sys.path.insert(0, str(ROOT / "gui"))
            from kartlog import data, device
            self.assertEqual(device.decode_page(fixture.read_bytes(), 0), records)
            session = data.sessions_from_records(records)[0]
            self.assertTrue((session.temperature == 25).all())
            csv_file = Path(directory) / "temperature.csv"
            data.save_csv(csv_file, [session])
            self.assertTrue((data.load_csv(csv_file)[0].temperature == 25).all())

    def test_spi_temperature_protocol(self):
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / "temperature-tests"
            subprocess.run([
                "cc", "-std=c11", "-Wall", "-Wextra", "-Werror",
                "-I", str(ROOT / "tests/fakes"), "-I", str(ROOT / "src"),
                str(ROOT / "tests/test_temperature.c"),
                str(ROOT / "src/imu_temperature.c"), "-o", str(binary),
            ], check=True)
            subprocess.run([str(binary)], check=True)


if __name__ == "__main__":
    unittest.main()
