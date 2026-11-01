import tkinter as tk
import unittest
from unittest.mock import patch

import numpy as np
from matplotlib.colors import to_hex

from kartlog.gui import App
from .test_alignment import recording


class LapHighlightTests(unittest.TestCase):
    def test_selection_highlights_all_axes_without_changing_template(self):
        with patch("kartlog.gui.device.list_ports", return_value=[]), \
                patch("tkinter.messagebox.askyesno", return_value=True):
            try:
                app = App()
            except tk.TclError as exc:
                self.skipTest(f"Tk display unavailable: {exc}")
            app.withdraw()
            try:
                session, values, offset = recording()
                app.forward_var.set("+X")
                app.set_sessions([session], "highlight test", "demo")
                app.apply_alignment(values, offset)
                # External timing numbers need not equal their table row indices.
                for index, lap in enumerate(app.laps):
                    lap.external_number = index + 19
                app._refresh_lap_table()
                app.update()
                for mode in ("elapsed", "progress"):
                    app.lap_mode_var.set(mode)
                    app.lap_tree.selection_remove(*app.lap_tree.selection())
                    app._draw_lap_comparison()
                    medians = [ax.lines[len(app.laps)].get_ydata().copy() for ax in app.lap_axes]
                    app.lap_tree.selection_set("1")
                    app.update()
                    for ax, median in zip(app.lap_axes, medians):
                        selected = ax.lines[1]
                        self.assertEqual(to_hex(selected.get_color()), "#ff0000")
                        self.assertEqual(selected.get_linewidth(), 2.5)
                        self.assertEqual(selected.get_alpha(), 1)
                        self.assertGreater(selected.get_zorder(), ax.lines[len(app.laps)].get_zorder())
                        self.assertEqual(ax.lines[0].get_linewidth(), 0.8)
                        np.testing.assert_array_equal(ax.lines[len(app.laps)].get_ydata(), median)
                    self.assertEqual(app.lap_axes[0].lines[1].get_label(), "Lap 20")
                    app.lap_tree.selection_set("2")
                    app.update()
                    self.assertEqual(app.lap_axes[0].lines[1].get_linewidth(), 0.8)
                    self.assertEqual(to_hex(app.lap_axes[0].lines[2].get_color()), "#ff0000")
                    app.lap_tree.selection_remove("2")
                    app.update()
                    self.assertTrue(all(line.get_linewidth() == 0.8
                                        for line in app.lap_axes[0].lines[:len(app.laps)]))
                app.laps[1].include = False
                app._refresh_lap_table()
                app.lap_tree.selection_set("1")
                app.update()
                self.assertFalse(any(to_hex(line.get_color()) == "#ff0000"
                                     for line in app.lap_axes[0].lines))
            finally:
                app.destroy()
