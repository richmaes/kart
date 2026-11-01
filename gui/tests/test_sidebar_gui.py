import tkinter as tk
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from kartlog.gui import App


class SidebarTests(unittest.TestCase):
    def test_sidebar_scrollbar_wheel_scope_and_resize(self):
        with patch("kartlog.gui.device.list_ports", return_value=[]):
            try:
                app = App()
            except tk.TclError as exc:
                self.skipTest(f"Tk display unavailable: {exc}")
            try:
                app.geometry("1100x500")
                app.update()
                canvas = app.controls_canvas
                self.assertLess(canvas.yview()[1], 1)
                self.assertTrue(app.controls_scrollbar.winfo_ismapped())
                original_unit = app.unit_var.get()
                # Dispatch actual wheel events over a child with its own wheel binding.
                app.unit_box.event_generate("<MouseWheel>", delta=-120)
                app.update()
                self.assertGreater(canvas.yview()[0], 0)
                self.assertEqual(app.unit_var.get(), original_unit)
                canvas.yview_moveto(0)
                app._scroll_controls(SimpleNamespace(num=5, delta=0))
                self.assertGreater(canvas.yview()[0], 0)
                app._scroll_controls(SimpleNamespace(num=4, delta=0))
                self.assertAlmostEqual(canvas.yview()[0], 0, delta=0.02)
                before = canvas.yview()
                app._scroll_controls(SimpleNamespace(num=None, delta=0))
                self.assertEqual(canvas.yview(), before)
                app.summary.event_generate("<MouseWheel>", delta=-120)
                app.update()
                self.assertGreater(canvas.yview()[0], 0)
                tag = next(t for t in app.unit_box.bindtags() if t.startswith("SidebarWheel:"))
                self.assertNotIn(tag, app.ts_canvas.get_tk_widget().bindtags())
                canvas.yview_moveto(1)
                app.update()
                frame = canvas.nametowidget(canvas.itemcget(canvas.find_all()[0], "window"))
                self.assertGreaterEqual(float(canvas.itemcget(canvas.find_all()[0], "height")),
                                        frame.winfo_reqheight())
                app.geometry("1100x1600")
                app.update()
                self.assertAlmostEqual(canvas.yview()[1], 1, delta=0.02)
            finally:
                app.destroy()
