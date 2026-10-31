import csv
import io
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch
import zlib

from tools.logger import (
    CSV_HEADER, ProtocolError, accel_micro, decode_page, gyro_micro, decimal_micro, download, read_line,
)


def page(index=0, session=1, timestamps=(10000, 20000), version=3):
    data = bytearray(b"\xff" * 4096)
    struct.pack_into("<IHHIIII", data, 0, 0x33474C41, version, len(timestamps),
                     session, index, 100, 4)
    if version == 2:
        struct.pack_into("<I", data, 24, 2000)
    elif version == 3:
        struct.pack_into("<IH", data, 24, 2000, 500)
    for i, timestamp in enumerate(timestamps):
        if version == 1:
            struct.pack_into("<Qiii", data, 32 + i * 20, timestamp, -1, 9806650, 0)
        elif version == 2:
            struct.pack_into("<Qiiiiii", data, 32 + i * 32, timestamp,
                             -1, 9806650, 0, 1500000, -2, 0)
        else:
            struct.pack_into("<Qhhhhhh", data, 32 + i * 20, timestamp,
                             -1, 500, 0, 100, -2, 0)
    struct.pack_into("<I", data, 4092, zlib.crc32(data[:-4]))
    return bytes(data)


class Port(io.BytesIO):
    def write(self, data):
        if data != b"DUMP\n":
            raise AssertionError(data)
        return len(data)

    def read(self, count=-1):
        return super().read(min(count, 17))


class LoggerTests(unittest.TestCase):
    def test_decode_units_and_timestamps(self):
        records = decode_page(page(), 0)
        self.assertEqual(records[0], (1, 10000, -19613, 9806650, 0, 106526, -2131, 0))
        self.assertEqual(decimal_micro(-1), "-0.000001")
        self.assertEqual(decimal_micro(9806650), "9.806650")

    def test_version_1_pages_have_no_gyro(self):
        records = decode_page(page(version=1), 0)
        self.assertEqual(records[0], (1, 10000, -1, 9806650, 0, None, None, None))
        self.assertEqual(len(decode_page(page(timestamps=range(1, 204), version=1), 0)), 203)
        data = bytearray(page(version=1))
        struct.pack_into("<H", data, 6, 204)
        struct.pack_into("<I", data, 4092, zlib.crc32(data[:-4]))
        with self.assertRaises(ProtocolError):
            decode_page(bytes(data), 0)

    def test_version_2_pages_keep_converted_values(self):
        records = decode_page(page(version=2), 0)
        self.assertEqual(records[0], (1, 10000, -1, 9806650, 0, 1500000, -2, 0))
        self.assertEqual(len(decode_page(page(timestamps=range(1, 127), version=2), 0)), 126)
        data = bytearray(page(version=2))
        struct.pack_into("<H", data, 6, 127)
        struct.pack_into("<I", data, 4092, zlib.crc32(data[:-4]))
        with self.assertRaises(ProtocolError):
            decode_page(bytes(data), 0)

    def test_raw_count_scaling(self):
        # +/-4 g: 500 counts per g. +/-2000 deg/s over 2^15 counts.
        self.assertEqual(accel_micro(500), 9806650)
        self.assertEqual(accel_micro(-2048), -40168038)
        self.assertAlmostEqual(gyro_micro(16384) / 1e6, 1000 * 3.141592653589793 / 180, places=6)
        self.assertAlmostEqual(gyro_micro(-32768) / 1e6, -2000 * 3.141592653589793 / 180, places=6)

    def test_full_page(self):
        self.assertEqual(len(decode_page(page(timestamps=range(1, 204)), 0)), 203)
        data = bytearray(page())
        struct.pack_into("<H", data, 6, 204)
        struct.pack_into("<I", data, 4092, zlib.crc32(data[:-4]))
        with self.assertRaises(ProtocolError):
            decode_page(bytes(data), 0)

    def test_wrong_accel_scale(self):
        data = bytearray(page())
        struct.pack_into("<H", data, 28, 235)
        struct.pack_into("<I", data, 4092, zlib.crc32(data[:-4]))
        with self.assertRaises(ProtocolError):
            decode_page(bytes(data), 0)

    def test_wrong_gyro_range(self):
        data = bytearray(page())
        struct.pack_into("<I", data, 24, 500)
        struct.pack_into("<I", data, 4092, zlib.crc32(data[:-4]))
        with self.assertRaises(ProtocolError):
            decode_page(bytes(data), 0)

    def test_crc_and_sequence_and_timestamp_checks(self):
        damaged = bytearray(page())
        damaged[40] ^= 1
        for data, index in (
            (damaged, 0), (page(), 1), (page(timestamps=(10, 10)), 0),
            (page(session=0), 0), (b"short", 0),
        ):
            with self.subTest(index=index, size=len(data)):
                with self.assertRaises(ProtocolError):
                    decode_page(data, index)
        self.assertEqual(decode_page(b"\xff" * 4096, 0), [])

    def test_download_multiple_sessions(self):
        port = Port(b"BEGIN 2\n" + page() + page(1, 2) + b"END\n")
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "data.csv"
            self.assertEqual(download(port, output), (4, 0))
            with output.open(newline="") as file:
                rows = list(csv.reader(file))
            self.assertEqual(tuple(rows[0]), CSV_HEADER)
            self.assertEqual(rows[1], ["1", "10000", "-0.019613", "9.806650", "0.000000",
                                       "0.106526", "-0.002131", "0.000000"])
            self.assertEqual(rows[3][0], "2")
            self.assertEqual(len(list(Path(directory).iterdir())), 1)

    def test_mixed_version_download(self):
        port = Port(b"BEGIN 2\n" + page(version=1) + page(1, 2) + b"END\n")
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "data.csv"
            self.assertEqual(download(port, output), (4, 0))
            with output.open(newline="") as file:
                rows = list(csv.reader(file))
            self.assertEqual(rows[1], ["1", "10000", "-0.000001", "9.806650", "0.000000", "", "", ""])
            self.assertEqual(rows[3][5], "0.106526")

    def test_incomplete_or_invalid_transfer_leaves_no_csv(self):
        streams = (
            b"BEGIN 1\n" + page()[:100],
            b"BEGIN 1\n" + page() + b"ERR\n",
            b"BEGIN 1\n" + b"\x00" * 4096 + b"END\n",
            b"BEGIN 2\n" + page() + page(1, 1) + b"END\n",
            b"BEGIN 999999\n",
            b"ERR recording\n",
        )
        for data in streams:
            with self.subTest(length=len(data)), tempfile.TemporaryDirectory() as directory:
                output = Path(directory) / "data.csv"
                with self.assertRaises(ProtocolError):
                    download(Port(data), output)
                self.assertEqual(list(Path(directory).iterdir()), [])

    def test_explicit_salvage(self):
        port = Port(b"BEGIN 2\n" + b"\x00" * 4096 + page(1, 2) + b"END\n")
        with tempfile.TemporaryDirectory() as directory, patch("sys.stderr", new_callable=io.StringIO) as warnings:
            self.assertEqual(download(port, Path(directory) / "data.csv", True), (2, 1))
            self.assertIn("CRC mismatch", warnings.getvalue())

    def test_no_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "data.csv"
            output.write_text("keep me")
            with self.assertRaises(FileExistsError):
                download(Port(b""), output)
            self.assertEqual(output.read_text(), "keep me")

    def test_line_bounds(self):
        for data in (b"x" * 256, b"\xff\n", b""):
            with self.assertRaises(ProtocolError):
                read_line(Port(data))


if __name__ == "__main__":
    unittest.main()
