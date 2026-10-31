from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from kartlog import laptimes


class LapTimeTests(unittest.TestCase):
    def test_exact_milliseconds_and_minutes(self):
        for text, expected in (("42.31", 42310), ("1:02.31", 62310), (" 42.3 ", 42300),
                               ("42", 42000), ("0.01", 10), ("0:00.001", 1),
                               ("22.879", 22879)):
            self.assertEqual(laptimes.parse(text), expected)
            self.assertEqual(laptimes.parse(laptimes.format_time(expected)), expected)
        self.assertEqual(sum(laptimes.parse_entries(["42.31", "41.86", "42.07"])), 126240)

    def test_invalid_and_excess_precision(self):
        for text in ("0", "0.00", "-1", "nan", "inf", "42.3111", "1:60.00",
                     "1:2.31", "1:02:03", "forty", "1e2", ".5"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                laptimes.parse(text)

    def test_exact_limit_and_trailing_blanks(self):
        self.assertEqual(len(laptimes.parse_entries(["42.31"] * 40)), 40)
        self.assertEqual(laptimes.parse_entries(["42.31", "", " "]), [42310])
        self.assertEqual(laptimes.parse_entries([""] * 40), [])
        with self.assertRaisesRegex(ValueError, "at most 40"):
            laptimes.parse_entries(["42.31"] * 41)
        with self.assertRaisesRegex(ValueError, "Lap 3"):
            laptimes.parse_entries(["42.31", "", "42.00"])

    def test_sidecar_roundtrip_preserves_recording_and_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            recording = Path(directory) / "session.csv"
            recording.write_text("CSV unchanged")
            metadata = recording.with_suffix(".meta.json")
            metadata.write_text('{"serial": "test"}')
            self.assertEqual(laptimes.load(recording), {})
            times = {"1": [4231, 4186], "2": [1234] * 40}
            laptimes.save(recording, times)
            self.assertEqual(laptimes.load(recording), times)
            self.assertEqual(recording.read_text(), "CSV unchanged")
            self.assertEqual(metadata.read_text(), '{"serial": "test"}')
            laptimes.save(recording, {"1": [], "2": [1234] * 40})
            self.assertEqual(laptimes.load(recording)["1"], [])

    def test_invalid_sidecar_and_failed_write(self):
        with tempfile.TemporaryDirectory() as directory:
            recording = Path(directory) / "session.csv"
            for payload in (
                '{"version": 99, "sessions": {}}', '{"version": 1, "sessions": []}',
                '{"version": 1, "sessions": {"1": [0]}}',
                '{"version": 1, "sessions": {"1": [42.31]}}',
                '{"version": 1, "sessions": {"1": [true]}}',
                '{"version": 1, "sessions": {"-1": [100]}}',
                '{broken',
            ):
                laptimes.sidecar(recording).write_text(payload)
                with self.assertRaises(ValueError):
                    laptimes.load(recording)
            laptimes.save(recording, {"1": [4231]})
            original = laptimes.sidecar(recording).read_bytes()
            with patch.object(Path, "replace", side_effect=OSError("disk failure")):
                with self.assertRaisesRegex(OSError, "disk failure"):
                    laptimes.save(recording, {"1": [4186]})
            self.assertEqual(laptimes.sidecar(recording).read_bytes(), original)
            self.assertEqual(list(Path(directory).iterdir()), [laptimes.sidecar(recording)])
            with self.assertRaises(ValueError):
                laptimes.save(recording, {"1": [100] * 41})

    def test_legacy_hundredths_migrate_without_value_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            recording = Path(directory) / "legacy.csv"
            filename = laptimes.sidecar(recording)
            filename.write_text('{"version": 1, "sessions": {"1": [4231, 4186], "2": []}}')
            migrated = laptimes.load(recording)
            self.assertEqual(migrated, {"1": [42310, 41860], "2": []})
            self.assertEqual(laptimes.format_time(migrated["1"][0]), "42.310")
            laptimes.save(recording, migrated)
            self.assertIn('"version": 2', filename.read_text())
            self.assertEqual(laptimes.load(recording), migrated)
