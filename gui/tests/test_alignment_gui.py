from pathlib import Path
import tempfile
import time
import tkinter as tk
import unittest
from unittest.mock import patch

from kartlog import data, laps, laptimes
from kartlog.alignment_dialog import AlignmentDialog
from kartlog.gui import App

from .test_alignment import recording


class AlignmentGuiTests(unittest.TestCase):
    def test_search_confirmation_export_reload_and_duration_edits(self):
        errors = []
        with tempfile.TemporaryDirectory() as directory, \
                patch("kartlog.gui.device.list_ports", return_value=[]), \
                patch("tkinter.messagebox.showerror", side_effect=lambda *a, **k: errors.append(a)), \
                patch("tkinter.messagebox.showwarning"), \
                patch("tkinter.messagebox.askyesno", return_value=True):
            try:
                app = App()
            except tk.TclError as exc:
                self.skipTest(f"Tk display unavailable: {exc}")
            app.withdraw()
            try:
                session, values, expected = recording()
                second = data.Session(2, session.time_us.copy(), session.acc.copy(), session.gyro.copy())
                csv = Path(directory) / "session.csv"
                data.save_csv(csv, [session, second])
                app.set_sessions([session, second], "alignment test", "demo",
                                 recording_path=csv, external_times={"1": values, "2": values})
                original = laps.Lap(2000000, 3000000)
                app.laps = [original]
                dialog = AlignmentDialog(app, values)
                dialog.withdraw()
                dialog.find()
                deadline = time.monotonic() + 15
                while dialog.busy and time.monotonic() < deadline:
                    app.update()
                    time.sleep(0.01)
                self.assertFalse(dialog.busy, "Background search did not complete")
                self.assertTrue(dialog.candidates, errors)
                self.assertLess(abs(dialog.candidates[0].offset_s - expected), 0.15)
                dialog.offset.set(str(expected))
                self.assertTrue(dialog.apply_button.instate(["disabled"]))
                dialog.preview()
                self.assertTrue(dialog.apply_button.instate(["!disabled"]))
                with patch("tkinter.messagebox.askyesno", return_value=False):
                    dialog.apply()
                self.assertEqual(app.laps, [original])
                self.assertFalse(laptimes.sidecar(csv).exists())
                with patch.object(Path, "replace", side_effect=OSError("disk full")):
                    dialog.apply()
                self.assertIn("disk full", errors[-1][1])
                self.assertEqual(app.laps, [original])
                errors.clear()
                dialog.apply()
                self.assertFalse(dialog.winfo_exists())
                self.assertEqual(len(app.laps), 5)
                app.session_box.current(1)
                app.select_session()
                app.apply_alignment(values, expected)
                self.assertEqual(set(laptimes.load_alignments(csv)), {"1", "2"})
                with patch("kartlog.gui.filedialog.askopenfilename", return_value=str(csv)):
                    app.open_csv()
                self.assertEqual(len(app.laps), 5)
                self.assertEqual(app.laps[0].external_number, 1)
                exported = Path(directory) / "copy.csv"
                with patch("kartlog.gui.filedialog.asksaveasfilename", return_value=str(exported)):
                    app.save_csv()
                self.assertEqual(laptimes.load_alignments(exported), laptimes.load_alignments(csv))
                self.assertTrue(app._forget_alignment())
                self.assertEqual(set(laptimes.load_alignments(exported)), {"2"})
                app.apply_alignment(values, expected)
                app.external_time_vars[0].set("10.500")
                app.save_external_times()
                self.assertEqual(app.laps, [])
                self.assertEqual(set(app.alignments), {"2"})
                self.assertEqual(set(laptimes.load_alignments(exported)), {"2"})
                self.assertEqual(laptimes.load(exported)["1"][0], 10500)
                self.assertEqual(errors, [])
            finally:
                app.destroy()
