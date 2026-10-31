from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from PIL import Image

from kartlog import photo_ocr


def word(text, x, y, width=.06, confidence=.99):
    return photo_ocr.Word(text, x, y, width, .025, confidence)


class PhotoOCRTests(unittest.TestCase):
    def test_selects_karts_and_numbered_page_continuation(self):
        words = [word("18", .4, .05), word("4", .8, .05)]
        for index in range(8):
            y = .15 + index * .1
            words.extend([word(str(19 + index), .05, y),
                          word("25.313" if index == 0 else "22.622", .4, y),
                          word("23.218", .8, y)])
        columns = photo_ocr.columns(words)
        self.assertEqual(len(columns), 2)
        self.assertIn("kart 18", columns[0].label)
        self.assertIn("kart 4", columns[1].label)
        self.assertEqual(columns[0].lap_numbers, list(range(19, 27)))
        self.assertEqual(columns[1].lap_numbers, list(range(19, 27)))

    def test_suspicious_rows_are_retained_for_review(self):
        words = [word("22.879", .4, .1), word("22.550.", .4, .2),
                 word("22.O11", .4, .3), word("22.190", .4, .4)]
        column = photo_ocr.columns(words)[0]
        self.assertEqual([w.text for w in column.words],
                         ["22.879", "22.550.", "22.O11", "22.190"])
        with self.assertRaises(ValueError):
            photo_ocr.reviewed_rows("1 22.879\n2 22.550.")

    def test_review_and_merge_continuation_without_shifting(self):
        entries = ["22.879"] * 18 + [""] * 22
        rows = photo_ocr.reviewed_rows("19 25.313\n20 22.622\n21 22.513")
        merged = photo_ocr.merge_entries(entries, rows)
        self.assertEqual(merged[:18], entries[:18])
        self.assertEqual(merged[18:21], ["25.313", "22.622", "22.513"])
        self.assertEqual(merged[21:], [""] * 19)
        for text in ("19 25.313\n21 22.513", "19 25.313\n19 22.622",
                     "41 22.622", "0 22.622", "", "19 ?"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                photo_ocr.reviewed_rows(text)
        with self.assertRaisesRegex(ValueError, "earlier laps"):
            photo_ocr.merge_entries([""] * 40, rows)
        with self.assertRaises(ValueError):
            photo_ocr.merge_entries([""] * 40, [(41, 22000)])

    def test_tesseract_word_boxes_and_confidence(self):
        tsv = ("level\tleft\ttop\twidth\theight\tconf\ttext\n"
               "1\t0\t0\t1000\t1000\t-1\t\n"
               "5\t100\t200\t60\t25\t93.5\t22.879\n")
        words = photo_ocr.tesseract_words(tsv, 1000, 1000)
        self.assertEqual(words, [photo_ocr.Word("22.879", .1, .2, .06, .025, .935)])

    def test_native_and_cross_platform_routing(self):
        image = Image.new("RGB", (100, 100))
        payload = '[{"text":"22.879","x":0.1,"y":0.2,"width":0.3,"height":0.1,"confidence":0.9}]'
        for platform, engine, output in (
            ("darwin", "swift", payload),
            ("linux", "tesseract",
             "level\tleft\ttop\twidth\theight\tconf\ttext\n5\t10\t20\t30\t10\t90\t22.879\n"),
        ):
            with patch("kartlog.photo_ocr.sys.platform", platform), \
                    patch("kartlog.photo_ocr.shutil.which", return_value=f"/local/{engine}"), \
                    patch("kartlog.photo_ocr.subprocess.run",
                          return_value=subprocess.CompletedProcess([], 0, output, "")) as run:
                self.assertEqual(photo_ocr.recognize(image)[0].text, "22.879")
                self.assertEqual(run.call_args.args[0][0], f"/local/{engine}")
                self.assertTrue(run.call_args.kwargs["check"])

    def test_missing_engine_timeout_and_failure_are_explicit(self):
        image = Image.new("RGB", (100, 100))
        with patch("kartlog.photo_ocr.shutil.which", return_value=None):
            with self.assertRaises(ValueError):
                photo_ocr.recognize(image)
        for error in (subprocess.TimeoutExpired("OCR", 120),
                      subprocess.CalledProcessError(1, "OCR", stderr="recognition failed")):
            with patch("kartlog.photo_ocr.shutil.which", return_value="/local/ocr"), \
                    patch("kartlog.photo_ocr.subprocess.run", side_effect=error):
                with self.assertRaises(ValueError):
                    photo_ocr.recognize(image)

    def test_image_format_and_orientation(self):
        with tempfile.TemporaryDirectory() as directory:
            filename = Path(directory) / "photo.jpeg"
            image = Image.new("RGB", (20, 10))
            exif = Image.Exif()
            exif[274] = 6
            image.save(filename, exif=exif)
            self.assertEqual(photo_ocr.open_image(filename).size, (10, 20))
            png = filename.with_suffix(".png")
            image.save(png)
            self.assertEqual(photo_ocr.open_image(png).size, (20, 10))
            gif = filename.with_suffix(".gif")
            image.save(gif)
            with self.assertRaises(ValueError):
                photo_ocr.open_image(gif)


class PhotoImportTests(unittest.TestCase):
    def dialog(self, text, entries):
        from kartlog.photo_import import PhotoImportDialog

        class Variable:
            def __init__(self, value):
                self.value = value

            def get(self):
                return self.value

            def set(self, value):
                self.value = value

        dialog = object.__new__(PhotoImportDialog)
        session = SimpleNamespace(number=1)
        edited = []
        dialog.app = SimpleNamespace(
            session=session, external_time_vars=[Variable(value) for value in entries],
            _loading_external_times=False, _external_times_edited=lambda: edited.append(True))
        dialog.session = session
        dialog.busy = False
        dialog.preview = SimpleNamespace(get=lambda *_args: text)
        dialog.destroy = lambda: None
        return dialog, edited

    def test_import_continuation_and_confirm_replacement(self):
        from kartlog.photo_import import PhotoImportDialog
        dialog, edited = self.dialog("19 25.313\n20 22.622", ["22.879"] * 18 + [""] * 22)
        with patch("kartlog.photo_import.messagebox.askyesno", return_value=True), \
                patch("kartlog.photo_import.messagebox.showerror") as error:
            PhotoImportDialog.apply(dialog)
        self.assertEqual([v.get() for v in dialog.app.external_time_vars][18:20],
                         ["25.313", "22.622"])
        self.assertEqual(edited, [True])
        error.assert_not_called()
        dialog, edited = self.dialog("1 22.879", ["22.000"] + [""] * 39)
        with patch("kartlog.photo_import.messagebox.askyesno", return_value=False) as confirm:
            PhotoImportDialog.apply(dialog)
        self.assertIn("Replace existing entries", confirm.call_args.args[1])
        self.assertEqual(dialog.app.external_time_vars[0].get(), "22.000")
        self.assertEqual(edited, [])

    def test_invalid_preview_or_stale_session_cannot_modify_entries(self):
        from kartlog.photo_import import PhotoImportDialog
        for text in ("1 22.879.", "1 22.879\n3 22.190", "41 22.879"):
            dialog, edited = self.dialog(text, [""] * 40)
            with patch("kartlog.photo_import.messagebox.showerror") as error:
                PhotoImportDialog.apply(dialog)
            self.assertTrue(error.called)
            self.assertEqual(edited, [])
        dialog, edited = self.dialog("1 22.879", [""] * 40)
        dialog.app.session = SimpleNamespace(number=2)
        with patch("kartlog.photo_import.messagebox.showerror") as error:
            PhotoImportDialog.apply(dialog)
        self.assertIn("Session changed", error.call_args.args[1])
        self.assertEqual(edited, [])
