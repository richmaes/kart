import tkinter as tk
import unittest
from unittest.mock import patch

import numpy as np

from kartlog import analysis
from kartlog.data import Session
from kartlog.gui import App
from kartlog.launch_dialog import LaunchDialog


def launch_session(sign=-1):
    t = np.arange(0, 15, 0.01)
    acc = np.tile([0., -analysis.G, 0.], (len(t), 1))
    acc[t >= 7, 2] += sign * 0.25 * analysis.G
    return Session(1, np.rint(t * 1e6).astype(np.int64), acc)


class LaunchTests(unittest.TestCase):
    def test_signed_direction_reference_and_no_raw_changes(self):
        for sign, axis in ((-1, "-Z"), (1, "+Z")):
            s = launch_session(sign)
            before = s.acc.copy()
            result = analysis.inspect_launch(s.t, s.acc, 1, 2)
            self.assertAlmostEqual(result["time_s"], 7, delta=0.16)
            self.assertEqual(result["axis"], axis)
            self.assertAlmostEqual(result["yaw_deg"], 0)
            self.assertGreater(sign * result["vector_g"][2], 0.2)
            np.testing.assert_array_equal(s.acc, before)

    def test_transient_gap_no_launch_and_invalid_inputs(self):
        s = launch_session()
        s.acc[400:405, 0] += 4 * analysis.G
        self.assertAlmostEqual(analysis.inspect_launch(s.t, s.acc, 1, 2)["time_s"], 7, delta=0.16)
        mask = ~((s.t > 7.3) & (s.t < 7.6))
        with self.assertRaisesRegex(ValueError, "gap"):
            analysis.inspect_launch(s.t[mask], s.acc[mask], 1, 2)
        for settings in ({"threshold_g": 0}, {"hold_s": np.nan}, {"window_s": -1},
                         {"ref_duration": 0}):
            kwargs = dict(ref_start=1, ref_duration=2)
            kwargs.update(settings)
            with self.subTest(settings=settings), self.assertRaises(ValueError):
                analysis.inspect_launch(s.t, s.acc, **kwargs)
        s.acc[:] = [0, -analysis.G, 0]
        with self.assertRaisesRegex(ValueError, "No sustained"):
            analysis.inspect_launch(s.t, s.acc, 1, 2)

    def test_gui_preview_refresh_and_confirmed_apply(self):
        with patch("kartlog.gui.device.list_ports", return_value=[]), \
                patch("tkinter.messagebox.askyesno", return_value=True):
            try:
                app = App()
            except tk.TclError as exc:
                self.skipTest(f"Tk display unavailable: {exc}")
            app.withdraw()
            try:
                self.assertEqual(app.forward_var.get(), "+Z")
                self.assertEqual(app.yaw_var.get(), 0)
                s = launch_session()
                app.set_sessions([s], "launch test", "demo")
                app.forward_var.set("+Z")
                app.ref_start.set(1)
                app.ref_dur.set(2)
                app.update_all()
                dialog = LaunchDialog(app)
                dialog.withdraw()
                self.assertEqual(dialog.result["axis"], "-Z")
                self.assertEqual(app.forward_var.get(), "+Z")
                self.assertEqual(len(dialog.axes), 3)
                lo, hi = dialog.axes[-1].get_xlim()
                self.assertLess(lo, 7)
                self.assertGreater(hi, 8)
                dialog.variables["threshold_g"].set("bad")
                self.assertIsNone(dialog.result)
                dialog.find()
                self.assertIn("Cannot inspect", dialog.status.get())
                dialog.variables["threshold_g"].set(".12")
                dialog.find()
                with patch("tkinter.messagebox.askyesno", return_value=False):
                    dialog.apply()
                self.assertEqual(app.forward_var.get(), "+Z")
                dialog.apply()
                self.assertEqual(app.forward_var.get(), "-Z")
                self.assertFalse(dialog.winfo_exists())
                np.testing.assert_array_equal(s.acc, launch_session().acc)
            finally:
                app.destroy()
